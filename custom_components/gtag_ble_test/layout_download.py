"""Authenticated, temporary downloads of layout snapshots."""
from collections import OrderedDict
from datetime import timedelta
from html import escape
import secrets
from time import monotonic

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from .const import DOMAIN

TTL = timedelta(minutes=20)
DATA = f"{DOMAIN}.layout_downloads"


class LayoutDownloadView(HomeAssistantView):
    url = f"/api/{DOMAIN}/layouts/{{token}}"
    name = f"api:{DOMAIN}:layouts"
    requires_auth = True

    def __init__(self):
        self.exports = OrderedDict()

    async def get(self, request, token):
        if (item := self.exports.get(token)) is None or item[0] <= monotonic():
            self.exports.pop(token, None)
            raise web.HTTPNotFound()
        return web.Response(text=item[1], content_type="application/json", charset="utf-8", headers={
            "Content-Disposition": 'attachment; filename="gtag-layout.json"',
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })


async def async_export_download(hass, text: str) -> dict[str, str]:
    if not await async_setup_component(hass, "http", {}):
        raise HomeAssistantError("HTTP is unavailable")
    if (view := hass.data.get(DATA)) is None:
        view = LayoutDownloadView()
        hass.http.register_view(view)
        hass.data[DATA] = view
    for key, (expires, _) in list(view.exports.items()):
        if expires <= monotonic():
            del view.exports[key]
    token = secrets.token_hex(16)
    url = async_sign_path(hass, f"/api/{DOMAIN}/layouts/{token}", TTL)
    view.exports[token] = (monotonic() + TTL.total_seconds(), text)
    while len(view.exports) > 32:
        view.exports.popitem(last=False)
    return {"download_url": url, "download_link_start": f'<a href="{escape(url, quote=True)}" target="_blank">',
            "download_link_end": "</a>", "filename": "gtag-layout.json"}
