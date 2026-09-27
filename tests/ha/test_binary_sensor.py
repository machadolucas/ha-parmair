"""Binary_sensor platform: on/off states off the real Rexo 120 bank, plus gating."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from conftest import FakeModbusClient
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.parmair.binary_sensor import ParmairCookingDetectedBinarySensor
from custom_components.parmair.const import (
    CONF_CAPABILITIES,
    CONF_COOKING_SENSORS,
    CONF_REGISTER_MAP,
    CONF_SCAN_INTERVAL,
    DOMAIN,
    SIGNAL_COOKING_UPDATE,
)


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str | None:
    registry = er.async_get(hass)
    return registry.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_{key}")


def _state(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str | None:
    entity_id = _entity_id(hass, entry, key)
    assert entity_id is not None, f"no binary_sensor entity registered for key {key!r}"
    state = hass.states.get(entity_id)
    assert state is not None, f"entity {entity_id} has no state"
    return state.state


async def test_real_bank_initial_states(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)

    assert _state(hass, entry, "defrosting") == "off"
    assert _state(hass, entry, "home") == "on"
    assert _state(hass, entry, "boost_active") == "off"
    assert _state(hass, entry, "fireplace_active") == "off"
    assert _state(hass, entry, "boost_switch_input") == "off"
    # filter_state == 1 (not due) -> filter_change_required is off.
    assert _state(hass, entry, "filter_change_required") == "off"
    # summary_alarm == 0 -> alarm is off.
    assert _state(hass, entry, "alarm") == "off"


async def test_boost_active_turns_on_after_refresh(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    assert _state(hass, entry, "boost_active") == "off"

    fake_client.bank[1201] = 1  # boost_active -> on
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, entry, "boost_active") == "on"

    fake_client.bank[1201] = 0
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, entry, "boost_active") == "off"


async def test_filter_change_required_tracks_filter_state_inverted(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    assert _state(hass, entry, "filter_change_required") == "off"

    fake_client.bank[1205] = 0  # filter_state -> due
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, entry, "filter_change_required") == "on"

    fake_client.bank[1205] = 1  # filter_state -> not due again
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, entry, "filter_change_required") == "off"


async def test_alarm_tracks_summary_alarm(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    assert _state(hass, entry, "alarm") == "off"

    fake_client.bank[1005] = 1  # summary_alarm -> on
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _state(hass, entry, "alarm") == "on"


async def test_diagnostic_disabled_by_default_entities_exist_but_disabled(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    registry = er.async_get(hass)

    for key in ("home_switch_input", "fireplace_switch_input", "io_initialized"):
        entity_id = _entity_id(hass, entry, key)
        assert entity_id is not None, f"{key} should be registered even though disabled"
        entry_reg = registry.entities[entity_id]
        assert entry_reg.disabled_by == er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None  # disabled entities carry no state


async def test_cooking_detected_skips_poll_rewrites_and_volatile_attributes(
    hass: HomeAssistant,
    rexo120_bank: dict[int, int],
    rexo120_capabilities_dict: dict[str, Any],
    freezer,
) -> None:
    """``cooking_detected`` carries a large, fast-changing attribute payload; it must
    neither be rewritten on every Modbus poll nor have that payload recorded."""
    assert {"score", "sensors"} <= ParmairCookingDetectedBinarySensor._unrecorded_attributes
    assert "threshold" not in ParmairCookingDetectedBinarySensor._unrecorded_attributes

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.168.101.56",
            CONF_PORT: 502,
            CONF_REGISTER_MAP: "v1_87",
            CONF_CAPABILITIES: rexo120_capabilities_dict,
        },
        options={CONF_SCAN_INTERVAL: 10, CONF_COOKING_SENSORS: ["sensor.test_voc"]},
        title="Parmair",
    )
    fake_client = FakeModbusClient(rexo120_bank)
    with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data
    entity_id = _entity_id(hass, entry, "cooking_detected")
    before = hass.states.get(entity_id)
    # HA mutates ``last_reported`` on the live State object, so snapshot it.
    reported = before.last_reported
    assert before.state == "off"

    freezer.tick(timedelta(seconds=30))
    fake_client.bank[1201] = 1  # a poll that changes other entities' data
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).last_reported == reported

    fake_client.fail_all = True
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"  # not "unavailable"

    freezer.tick(timedelta(seconds=30))
    async_dispatcher_send(hass, SIGNAL_COOKING_UPDATE.format(entry.entry_id))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).last_reported > reported
