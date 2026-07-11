"""``__init__.py`` wiring: setup/unload, device registration, ConfigEntryNotReady."""

from __future__ import annotations

from unittest.mock import patch

from conftest import FakeModbusClient
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.parmair.const import DOMAIN
from custom_components.parmair.coordinator import ParmairCoordinator


async def test_setup_creates_coordinator_and_registers_device(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, ParmairCoordinator)
    assert entry.runtime_data.client is fake_client

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device is not None
    assert device.manufacturer == "Parmair"
    assert device.model == "MAC 120"
    assert device.sw_version == "1.87"
    assert device.hw_version == "2.72"


async def test_unload_closes_the_modbus_client(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert fake_client.close_calls == 1


async def test_setup_with_dead_connection_retries(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    rexo120_bank: dict[int, int],
) -> None:
    """``ParmairConnectionError`` from ``connect()`` must become ``ConfigEntryNotReady``."""
    fake_client = FakeModbusClient(rexo120_bank)
    fake_client.fail_connect = True

    with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_frontend_registration_is_a_noop_without_the_card_file(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """The M7 card doesn't exist yet; setup must still succeed (best-effort log only)."""
    entry, _fake_client = await async_setup_integration(rexo120_bank)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.data.get(f"{DOMAIN}_card") is None
