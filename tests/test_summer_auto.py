"""Unit tests for the summer-mode dwell/auto-toggle logic (no Home Assistant).

summer_auto.py is stdlib-only with no internal deps, so it's loaded the same
way as test_registers.py (arbitrary sys.modules name).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta

_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "parmair" / "summer_auto.py"
)
_spec = importlib.util.spec_from_file_location("parmair_summer_auto", _PATH)
summer_auto = importlib.util.module_from_spec(_spec)
sys.modules["parmair_summer_auto"] = summer_auto
_spec.loader.exec_module(summer_auto)

SummerAutoLogic = summer_auto.SummerAutoLogic
SummerAutoParams = summer_auto.SummerAutoParams

T0 = datetime(2026, 7, 11, 12, 0)
PARAMS = SummerAutoParams(on_temp_c=18.0, on_dwell_min=30.0, off_temp_c=14.0, off_dwell_min=30.0)


def _advance(minutes: float) -> timedelta:
    return timedelta(minutes=minutes)


# --------------------------------------------------------------------------- #
# Turn-on dwell
# --------------------------------------------------------------------------- #


def test_fires_true_only_after_continuous_dwell():
    logic = SummerAutoLogic()
    assert logic.update(T0, 20.0, False, PARAMS) is None
    assert logic.update(T0 + _advance(15), 20.0, False, PARAMS) is None
    assert logic.update(T0 + _advance(29.9), 20.0, False, PARAMS) is None
    assert logic.update(T0 + _advance(30), 20.0, False, PARAMS) is True


def test_dip_below_on_temp_mid_dwell_resets():
    logic = SummerAutoLogic()
    logic.update(T0, 20.0, False, PARAMS)
    # 20 minutes in, still short of the 30-minute dwell.
    logic.update(T0 + _advance(20), 20.0, False, PARAMS)
    # Dips into the dead band: resets the "above" timer.
    logic.update(T0 + _advance(21), 16.0, False, PARAMS)
    # Back above threshold: the first tick after a reset (re)starts the timer here.
    logic.update(T0 + _advance(22), 20.0, False, PARAMS)
    result = logic.update(T0 + _advance(22 + 29), 20.0, False, PARAMS)
    assert result is None
    result = logic.update(T0 + _advance(22 + 30), 20.0, False, PARAMS)
    assert result is True


def test_no_refire_while_already_on():
    logic = SummerAutoLogic()
    logic.update(T0, 20.0, False, PARAMS)
    fired_at = logic.update(T0 + _advance(30), 20.0, False, PARAMS)
    assert fired_at is True
    # Unit now reports summer_on=True; further ticks above threshold must stay quiet.
    for minutes in (35, 60, 120):
        assert logic.update(T0 + _advance(minutes), 20.0, True, PARAMS) is None


# --------------------------------------------------------------------------- #
# Turn-off dwell (mirror image)
# --------------------------------------------------------------------------- #


def test_fires_false_only_after_continuous_dwell():
    logic = SummerAutoLogic()
    assert logic.update(T0, 10.0, True, PARAMS) is None
    assert logic.update(T0 + _advance(29.9), 10.0, True, PARAMS) is None
    assert logic.update(T0 + _advance(30), 10.0, True, PARAMS) is False


def test_dip_above_off_temp_mid_dwell_resets():
    logic = SummerAutoLogic()
    logic.update(T0, 10.0, True, PARAMS)
    logic.update(T0 + _advance(15), 16.0, True, PARAMS)  # dead band: resets
    logic.update(T0 + _advance(16), 10.0, True, PARAMS)  # first tick after reset restarts timer
    result = logic.update(T0 + _advance(16 + 29), 10.0, True, PARAMS)
    assert result is None
    result = logic.update(T0 + _advance(16 + 30), 10.0, True, PARAMS)
    assert result is False


def test_no_refire_while_already_off():
    logic = SummerAutoLogic()
    logic.update(T0, 10.0, True, PARAMS)
    fired_at = logic.update(T0 + _advance(30), 10.0, True, PARAMS)
    assert fired_at is False
    for minutes in (35, 60, 120):
        assert logic.update(T0 + _advance(minutes), 10.0, False, PARAMS) is None


# --------------------------------------------------------------------------- #
# Dead band and missing readings
# --------------------------------------------------------------------------- #


def test_band_between_thresholds_resets_both_timers():
    logic = SummerAutoLogic()
    logic.update(T0, 20.0, False, PARAMS)  # start an "above" timer
    logic.update(T0 + _advance(10), 16.0, False, PARAMS)  # dead band
    # Both timers must be clear now: neither on nor off can fire immediately.
    assert logic.update(T0 + _advance(11), 20.0, False, PARAMS) is None
    assert logic.update(T0 + _advance(11), 10.0, True, PARAMS) is None


def test_none_temperature_resets_state():
    logic = SummerAutoLogic()
    logic.update(T0, 20.0, False, PARAMS)
    logic.update(T0 + _advance(20), 20.0, False, PARAMS)
    assert logic.update(T0 + _advance(21), None, False, PARAMS) is None
    # First tick after the reset (re)starts the timer here, from scratch.
    logic.update(T0 + _advance(22), 20.0, False, PARAMS)
    assert logic.update(T0 + _advance(22 + 29), 20.0, False, PARAMS) is None
    assert logic.update(T0 + _advance(22 + 30), 20.0, False, PARAMS) is True


# --------------------------------------------------------------------------- #
# Misconfiguration guard
# --------------------------------------------------------------------------- #


def test_misconfigured_params_never_fires():
    bad = SummerAutoParams(on_temp_c=14.0, on_dwell_min=30.0, off_temp_c=18.0, off_dwell_min=30.0)
    logic = SummerAutoLogic()
    for minutes in range(0, 200, 10):
        result_hot = logic.update(T0 + _advance(minutes), 30.0, False, bad)
        assert result_hot is None
        result_cold = logic.update(T0 + _advance(minutes), 0.0, True, bad)
        assert result_cold is None


def test_equal_thresholds_is_also_misconfiguration():
    bad = SummerAutoParams(on_temp_c=16.0, on_dwell_min=30.0, off_temp_c=16.0, off_dwell_min=30.0)
    logic = SummerAutoLogic()
    for minutes in range(0, 100, 10):
        assert logic.update(T0 + _advance(minutes), 16.0, False, bad) is None


# --------------------------------------------------------------------------- #
# reset()
# --------------------------------------------------------------------------- #


def test_reset_clears_partial_dwell():
    logic = SummerAutoLogic()
    logic.update(T0, 20.0, False, PARAMS)
    logic.update(T0 + _advance(20), 20.0, False, PARAMS)
    logic.reset()
    # The 20 minutes already accumulated must not count: the next tick restarts the timer.
    logic.update(T0 + _advance(21), 20.0, False, PARAMS)
    assert logic.update(T0 + _advance(21 + 29), 20.0, False, PARAMS) is None
    assert logic.update(T0 + _advance(21 + 30), 20.0, False, PARAMS) is True


# --------------------------------------------------------------------------- #
# Fractional minutes
# --------------------------------------------------------------------------- #


def test_fractional_dwell_minutes():
    params = SummerAutoParams(on_temp_c=18.0, on_dwell_min=0.5, off_temp_c=14.0, off_dwell_min=0.5)
    logic = SummerAutoLogic()
    logic.update(T0, 20.0, False, params)
    assert logic.update(T0 + timedelta(seconds=29), 20.0, False, params) is None
    assert logic.update(T0 + timedelta(seconds=30), 20.0, False, params) is True
