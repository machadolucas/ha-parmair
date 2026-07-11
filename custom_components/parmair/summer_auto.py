"""Dwell-based summer-mode auto-toggle (pure module — stdlib only).

Summer mode (reg 79) shouldn't flip on every reading that crosses a
threshold — a single warm gust or the sensor's own noise would toggle it back
and forth. ``SummerAutoLogic`` instead requires the temperature to stay
continuously on the "wrong" side of a threshold for a configured dwell time
before requesting a change, and stays silent (returns ``None``) otherwise.
The coordinator owns one long-lived instance and calls :meth:`update` once
per poll tick with the current sensor reading and the unit's actual
``summer_mode`` state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class SummerAutoParams:
    """Thresholds and dwell times gating the auto on/off transition."""

    on_temp_c: float
    on_dwell_min: float
    off_temp_c: float
    off_dwell_min: float


class SummerAutoLogic:
    """Tracks continuous dwell above/below the on/off thresholds.

    Holds two timer marks (``_above_since``/``_below_since``) between calls
    so the caller doesn't need to keep its own state. Never requests a change
    while the unit already reports the target ``summer_on`` state, and treats
    an inverted/degenerate threshold pair (``on_temp_c <= off_temp_c``) as a
    misconfiguration that must never fire, rather than an oscillation trap.
    """

    def __init__(self) -> None:
        self._above_since: datetime | None = None
        self._below_since: datetime | None = None

    def reset(self) -> None:
        """Clear both dwell timers (e.g. after a manual mode override)."""
        self._above_since = None
        self._below_since = None

    def update(
        self,
        now: datetime,
        temp: float | None,
        summer_on: bool,
        params: SummerAutoParams,
    ) -> bool | None:
        """Advance the dwell timers for one tick; return a requested change, if any.

        Returns ``True`` to turn summer mode on, ``False`` to turn it off, or
        ``None`` to take no action this tick.
        """
        if temp is None:
            # Missing reading: don't let a stale timer fire once the sensor
            # comes back, and don't guess at intent while it's gone.
            self.reset()
            return None

        if params.on_temp_c <= params.off_temp_c:
            # A dead band requires on_temp > off_temp; an inverted or equal
            # pair would make the two timers fight every tick.
            self.reset()
            return None

        if temp >= params.on_temp_c:
            self._below_since = None
            if summer_on:
                # Already at the target state — nothing left to time.
                self._above_since = None
                return None
            if self._above_since is None:
                self._above_since = now
                return None
            if now - self._above_since >= timedelta(minutes=params.on_dwell_min):
                self._above_since = None
                return True
            return None

        if temp <= params.off_temp_c:
            self._above_since = None
            if not summer_on:
                self._below_since = None
                return None
            if self._below_since is None:
                self._below_since = now
                return None
            if now - self._below_since >= timedelta(minutes=params.off_dwell_min):
                self._below_since = None
                return False
            return None

        # Inside the dead band: neither direction is being timed.
        self._above_since = None
        self._below_since = None
        return None
