"""Shared pytest configuration.

Puts the repo root on ``sys.path`` so ``custom_components.parmair`` is
importable (needed for the relative import inside capabilities.py — ``from
.registers import ...`` — to resolve when that module is loaded standalone
below). No Home Assistant plugin registered here — these are the pure tests;
the HA-side test plugin is registered in ``tests/ha/conftest.py`` once that
suite exists (later phase).
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
