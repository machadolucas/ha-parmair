"""Event-driven cooking detector (pure module — stdlib only).

Cooking dumps a fast, large transient into kitchen air-quality signals (VOC/NOx
index, humidity, particulates) that a fixed absolute threshold can't track
across seasons, sensor noise, or ESPHome reboots. ``CookingDetector`` instead
learns a per-sensor baseline online and scores how far *above* that baseline
each sensor currently sits, fusing the sensors into a single score and running
a hysteretic state machine on it.

Design choices worth stating up front, because they drive every constant below:

* **One-sided z.** Cooking only *raises* these signals; a drop below baseline
  (airing the room, a sensor re-baselining downward after a reboot) must never
  score. So evidence uses ``max(0, x - mu)`` only.
* **Freeze, don't chase.** The baseline is frozen while a detection is active
  (and briefly after). A 45 s cooking ramp would otherwise drag ``mu`` up with
  it and the signal would "disappear" into its own baseline mid-cook. Freezing
  — not a slow time constant — is what makes long cooks and their big
  oscillating waves hold the detection ON.
* **Warm-up guard.** After a cold start, an unavailable episode, or a long gap
  the baseline is rebuilt from scratch and produces no evidence for a spell:
  an ESPHome reboot restarts the Sensirion index near 100 and re-baselines for
  minutes, which must not read as a spike.

The module is deliberately Home-Assistant-free: ``now`` is injected on every
call and per-call tuning arrives in a :class:`CookingParams` (mirroring
``summer_auto.py``), so the coordinator owns one long-lived detector and the
number entities can retune it without a rebuild.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# --------------------------------------------------------------------------- #
# Tuning constants (module-level; each carries the reasoning behind its value).
# --------------------------------------------------------------------------- #

# Baseline EMA time constant. Only governs tracking of *non-cooking* drift —
# during a real cook the baseline is frozen, not merely slow — so it is chosen
# small enough that a plausible slow regime shift (the ~+0.5 index/min ramp in
# the regression suite) is absorbed rather than mistaken for cooking: a
# first-order tracker lags a linear ramp by slope*TAU, so at TAU=300 s that lag
# is ~2.5 index (z well under the ignore threshold). Over the 3-8 s onset
# window this same TAU moves mu by <1 %, so it never blunts a fast spike.
# (Spec §2 suggested 1800 s; that lagged the +60/2 h ramp by ~15 index — z~7.5,
# a false trigger — see module note. Lowered to 300 s.)
TAU_S = 300.0

# Adaptation rate multiplier while a sample sits above Z_GATE but detection has
# not (yet) confirmed. Ten-times-slower tracking keeps a sub-threshold
# contaminant or the pre-confirmation ramp from being absorbed into mu, yet a
# genuine sustained regime shift still heals in well under an hour.
RELUCTANT = 0.1

# Residual-z above which a sample is treated as "not baseline" — gates both the
# reluctant adaptation rate and the stale-baseline restore check.
Z_GATE = 3.0

# Fusion shape. A sensor at or below Z_IGNORE contributes nothing; one at
# Z_STRONG contributes 1.0 (= the default S_on), and the contribution is capped
# at CONTRIB_CAP so a single wild sensor can't dominate a multi-sensor fusion.
# Z_IGNORE=2.0 sits above the measured ~1.5-sigma VOC baseline drift (so drift
# stays rejected) but low enough that moderate humidity/PM spikes still count;
# Z_STRONG=4.5 lets one VOC onset sample trigger alone while two moderate
# sensors (z~3-4) only trigger together. (Spec §2 suggested 3/8; that zeroed a
# z=3 PM spike and made the "two moderate sensors" requirement unreachable —
# see module note. Lowered to 2.0/4.5.)
Z_IGNORE = 2.0
Z_STRONG = 4.5
Z_CAP = 20.0
CONTRIB_CAP = 2.0

# Score numerator: S_on = ON_SCORE_NUMERATOR / sensitivity, so the default
# sensitivity of 5 yields S_on = 1.0 (one Z_STRONG sensor). Higher sensitivity
# lowers the bar; lower sensitivity raises it.
ON_SCORE_NUMERATOR = 5.0
# Off threshold as a fraction of S_on — hysteresis so a signal hovering near
# the on threshold can't chatter the binary sensor.
S_OFF_RATIO = 0.3

# How long the fused score must hold >= S_on before turning ON. Two samples at
# the ~2 s sensor cadence; rejects single-sample glitches while keeping onset
# latency to ~6-8 s from the first elevated sample.
ON_PERSIST_S = 3.0
# Minimum time a detection stays ON once started — anti-flap on the binary
# sensor even if the signal collapses instantly.
MIN_ON_S = 60.0
# Baseline stays frozen this long after a detection ends: residual cooking
# odors linger and must not be baked into mu.
FREEZE_TAIL_S = 300.0

# Warm-up: no evidence until the baseline has both this many samples and this
# much wall-clock behind it. Covers cold start and the reboot re-baseline.
WARMUP_SAMPLES = 15
WARMUP_S = 120.0

# A sensor older than STALE_S contributes no evidence (it may be dead); a gap
# longer than GAP_RESET_S forces a full warm-up (we can't trust continuity).
STALE_S = 30.0
GAP_RESET_S = 300.0

# Slope detector: look back up to SLOPE_WINDOW_S, but only score a slope once
# the oldest kept sample is at least SLOPE_MIN_SPAN_S old (so a single fresh
# sample can't manufacture an infinite rate on the irregular ~2 s cadence).
SLOPE_WINDOW_S = 12.0
SLOPE_MIN_SPAN_S = 4.0

# Stuck-high escape hatch: after MAX_ON_S force OFF and, for REBASE_S, suppress
# evidence while adapting ungated at TAU_FAST_S so mu snaps to the (possibly
# still-elevated) level and a genuinely new cook can re-trigger via its slope.
# TAU_FAST_S is chosen so REBASE_S spans several time constants (~6) and mu
# converges to within ~1 unit of the current level: at the spec's 300 s the
# 600 s window is only ~2 tau, leaving a ~14 % residual (tens of sigma at the
# noise floor) that would instantly re-trigger instead of escaping the
# stuck-high state. (Spec §2 suggested 300 s — see module note. Lowered to 100.)
MAX_ON_S = 90.0 * 60.0
REBASE_S = 600.0
TAU_FAST_S = 100.0

# dt is clamped before it feeds the time-aware EMA: sub-100 ms deltas would
# make alpha ~0 (no adaptation), and a multi-minute delta that slipped past the
# gap guard shouldn't jump mu in one step.
DT_MIN = 0.1
DT_MAX = 60.0

# Restored baselines older than this are untrustworthy (the room and sensors
# have moved on) and are dropped in favour of a fresh warm-up.
RESTORE_MAX_AGE_S = 24.0 * 3600.0

# Fallback sigma floor for a sensor we were never told about (the HA glue
# classifies real floors from device_class/unit and calls set_sigma_floor).
DEFAULT_SIGMA_FLOOR = 2.0

# Guards max(dev, floor) against a zero/degenerate floor so z never divides by
# zero even if a caller passes floor 0.
_EPS = 1e-9


@dataclass(frozen=True)
class CookingParams:
    """Per-call tuning; mutated by the number entities, not the detector."""

    sensitivity: float = 5.0  # 1 (least sensitive) .. 10 (most)
    off_delay_min: float = 4.0


@dataclass(frozen=True)
class SensorSpec:
    """Static per-sensor configuration decided by the HA glue."""

    sigma_floor: float  # unit-dependent noise floor (min usable sigma)


@dataclass(frozen=True)
class CookingResult:
    """Outcome of a single :meth:`CookingDetector.update`/:meth:`tick`.

    ``transition`` reports the *edge*: ``True`` when a detection just started,
    ``False`` when one just ended, ``None`` when nothing changed this call.
    """

    transition: bool | None
    active: bool
    score: float


@dataclass
class _SensorState:
    """Mutable online state for one source sensor."""

    sigma_floor: float
    mu: float | None = None
    dev: float = 0.0
    n: int = 0
    last_t: datetime | None = None
    warm_start: datetime | None = None
    window: deque[tuple[datetime, float]] = field(default_factory=deque)
    last_value: float | None = None
    last_z: float = 0.0
    last_evidence: float = 0.0
    needs_rewarm: bool = False
    restored_unverified: bool = False
    rebase_until: datetime | None = None
    available: bool = False


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp ``value`` into ``[low, high]``."""
    return max(low, min(high, value))


class CookingDetector:
    """Learns per-sensor baselines and fuses them into a cooking detection.

    The coordinator owns exactly one instance, feeds it every source-sensor
    state change via :meth:`update`, and ticks it (:meth:`tick`) on a heartbeat
    while a detection is active so an all-silent sensor set can still time out.
    All math is pure and ``now``-driven; nothing here reads the wall clock.
    """

    def __init__(self, specs: dict[str, SensorSpec]) -> None:
        self._sensors: dict[str, _SensorState] = {
            eid: _SensorState(sigma_floor=spec.sigma_floor) for eid, spec in specs.items()
        }
        self._active = False
        self._score = 0.0
        self._on_since: datetime | None = None
        self._arm_since: datetime | None = None
        self._off_since: datetime | None = None
        self._freeze_until: datetime | None = None

    # -- properties --------------------------------------------------------- #

    @property
    def active(self) -> bool:
        """Whether a cooking detection is currently held ON."""
        return self._active

    @property
    def score(self) -> float:
        """The most recently computed fused score."""
        return self._score

    # -- configuration ------------------------------------------------------ #

    def set_sigma_floor(self, entity_id: str, floor: float) -> None:
        """Set (or refine) a sensor's noise floor; creates the sensor if new."""
        self._state(entity_id).sigma_floor = floor

    def _state(self, entity_id: str) -> _SensorState:
        """Return the sensor's state, creating it with the fallback floor."""
        st = self._sensors.get(entity_id)
        if st is None:
            st = _SensorState(sigma_floor=DEFAULT_SIGMA_FLOOR)
            self._sensors[entity_id] = st
        return st

    # -- ingestion ---------------------------------------------------------- #

    def update(
        self,
        now: datetime,
        entity_id: str,
        value: float | None,
        params: CookingParams,
    ) -> CookingResult:
        """Feed one sample (or ``None`` for unavailable) and re-evaluate.

        ``value=None`` marks the sensor unavailable: it stops contributing
        evidence and is flagged to re-warm-up on its next real sample (the
        reboot guard). Returns the fusion/state-machine outcome at ``now``.
        """
        st = self._state(entity_id)
        if value is None:
            # Unavailable: drop the slope window, zero evidence, and require a
            # fresh warm-up when it returns (an unavailable episode is exactly
            # how a sensor reboot presents to HA).
            st.available = False
            st.needs_rewarm = True
            st.window.clear()
            st.last_value = None
            st.last_evidence = 0.0
            return self._evaluate(now, params)

        # Freeze reflects the detection state *as of this sample*, so a sample
        # arriving mid-detection can't nudge the baseline it's being scored on.
        frozen = self._frozen(now)
        self._ingest(st, now, float(value), frozen)
        return self._evaluate(now, params)

    def tick(self, now: datetime, params: CookingParams) -> CookingResult:
        """Advance the state machine without a new sample.

        Staleness is re-evaluated at ``now``, so if every sensor has gone
        silent during an active detection the fused score decays to 0 and the
        normal off-delay path ends the detection.
        """
        return self._evaluate(now, params)

    def _frozen(self, now: datetime) -> bool:
        """Whether baselines are held (active detection or its freeze tail)."""
        if self._active:
            return True
        return self._freeze_until is not None and now < self._freeze_until

    def _warmup_reset(self, st: _SensorState, now: datetime, x: float) -> None:
        """Rebuild a sensor's baseline from a single sample (no evidence yet)."""
        st.mu = x
        st.dev = st.sigma_floor
        st.n = 1
        st.warm_start = now
        st.last_t = now
        st.window.clear()
        st.window.append((now, x))
        st.needs_rewarm = False
        st.restored_unverified = False
        st.rebase_until = None
        st.last_value = x
        st.last_z = 0.0
        st.last_evidence = 0.0
        st.available = True

    def _ingest(self, st: _SensorState, now: datetime, x: float, frozen: bool) -> None:
        """Fold one numeric sample into a sensor's online baseline + evidence."""
        # Cold start or a forced re-warm-up (post-unavailable): start over.
        if st.mu is None or st.needs_rewarm:
            self._warmup_reset(st, now, x)
            return

        if st.restored_unverified:
            # A baseline restored from disk is trusted only until the first live
            # sample confirms it: a big positive jump means the sensor moved on
            # while HA was down (e.g. rebooted), so discard and warm up fresh.
            st.restored_unverified = False
            sigma = max(st.dev, st.sigma_floor, _EPS)
            if (x - st.mu) / sigma > Z_GATE:
                self._warmup_reset(st, now, x)
                return
            # Consistent: accept the baseline and skip warm-up entirely so a
            # real cook right after a restart can trigger without a 120 s wait.
            st.warm_start = now - timedelta(seconds=WARMUP_S)
            st.n = max(st.n, WARMUP_SAMPLES)
            st.last_t = now
            st.window.clear()
        elif st.last_t is not None and (now - st.last_t).total_seconds() > GAP_RESET_S:
            # Continuity broke without an explicit unavailable — can't trust the
            # old baseline across the gap.
            self._warmup_reset(st, now, x)
            return

        dt = _clamp((now - st.last_t).total_seconds(), DT_MIN, DT_MAX)
        r = x - st.mu
        sigma = max(st.dev, st.sigma_floor, _EPS)
        z = max(0.0, r) / sigma

        # Slope-z catches a fast onset before the level has fully separated: the
        # window is time-based (robust to the irregular cadence) and only scores
        # once it spans enough real time.
        st.window.append((now, x))
        while len(st.window) > 1 and (now - st.window[0][0]).total_seconds() > SLOPE_WINDOW_S:
            st.window.popleft()
        t_old, x_old = st.window[0]
        if (now - t_old).total_seconds() >= SLOPE_MIN_SPAN_S:
            z_slope = max(0.0, x - x_old) / sigma
        else:
            z_slope = 0.0

        warming = st.n < WARMUP_SAMPLES or (
            st.warm_start is not None and (now - st.warm_start).total_seconds() < WARMUP_S
        )
        in_rebase = st.rebase_until is not None and now < st.rebase_until

        st.last_value = x
        st.last_z = z
        # No evidence while re-learning a baseline (warm-up) or deliberately
        # snapping to a stuck-high level (rebase).
        st.last_evidence = 0.0 if (warming or in_rebase) else min(max(z, z_slope), Z_CAP)

        # Time-aware EMA. Rebase adapts ungated and fast to escape a stuck-high
        # baseline; otherwise adaptation is frozen during detection, reluctant
        # above the gate, and full-rate on quiet samples. Warm-up guarantees a
        # 1/n floor so a fresh baseline actually converges.
        if in_rebase:
            alpha = 1.0 - math.exp(-dt / TAU_FAST_S)
        else:
            rate = 0.0 if frozen else (RELUCTANT if z > Z_GATE else 1.0)
            alpha = rate * (1.0 - math.exp(-dt / TAU_S))
            if warming and not frozen:
                alpha = max(alpha, 1.0 / (st.n + 1))
        st.mu += alpha * r
        st.dev += alpha * (abs(r) - st.dev)
        st.n += 1
        st.last_t = now
        st.available = True

    # -- fusion + state machine -------------------------------------------- #

    def _evaluate(self, now: datetime, params: CookingParams) -> CookingResult:
        """Fuse current per-sensor evidence and step the on/off state machine."""
        sensitivity = max(params.sensitivity, _EPS)
        s_on = ON_SCORE_NUMERATOR / sensitivity
        s_off = S_OFF_RATIO * s_on

        score = 0.0
        for st in self._sensors.values():
            # A sensor contributes only while available and fresh; a stale or
            # unavailable sensor is worth exactly zero (this is what lets an
            # all-silent set decay the score to 0 on a heartbeat tick).
            if (
                st.available
                and st.last_t is not None
                and (now - st.last_t).total_seconds() <= STALE_S
            ):
                evidence = st.last_evidence
            else:
                evidence = 0.0
            contribution = _clamp((evidence - Z_IGNORE) / (Z_STRONG - Z_IGNORE), 0.0, CONTRIB_CAP)
            score += contribution
        self._score = score

        transition: bool | None = None

        if not self._active:
            # OFF -> ON: score must hold >= S_on continuously; any dip re-arms.
            if score >= s_on:
                if self._arm_since is None:
                    self._arm_since = now
                elif (now - self._arm_since).total_seconds() >= ON_PERSIST_S:
                    self._active = True
                    self._on_since = now
                    self._arm_since = None
                    self._off_since = None
                    transition = True
            else:
                self._arm_since = None
        else:
            on_elapsed = (now - self._on_since).total_seconds() if self._on_since else 0.0
            if on_elapsed >= MAX_ON_S:
                # Stuck-high safety valve: force OFF and rebase every sensor so a
                # slow-clearing signal can't pin the detection forever.
                self._active = False
                self._on_since = None
                self._off_since = None
                self._arm_since = None
                for st in self._sensors.values():
                    st.rebase_until = now + timedelta(seconds=REBASE_S)
                transition = False
            elif score < s_off:
                # ON -> OFF: below the off threshold long enough, but never
                # before the minimum on-time (anti-flap).
                if self._off_since is None:
                    self._off_since = now
                elif (
                    now - self._off_since
                ).total_seconds() >= params.off_delay_min * 60.0 and on_elapsed >= MIN_ON_S:
                    self._active = False
                    self._on_since = None
                    self._off_since = None
                    self._freeze_until = now + timedelta(seconds=FREEZE_TAIL_S)
                    transition = False
            else:
                self._off_since = None

        return CookingResult(transition=transition, active=self._active, score=score)

    # -- persistence + diagnostics ----------------------------------------- #

    def snapshot(self, now: datetime) -> dict:
        """Return a JSON-serializable view of every learned baseline.

        ``saved_at`` is a POSIX timestamp so :meth:`restore` can age entries out
        without a timezone round-trip.
        """
        sensors: dict[str, dict] = {}
        for eid, st in self._sensors.items():
            if st.mu is None:
                continue
            sensors[eid] = {
                "mu": st.mu,
                "dev": st.dev,
                "n": st.n,
                "saved_at": now.timestamp(),
            }
        return {"sensors": sensors}

    def restore(self, stored: dict, now: datetime) -> None:
        """Reload baselines saved by :meth:`snapshot`.

        Entries older than 24 h are dropped (the room has moved on). Accepted
        entries are marked *unverified*: the first live sample either confirms
        them (small z -> skip warm-up) or exposes them as stale (z > gate ->
        fall back to a full warm-up).
        """
        if not stored:
            return
        for eid, data in (stored.get("sensors") or {}).items():
            saved_at = data.get("saved_at")
            if saved_at is None or now.timestamp() - saved_at > RESTORE_MAX_AGE_S:
                continue
            st = self._state(eid)
            st.mu = float(data["mu"])
            st.dev = float(data["dev"])
            st.n = int(data.get("n", WARMUP_SAMPLES))
            st.restored_unverified = True
            st.needs_rewarm = False
            st.last_t = None
            st.warm_start = now - timedelta(seconds=WARMUP_S)
            st.window.clear()
            st.rebase_until = None
            st.available = True
            st.last_value = None
            st.last_z = 0.0
            st.last_evidence = 0.0

    def diagnostics(self, now: datetime) -> dict[str, dict]:
        """Per-sensor snapshot for the binary sensor's attributes."""
        out: dict[str, dict] = {}
        for eid, st in self._sensors.items():
            sigma = max(st.dev, st.sigma_floor, _EPS)
            if not st.available:
                status = "unavailable"
            elif st.mu is None:
                status = "warming"
            elif st.n < WARMUP_SAMPLES or (
                st.warm_start is not None and (now - st.warm_start).total_seconds() < WARMUP_S
            ):
                status = "warming"
            elif st.last_t is not None and (now - st.last_t).total_seconds() > STALE_S:
                status = "stale"
            else:
                status = "ok"
            out[eid] = {
                "value": st.last_value,
                "baseline": round(st.mu, 3) if st.mu is not None else None,
                "sigma": round(sigma, 3),
                "z": round(st.last_z, 3),
                "evidence": round(st.last_evidence, 3),
                "status": status,
            }
        return out
