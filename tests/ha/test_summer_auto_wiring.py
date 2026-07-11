"""End-to-end M6 wiring: the summer_auto switch/numbers driving the coordinator's
dwell-gated auto-toggle (:mod:`custom_components.parmair.summer_auto`), including
the optional external temperature-source override.
"""

from __future__ import annotations

from datetime import timedelta
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
    CONF_SUMMER_AUTO_SOURCE,
    DOMAIN,
)


def _entity_id(hass: HomeAssistant, entry_id: str, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry_id}_{key}")
    assert entity_id is not None
    return entity_id


async def _enable_summer_auto(
    hass: HomeAssistant, entry_id: str, on_min: float, off_min: float
) -> None:
    """Turn the summer_auto switch on and set 20C-on / 10C-off thresholds with the given dwells."""
    switch_id = _entity_id(hass, entry_id, "switch", "summer_auto")
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch_id}, blocking=True)
    for key, value in (
        ("summer_auto_on_temperature", 20.0),
        ("summer_auto_off_temperature", 10.0),
        ("summer_auto_on_minutes", on_min),
        ("summer_auto_off_minutes", off_min),
    ):
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": _entity_id(hass, entry_id, "number", key), "value": value},
            blocking=True,
        )


async def test_summer_auto_switch_and_numbers_drive_the_coordinator_write(
    hass: HomeAssistant,
    async_setup_integration,
    rexo120_bank: dict[int, int],
    freezer,
) -> None:
    """Enabling the switch + numbers wires straight into the existing dwell logic.

    fresh_air_temperature is 25.4 (bank 1020:254), comfortably above the 20C
    on-threshold configured here; summer_mode starts off (forced to 0). Two
    refreshes 2 minutes apart satisfy the 1-minute on-dwell and fire the write
    — mirrors ``test_coordinator.test_summer_auto_writes_on_after_dwell`` but
    driven through the real M6 entities instead of poking the coordinator
    attributes directly.
    """
    rexo120_bank[1079] = 0  # summer_mode off
    entry, fake_client = await async_setup_integration(rexo120_bank)

    await _enable_summer_auto(hass, entry.entry_id, on_min=1, off_min=1)

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert (1079, 1) not in fake_client.writes  # dwell not satisfied yet

    freezer.tick(timedelta(minutes=2))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert (1079, 1) in fake_client.writes


async def test_disabling_summer_auto_stops_further_writes(
    hass: HomeAssistant,
    async_setup_integration,
    rexo120_bank: dict[int, int],
    freezer,
) -> None:
    rexo120_bank[1079] = 0
    entry, fake_client = await async_setup_integration(rexo120_bank)

    await _enable_summer_auto(hass, entry.entry_id, on_min=1, off_min=1)

    await entry.runtime_data.async_refresh()  # arms the on-dwell timer
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=2))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert (1079, 1) in fake_client.writes

    fake_client.writes.clear()
    switch_id = _entity_id(hass, entry.entry_id, "switch", "summer_auto")
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch_id}, blocking=True)

    freezer.tick(timedelta(minutes=5))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert (1079, 1) not in fake_client.writes


async def test_summer_auto_source_entity_overrides_fresh_air_temperature(
    hass: HomeAssistant,
    rexo120_bank: dict[int, int],
    rexo120_capabilities_dict: dict[str, Any],
    freezer,
) -> None:
    """With ``CONF_SUMMER_AUTO_SOURCE`` set, the driving temperature comes from
    the referenced entity's state instead of the unit's own fresh-air sensor
    (which reads 25.4 here — well above the threshold configured below, so a
    write would fire if it were still the source).
    """
    rexo120_bank[1079] = 0  # summer_mode off
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.168.101.56",
            CONF_PORT: 502,
            CONF_REGISTER_MAP: "v1_87",
            CONF_CAPABILITIES: rexo120_capabilities_dict,
        },
        options={
            CONF_SCAN_INTERVAL: 10,
            CONF_SUMMER_AUTO_SOURCE: "sensor.test_outdoor",
        },
        title="Parmair",
    )
    fake_client = FakeModbusClient(rexo120_bank)
    with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    hass.states.async_set("sensor.test_outdoor", "12.0")  # below the 20C on-threshold

    await _enable_summer_auto(hass, entry.entry_id, on_min=1, off_min=1)

    freezer.tick(timedelta(minutes=2))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    # Source reads 12.0 (< 20C on-threshold): no on-write, despite fresh_air
    # being 25.4 — proof the external source, not the unit sensor, is driving.
    assert (1079, 1) not in fake_client.writes

    hass.states.async_set("sensor.test_outdoor", "25.0")  # now above the on-threshold
    await entry.runtime_data.async_refresh()  # arms the on-dwell timer
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=2))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert (1079, 1) in fake_client.writes
