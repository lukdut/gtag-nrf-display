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
            const reply = Buffer.concat([Buffer.from([0x19, wire[1], 0x80, ack.length]), ack]);
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
    await test('capabilities expose firmware version and select RAW independently per device', async () => {
        const rawOnly = endpoint(async (phase, packet, ack) => {
            if (phase === 'after' && packet[0] === 7) ack.writeUInt32LE(0x80000001, 7);
        });
        const raw = Buffer.alloc(4096, 255);
        const first = await transferFrame(rawOnly, raw, {sleep: noSleep});
        assert.equal(first.info.firmware_version, '0.9.0');
        assert.equal(first.info.features, 15);
        assert.equal(first.codec, 0);
        assert.equal(first.bytes, 4096);
        await request('reset');
        const second = await transferFrame(endpoint(), raw, {sleep: noSleep});
        assert.equal(second.codec, 1);
        assert.equal(second.bytes, 32);
    });
    await test('legacy firmware uses RAW and the original BEGIN without freshness', async () => {
        const ep = endpoint(async (phase, packet) => {
            if (phase === 'before' && packet[0] === 1) {
                assert.equal(packet.length, 13); assert.equal(packet[2], 0);
            }
        });
        const base = ep.command.bind(ep);
        ep.command = async (cluster, command, payload, options) => {
            if (payload.payload[0] === 7) {
                const reply = Buffer.alloc(20); reply[0] = 1; reply[1] = 7; reply[2] = 2;
                return {payload: reply};
            }
            return base(cluster, command, payload, options);
        };
        const result = await transferFrame(ep, testImage(), {freshnessTimeout: 60, sleep: noSleep});
        assert.equal(result.info.firmware_version, null);
        assert.equal(result.info.legacy, true);
        assert.equal(result.freshnessTimeout, 0);
        await verify(testImage());
    });
    await test('unsupported protocol/codec/schema/geometry never sends BEGIN', async () => {
        for (const mode of ['protocol', 'codec', 'schema', 'geometry', 'limit']) {
            let begin = false;
            const ep = endpoint(async (phase, packet, ack) => {
                if (phase === 'before' && packet[0] === 1) begin = true;
                if (phase === 'after' && packet[0] === 7) {
                    if (mode === 'protocol') {ack[4] = 2; ack[5] = 2;}
                    if (mode === 'codec') ack.writeUInt32LE(4, 7);
                    if (mode === 'schema') ack[3] = 2;
                    if (mode === 'geometry') ack.writeUInt16LE(128, 15);
                    if (mode === 'limit') ack.writeUInt16LE(1, 19);
                }
            });
            await assert.rejects(transferFrame(ep, testImage(), {sleep: noSleep}), /compatible|supported|malformed/);
            assert.equal(begin, false);
        }
    });
    await test('INFO does not disturb an active frame session', async () => {
        let checked = false;
        const ep = endpoint(async (phase, packet) => {
            if (phase === 'before' && packet[0] === 2 && !checked) {
                checked = true;
                const info = await converter.readFirmwareInfo(ep, noSleep);
                assert.equal(info.schema, 1);
            }
        });
        await transferFrame(ep, testImage(), {sleep: noSleep});
        assert.equal(checked, true);
        await verify(testImage());
    });
    await test('small advertised chunks are respected', async () => {
        const ep = endpoint(async (phase, packet, ack) => {
            if (phase === 'after' && packet[0] === 7) ack.writeUInt16LE(8, 21);
            if (phase === 'before' && packet[0] === 2) assert.ok(packet.length <= 15);
        });
        await transferFrame(ep, Buffer.alloc(4096, 255), {sleep: noSleep});
        await verify(Buffer.alloc(4096, 255));
    });
    await test('a stalled device does not block another device or share its transfer lock', async () => {
        let release, entered;
        const blocked = new Promise((resolve) => {release = resolve;});
        const started = new Promise((resolve) => {entered = resolve;});
        const firstStates = [], secondStates = [];
        const first = {device: {ieeeAddr: '0xfirst', endpoints: [{
            supportsInputCluster: () => true,
            command: async () => {entered(); await blocked; throw new Error('First device offline');},
        }]}, publish: (value) => firstStates.push(value)};
        const second = {device: {ieeeAddr: '0xsecond', endpoints: [endpoint()]},
            publish: (value) => secondStates.push(value)};
        const raw = Buffer.alloc(4096, 255);
        const message = (request_id) => ({data: raw.toString('base64'), request_id});
        const waiting = frameConverter.convertSet(null, 'frame', message('first'), first);
        const failure = assert.rejects(waiting, /First device offline/);
        await started;
        try {
            await assert.rejects(frameConverter.convertSet(null, 'frame', message('duplicate'), first), /already in progress/);
            await frameConverter.convertSet(null, 'frame', message('second'), second);
            assert.equal(secondStates.at(-1).frame_status, 'displayed');
            assert.equal(secondStates.at(-1).frame_request_id, 'second');
            assert.ok(!firstStates.some((value) => value.frame_status === 'displayed'));
            await verify(raw);
        } finally {
            release();
            await failure;
        }
        assert.equal(firstStates.at(-1).frame_request_id, 'first');
        assert.equal(firstStates.at(-1).frame_status, 'error');
        // Recovered device can start a new transfer after its own failure.
        first.device.endpoints = [endpoint()];
        await frameConverter.convertSet(null, 'frame', message('recovered'), first);
        assert.equal(firstStates.at(-1).frame_status, 'displayed');
        assert.equal(secondStates.at(-1).frame_request_id, 'second');
    });
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
        const definition = prepareDefinition(converter.default[0]);
        assert.ok(definition.toZigbee.includes(frameConverter));
        const properties = definition.exposes.map((expose) => expose.property);
        for (const property of ['test_image', 'frame_status', 'frame_error', 'battery_voltage_1', 'check_connection', 'connection_status'])
            assert.ok(properties.includes(property), property);
        assert.equal(crc32(Buffer.from('123456789')), 0xcbf43926);
    });
    await test('no-battery model omits voltage and maps the remaining endpoints independently', async () => {
        const normal = prepareDefinition(converter.default[0]);
        const noBattery = prepareDefinition(converter.default[1]);
        const properties = noBattery.exposes.map((expose) => expose.property);
        assert.ok(!properties.includes('battery_voltage_1'));
        for (const property of ['frame_request_id', 'check_connection', 'connection_status'])
            assert.ok(properties.includes(property), property);
        assert.deepEqual(normal.endpoint({}), {'1': 1, '2': 2, '3': 3, '4': 4});
        assert.deepEqual(noBattery.endpoint({}), {'2': 1, '3': 2, '4': 3});
        assert.ok(noBattery.toZigbee.includes(frameConverter));
        // It must not bind/report battery data from endpoint 1 (now frame count).
        const configured = [];
        const endpoints = [1, 2, 3].map((ID) => Object.assign(Object.create(Endpoint.prototype), {ID, deviceIeeeAddress: '0xsecond',
            bind: async (cluster) => configured.push([ID, cluster]),
            configureReporting: async () => {},
            read: async () => ({}),
        }));
        const device = {addCustomCluster: () => {}, endpoints,
            getEndpoint: (ID) => endpoints.find((ep) => ep.ID === ID)};
        await noBattery.configure(device, {}, noBattery);
        assert.deepEqual(configured, [[1, 'genAnalogInput'], [2, 'genAnalogOutput']]);
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
        const state = converter.default[0].fromZigbee[0].convert(null, {data: decoded.payload});
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
        assert.deepEqual(converter.default[0].fromZigbee[0].convert(null, {data: {payload: unsupported}}), {});
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
            if (phase === 'after' && packet[0] === 1 && !malformed) {ack[0] = 99; malformed = true;}
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
    await test('exposes keep useful diagnostics and compatibility, while raw MQTT commands remain', async () => {
        for (const source of converter.default) {
            const definition = prepareDefinition(source);
            const properties = definition.exposes.map((item) => item.property);
            for (const hidden of ['frame', 'frame_freshness', 'firmware_capabilities', 'frame_crc32',
                'frame_id', 'frame_stale_frame_id', 'freshness_request_id', 'freshness_status',
                'freshness_error', 'freshness_frame_id', 'freshness_crc32', 'freshness_sequence',
                'power_diagnostics', 'rendered_frames_2', 'display_pattern_3'])
                assert.ok(!properties.includes(hidden), hidden);
            for (const key of ['frame', 'frame_freshness', 'power_diagnostics', 'display_pattern'])
                assert.ok(definition.toZigbee.some((item) => item.key.includes(key)), key);
            assert.ok(properties.includes('frame_request_id'), 'Released HA discovery still works');
        }
    });
    await test('connection check is correlated and does not alter LCD pixels or renew freshness', async () => {
        const ep = endpoint();
        await transferFrame(ep, testImage(), {session: 567, freshnessTimeout: 60, sleep: noSleep});
        await request('advance', {ms: 60_000});
        const before = await request('inspect');
        const packets = [], states = [];
        const diagnosticEndpoint = endpoint(async (phase, packet) => {
            if (phase === 'before') packets.push(packet[0]);
        });
        const meta = {device: {ieeeAddr: '0xdiagnostics', endpoints: [diagnosticEndpoint]},
            publish: (state) => states.push(state)};
        await converter.connectionConverter.convertSet(null, 'check_connection', {request_id: 'check-1'}, meta);
        assert.deepEqual(packets, [7]);
        assert.equal(states[0].connection_status, 'checking');
        const last = states.at(-1);
        assert.equal(last.connection_status, 'ok');
        assert.equal(last.connection_request_id, 'check-1');
        assert.equal(last.firmware_version, '0.9.0');
        assert.ok(last.connection_checked_at);
        assert.deepEqual(await request('inspect'), before);
        await converter.connectionConverter.convertSet(null, 'check_connection', 'check', meta);
        assert.equal(states.at(-1).connection_request_id, null, 'UI cannot reuse an HA request ID');
    });
    await test('connection check distinguishes legacy, invalid reply, timeout and incompatible firmware', async () => {
        for (const kind of ['legacy', 'invalid_response', 'timeout', 'incompatible_firmware']) {
            const states = [];
            const ep = endpoint(async (phase, packet, ack) => {
                if (phase === 'after' && packet[0] === 7 && kind === 'incompatible_firmware') { ack[4] = 2; ack[5] = 2; }
            });
            const original = ep.command.bind(ep);
            ep.command = async (...args) => {
                if (kind === 'timeout') throw new Error('No radio reply');
                if (kind === 'invalid_response') return {payload: Buffer.from([1, 7, 0])};
                if (kind === 'legacy') {
                    const data = Buffer.alloc(20); data[0] = 1; data[1] = 7; data[2] = 2;
                    return {payload: data};
                }
                return original(...args);
            };
            const meta = {device: {ieeeAddr: '0xdiagnostics', endpoints: [ep]}, publish: (state) => states.push(state)};
            await converter.connectionConverter.convertSet(null, 'check_connection', {request_id: kind}, meta);
            const last = states.at(-1);
            assert.equal(last.connection_status, kind === 'legacy' ? 'ok' : 'error');
            assert.equal(last.connection_error_code, kind === 'legacy' ? '' : kind);
            assert.equal(last.connection_request_id, kind);
            if (kind === 'legacy') assert.equal(last.firmware_legacy, true);
        }
        assert.equal((await request('inspect')).frames, 0);
    });
    await test('connection check cannot overlap frame operations and one offline device cannot block another', async () => {
        let release, entered;
        const blocked = new Promise((resolve) => {release = resolve;});
        const started = new Promise((resolve) => {entered = resolve;});
        const firstStates = [], secondStates = [];
        const first = {device: {ieeeAddr: '0xfirst', endpoints: [{supportsInputCluster: () => true,
            command: async () => {entered(); await blocked; throw new Error('Offline');}}]},
            publish: (state) => firstStates.push(state)};
        const second = {device: {ieeeAddr: '0xsecond', endpoints: [endpoint()]}, publish: (state) => secondStates.push(state)};
        const running = converter.connectionConverter.convertSet(null, 'check_connection', {request_id: 'first'}, first);
        await started;
        try {
            await converter.connectionConverter.convertSet(null, 'check_connection', {request_id: 'busy'}, first);
            assert.equal(firstStates.at(-1).connection_error_code, 'device_busy');
            assert.equal(firstStates.at(-1).connection_request_id, 'busy');
            await assert.rejects(frameConverter.convertSet(null, 'test_image', 'zigbee', first), /already in progress/);
            await converter.connectionConverter.convertSet(null, 'check_connection', {request_id: 'second'}, second);
            assert.equal(secondStates.at(-1).connection_status, 'ok');
        } finally { release(); await running; }
        first.device.endpoints = [endpoint()];
        await converter.connectionConverter.convertSet(null, 'check_connection', 'check', first);
        assert.equal(firstStates.at(-1).connection_status, 'ok');
    });
    await test('publication failure releases the connection-check lock', async () => {
        const meta = {device: {ieeeAddr: '0xpublication', endpoints: [endpoint()]},
            publish: () => {throw new Error('MQTT publication failed');}};
        await assert.rejects(converter.connectionConverter.convertSet(null, 'check_connection', 'check', meta), /publication failed/);
        const states = [];
        meta.publish = (state) => states.push(state);
        await converter.connectionConverter.convertSet(null, 'check_connection', 'check', meta);
        assert.equal(states.at(-1).connection_status, 'ok');
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
