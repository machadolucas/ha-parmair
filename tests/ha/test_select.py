"""Select platform: enum-valued registers mapped raw <-> displayed value."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.parmair.const import DOMAIN


def _entity_id(hass: HomeAssistant, entry_id: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("select", DOMAIN, f"{entry_id}_{key}")
    assert entity_id is not None
    return entity_id


async def test_boost_duration_current_option(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    state = hass.states.get(_entity_id(hass, entry.entry_id, "boost_duration"))

    assert state.state == "180"  # raw 1106:4


async def test_boost_duration_select_option_writes_raw(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "boost_duration")

    await hass.services.async_call(
        "select", "select_option", {"entity_id": entity_id, "option": "60"}, blocking=True
    )

    assert (1106, 1) in fake_client.writes


async def test_fireplace_duration_current_option(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    state = hass.states.get(_entity_id(hass, entry.entry_id, "fireplace_duration"))

    assert state.state == "15"  # raw 1107:0


async def test_boost_speed_current_option(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    state = hass.states.get(_entity_id(hass, entry.entry_id, "boost_speed"))

    assert state.state == "5"  # raw 1117:4


async def test_filter_interval_disabled_by_default(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "filter_interval")

    entry_reg = er.async_get(hass).entities[entity_id]
    assert entry_reg.disabled is True


async def test_unknown_raw_value_reports_no_current_option(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    entity_id = _entity_id(hass, entry.entry_id, "boost_duration")

    coordinator.async_set_updated_data({**coordinator.data, "boost_duration": 99})
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state is None or hass.states.get(entity_id).state == "unknown"
