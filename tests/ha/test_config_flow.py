"""Config-flow + options-flow tests: probe/detect, confirm, and re-detect."""

from __future__ import annotations

from unittest.mock import patch

from conftest import FakeModbusClient
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.parmair.const import (
    CONF_CAPABILITIES,
    CONF_CO2_OFFSET,
    CONF_REDETECT,
    CONF_REGISTER_MAP,
    CONF_SCAN_INTERVAL,
    CONF_SUMMER_AUTO_SOURCE,
    DOMAIN,
)

_HOST = "192.168.101.56"
_PORT = 502
_USER_INPUT = {
    CONF_HOST: _HOST,
    CONF_PORT: _PORT,
    CONF_NAME: "Parmair",
    CONF_SCAN_INTERVAL: 10,
}


async def _start_and_probe(
    hass: HomeAssistant, bank: dict[int, int]
) -> tuple[dict, FakeModbusClient]:
    """Init the user flow, submit the connection form, and return the confirm-step result."""
    fake_client = FakeModbusClient(bank)
    with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], _USER_INPUT)
    return result, fake_client


# ── User flow ───────────────────────────────────────────────────────────────


async def test_happy_path_creates_entry(hass: HomeAssistant, rexo120_bank: dict[int, int]) -> None:
    result, fake_client = await _start_and_probe(hass, rexo120_bank)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    placeholders = result["description_placeholders"]
    assert placeholders["model"] == "MAC 120"
    assert placeholders["sw_version"] == "1.87"
    assert "CO₂" in placeholders["features"]

    with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Parmair"
    assert result["data"][CONF_HOST] == _HOST
    assert result["data"][CONF_PORT] == _PORT
    assert result["data"][CONF_REGISTER_MAP] == "v1_87"
    assert result["data"][CONF_CAPABILITIES]["has_co2"] is True
    assert result["options"][CONF_SCAN_INTERVAL] == 10
    assert result["options"][CONF_CO2_OFFSET] == 0


async def test_cannot_connect_then_retry_succeeds(
    hass: HomeAssistant, rexo120_bank: dict[int, int]
) -> None:
    failing_client = FakeModbusClient(rexo120_bank)
    failing_client.fail_connect = True
    with patch("custom_components.parmair.modbus.create_client", return_value=failing_client):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], _USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "cannot_connect"

    working_client = FakeModbusClient(rexo120_bank)
    with patch("custom_components.parmair.modbus.create_client", return_value=working_client):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], _USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"


async def test_not_parmair_shows_error(hass: HomeAssistant, rexo120_bank: dict[int, int]) -> None:
    bad_bank = dict(rexo120_bank)
    bad_bank[1244] = 0  # machine_type out of the 1..600 sanity range

    result, _fake_client = await _start_and_probe(hass, bad_bank)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "not_parmair"


async def test_read_failure_after_connect_is_cannot_connect(
    hass: HomeAssistant, rexo120_bank: dict[int, int]
) -> None:
    fake_client = FakeModbusClient(rexo120_bank)
    fake_client.fail_reads_at = {1016}  # first static block

    with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], _USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "cannot_connect"
    assert fake_client.close_calls >= 1  # client.close() must always run


async def test_duplicate_host_port_aborts(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, rexo120_bank: dict[int, int]
) -> None:
    existing = MockConfigEntry(
        domain=DOMAIN,
        data=dict(mock_config_entry.data),
        options=dict(mock_config_entry.options),
        title=mock_config_entry.title,
        unique_id=f"{_HOST}:{_PORT}",
    )
    existing.add_to_hass(hass)

    fake_client = FakeModbusClient(rexo120_bank)
    with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], _USER_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ── Options flow ──────────────────────────────────────────────────────────────


async def test_options_flow_updates_scan_interval_and_reloads(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)

    with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SCAN_INTERVAL: 30,
                CONF_CO2_OFFSET: -480,
                CONF_REDETECT: False,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL] == 30
    assert entry.options[CONF_CO2_OFFSET] == -480
    assert entry.runtime_data.update_interval.total_seconds() == 30


async def test_options_flow_redetect_updates_capabilities(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _original_client = await async_setup_integration(rexo120_bank)

    new_bank = dict(rexo120_bank)
    new_bank[1242] = 2  # m10_sensor_type -> humidity
    new_bank[1030] = 500  # wet_room_humidity now reads present
    redetect_client = FakeModbusClient(new_bank)

    with patch("custom_components.parmair.modbus.create_client", return_value=redetect_client):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SCAN_INTERVAL: 10,
                CONF_CO2_OFFSET: -480,
                CONF_REDETECT: True,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_CAPABILITIES]["has_wet_room_humidity"] is True
    assert entry.data[CONF_CAPABILITIES]["has_co2"] is True  # still probed present via 1031


async def test_options_flow_redetect_cannot_connect_shows_error(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _original_client = await async_setup_integration(rexo120_bank)

    broken_client = FakeModbusClient(rexo120_bank)
    broken_client.fail_connect = True

    with patch("custom_components.parmair.modbus.create_client", return_value=broken_client):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SCAN_INTERVAL: 10,
                CONF_CO2_OFFSET: -480,
                CONF_REDETECT: True,
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["errors"]["base"] == "cannot_connect"


async def test_options_flow_sets_and_clears_summer_auto_source(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)

    with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SCAN_INTERVAL: 10,
                CONF_CO2_OFFSET: 0,
                CONF_SUMMER_AUTO_SOURCE: "sensor.outdoor_temperature",
                CONF_REDETECT: False,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SUMMER_AUTO_SOURCE] == "sensor.outdoor_temperature"

    with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SCAN_INTERVAL: 10,
                CONF_CO2_OFFSET: 0,
                CONF_REDETECT: False,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_SUMMER_AUTO_SOURCE not in entry.options
