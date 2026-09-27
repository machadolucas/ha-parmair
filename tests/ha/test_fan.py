"""Fan platform: the primary on/off + speed + preset control entity."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.parmair.const import DOMAIN


def _entity_id(hass: HomeAssistant, entry_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("fan", DOMAIN, f"{entry_id}_fan")
    assert entity_id is not None
    return entity_id


async def _fire_verify(hass: HomeAssistant) -> None:
    """Run the coordinator's delayed verify-read (VERIFY_DELAY after a write)."""
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()


async def _call(hass: HomeAssistant, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call("fan", service, {"entity_id": entity_id, **data}, blocking=True)


async def test_fan_reports_on_percentage_and_preset(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    state = hass.states.get(_entity_id(hass, entry.entry_id))

    assert state.state == "on"  # power_state 1208:3
    assert state.attributes["percentage"] == 60  # fan_speed_state 1186:3
    assert state.attributes["preset_mode"] == "home"  # control_state 1185:2


async def test_set_percentage_writes_manual_speed(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id)

    await _call(hass, "set_percentage", entity_id, percentage=80)
    assert (1187, 5) in fake_client.writes

    # After the verify-read the unit's own report wins: manual speed 4 flips
    # control_state to 9, which no preset maps to.
    await _fire_verify(hass)
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["percentage"] == 80
    assert state.attributes["preset_mode"] is None


async def test_set_percentage_zero_turns_off(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """HA's fan contract: 0 % is off — not speed_control 1 with the entity still on."""
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id)
    fake_client.writes.clear()

    await _call(hass, "set_percentage", entity_id, percentage=0)

    assert fake_client.writes == [(1208, 1)]
    await _fire_verify(hass)
    assert hass.states.get(entity_id).state == "off"


async def test_decrease_speed_from_lowest_turns_off(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    rexo120_bank[1186] = 1  # fan_speed_state 1 -> 20 %
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id)
    fake_client.writes.clear()

    await _call(hass, "decrease_speed", entity_id)

    assert fake_client.writes == [(1208, 1)]
    assert (1187, 1) not in fake_client.writes


async def test_preset_after_manual_speed_returns_to_preset(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id)
    await _call(hass, "set_percentage", entity_id, percentage=100)
    await _fire_verify(hass)
    assert hass.states.get(entity_id).attributes["preset_mode"] is None

    await _call(hass, "set_preset_mode", entity_id, preset_mode="home")
    await _fire_verify(hass)

    state = hass.states.get(entity_id)
    assert state.attributes["preset_mode"] == "home"
    assert state.attributes["percentage"] == 60  # home_speed raw 2 -> speed 3


async def test_boost_preset_reflected_after_verify(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id)

    await _call(hass, "set_preset_mode", entity_id, preset_mode="boost")
    await _fire_verify(hass)

    assert hass.states.get(entity_id).attributes["preset_mode"] == "boost"
    boost_active = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_boost_active"
    )
    assert hass.states.get(boost_active).state == "on"


async def test_write_failure_raises_translated_error(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id)
    fake_client.fail_writes = True

    with pytest.raises(HomeAssistantError) as exc_info:
        await _call(hass, "set_preset_mode", entity_id, preset_mode="away")

    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "write_failed"
    assert fake_client.writes == []


async def test_set_preset_mode_boost_releases_manual_then_writes_control_state(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id)
    fake_client.writes.clear()

    await hass.services.async_call(
        "fan", "set_preset_mode", {"entity_id": entity_id, "preset_mode": "boost"}, blocking=True
    )

    assert fake_client.writes == [(1187, 0), (1185, 3)]


async def test_turn_off_and_on_write_power_state(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id)

    await _call(hass, "turn_off", entity_id)
    assert (1208, 1) in fake_client.writes
    await _fire_verify(hass)
    assert hass.states.get(entity_id).state == "off"

    await _call(hass, "turn_on", entity_id)
    assert (1208, 2) in fake_client.writes
    await _fire_verify(hass)
    assert hass.states.get(entity_id).state == "on"


async def test_preset_mode_none_for_manual_control_state(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    rexo120_bank[1185] = 9  # CONTROL_STATE_MANUAL
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    state = hass.states.get(_entity_id(hass, entry.entry_id))

    assert state.attributes["preset_mode"] is None


async def test_unavailable_when_power_state_missing(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id)
    assert hass.states.get(entity_id).state != "unavailable"

    fake_client.fail_all = True
    coordinator = entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "unavailable"
