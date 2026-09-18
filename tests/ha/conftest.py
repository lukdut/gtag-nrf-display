"""Real Home Assistant fixtures; only the physical BLE transport is replaced."""
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from custom_components.gtag_ble_test import display as display_module
from custom_components.gtag_ble_test import battery as battery_module
from custom_components.gtag_ble_test.const import DOMAIN
from custom_components.gtag_ble_test.display import Display
from custom_components.gtag_ble_test.frame_codec import CODEC_RAW, white_rle_v1_decode
from custom_components.gtag_ble_test.transport import Report
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Allow the real custom component to be discovered by the HA loader."""


@pytest.fixture(autouse=True)
def battery_client(monkeypatch):
    client = SimpleNamespace(
        read_gatt_char=AsyncMock(return_value=(4200).to_bytes(2, "little")),
        disconnect=AsyncMock(),
        clear_cache=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(battery_module, "connect", AsyncMock(return_value=client))
    return client


@pytest.fixture
def sent(monkeypatch):
    frames = []

    async def send(_sender, prepared):
        raw = prepared.payload if prepared.descriptor.codec == CODEC_RAW else white_rle_v1_decode(prepared.payload)
        frames.append(raw)
        descriptor = prepared.descriptor
        return Report(
            descriptor.frame_id, descriptor.raw_crc32, descriptor.codec,
            prepared.codec_name, descriptor.encoded_size, sessions=1,
            freshness_timeout=_sender._freshness_timeout,
            last_status=f"state=complete received={descriptor.encoded_size} error=0",
        )

    monkeypatch.setattr(display_module.FrameSender, "send_prepared", send)
    monkeypatch.setattr(display_module, "MIN_UPDATE_INTERVAL", 0)
    monkeypatch.setattr(display_module, "COALESCE_SECONDS", 0)
    return frames


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, title="GTag Display",
        data={"address": "AA:BB:CC:DD:EE:FF"}, unique_id="AA:BB:CC:DD:EE:FF",
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def display(hass, entry, sent):
    value = Display(hass, entry)
    await value.async_load()
    yield value
    await value.async_close()
