"""Parmair MAC Modbus register map (pure module — no Home Assistant imports).

Transcribed from the manufacturer spec "Modbus Parmair" v1.87 (registers
1-245). The Multi24 controller exposes each spec register at on-wire holding
register address = register number + 1000 (e.g. IV01_CONTROLSTATE_FO, spec
register 185, is read at address 1185). Function codes 03/06/16, unit id 0,
values are int16; MinLimit/MaxLimit in the spec (and here) are in engineering
units, i.e. already scaled.

Optional sensors that are not fitted read -1 (``absent_sentinel``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Capability gate names resolved against capabilities.Capabilities attributes
# ("co2" -> has_co2). A RegisterDef with a capability the unit lacks is
# excluded from the read plan and no entity is created for it.
CAP_CO2 = "co2"
CAP_HUMIDITY_SENSOR = "humidity_sensor"
CAP_MAIN_HUMIDITY = "main_humidity"
CAP_HEATER = "heater"
CAP_WATER_HEATER = "water_heater"
CAP_M12_BOOST = "m12_boost"
CAP_M12_DEFLECTION = "m12_deflection"

# Read-plan tuning. The Multi24 is slow; blocks are kept short and gap reads
# (spanning spec-undefined registers) are assumed OK. If a live unit faults on
# gap-spanning reads, set MAX_GAP to 0 (planner then emits contiguous-only
# blocks).
MAX_GAP = 8
MAX_BLOCK = 100


@dataclass(frozen=True, slots=True)
class RegisterDef:
    """One holding register from the v1.87 spec."""

    key: str
    register: int  # spec register number (1..245); on-wire address adds the map offset
    scale: float = 1.0  # engineering value = raw * scale (spec Kerroin 10 -> 0.1)
    writable: bool = False
    signed: bool = True
    min_value: float | None = None  # engineering units
    max_value: float | None = None
    static: bool = False  # read once at setup, not part of the polling plan
    absent_sentinel: int | None = None  # raw value meaning "sensor not fitted"
    capability: str | None = None


def _d(*args, **kwargs) -> RegisterDef:
    return RegisterDef(*args, **kwargs)


_V1_87_DEFS: tuple[RegisterDef, ...] = (
    # --- 1 SYSTEM SETTINGS ---
    _d("acknowledge_alarms", 3, writable=True, min_value=0, max_value=1),
    _d("active_alarm_count", 4, min_value=0, max_value=100),
    _d("summary_alarm", 5, min_value=0, max_value=1),
    _d("alarm_sound", 6, writable=True, min_value=0, max_value=1),
    _d("display_brightness", 7, writable=True, min_value=0, max_value=5),
    _d("setup_state", 16, static=True, min_value=0, max_value=2),
    _d("firmware_version", 17, scale=0.01, static=True),
    _d("software_version", 18, scale=0.01, static=True),
    _d("bootloader_version", 19, scale=0.01, static=True),
    # --- 2 PHYSICAL INPUTS ---
    _d("fresh_air_temperature", 20, scale=0.1, min_value=-50.0, max_value=120.0),
    _d("supply_temperature_after_hru", 22, scale=0.1, min_value=-50.0, max_value=120.0),
    _d("supply_temperature", 23, scale=0.1, min_value=-50.0, max_value=120.0),
    _d("extract_temperature", 24, scale=0.1, min_value=-50.0, max_value=120.0),
    _d("waste_temperature", 25, scale=0.1, min_value=-50.0, max_value=120.0),
    _d("hru_humidity", 26, min_value=0, max_value=100),
    _d(
        "wet_room_humidity",
        30,
        min_value=0,
        max_value=100,
        absent_sentinel=-1,
        capability=CAP_HUMIDITY_SENSOR,
    ),
    _d("co2", 31, min_value=0, max_value=2000, absent_sentinel=-1, capability=CAP_CO2),
    _d("external_control_signal", 32, scale=0.1, min_value=0.0, max_value=100.0),
    _d("home_switch_input", 33, min_value=0, max_value=1),
    _d("fireplace_switch_input", 34, min_value=0, max_value=1),
    _d("boost_switch_input", 35, min_value=0, max_value=1),
    _d(
        "external_boost_signal",
        36,
        scale=0.1,
        min_value=0.0,
        max_value=100.0,
        capability=CAP_M12_BOOST,
    ),
    _d(
        "supply_temp_deflection",
        37,
        scale=0.1,
        min_value=-3.0,
        max_value=3.0,
        capability=CAP_M12_DEFLECTION,
    ),
    _d(
        "post_heater_return_water",
        38,
        scale=0.1,
        min_value=-99.0,
        max_value=120.0,
        capability=CAP_WATER_HEATER,
    ),
    # --- 3 PHYSICAL OUTPUTS ---
    _d("supply_fan_output", 40, scale=0.1, min_value=0.0, max_value=100.0),
    _d("extract_fan_output", 42, scale=0.1, min_value=0.0, max_value=100.0),
    _d(
        "post_heater_output",
        44,
        scale=0.1,
        min_value=0.0,
        max_value=100.0,
        capability=CAP_HEATER,
    ),
    _d("hru_output", 46, scale=0.1, min_value=0.0, max_value=100.0),
    _d("preheater_output", 48, scale=0.1, min_value=0.0, max_value=100.0),
    # --- 4 SETTINGS ---
    _d("extract_temperature_target", 60, scale=0.1, writable=True, min_value=18.0, max_value=26.0),
    _d("supply_temperature_target", 65, scale=0.1, writable=True, min_value=15.0, max_value=25.0),
    _d("summer_mode_outdoor_limit", 78, scale=0.1, writable=True, min_value=8.0, max_value=50.0),
    _d("summer_mode", 79, writable=True, min_value=0, max_value=1),
    _d("boost_outdoor_limit", 80, scale=0.1, writable=True, min_value=-50.0, max_value=50.0),
    _d("filter_interval", 85, writable=True, min_value=0, max_value=2),
    _d("filter_day", 86, writable=True, min_value=1, max_value=31),
    _d("filter_month", 87, writable=True, min_value=1, max_value=12),
    _d("filter_year", 88, writable=True, min_value=2000, max_value=3000),
    _d("filter_next_day", 89, writable=True, min_value=1, max_value=31),
    _d("filter_next_month", 90, writable=True, min_value=1, max_value=12),
    _d("filter_next_year", 91, writable=True, min_value=2000, max_value=3000),
    _d(
        "co2_boost_start",
        92,
        writable=True,
        min_value=100,
        max_value=2000,
        capability=CAP_CO2,
    ),
    _d(
        "co2_boost_max",
        93,
        writable=True,
        min_value=100,
        max_value=2000,
        capability=CAP_CO2,
    ),
    _d("defrost_start_limit_computed", 96, scale=0.1, min_value=-30.0, max_value=30.0),
    _d("defrost_min_efficiency", 97, writable=True, min_value=0, max_value=100),
    _d("defrost_stop_temperature", 98, scale=0.1, writable=True, min_value=0.0, max_value=15.0),
    _d("defrost_interval", 99, writable=True, min_value=0, max_value=60),
    _d("home_speed", 104, writable=True, min_value=0, max_value=4),
    _d("away_speed", 105, writable=True, min_value=0, max_value=4),
    _d("boost_duration", 106, writable=True, min_value=0, max_value=4),
    _d("fireplace_duration", 107, writable=True, min_value=0, max_value=4),
    _d("week_clock", 108, writable=True, min_value=0, max_value=1),
    _d("post_heating", 109, writable=True, min_value=0, max_value=1, capability=CAP_HEATER),
    _d("humidity_boost_start", 114, writable=True, min_value=0, max_value=100),
    _d("humidity_boost_range", 115, writable=True, min_value=10, max_value=100),
    _d("hru_temperature_control", 116, writable=True, min_value=0, max_value=1),
    _d("boost_speed", 117, writable=True, min_value=2, max_value=4),
    _d("fan_curve_speed_1", 120, writable=True, min_value=10, max_value=100),
    _d("fan_curve_speed_2", 121, writable=True, min_value=10, max_value=100),
    _d("fan_curve_speed_3", 122, writable=True, min_value=10, max_value=100),
    _d("fan_curve_speed_4", 123, writable=True, min_value=10, max_value=100),
    _d("fan_curve_speed_5", 124, writable=True, min_value=10, max_value=100),
    # --- 6 SOFT MEASUREMENTS AND CONTROLS ---
    _d(
        "humidity",
        180,
        min_value=0,
        max_value=100,
        absent_sentinel=-1,
        capability=CAP_MAIN_HUMIDITY,
    ),
    _d("defrosting", 183, min_value=0, max_value=1),
    _d("operating_point", 184, min_value=0, max_value=150),
    _d("control_state", 185, writable=True, min_value=0, max_value=9),
    _d("fan_speed_state", 186, min_value=0, max_value=5),
    _d("speed_control", 187, writable=True, min_value=0, max_value=6),
    _d("temperature_mode", 188, min_value=0, max_value=3),
    _d("speed_control_detail", 189, min_value=0, max_value=23),
    _d("heat_recovery_efficiency", 190, scale=0.1, min_value=0.0, max_value=100.0),
    _d("power_limit", 191, scale=0.1, min_value=0.0, max_value=100.0),
    _d("humidity_24h_average", 192, scale=0.1, min_value=0.0, max_value=100.0),
    _d("auto_boost_timer", 193, min_value=-1, max_value=600),
    _d("defrost_cycle", 194, min_value=1, max_value=6),
    _d("home_state", 200, min_value=0, max_value=1),
    _d("boost_active", 201, min_value=0, max_value=1),
    _d("boost_time_remaining", 202, writable=True, min_value=-1, max_value=300),
    _d("fireplace_active", 203, min_value=0, max_value=1),
    _d("fireplace_time_remaining", 204, min_value=-1, max_value=300),
    _d("filter_state", 205, writable=True, min_value=0, max_value=1),
    _d("alarm_state", 206, min_value=0, max_value=2),
    _d("io_initialized", 207, min_value=0, max_value=1),
    _d("power_state", 208, writable=True, min_value=0, max_value=3),
    _d("post_run_timer", 209, min_value=0, max_value=500),
    # --- 7 ALARMS (fault/alarm codes 0-11; 0 = no alarm) ---
    _d("fault_fresh_air_sensor", 220, min_value=0, max_value=11),
    _d("fault_supply_sensor", 221, min_value=0, max_value=11),
    _d("fault_supply_after_hru_sensor", 222, min_value=0, max_value=11),
    _d("fault_extract_sensor", 223, min_value=0, max_value=11),
    _d("fault_waste_sensor", 224, min_value=0, max_value=11),
    _d("fault_hru_humidity_sensor", 225, min_value=0, max_value=11),
    _d("fault_supply_fan", 226, min_value=0, max_value=11),
    _d("fault_extract_fan", 227, min_value=0, max_value=11),
    _d("alarm_supply_temp_high", 228, min_value=0, max_value=11),
    _d("alarm_extract_temp_high", 229, min_value=0, max_value=11),
    _d("alarm_supply_temp_low", 230, min_value=0, max_value=11),
    _d("alarm_filter", 231, min_value=0, max_value=11),
    _d(
        "alarm_return_water_low",
        232,
        min_value=0,
        max_value=11,
        capability=CAP_WATER_HEATER,
    ),
    _d(
        "fault_return_water_sensor",
        238,
        min_value=-1,
        max_value=11,
        capability=CAP_WATER_HEATER,
    ),
    # --- 10 CONFIGURATION PARAMETERS (static, drive capability detection) ---
    _d("heater_type", 240, static=True, min_value=0, max_value=2),
    _d("recovery_type", 241, static=True, min_value=0, max_value=1),
    _d("m10_sensor_type", 242, static=True, min_value=0, max_value=2),
    _d("m12_usage", 243, static=True, min_value=0, max_value=5),
    _d("machine_type", 244, static=True, min_value=0, max_value=600),
    _d("m11_potentiometer_priority", 245, static=True, min_value=0, max_value=1),
)


@dataclass(frozen=True)
class RegisterMap:
    """A firmware family's register layout."""

    name: str
    registers: dict[str, RegisterDef]
    address_offset: int = 1000

    def address(self, definition: RegisterDef) -> int:
        return definition.register + self.address_offset

    def address_for(self, key: str) -> int:
        return self.address(self.registers[key])


def _build_map(name: str, defs: Iterable[RegisterDef]) -> RegisterMap:
    registers = {d.key: d for d in defs}
    return RegisterMap(name=name, registers=registers)


MAP_V1_87 = _build_map("v1_87", _V1_87_DEFS)

# Future firmware families (e.g. the newer "MAC v2" layout with different
# addresses) plug in here; entry.data stores the map name.
REGISTER_MAPS: dict[str, RegisterMap] = {MAP_V1_87.name: MAP_V1_87}
DEFAULT_MAP = MAP_V1_87.name


@dataclass(frozen=True, slots=True)
class ReadBlock:
    """One Modbus read spanning ``count`` registers from ``address``."""

    address: int
    count: int
    keys: tuple[str, ...]


def build_read_plan(
    map_: RegisterMap,
    keys: Iterable[str],
    *,
    max_gap: int = MAX_GAP,
    max_block: int = MAX_BLOCK,
) -> list[ReadBlock]:
    """Coalesce the given registers into bulk-read blocks.

    Consecutive registers whose address gap is <= ``max_gap`` share a block
    (the gap registers are read and discarded); blocks never exceed
    ``max_block`` registers (Modbus caps a read at 125).
    """
    defs = sorted((map_.registers[k] for k in keys), key=lambda d: d.register)
    blocks: list[ReadBlock] = []
    current: list[RegisterDef] = []
    for d in defs:
        if current:
            start = map_.address(current[0])
            gap = map_.address(d) - (map_.address(current[-1]) + 1)
            length = map_.address(d) - start + 1
            if gap > max_gap or length > max_block:
                blocks.append(_finish_block(map_, current))
                current = []
        current.append(d)
    if current:
        blocks.append(_finish_block(map_, current))
    return blocks


def _finish_block(map_: RegisterMap, defs: list[RegisterDef]) -> ReadBlock:
    start = map_.address(defs[0])
    count = map_.address(defs[-1]) - start + 1
    return ReadBlock(address=start, count=count, keys=tuple(d.key for d in defs))


def decode(raw: int, definition: RegisterDef) -> float | int | None:
    """Raw word -> engineering value (None when the sensor is absent)."""
    value = raw
    if definition.signed and value >= 0x8000:
        value -= 0x10000
    if definition.absent_sentinel is not None and value == definition.absent_sentinel:
        return None
    if definition.scale != 1.0:
        # Round to the scale's precision to avoid float dust (24.700000000000003).
        decimals = max(0, -_scale_exponent(definition.scale))
        return round(value * definition.scale, decimals)
    return value


def encode(value: float | int, definition: RegisterDef) -> int:
    """Engineering value -> raw word, clamped to the spec limits."""
    if definition.min_value is not None:
        value = max(definition.min_value, value)
    if definition.max_value is not None:
        value = min(definition.max_value, value)
    raw = round(value / definition.scale)
    if raw < 0:
        if not definition.signed:
            raise ValueError(f"negative value {value} for unsigned register {definition.key}")
        raw += 0x10000
    if not 0 <= raw <= 0xFFFF:
        raise ValueError(f"value {value} out of int16 range for register {definition.key}")
    return raw


def _scale_exponent(scale: float) -> int:
    """Exponent of a decimal scale (0.1 -> -1, 0.01 -> -2, 1.0 -> 0)."""
    exponent = 0
    while scale < 0.999:
        scale *= 10
        exponent -= 1
    return exponent
