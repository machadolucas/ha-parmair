"""Parmair MAC switch platform.

Description-driven direct-register toggles (``summer_mode``,
``hru_temperature_control``, ``post_heating``, ``week_clock``,
``alarm_sound``); two mode-switches that map onto ``control_state`` rather
than a register of their own (``boost``, ``fireplace`` — the unit has no
independent boost/fireplace bit, only the shared control-state register), and
the ``summer_auto`` switch, which is pure local state (the coordinator's
dwell-gated auto-toggle logic in :mod:`summer_auto` reads it every poll tick)
restored across restarts via ``RestoreEntity``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONTROL_STATE_BOOST,
    CONTROL_STATE_FIREPLACE,
)
from .coordinator import (
    ParmairConfigEntry,
    ParmairCoordinator,
    ParmairData,
    restore_control_state,
)
from .entity import ParmairEntity
from .registers import REGISTER_MAPS


def _direct_is_on(register_key: str) -> Callable[[ParmairData], bool | None]:
    def _fn(data: ParmairData) -> bool | None:
        value = data.get(register_key)
        return None if value is None else bool(value)

    return _fn


def _direct_turn_on(
    register_key: str,
) -> Callable[[ParmairCoordinator, ParmairData], Awaitable[None]]:
    async def _fn(coordinator: ParmairCoordinator, data: ParmairData) -> None:
        await coordinator.async_write(register_key, 1)

    return _fn


def _direct_turn_off(
    register_key: str,
) -> Callable[[ParmairCoordinator, ParmairData], Awaitable[None]]:
    async def _fn(coordinator: ParmairCoordinator, data: ParmairData) -> None:
        await coordinator.async_write(register_key, 0)

    return _fn


def _boost_is_on(data: ParmairData) -> bool | None:
    value = data.get("boost_active")
    return None if value is None else bool(value)


async def _boost_turn_on(coordinator: ParmairCoordinator, data: ParmairData) -> None:
    await coordinator.async_write("control_state", CONTROL_STATE_BOOST)


async def _boost_turn_off(coordinator: ParmairCoordinator, data: ParmairData) -> None:
    await coordinator.async_write("control_state", restore_control_state(data))


def _fireplace_is_on(data: ParmairData) -> bool | None:
    value = data.get("fireplace_active")
    return None if value is None else bool(value)


async def _fireplace_turn_on(coordinator: ParmairCoordinator, data: ParmairData) -> None:
    await coordinator.async_write("control_state", CONTROL_STATE_FIREPLACE)


async def _fireplace_turn_off(coordinator: ParmairCoordinator, data: ParmairData) -> None:
    await coordinator.async_write("control_state", restore_control_state(data))


@dataclass(frozen=True, kw_only=True)
class ParmairSwitchDescription(SwitchEntityDescription):
    """Describes one switch backed by ``is_on_fn``/``turn_on_fn``/``turn_off_fn``.

    ``register_key`` names the register this switch's availability/capability
    gating is keyed on; ``None`` for the mode-switches (``boost``,
    ``fireplace``), which have no capability tag of their own.
    """

    is_on_fn: Callable[[ParmairData], bool | None]
    turn_on_fn: Callable[[ParmairCoordinator, ParmairData], Awaitable[None]]
    turn_off_fn: Callable[[ParmairCoordinator, ParmairData], Awaitable[None]]
    register_key: str | None = None


DESCRIPTIONS: tuple[ParmairSwitchDescription, ...] = (
    ParmairSwitchDescription(
        key="boost",
        is_on_fn=_boost_is_on,
        turn_on_fn=_boost_turn_on,
        turn_off_fn=_boost_turn_off,
    ),
    ParmairSwitchDescription(
        key="fireplace",
        is_on_fn=_fireplace_is_on,
        turn_on_fn=_fireplace_turn_on,
        turn_off_fn=_fireplace_turn_off,
    ),
    ParmairSwitchDescription(
        key="summer_mode",
        register_key="summer_mode",
        is_on_fn=_direct_is_on("summer_mode"),
        turn_on_fn=_direct_turn_on("summer_mode"),
        turn_off_fn=_direct_turn_off("summer_mode"),
    ),
    ParmairSwitchDescription(
        key="hru_temperature_control",
        register_key="hru_temperature_control",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=_direct_is_on("hru_temperature_control"),
        turn_on_fn=_direct_turn_on("hru_temperature_control"),
        turn_off_fn=_direct_turn_off("hru_temperature_control"),
    ),
    ParmairSwitchDescription(
        key="post_heating",
        register_key="post_heating",
        entity_category=EntityCategory.CONFIG,
        is_on_fn=_direct_is_on("post_heating"),
        turn_on_fn=_direct_turn_on("post_heating"),
        turn_off_fn=_direct_turn_off("post_heating"),
    ),
    ParmairSwitchDescription(
        key="week_clock",
        register_key="week_clock",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        is_on_fn=_direct_is_on("week_clock"),
        turn_on_fn=_direct_turn_on("week_clock"),
        turn_off_fn=_direct_turn_off("week_clock"),
    ),
    ParmairSwitchDescription(
        key="alarm_sound",
        register_key="alarm_sound",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        is_on_fn=_direct_is_on("alarm_sound"),
        turn_on_fn=_direct_turn_on("alarm_sound"),
        turn_off_fn=_direct_turn_off("alarm_sound"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ParmairConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up switch entities, gating capability-tagged registers."""
    coordinator = entry.runtime_data
    register_map = REGISTER_MAPS[coordinator.register_map_name]
    included = coordinator.capabilities.included_keys(register_map)

    entities: list[SwitchEntity] = [
        ParmairSwitch(coordinator, description)
        for description in DESCRIPTIONS
        if description.register_key is None or description.register_key in included
    ]
    entities.append(ParmairSummerAutoSwitch(coordinator))
    if coordinator.cooking_configured:
        entities.append(ParmairCookingAutoBoostSwitch(coordinator))
    async_add_entities(entities)


class ParmairSwitch(ParmairEntity, SwitchEntity):
    """One switch driven by its description's is_on/turn_on/turn_off callables."""

    entity_description: ParmairSwitchDescription
    # Availability is derived from is_on_fn below rather than a plain
    # register lookup on self._key (boost/fireplace have no register of
    # their own to check).
    _requires_register = False

    def __init__(
        self, coordinator: ParmairCoordinator, description: ParmairSwitchDescription
    ) -> None:
        self.entity_description = description
        super().__init__(coordinator, description.key)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data
        if data is None:
            return False
        return self.entity_description.is_on_fn(data) is not None

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        return self.entity_description.is_on_fn(data)

    async def async_turn_on(self, **kwargs) -> None:
        await self.entity_description.turn_on_fn(self.coordinator, self.coordinator.data or {})

    async def async_turn_off(self, **kwargs) -> None:
        await self.entity_description.turn_off_fn(self.coordinator, self.coordinator.data or {})


class ParmairSummerAutoSwitch(ParmairEntity, RestoreEntity, SwitchEntity):
    """Local enable flag for the coordinator's dwell-gated summer-mode auto-toggle."""

    _attr_entity_category = EntityCategory.CONFIG
    _requires_register = False

    def __init__(self, coordinator: ParmairCoordinator) -> None:
        super().__init__(coordinator, "summer_auto")

    @property
    def is_on(self) -> bool:
        return self.coordinator.summer_auto_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.summer_auto_enabled = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.summer_auto_enabled = False
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self.coordinator.summer_auto_enabled = last_state.state == "on"


class ParmairCookingAutoBoostSwitch(ParmairEntity, RestoreEntity, SwitchEntity):
    """Local enable flag for the coordinator's cooking-triggered auto-boost.

    Mirrors :class:`ParmairSummerAutoSwitch`: pure local state (the coordinator
    reads ``cooking_auto_boost_enabled`` when a detection starts/ends), default
    OFF, restored across restarts.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:fan-plus"
    _requires_register = False

    def __init__(self, coordinator: ParmairCoordinator) -> None:
        super().__init__(coordinator, "cooking_auto_boost")

    @property
    def is_on(self) -> bool:
        return self.coordinator.cooking_auto_boost_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.cooking_auto_boost_enabled = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.cooking_auto_boost_enabled = False
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self.coordinator.cooking_auto_boost_enabled = last_state.state == "on"
