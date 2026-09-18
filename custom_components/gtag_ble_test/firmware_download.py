"""Short-lived, authenticated YAML downloads for preparation flows."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from .const import DOMAIN

DATA_DOWNLOADS = f"{DOMAIN}.firmware_downloads"
DOWNLOAD_TTL = timedelta(minutes=20)


@dataclass
class Download:
    filename: str
    yaml: str
    expires: float
    finished: bool = False


class FirmwareDownloadView(HomeAssistantView):
    url = f"/api/{DOMAIN}/firmware/{{flow_id}}"
    name = f"api:{DOMAIN}:firmware"
    requires_auth = True

    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self.downloads: OrderedDict[str, Download] = OrderedDict()

    def add(self, flow_id: str, filename: str, yaml: str, *, finished: bool = False) -> None:
        now = monotonic()
        for key in list(self.downloads):
            if self.downloads[key].expires <= now:
                del self.downloads[key]
        self.downloads.pop(flow_id, None)
        self.downloads[flow_id] = Download(filename, yaml, now + DOWNLOAD_TTL.total_seconds(), finished)
        while len(self.downloads) > 32:
            self.downloads.popitem(last=False)

    async def get(self, request: web.Request, flow_id: str) -> web.Response:
        item = self.downloads.get(flow_id)
        try:
            flow = self.hass.config_entries.flow.async_get(flow_id)
        except UnknownFlow:
            flow = {}
        if (item is None or item.expires <= monotonic()
                or (not item.finished and (flow.get("handler") != DOMAIN
                    or flow.get("step_id") != "firmware_download"))):
            self.downloads.pop(flow_id, None)
            raise web.HTTPNotFound()
        return web.Response(text=item.yaml, content_type="application/yaml", charset="utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{item.filename}"',
                                     "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


async def async_prepare_download(
    hass: HomeAssistant, flow_id: str, filename: str, yaml: str, *, finished: bool = False,
) -> str:
    if not await async_setup_component(hass, "http", {}):
        raise HomeAssistantError("HTTP is unavailable")
    if (view := hass.data.get(DATA_DOWNLOADS)) is None:
        view = FirmwareDownloadView(hass)
        hass.http.register_view(view)
        hass.data[DATA_DOWNLOADS] = view
    url = async_sign_path(hass, f"/api/{DOMAIN}/firmware/{flow_id}", DOWNLOAD_TTL)
    view.add(flow_id, filename, yaml, finished=finished)
    return url
