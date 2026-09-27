"""``__init__.py`` wiring: setup/unload, device registration, ConfigEntryNotReady."""

from __future__ import annotations

from unittest.mock import patch

from conftest import FakeModbusClient
from homeassistant.components.modbus import async_get_unit
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.parmair.const import (
    CARD_FILENAME,
    CARD_URL_BASE,
    COOKING_STORAGE_KEY,
    COOKING_STORAGE_VERSION,
    DOMAIN,
    ISSUE_FILTER_CHANGE_DUE,
)
from custom_components.parmair.coordinator import ParmairCoordinator, issue_id
from custom_components.parmair.modbus import UNIT_ID_DEFAULT, tcp_params


async def test_setup_creates_coordinator_and_registers_device(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, ParmairCoordinator)
    assert entry.runtime_data.client is fake_client

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert device is not None
    assert device.manufacturer == "Parmair"
    assert device.model == "MAC 120"
    assert device.sw_version == "1.87"
    assert device.hw_version == "2.72"


async def test_unload_releases_the_modbus_client(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """Unload gives up our hold on the shared connection.

    ``close()`` is a release, not a disconnect — the ``modbus`` integration
    closes the connection itself once no entry holds a unit on it. That the
    real client leaves the link up is pinned in ``tests/test_modbus.py``.
    """
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


async def test_borrow_conflict_is_a_terminal_setup_error(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    rexo120_bank: dict[int, int],
) -> None:
    """A shared endpoint already held with different link settings can't be retried.

    Nests its own patch over the autouse ``mock_modbus_borrow`` fixture, so this
    exercises the real ``async_get_unit`` call site.
    """
    with (
        patch(
            "custom_components.parmair.async_get_unit",
            side_effect=HomeAssistantError("already in use with different link settings"),
        ),
        patch(
            "custom_components.parmair.modbus.create_client",
            return_value=FakeModbusClient(rexo120_bank),
        ),
    ):
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_the_unit_is_borrowed_for_the_configured_endpoint(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """The borrow carries our link settings and the Multi24's unit id 0."""
    with patch("custom_components.parmair.async_get_unit", wraps=async_get_unit) as borrow:
        entry, _fake_client = await async_setup_integration(rexo120_bank)

    assert entry.state is ConfigEntryState.LOADED
    _hass, borrowed_entry, params, unit_id = borrow.call_args.args
    assert borrowed_entry is entry
    assert params == tcp_params(entry.data[CONF_HOST], entry.data[CONF_PORT])
    assert unit_id == UNIT_ID_DEFAULT


async def test_failed_setup_still_releases_the_client(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    rexo120_bank: dict[int, int],
) -> None:
    """A ConfigEntryNotReady must not leave the lost-link callback subscribed.

    ``client.close()`` used to run only from async_unload_entry, which a failed
    setup never reaches; every retry then leaked one more subscription.
    """
    fake_client = FakeModbusClient(rexo120_bank)
    fake_client.fail_connect = True

    with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
        mock_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert fake_client.close_calls == 1


async def test_unload_shuts_the_coordinator_down_before_releasing_the_client(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """LIFO on_unload: coordinator shutdown (timers off) starts before the release."""
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    order: list[str] = []
    real_shutdown = coordinator.async_shutdown
    real_close = fake_client.close

    async def shutdown() -> None:
        order.append("shutdown")
        await real_shutdown()

    async def close() -> None:
        order.append("close")
        await real_close()

    # The on_unload stack holds the bound methods, so swap them in place.
    unload = entry._on_unload
    unload[unload.index(coordinator.async_shutdown)] = shutdown
    unload[unload.index(fake_client.close)] = close

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert order == ["shutdown", "close"]
    assert fake_client.close_calls == 1


async def test_remove_entry_cleans_up_issues_and_cooking_store(
    hass: HomeAssistant,
    async_setup_integration,
    rexo120_bank: dict[int, int],
    hass_storage: dict,
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    fake_client.bank[1205] = 0  # filter due -> issue raised
    await entry.runtime_data.async_refresh()
    filter_issue = issue_id(ISSUE_FILTER_CHANGE_DUE, entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, filter_issue) is not None
    store_key = COOKING_STORAGE_KEY.format(entry.entry_id)
    await Store(hass, COOKING_STORAGE_VERSION, store_key).async_save({"sensors": {}})
    assert store_key in hass_storage

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, filter_issue) is None
    assert store_key not in hass_storage


async def test_remove_last_entry_removes_the_lovelace_resource(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    rexo120_bank: dict[int, int],
) -> None:
    """The card is shared by all entries: it goes with the last one, not before."""
    from homeassistant.components.lovelace.const import LOVELACE_DATA
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "lovelace", {"lovelace": {"mode": "storage"}})
    await hass.async_block_till_done()

    second = MockConfigEntry(
        domain=DOMAIN,
        data={**mock_config_entry.data, CONF_HOST: "192.168.101.57"},
        options=dict(mock_config_entry.options),
        title="Parmair 2",
    )
    with patch(
        "custom_components.parmair.modbus.create_client",
        side_effect=[FakeModbusClient(rexo120_bank), FakeModbusClient(rexo120_bank)],
    ):
        for entry in (mock_config_entry, second):
            entry.add_to_hass(hass)
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

    card_base = f"{CARD_URL_BASE}/{CARD_FILENAME}"

    def card_resources() -> list:
        return [
            item
            for item in hass.data[LOVELACE_DATA].resources.async_items()
            if str(item["url"]).startswith(card_base)
        ]

    assert len(card_resources()) == 1

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert len(card_resources()) == 1  # the other unit still uses it

    await hass.config_entries.async_remove(second.entry_id)
    await hass.async_block_till_done()
    assert card_resources() == []
    assert hass.data.get(f"{DOMAIN}_card") is None  # a re-add registers it again
