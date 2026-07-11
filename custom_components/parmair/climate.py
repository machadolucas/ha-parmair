"""Parmair MAC climate platform.

Two setpoint entities (supply/extract target temperature) modeled as
``ClimateEntity`` rather than ``number``: each pairs a target with a live
current-temperature reading and a derived ``hvac_action``, which is the
natural HA fit even though the unit has no selectable HVAC mode of its own
(hence the single, fixed ``HVACMode.AUTO``).
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityDescription,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_TENTHS, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import ParmairConfigEntry, ParmairCoordinator
from .entity import ParmairEntity

_TEMPERATURE_MODE_TO_HVAC_ACTION: dict[int, HVACAction] = {
    0: HVACAction.IDLE,
    1: HVACAction.FAN,
    2: HVACAction.HEATING,
    3: HVACAction.COOLING,
}


@dataclass(frozen=True, kw_only=True)
class ParmairClimateDescription(ClimateEntityDescription):
    """Describes one setpoint climate entity."""

    current_key: str
    min_temp: float
    max_temp: float


DESCRIPTIONS: tuple[ParmairClimateDescription, ...] = (
    ParmairClimateDescription(
        key="supply_temperature_target",
        current_key="supply_temperature",
        min_temp=15.0,
        max_temp=25.0,
    ),
    ParmairClimateDescription(
        key="extract_temperature_target",
        current_key="extract_temperature",
        min_temp=18.0,
        max_temp=26.0,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ParmairConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the supply/extract setpoint climate entities."""
    coordinator = entry.runtime_data
    async_add_entities(ParmairClimate(coordinator, description) for description in DESCRIPTIONS)


class ParmairClimate(ParmairEntity, ClimateEntity):
    """One temperature setpoint, paired with its live current reading."""

    entity_description: ParmairClimateDescription

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_precision = PRECISION_TENTHS
    _attr_hvac_modes = [HVACMode.AUTO]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    def __init__(
        self, coordinator: ParmairCoordinator, description: ParmairClimateDescription
    ) -> None:
        self.entity_description = description
        super().__init__(coordinator, description.key)
        self._attr_min_temp = description.min_temp
        self._attr_max_temp = description.max_temp

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode.AUTO

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """No-op: the unit has no selectable HVAC mode besides AUTO."""
        return

    @property
    def hvac_action(self) -> HVACAction | None:
        data = self.coordinator.data
        if data is None:
            return None
        mode = data.get("temperature_mode")
        if mode is None:
            return None
        return _TEMPERATURE_MODE_TO_HVAC_ACTION.get(int(mode))

    @property
    def current_temperature(self) -> float | None:
        data = self.coordinator.data
        if data is None:
            return None
        return data.get(self.entity_description.current_key)

    @property
    def target_temperature(self) -> float | None:
        return self.register_value

    async def async_set_temperature(self, **kwargs) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        await self.coordinator.async_write(self._key, temperature)
