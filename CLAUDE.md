# CLAUDE.md — Parmair MAC integration

## What this is

Home Assistant custom integration (domain `parmair`) for Parmair MAC
ventilation/heat-recovery units over Modbus TCP, plus a bundled Lovelace card
(`frontend/parmair-card.js`). Developed against a Parmair REXO 120 MAC
(Multi24 controller, register map spec "Modbus Parmair" V1.87). Distributed
via HACS from `machadolucas/ha-parmair`.

## Modbus contract (read before touching modbus.py / registers.py)

- **On-wire address = spec register number + 1000.** `RegisterDef.register`
  stores the spec number (1–245); `RegisterMap.address()` adds the offset.
  Example: IV01_CONTROLSTATE_FO is register 185 → address 1185.
- **Unit id 0** (`device_id=0` in pymodbus ≥ 3.11). The unit also answers on
  id 1, but 0 is the documented one.
- Values are int16; spec Min/MaxLimit are engineering units (already scaled).
  `Kerroin` 10 → scale 0.1, 100 → scale 0.01. Absent optional sensors read
  **-1** (`absent_sentinel`; `decode()` maps it to `None`).
- The Multi24 is slow and cannot pipeline: one `asyncio.Lock` serializes all
  transactions, spaced ≥ 0.3 s (`INTER_TRANSACTION_DELAY`); reads retry 3×
  with reconnect + warm-up (the first reply after a fresh TCP connect is
  flaky).
- ⚠ **Two simultaneous Modbus TCP clients corrupt each other** on this
  controller (responses leak across sockets, observed live). Never test
  against the real unit while another poller is attached.
- Gap-spanning block reads work (verified live up to 65 registers spanning
  undefined registers). If a future unit faults on them, set
  `registers.MAX_GAP = 0`.

## Architecture

| Module | Purity | Role |
|---|---|---|
| `registers.py` | pure | v1.87 register map, `build_read_plan()` block coalescing, `encode`/`decode` |
| `capabilities.py` | pure | parse config registers 240–245 + probe sentinels → `Capabilities`; gates entities & read plan |
| `summer_auto.py` | pure | dwell state machine for the summer-mode automation |
| `modbus.py` | pymodbus only | `ParmairModbusClient` (persistent async client, lock, pacing, retries); `create_client()` factory is what tests patch |
| `coordinator.py` | HA | `ParmairCoordinator`: block reads, static-once reads, partial-failure tolerance, write + optimistic update + delayed verify-read, repairs, summer-auto evaluation |
| `config_flow.py` | HA | user → probe → confirm; options (scan interval, CO₂ offset, summer-auto source, re-detect) |
| platforms | HA | thin description-driven entity files over `entity.ParmairEntity` |
| `frontend/parmair-card.js` | JS | self-contained card + editor; registered from `__init__.py` (static path + Lovelace resource registry, best-effort) |

Rules:

- **Calculation/protocol logic goes in the pure modules first**, then gets
  wired into the coordinator. Never put decoding/planning logic in entities.
- Pure modules must stay importable without `homeassistant` installed;
  `modbus.py` may import only pymodbus/stdlib.
- Coordinator data is `dict[register_key, engineering value | None]`;
  entities read via `ParmairEntity.register_value` and MUST NOT talk to the
  client directly. Writes go through `coordinator.async_write` /
  `async_write_sequence` (they encode, optimistically update, and schedule
  the verify-read from `const.VERIFY_KEY`).
- Capability gating: an entity/register with `RegisterDef.capability` is
  created/polled only when `Capabilities.supports()` says so. When adding a
  register, decide its gate; probe-based gates read the sentinel at config
  time.

## Write semantics (verified on the real unit)

- Fan speed: write `speed_control` (1187): 0=AUTO, 1=stop, 2–6 = manual
  speed 1–5 (manual flips `control_state` to 9).
- Presets: write `speed_control 0` **then** `control_state` (1185)
  1=away/2=home/3=boost/4=fireplace.
- Power: write `power_state` (1208) 2=start, 1=stop (state 0/3 = off/on).
- Boost/fireplace switches turn off by restoring home (2) or away (1) based
  on `home_state`.
- `home_speed`/`away_speed` (1104/1105) store raw 0–4 = displayed speed 1–5;
  the number entities add/subtract 1.
- Filter button writes today's date into 1086–1088 then `filter_state = 1`.

## Dev workflow

```bash
uv venv --python 3.13 .venv313          # HA does not support 3.14
uv pip install --python .venv313/bin/python -r requirements_test.txt
.venv313/bin/python -m pytest -q                        # full suite
.venv313/bin/python -m pytest tests/test_registers.py  # pure only, no HA needed
.venv313/bin/python -m pytest --cov=custom_components/parmair --cov-fail-under=90
.venv313/bin/ruff check . && .venv313/bin/ruff format .
```

## Testing conventions

- **Pure vs HA split**: `tests/test_*.py` are pure (plain pytest; modules
  loaded under their dotted names via importlib — see `tests/conftest.py`;
  root conftest must NOT import HA or load the HA plugin).
  `tests/ha/test_*.py` use `pytest-homeassistant-custom-component`; the
  `auto_enable_custom_integrations` autouse fixture lives in
  `tests/ha/conftest.py`.
- HA tests never touch pymodbus: they patch
  `custom_components.parmair.modbus.create_client` to return the
  `FakeModbusClient` from `tests/ha/conftest.py`, seeded with
  `rexo120_bank` — **register values captured from the real device**. Keep
  that fixture realistic; when live behavior surprises you, encode the truth
  there and regression-test it.
- After `async_fire_time_changed`, call `hass.async_block_till_done()`
  before AND after. Coordinator-driven cycles in entity-less tests need
  direct `coordinator.async_refresh()` (the polling timer only runs with
  listeners).
- Coverage gate: 90 % (CI enforces).

## Releasing a version

HACS reads both `manifest.json` and GitHub releases; a release is 4 steps:

1. Bump `"version"` in `custom_components/parmair/manifest.json` (SemVer).
2. Commit to `main` (`Area: short description (vX.Y.Z)`).
3. `git push origin main`.
4. `gh release create vX.Y.Z --title "vX.Y.Z — description" --generate-notes`
   — the release creates the tag; local `git tag` lags, use
   `gh release list` to see the real latest.

## Conventions

- `strings.json` and `translations/en.json` **must stay byte-identical**; no
  URLs inside translation strings (hassfest rejects them).
- Entity naming: `_attr_has_entity_name = True`,
  `unique_id = f"{entry_id}_{key}"`, `translation_key = key`; names live in
  `strings.json`. The fan uses `_attr_name = None` (takes the device name).
- Comment the *why*, not the *what*; docstrings on modules/classes/public
  functions.
- Don't commit secrets. Diagnostics redact the host via `async_redact_data`.

## Branding / icon

`brands/icon.svg` is repo-local; the HACS validate action runs with
`ignore: brands`. TODO: submit to `home-assistant/brands` and drop the
ignore.

## Open on-device verifications

Interpretations still to confirm against the live unit when convenient (all
flagged in code comments where relevant):

- Whether writing `filter_state = 1` alone would make the unit stamp
  `FILTER_*`/`FILTERNEXT_*` itself (we stamp the change date ourselves and
  expect the unit to recompute the next-change date from the interval).
- `heater_type = 2` semantics (assumed "none"; spec names only 0=water,
  1=electric).
- Whether `control_state` writes alone exit manual speed mode (we always
  pre-write `speed_control = 0`, which is safe either way).
