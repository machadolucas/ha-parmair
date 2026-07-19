"""Diagnostics for the Parmair MAC integration.

Redacts the Modbus host (the only entry-data field with any privacy weight —
a LAN IP); surfaces the detected capability set, register map, dynamic read
plan, and the coordinator's health counters, which is normally enough to
diagnose a stuck/misbehaving unit without a live HA session.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import ParmairConfigEntry

TO_REDACT = {CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ParmairConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the config entry."""
    coordinator = entry.runtime_data

    read_plan = [
        {"address": block.address, "count": block.count, "n_keys": len(block.keys)}
        for block in coordinator.read_plan
    ]

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "capabilities": coordinator.capabilities.as_dict(),
        "register_map": coordinator.register_map_name,
        "read_plan": read_plan,
        "static_data": coordinator.static_data,
        "data": dict(coordinator.data) if coordinator.data is not None else None,
        "stats": {
            "block_failures": coordinator.block_failures,
            "consecutive_full_failures": coordinator.consecutive_full_failures,
            "last_successful_update": (
                coordinator.last_successful_update.isoformat()
                if coordinator.last_successful_update is not None
                else None
            ),
            "last_update_success": coordinator.last_update_success,
            "summer_auto_enabled": coordinator.summer_auto_enabled,
            "summer_auto_params": asdict(coordinator.summer_auto_params),
        },
        "cooking": {
            "configured": coordinator.cooking_configured,
            "auto_boost_enabled": coordinator.cooking_auto_boost_enabled,
            "params": asdict(coordinator.cooking_params),
            "min_boost_run_min": coordinator.cooking_min_boost_run_min,
            "active": coordinator.cooking_active,
            "score": coordinator.cooking_score,
            # Source entity ids aren't sensitive (only the Modbus host is
            # redacted above), so per-sensor diagnostics are included as-is.
            "sensors": (
                coordinator.cooking_detector.diagnostics(dt_util.utcnow())
                if coordinator.cooking_detector is not None
                else {}
            ),
        },
    }
