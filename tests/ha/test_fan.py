"""Fan platform: the primary on/off + speed + preset control entity."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.parmair.const import DOMAIN


def _entity_id(hass: HomeAssistant, entry_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("fan", DOMAIN, f"{entry_id}_fan")
    assert entity_id is not None
    return entity_id


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

    await hass.services.async_call(
        "fan", "set_percentage", {"entity_id": entity_id, "percentage": 80}, blocking=True
    )
    assert (1187, 5) in fake_client.writes

    await hass.services.async_call(
        "fan", "set_percentage", {"entity_id": entity_id, "percentage": 0}, blocking=True
    )
    assert (1187, 1) in fake_client.writes


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

    await hass.services.async_call("fan", "turn_off", {"entity_id": entity_id}, blocking=True)
    assert (1208, 1) in fake_client.writes

    await hass.services.async_call("fan", "turn_on", {"entity_id": entity_id}, blocking=True)
    assert (1208, 2) in fake_client.writes


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
