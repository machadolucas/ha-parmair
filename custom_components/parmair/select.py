"""Parmair MAC select platform.

Enum-valued settings registers (boost/fireplace duration, boost speed, filter
interval) where the raw register value doesn't match the user-facing option
directly (see ``const.BOOST_DURATION_MINUTES`` and friends) — the select's
options are the mapped values as strings, and ``current_option``/
``async_select_option`` translate through the map in each direction.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    BOOST_DURATION_MINUTES,
    BOOST_SPEED_VALUES,
    FILTER_INTERVAL_MONTHS,
    FIREPLACE_DURATION_MINUTES,
)
from .coordinator import ParmairConfigEntry, ParmairCoordinator
from .entity import ParmairEntity


@dataclass(frozen=True, kw_only=True)
class ParmairSelectDescription(SelectEntityDescription):
    """Describes one enum-valued register, mapped raw <-> displayed value."""

    entity_category: EntityCategory | None = EntityCategory.CONFIG
    raw_to_value: dict[int, int]


def _options(raw_to_value: dict[int, int]) -> list[str]:
    return [str(value) for value in raw_to_value.values()]


DESCRIPTIONS: tuple[ParmairSelectDescription, ...] = (
    ParmairSelectDescription(
        key="boost_duration",
        options=_options(BOOST_DURATION_MINUTES),
        raw_to_value=BOOST_DURATION_MINUTES,
    ),
    ParmairSelectDescription(
        key="fireplace_duration",
        options=_options(FIREPLACE_DURATION_MINUTES),
        raw_to_value=FIREPLACE_DURATION_MINUTES,
    ),
    ParmairSelectDescription(
        key="boost_speed",
        options=_options(BOOST_SPEED_VALUES),
        raw_to_value=BOOST_SPEED_VALUES,
    ),
    ParmairSelectDescription(
        key="filter_interval",
        options=_options(FILTER_INTERVAL_MONTHS),
        raw_to_value=FILTER_INTERVAL_MONTHS,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ParmairConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the enum-valued select entities."""
    coordinator = entry.runtime_data
    async_add_entities(ParmairSelect(coordinator, description) for description in DESCRIPTIONS)


class ParmairSelect(ParmairEntity, SelectEntity):
    """One enum-valued register, presented as its mapped display value."""

    entity_description: ParmairSelectDescription

    def __init__(
        self, coordinator: ParmairCoordinator, description: ParmairSelectDescription
    ) -> None:
        self.entity_description = description
        super().__init__(coordinator, description.key)
        self._value_to_raw = {value: raw for raw, value in description.raw_to_value.items()}

    @property
    def current_option(self) -> str | None:
        raw = self.register_value
        if raw is None:
            return None
        value = self.entity_description.raw_to_value.get(int(raw))
        return None if value is None else str(value)

    async def async_select_option(self, option: str) -> None:
        raw = self._value_to_raw[int(option)]
        await self.coordinator.async_write(self._key, raw)
