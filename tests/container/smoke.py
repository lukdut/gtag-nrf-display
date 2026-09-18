"""Black-box check of an installed ZIP in a running, initially empty HA Container.

Only the MQTT device is simulated. HA, its config/options flows, authentication,
downloads, uploads, rendering, persistence and MQTT client are real. This client
never imports HA/custom_components or edits .storage.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import secrets
import struct
import subprocess
import zlib

import aiohttp
import paho.mqtt.client as mqtt
import yaml

DOMAIN = "gtag_ble_test"
IEEE = "0x0011223344556677"
NAME = "container-test-display"
BASE = "http://127.0.0.1:18123"
FLOW = "/api/config/config_entries/flow"
OPTIONS = "/api/config/config_entries/options/flow"
INFO = {
    "firmware_version": "0.9.0", "schema": 1, "protocol_min": 1, "protocol_max": 1,
    "pixel_format": 1, "codecs": 3, "features": 15, "width": 256, "height": 128,
    "max_encoded_size": 4096, "max_chunk_size": 32, "legacy": False,
}


class Device:
    """Zigbee2MQTT-facing simulator, isolated from every real MQTT installation."""

    def __init__(self):
        self.frames = []
        self.checks = 0
        self.check_error = False
        self.errors = []
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="gtag-container-check")
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

    def publish(self, topic, data, *, retain=False):
        self.client.publish(f"zigbee2mqtt/{topic}", json.dumps(data), qos=1, retain=retain)

    def on_connect(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            self.errors.append(f"MQTT connect: {reason}")
            return
        client.subscribe(f"zigbee2mqtt/{IEEE}/set", qos=1)
        self.publish("bridge/devices", [{
            "ieee_address": IEEE, "friendly_name": NAME, "model_id": "GTag_Display_Frame_V1",
            "definition": {"exposes": [{"property": p} for p in ("frame_request_id", "check_connection")]},
        }], retain=True)
        self.publish("bridge/state", {"state": "online"}, retain=True)
        self.publish(f"{NAME}/availability", {"state": "online"}, retain=True)
        self.publish(NAME, {"battery_voltage_1": 4.1, "firmware_capabilities": INFO,
                            "last_seen": datetime.now(timezone.utc).isoformat()}, retain=True)

    def on_message(self, client, userdata, message):
        try:
            assert not message.retain, "HA sent a retained command"
            data = json.loads(message.payload)
            if "frame" in data:
                frame = data["frame"]
                raw = base64.b64decode(frame["data"], validate=True)
                assert len(raw) == 4096 and set(raw) != {255}, "Empty or malformed framebuffer"
                self.frames.append(raw)
                self.publish(NAME, {
                    "frame_status": "displayed", "frame_request_id": frame["request_id"],
                    "frame_crc32": f"{zlib.crc32(raw):08x}", "frame_id": len(self.frames),
                    "frame_freshness_timeout": frame["freshness_timeout"], "frame_codec": 0,
                    "frame_bytes": len(raw), "frame_transfer_ms": 100, "frame_retries": 0,
                    "firmware_capabilities": INFO,
                })
            elif "frame_freshness" in data:
                frame = data["frame_freshness"]
                self.publish(NAME, {
                    "freshness_status": "confirmed", "freshness_request_id": frame["request_id"],
                    "freshness_frame_id": frame["frame_id"], "freshness_crc32": f"{frame['crc']:08x}",
                    "freshness_sequence": frame["sequence"],
                })
            elif "check_connection" in data:
                self.checks += 1
                self.publish(NAME, {
                    "connection_request_id": data["check_connection"]["request_id"],
                    "connection_status": "error" if self.check_error else "ok",
                    "connection_error_code": "timeout" if self.check_error else "",
                    "connection_error": "Simulated radio timeout" if self.check_error else "",
                    "firmware_capabilities": INFO,
                })
            else:
                raise AssertionError(f"Unexpected command keys: {list(data)}")
        except Exception as err:
            self.errors.append(str(err))

    async def start(self):
        async with asyncio.timeout(30):
            while True:
                try:
                    await asyncio.to_thread(self.client.connect, "127.0.0.1", 18883)
                    break
                except OSError:
                    await asyncio.sleep(1)
        self.client.loop_start()

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()


class Check:
    def __init__(self, session, device, output):
        self.session, self.device, self.output = session, device, output
        self.token = None
        self.passed = []
        self.entry_id = None
        self.entities = {}

    def passed_step(self, label):
        assert not self.device.errors, self.device.errors
        self.passed.append(label)
        print(f"PASS: {label}", flush=True)

    async def request(self, method, path, *, auth=True, **kwargs):
        headers = {"Authorization": f"Bearer {self.token}"} if auth and self.token else {}
        async with self.session.request(method, BASE + path, headers=headers, **kwargs) as response:
            if response.status >= 400:
                # Strip signed query strings from diagnostics.
                raise AssertionError(f"{method} {path.split('?')[0]}: {response.status}: {(await response.text())[:600]}")
            return await response.json()

    async def wait_ready(self):
        async with asyncio.timeout(180):
            while True:
                try:
                    async with self.session.get(BASE + "/api/onboarding") as response:
                        if response.status in (200, 401):
                            return
                except (aiohttp.ClientError, TimeoutError):
                    pass
                await asyncio.sleep(1)

    async def wait_for(self, operation, predicate, label, timeout=60):
        async with asyncio.timeout(timeout):
            while True:
                assert not self.device.errors, self.device.errors
                value = await operation()
                if predicate(value):
                    return value
                await asyncio.sleep(0.5)

    async def ws(self, command):
        async with self.session.ws_connect(BASE + "/api/websocket") as socket:
            assert (await socket.receive_json())["type"] == "auth_required"
            await socket.send_json({"type": "auth", "access_token": self.token})
            assert (await socket.receive_json())["type"] == "auth_ok"
            await socket.send_json({"id": 1, **command})
            while True:
                result = await socket.receive_json()
                if result.get("id") == 1:
                    assert result["success"], result
                    return result["result"]

    async def step(self, flow, data, expected=None, *, options=False):
        result = await self.request("POST", f"{OPTIONS if options else FLOW}/{flow['flow_id']}", json=data)
        assert not result.get("errors"), result.get("errors")
        if expected:
            assert result.get("step_id", result["type"]) == expected, result
        return result

    async def start_flow(self, handler=DOMAIN, *, options=False):
        return await self.request("POST", OPTIONS if options else FLOW, json={"handler": handler})

    async def options(self, step):
        flow = await self.start_flow(self.entry_id, options=True)
        return await self.step(flow, {"next_step_id": step}, step, options=True)

    async def download(self, path, filename, content_type):
        # Signed link must work in a new tab without an Authorization header.
        async with self.session.get(BASE + path) as response:
            assert response.status == 200, response.status
            assert response.content_type == content_type, response.content_type
            assert response.headers["Content-Disposition"] == f'attachment; filename="{filename}"'
            data = await response.read()
        async with self.session.get(BASE + path.split("?")[0]) as response:
            assert response.status == 401, "Unsigned download unexpectedly public"
        return data

    async def onboard(self):
        await self.wait_ready()
        onboarding = await self.request("GET", "/api/onboarding", auth=False)
        assert not any(item["done"] for item in onboarding), "HA config was not empty"
        created = await self.request("POST", "/api/onboarding/users", auth=False, json={
            "name": "Container tester", "username": "container-test", "password": secrets.token_urlsafe(32),
            "client_id": BASE + "/", "language": "en",
        })
        tokens = await self.request("POST", "/auth/token", auth=False, data={
            "grant_type": "authorization_code", "code": created["auth_code"], "client_id": BASE + "/",
        })
        self.token = tokens["access_token"]
        for step in ("core_config", "analytics"):
            await self.request("POST", f"/api/onboarding/{step}", json={})
        await self.request("POST", "/api/onboarding/integration", json={
            "client_id": BASE + "/", "redirect_uri": BASE + "/?auth_callback=1",
        })
        config = await self.request("GET", "/api/config")
        assert "hassio" not in config["components"]
        assert config["version"] == os.environ.get("HA_VERSION", "2026.9.2"), config["version"]
        self.passed_step("Fresh onboarding, official HA version, no Supervisor")

    async def wizard(self):
        flow = await self.start_flow()
        flow = await self.step(flow, {"next_step_id": "firmware"}, "firmware")
        flow = await self.step(flow, {"board": "promicro", "transport": "zigbee",
            "name": "gtag-container-check", "friendly_name": "GTag Container Check"}, "firmware_pins")
        flow = await self.step(flow, {"dio_pin": "P0.11", "clk_pin": "P1.04", "cs_pin": "P1.06",
            "reset_pin": "P1.13", "battery_enabled": True}, "firmware_battery")
        flow = await self.step(flow, {"battery_pin": "P0.31", "calibration": 1.0,
            "empty_voltage": 3.306, "full_voltage": 4.19, "recovery_voltage": 3.45,
            "indicator": True}, "firmware_download")
        placeholders = flow["description_placeholders"]
        downloaded = await self.download(placeholders["download_url"], "gtag-container-check.yaml", "application/yaml")
        assert downloaded.decode() == placeholders["yaml"]
        document = yaml.safe_load(downloaded)
        assert document["gtag_display"]["battery_voltage"]["enabled"] is True
        self.package = document["packages"]["gtag"]
        (self.output / "gtag-container-check.yaml").write_bytes(downloaded)
        finished = await self.step(flow, {"action": "finish"}, "abort")
        assert finished["reason"] == "firmware_ready"
        assert await self.download(finished["description_placeholders"]["download_url"],
                                   "gtag-container-check.yaml", "application/yaml") == downloaded
        assert not await self.request("GET", "/api/config/config_entries/entry?domain=" + DOMAIN)
        self.passed_step("Wizard before MQTT setup; signed YAML downloads before/after Finish; no dummy entry")

    async def setup_device(self):
        await self.device.start()
        flow = await self.start_flow("mqtt")
        await self.step(flow, {"broker": "mqtt", "port": 1883, "protocol": "3.1.1",
            "other_settings": {"set_client_cert": False, "set_ca_cert": "off", "transport": "tcp"}}, "create_entry")
        await self.wait_for(lambda: self.request("GET", "/api/config/config_entries/entry?domain=mqtt"),
                            lambda entries: len(entries) == 1 and entries[0]["state"] == "loaded", "MQTT setup")
        flow = await self.start_flow()
        flow = await self.step(flow, {"next_step_id": "zigbee"}, "zigbee")
        flow = await self.step(flow, {"base_topic": "zigbee2mqtt"}, "zigbee_device")
        flow = await self.step(flow, {"address": IEEE}, "create_entry")
        self.entry_id = flow["result"]["entry_id"]
        await self.wait_for(lambda: self.request("GET", "/api/config/config_entries/entry?domain=" + DOMAIN),
                            lambda entries: len(entries) == 1 and entries[0]["state"] == "loaded", "GTag setup")
        entities = await self.ws({"type": "config/entity_registry/list"})
        self.entities = {e["unique_id"].removeprefix(f"zigbee:{IEEE}_"): e["entity_id"]
                         for e in entities if e["config_entry_id"] == self.entry_id}
        self.device_id = next(e["device_id"] for e in entities if e["config_entry_id"] == self.entry_id)
        assert {"preview", "last_update", "transfer", "connection_check", "battery_voltage"} <= self.entities.keys()
        await self.wait_for(lambda: self.state("battery_voltage"), lambda s: s["state"] == "4.1", "Battery report")
        self.passed_step("Real MQTT integration, retained discovery, GTag config entry and battery entities")

    async def state(self, key):
        return await self.request("GET", "/api/states/" + self.entities[key])

    async def transfer_done(self, previous_count):
        return await self.wait_for(lambda: self.state("transfer"),
            lambda s: len(self.device.frames) > previous_count and s["state"] in ("sent", "unchanged"),
            "Confirmed framebuffer")

    async def preview(self):
        state = await self.state("preview")
        headers = {"Authorization": f"Bearer {self.token}"}
        async with self.session.get(BASE + state["attributes"]["entity_picture"], headers=headers) as response:
            assert response.status == 200 and response.content_type == "image/png"
            png = await response.read()
            assert png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack_from(">II", png, 16) == (256, 128)
            return png

    async def layout(self):
        flow = await self.options("configure")
        flow = await self.step(flow, {"preset": "clock_two_values", "update_interval": 30,
                                     "stale_after": 5}, "values", options=True)
        flow = await self.step(flow, {"entity_1": "input_number.gtag_temperature", "label_1": "Температура",
            "unit_1": "°C", "decimals_1": "1", "entity_2": "input_number.gtag_humidity",
            "label_2": "Влажность", "unit_2": "%", "decimals_2": "0"}, "preview", options=True)
        assert '<svg xmlns=' in flow["description_placeholders"]["preview"]
        assert not self.device.frames, "Preview sent a radio frame before Apply"
        flow = await self.step(flow, {"action": "apply"}, "create_entry", options=True)
        await self.transfer_done(0)
        before = self.device.frames[-1]
        self.output.joinpath("preview.png").write_bytes(await self.preview())
        count = len(self.device.frames)
        await self.request("POST", "/api/services/input_number/set_value", json={
            "entity_id": "input_number.gtag_temperature", "value": 32.1})
        await self.transfer_done(count)
        assert self.device.frames[-1] != before, "HA entity change did not change the framebuffer"
        self.passed_step("Two values + clock, Cyrillic fonts, local preview, Apply, PNG and automatic entity updates")

    async def export(self):
        flow = await self.options("export_layout")
        flow = await self.step(flow, {"name": "Container layout"}, "export_download", options=True)
        data = await self.download(flow["description_placeholders"]["download_url"],
                                   "gtag-layout.json", "application/json")
        assert json.loads(data)["format"] == "gtag-display-layout"
        await self.step(flow, {}, "abort", options=True)
        return data

    async def layout_transfer(self):
        data = await self.export()
        flow = await self.options("import_layout")
        body = aiohttp.FormData()
        body.add_field("file", data, filename="layout.json", content_type="application/json")
        uploaded = await self.request("POST", "/api/file_upload", data=body)
        flow = await self.step(flow, {"file": uploaded["file_id"]}, "import_entities", options=True)
        mapping = {"input_number.gtag_temperature": "input_number.gtag_replacement",
                   "input_number.gtag_humidity": "input_number.gtag_humidity"}
        while flow.get("step_id") == "import_entities":
            source = flow["description_placeholders"]["source"]
            flow = await self.step(flow, {"entity_id": mapping[source]}, options=True)
        assert flow["step_id"] == "preview" and flow["description_placeholders"]["preview"]
        count = len(self.device.frames)
        await self.step(flow, {"action": "apply"}, "create_entry", options=True)
        await self.transfer_done(count)
        self.exported = json.loads(await self.export())
        assert self.exported["screen"]["entity_1"] == "input_number.gtag_replacement"
        self.output.joinpath("layout.json").write_text(json.dumps(self.exported, ensure_ascii=False, indent=2))
        self.passed_step("Authenticated JSON download, real file upload, explicit entity remap and Apply")

    async def diagnostics(self):
        flow = await self.options("diagnostics")
        info = flow["description_placeholders"]
        assert info["firmware"] == INFO["firmware_version"] and info["last_update"] != "—"
        count = self.device.checks
        flow = await self.step(flow, {"action": "check"}, options=True)
        async with asyncio.timeout(60):
            while flow.get("type") in ("progress", "progress_done"):
                await asyncio.sleep(0.2)
                flow = await self.step(flow, {}, options=True)
        assert flow["step_id"] == "diagnostics" and self.device.checks == count + 1
        status = await self.state("connection_check")
        assert status["state"] == "ok" and status["attributes"]["checked_at"]
        await self.request("DELETE", f"{OPTIONS}/{flow['flow_id']}")
        self.device.check_error = True
        response = await self.request("POST", f"/api/services/{DOMAIN}/check_connection?return_response",
                                      json={"device_id": self.device_id})
        assert response["service_response"]["status"] == "error"
        assert response["service_response"]["error_code"] == "timeout"
        self.device.check_error = False
        response = await self.request("POST", f"/api/services/{DOMAIN}/check_connection?return_response",
                                      json={"device_id": self.device_id})
        assert response["service_response"]["status"] == "ok"
        self.passed_step("Diagnostic page and Check action; fresh MQTT response, clear timeout, recovery")

    async def restart(self):
        count = len(self.device.frames)
        await asyncio.to_thread(subprocess.run, ["docker", "compose", "-p", os.environ["GTAG_COMPOSE_PROJECT"],
            "-f", "tests/container/compose.yaml", "restart", "homeassistant"], check=True)
        await self.wait_ready()
        await self.transfer_done(count)
        assert json.loads(await self.export())["screen"] == self.exported["screen"]
        assert (await self.state("last_update"))["state"] not in ("unknown", "unavailable")
        assert await self.preview()
        self.passed_step("Container restart: saved layout/remapping, entities, MQTT reconnect and new frame")


async def main():
    output = Path(os.environ["ARTIFACT_DIR"])
    device = Device()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        check = Check(session, device, output)
        try:
            for task in (check.onboard, check.wizard, check.setup_device, check.layout,
                         check.layout_transfer, check.diagnostics, check.restart):
                print(f"RUN: {task.__name__}", flush=True)
                await task()
        finally:
            device.close()
            output.joinpath("report.json").write_text(json.dumps({
                "ha_image": os.environ["HA_IMAGE"], "host_arch": platform.machine(),
                "commit": os.environ.get("GITHUB_SHA"), "passed": check.passed,
                "firmware_package": getattr(check, "package", None),
                "integration_zip_sha256": sha256(Path("dist/gtag-ha-integration.zip").read_bytes()).hexdigest(),
                "simulated_device": True, "mqtt_frames": len(device.frames), "mqtt_checks": device.checks,
                "simulator_errors": device.errors,
            }, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
