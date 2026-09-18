// GTag frame protocol v1. Zigbee2MQTT 2.12.0 / converters 26.63.0.
import {randomBytes} from 'node:crypto';
import {Zcl} from 'zigbee-herdsman';
import * as m from 'zigbee-herdsman-converters/lib/modernExtend';
import {presets as e, access as ea} from 'zigbee-herdsman-converters/lib/exposes';

export const frameCluster = {
    name: 'gtagFrame',
    ID: 0xfc11,
    attributes: {revision: {name: 'revision', ID: 0xfffd, type: Zcl.DataType.UINT16}},
    commands: {packet: {
        name: 'packet', ID: 0, response: 0x80,
        parameters: [{name: 'payload', type: Zcl.DataType.OCTET_STR}],
    }},
    commandsResponse: {reply: {
        name: 'reply', ID: 0x80, parameters: [{name: 'payload', type: Zcl.DataType.OCTET_STR}],
    }},
};

class FirmwareInfoResponseError extends Error {}

// Discovery opcode/schema are stable even when the frame protocol changes.
export function parseFirmwareInfo(payload) {
    const data = Buffer.from(payload ?? []);
    if (data.length === 20 && data[0] === 1 && data[1] === 7 && data[2] === 2) {
        return {firmware_version: null, schema: 0, protocol_min: 1, protocol_max: 1,
            pixel_format: 1, codecs: 1, features: 0, width: 256, height: 128,
            max_encoded_size: 4096, max_chunk_size: 32, legacy: true};
    }
    if (data.length < 43 || data[0] !== 1 || data[1] !== 7 || data[2] !== 0 || data[3] !== 1)
        throw new Error('Unsupported or malformed GTag firmware-info schema; update the converter');
    const raw = data.subarray(3);
    const version = raw.subarray(20, 40).toString('latin1').split('\0')[0];
    if (!/^[0-9A-Za-z][0-9A-Za-z.+_-]{0,18}$/.test(version)) throw new Error('Invalid GTag firmware version');
    const info = {firmware_version: version, schema: raw[0], protocol_min: raw[1], protocol_max: raw[2],
        pixel_format: raw[3], codecs: raw.readUInt32LE(4), features: raw.readUInt32LE(8),
        width: raw.readUInt16LE(12), height: raw.readUInt16LE(14), max_encoded_size: raw.readUInt16LE(16),
        max_chunk_size: raw.readUInt16LE(18), legacy: false};
    if (!info.protocol_min || info.protocol_max < info.protocol_min || !info.max_chunk_size)
        throw new Error('Invalid GTag firmware capabilities');
    return info;
}
export async function readFirmwareInfo(endpoint, sleep = pause) {
    let result;
    for (let attempt = 0; attempt < 4; attempt++) {
        try {
            result = await endpoint.command('gtagFrame', 'packet', {payload: Buffer.from([7])},
                {disableDefaultResponse: true, timeout: 8000, sendPolicy: 'immediate'});
            break;
        } catch (error) {
            if (attempt === 3) throw error;
            await sleep(150);
        }
    }
    // A timeout is not evidence of old firmware. Never silently downgrade it.
    try { return parseFirmwareInfo(result?.payload); }
    catch (error) { throw new FirmwareInfoResponseError(error.message); }
}
function validateFirmwareInfo(info) {
    if (!(info.protocol_min <= 1 && info.protocol_max >= 1))
        throw new Error('No compatible GTag frame protocol; update the converter or firmware');
    if (info.width !== 256 || info.height !== 128 || info.pixel_format !== 1)
        throw new Error('Unsupported GTag display dimensions or pixel format');
    if (!(info.codecs & 3) || !info.max_encoded_size || !info.max_chunk_size)
        throw new Error('No compatible GTag codec or transfer limits');
}
const infoState = (info) => ({firmware_version: info.firmware_version,
    firmware_capabilities: JSON.stringify(info), firmware_legacy: info.legacy,
    firmware_codecs: ['raw', 'white_rle_v1'].filter((_, bit) => info.codecs & (1 << bit)).join(', '),
    firmware_features: ['freshness', 'battery_voltage', 'battery_bar', 'battery_protection']
        .filter((_, bit) => info.features & (1 << bit)).join(', ')});

export function crc32(data) {
    let crc = 0xffffffff;
    for (const byte of data) {
        crc ^= byte;
        for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
    }
    return (crc ^ 0xffffffff) >>> 0;
}

// Same WHITE_RLE_V1 grammar as BLE. Choose raw if compression isn't smaller.
export function encodeFrame(raw, codecs = 3, maxSize = 4096) {
    if (!Buffer.isBuffer(raw) || raw.length !== 4096) throw new Error('Frame must contain exactly 4096 bytes');
    const encoded = [];
    for (let i = 0; i < raw.length;) {
        let run = 0;
        while (run < 128 && i + run < raw.length && raw[i + run] === 255) run++;
        if (run >= 2) {
            encoded.push(0x80 | (run - 1));
            i += run;
        } else {
            const start = i++;
            while (i - start < 128 && i < raw.length && !(raw[i] === 255 && raw[i + 1] === 255)) i++;
            encoded.push(i - start - 1, ...raw.subarray(start, i));
        }
    }
    const candidates = [];
    if ((codecs & 1) && raw.length <= maxSize) candidates.push({payload: raw, codec: 0});
    if ((codecs & 2) && encoded.length <= maxSize) candidates.push({payload: Buffer.from(encoded), codec: 1});
    candidates.sort((a, b) => a.payload.length - b.payload.length);
    if (!candidates.length) throw new Error('No supported codec can encode this frame within the device limit');
    return {...candidates[0], crc: crc32(raw)};
}

export function decodeFrameInput(value) {
    if (typeof value !== 'string' || value.length !== 5464 ||
        !/^[A-Za-z0-9+/]{5462}==$/.test(value)) {
        // 4096 bytes has a one-byte final group: two padding characters.
        throw new Error('frame must be canonical base64 of a 256x128 monochrome framebuffer (4096 bytes)');
    }
    const raw = Buffer.from(value, 'base64');
    if (raw.length !== 4096 || raw.toString('base64') !== value) throw new Error('Invalid framebuffer base64');
    return raw;
}

export function parseReply(payload, command) {
    const data = Buffer.from(payload ?? []);
    if (data.length !== 20 || data[0] !== 1 || data[1] !== command || data[2] > 4) {
        throw new Error('Invalid GTag frame reply or unsupported firmware');
    }
    return {
        result: data[2], session: data.readUInt32LE(3), state: data[7],
        received: data.readUInt16LE(8), crc: data.readUInt32LE(10), error: data[14],
        pending: !!(data[15] & 1), rendered: !!(data[15] & 2), renderedId: data.readUInt32LE(16),
        stale: !!(data[15] & 4),
    };
}

class SessionLost extends Error {}
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export async function transferFrame(endpoint, raw, options = {}) {
    let freshnessTimeout = options.freshnessTimeout ?? 0;
    if (!Number.isInteger(freshnessTimeout) || freshnessTimeout < 0 || freshnessTimeout > 86400)
        throw new Error('freshness_timeout must be an integer between 0 and 86400 seconds');
    const sleep = options.sleep ?? pause;
    const info = await readFirmwareInfo(endpoint, sleep);
    options.onInfo?.(info);
    validateFirmwareInfo(info);
    if (!(info.features & 1)) freshnessTimeout = 0;
    const encoded = encodeFrame(raw, info.codecs, info.max_encoded_size);
    const chunkSize = Math.min(32, info.max_chunk_size);
    const session = options.session ?? randomBytes(4).readUInt32LE(0);
    const started = Date.now();
    const deadline = started + 180_000;
    let retries = 0;
    const rpc = async (packet) => {
        let lastError;
        for (let attempt = 0; attempt < 4; attempt++) {
            if (Date.now() >= deadline) throw new Error('GTag frame transfer timed out');
            let reply;
            try {
                const result = await endpoint.command('gtagFrame', 'packet', {payload: packet},
                    // Own retries are bounded; don't leave a stale frame packet
                    // in herdsman's battery-device queue until a later check-in.
                    {disableDefaultResponse: true, timeout: 8000, sendPolicy: 'immediate'});
                reply = parseReply(result?.payload, packet[0]);
            } catch (error) {
                lastError = error;
            }
            if (reply) {
                if (reply.result !== 1) return reply;
                lastError = new Error('GTag is busy');
            }
            if (attempt < 3) {
                retries++;
                await sleep(reply?.result === 1 ? 500 : 150);
            }
        }
        throw lastError;
    };
    const check = (status) => {
        if (status.result === 3 || status.session !== session || status.state === 0) {
            throw new SessionLost('GTag frame session lost (device restarted or another sender started a frame)');
        }
        if (status.result !== 0 || status.state === 3 || status.error !== 0) {
            throw new Error(`GTag rejected frame: result=${status.result}, state=${status.state}, error=${status.error}`);
        }
        if (status.received > encoded.payload.length) throw new Error('GTag returned invalid frame offset');
        return status;
    };
    const control = (command) => {
        const packet = Buffer.alloc(5);
        packet[0] = command;
        packet.writeUInt32LE(session, 1);
        return packet;
    };
    for (let restart = 0; restart < 2; restart++) {
        try {
            const begin = Buffer.alloc(info.features & 1 ? 17 : 13);
            begin[0] = 1; begin[1] = 1; begin[2] = encoded.codec;
            begin.writeUInt16LE(encoded.payload.length, 3);
            begin.writeUInt32LE(session, 5);
            begin.writeUInt32LE(encoded.crc, 9);
            if (info.features & 1) begin.writeUInt32LE(freshnessTimeout, 13);
            let status = check(await rpc(begin));
            for (let offset = status.received; offset < encoded.payload.length;) {
                const chunk = encoded.payload.subarray(offset, offset + chunkSize);
                const packet = Buffer.alloc(7 + chunk.length);
                packet[0] = 2;
                packet.writeUInt32LE(session, 1);
                packet.writeUInt16LE(offset, 5);
                chunk.copy(packet, 7);
                status = check(await rpc(packet));
                if (status.received < offset + chunk.length) throw new Error('GTag did not accept the complete chunk');
                offset = status.received;
            }
            status = check(await rpc(control(3)));
            if (status.state !== 2 || status.crc !== encoded.crc) throw new Error('GTag frame CRC confirmation failed');
            // COMPLETE means decoded/verified. Wait separately for the LCD write.
            for (let poll = 0; poll <= 30; poll++) {
                if (status.rendered && status.renderedId === session && !status.pending) {
                    return {session, crc: encoded.crc, bytes: encoded.payload.length, retries, freshnessTimeout, info, codec: encoded.codec,
                        milliseconds: Date.now() - started};
                }
                if (poll === 30) break;
                await sleep(100);
                status = check(await rpc(control(4)));
                if (status.state !== 2 || status.crc !== encoded.crc) throw new Error('GTag frame changed before display confirmation');
            }
            throw new Error('Frame verified, but LCD completion was not confirmed');
        } catch (error) {
            if (!(error instanceof SessionLost) || restart === 1) throw error;
            retries++;
        }
    }
    throw new Error('GTag frame transfer failed');
}

// A complete host-rendered picture, not the firmware's diagnostic-pattern command.
export function testImage(inverted = false) {
    const raw = Buffer.alloc(4096, 255);
    const pixel = (x, y) => {
        // Native LCD/HA layout is row-lsb: leftmost pixel uses bit 0.
        if (x >= 0 && x < 256 && y >= 0 && y < 128) raw[y * 32 + (x >> 3)] &= ~(1 << (x & 7));
    };
    for (let x = 3; x <= 252; x++) {pixel(x, 3); pixel(x, 124);}
    for (let y = 3; y <= 124; y++) {pixel(3, y); pixel(252, y);}
    const font = {
        Z: ['11111','00001','00010','00100','01000','10000','11111'],
        I: ['11111','00100','00100','00100','00100','00100','11111'],
        G: ['01110','10001','10000','10111','10001','10001','01110'],
        B: ['11110','10001','10001','11110','10001','10001','11110'],
        E: ['11111','10000','10000','11110','10000','10000','11111'],
        O: ['01110','10001','10001','10001','10001','10001','01110'],
        K: ['10001','10010','10100','11000','10100','10010','10001'],
    };
    const text = (word, left, top, scale) => {
        for (let n = 0; n < word.length; n++) {
            const glyph = font[word[n]];
            for (let y = 0; y < 7; y++) for (let x = 0; x < 5; x++) if (glyph[y][x] === '1') {
                for (let dy = 0; dy < scale; dy++) for (let dx = 0; dx < scale; dx++)
                    pixel(left + (n * 6 + x) * scale + dx, top + y * scale + dy);
            }
        }
    };
    text('ZIGBEE', 23, 20, 6);
    text('OK', 106, 77, 4);
    for (let x = 16; x < 80; x++) pixel(x, 104 - ((x - 16) % 16));
    for (let x = 176; x < 240; x++) pixel(x, 88 + ((x - 176) % 16));
    if (inverted) for (let i = 0; i < raw.length; i++) raw[i] ^= 255;
    return raw;
}

const inFlight = new Set();

export async function confirmFreshness(endpoint, value) {
    for (const name of ['frame_id', 'crc', 'sequence']) {
        if (!Number.isInteger(value[name]) || value[name] < (name === 'sequence' ? 1 : 0) || value[name] > 0xffffffff)
            throw new Error(`Invalid freshness ${name}`);
    }
    const info = await readFirmwareInfo(endpoint);
    validateFirmwareInfo(info);
    if (!(info.features & 1)) throw new Error('Firmware does not support freshness confirmations; send a complete frame');
    const packet = Buffer.alloc(13);
    packet[0] = 6;
    packet.writeUInt32LE(value.frame_id, 1);
    packet.writeUInt32LE(value.crc, 5);
    packet.writeUInt32LE(value.sequence, 9);
    const result = await endpoint.command('gtagFrame', 'packet', {payload: packet},
        {disableDefaultResponse: true, timeout: 8000, sendPolicy: 'immediate'});
    const status = parseReply(result?.payload, 6);
    if (status.result !== 0 || status.session !== value.frame_id || status.crc !== value.crc ||
        status.state !== 2 || !status.rendered || status.renderedId !== value.frame_id)
        throw new Error('Freshness confirmation rejected: the device needs a complete frame');
}

export const freshnessConverter = {
    key: ['frame_freshness', 'frame_stale'],
    convertGet: async (entity, key, meta) => {
        const endpoint = meta.device?.endpoints.find((ep) => ep.supportsInputCluster(frameCluster.ID));
        if (!endpoint) throw new Error('GTag frame endpoint missing');
        const result = await endpoint.command('gtagFrame', 'packet', {payload: Buffer.from([4, 0, 0, 0, 0])},
            {disableDefaultResponse: true, timeout: 8000, sendPolicy: 'immediate'});
        parseReply(result?.payload, 4);
    },
    convertSet: async (entity, key, value, meta) => {
        if (!value || typeof value !== 'object' || typeof value.request_id !== 'string' ||
            !/^[a-zA-Z0-9_-]{1,64}$/.test(value.request_id))
            throw new Error('frame_freshness requires frame_id, crc, sequence and request_id');
        const device = meta.device;
        const endpoint = device?.endpoints.find((ep) => ep.supportsInputCluster(frameCluster.ID));
        if (!endpoint || !device.ieeeAddr) throw new Error('GTag frame endpoint missing');
        const publish = (status, error = '') => meta.publish({
            freshness_request_id: value.request_id, freshness_status: status, freshness_error: error,
            freshness_frame_id: value.frame_id, freshness_crc32: value.crc?.toString(16).padStart(8, '0'),
            freshness_sequence: value.sequence,
        });
        if (inFlight.has(device.ieeeAddr)) {
            publish('error', 'A frame operation is already in progress');
            throw new Error('A frame operation is already in progress');
        }
        inFlight.add(device.ieeeAddr);
        try {
            await confirmFreshness(endpoint, value);
            publish('confirmed');
        } catch (error) {
            publish('error', error.message);
            throw error;
        } finally {
            inFlight.delete(device.ieeeAddr);
        }
    },
};
export function parsePowerDiagnostics(payload) {
    const data = Buffer.from(payload ?? []);
    if (data.length !== 40 || data[0] !== 1 || data[1] !== 5 || data[2] !== 0 || (data[3] & ~3))
        throw new Error('Power diagnostics unavailable; install a build with zigbee_power_diagnostics: true');
    const result = {rx_on_when_idle: !!(data[3] & 1), joined: !!(data[3] & 2)};
    const fields = ['uptime_ms', 'stack_sleep_ms', 'stack_sleep_calls', 'cpu_idle_ms',
        'cpu_main_ms', 'cpu_zigbee_ms', 'samples', 'radio_off_samples', 'hfclk_xtal_samples'];
    for (let i = 0; i < fields.length; i++) result[fields[i]] = data.readUInt32LE(4 + 4 * i);
    return result;
}

export const powerConverter = {
    key: ['power_diagnostics'],
    convertGet: async (entity, key, meta) => {
        const endpoint = meta.device?.endpoints.find((ep) => ep.supportsInputCluster(frameCluster.ID));
        if (!endpoint) throw new Error('GTag frame endpoint missing');
        if (inFlight.has(meta.device.ieeeAddr)) throw new Error('Wait for the frame transfer to finish');
        const result = await endpoint.command('gtagFrame', 'packet', {payload: Buffer.from([5])},
            {disableDefaultResponse: true, timeout: 8000, sendPolicy: 'immediate'});
        // read failures/older firmware surface in the request log; publication
        // is handled by the incoming commandReply converter below.
        parsePowerDiagnostics(result?.payload);
    },
};

export const frameConverter = {
    key: ['frame', 'test_image'],
    convertSet: async (entity, key, value, meta) => {
        const device = meta.device;
        if (!device?.ieeeAddr) throw new Error('GTag frame transfer requires a single device');
        // The HA adapter attaches an opaque ID. Preserve the original base64
        // form for scripts and Test image, and never reuse a cached request ID.
        const envelope = key === 'frame' && value !== null && typeof value === 'object';
        const requestId = envelope ? value.request_id : null;
        if (envelope && (typeof requestId !== 'string' || !/^[a-zA-Z0-9_-]{1,64}$/.test(requestId)))
            throw new Error('frame.request_id must contain 1..64 letters, digits, underscores or hyphens');
        const raw = key === 'frame' ? decodeFrameInput(envelope ? value.data : value) :
            value === 'zigbee' ? testImage() : value === 'inverted' ? testImage(true) : null;
        if (!raw) throw new Error('test_image must be zigbee or inverted');
        const endpoint = device.endpoints.find((ep) => ep.supportsInputCluster(frameCluster.ID));
        if (!endpoint) throw new Error('Frame endpoint missing; install Frame_V1 firmware and re-interview the device');
        if (inFlight.has(device.ieeeAddr)) {
            if (requestId !== null) meta.publish({frame_request_id: requestId,
                frame_status: 'error', frame_error: 'A GTag frame transfer is already in progress for this device'});
            throw new Error('A GTag frame transfer is already in progress for this device');
        }
        inFlight.add(device.ieeeAddr);
        try {
            meta.publish({frame_status: 'sending', frame_error: '', frame_request_id: requestId});
            const result = await transferFrame(endpoint, raw, {freshnessTimeout: envelope ? value.freshness_timeout : 0,
                onInfo: (info) => meta.publish(infoState(info))});
            // This is a device-confirmed result. Publish even when the user
            // disables Zigbee2MQTT's optimistic state updates.
            meta.publish({
                ...infoState(result.info), frame_codec: result.codec,
                frame_status: 'displayed', frame_id: result.session,
                frame_request_id: requestId, frame_error: '',
                frame_crc32: result.crc.toString(16).padStart(8, '0'),
                frame_bytes: result.bytes, frame_transfer_ms: result.milliseconds,
                frame_retries: result.retries,
                frame_freshness_timeout: result.freshnessTimeout,
            });
        } catch (error) {
            meta.publish({frame_status: 'error', frame_error: error.message, frame_request_id: requestId});
            throw error;
        } finally {
            inFlight.delete(device.ieeeAddr);
        }
    },
};

export const infoConverter = {
    key: ['firmware_version', 'firmware_capabilities', 'firmware_legacy'],
    convertGet: async (entity, key, meta) => {
        const endpoint = meta.device?.endpoints.find((ep) => ep.supportsInputCluster(frameCluster.ID));
        if (!endpoint) throw new Error('GTag frame endpoint missing');
        meta.publish(infoState(await readFirmwareInfo(endpoint)));
    },
};

export const connectionConverter = {
    key: ['check_connection'],
    convertSet: async (entity, key, value, meta) => {
        const requestId = value === 'check' ? null : value?.request_id;
        if (value !== 'check' && (typeof requestId !== 'string' || !/^[a-zA-Z0-9_-]{1,64}$/.test(requestId)))
            throw new Error('check_connection requires "check" or {request_id}');
        const device = meta.device;
        if (!device?.ieeeAddr) throw new Error('Select a single GTag device');
        const started = Date.now();
        const publish = (status, code = '', error = '', extra = {}) => meta.publish({
            connection_request_id: requestId, connection_status: status,
            connection_error_code: code, connection_error: error,
            connection_checked_at: status === 'checking' ? null : new Date().toISOString(),
            connection_duration_ms: status === 'checking' ? null : Date.now() - started, ...extra,
        });
        if (inFlight.has(device.ieeeAddr)) {
            publish('error', 'device_busy', 'A frame operation is in progress; try again after it finishes');
            return;
        }
        const endpoint = device.endpoints.find((ep) => ep.supportsInputCluster(frameCluster.ID));
        if (!endpoint) {
            publish('error', 'incompatible_firmware', 'GTag frame endpoint missing; re-interview the device');
            return;
        }
        inFlight.add(device.ieeeAddr);
        try {
            publish('checking');
            let result;
            try {
                result = await readFirmwareInfo(endpoint);
            } catch (error) {
                // A missing radio reply must never become a successful legacy check.
                publish('error', error instanceof FirmwareInfoResponseError ? 'invalid_response' : 'timeout', error.message);
                return;
            }
            try {
                validateFirmwareInfo(result);
            } catch (error) {
                publish('error', 'incompatible_firmware', error.message, infoState(result));
                return;
            }
            publish('ok', '', '', infoState(result));
        } finally {
            inFlight.delete(device.ieeeAddr);
        }
    },
};

// Preserve old MQTT commands/reporting without presenting raw protocol controls.
const withoutExposes = (extension) => ({...extension, exposes: []});

function definition(battery) {
    return {
    zigbeeModel: [battery ? 'GTag_Display_Frame_V1' : 'GTag_Display_Frame_NoBat'],
    model: battery ? 'GTag Display Zigbee' : 'GTag Display Zigbee (no battery sensor)', vendor: 'GTag',
    description: 'G-Tag 256x128 display on nRF52840, frame protocol v1',
    extend: [
        m.deviceAddCustomCluster('gtagFrame', frameCluster),
        // Keep MQTT property names stable when the battery endpoint is absent.
        m.deviceEndpoints({endpoints: battery ? {'1': 1, '2': 2, '3': 3, '4': 4} : {'2': 1, '3': 2, '4': 3}}),
        ...(battery ? [m.numeric({name: 'battery_voltage', label: 'Battery voltage', endpointNames: ['1'],
            cluster: 'genAnalogInput', attribute: 'presentValue', unit: 'V', access: 'STATE_GET',
            reporting: {min: 30, max: 300, change: 0.01}})] : []),
        withoutExposes(m.numeric({name: 'rendered_frames', label: 'Rendered frames', endpointNames: ['2'],
            cluster: 'genAnalogInput', attribute: 'presentValue', access: 'STATE_GET',
            reporting: {min: 0, max: 300, change: 1}})),
        withoutExposes(m.numeric({name: 'display_pattern', label: 'Display pattern', endpointNames: ['3'],
            cluster: 'genAnalogOutput', attribute: 'presentValue', access: 'ALL',
            valueMin: 0, valueMax: 3, valueStep: 1, reporting: {min: 0, max: 300, change: 1}})),
    ],
    fromZigbee: [{cluster: 'gtagFrame', type: ['commandReply'], convert: (model, msg) => {
        const data = Buffer.from(msg.data?.payload ?? []);
        if (data[1] === 7) {
            try { return infoState(parseFirmwareInfo(data)); } catch { return {}; }
        }
        if (data.length === 20 && data[0] === 1 && data[1] === 4 && data[2] === 0) {
            const reply = parseReply(data, 4);
            return {frame_stale: reply.stale, frame_stale_frame_id: reply.renderedId};
        }
        if (data.length !== 40 || data[0] !== 1 || data[1] !== 5 || data[2] !== 0 || (data[3] & ~3)) return {};
        return {power_diagnostics: JSON.stringify(parsePowerDiagnostics(data))};
    }}],
    toZigbee: [frameConverter, powerConverter, freshnessConverter, infoConverter, connectionConverter],
    exposes: [
        e.text('firmware_version', ea.STATE_GET).withCategory('diagnostic'),
        e.text('firmware_codecs', ea.STATE).withCategory('diagnostic'),
        e.text('firmware_features', ea.STATE).withCategory('diagnostic'),
        e.enum('test_image', ea.SET, ['zigbee', 'inverted'])
            .withDescription('Replace the screen with a demo image; restore your layout from GTag Display in HA'),
        e.text('frame_status', ea.STATE).withCategory('diagnostic')
            .withDescription('displayed only after CRC and LCD completion are confirmed'),
        e.text('frame_error', ea.STATE).withCategory('diagnostic'),
        // Discovery marker required by released HA integrations. Keep until a
        // coordinated migration; publication alone does not advertise support.
        e.text('frame_request_id', ea.STATE).withCategory('diagnostic')
            .withDescription('Compatibility marker for GTag HA discovery; not a user setting'),
        e.numeric('frame_transfer_ms', ea.STATE).withUnit('ms').withCategory('diagnostic'),
        e.binary('frame_stale', ea.STATE_GET, true, false).withCategory('diagnostic')
            .withDescription('Read the physical stale-data icon state; does not refresh the image'),
        e.enum('check_connection', ea.SET, ['check'])
            .withDescription('Request a fresh reply from this display; leaves the image and freshness timer unchanged'),
        e.enum('connection_status', ea.STATE, ['checking', 'ok', 'error']).withCategory('diagnostic'),
        e.text('connection_error', ea.STATE).withCategory('diagnostic'),
        e.text('connection_checked_at', ea.STATE).withCategory('diagnostic'),
    ],
    };
}

export default [definition(true), definition(false)];
