"""Shared base entity for the Parmair MAC platforms.

One HA device per config entry (the Parmair unit itself — no subentries in
this integration), matching the single :class:`~.coordinator.ParmairCoordinator`
built in ``__init__.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SIGNAL_COOKING_UPDATE
from .coordinator import ParmairCoordinator
from .modbus import ParmairConnectionError


class ParmairEntity(CoordinatorEntity[ParmairCoordinator]):
    """Base entity for one register-backed Parmair value."""

    _attr_has_entity_name = True

    # Entities not backed by a polled register (e.g. the local summer-auto
    # switch/numbers, which are purely local state) set this to False in
    # their subclass so ``available`` falls back to the coordinator's own
    # last-update-success instead of requiring a register value.
    _requires_register: bool = True

    def __init__(self, coordinator: ParmairCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = coordinator.device_info

    @property
    def register_value(self) -> float | int | None:
        """This entity's current decoded value (``None`` before/without data)."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._key)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if not self._requires_register:
            return True
        return self.register_value is not None

    async def _async_guard_write(self, write: Awaitable[None]) -> None:
        """Await a coordinator write, surfacing transport failures to the caller.

        A raw :class:`~.modbus.ParmairConnectionError` reaches a service caller
        as "Unknown error" plus a traceback in the log; re-raising it as a
        translated ``HomeAssistantError`` shows the user what actually failed.
        """
        try:
            await write
        except ParmairConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def _async_write(self, key: str, value: float | int) -> None:
        """``coordinator.async_write`` with :meth:`_async_guard_write` error mapping."""
        await self._async_guard_write(self.coordinator.async_write(key, value))

    async def _async_write_sequence(self, pairs: list[tuple[str, float | int]]) -> None:
        """``coordinator.async_write_sequence`` with :meth:`_async_guard_write` mapping."""
        await self._async_guard_write(self.coordinator.async_write_sequence(pairs))


class ParmairCookingEntity(ParmairEntity):
    """Base for the dispatcher-driven cooking-detection entities.

    The detector runs off kitchen source-sensor state-change events, never the
    Modbus poll cycle, so these entities are refreshed via
    ``SIGNAL_COOKING_UPDATE`` only. Two consequences:

    * ``available`` is NOT allowed to follow ``coordinator.last_update_success``
      — a stalled/failing Modbus poll must not blank out a detection that keeps
      computing fine from its own event stream. They're available whenever the
      entry is loaded.
    * The inherited per-poll ``_handle_coordinator_update`` would re-write the
      state every scan interval; with time-dependent attributes (the detector's
      per-sensor status) that means a new recorder row each poll for nothing.
      Since ``available`` is constant too, a poll has nothing to tell these
      entities, so it's dropped outright.
    """

    _requires_register = False

    @property
    def available(self) -> bool:
        return True

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
    def _handle_coordinator_update(self) -> None:
        # Deliberately empty — see the class docstring. If ``available`` is
        # ever tied back to the coordinator, this must write on a flip.
        return

    @callback
    def _handle_cooking_update(self) -> None:
        self.async_write_ha_state()
