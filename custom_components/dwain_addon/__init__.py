"""Dwain-addon: authenticated layout API for the Lovelace fork."""
from __future__ import annotations

import logging

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers import area_registry, device_registry, entity_registry

from custom_components.dwains_dashboard.configuration_runtime import get_configuration_runtime
from custom_components.dwains_dashboard.load_dashboard import invalidate_dashboard_cache

from .layout import LayoutError, LayoutStore

DOMAIN = "dwain_addon"
_LOGGER = logging.getLogger(__name__)


def _registry(hass):
    areas = area_registry.async_get(hass)
    devices = device_registry.async_get(hass)
    entities = entity_registry.async_get(hass)
    area_data = {a.id: {"name": a.name} for a in areas.async_list_areas()}
    entity_data = {}
    for entry in entities.entities.values():
        device = devices.async_get(entry.device_id) if entry.device_id else None
        entity_data[entry.entity_id] = {
            "name": entry.name or entry.original_name or entry.entity_id,
            "area_id": entry.area_id or (device.area_id if device else None),
            "disabled": entry.disabled_by is not None,
        }
    for state in hass.states.async_all():
        entity_data.setdefault(state.entity_id, {
            "name": state.name, "area_id": None, "disabled": False,
        })
    return {"areas": area_data, "entities": entity_data}


def _refresh(hass, runtime):
    runtime.clear_cache()
    invalidate_dashboard_cache(hass)
    # The full reload is used by the existing Lovelace dashboard.
    hass.bus.async_fire("dwains_dashboard_reload")
    hass.bus.async_fire("dwains_dashboard_homepage_card_reload")
    hass.bus.async_fire("dwains_dashboard_devicespage_card_reload")


class LayoutView(HomeAssistantView):
    url = "/api/dwain_addon/layout"
    name = "api:dwain_addon:layout"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass
        self.store = LayoutStore(hass.config.config_dir)

    def _authorize(self, request):
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            raise web.HTTPForbidden()
        if not self.hass.data.get(DOMAIN, {}).get("enabled"):
            raise web.HTTPServiceUnavailable(text="Dwain-addon is not enabled")
        if not any(e.state.value == "loaded" for e in
                   self.hass.config_entries.async_entries("dwains_dashboard")):
            raise web.HTTPServiceUnavailable(text="Dwains Lovelace is not loaded")

    async def get(self, request):
        self._authorize(request)
        hass = self.hass
        runtime = get_configuration_runtime(hass)
        try:
            async with runtime.mutation_lock:
                result = await hass.async_add_executor_job(self.store.snapshot)
                result["registry"] = _registry(hass)
            return self.json(result)
        except LayoutError as exc:
            return self.json({"error": str(exc)}, status_code=400)
        except Exception:
            _LOGGER.exception("Failed to read Dwains layout")
            return self.json({"error": "Layout read failed"}, status_code=500)

    async def post(self, request):
        self._authorize(request)
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return self.json({"error": "Invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return self.json({"error": "Expected an object"}, status_code=400)
        action = body.get("action")
        expected = body.get("revision")
        hass = self.hass
        runtime = get_configuration_runtime(hass)
        try:
            async with runtime.mutation_lock:
                self._authorize(request)
                if action in ("preview", "apply"):
                    registry = _registry(hass)
                    fn = self.store.preview if action == "preview" else self.store.apply
                    args = (body.get("patch"), expected, registry)
                elif action in ("restore_preview", "restore"):
                    fn = self.store.restore_preview if action == "restore_preview" else self.store.restore
                    args = (body.get("backup_id"), expected)
                elif action == "recover":
                    fn, args = self.store.recover, (body.get("backup_id"),)
                else:
                    return self.json({"error": "Unknown action"}, status_code=400)
                # Shield executor writes from client disconnect. Retain shared lock until
                # the disk operation completes even if the request task is cancelled.
                import asyncio
                job = asyncio.ensure_future(hass.async_add_executor_job(fn, *args))
                try:
                    result = await asyncio.shield(job)
                except asyncio.CancelledError:
                    try:
                        await asyncio.shield(job)
                    finally:
                        if action in ("apply", "restore", "recover"):
                            _refresh(hass, runtime)
                    raise
                if result.get("changed") or result.get("recovered"):
                    _refresh(hass, runtime)
            return self.json(result)
        except LayoutError as exc:
            return self.json({"error": str(exc)}, status_code=409)
        except Exception:
            _LOGGER.exception("Dwains layout operation failed")
            return self.json({"error": "Operation failed; read layout and pending status before retrying"},
                             status_code=500)


async def async_setup(hass, config):
    hass.data.setdefault(DOMAIN, {"enabled": False})
    hass.http.register_view(LayoutView(hass))
    return True


async def async_setup_entry(hass, entry):
    hass.data[DOMAIN]["enabled"] = True
    return True


async def async_unload_entry(hass, entry):
    hass.data[DOMAIN]["enabled"] = False
    return True
