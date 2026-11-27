"""Exercise the actual view and lifecycle with explicit HA adapters, no live HA."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from aiohttp import web


def module(name, **attrs):
    value = types.ModuleType(name)
    value.__dict__.update(attrs)
    return value


class View:
    def json(self, result, status_code=200):
        return types.SimpleNamespace(status=status_code, data=result)


class Request(dict):
    def __init__(self, body=None, admin=True):
        super().__init__(hass_user=types.SimpleNamespace(is_admin=admin))
        self.body = body

    async def json(self):
        return self.body


class Runtime:
    def __init__(self):
        self.mutation_lock = asyncio.Lock()
        self.clears = 0

    def clear_cache(self):
        self.clears += 1


class FakeHass:
    def __init__(self, root):
        self.data = {}
        self.config = types.SimpleNamespace(config_dir=root)
        self.runtime = Runtime()
        self.config_entries = types.SimpleNamespace(async_entries=lambda domain: [
            types.SimpleNamespace(state=types.SimpleNamespace(value=self.dwains_state))
        ])
        self.dwains_state = "loaded"
        self.states = types.SimpleNamespace(async_all=lambda: [
            types.SimpleNamespace(entity_id="light.test", name="Test light")
        ])
        self.areas = types.SimpleNamespace(async_list_areas=lambda: [
            types.SimpleNamespace(id="living", name="Living")
        ])
        self.devices = types.SimpleNamespace(async_get=lambda ident: None)
        self.entities = types.SimpleNamespace(entities={})
        self.events = []
        self.bus = types.SimpleNamespace(async_fire=self.events.append)
        self.cache_invalidations = 0
        self.panels = {}
        self.views = []
        self.static = []
        self.http = types.SimpleNamespace(
            register_view=self.views.append,
            async_register_static_paths=self.register_static,
        )

    async def register_static(self, paths):
        self.static.extend(paths)

    async def register_panel(self, path, name, **kwargs):
        self.panels[path] = (name, kwargs)

    async def async_add_executor_job(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    def async_create_task(self, coro):
        return asyncio.create_task(coro)


def load_integration():
    frontend = module("homeassistant.components.frontend",
        async_panel_exists=lambda hass, path: path in hass.panels,
        async_remove_panel=lambda hass, path: hass.panels.pop(path))
    async def register_panel(hass, *args, **kwargs):
        await hass.register_panel(*args, **kwargs)
    panels = module("homeassistant.components.panel_custom", async_register_panel=register_panel)
    components = module("homeassistant.components", frontend=frontend, panel_custom=panels)
    http = module("homeassistant.components.http", HomeAssistantView=View,
                  StaticPathConfig=lambda *args: args)
    helpers = module("homeassistant.helpers",
        area_registry=types.SimpleNamespace(async_get=lambda hass: hass.areas),
        device_registry=types.SimpleNamespace(async_get=lambda hass: hass.devices),
        entity_registry=types.SimpleNamespace(async_get=lambda hass: hass.entities))
    modules = {
        "homeassistant": module("homeassistant"),
        "homeassistant.components": components,
        "homeassistant.components.http": http,
        "homeassistant.helpers": helpers,
        "custom_components.dwains_dashboard.configuration_runtime":
            module("custom_components.dwains_dashboard.configuration_runtime", get_configuration_runtime=lambda hass: hass.runtime),
        "custom_components.dwains_dashboard.load_dashboard":
            module("custom_components.dwains_dashboard.load_dashboard", invalidate_dashboard_cache=lambda hass: setattr(hass, "cache_invalidations", hass.cache_invalidations + 1)),
    }
    path = Path(__file__).parents[1] / "custom_components/dwain_addon/__init__.py"
    spec = importlib.util.spec_from_file_location("dwain_addon_adapter_test", path, submodule_search_locations=[str(path.parent)])
    result = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        sys.modules[spec.name] = result
        spec.loader.exec_module(result)
    return result


integration = load_integration()


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.hass = FakeHass(self.temp.name)
        self.assertTrue(await integration.async_setup(self.hass, {}))
        self.assertTrue(await integration.async_setup_entry(self.hass, None))
        self.view = self.hass.views[0]
        self.rev = (await self.view.get(Request())).data["revision"]
        self.body = {"action": "apply", "revision": self.rev,
                     "patch": {"entities": {"light.test": {"sort_order": 1}}}}

    async def test_lifecycle_and_admin_panel(self):
        self.assertTrue(self.view.requires_auth)
        name, config = self.hass.panels["dwain-addon"]
        self.assertEqual(name, "dwain-addon-manager")
        self.assertTrue(config["require_admin"])
        self.assertEqual(len(self.hass.static), 1)
        self.assertTrue(await integration.async_unload_entry(self.hass, None))
        self.assertFalse(self.hass.panels)
        with self.assertRaises(web.HTTPServiceUnavailable):
            await self.view.get(Request())

    async def test_non_admin_cannot_read_or_write(self):
        with self.assertRaises(web.HTTPForbidden):
            await self.view.get(Request(admin=False))
        with self.assertRaises(web.HTTPForbidden):
            await self.view.post(Request(self.body, admin=False))
        self.assertFalse(self.view.store.backups.exists())

    async def test_dwains_unloaded_blocks_api(self):
        self.hass.dwains_state = "not_loaded"
        with self.assertRaises(web.HTTPServiceUnavailable):
            await self.view.post(Request(self.body))

    async def test_preview_and_apply_refresh(self):
        response = await self.view.post(Request({**self.body, "action": "preview"}))
        self.assertEqual(response.status, 200)
        self.assertFalse(self.hass.events)
        response = await self.view.post(Request(self.body))
        self.assertEqual(response.status, 200)
        self.assertTrue(response.data["changed"])
        self.assertEqual(self.hass.runtime.clears, 1)
        self.assertEqual(self.hass.cache_invalidations, 1)
        self.assertIn("dwains_dashboard_reload", self.hass.events)
        response = await self.view.post(Request(self.body))
        self.assertEqual(response.status, 409)
        self.assertEqual(self.hass.runtime.clears, 1)

    async def test_invalid_id_and_field_blocked(self):
        for patch_data in ({"entities": {"light.unknown": {"sort_order": 1}}},
                           {"entities": {"light.test": {"custom_card": True}}}):
            response = await self.view.post(Request({**self.body, "patch": patch_data}))
            self.assertEqual(response.status, 409)
        self.assertFalse(self.view.store.backups.exists())

    async def test_concurrent_requests_same_revision_only_one_writes(self):
        response1, response2 = await asyncio.gather(
            self.view.post(Request(self.body)), self.view.post(Request(self.body)))
        self.assertEqual(sorted([response1.status, response2.status]), [200, 409])
        self.assertEqual(len(self.view.store.list_backups()), 1)

    async def test_client_cancel_does_not_release_mutation_lock(self):
        entered, release = threading.Event(), threading.Event()
        apply = self.view.store.apply
        def blocked(*args):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test failed to release worker")
            return apply(*args)
        self.view.store.apply = blocked
        first = asyncio.create_task(self.view.post(Request(self.body)))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertTrue(self.hass.runtime.mutation_lock.locked())
            second = asyncio.create_task(self.view.post(Request(self.body)))
            await asyncio.sleep(0)
            self.assertFalse(second.done())
        finally:
            release.set()
        response = await asyncio.wait_for(second, 5)
        self.assertEqual(response.status, 409)
        self.assertEqual(len(self.view.store.list_backups()), 1)
        self.assertEqual(self.hass.runtime.clears, 1)
