"""Unit tests for the cooking detector (no Home Assistant).

cooking_detect.py is stdlib-only with no internal deps, so it's loaded the same
way as test_summer_auto.py (arbitrary sys.modules name). The headline test
replays a real VOC-index trace captured from the kitchen sensor on 2026-07-18.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "parmair"
    / "cooking_detect.py"
)
_spec = importlib.util.spec_from_file_location("parmair_cooking_detect", _PATH)
cooking_detect = importlib.util.module_from_spec(_spec)
sys.modules["parmair_cooking_detect"] = cooking_detect
_spec.loader.exec_module(cooking_detect)

CookingDetector = cooking_detect.CookingDetector
CookingParams = cooking_detect.CookingParams
SensorSpec = cooking_detect.SensorSpec

T0 = datetime(2026, 7, 18, 12, 0)
VOC = "sensor.kitchen_voc"
HUM = "sensor.kitchen_humidity"
PM = "sensor.kitchen_pm25"

DEFAULTS = CookingParams()


def _sec(seconds: float) -> timedelta:
    return timedelta(seconds=seconds)


def _voc_detector(floor: float = 2.0) -> CookingDetector:
    return CookingDetector({VOC: SensorSpec(sigma_floor=floor)})


def _feed(
    detector: CookingDetector,
    entity_id: str,
    start: datetime,
    values,
    step_s: float = 2.0,
    params: CookingParams = DEFAULTS,
):
    """Feed a value trace at a fixed cadence; return (last_time, results)."""
    t = start
    results = []
    for value in values:
        results.append(detector.update(t, entity_id, value, params))
        t += _sec(step_s)
    return t - _sec(step_s), results


def _first_on(results) -> int | None:
    for i, r in enumerate(results):
        if r.transition is True:
            return i
    return None


# --------------------------------------------------------------------------- #
# Measured real-device VOC trace (2026-07-18) — regression fixture
# --------------------------------------------------------------------------- #

# Quiescent baseline: ~10 min of 2 s samples oscillating 78-82.
_QUIESCENT = [78, 80, 82, 81, 79, 78, 80, 82, 80, 79] * 30  # 300 samples ~= 10 min

# Real recorded onset (2 s apart).
_ONSET = [
    82,
    84,
    90,
    104,
    119,
    136,
    156,
    176,
    195,
    214,
    232,
    249,
    265,
    280,
    295,
    307,
    318,
    326,
    333,
    339,
    344,
    347,
    350,
    352,
    354,
]


# Decay/wave phase: 354 -> ~119 over ~3.5 min, back to ~184 over ~1 min,
# dip to ~135, rise to ~193 (approximating the recorded shape).
def _ramp(a: float, b: float, n: int) -> list[float]:
    return [a + (b - a) * i / (n - 1) for i in range(n)]


_WAVE = (
    _ramp(354, 119, 105)  # ~3.5 min decline
    + _ramp(119, 184, 30)  # ~1 min rise
    + _ramp(184, 135, 20)  # dip
    + _ramp(135, 193, 20)  # rise
)

# Real recorded second surge (2 s apart).
_SURGE = [
    211,
    238,
    262,
    284,
    304,
    322,
    339,
    354,
    367,
    374,
    385,
    396,
    405,
    414,
    422,
    429,
    435,
    441,
    447,
    451,
    456,
    460,
    463,
    466,
    469,
    472,
    474,
    476,
    478,
    479,
    481,
    482,
    483,
    484,
    485,
    486,
    487,
    488,
]


def test_measured_voc_trace_regression():
    det = _voc_detector(floor=2.0)

    # Quiescent phase must never trigger and must settle mu near 80.
    t, quiet = _feed(det, VOC, T0, _QUIESCENT)
    assert all(r.transition is None for r in quiet)
    assert not det.active
    diag = det.diagnostics(t)[VOC]
    mu_pre = diag["baseline"]
    assert 78.0 <= mu_pre <= 82.0

    onset_start = t + _sec(2.0)
    t, onset = _feed(det, VOC, onset_start, _ONSET)

    on_idx = _first_on(onset)
    assert on_idx is not None, "onset never triggered"
    # First elevated sample is index 1 (84); ON must land within 8 s of it,
    # i.e. by the 119 sample (index 4) at the latest.
    on_time = onset_start + _sec(2.0 * on_idx)
    first_elevated = onset_start + _sec(2.0 * 1)
    assert (on_time - first_elevated).total_seconds() <= 8.0
    assert on_idx <= 4

    # mu must stay frozen (within +/-3 of its pre-onset value) throughout ON.
    assert abs(det.diagnostics(t)[VOC]["baseline"] - mu_pre) <= 3.0

    # Detection holds through the big decay/rise waves (mu stays frozen, so the
    # trough at ~119 is still ~20 sigma above baseline).
    t, wave = _feed(det, VOC, t + _sec(2.0), _WAVE)
    assert det.active
    assert all(r.transition is not False for r in wave)
    assert abs(det.diagnostics(t)[VOC]["baseline"] - mu_pre) <= 3.0

    # Second surge: still ON, still frozen.
    t, surge = _feed(det, VOC, t + _sec(2.0), _SURGE)
    assert det.active
    assert abs(det.diagnostics(t)[VOC]["baseline"] - mu_pre) <= 3.0
    cook_end = t

    # Synthetic decay back to quiescent, then hold at baseline. OFF must fire
    # only after off_delay_min below threshold (not immediately).
    decay = _ramp(488, 80, 60) + [80, 79, 81, 80] * 120  # decline + ~16 min quiet
    t, tail = _feed(det, VOC, t + _sec(2.0), decay)

    off_idx = next((i for i, r in enumerate(tail) if r.transition is False), None)
    assert off_idx is not None, "detection never ended"
    off_time = t - _sec(2.0 * (len(tail) - 1 - off_idx))

    # Total ON duration spans at least the whole cooking period.
    assert (off_time - on_time).total_seconds() >= (cook_end - on_time).total_seconds()
    # OFF is delayed by roughly off_delay_min after the signal fell to baseline
    # (well after the decay completes, not during the surge).
    assert off_time > cook_end
    assert not det.active


# --------------------------------------------------------------------------- #
# Glitch rejection
# --------------------------------------------------------------------------- #


def test_single_sample_glitch_does_not_trigger():
    det = _voc_detector()
    # Warm up on a flat baseline.
    t, _ = _feed(det, VOC, T0, [80] * 80)
    # One wild sample, then straight back — persistence (3 s) rejects it.
    _, spiked = _feed(det, VOC, t + _sec(2.0), [80, 120, 80, 80, 80])
    assert all(r.transition is None for r in spiked)
    assert not det.active


# --------------------------------------------------------------------------- #
# Two-sensor fusion
# --------------------------------------------------------------------------- #


def _two_sensor_detector() -> CookingDetector:
    return CookingDetector({HUM: SensorSpec(sigma_floor=1.0), PM: SensorSpec(sigma_floor=1.0)})


def _warm_flat(det, entity_id, start, base, params=DEFAULTS):
    return _feed(det, entity_id, start, [base] * 80, params=params)


def test_two_moderate_sensors_trigger_together():
    det = _two_sensor_detector()
    _warm_flat(det, HUM, T0, 40.0)
    _warm_flat(det, PM, T0, 5.0)

    # Interleave the two elevated streams (+4 humidity, +3 PM) at 2 s cadence.
    t = T0 + _sec(160.0)
    triggered = False
    for _ in range(10):
        det.update(t, HUM, 44.0, DEFAULTS)
        r = det.update(t, PM, 8.0, DEFAULTS)
        if r.transition is True or det.active:
            triggered = True
            break
        t += _sec(2.0)
    assert triggered


def test_either_moderate_sensor_alone_does_not_trigger():
    # Humidity alone (+4, floor 1 -> z=4 -> c=0.8 < S_on=1.0).
    det = _two_sensor_detector()
    _warm_flat(det, HUM, T0, 40.0)
    _warm_flat(det, PM, T0, 5.0)  # PM stays at baseline
    _, res = _feed(det, HUM, T0 + _sec(160.0), [44.0] * 15)
    assert all(r.transition is None for r in res)
    assert not det.active

    # PM alone (+3, floor 1 -> z=3 -> c=0.4 < S_on).
    det2 = _two_sensor_detector()
    _warm_flat(det2, HUM, T0, 40.0)
    _warm_flat(det2, PM, T0, 5.0)
    _, res2 = _feed(det2, PM, T0 + _sec(160.0), [8.0] * 15)
    assert all(r.transition is None for r in res2)
    assert not det2.active


# --------------------------------------------------------------------------- #
# Slow ramp (regime shift) is absorbed, never triggers
# --------------------------------------------------------------------------- #


def test_slow_two_hour_ramp_never_triggers_and_mu_tracks():
    det = _voc_detector()
    # VOC 80 -> 140 linearly over 2 h at 2 s cadence (3600 samples).
    ramp = _ramp(80.0, 140.0, 3600)
    t, results = _feed(det, VOC, T0, ramp)
    assert all(r.transition is None for r in results)
    assert not det.active
    # mu should sit close to the final value (small EMA lag only).
    assert abs(det.diagnostics(t)[VOC]["baseline"] - 140.0) <= 10.0


# --------------------------------------------------------------------------- #
# Warm-up / reboot guard
# --------------------------------------------------------------------------- #


def test_fresh_detector_at_reboot_level_does_not_trigger_during_warmup():
    det = _voc_detector()
    # A device reboot re-baselines the index near 100; a fresh detector fed
    # ~100 immediately must not read the (unknown) offset as a spike.
    _, results = _feed(det, VOC, T0, [100, 101, 99, 100, 102, 98, 100] * 5)
    assert all(r.transition is None for r in results)
    assert not det.active
    assert 98.0 <= det.diagnostics(T0 + _sec(2.0 * 34))[VOC]["baseline"] <= 102.0


def test_unavailable_forces_rewarmup_no_trigger_at_new_level():
    det = _voc_detector()
    t, _ = _feed(det, VOC, T0, [80] * 80)  # warm at 80
    # Sensor goes unavailable.
    det.update(t + _sec(2.0), VOC, None, DEFAULTS)
    # Comes back at a brand-new level of 100 (reboot). Re-warm-up must swallow
    # the jump instead of scoring it.
    _, results = _feed(det, VOC, t + _sec(4.0), [100] * 40)
    assert all(r.transition is None for r in results)
    assert not det.active


# --------------------------------------------------------------------------- #
# All-sensors-silent during ON ends detection via tick()
# --------------------------------------------------------------------------- #


def test_all_sensors_unavailable_during_on_ends_via_tick():
    params = CookingParams(off_delay_min=1.0)
    det = _voc_detector()
    t, _ = _feed(det, VOC, T0, [80] * 80, params=params)
    # Drive ON with a sustained strong signal.
    t, res = _feed(det, VOC, t + _sec(2.0), [300] * 40, params=params)
    assert det.active
    t += _sec(2.0)
    # Sensor goes unavailable; nothing feeds the detector after this.
    det.update(t, VOC, None, params)
    # Heartbeat ticks eventually time out the detection (min-on already met).
    ended = False
    for _ in range(200):
        t += _sec(2.0)
        r = det.tick(t, params)
        if r.transition is False:
            ended = True
            break
    assert ended
    assert not det.active


# --------------------------------------------------------------------------- #
# Minimum / maximum on-time
# --------------------------------------------------------------------------- #


def test_min_on_time_keeps_detection_at_least_60s():
    params = CookingParams(off_delay_min=0.02)  # ~1.2 s off delay
    det = _voc_detector()
    t, _ = _feed(det, VOC, T0, [80] * 80, params=params)
    # Trigger with a strong signal, capturing the exact ON timestamp.
    on_time = None
    tt = t + _sec(2.0)
    for _ in range(5):
        r = det.update(tt, VOC, 300.0, params)
        if r.transition is True:
            on_time = tt
            break
        tt += _sec(2.0)
    assert on_time is not None
    # Drop the signal instantly.
    tt += _sec(2.0)
    det.update(tt, VOC, 80.0, params)
    # At +30 s (well past the ~1.2 s off delay, before min-on) still ON.
    assert det.tick(on_time + _sec(30.0), params).active
    # It may only end at/after 60 s of on-time.
    ended = False
    probe = on_time + _sec(32.0)
    for _ in range(60):
        probe += _sec(2.0)
        r = det.tick(probe, params)
        if r.transition is False:
            assert (probe - on_time).total_seconds() >= 60.0
            ended = True
            break
    assert ended


def test_max_on_forces_off_then_rebase_then_retrigger():
    det = _voc_detector()
    t, _ = _feed(det, VOC, T0, [80] * 80)
    # Sustained high signal for > 90 min forces OFF.
    t, res = _feed(det, VOC, t + _sec(2.0), [480] * (46 * 60))  # ~92 min
    assert any(r.transition is False for r in res)
    assert not det.active

    # During the 600 s rebase, evidence is suppressed and mu converges toward
    # the elevated level.
    t, rebase = _feed(det, VOC, t + _sec(2.0), [480] * 300)  # 600 s
    assert all(r.transition is None for r in rebase)
    mu_after = det.diagnostics(t)[VOC]["baseline"]
    assert mu_after > 300.0  # snapped a long way up from ~80

    # A fresh slope spike after rebase re-triggers via the slope detector.
    t, spike = _feed(det, VOC, t + _sec(2.0), _ramp(480, 900, 12))
    assert det.active


# --------------------------------------------------------------------------- #
# snapshot / restore
# --------------------------------------------------------------------------- #


def test_snapshot_restore_accepts_consistent_baseline_and_can_trigger():
    det = _voc_detector()
    t, _ = _feed(det, VOC, T0, [80] * 200)
    snap = det.snapshot(t)
    assert VOC in snap["sensors"]

    fresh = _voc_detector()
    fresh.restore(snap, t + _sec(60.0))
    # First consistent sample (near baseline) is accepted without warm-up.
    r = fresh.update(t + _sec(62.0), VOC, 81.0, DEFAULTS)
    assert r.transition is None
    assert fresh.diagnostics(t + _sec(62.0))[VOC]["status"] == "ok"
    # A cooking onset right after restore triggers without a 120 s warm-up.
    _, onset = _feed(fresh, VOC, t + _sec(64.0), _ONSET)
    assert fresh.active


def test_restore_discards_entries_older_than_24h():
    det = _voc_detector()
    t, _ = _feed(det, VOC, T0, [80] * 200)
    snap = det.snapshot(t)

    fresh = _voc_detector()
    fresh.restore(snap, t + timedelta(hours=25))  # too old -> discarded
    # Baseline was not loaded, so the first samples warm up (no trigger even if
    # they would have looked elevated against the old baseline).
    _, results = _feed(fresh, VOC, t + timedelta(hours=25, seconds=2), [120] * 40)
    assert all(r.transition is None for r in results)
    assert not fresh.active


def test_restore_stale_baseline_resets_to_warmup():
    det = _voc_detector()
    t, _ = _feed(det, VOC, T0, [80] * 200)
    snap = det.snapshot(t)

    fresh = _voc_detector()
    fresh.restore(snap, t + _sec(60.0))
    # First live sample is far above the restored baseline (z > gate): stale,
    # so it resets to warm-up rather than instantly scoring a huge spike.
    r = fresh.update(t + _sec(62.0), VOC, 130.0, DEFAULTS)
    assert r.transition is None
    assert not fresh.active
    _, results = _feed(fresh, VOC, t + _sec(64.0), [130] * 40)
    assert all(r.transition is None for r in results)


# --------------------------------------------------------------------------- #
# Sensitivity monotonicity
# --------------------------------------------------------------------------- #


def test_sensitivity_monotonicity():
    # A borderline spike (~z 4) that does not trigger at low sensitivity does
    # trigger at high sensitivity.
    spike = [80] * 8  # sustained +8 above baseline (floor 2 -> z=4)

    det_low = _voc_detector()
    t, _ = _feed(det_low, VOC, T0, [72] * 80, params=CookingParams(sensitivity=3.0))
    low = CookingParams(sensitivity=3.0)
    _, res_low = _feed(det_low, VOC, t + _sec(2.0), spike, params=low)
    assert all(r.transition is None for r in res_low)
    assert not det_low.active

    det_high = _voc_detector()
    t, _ = _feed(det_high, VOC, T0, [72] * 80, params=CookingParams(sensitivity=9.0))
    high = CookingParams(sensitivity=9.0)
    _, res_high = _feed(det_high, VOC, t + _sec(2.0), spike, params=high)
    assert det_high.active


# --------------------------------------------------------------------------- #
# Sigma floor never collapses
# --------------------------------------------------------------------------- #


def test_sigma_never_collapses_after_constant_input():
    det = _voc_detector(floor=2.0)
    # Hours of perfectly constant input would drive dev -> 0.
    t, _ = _feed(det, VOC, T0, [80.0] * 4000)
    diag = det.diagnostics(t)[VOC]
    assert diag["sigma"] >= 2.0  # floored, not collapsed
    # A +6*floor step yields a bounded z using the floor (no ZeroDivisionError).
    r = det.update(t + _sec(2.0), VOC, 80.0 + 6 * 2.0, DEFAULTS)
    z = det.diagnostics(t + _sec(2.0))[VOC]["z"]
    assert 5.0 <= z <= 7.0
    assert r is not None


# --------------------------------------------------------------------------- #
# tick() with no samples ever
# --------------------------------------------------------------------------- #


def test_tick_with_no_samples_is_inactive():
    det = _voc_detector()
    r = det.tick(T0, DEFAULTS)
    assert r.score == 0.0
    assert not r.active
    assert r.transition is None

    empty = CookingDetector({})
    r2 = empty.tick(T0, DEFAULTS)
    assert r2.score == 0.0
    assert not r2.active
