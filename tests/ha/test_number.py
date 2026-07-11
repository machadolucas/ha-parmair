"""Number platform: register-backed config numbers + M6 summer-auto local numbers."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.parmair.const import DOMAIN


def _entity_id(hass: HomeAssistant, entry_id: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("number", DOMAIN, f"{entry_id}_{key}")
    assert entity_id is not None
    return entity_id


async def _set_value(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )


async def test_home_speed_shows_offset_display_value(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    state = hass.states.get(_entity_id(hass, entry.entry_id, "home_speed"))

    assert float(state.state) == 3  # raw 1104:2 + 1


async def test_home_speed_set_writes_raw_minus_offset(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "home_speed")

    await _set_value(hass, entity_id, 4)

    assert (1104, 3) in fake_client.writes


async def test_away_speed_shows_offset_display_value(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    state = hass.states.get(_entity_id(hass, entry.entry_id, "away_speed"))

    assert float(state.state) == 2  # raw 1105:1 + 1


async def test_defrost_min_efficiency_set(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "defrost_min_efficiency")

    assert float(hass.states.get(entity_id).state) == 60

    await _set_value(hass, entity_id, 65)
    assert (1097, 65) in fake_client.writes


async def test_summer_mode_outdoor_limit_set(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "summer_mode_outdoor_limit")

    assert float(hass.states.get(entity_id).state) == 8.0

    await _set_value(hass, entity_id, 10.5)
    assert (1078, 105) in fake_client.writes


async def test_co2_boost_start_exists_for_co2_capable_unit(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    state = hass.states.get(_entity_id(hass, entry.entry_id, "co2_boost_start"))

    assert float(state.state) == 999


async def test_summer_auto_numbers_defaults_and_set(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data

    assert coordinator.summer_auto_params.on_temp_c == 21.0
    assert coordinator.summer_auto_params.off_temp_c == 15.0
    assert coordinator.summer_auto_params.on_dwell_min == 60
    assert coordinator.summer_auto_params.off_dwell_min == 120

    on_temp_entity = _entity_id(hass, entry.entry_id, "summer_auto_on_temperature")
    await _set_value(hass, on_temp_entity, 24)

    assert coordinator.summer_auto_params.on_temp_c == 24.0
