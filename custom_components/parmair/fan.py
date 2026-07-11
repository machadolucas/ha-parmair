"""Parmair MAC fan platform.

The single primary control entity: HA's fan model (on/off + percentage +
preset) doesn't line up 1:1 with a single device register, so ``ParmairFan``
composes three of them (``power_state`` for on/off, ``speed_control`` for
manual percentage, ``control_state`` for the home/away/boost/fireplace
presets) rather than being a plain register-backed entity.
"""

from __future__ import annotations

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONTROL_STATE_AWAY,
    CONTROL_STATE_BOOST,
    CONTROL_STATE_FIREPLACE,
    CONTROL_STATE_HOME,
    POWER_STATE_ON,
    POWER_STATE_TURNING_OFF,
    POWER_STATE_TURNING_ON,
    SPEED_CONTROL_STOP,
)
from .coordinator import ParmairConfigEntry, ParmairCoordinator
from .entity import ParmairEntity

SPEED_COUNT = 5
PRESET_MODES = ["home", "away", "boost", "fireplace"]

# Control-state values 5-8 are the week-clock ("program") variants of 1-4
# (see const.CONTROL_STATE_NAMES); they still read as the same preset.
_PRESET_FROM_CONTROL_STATE: dict[int, str] = {
    CONTROL_STATE_AWAY: "away",
    CONTROL_STATE_HOME: "home",
    CONTROL_STATE_BOOST: "boost",
    CONTROL_STATE_FIREPLACE: "fireplace",
    CONTROL_STATE_AWAY + 4: "away",
    CONTROL_STATE_HOME + 4: "home",
    CONTROL_STATE_BOOST + 4: "boost",
    CONTROL_STATE_FIREPLACE + 4: "fireplace",
}

_CONTROL_STATE_FROM_PRESET: dict[str, int] = {
    "away": CONTROL_STATE_AWAY,
    "home": CONTROL_STATE_HOME,
    "boost": CONTROL_STATE_BOOST,
    "fireplace": CONTROL_STATE_FIREPLACE,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ParmairConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the single ventilation fan entity."""
    async_add_entities([ParmairFan(entry.runtime_data)])


class ParmairFan(ParmairEntity, FanEntity):
    """The unit's primary on/off + speed + preset control."""

    _attr_name = None
    _attr_speed_count = SPEED_COUNT
    _attr_preset_modes = PRESET_MODES
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    # Not backed by its own register (see entity.ParmairEntity); availability
    # is overridden below to key off ``power_state`` instead.
    _requires_register = False

    def __init__(self, coordinator: ParmairCoordinator) -> None:
        super().__init__(coordinator, "fan")

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data
        return data is not None and data.get("power_state") is not None

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        power_state = data.get("power_state")
        if power_state is None:
            return None
        return power_state in (POWER_STATE_TURNING_ON, POWER_STATE_ON)

    @property
    def percentage(self) -> int | None:
        data = self.coordinator.data
        if data is None:
            return None
        speed = data.get("fan_speed_state")
        if speed is None:
            return None
        return int(speed) * 20

    @property
    def preset_mode(self) -> str | None:
        data = self.coordinator.data
        if data is None:
            return None
        control_state = data.get("control_state")
        if control_state is None:
            return None
        return _PRESET_FROM_CONTROL_STATE.get(int(control_state))

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self.coordinator.async_write("speed_control", SPEED_CONTROL_STOP)
            return
        speed = max(1, min(5, round(percentage / 20)))
        await self.coordinator.async_write("speed_control", speed + 1)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        value = _CONTROL_STATE_FROM_PRESET[preset_mode]
        # Releasing manual speed control first is required before the
        # control-state write takes effect.
        await self.coordinator.async_write_sequence(
            [("speed_control", 0), ("control_state", value)]
        )

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs,
    ) -> None:
        await self.coordinator.async_write("power_state", POWER_STATE_TURNING_ON)
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
        elif percentage is not None:
            await self.async_set_percentage(percentage)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_write("power_state", POWER_STATE_TURNING_OFF)
