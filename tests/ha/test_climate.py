"""Climate platform: supply/extract setpoints paired with their live readings."""

from __future__ import annotations

from homeassistant.components.climate import DATA_COMPONENT
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.parmair.const import DOMAIN


def _entity_id(hass: HomeAssistant, entry_id: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("climate", DOMAIN, f"{entry_id}_{key}")
    assert entity_id is not None
    return entity_id


async def test_supply_climate_current_and_target(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    state = hass.states.get(_entity_id(hass, entry.entry_id, "supply_temperature_target"))

    assert state.attributes["current_temperature"] == 24.9
    assert state.attributes["temperature"] == 18.0
    assert state.state == "auto"


async def test_extract_climate_current_and_target(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    state = hass.states.get(_entity_id(hass, entry.entry_id, "extract_temperature_target"))

    assert state.attributes["current_temperature"] == 23.2
    assert state.attributes["temperature"] == 21.0


async def test_set_temperature_writes_register(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "supply_temperature_target")

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": entity_id, "temperature": 19.5},
        blocking=True,
    )

    assert (1065, 195) in fake_client.writes


async def test_set_temperature_clamps_to_max(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """The register's own encode() clamp is defense-in-depth: HA's climate service
    already rejects an out-of-range value against the entity's min/max before it
    would ever reach ``async_set_temperature``, so this calls the entity method
    directly to exercise the coordinator/encode() clamp itself.
    """
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "supply_temperature_target")
    entity = hass.data[DATA_COMPONENT].get_entity(entity_id)

    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 30})

    assert (1065, 250) in fake_client.writes


async def test_hvac_action_from_temperature_mode(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "supply_temperature_target")

    state = hass.states.get(entity_id)
    assert state.attributes["hvac_action"] == "fan"  # temperature_mode 1188:1

    fake_client.bank[1188] = 2
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.attributes["hvac_action"] == "heating"
