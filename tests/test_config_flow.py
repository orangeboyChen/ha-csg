"""Unit tests for CSG configuration-flow behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.csg.config_flow import CSGOptionsFlowHandler
from custom_components.csg.const import CONF_SETTINGS, CONF_UPDATE_INTERVAL


def run(coroutine):
    """Run an async unit under pytest without pytest-asyncio."""
    return asyncio.run(coroutine)


def test_options_flow_shows_translated_menu() -> None:
    """The options entry point routes through Home Assistant menu translations."""
    flow = CSGOptionsFlowHandler()
    flow.async_show_menu = lambda **kwargs: kwargs

    result = run(flow.async_step_init())

    assert result == {
        "step_id": "init",
        "menu_options": ["add_account", "settings"],
    }


def test_settings_update_reloads_entry() -> None:
    """Changing the interval reloads coordinators so the value takes effect."""
    entry = SimpleNamespace(
        entry_id="entry-id",
        data={CONF_SETTINGS: {CONF_UPDATE_INTERVAL: 14_400}, "updated_at": "0"},
    )

    class FakeConfigEntries:
        def __init__(self) -> None:
            self.updated_data = None
            self.reloaded_entry_id = None

        def async_update_entry(self, config_entry, *, data) -> None:
            self.updated_data = data

        async def async_reload(self, entry_id: str) -> None:
            self.reloaded_entry_id = entry_id

    config_entries = FakeConfigEntries()
    config_entries.async_get_known_entry = lambda entry_id: entry
    flow = CSGOptionsFlowHandler()
    flow.hass = SimpleNamespace(config_entries=config_entries)
    flow.handler = "entry-id"
    flow.async_create_entry = lambda **kwargs: kwargs

    result = run(flow.async_step_settings({CONF_UPDATE_INTERVAL: 3_600}))

    assert config_entries.updated_data[CONF_SETTINGS][CONF_UPDATE_INTERVAL] == 3_600
    assert config_entries.reloaded_entry_id == "entry-id"
    assert result == {"title": "", "data": {}}
