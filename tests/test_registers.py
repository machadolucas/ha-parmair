"""Unit tests for the v1.87 register map (no Home Assistant).

registers.py has zero internal deps (no relative imports), so — like
test_models.py in ha-wind-forecast-fi — it's loaded standalone via
importlib under an arbitrary sys.modules name; capabilities.py (which does
have a relative import on this module) is loaded under its real dotted name
in test_capabilities.py instead.

Several assertions below are ground truth pinned against the manufacturer
PDF and a live probe of the user's REXO 120 (see the module docstring in
registers.py and the Live read-only Modbus probe task) rather than derived
from the code under test, so they'd catch a transcription error.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "parmair" / "registers.py"
)
_spec = importlib.util.spec_from_file_location("parmair_registers", _PATH)
registers = importlib.util.module_from_spec(_spec)
sys.modules["parmair_registers"] = registers
_spec.loader.exec_module(registers)

RegisterDef = registers.RegisterDef
MAP_V1_87 = registers.MAP_V1_87
_DEFS = registers._V1_87_DEFS


# --------------------------------------------------------------------------- #
# Map integrity
# --------------------------------------------------------------------------- #


def test_all_keys_unique():
    keys = [d.key for d in _DEFS]
    assert len(keys) == len(set(keys))


def test_all_register_numbers_unique():
    numbers = [d.register for d in _DEFS]
    assert len(numbers) == len(set(numbers))


def test_all_registers_in_spec_range():
    assert all(1 <= d.register <= 245 for d in _DEFS)


def test_capability_strings_are_all_valid():
    valid = {v for name, v in vars(registers).items() if name.startswith("CAP_")}
    assert valid  # sanity: the module actually defines some
    for d in _DEFS:
        if d.capability is not None:
            assert d.capability in valid


# --------------------------------------------------------------------------- #
# Spot values from the manufacturer PDF
# --------------------------------------------------------------------------- #


def test_supply_temperature_target():
    d = MAP_V1_87.registers["supply_temperature_target"]
    assert d.register == 65
    assert d.scale == 0.1
    assert d.min_value == 15.0
    assert d.max_value == 25.0
    assert d.writable is True


def test_extract_temperature_target():
    d = MAP_V1_87.registers["extract_temperature_target"]
    assert d.register == 60
    assert d.min_value == 18.0
    assert d.max_value == 26.0


def test_co2():
    d = MAP_V1_87.registers["co2"]
    assert d.register == 31
    assert d.absent_sentinel == -1
    assert d.capability == "co2"


def test_summer_mode_writable():
    d = MAP_V1_87.registers["summer_mode"]
    assert d.register == 79
    assert d.writable is True


def test_control_state():
    d = MAP_V1_87.registers["control_state"]
    assert d.register == 185
    assert d.writable is True
    assert d.max_value == 9


def test_speed_control():
    d = MAP_V1_87.registers["speed_control"]
    assert d.register == 187
    assert d.max_value == 6


def test_software_version_static():
    d = MAP_V1_87.registers["software_version"]
    assert d.register == 18
    assert d.scale == 0.01
    assert d.static is True


def test_machine_type_static():
    d = MAP_V1_87.registers["machine_type"]
    assert d.register == 244
    assert d.static is True


def test_address_for_control_state():
    assert MAP_V1_87.address_for("control_state") == 1185


# --------------------------------------------------------------------------- #
# build_read_plan
# --------------------------------------------------------------------------- #


def _dynamic_keys() -> list[str]:
    return [k for k, d in MAP_V1_87.registers.items() if not d.static]


def test_every_dynamic_key_appears_in_exactly_one_block():
    keys = _dynamic_keys()
    blocks = registers.build_read_plan(MAP_V1_87, keys)
    seen: list[str] = []
    for block in blocks:
        seen.extend(block.keys)
    assert sorted(seen) == sorted(keys)
    assert len(seen) == len(set(seen))


def test_block_counts_within_modbus_limit():
    blocks = registers.build_read_plan(MAP_V1_87, _dynamic_keys())
    assert all(b.count <= 100 for b in blocks)


def test_first_block_starts_at_lowest_dynamic_address():
    blocks = registers.build_read_plan(MAP_V1_87, _dynamic_keys())
    assert blocks[0].address == 1003


def test_alarms_block_spans_exactly_1220_to_1238():
    blocks = registers.build_read_plan(MAP_V1_87, _dynamic_keys())
    alarms_block = next(b for b in blocks if b.address == 1220)
    assert alarms_block.count == 19


def test_gaps_within_a_block_never_exceed_max_gap():
    blocks = registers.build_read_plan(MAP_V1_87, _dynamic_keys())
    for block in blocks:
        addresses = [MAP_V1_87.address_for(k) for k in block.keys]
        for prev, nxt in zip(addresses, addresses[1:], strict=False):
            assert nxt - prev - 1 <= registers.MAX_GAP


def test_max_gap_zero_yields_only_contiguous_blocks():
    blocks = registers.build_read_plan(MAP_V1_87, _dynamic_keys(), max_gap=0)
    for block in blocks:
        # No filler registers were read: count matches the number of real keys.
        assert block.count == len(block.keys)


def test_static_keys_excluded_by_the_caller():
    blocks = registers.build_read_plan(MAP_V1_87, _dynamic_keys())
    all_keys = {k for b in blocks for k in b.keys}
    static_keys = {k for k, d in MAP_V1_87.registers.items() if d.static}
    assert all_keys.isdisjoint(static_keys)


# --------------------------------------------------------------------------- #
# decode / encode
# --------------------------------------------------------------------------- #


def test_decode_negative_temperature():
    d = MAP_V1_87.registers["fresh_air_temperature"]
    assert registers.decode(0xFFCE, d) == -5.0


def test_decode_absent_sentinel_returns_none():
    d = MAP_V1_87.registers["co2"]
    assert registers.decode(65535, d) is None


def test_decode_software_version():
    d = MAP_V1_87.registers["software_version"]
    assert registers.decode(187, d) == 1.87


def test_encode_negative_temperature():
    d = MAP_V1_87.registers["fresh_air_temperature"]
    assert registers.encode(-5.0, d) == 0xFFCE


def test_encode_clamps_to_max():
    d = MAP_V1_87.registers["supply_temperature_target"]
    assert registers.encode(30.0, d) == 250


def test_encode_round_trips_for_a_dozen_defs():
    cases = [
        ("fresh_air_temperature", -5.0),
        ("fresh_air_temperature", 20.0),
        ("supply_temperature", 21.5),
        ("extract_temperature", 22.0),
        ("waste_temperature", -10.3),
        ("supply_temperature_target", 20.0),
        ("extract_temperature_target", 24.0),
        ("summer_mode_outdoor_limit", 12.5),
        ("boost_outdoor_limit", -20.0),
        ("heat_recovery_efficiency", 86.4),
        ("humidity_24h_average", 54.3),
        ("control_state", 4),
    ]
    for key, value in cases:
        d = MAP_V1_87.registers[key]
        raw = registers.encode(value, d)
        assert registers.decode(raw, d) == value


def test_encode_raises_for_negative_value_on_unsigned_def():
    d = RegisterDef(key="test_unsigned", register=999, signed=False)
    try:
        registers.encode(-1, d)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


# --------------------------------------------------------------------------- #
# Real-device regression values (live probe, REXO 120)
# --------------------------------------------------------------------------- #


def test_decode_heat_recovery_efficiency_live_value():
    d = MAP_V1_87.registers["heat_recovery_efficiency"]
    assert registers.decode(864, d) == 86.4


def test_decode_humidity_24h_average_live_value():
    d = MAP_V1_87.registers["humidity_24h_average"]
    assert registers.decode(543, d) == 54.3


def test_decode_humidity_absent_on_live_unit():
    d = MAP_V1_87.registers["humidity"]
    assert registers.decode(65535, d) is None
