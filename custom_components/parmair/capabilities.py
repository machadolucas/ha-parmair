"""Unit capability detection (pure module — no Home Assistant imports).

Which optional sensors/actuators a specific Parmair MAC unit has fitted
cannot be assumed from firmware alone: the static configuration registers
(``heater_type``, ``m10_sensor_type``, ...) describe how the M10/M11/M12
multi-purpose inputs are wired, but some optional sensors (CO2, main-duct
humidity) are read on their own dedicated registers independent of that
wiring. ``parse_capabilities`` therefore combines the static registers with a
one-shot probe read of the optional sensors — an absent-sentinel read
(``None`` after :func:`registers.decode`) is the authoritative "not fitted"
signal, on top of whatever the static config claims.

The resulting :class:`Capabilities` is stored in the config entry (via
``as_dict``/``from_dict``) so setup after a restart doesn't need to re-probe,
and it drives both the read plan (``included_keys``) and which entities get
created for a given unit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from .registers import (
    CAP_CO2,
    CAP_HEATER,
    CAP_HUMIDITY_SENSOR,
    CAP_M12_BOOST,
    CAP_M12_DEFLECTION,
    CAP_MAIN_HUMIDITY,
    CAP_WATER_HEATER,
    RegisterMap,
)

# heater_type (reg 240)
HEATER_TYPE_WATER = 0
HEATER_TYPE_ELECTRIC = 1
HEATER_TYPE_NONE = 2

# m10_sensor_type (reg 242)
M10_SENSOR_NONE = 0
M10_SENSOR_CO2 = 1
M10_SENSOR_HUMIDITY = 2

# m12_usage (reg 243) values gating the two M12-dependent capabilities
M12_USAGE_BOOST_0_10V = 4
M12_USAGE_SUPPLY_DEFLECTION = 5


@dataclass(frozen=True)
class Capabilities:
    """Detected feature set of one physical unit, resolved once at setup."""

    machine_type: int
    heater_type: int
    recovery_type: int
    m10_sensor_type: int
    m12_usage: int
    m11_potentiometer_priority: int
    sw_version: str
    fw_version: str
    has_co2: bool
    has_wet_room_humidity: bool
    has_main_humidity: bool

    @property
    def has_heater(self) -> bool:
        """True for water or electric post-heater; false when reg 240 == 2 (none)."""
        return self.heater_type in (HEATER_TYPE_WATER, HEATER_TYPE_ELECTRIC)

    @property
    def has_water_heater(self) -> bool:
        return self.heater_type == HEATER_TYPE_WATER

    @property
    def model_name(self) -> str:
        return f"MAC {self.machine_type}"

    def supports(self, capability: str) -> bool:
        """Resolve one of the ``registers.CAP_*`` strings against this unit."""
        if capability == CAP_CO2:
            return self.has_co2
        if capability == CAP_HUMIDITY_SENSOR:
            return self.has_wet_room_humidity
        if capability == CAP_MAIN_HUMIDITY:
            return self.has_main_humidity
        if capability == CAP_HEATER:
            return self.has_heater
        if capability == CAP_WATER_HEATER:
            return self.has_water_heater
        if capability == CAP_M12_BOOST:
            return self.m12_usage == M12_USAGE_BOOST_0_10V
        if capability == CAP_M12_DEFLECTION:
            return self.m12_usage == M12_USAGE_SUPPLY_DEFLECTION
        raise ValueError(f"unknown capability {capability!r}")

    def included_keys(self, map_: RegisterMap) -> set[str]:
        """Register keys to poll/expose: ungated, or gated by a supported capability."""
        return {
            key
            for key, definition in map_.registers.items()
            if definition.capability is None or self.supports(definition.capability)
        }

    def as_dict(self) -> dict:
        """Plain JSON-safe dict, for storage in the config entry."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping) -> Capabilities:
        """Inverse of :meth:`as_dict`."""
        return cls(**data)


def parse_capabilities(
    static_values: Mapping[str, float | int | None],
    probe_values: Mapping[str, int | None],
) -> Capabilities:
    """Build :class:`Capabilities` from decoded static registers + a sensor probe.

    ``static_values`` is the decoded static-register dict keyed by register
    key (``machine_type``, ``heater_type``, ``recovery_type``,
    ``m10_sensor_type``, ``m12_usage``, ``m11_potentiometer_priority``,
    ``software_version``, ``firmware_version``). ``probe_values`` holds
    decoded one-shot reads for ``co2``, ``wet_room_humidity``, ``humidity`` —
    ``None`` means :func:`registers.decode` mapped the -1 absent sentinel,
    i.e. that sensor isn't fitted.
    """
    m10_sensor_type = int(static_values["m10_sensor_type"])
    has_co2 = m10_sensor_type == M10_SENSOR_CO2 or probe_values["co2"] is not None
    has_wet_room_humidity = (
        m10_sensor_type == M10_SENSOR_HUMIDITY or probe_values["wet_room_humidity"] is not None
    )
    has_main_humidity = probe_values["humidity"] is not None

    return Capabilities(
        machine_type=int(static_values["machine_type"]),
        heater_type=int(static_values["heater_type"]),
        recovery_type=int(static_values["recovery_type"]),
        m10_sensor_type=m10_sensor_type,
        m12_usage=int(static_values["m12_usage"]),
        m11_potentiometer_priority=int(static_values["m11_potentiometer_priority"]),
        sw_version=f"{float(static_values['software_version']):.2f}",
        fw_version=f"{float(static_values['firmware_version']):.2f}",
        has_co2=has_co2,
        has_wet_room_humidity=has_wet_room_humidity,
        has_main_humidity=has_main_humidity,
    )
