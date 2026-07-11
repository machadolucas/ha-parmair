"""Sensor platform: values off the real Rexo 120 bank, gating, and entity metadata."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from conftest import FakeModbusClient
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.parmair.const import (
    CONF_CAPABILITIES,
    CONF_REGISTER_MAP,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str | None:
    registry = er.async_get(hass)
    return registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")


def _state(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str | None:
    entity_id = _entity_id(hass, entry, key)
    assert entity_id is not None, f"no sensor entity registered for key {key!r}"
    state = hass.states.get(entity_id)
    assert state is not None, f"entity {entity_id} has no state"
    return state.state


async def test_real_bank_values(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)

    assert _state(hass, entry, "supply_temperature") == "24.9"
    assert _state(hass, entry, "extract_temperature") == "23.2"
    assert _state(hass, entry, "fresh_air_temperature") == "25.4"
    assert _state(hass, entry, "waste_temperature") == "23.9"
    assert _state(hass, entry, "supply_temperature_after_hru") == "23.5"
    assert _state(hass, entry, "hru_humidity") == "54"
    assert _state(hass, entry, "humidity_24h_average") == "54.3"
    assert _state(hass, entry, "heat_recovery_efficiency") == "86.4"
    assert _state(hass, entry, "supply_fan_output") == "28.8"
    assert _state(hass, entry, "extract_fan_output") == "40.0"
    assert _state(hass, entry, "fan_speed_state") == "3"
    # co2 raw 969 + the -480 offset baked into mock_config_entry's options.
    assert _state(hass, entry, "co2") == "489"
    assert _state(hass, entry, "control_state") == "home"
    assert _state(hass, entry, "temperature_mode") == "ventilation"
    assert _state(hass, entry, "alarm_state") == "ok"
    # raw -1 (inactive) clamped to 0.
    assert _state(hass, entry, "boost_time_remaining") == "0"
    assert _state(hass, entry, "filter_last_change") == "2026-05-16"
    assert _state(hass, entry, "filter_next_change") == "2026-11-16"
    assert _state(hass, entry, "active_alarm_count") == "0"


async def test_absent_sensors_are_not_created(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)

    for key in (
        "humidity",  # no main-duct humidity sensor on this unit
        "wet_room_humidity",  # no wet-room humidity sensor on this unit
        "post_heater_return_water",  # electric heater, not water
        "alarm_return_water_low",  # water-heater capability gated
        "fault_return_water_sensor",  # water-heater capability gated
        "external_boost_signal",  # m12_usage != BOOST_0_10V
        "supply_temp_deflection",  # m12_usage != SUPPLY_DEFLECTION
    ):
        assert _entity_id(hass, entry, key) is None, f"{key} should not have been created"


async def test_diagnostic_disabled_by_default_entities_exist_but_disabled(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    registry = er.async_get(hass)

    for key in (
        "preheater_output",
        "power_limit",
        "speed_control_detail",
        "operating_point",
        "auto_boost_timer",
        "post_run_timer",
        "defrost_cycle",
        "defrost_start_limit_computed",
        "external_control_signal",
        "fault_fresh_air_sensor",
        "fault_supply_sensor",
        "fault_supply_after_hru_sensor",
        "fault_extract_sensor",
        "fault_waste_sensor",
        "fault_hru_humidity_sensor",
        "fault_supply_fan",
        "fault_extract_fan",
        "alarm_supply_temp_high",
        "alarm_extract_temp_high",
        "alarm_supply_temp_low",
        "alarm_filter",
    ):
        entity_id = _entity_id(hass, entry, key)
        assert entity_id is not None, f"{key} should be registered even though disabled"
        entry_reg = registry.entities[entity_id]
        assert entry_reg.disabled_by == er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None  # disabled entities carry no state


async def test_enabled_diagnostic_entities_have_a_state(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)

    assert _state(hass, entry, "hru_output") == "100.0"
    assert _state(hass, entry, "post_heater_output") == "0.0"


async def test_co2_offset_defaults_to_zero_when_option_absent(
    hass: HomeAssistant, rexo120_bank: dict[int, int], rexo120_capabilities_dict: dict[str, Any]
) -> None:
    """A config entry without ``CONF_CO2_OFFSET`` in options must not offset co2."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.168.101.56",
            CONF_PORT: 502,
            CONF_REGISTER_MAP: "v1_87",
            CONF_CAPABILITIES: rexo120_capabilities_dict,
        },
        options={CONF_SCAN_INTERVAL: 10},
        title="Parmair",
    )
    fake_client = FakeModbusClient(rexo120_bank)
    with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert _state(hass, entry, "co2") == "969"


async def test_bad_filter_date_reads_as_unknown(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """A garbage filter_month (13) must not raise; the composed date is unknown."""
    rexo120_bank[1087] = 13  # filter_month -> invalid
    entry, _fake_client = await async_setup_integration(rexo120_bank)

    assert _state(hass, entry, "filter_last_change") == "unknown"
    # The next-change date uses different registers and is unaffected.
    assert _state(hass, entry, "filter_next_change") == "2026-11-16"


async def test_unknown_control_state_raw_reads_as_unknown(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """A raw value outside the documented 0-9 enum range must not raise."""
    rexo120_bank[1185] = 15  # control_state -> not in CONTROL_STATE_NAMES
    entry, _fake_client = await async_setup_integration(rexo120_bank)

    assert _state(hass, entry, "control_state") == "unknown"
