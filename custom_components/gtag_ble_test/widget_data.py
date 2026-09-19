"""Resolve weather and Recorder data without blocking HA's event loop."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
import math

from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import DOMAIN

DATA_CACHE = f"{DOMAIN}.widget_cache"
HISTORY_LIMIT = 10000


def number(value):
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


@dataclass
class CachedData:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    expires: float = 0
    value: dict = field(default_factory=dict)


async def cached(hass, key, lifetime, fetch):
    cache = hass.data.setdefault(DATA_CACHE, OrderedDict())
    slot = cache.setdefault(key, CachedData())
    cache.move_to_end(key)
    # Retain active requests so two displays can share the same fetch.
    for old_key in list(cache):
        if len(cache) <= 32:
            break
        if old_key != key and not cache[old_key].lock.locked():
            del cache[old_key]
    async with slot.lock:
        if hass.loop.time() >= slot.expires:
            try:
                async with asyncio.timeout(20):
                    slot.value = await fetch()
            except (HomeAssistantError, TimeoutError) as err:
                slot.value = {"error": str(err)}
            slot.expires = hass.loop.time() + (60 if slot.value.get("error") else lifetime)
        return slot.value


def read_history(hass, entity_id, hours, now):
    from homeassistant.components.recorder.history import state_changes_during_period

    states = state_changes_during_period(
        hass, now - timedelta(hours=hours), now, entity_id=entity_id,
        limit=HISTORY_LIMIT + 1, include_start_time_state=True,
    ).get(entity_id, [])
    # Keep the original units: changing an entity's unit must not draw a false
    # jump or silently compare values in Celsius and Fahrenheit.
    return {"points": [(s.last_updated.timestamp(), number(s.state),
                        s.attributes.get("unit_of_measurement") or "") for s in states],
            "truncated": len(states) >= HISTORY_LIMIT + 1}


async def resolve_widget(hass, item):
    now = dt_util.utcnow()
    state = hass.states.get(item["entity_id"])
    attrs = state.attributes if state is not None else {}
    available = state is not None and state.state not in ("unknown", "unavailable")
    common = {"now": dt_util.as_local(now), "label": (item.get("label", "").strip()
              or str(attrs.get("friendly_name") or item["entity_id"]))[:80]}
    if item["type"] == "weather":
        async def forecast():
            response = await hass.services.async_call(
                "weather", "get_forecasts", {"entity_id": item["entity_id"], "type": "hourly"},
                blocking=True, return_response=True,
            )
            return {"forecast": (response or {}).get(item["entity_id"], {}).get("forecast", [])}

        data = await cached(hass, ("weather", item["entity_id"]), 600, forecast) if available else {}
        forecasts = {}
        for entry in data.get("forecast") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("datetime"), str):
                continue
            stamp = dt_util.parse_datetime(entry["datetime"])
            if stamp is None or stamp.tzinfo is None or stamp <= now:
                continue
            forecasts[stamp] = {"time": dt_util.as_local(stamp), "condition": entry.get("condition"),
                                "temperature": number(entry.get("temperature"))}
        return {**common, "condition": state.state if available else "unknown",
                "temperature": number(attrs.get("temperature")) if available else None,
                "apparent_temperature": number(attrs.get("apparent_temperature")) if available else None,
                "humidity": number(attrs.get("humidity")) if available else None,
                "unit": str(attrs.get("temperature_unit") or "")[:16],
                "forecast": [forecasts[key] for key in sorted(forecasts)[:6]]}

    native_unit = attrs.get("unit_of_measurement") or ""
    async def history():
        if "recorder" not in hass.config.components:
            return {"error": "Recorder is not loaded"}
        from homeassistant.components.recorder import get_instance
        from sqlalchemy.exc import SQLAlchemyError

        try:
            return await get_instance(hass).async_add_executor_job(
                partial(read_history, hass, item["entity_id"], item["hours"], now))
        except SQLAlchemyError as err:
            return {"error": str(err)}

    data = await cached(hass, ("history", item["entity_id"], item["hours"]), 60, history)
    start = now - timedelta(hours=item["hours"])
    points = [(stamp, value if unit == native_unit else None)
              for stamp, value, unit in data.get("points", [])]
    current = number(state.state) if available else None
    if points and not data.get("truncated"):
        points.append((now.timestamp(), current))
    return {**common, "value": current, "state": state.state if available else "—",
            "unit": native_unit, "start": dt_util.as_local(start), "points": points,
            "truncated": data.get("truncated", False)}
