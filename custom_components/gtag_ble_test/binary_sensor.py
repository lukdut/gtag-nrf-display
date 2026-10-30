"""Button state via BLE notifications and display freshness."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory

from .entity import GTagEntity
from .const import BUTTON_CHAR_UUID, TRANSPORT_BLE

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    display = entry.runtime_data
    entities = [DisplayStale(display)]

    # Добавляем кнопку только для BLE
    if display.transport == TRANSPORT_BLE:
        entities.append(PhysicalButton(display))

    async_add_entities(entities)


class DisplayStale(GTagEntity, BinarySensorEntity):
    _attr_translation_key = "display_stale"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, display):
        super().__init__(display, "stale")

    @property
    def is_on(self):
        return self.display.stale

    @property
    def extra_state_attributes(self):
        return {"timeout_seconds": self.display.confirmed_timeout,
                "configured_timeout_seconds": self.display.freshness_timeout,
                "last_confirmation": self.display.last_confirmation}


class PhysicalButton(GTagEntity, BinarySensorEntity):
    _attr_name = "Physical Button"

    def __init__(self, display):
        super().__init__(display, "physical_button")
        self._attr_is_on = False
        self._listener_task = None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self._listener_task = self.hass.async_create_background_task(
            self._listen_for_notifications(), f"GTag button {self.display.address}"
        )

    async def async_will_remove_from_hass(self):
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None
        await super().async_will_remove_from_hass()

    async def _listen_for_notifications(self):
        """Подключаемся к плате и слушаем уведомления от кнопки."""
        from .transport import connect, _disconnect, get_operation_lock

        while not self.display._closed:
            client = None
            try:
                lock = get_operation_lock(self.hass, self.display.address)

                # Если экран занят передачей картинки, ждем
                if lock.locked() or self.display.status == "sending":
                    await asyncio.sleep(1)
                    continue

                async with lock:
                    client = await connect(self.hass, self.display.address)

                    def notification_handler(characteristic, data: bytearray):
                        # data[0] == 1 при нажатии, 0 при отпускании
                        state = len(data) > 0 and data[0] == 1
                        if self._attr_is_on != state:
                            self._attr_is_on = state
                            self.async_write_ha_state()

                    await client.start_notify(BUTTON_CHAR_UUID, notification_handler)
                    _LOGGER.debug("%s: Button subscription active", self.display.address)

                # ВАЖНО: Мы выходим из блока lock, чтобы экран мог забрать блокировку.
                # Мы держим соединение. Если экран начнет передачу, он переиспользует
                # это соединение, отправит данные и отключит его.
                while client.is_connected and not self.display._closed:
                    if self.display.status == "sending":
                        break # Уступаем экрану
                    await asyncio.sleep(1)

            except Exception as err:
                _LOGGER.debug("%s: Button listener disconnected: %s", self.display.address, err)
            finally:
                if client:
                    await _disconnect(client)

            if not self.display._closed:
                await asyncio.sleep(1) # Пауза перед переподключением
