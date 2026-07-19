"""Parmair MAC number platform.

Register-backed configuration numbers, plus the local summer-auto threshold/
dwell numbers, which are pure local ``RestoreNumber`` state feeding
``coordinator.summer_auto_params`` (mirroring the ``summer_auto`` switch
in :mod:`switch`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import (
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_COOKING_MIN_BOOST_MIN,
    DEFAULT_COOKING_OFF_DELAY_MIN,
    DEFAULT_COOKING_SENSITIVITY,
    DEFAULT_SUMMER_AUTO_OFF_DWELL_MIN,
    DEFAULT_SUMMER_AUTO_OFF_TEMP_C,
    DEFAULT_SUMMER_AUTO_ON_DWELL_MIN,
    DEFAULT_SUMMER_AUTO_ON_TEMP_C,
)
from .coordinator import ParmairConfigEntry, ParmairCoordinator
from .entity import ParmairEntity
from .registers import REGISTER_MAPS


@dataclass(frozen=True, kw_only=True)
class ParmairNumberDescription(NumberEntityDescription):
    """Describes one register-backed number.

    ``display_offset`` supports registers whose raw value differs from the
    value shown to the user by a constant (``home_speed``/``away_speed``
    store 0..4 for display speed 1..5): ``native_value = raw + display_offset``,
    and the offset is subtracted back out before writing.
    """

    mode: NumberMode = NumberMode.BOX
    entity_category: EntityCategory | None = EntityCategory.CONFIG
    display_offset: float = 0.0


DESCRIPTIONS: tuple[ParmairNumberDescription, ...] = (
    ParmairNumberDescription(
        key="home_speed",
        display_offset=1,
        native_min_value=1,
        native_max_value=5,
        native_step=1,
    ),
    ParmairNumberDescription(
        key="away_speed",
        display_offset=1,
        native_min_value=1,
        native_max_value=5,
        native_step=1,
    ),
    ParmairNumberDescription(
        key="defrost_min_efficiency",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
    ),
    ParmairNumberDescription(
        key="summer_mode_outdoor_limit",
        native_min_value=8.0,
        native_max_value=50.0,
        native_step=0.5,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    ParmairNumberDescription(
        key="co2_boost_start",
        native_min_value=100,
        native_max_value=2000,
        native_step=10,
        native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
    ),
    ParmairNumberDescription(
        key="co2_boost_max",
        native_min_value=100,
        native_max_value=2000,
        native_step=10,
        native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
    ),
    ParmairNumberDescription(
        key="humidity_boost_start",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
    ),
    ParmairNumberDescription(
        key="humidity_boost_range",
        native_min_value=10,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
    ),
    # Disabled by default: advanced/rarely-tuned settings.
    ParmairNumberDescription(
        key="fan_curve_speed_1",
        native_min_value=10,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
    ),
    ParmairNumberDescription(
        key="fan_curve_speed_2",
        native_min_value=10,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
    ),
    ParmairNumberDescription(
        key="fan_curve_speed_3",
        native_min_value=10,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
    ),
    ParmairNumberDescription(
        key="fan_curve_speed_4",
        native_min_value=10,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
    ),
    ParmairNumberDescription(
        key="fan_curve_speed_5",
        native_min_value=10,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
    ),
    ParmairNumberDescription(
        key="boost_outdoor_limit",
        native_min_value=-50.0,
        native_max_value=50.0,
        native_step=0.5,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_registry_enabled_default=False,
    ),
    ParmairNumberDescription(
        key="defrost_stop_temperature",
        native_min_value=0.0,
        native_max_value=15.0,
        native_step=0.5,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_registry_enabled_default=False,
    ),
    ParmairNumberDescription(
        key="defrost_interval",
        native_min_value=0,
        native_max_value=60,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_registry_enabled_default=False,
    ),
    ParmairNumberDescription(
        key="display_brightness",
        native_min_value=0,
        native_max_value=5,
        native_step=1,
        entity_registry_enabled_default=False,
    ),
)


@dataclass(frozen=True, kw_only=True)
class ParmairSummerAutoNumberDescription(NumberEntityDescription):
    """Describes one local summer-auto threshold/dwell number.

    ``params_field`` names the :class:`~.summer_auto.SummerAutoParams` field
    this number drives (updated on the coordinator via ``dataclasses.replace``).
    """

    mode: NumberMode = NumberMode.BOX
    entity_category: EntityCategory | None = EntityCategory.CONFIG
    params_field: str
    default: float


SUMMER_AUTO_DESCRIPTIONS: tuple[ParmairSummerAutoNumberDescription, ...] = (
    ParmairSummerAutoNumberDescription(
        key="summer_auto_on_temperature",
        params_field="on_temp_c",
        default=DEFAULT_SUMMER_AUTO_ON_TEMP_C,
        native_min_value=5.0,
        native_max_value=30.0,
        native_step=0.5,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    ParmairSummerAutoNumberDescription(
        key="summer_auto_off_temperature",
        params_field="off_temp_c",
        default=DEFAULT_SUMMER_AUTO_OFF_TEMP_C,
        native_min_value=0.0,
        native_max_value=25.0,
        native_step=0.5,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    ),
    ParmairSummerAutoNumberDescription(
        key="summer_auto_on_minutes",
        params_field="on_dwell_min",
        default=DEFAULT_SUMMER_AUTO_ON_DWELL_MIN,
        native_min_value=1,
        native_max_value=720,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
    ParmairSummerAutoNumberDescription(
        key="summer_auto_off_minutes",
        params_field="off_dwell_min",
        default=DEFAULT_SUMMER_AUTO_OFF_DWELL_MIN,
        native_min_value=1,
        native_max_value=720,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
)


@dataclass(frozen=True, kw_only=True)
class ParmairCookingNumberDescription(NumberEntityDescription):
    """Describes one local cooking-detection tuning number.

    ``params_field`` names the :class:`~.cooking_detect.CookingParams` field
    this number drives (updated on the coordinator via ``dataclasses.replace``),
    mirroring :class:`ParmairSummerAutoNumberDescription`. ``cooking_min_boost_minutes``
    isn't part of the frozen detector params at all — it's boost-restore glue,
    not detector math — so it sets ``params_field=None`` and instead names a
    plain ``coordinator`` attribute via ``attr_field``.
    """

    mode: NumberMode = NumberMode.BOX
    entity_category: EntityCategory | None = EntityCategory.CONFIG
    params_field: str | None
    attr_field: str | None = None
    default: float


COOKING_DESCRIPTIONS: tuple[ParmairCookingNumberDescription, ...] = (
    ParmairCookingNumberDescription(
        key="cooking_sensitivity",
        params_field="sensitivity",
        default=DEFAULT_COOKING_SENSITIVITY,
        native_min_value=1,
        native_max_value=10,
        native_step=1,
    ),
    ParmairCookingNumberDescription(
        key="cooking_off_delay",
        params_field="off_delay_min",
        default=DEFAULT_COOKING_OFF_DELAY_MIN,
        native_min_value=1,
        native_max_value=30,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
    ParmairCookingNumberDescription(
        key="cooking_min_boost_minutes",
        params_field=None,
        attr_field="cooking_min_boost_run_min",
        default=DEFAULT_COOKING_MIN_BOOST_MIN,
        native_min_value=0,
        native_max_value=60,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ParmairConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up number entities, gating capability-tagged registers."""
    coordinator = entry.runtime_data
    register_map = REGISTER_MAPS[coordinator.register_map_name]
    included = coordinator.capabilities.included_keys(register_map)

    entities: list[NumberEntity] = [
        ParmairNumber(coordinator, description)
        for description in DESCRIPTIONS
        if description.key in included
    ]
    entities.extend(
        ParmairSummerAutoNumber(coordinator, description)
        for description in SUMMER_AUTO_DESCRIPTIONS
    )
    if coordinator.cooking_configured:
        entities.extend(
            ParmairCookingNumber(coordinator, description) for description in COOKING_DESCRIPTIONS
        )
    async_add_entities(entities)


class ParmairNumber(ParmairEntity, NumberEntity):
    """One register-backed configuration number."""

    entity_description: ParmairNumberDescription

    def __init__(
        self, coordinator: ParmairCoordinator, description: ParmairNumberDescription
    ) -> None:
        self.entity_description = description
        super().__init__(coordinator, description.key)

    @property
    def native_value(self) -> float | None:
        value = self.register_value
        if value is None:
            return None
        return value + self.entity_description.display_offset

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write(
            self._key, value - self.entity_description.display_offset
        )


class ParmairSummerAutoNumber(ParmairEntity, RestoreNumber):
    """Local threshold/dwell number driving ``coordinator.summer_auto_params``."""

    entity_description: ParmairSummerAutoNumberDescription
    _requires_register = False

    def __init__(
        self, coordinator: ParmairCoordinator, description: ParmairSummerAutoNumberDescription
    ) -> None:
        self.entity_description = description
        super().__init__(coordinator, description.key)
        self._native_value = description.default

    @property
    def native_value(self) -> float | None:
        return self._native_value

    async def async_set_native_value(self, value: float) -> None:
        self._native_value = value
        self.coordinator.summer_auto_params = replace(
            self.coordinator.summer_auto_params, **{self.entity_description.params_field: value}
        )
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        value = (
            last_data.native_value
            if last_data is not None and last_data.native_value is not None
            else self.entity_description.default
        )
        self._native_value = value
        self.coordinator.summer_auto_params = replace(
            self.coordinator.summer_auto_params, **{self.entity_description.params_field: value}
        )


class ParmairCookingNumber(ParmairEntity, RestoreNumber):
    """Local cooking-detection tuning number.

    Drives either ``coordinator.cooking_params`` (via the description's
    ``params_field``) or a plain coordinator attribute (via ``attr_field``),
    mirroring :class:`ParmairSummerAutoNumber` but supporting both targets —
    see :class:`ParmairCookingNumberDescription`.
    """

    entity_description: ParmairCookingNumberDescription
    _requires_register = False

    def __init__(
        self, coordinator: ParmairCoordinator, description: ParmairCookingNumberDescription
    ) -> None:
        self.entity_description = description
        super().__init__(coordinator, description.key)
        self._native_value = description.default

    @property
    def native_value(self) -> float | None:
        return self._native_value

    def _apply(self, value: float) -> None:
        description = self.entity_description
        if description.params_field is not None:
            self.coordinator.cooking_params = replace(
                self.coordinator.cooking_params, **{description.params_field: value}
            )
        else:
            setattr(self.coordinator, description.attr_field, value)

    async def async_set_native_value(self, value: float) -> None:
        self._native_value = value
        self._apply(value)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        value = (
            last_data.native_value
            if last_data is not None and last_data.native_value is not None
            else self.entity_description.default
        )
        self._native_value = value
        self._apply(value)
