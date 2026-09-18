// Test the unchanged external converter against Zigbee2MQTT 2.12.0 dependencies
// and the production C++ receiver/LCD driver. Only radio delivery is simulated.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {copyFile, mkdtemp, rm} from 'node:fs/promises';
import {createInterface} from 'node:readline';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {Controller, Zcl} from 'zigbee-herdsman';
import {Device} from 'zigbee-herdsman/dist/controller/model/device.js';
import {Endpoint} from 'zigbee-herdsman/dist/controller/model/endpoint.js';
import {prepareDefinition} from 'zigbee-herdsman-converters';

const root = fileURLToPath(new URL('../../', import.meta.url));
const runtime = await mkdtemp(new URL('./.runtime-', import.meta.url));
await copyFile(`${root}/zigbee2mqtt/gtag-display.mjs`, `${runtime}/converter.mjs`);
const converter = await import(pathToFileURL(`${runtime}/converter.mjs`));
const {frameCluster, encodeFrame, crc32, decodeFrameInput, transferFrame, testImage, frameConverter,
    powerConverter, parsePowerDiagnostics, confirmFreshness, freshnessConverter, parseReply} = converter;
const child = spawn(process.env.PYTHON || 'python3', ['tests/zigbee/native_bridge.py'],
    {cwd: root, stdio: ['pipe', 'pipe', 'inherit']});
const lines = createInterface({input: child.stdout})[Symbol.asyncIterator]();
const request = async (command, data = {}) => {
    child.stdin.write(`${JSON.stringify({command, ...data})}\n`);
    const line = await lines.next();
    assert.equal(line.done, false, 'C++ bridge stopped unexpectedly');
    return JSON.parse(line.value);
};
const custom = {gtagFrame: frameCluster};
const noSleep = async () => {};
let passed = 0;
const test = async (name, fn) => {
    await request('reset');
    await fn();
    console.log(`ok ${++passed} - ${name}`);
};

// Matches Endpoint.command's return shape: a decoded ZCL payload object.
function endpoint(fault = async () => {}) {
    let sequence = 0;
    return {
        supportsInputCluster: (id) => id === frameCluster.ID,
        async command(cluster, command, payload, options) {
            assert.equal(options.disableDefaultResponse, true);
            assert.equal(options.timeout, 8000);
            assert.equal(options.sendPolicy, 'immediate');
            const frame = Zcl.Frame.create(Zcl.FrameType.SPECIFIC, Zcl.Direction.CLIENT_TO_SERVER,
                true, undefined, sequence++ % 256, command, cluster, payload, custom);
            assert.equal(frame.command.response, 0x80);
            assert.equal(frame.command.name, 'packet');
            assert.equal(frame.cluster.name, 'gtagFrame');
            const wire = frame.toBuffer();
            assert.ok(wire.length <= 43, 'Must fit one small Zigbee packet');
            assert.equal(wire[3], wire.length - 4, 'ZCL octet-string prefix');
            const packet = Buffer.from(wire.subarray(4));
            await fault('before', packet);
            const result = await request('packet', {hex: packet.toString('hex')});
            const ack = Buffer.from(result.hex, 'hex');
            await fault('after', packet, ack);
            const reply = Buffer.concat([Buffer.from([0x19, wire[1], 0x80, 20]), ack]);
            return Zcl.Frame.fromBuffer(frameCluster.ID, Zcl.Header.fromBuffer(reply), reply, custom).payload;
        },
    };
}
const verify = async (raw, frames = 1) => {
    const shown = await request('inspect');
    assert.equal(shown.raw, raw.toString('hex'), 'Actual 4096 LCD bytes must match');
    assert.equal(shown.frames, frames, 'No duplicate rendering');
    assert.equal(shown.advertising, 0, 'Zigbee must not start Bluetooth');
};
try {
    assert.equal((await lines.next()).value, 'ready');
    await test('expiry inverts pixels, short confirmation restores them, replay cannot renew', async () => {
        const ep = endpoint();
        const raw = testImage();
        const result = await transferFrame(ep, raw, {session: 99, freshnessTimeout: 60, sleep: noSleep});
        assert.equal(result.freshnessTimeout, 60);
        await verify(raw);
        await request('advance', {ms: 60_000});
        const stale = await request('inspect');
        assert.notEqual(stale.raw, raw.toString('hex'));
        assert.equal(stale.frames, 2);
        const query = Buffer.alloc(5); query[0] = 4;
        assert.equal(parseReply((await ep.command('gtagFrame', 'packet', {payload: query},
            {disableDefaultResponse: true, timeout: 8000, sendPolicy: 'immediate'})).payload, 4).stale, true);
        const message = {frame_id: 99, crc: crc32(raw), sequence: 1};
        await assert.rejects(confirmFreshness(ep, {...message, frame_id: 100}));
        await confirmFreshness(ep, message);
        await verify(raw, 3);
        await request('advance', {ms: 30_000});
        await confirmFreshness(ep, message); // Duplicate cannot restart the timer.
        await request('advance', {ms: 31_000});
        assert.equal((await request('inspect')).frames, 4);
        const states = [];
        const meta = {device: {ieeeAddr: '0x1234', endpoints: [ep]}, publish: (v) => states.push(v)};
        await freshnessConverter.convertSet(null, 'frame_freshness',
            {...message, sequence: 2, request_id: 'fresh-2'}, meta);
        assert.equal(states.at(-1).freshness_status, 'confirmed');
        assert.equal(states.at(-1).freshness_request_id, 'fresh-2');
        await verify(raw, 5);
        await assert.rejects(freshnessConverter.convertSet(null, 'frame_freshness',
            {...message, sequence: 1, request_id: 'old'}, meta));
        assert.equal(states.at(-1).freshness_status, 'error');
        await request('reset');
        await assert.rejects(confirmFreshness(ep, {...message, sequence: 3}));
    });
    await test('definition loads with pinned Zigbee2MQTT converter dependencies', async () => {
        const definition = prepareDefinition(converter.default);
        assert.ok(definition.toZigbee.includes(frameConverter));
        const properties = definition.exposes.map((expose) => expose.property);
        for (const property of ['test_image', 'frame', 'frame_status', 'frame_error', 'battery_voltage_1', 'display_pattern_3'])
            assert.ok(properties.includes(property), property);
        assert.equal(crc32(Buffer.from('123456789')), 0xcbf43926);
    });
    await test('real herdsman controller dispatches the custom reply without crashing', async () => {
        // Decoding with Frame.fromBuffer alone misses command/cluster name
        // requirements used by the controller's asynchronous message handler.
        const packet = Buffer.alloc(24);
        packet.set([0x19, 42, 0x80, 20, 1, 4]);
        const device = {
            customClusters: custom, updateLastSeen() {}, implicitCheckin() {},
            getEndpoint: () => ({}), onZclData: async () => {},
        };
        const events = [];
        const controller = {selfAndDeviceEmit: (device, event, data) => events.push({event, data})};
        const originalFind = Device.find;
        Device.find = () => device;
        try {
            await Controller.prototype.onZclPayload.call(controller, {
                clusterID: frameCluster.ID, address: 0x1234, endpoint: 4,
                destinationEndpoint: 1, groupID: 0, linkquality: 170,
                header: Zcl.Header.fromBuffer(packet), data: packet,
            });
        } finally {
            Device.find = originalFind;
        }
        const message = events.find((event) => event.event === 'message').data;
        assert.equal(message.type, 'commandReply');
        assert.equal(message.cluster, 'gtagFrame');
        assert.deepEqual(message.data.payload, packet.subarray(4));
    });
    await test('base64 rejects malformed and noncanonical input', async () => {
        const raw = testImage();
        assert.deepEqual(decodeFrameInput(raw.toString('base64')), raw);
        for (const input of [null, '', raw.subarray(1).toString('base64'), `${raw.toString('base64')}\n`])
            assert.throws(() => decodeFrameInput(input));
        const canonical = Buffer.alloc(4096).toString('base64');
        assert.throws(() => decodeFrameInput(`${canonical.slice(0, -3)}B==`));
    });
    await test('power snapshot crosses ZCL and publishes without changing frame state', async () => {
        const data = Buffer.alloc(40);
        data.set([1, 5, 0, 2]);
        [60000, 55000, 93, 54000, 2000, 3000, 59, 57, 2].forEach((value, i) => data.writeUInt32LE(value, 4+4*i));
        const frame = Zcl.Frame.create(Zcl.FrameType.SPECIFIC, Zcl.Direction.SERVER_TO_CLIENT,
            true, undefined, 23, 'reply', 'gtagFrame', {payload: data}, custom);
        const wire = frame.toBuffer();
        assert.equal(wire.length, 44, 'Power snapshot fits a single small Zigbee frame');
        const decoded = Zcl.Frame.fromBuffer(frameCluster.ID, Zcl.Header.fromBuffer(wire), wire, custom);
        const state = converter.default.fromZigbee[0].convert(null, {data: decoded.payload});
        const snapshot = JSON.parse(state.power_diagnostics);
        assert.deepEqual(snapshot, {rx_on_when_idle: false, joined: true, uptime_ms: 60000,
            stack_sleep_ms: 55000, stack_sleep_calls: 93, cpu_idle_ms: 54000,
            cpu_main_ms: 2000, cpu_zigbee_ms: 3000, samples: 59, radio_off_samples: 57, hfclk_xtal_samples: 2});
        assert.equal(state.frame_status, undefined);
        let requests = 0;
        await powerConverter.convertGet(null, 'power_diagnostics', {device: {ieeeAddr: '0x5678', endpoints: [{
            supportsInputCluster: () => true,
            command: async (cluster, command, payload, options) => {
                assert.deepEqual(payload.payload, Buffer.from([5]));
                assert.equal(options.sendPolicy, 'immediate');
                requests++;
                return decoded.payload;
            },
        }]}});
        assert.equal(requests, 1, 'Diagnostics are read only on request');
        const unsupported = Buffer.alloc(20);
        unsupported.set([1, 5, 2]);
        assert.throws(() => parsePowerDiagnostics(unsupported), /diagnostics unavailable/);
        assert.deepEqual(converter.default.fromZigbee[0].convert(null, {data: {payload: unsupported}}), {});
        for (const malformed of [Buffer.alloc(40), data.subarray(1), null])
            assert.throws(() => parsePowerDiagnostics(malformed), /diagnostics unavailable/);
    });
    await test('demo pixels use the LCD row-lsb layout, matching the HA renderer', async () => {
        const raw = testImage();
        const inverted = testImage(true);
        const black = (x, y) => !(raw[y * 32 + (x >> 3)] & (1 << (x & 7)));
        // At y=10 only the two border pixels are black. This asymmetric pair
        // detects byte-local mirroring that transport/CRC tests cannot catch.
        assert.equal(raw[10 * 32], 0xf7); // x=3 -> bit 3
        assert.equal(raw[10 * 32 + 31], 0xef); // x=252 -> bit 4
        for (let x = 0; x < 256; x++) assert.equal(black(x, 10), x === 3 || x === 252);
        // A full diagonal covers every bit position across multiple bytes.
        for (let x = 16; x < 80; x++) {
            const y = 104 - ((x - 16) % 16);
            assert.equal(black(x, y), true, `wave at (${x}, ${y})`);
            assert.equal(black(x, y + 1), false, `below wave at (${x}, ${y})`);
        }
        // First Z occupies x=23..52 in its top row, spanning byte boundaries.
        for (let x = 16; x < 59; x++) assert.equal(black(x, 20), x >= 23 && x <= 52);
        for (let i = 0; i < raw.length; i++) assert.equal(inverted[i], raw[i] ^ 255);
    });
    for (const [name, raw, codec] of [
        ['white RLE', Buffer.alloc(4096, 255), 1],
        ['test image RLE', testImage(), 1],
        ['inverted image', testImage(true), 1],
        ['incompressible raw', Buffer.from(Array.from({length: 4096}, (_, i) => i % 256)), 0],
    ]) {
        await test(`${name} crosses real ZCL codec and C++ LCD driver`, async () => {
            assert.equal(encodeFrame(raw).codec, codec);
            const result = await transferFrame(endpoint(), raw, {session: 0x12345678, sleep: noSleep});
            assert.equal(result.crc, crc32(raw));
            assert.equal(result.retries, 0);
            await verify(raw);
        });
    }
    for (const opcode of [1, 2, 3, 4]) {
        await test(`lost reply to opcode ${opcode} is retried without duplicate display`, async () => {
            let dropped = false;
            const ep = endpoint(async (phase, packet) => {
                if (phase === 'after' && packet[0] === opcode && !dropped) {
                    dropped = true;
                    throw new Error('simulated lost reply');
                }
            });
            const result = await transferFrame(ep, testImage(), {sleep: noSleep});
            assert.equal(dropped, true);
            assert.equal(result.retries, 1);
            await verify(testImage());
        });
    }
    await test('device reboot during DATA restarts from BEGIN', async () => {
        let rebooted = false;
        const ep = endpoint(async (phase, packet) => {
            if (phase === 'before' && packet[0] === 2 && packet.readUInt16LE(5) >= 96 && !rebooted) {
                rebooted = true;
                await request('reset');
            }
        });
        const result = await transferFrame(ep, testImage(), {sleep: noSleep});
        assert.equal(rebooted, true);
        assert.equal(result.retries, 1);
        await verify(testImage());
    });
    await test('corruption fails CRC and never reaches LCD', async () => {
        let corrupted = false;
        const raw = Buffer.alloc(4096, 0x33);
        const ep = endpoint(async (phase, packet) => {
            if (phase === 'before' && packet[0] === 2 && !corrupted) {
                packet[7] ^= 1;
                corrupted = true;
            }
        });
        await assert.rejects(transferFrame(ep, raw, {sleep: noSleep}), /rejected frame/);
        assert.equal((await request('inspect')).frames, 0);
    });
    await test('CRC completion alone cannot report displayed', async () => {
        await request('hold', {value: true});
        await assert.rejects(transferFrame(endpoint(), testImage(), {sleep: noSleep}), /LCD completion/);
        assert.equal((await request('inspect')).frames, 0);
    });
    await test('malformed reply is retried', async () => {
        let malformed = false;
        const ep = endpoint(async (phase, packet, ack) => {
            if (phase === 'after' && !malformed) {ack[0] = 99; malformed = true;}
        });
        const result = await transferFrame(ep, testImage(), {sleep: noSleep});
        assert.equal(result.retries, 1);
        await verify(testImage());
    });
    await test('unreachable device has bounded retries', async () => {
        let attempts = 0;
        await assert.rejects(transferFrame({command: async () => {attempts++; throw new Error('offline');}},
            testImage(), {sleep: noSleep}), /offline/);
        assert.equal(attempts, 4);
    });
    await test('sleeping-device failures bypass herdsman check-in queue', async () => {
        let attempts = 0;
        let queued = 0;
        const context = {
            getDevice: () => ({pendingRequestTimeout: 3600000}),
            pendingRequests: {
                filter() {},
                queue() { queued++; throw new Error('unexpected check-in queue'); },
            },
        };
        const ep = {command: async (cluster, command, payload, options) => {
            const frame = Zcl.Frame.create(Zcl.FrameType.SPECIFIC, Zcl.Direction.CLIENT_TO_SERVER,
                true, undefined, 1, command, cluster, payload, custom);
            return Endpoint.prototype.sendRequest.call(context, frame, options, async () => {
                attempts++;
                throw new Error('sleepy device unreachable');
            });
        }};
        await assert.rejects(transferFrame(ep, testImage(), {sleep: noSleep}), /sleepy device unreachable/);
        assert.equal(attempts, 4);
        assert.equal(queued, 0, 'No old DATA/COMMIT may arrive after this transfer fails');
    });
    await test('per-device transfer lock and visible sending/displayed/error states', async () => {
        const states = [];
        const ep = endpoint();
        const meta = {device: {ieeeAddr: '0x1234', endpoints: [ep]}, publish: (state) => states.push(state)};
        const first = frameConverter.convertSet(ep, 'test_image', 'zigbee', meta);
        await assert.rejects(frameConverter.convertSet(ep, 'test_image', 'inverted', meta), /already in progress/);
        await first;
        assert.equal(states[0].frame_status, 'sending');
        assert.equal(states.at(-1).frame_status, 'displayed');
        await verify(testImage());
        await request('hold', {value: true});
        await assert.rejects(frameConverter.convertSet(ep, 'test_image', 'inverted', meta), /LCD completion/);
        assert.equal(states.at(-1).frame_status, 'error');
        assert.match(states.at(-1).frame_error, /LCD completion/);
        // Failed transfers must release the lock for another attempt.
        await request('reset');
        await frameConverter.convertSet(ep, 'test_image', 'zigbee', meta);
        assert.equal(states.at(-1).frame_status, 'displayed');
    });
    await test('MQTT request IDs correlate sending and LCD confirmation; legacy clears the ID', async () => {
        const states = [];
        const ep = endpoint();
        const meta = {device: {ieeeAddr: '0x1234', endpoints: [ep]}, publish: (state) => states.push(state)};
        const raw = testImage();
        await frameConverter.convertSet(ep, 'frame',
            {data: raw.toString('base64'), request_id: 'ha-request-123'}, meta);
        assert.equal(states[0].frame_request_id, 'ha-request-123');
        assert.equal(states.at(-1).frame_request_id, 'ha-request-123');
        assert.equal(states.at(-1).frame_status, 'displayed');
        assert.equal(states.at(-1).frame_crc32, crc32(raw).toString(16).padStart(8, '0'));
        await verify(raw);
        meta.options = {optimistic: false};
        await frameConverter.convertSet(ep, 'frame', raw.toString('base64'), meta);
        assert.equal(states.at(-1).frame_request_id, null);
        assert.equal(states.at(-1).frame_status, 'displayed');
    });
    await test('invalid request IDs never reach the radio; busy errors carry the rejected ID', async () => {
        const states = [];
        const ep = endpoint();
        const meta = {device: {ieeeAddr: '0x1234', endpoints: [ep]}, publish: (state) => states.push(state)};
        const data = testImage().toString('base64');
        for (const request_id of [undefined, '', 123, 'x'.repeat(65), 'a/b'])
            await assert.rejects(frameConverter.convertSet(ep, 'frame', {data, request_id}, meta), /request_id/);
        assert.equal(states.length, 0);
        const first = frameConverter.convertSet(ep, 'frame', {data, request_id: 'first'}, meta);
        await assert.rejects(frameConverter.convertSet(ep, 'frame', {data, request_id: 'second'}, meta), /already in progress/);
        assert.equal(states.at(-1).frame_request_id, 'second');
        assert.equal(states.at(-1).frame_status, 'error');
        await first;
        assert.equal(states.at(-1).frame_request_id, 'first');
    });
    console.log(`${passed} Zigbee converter/firmware tests passed`);
} finally {
    const stopped = new Promise((resolve) => {
        if (child.exitCode !== null || child.signalCode !== null) resolve();
        else child.once('exit', resolve);
    });
    child.stdin.end();
    await stopped;
    await rm(runtime, {recursive: true, force: true});
}
