"""Parmair MAC button platform.

Two one-shot actions the unit exposes as writable registers rather than
readable state: acknowledging active alarms, and marking the filter changed
(which stamps today's date before flipping ``filter_state`` so the unit's own
next-change computation sees fresh dates first).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import ParmairConfigEntry, ParmairCoordinator
from .entity import ParmairEntity


@dataclass(frozen=True, kw_only=True)
class ParmairButtonDescription(ButtonEntityDescription):
    """Describes one button; ``press_fn`` performs the write(s)."""

    press_fn: Callable[[ParmairCoordinator], Awaitable[None]]


async def _acknowledge_alarms(coordinator: ParmairCoordinator) -> None:
    await coordinator.async_write("acknowledge_alarms", 1)


async def _filter_changed(coordinator: ParmairCoordinator) -> None:
    today = dt_util.now().date()
    await coordinator.async_write_sequence(
        [
            ("filter_day", today.day),
            ("filter_month", today.month),
            ("filter_year", today.year),
            ("filter_state", 1),
        ]
    )


DESCRIPTIONS: tuple[ParmairButtonDescription, ...] = (
    ParmairButtonDescription(key="acknowledge_alarms", press_fn=_acknowledge_alarms),
    ParmairButtonDescription(
        key="filter_changed", entity_category=EntityCategory.CONFIG, press_fn=_filter_changed
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ParmairConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the acknowledge-alarms and filter-changed buttons."""
    coordinator = entry.runtime_data
    async_add_entities(ParmairButton(coordinator, description) for description in DESCRIPTIONS)


class ParmairButton(ParmairEntity, ButtonEntity):
    """One one-shot action button."""

    entity_description: ParmairButtonDescription
    _requires_register = False

    def __init__(
        self, coordinator: ParmairCoordinator, description: ParmairButtonDescription
    ) -> None:
        self.entity_description = description
        super().__init__(coordinator, description.key)

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.coordinator)
