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
- **Unit id 0** (`modbus.UNIT_ID_DEFAULT`, passed at borrow time). The unit
  also answers on id 1, but 0 is the documented one. tmodbus verifies the
  echoed unit id and raises `ModbusDesyncError` on a mismatch (dropping the
  link) instead of silently skipping the frame the way pymodbus did — which is
  what used to turn the cross-talk below into an unexplained timeout.
- Values are int16; spec Min/MaxLimit are engineering units (already scaled).
  `Kerroin` 10 → scale 0.1, 100 → scale 0.01. Absent optional sensors read
  **-1** (`absent_sentinel`; `decode()` maps it to `None`).
- **We do not own the connection.** The unit is borrowed from the core
  `modbus` integration via `async_get_unit` (entry-bound) or
  `async_get_temporary_unit` (config flow), which shares and refcounts one
  connection per endpoint. Two invariants follow, and breaking either is a
  bug: **never** close or disconnect the shared connection (`client.close()`
  only gives up our hold; `unit.disconnect()` is allowed *only* as a
  retry-time measure), and **never** reload the entry from
  `on_connection_lost` — recovery is the next transaction's warm-up.
- The Multi24 is slow and cannot pipeline: spacing is delegated to
  `unit.set_message_spacing(INTER_TRANSACTION_DELAY)`, which the library
  enforces bus-wide by holding a connection lock across each request; our own
  `asyncio.Lock` remains, and is only about keeping *our* multi-step sequences
  (warm-up + request + retry) atomic against a concurrent verify-read.
- Reads/writes retry 3× (`RETRY_BACKOFF`). The warm-up read of
  `WARM_UP_ADDRESS` is **lazy**, gated on `unit.connected` — that also catches
  drops we did not cause (a desync the backend handled, another holder's
  teardown), which a "we saw a drop" flag would miss. A failed attempt
  discards the link (the last one included, so a caller that gives up doesn't
  hand the next one a link we know is broken); an error *response* does not,
  since the controller demonstrably answered — see
  `ParmairConnectionError.device_answered`.
- Each request is bounded by `REQUEST_TIMEOUT` because core fixes the shared
  connection's own timeout at 10 s, longer than a whole poll cycle, and
  consumers cannot override it.
- ⚠ **Two simultaneous Modbus TCP clients corrupt each other** on this
  controller (responses leak across sockets, observed live). Sharing one
  connection is now the *fix* for consumers that borrow through `modbus`
  (which is why the options-flow redetect borrows instead of dialling its
  own), but it says nothing about a poller outside HA, or a core `modbus:`
  YAML hub — never test against the real unit while one of those is attached.
- Gap-spanning block reads work (verified live up to 65 registers spanning
  undefined registers). If a future unit faults on them, set
  `registers.MAX_GAP = 0`.

## Architecture

| Module | Purity | Role |
|---|---|---|
| `registers.py` | pure | v1.87 register map, `build_read_plan()` block coalescing, `encode`/`decode` |
| `capabilities.py` | pure | parse config registers 240–245 + probe sentinels → `Capabilities`; gates entities & read plan |
| `summer_auto.py` | pure | dwell state machine for the summer-mode automation |
| `cooking_detect.py` | pure | online per-sensor baselines (EMA + EW abs-residual, one-sided z, slope-z) fused into a hysteretic cooking detection; baselines freeze while active |
| `modbus.py` | modbus_connection only | `ParmairModbusClient`: thin adapter over a *borrowed* `ModbusUnit` — owns warm-up, retries, spacing, per-request timeout and error translation, but no socket. `create_client(unit)` factory is what tests patch |
| `coordinator.py` | HA | `ParmairCoordinator`: block reads, static-once reads, partial-failure tolerance, write + optimistic update + delayed verify-read, repairs, summer-auto evaluation |
| `config_flow.py` | HA | user → probe → confirm; options (scan interval, CO₂ offset, summer-auto source, re-detect). Borrows a temporary unit for both probe paths |
| platforms | HA | thin description-driven entity files over `entity.ParmairEntity` |
| `frontend/parmair-card.js` | JS | self-contained card + editor; registered from `__init__.py` (static path + Lovelace resource registry, best-effort). Renders in **shadow DOM** with the stylesheet injected once — a light-DOM `<style>` re-injected per render leaked rules to sibling cards and stalled dashboard paint (v0.2.1 fix); responsive stacking via ResizeObserver, NOT `@container` |

Rules:

- **Calculation/protocol logic goes in the pure modules first**, then gets
  wired into the coordinator. Never put decoding/planning logic in entities.
- Pure modules must stay importable without `homeassistant` installed;
  `modbus.py` may import only `modbus_connection`/stdlib.
  `homeassistant.components.modbus` (`async_get_unit` /
  `async_get_temporary_unit`) is imported **only** by `__init__.py` and
  `config_flow.py` — that is the whole HA-facing surface of the transport.
- Coordinator data is `dict[register_key, engineering value | None]`;
  entities read via `ParmairEntity.register_value` and MUST NOT talk to the
  client directly. Writes go through `coordinator.async_write` /
  `async_write_sequence` (they encode, optimistically update, and schedule
  the verify-read from `const.VERIFY_KEY`).
- Capability gating: an entity/register with `RegisterDef.capability` is
  created/polled only when `Capabilities.supports()` says so. When adding a
  register, decide its gate; probe-based gates read the sentinel at config
  time.
- Cooking detection is **event-driven**, not polled: the coordinator feeds
  external sensor state-change events into one long-lived `CookingDetector`
  and notifies its entities via the `SIGNAL_COOKING_UPDATE` dispatcher —
  never `async_set_updated_data` (cooking state lives on coordinator
  attributes, `coordinator.data` stays register-values only). Learned
  baselines persist in `Store` (`.storage/parmair_cooking_<entry_id>`).
  Auto-boost claims ownership only for boosts it started (write succeeds →
  claim; poll shows boost gone → drop) so manual/CO₂ boosts are respected.

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
uv venv --python 3.14 .venv314          # HA requires >=3.14.2 as of 2026.8
uv pip install --python .venv314/bin/python -r requirements_test.txt
.venv314/bin/python -m pytest -q                        # full suite
.venv314/bin/python -m pytest tests/test_registers.py  # pure only, no HA needed
.venv314/bin/python -m pytest --cov=custom_components/parmair --cov-fail-under=90
.venv314/bin/ruff check . && .venv314/bin/ruff format .
```

`requirements_test.txt` mirrors core's `homeassistant/components/modbus/manifest.json`
pins (`pymodbus`, `modbus-connection[tmodbus]`, `tmodbus`): tests run with
`skip_pip`, so nothing installs them for us even though `dependencies:
["modbus"]` makes HA import that component. Bump them in lockstep with core.

## Testing conventions

- **Pure vs HA split**: `tests/test_*.py` are pure (plain pytest; modules
  loaded under their dotted names via importlib — see `tests/conftest.py`;
  root conftest must NOT import HA or load the HA plugin).
  `tests/ha/test_*.py` use `pytest-homeassistant-custom-component`; the
  `auto_enable_custom_integrations` autouse fixture lives in
  `tests/ha/conftest.py`.
- HA tests never touch a real Modbus stack: they patch
  `custom_components.parmair.modbus.create_client` to return the
  `FakeModbusClient` from `tests/ha/conftest.py`, seeded with
  `rexo120_bank` — **register values captured from the real device**. Keep
  that fixture realistic; when live behavior surprises you, encode the truth
  there and regression-test it. The autouse `mock_modbus_borrow` fixture
  additionally stubs `async_get_unit` / `async_get_temporary_unit`, keeping the
  suite off core's connection registry; the few tests that want the real borrow
  nest their own `patch` over it.
- `tests/test_modbus.py` is pure but drives `modbus_connection`'s own
  `MockModbusUnit` (a third-party package, like pymodbus was — the pure/HA
  split is about `homeassistant`, not about dependencies). Prefer it over a
  hand-rolled double: the fidelity is then the library authors'.
- After `async_fire_time_changed`, call `hass.async_block_till_done()`
  before AND after. Coordinator-driven cycles in entity-less tests need
  direct `coordinator.async_refresh()` (the polling timer only runs with
  listeners).
- Coverage gate: 90 % (CI enforces).

## Releasing a version

**Minimum Home Assistant: 2026.9** (the shared-Modbus helper landed there),
declared in `hacs.json`. Bumping it is a breaking change for HACS users on
older cores — HACS withholds the update rather than breaking them.

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
- Whether the library's 0.3 s `message_spacing` measures end-to-start or
  start-to-start (affects the real poll-cycle duration, not correctness).
- Whether `REQUEST_TIMEOUT = 5` is the right cap now that the transport no
  longer sets its own connect timeout.
- Whether a core `modbus:` YAML hub on the *same* host:port coexists on the
  shared connection. If it does, README's "remove any other Modbus poller"
  warning can be softened for that case.

## Shared Modbus connection (shipped in v0.5.0)

Since v0.5.0 the integration borrows its Modbus unit from the core `modbus`
integration rather than owning a pymodbus client — see the *Modbus contract*
section above for the invariants that come with it, and
<https://developers.home-assistant.io/docs/modbus/introduction/> for the
upstream contract. `pymodbus` is gone from our `requirements`; it arrives
transitively via `dependencies: ["modbus"]`.

Not done, and deliberately so: extracting `registers.py`/`capabilities.py` into
a standalone `parmair-modbus` PyPI package (the `trovis-modbus` pattern the dev
blog pitches). Phase 2 of
`~/.claude/plans/there-is-this-blog-compiled-spindle.md` sketches it. The
blocker for a *core* integration is that core cannot ship a Lovelace card, so
`frontend/parmair-card.js` would have to be split into its own HACS frontend
repo — weigh that before starting.
