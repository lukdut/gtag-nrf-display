from __future__ import annotations

from typing import Any
import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import DOMAIN, SERVICE_UUID


class GTagBLETestConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._info: BluetoothServiceInfoBleak | None = None
        self._devices: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._info is not None

        if user_input is not None:
            return self.async_create_entry(
                title=self._info.name or self._info.address,
                data={CONF_ADDRESS: self._info.address},
            )

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        configured = self._async_current_ids(include_ignore=False)

        for info in async_discovered_service_info(self.hass):
            uuids = {uuid.lower() for uuid in info.service_uuids}
            if (
                SERVICE_UUID not in uuids
                or info.address in configured
                or info.address in self._devices
            ):
                continue
            self._devices[info.address] = info

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            info = self._devices[address]

            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=info.name or info.address,
                data={CONF_ADDRESS: address},
            )

        if not self._devices:
            return self.async_abort(reason="no_devices_found")

        choices = {
            address: f"{info.name or 'GTag Display'} ({address})"
            for address, info in self._devices.items()
        }

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(choices)}
            ),
        )
