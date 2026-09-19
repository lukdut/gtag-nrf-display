"""One readable diagnostic page in the device's options flow."""
from __future__ import annotations

import asyncio
import html
import re

import voluptuous as vol
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .display import Display


def safe_text(value) -> str:
    """Firmware/error text is data, never Markdown links, images or HTML."""
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", html.escape(str(value)[:1500])).replace("\n", " ")


class DiagnosticFlowMixin:
    _connection_task = None

    async def async_step_diagnostics(self, user_input=None):
        display = getattr(self.config_entry, "runtime_data", None)
        if not isinstance(display, Display):
            return self.async_abort(reason="diagnostics_unavailable")
        if user_input is not None:
            if user_input["action"] == "back":
                return await self.async_step_init()
            if user_input["action"] == "check":
                return await self.async_step_check_connection()
        def label(key):
            # HA options HTTP flows carry no profile language. Return enum keys;
            # the frontend's ICU message translates them in the user's language.
            return key

        def timestamp(value):
            return dt_util.as_local(value).strftime("%Y-%m-%d %H:%M:%S %Z") if value else label("unknown")

        info = display.device_firmware_info
        check = display.connection_check
        features = info.get("firmware_features")
        legacy = info.get("firmware_legacy")
        placeholders = {
            "transport": "Zigbee2MQTT" if display.zigbee else "Bluetooth",
            "firmware": safe_text(label("legacy") if legacy else info.get("firmware_version") or label("unknown")),
            "features": (label("unknown") if features is None or legacy else
                         "_".join(features) or label("none")),
            "codecs": "_".join(info.get("firmware_codecs", [])) or label("unknown"),
            "last_update": timestamp(display.last_success),
            "last_confirmation": timestamp(display.last_confirmation),
            "display_status": label("transfer_error" if display.status == "error" else display.status),
            "display_error": safe_text(display.last_error) if display.last_error else label("none"),
            "stale": label("unknown" if display.stale is None else "stale" if display.stale else "fresh"),
            "check_status": label(check.status),
            "checked_at": timestamp(check.checked_at),
            "duration": str(check.seconds) if check.seconds is not None else "—",
            "check_error": safe_text(label(check.error_code)) if check.error_code else label("none"),
            "check_detail": safe_text(check.detail) if check.detail else label("none"),
            "battery": (label("disabled") if display.battery.supported is False else
                        f"{display.battery.voltage:.3f} V" if display.battery.voltage is not None else label("unknown")),
        }
        return self.async_show_form(step_id="diagnostics", data_schema=vol.Schema({
            vol.Required("action", default="check"): selector.SelectSelector(
                selector.SelectSelectorConfig(options=["check", "refresh", "back"],
                    translation_key="diagnostic_action", mode=selector.SelectSelectorMode.DROPDOWN)),
        }), description_placeholders=placeholders)

    async def async_step_check_connection(self, user_input=None):
        display = getattr(self.config_entry, "runtime_data", None)
        if not isinstance(display, Display):
            return self.async_show_progress_done(next_step_id="diagnostics_result")
        if self._connection_task is None:
            self._connection_task = self.hass.async_create_background_task(
                display.connection_check.async_run(), "GTag diagnostic options check")
        if not self._connection_task.done():
            return self.async_show_progress(step_id="check_connection", progress_action="checking_connection",
                                            progress_task=self._connection_task)
        try:
            self._connection_task.result()
        except asyncio.CancelledError:
            pass  # Device unload cancels the radio check; the result page explains it.
        self._connection_task = None
        # HA can run short tasks eagerly. Do not reuse the submitted "check"
        # action if it advances to the result in the same HTTP request.
        return self.async_show_progress_done(next_step_id="diagnostics_result")

    async def async_step_diagnostics_result(self, user_input=None):
        return await self.async_step_diagnostics()
