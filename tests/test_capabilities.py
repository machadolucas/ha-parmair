"""Unit tests for capability detection (no Home Assistant).

capabilities.py has an internal relative import (``from .registers import
...``), so — unlike the fully standalone test_registers.py — it's loaded
under its *real* dotted module name; see tests/conftest.py for why that
makes the relative import resolve.

The base fixture values (static registers + probe) are a live probe of the
user's REXO 120: heater_type=1 (electric), m10_sensor_type=1 (CO2), CO2
probe reads 969 (fitted), wet-room-humidity and main-humidity probes read
back the absent sentinel (None).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PATH = _ROOT / "custom_components" / "parmair" / "capabilities.py"
_MODNAME = "custom_components.parmair.capabilities"
_spec = importlib.util.spec_from_file_location(_MODNAME, _PATH)
capabilities = importlib.util.module_from_spec(_spec)
sys.modules[_MODNAME] = capabilities
_spec.loader.exec_module(capabilities)

_registers_spec = importlib.util.spec_from_file_location(
    "custom_components.parmair.registers", _ROOT / "custom_components" / "parmair" / "registers.py"
)
registers = importlib.util.module_from_spec(_registers_spec)
sys.modules["custom_components.parmair.registers"] = registers
_registers_spec.loader.exec_module(registers)

Capabilities = capabilities.Capabilities
parse_capabilities = capabilities.parse_capabilities
MAP_V1_87 = registers.MAP_V1_87

_BASE_STATIC = {
    "machine_type": 120,
    "heater_type": 1,
    "recovery_type": 0,
    "m10_sensor_type": 1,
    "m12_usage": 3,
    "m11_potentiometer_priority": 0,
    "software_version": 1.87,
    "firmware_version": 2.72,
}
_BASE_PROBE = {"co2": 969, "wet_room_humidity": None, "humidity": None}

_WATER_GATED_KEYS = {
    "post_heater_return_water",
    "alarm_return_water_low",
    "fault_return_water_sensor",
}


def _caps(
    static_overrides: dict | None = None, probe_overrides: dict | None = None
) -> Capabilities:
    static = {**_BASE_STATIC, **(static_overrides or {})}
    probe = {**_BASE_PROBE, **(probe_overrides or {})}
    return parse_capabilities(static, probe)


# --------------------------------------------------------------------------- #
# Base fixture (REXO 120 live probe)
# --------------------------------------------------------------------------- #


def test_base_fixture_detected_flags():
    caps = _caps()
    assert caps.has_co2 is True
    assert caps.has_wet_room_humidity is False
    assert caps.has_main_humidity is False
    assert caps.has_heater is True
    assert caps.has_water_heater is False


def test_base_fixture_model_name():
    assert _caps().model_name == "MAC 120"


def test_base_fixture_versions_formatted():
    caps = _caps()
    assert caps.sw_version == "1.87"
    assert caps.fw_version == "2.72"


def test_base_fixture_supports_each_capability():
    caps = _caps()
    assert caps.supports("co2") is True
    assert caps.supports("humidity_sensor") is False
    assert caps.supports("main_humidity") is False
    assert caps.supports("heater") is True
    assert caps.supports("water_heater") is False
    assert caps.supports("m12_boost") is False  # m12_usage == 3
    assert caps.supports("m12_deflection") is False


def test_base_fixture_included_keys_excludes_unsupported_gated_keys():
    included = _caps().included_keys(MAP_V1_87)
    for key in _WATER_GATED_KEYS | {
        "wet_room_humidity",
        "humidity",
        "external_boost_signal",
        "supply_temp_deflection",
    }:
        assert key not in included


def test_base_fixture_included_keys_includes_supported_gated_keys():
    included = _caps().included_keys(MAP_V1_87)
    for key in ("co2", "co2_boost_start", "post_heating", "post_heater_output"):
        assert key in included


# --------------------------------------------------------------------------- #
# Variants
# --------------------------------------------------------------------------- #


def test_m10_humidity_variant():
    caps = _caps(
        static_overrides={"m10_sensor_type": 2},
        probe_overrides={"co2": None, "wet_room_humidity": 45},
    )
    assert caps.has_co2 is False
    assert caps.has_wet_room_humidity is True
    assert caps.supports("humidity_sensor") is True
    assert "wet_room_humidity" in caps.included_keys(MAP_V1_87)
    assert "co2" not in caps.included_keys(MAP_V1_87)


def test_water_heater_variant_includes_water_gated_keys():
    caps = _caps(static_overrides={"heater_type": 0})
    assert caps.has_heater is True
    assert caps.has_water_heater is True
    included = caps.included_keys(MAP_V1_87)
    for key in _WATER_GATED_KEYS:
        assert key in included


def test_heater_type_none_has_no_heater():
    caps = _caps(static_overrides={"heater_type": 2})
    assert caps.has_heater is False
    assert caps.has_water_heater is False
    assert "post_heating" not in caps.included_keys(MAP_V1_87)
    assert "post_heater_output" not in caps.included_keys(MAP_V1_87)


def test_m12_usage_boost_variant():
    caps = _caps(static_overrides={"m12_usage": 4})
    assert caps.supports("m12_boost") is True
    assert caps.supports("m12_deflection") is False
    assert "external_boost_signal" in caps.included_keys(MAP_V1_87)
    assert "supply_temp_deflection" not in caps.included_keys(MAP_V1_87)


def test_m12_usage_deflection_variant():
    caps = _caps(static_overrides={"m12_usage": 5})
    assert caps.supports("m12_boost") is False
    assert caps.supports("m12_deflection") is True
    assert "supply_temp_deflection" in caps.included_keys(MAP_V1_87)
    assert "external_boost_signal" not in caps.included_keys(MAP_V1_87)


# --------------------------------------------------------------------------- #
# as_dict / from_dict round trip
# --------------------------------------------------------------------------- #


def test_as_dict_from_dict_round_trip():
    caps = _caps()
    restored = Capabilities.from_dict(caps.as_dict())
    assert restored == caps


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def test_supports_unknown_capability_raises():
    caps = _caps()
    try:
        caps.supports("not_a_real_capability")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
