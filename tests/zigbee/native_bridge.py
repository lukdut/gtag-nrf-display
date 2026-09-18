"""Line-based bridge to the production C++ receiver and LCD driver for JS tests."""
import ctypes
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_display as native

native.setUpModule()
fw = native.fw_zigbee
hold = False
print('ready', flush=True)
try:
    for line in sys.stdin:
        message = json.loads(line)
        command = message['command']
        if command == 'reset':
            fw.firmware_create(0, 1000)
            fw.firmware_run(4000)
            hold = False
            result = {}
        elif command == 'packet':
            data = bytes.fromhex(message['hex'])
            reply = ctypes.create_string_buffer(20)
            fw.firmware_packet(data, len(data), reply)
            if not hold:
                fw.firmware_run(0)
            result = {'hex': reply.raw.hex()}
        elif command == 'hold':
            hold = message['value']
            result = {}
        elif command == 'advance':
            fw.firmware_run(message['ms'])
            result = {}
        elif command == 'inspect':
            count = fw.firmware_words()
            raw = bytes(fw.firmware_word(i) & 255 for i in range(count - 4096, count))
            result = {'frames': fw.firmware_frames(), 'raw': raw.hex(),
                      'advertising': fw.firmware_adv_attempts()}
        else:
            raise ValueError(command)
        print(json.dumps(result), flush=True)
finally:
    native.tearDownModule()
