"""Options-flow export, native file upload and explicit entity correspondence."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, selector
from homeassistant.setup import async_setup_component

from .const import DOMAIN
from .layout_download import async_export_download
from .layout_transfer import (
    MAX_FILE_BYTES, LayoutFileError, current_screen, decode_document, encode_document,
    entity_domains, entity_references, export_document, remap_settings,
)


def excluded_entities(hass) -> list[str]:
    return [item.entity_id for item in er.async_get(hass).entities.values()
            if item.platform == DOMAIN and not (
                item.domain == "sensor" and item.translation_key == "battery_voltage")]


def read_upload(hass, file_id: str) -> dict:
    # The complete context must run in the executor: it also deletes the upload.
    with process_uploaded_file(hass, file_id) as path:
        with path.open("rb") as stream:
            return decode_document(stream.read(MAX_FILE_BYTES + 1))


class LayoutTransferMixin:
    _import_document: dict | None = None
    _import_mapping: dict | None = None
    _import_index = 0
    _export_text = ""
    _export_name = ""

    async def async_step_export_layout(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                settings = await current_screen(self.hass, self.config_entry)
                document = export_document(settings, user_input["name"])
                self._export_text = encode_document(document)
                await self.hass.async_add_executor_job(decode_document, self._export_text.encode("utf-8"))
                self._export_name = document["name"]
            except LayoutFileError as err:
                errors["base"] = str(err)
            except (vol.Invalid, HomeAssistantError, ValueError, TypeError):
                errors["base"] = "invalid_layout_file"
            else:
                return await self.async_step_export_download()
        return self.async_show_form(step_id="export_layout", data_schema=vol.Schema({
            vol.Required("name", default=self._export_name or "GTag layout"): selector.TextSelector(),
        }), errors=errors, last_step=False)

    async def async_step_export_download(self, user_input=None):
        errors = {}
        try:
            placeholders = await async_export_download(self.hass, self._export_text)
        except HomeAssistantError:
            errors["base"] = "layout_download_unavailable"
            placeholders = {"download_url": "", "download_link_start": "", "download_link_end": "", "filename": "gtag-layout.json"}
        placeholders["json"] = self._export_text
        if user_input is not None and not errors:
            return self.async_abort(reason="layout_exported", description_placeholders=placeholders)
        return self.async_show_form(step_id="export_download", data_schema=vol.Schema({}),
                                    errors=errors, description_placeholders=placeholders, last_step=True)

    async def async_step_import_layout(self, user_input=None):
        errors = {}
        if not await async_setup_component(self.hass, "file_upload", {}):
            return self.async_abort(reason="layout_upload_unavailable")
        if user_input is not None:
            try:
                document = await self.hass.async_add_executor_job(read_upload, self.hass, user_input["file"])
            except LayoutFileError as err:
                errors["file"] = str(err)
            except (OSError, ValueError, HomeAssistantError):
                errors["file"] = "invalid_layout_file"
            else:
                self._import_document = document
                self._import_mapping = {}
                self._import_index = 0
                return await self.async_step_import_entities()
        return self.async_show_form(step_id="import_layout", data_schema=vol.Schema({
            vol.Required("file"): selector.FileSelector(selector.FileSelectorConfig(accept=".json,application/json")),
        }), errors=errors, last_step=False)

    async def async_step_import_entities(self, user_input=None):
        sources = entity_references(self._import_document["screen"])
        registry = er.async_get(self.hass)
        excluded = excluded_entities(self.hass)
        errors = {}
        if user_input is not None and self._import_index < len(sources):
            target = user_input["entity_id"]
            domains = entity_domains(self._import_document["screen"], sources[self._import_index])
            if domains and target.split(".")[0] not in domains:
                errors["entity_id"] = "invalid_entity_mapping"
            elif target in excluded:
                errors["entity_id"] = "invalid_entity"
            elif not self.hass.states.get(target) and not registry.async_get(target):
                errors["entity_id"] = "entity_not_found"
            else:
                self._import_mapping[sources[self._import_index]] = target
                self._import_index += 1
        if self._import_index >= len(sources):
            try:
                self._settings = remap_settings(self._import_document["screen"], self._import_mapping)
            except (vol.Invalid, LayoutFileError):
                # Longer destination IDs may exceed the text-element limit.
                self._import_index = 0
                errors["base"] = "invalid_settings"
            else:
                return await self.async_step_preview()
        source = sources[self._import_index]
        domains = entity_domains(self._import_document["screen"], source)
        suggested = self._import_mapping.get(source)
        if not suggested and source not in excluded and (self.hass.states.get(source) or registry.async_get(source)):
            suggested = source
        schema = vol.Schema({vol.Required("entity_id"): selector.EntitySelector(
            selector.EntitySelectorConfig(exclude_entities=excluded, **({"filter": {"domain": domains}} if domains else {})),
        )})
        if suggested:
            schema = self.add_suggested_values_to_schema(schema, {"entity_id": suggested})
        return self.async_show_form(step_id="import_entities", data_schema=schema, errors=errors,
                                    description_placeholders={"source": source, "index": str(self._import_index + 1),
                                                              "count": str(len(sources))}, last_step=False)

    async def async_step_import_settings(self, user_input=None):
        """Edit imported timing without replacing custom drawing elements."""
        return self.async_show_menu(step_id="import_settings", menu_options=["import_remap", "import_configure"])

    async def async_step_import_configure(self, user_input=None):
        self._import_document = None
        return await self.async_step_configure()

    async def async_step_import_remap(self, user_input=None):
        self._import_index = 0
        return await self.async_step_import_entities()
