"""Constants for the Parmair MAC integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "parmair"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.FAN,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONF_SCAN_INTERVAL = "scan_interval"
CONF_CO2_OFFSET = "co2_offset"
CONF_SUMMER_AUTO_SOURCE = "summer_auto_source"
CONF_COOKING_SENSORS = "cooking_sensors"
CONF_REGISTER_MAP = "register_map"
CONF_CAPABILITIES = "capabilities"
CONF_REDETECT = "redetect"

DEFAULT_PORT = 502
DEFAULT_NAME = "Parmair"
DEFAULT_SCAN_INTERVAL = 10  # seconds
DEFAULT_CO2_OFFSET = 0
UNIT_ID = 0  # the Multi24 answers on unit/slave id 0, not 1

# Summer-auto defaults (SummerAutoParams) — the summer-auto switch/number entities let
# the user override these; the coordinator falls back to them until then.
DEFAULT_SUMMER_AUTO_ON_TEMP_C = 21.0
DEFAULT_SUMMER_AUTO_ON_DWELL_MIN = 60.0
DEFAULT_SUMMER_AUTO_OFF_TEMP_C = 15.0
DEFAULT_SUMMER_AUTO_OFF_DWELL_MIN = 120.0

# Cooking-detection defaults — the cooking switch/number entities override these
# at runtime; the coordinator seeds the detector with them. sensitivity/off-delay
# feed CookingParams; min-boost is boost glue (a coordinator attribute, not
# detector math).
DEFAULT_COOKING_SENSITIVITY = 5.0
DEFAULT_COOKING_OFF_DELAY_MIN = 4.0
DEFAULT_COOKING_MIN_BOOST_MIN = 10.0

# Dispatcher signal (formatted with the entry_id) the cooking binary-sensor and
# score sensor subscribe to: the event-driven cooking path never touches
# ``coordinator.data``/``async_set_updated_data`` (that would fake a register
# poll every ~2 s), so it nudges those entities over the dispatcher instead.
SIGNAL_COOKING_UPDATE = "parmair_cooking_update_{}"

# Learned-baseline persistence (homeassistant.helpers.storage.Store). Saved on a
# fixed interval plus an unload flush rather than debounced per event — 2 s
# events would defer a delay-save forever.
COOKING_STORAGE_VERSION = 1
COOKING_STORAGE_KEY = "parmair_cooking_{}"
COOKING_SAVE_INTERVAL_S = 900
# Heartbeat cadence while a detection is active, so an all-silent sensor set
# still times the detection out (the detector only re-evaluates on a tick).
COOKING_HEARTBEAT_S = 30

MANUFACTURER = "Parmair"

# IV01_CONTROLSTATE_FO (reg 185). 4 = YLIPAINEISTUS (overpressure), used for
# fireplace mode; 5-8 are the week-clock variants of 1-4; 9 = manual speed.
CONTROL_STATE_STOP = 0
CONTROL_STATE_AWAY = 1
CONTROL_STATE_HOME = 2
CONTROL_STATE_BOOST = 3
CONTROL_STATE_FIREPLACE = 4
CONTROL_STATE_MANUAL = 9

CONTROL_STATE_NAMES: dict[int, str] = {
    0: "stopped",
    1: "away",
    2: "home",
    3: "boost",
    4: "fireplace",
    5: "away_program",
    6: "home_program",
    7: "boost_program",
    8: "fireplace_program",
    9: "manual",
}

# IV01_TEMP_FO (reg 188)
TEMPERATURE_MODE_NAMES: dict[int, str] = {
    0: "standby",
    1: "ventilation",
    2: "heating",
    3: "cooling",
}

# ALARMS_STATE_FI (reg 206)
ALARM_STATE_NAMES: dict[int, str] = {
    0: "ok",
    1: "alarm",
    2: "filter_dirty",
}

# POWER_BTN_FI (reg 208): 0=off, 1=turning off, 2=turning on, 3=on.
# Writing 2 starts the unit, writing 1 stops it.
POWER_STATE_OFF = 0
POWER_STATE_TURNING_OFF = 1
POWER_STATE_TURNING_ON = 2
POWER_STATE_ON = 3

# IV01_SPEED_FOC (reg 187): 0=AUTO, 1=stop, 2..6 = manual speed 1..5.
SPEED_CONTROL_AUTO = 0
SPEED_CONTROL_STOP = 1

# Enum-valued settings registers (raw -> minutes / speed / months).
BOOST_DURATION_MINUTES: dict[int, int] = {0: 30, 1: 60, 2: 90, 3: 120, 4: 180}
FIREPLACE_DURATION_MINUTES: dict[int, int] = {0: 15, 1: 30, 2: 45, 3: 60, 4: 120}
BOOST_SPEED_VALUES: dict[int, int] = {2: 3, 3: 4, 4: 5}  # BOOST_SETTING_S 2-4 = speed 3-5
FILTER_INTERVAL_MONTHS: dict[int, int] = {0: 3, 1: 4, 2: 6}

# After writing register X the device reports the effect on register Y; the
# post-write verify refresh reads Y's block (defaults to the written key).
VERIFY_KEY: dict[str, str] = {
    "control_state": "boost_active",  # boost/fireplace writes reflect in *_active
    "speed_control": "fan_speed_state",
    "power_state": "power_state",
}

# Repairs issue ids
ISSUE_FILTER_CHANGE_DUE = "filter_change_due"
ISSUE_ACTIVE_ALARM = "active_alarm"
ISSUE_CONNECTION_LOST = "connection_lost"

# Number of consecutive fully-failed update cycles before the
# connection_lost repair issue is raised.
CONNECTION_LOST_THRESHOLD = 5

# Card
CARD_FILENAME = "parmair-card.js"
CARD_URL_BASE = f"/{DOMAIN}"
