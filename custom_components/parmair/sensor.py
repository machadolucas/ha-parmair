"""Parmair MAC sensor platform.

One frozen :class:`ParmairSensorDescription` per register-backed sensor and a
single generic :class:`ParmairSensor` entity. ``async_setup_entry`` only
creates entities whose backing register(s) are included for this unit
(``Capabilities.included_keys``) — optional sensors the physical unit doesn't
have (main-duct humidity, wet-room humidity, the water-heater return-water
sensor, the two M12-dependent signals) never appear.

Two sensors — ``filter_last_change``/``filter_next_change`` — aren't backed by
a single register at all: they compose a :class:`datetime.date` from three
raw settings registers each (day/month/year), so they carry a ``value_fn``
and gate on all three backing keys instead of on ``description.key``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ALARM_STATE_NAMES,
    CONF_CO2_OFFSET,
    CONTROL_STATE_NAMES,
    DEFAULT_CO2_OFFSET,
    SIGNAL_COOKING_UPDATE,
    TEMPERATURE_MODE_NAMES,
)
from .coordinator import ParmairConfigEntry, ParmairCoordinator
from .entity import ParmairEntity
from .registers import REGISTER_MAPS


def _compose_date(
    coordinator: ParmairCoordinator, day_key: str, month_key: str, year_key: str
) -> dt.date | None:
    """Build a date from three decoded settings registers (garbage -> None)."""
    data = coordinator.data
    if data is None:
        return None
    day, month, year = data.get(day_key), data.get(month_key), data.get(year_key)
    if day is None or month is None or year is None:
        return None
    try:
        return dt.date(int(year), int(month), int(day))
    except ValueError:
        return None


def _filter_last_change(coordinator: ParmairCoordinator) -> dt.date | None:
    return _compose_date(coordinator, "filter_day", "filter_month", "filter_year")


def _filter_next_change(coordinator: ParmairCoordinator) -> dt.date | None:
    return _compose_date(coordinator, "filter_next_day", "filter_next_month", "filter_next_year")


@dataclass(frozen=True, kw_only=True)
class ParmairSensorDescription(SensorEntityDescription):
    """Extra fields layered onto :class:`SensorEntityDescription`."""

    # Full override for values not derived from a single register (the two
    # composed filter-date sensors). Receives the coordinator directly.
    value_fn: Callable[[ParmairCoordinator], object] | None = None
    # Raw values below zero (device convention for "inactive") read as 0.
    clamp_negative_to_zero: bool = False
    # Maps a decoded int onto its ``strings.json`` enum state string.
    enum_map: dict[int, str] | None = None
    # Register keys that gate entity creation, when they differ from `key`
    # (the two composed filter-date sensors, which aren't a register key at
    # all). Defaults to ``(key,)``.
    backing_keys: tuple[str, ...] | None = None
    # False only for the two composed filter-date sensors: they have no
    # single backing register, so availability can't require one.
    requires_register: bool = True


SENSOR_DESCRIPTIONS: tuple[ParmairSensorDescription, ...] = (
    # --- Temperatures ---
    ParmairSensorDescription(
        key="fresh_air_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    ParmairSensorDescription(
        key="supply_temperature_after_hru",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    ParmairSensorDescription(
        key="supply_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    ParmairSensorDescription(
        key="extract_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    ParmairSensorDescription(
        key="waste_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    # --- Humidity ---
    ParmairSensorDescription(
        key="hru_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ParmairSensorDescription(
        key="humidity_24h_average",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ParmairSensorDescription(
        key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    ParmairSensorDescription(
        key="wet_room_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # --- CO2 (offset-adjusted in ParmairSensor.native_value) ---
    ParmairSensorDescription(
        key="co2",
        device_class=SensorDeviceClass.CO2,
        native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # --- Fan/HRU percentages ---
    ParmairSensorDescription(
        key="heat_recovery_efficiency",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    ParmairSensorDescription(
        key="supply_fan_output",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    ParmairSensorDescription(
        key="extract_fan_output",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    # --- Numeric state ---
    ParmairSensorDescription(
        key="fan_speed_state",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # --- Enums ---
    ParmairSensorDescription(
        key="control_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(CONTROL_STATE_NAMES.values()),
        enum_map=CONTROL_STATE_NAMES,
    ),
    ParmairSensorDescription(
        key="temperature_mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(TEMPERATURE_MODE_NAMES.values()),
        enum_map=TEMPERATURE_MODE_NAMES,
    ),
    ParmairSensorDescription(
        key="alarm_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(ALARM_STATE_NAMES.values()),
        enum_map=ALARM_STATE_NAMES,
    ),
    # --- Timers (raw -1 == inactive) ---
    ParmairSensorDescription(
        key="boost_time_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        clamp_negative_to_zero=True,
    ),
    ParmairSensorDescription(
        key="fireplace_time_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        clamp_negative_to_zero=True,
    ),
    # --- Composed filter dates (no single backing register) ---
    ParmairSensorDescription(
        key="filter_last_change",
        device_class=SensorDeviceClass.DATE,
        value_fn=_filter_last_change,
        backing_keys=("filter_day", "filter_month", "filter_year"),
        requires_register=False,
    ),
    ParmairSensorDescription(
        key="filter_next_change",
        device_class=SensorDeviceClass.DATE,
        value_fn=_filter_next_change,
        backing_keys=("filter_next_day", "filter_next_month", "filter_next_year"),
        requires_register=False,
    ),
    # --- Alarm count ---
    ParmairSensorDescription(
        key="active_alarm_count",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # --- Diagnostic, enabled ---
    ParmairSensorDescription(
        key="hru_output",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    ParmairSensorDescription(
        key="post_heater_output",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # --- Diagnostic, disabled by default ---
    ParmairSensorDescription(
        key="preheater_output",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ParmairSensorDescription(
        key="post_heater_return_water",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ParmairSensorDescription(
        key="power_limit",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ParmairSensorDescription(
        key="speed_control_detail",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ParmairSensorDescription(
        key="operating_point",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ParmairSensorDescription(
        key="auto_boost_timer",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        clamp_negative_to_zero=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ParmairSensorDescription(
        key="post_run_timer",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ParmairSensorDescription(
        key="defrost_cycle",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ParmairSensorDescription(
        key="defrost_start_limit_computed",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ParmairSensorDescription(
        key="external_control_signal",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ParmairSensorDescription(
        key="external_boost_signal",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ParmairSensorDescription(
        key="supply_temp_deflection",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    # --- Fault/alarm codes (0-11; 0 = no fault), diagnostic, disabled ---
    *(
        ParmairSensorDescription(
            key=key,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        )
        for key in (
            "fault_fresh_air_sensor",
            "fault_supply_sensor",
            "fault_supply_after_hru_sensor",
            "fault_extract_sensor",
            "fault_waste_sensor",
            "fault_hru_humidity_sensor",
            "fault_supply_fan",
            "fault_extract_fan",
            "alarm_supply_temp_high",
            "alarm_extract_temp_high",
            "alarm_supply_temp_low",
            "alarm_filter",
            "alarm_return_water_low",
            "fault_return_water_sensor",
        )
    ),
)


class ParmairSensor(ParmairEntity, SensorEntity):
    """One register-backed (or register-composed) Parmair sensor."""

    entity_description: ParmairSensorDescription

    def __init__(
        self, coordinator: ParmairCoordinator, description: ParmairSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._requires_register = description.requires_register

    @property
    def native_value(self) -> object:
        if self.entity_description.value_fn is not None:
            return self.entity_description.value_fn(self.coordinator)

        value = self.register_value
        if value is None:
            return None
        if self.entity_description.clamp_negative_to_zero and value < 0:
            value = 0
        if self.entity_description.enum_map is not None:
            return self.entity_description.enum_map.get(int(value))
        if self.entity_description.key == "co2":
            offset = self.coordinator.config_entry.options.get(CONF_CO2_OFFSET, DEFAULT_CO2_OFFSET)
            return value + (offset or 0)
        return value


class ParmairCookingScoreSensor(ParmairEntity, SensorEntity):
    """Diagnostic view of the cooking detector's current fused score.

    Same dispatcher-only availability rationale as
    ``ParmairCookingDetectedBinarySensor`` (binary_sensor.py): the score is
    computed off kitchen-sensor events, not the Modbus poll cycle, so it stays
    available through a Modbus outage rather than following
    ``coordinator.last_update_success``.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _requires_register = False

    def __init__(self, coordinator: ParmairCoordinator) -> None:
        super().__init__(coordinator, "cooking_score")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float:
        return self.coordinator.cooking_score

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_COOKING_UPDATE.format(self.coordinator.config_entry.entry_id),
                self._handle_cooking_update,
            )
        )

    @callback
    def _handle_cooking_update(self) -> None:
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant, entry: ParmairConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up sensor entities for the registers this unit actually has."""
    coordinator = entry.runtime_data
    register_map = REGISTER_MAPS[coordinator.register_map_name]
    included = coordinator.capabilities.included_keys(register_map)

    entities: list[SensorEntity] = [
        ParmairSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
        if all(key in included for key in (description.backing_keys or (description.key,)))
    ]
    if coordinator.cooking_configured:
        entities.append(ParmairCookingScoreSensor(coordinator))
    async_add_entities(entities)
