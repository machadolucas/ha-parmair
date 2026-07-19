"""Parmair MAC binary_sensor platform.

One frozen :class:`ParmairBinarySensorDescription` per boolean register and a
single generic :class:`ParmairBinarySensor` entity. Several descriptions'
``key`` (used for the unique id and translation, per ``strings.json``) don't
match the register that actually backs them — e.g. ``home`` reads
``home_state``, ``alarm`` reads ``summary_alarm``, ``filter_change_required``
reads ``filter_state`` — so ``register_key`` carries the real register key
and defaults to ``key`` when they're the same.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import SIGNAL_COOKING_UPDATE
from .coordinator import ParmairConfigEntry, ParmairCoordinator
from .entity import ParmairEntity
from .registers import REGISTER_MAPS


@dataclass(frozen=True, kw_only=True)
class ParmairBinarySensorDescription(BinarySensorEntityDescription):
    """Extra fields layered onto :class:`BinarySensorEntityDescription`."""

    # The register key backing this entity, when it differs from `key`
    # (unique_id/translation_key). Defaults to `key` in __post_init__.
    register_key: str | None = None
    # Custom on/off rule for registers where raw 1 doesn't mean "on"
    # (filter_change_required is "on" when filter_state == 0).
    is_on_fn: Callable[[float | int], bool] | None = None

    def __post_init__(self) -> None:
        if self.register_key is None:
            object.__setattr__(self, "register_key", self.key)


BINARY_SENSOR_DESCRIPTIONS: tuple[ParmairBinarySensorDescription, ...] = (
    ParmairBinarySensorDescription(
        key="defrosting",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    ParmairBinarySensorDescription(
        key="home",
        device_class=BinarySensorDeviceClass.PRESENCE,
        register_key="home_state",
    ),
    ParmairBinarySensorDescription(
        key="boost_active",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    ParmairBinarySensorDescription(
        key="fireplace_active",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    ParmairBinarySensorDescription(
        key="filter_change_required",
        device_class=BinarySensorDeviceClass.PROBLEM,
        register_key="filter_state",
        is_on_fn=lambda value: value == 0,
    ),
    ParmairBinarySensorDescription(
        key="alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
        register_key="summary_alarm",
    ),
    ParmairBinarySensorDescription(
        key="boost_switch_input",
    ),
    ParmairBinarySensorDescription(
        key="home_switch_input",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ParmairBinarySensorDescription(
        key="fireplace_switch_input",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    ParmairBinarySensorDescription(
        key="io_initialized",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


class ParmairBinarySensor(ParmairEntity, BinarySensorEntity):
    """One register-backed Parmair boolean sensor."""

    entity_description: ParmairBinarySensorDescription

    def __init__(
        self, coordinator: ParmairCoordinator, description: ParmairBinarySensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def register_value(self) -> float | int | None:
        """Override: reads ``register_key``, which may differ from ``key``."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.entity_description.register_key)

    @property
    def is_on(self) -> bool | None:
        value = self.register_value
        if value is None:
            return None
        if self.entity_description.is_on_fn is not None:
            return self.entity_description.is_on_fn(value)
        return value == 1


class ParmairCookingDetectedBinarySensor(ParmairEntity, BinarySensorEntity):
    """Whether the adaptive multi-sensor cooking detector currently holds a detection.

    Purely dispatcher-driven (see ``coordinator.async_setup_cooking``/
    ``_handle_cooking_result``): the detector runs off kitchen source-sensor
    state-change events, never the Modbus poll cycle, so unlike every other
    entity here ``available`` is NOT allowed to fall back to
    ``coordinator.last_update_success`` (the ``_requires_register = False``
    default in :class:`~.entity.ParmairEntity`) — a stalled/failing Modbus
    poll must not blank out a cooking detection that keeps computing fine
    from its own independent event stream. It's simply available whenever the
    entry is loaded.
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _requires_register = False

    def __init__(self, coordinator: ParmairCoordinator) -> None:
        super().__init__(coordinator, "cooking_detected")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.cooking_active

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        params = self.coordinator.cooking_params
        threshold = 5.0 / params.sensitivity if params.sensitivity else 0.0
        detector = self.coordinator.cooking_detector
        sensors = detector.diagnostics(dt_util.utcnow()) if detector is not None else {}
        return {
            "score": round(self.coordinator.cooking_score, 2),
            "threshold": round(threshold, 2),
            "sensors": sensors,
        }

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
    """Set up binary_sensor entities for the registers this unit actually has."""
    coordinator = entry.runtime_data
    register_map = REGISTER_MAPS[coordinator.register_map_name]
    included = coordinator.capabilities.included_keys(register_map)

    entities: list[BinarySensorEntity] = [
        ParmairBinarySensor(coordinator, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
        if description.register_key in included
    ]
    if coordinator.cooking_configured:
        entities.append(ParmairCookingDetectedBinarySensor(coordinator))
    async_add_entities(entities)
