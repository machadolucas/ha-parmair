"""Shared base entity for the Parmair MAC platforms.

One HA device per config entry (the Parmair unit itself — no subentries in
this integration), matching the single :class:`~.coordinator.ParmairCoordinator`
built in ``__init__.py``.
"""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import ParmairCoordinator


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
