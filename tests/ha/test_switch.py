"""Switch platform: direct-register toggles, boost/fireplace mode-switches, summer-auto."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.parmair.const import DOMAIN


def _entity_id(hass: HomeAssistant, entry_id: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{entry_id}_{key}")
    assert entity_id is not None
    return entity_id


async def _call(hass: HomeAssistant, service: str, entity_id: str) -> None:
    await hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)


async def _fire_verify(hass: HomeAssistant) -> None:
    """Run the coordinator's delayed verify-read (VERIFY_DELAY after a write)."""
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()


async def test_boost_off_then_turn_on_writes_preset_sequence(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "boost")
    fake_client.writes.clear()

    assert hass.states.get(entity_id).state == "off"  # boost_active 1201:0

    await _call(hass, "turn_on", entity_id)
    # Presets release manual speed first (CLAUDE.md write semantics).
    assert fake_client.writes == [(1187, 0), (1185, 3)]

    await _fire_verify(hass)
    assert hass.states.get(entity_id).state == "on"

    await _call(hass, "turn_off", entity_id)
    await _fire_verify(hass)
    assert fake_client.writes[-1] == (1185, 2)  # home_state 1200:1 -> home
    assert hass.states.get(entity_id).state == "off"


async def test_boost_turn_on_from_manual_speed_takes_effect(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    rexo120_bank[1185] = 9  # control_state: manual
    rexo120_bank[1187] = 4  # speed_control: manual speed 3
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "boost")

    await _call(hass, "turn_on", entity_id)
    await _fire_verify(hass)

    assert hass.states.get(entity_id).state == "on"
    assert entry.runtime_data.data["speed_control"] == 0


async def test_boost_turn_off_restores_home_when_home_state_1(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "boost")

    await _call(hass, "turn_off", entity_id)  # home_state 1200:1

    assert (1185, 2) in fake_client.writes


async def test_boost_turn_off_restores_away_when_home_state_0(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    rexo120_bank[1200] = 0
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "boost")

    await _call(hass, "turn_off", entity_id)

    assert (1185, 1) in fake_client.writes


async def test_fireplace_turn_on_writes_preset_sequence(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "fireplace")
    fake_client.writes.clear()

    await _call(hass, "turn_on", entity_id)

    assert fake_client.writes == [(1187, 0), (1185, 4)]
    await _fire_verify(hass)
    assert hass.states.get(entity_id).state == "on"
    assert hass.states.get(_entity_id(hass, entry.entry_id, "boost")).state == "off"


async def test_write_failure_raises_translated_error(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    fake_client.fail_writes = True

    for key in ("boost", "summer_mode"):
        with pytest.raises(HomeAssistantError) as exc_info:
            await _call(hass, "toggle", _entity_id(hass, entry.entry_id, key))
        assert exc_info.value.translation_key == "write_failed"
    assert fake_client.writes == []


async def test_summer_mode_is_on_and_turn_off_writes_register(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "summer_mode")

    assert hass.states.get(entity_id).state == "on"  # summer_mode 1079:1

    await _call(hass, "turn_off", entity_id)
    assert (1079, 0) in fake_client.writes


async def test_hru_temperature_control_off_then_turn_on(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "hru_temperature_control")

    assert hass.states.get(entity_id).state == "off"  # 1116:0

    await _call(hass, "turn_on", entity_id)
    assert (1116, 1) in fake_client.writes


async def test_post_heating_exists_and_is_on_for_electric_heater(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "post_heating")

    assert hass.states.get(entity_id).state == "on"  # 1109:1


async def test_week_clock_and_alarm_sound_disabled_by_default(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    registry = er.async_get(hass)

    for key in ("week_clock", "alarm_sound"):
        entry_reg = registry.entities[_entity_id(hass, entry.entry_id, key)]
        assert entry_reg.disabled is True


async def test_summer_auto_switch_off_by_default_turn_on_sets_coordinator_flag(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    entity_id = _entity_id(hass, entry.entry_id, "summer_auto")

    assert hass.states.get(entity_id).state == "off"
    assert coordinator.summer_auto_enabled is False

    await _call(hass, "turn_on", entity_id)

    assert coordinator.summer_auto_enabled is True
    assert hass.states.get(entity_id).state == "on"
