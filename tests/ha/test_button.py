"""Button platform: acknowledge-alarms and filter-changed one-shot actions."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.parmair.const import DOMAIN


def _entity_id(hass: HomeAssistant, entry_id: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{entry_id}_{key}")
    assert entity_id is not None
    return entity_id


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


async def test_acknowledge_alarms_press_writes_register(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "acknowledge_alarms")

    await _press(hass, entity_id)

    assert (1003, 1) in fake_client.writes


async def test_filter_changed_press_writes_dates_then_state(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    entity_id = _entity_id(hass, entry.entry_id, "filter_changed")
    today = dt_util.now().date()

    await _press(hass, entity_id)

    expected = [
        (1086, today.day),
        (1087, today.month),
        (1088, today.year),
        (1205, 1),
    ]
    for write in expected:
        assert write in fake_client.writes


async def test_write_failure_raises_translated_error(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    fake_client.fail_writes = True

    with pytest.raises(HomeAssistantError) as exc_info:
        await _press(hass, _entity_id(hass, entry.entry_id, "filter_changed"))

    assert exc_info.value.translation_key == "write_failed"
    assert fake_client.writes == []
