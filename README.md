# Parmair MAC for Home Assistant

Local Home Assistant integration for **Parmair MAC** ventilation / heat-recovery
units (My Air Control, Multi24 controller) over **Modbus TCP** — with full
UI configuration, automatic feature detection, a proper `fan` entity, and a
bundled compact Lovelace card.

Developed and tested against a **Parmair REXO 120 MAC** (register map v1.87).
Not affiliated with or endorsed by Parmair Oy.

## Features

- **UI configuration** — enter host + port, the integration probes the unit,
  detects its features and shows what it found before creating anything.
- **Feature detection** — entities are only created for hardware your unit
  actually has: CO₂ sensor, wet-room humidity sensor, post-heater type
  (water/electric), M12 input usage. Absent sensors (which read `-1` over
  Modbus) never appear as dead entities.
- **A real `fan` entity** — speed 1–5 as percentage steps, plus preset modes
  *Home / Away / Boost / Fireplace*, and on/off. No more template fans and
  register-writing automations.
- **Two `climate` entities** for the supply and extract (summer) temperature
  setpoints, with live current temperature and heating/cooling action.
- **Switches** for boost, fireplace mode, summer mode, HRU temperature
  control and post-heating; **numbers** and **selects** for the useful
  settings (default speeds, boost duration, CO₂/humidity boost limits,
  defrost tuning, filter interval…).
- **Summer mode automation** (optional): turn summer mode on/off
  automatically when a temperature stays above/below your thresholds for a
  dwell time you choose. Uses the unit's own fresh-air sensor or any
  temperature entity you pick.
- **~75 entities** total; low-value diagnostics are disabled by default so
  they don't clutter your setup — enable the ones you want.
- **Repairs integration** — filter change due, active alarms and lost
  connection raise Home Assistant repair issues; alarms can be acknowledged
  with a button entity.
- **Diagnostics download** (host redacted) with the raw register snapshot,
  read plan and failure counters for easy bug reports.
- **Bundled Lovelace card** — a compact ventilation control card,
  configurable in the UI, auto-registered (no manual resource setup).

## Supported hardware

Parmair MAC family units with the Multi24 controller exposing the **v1.87
Modbus register map** (spec "Modbus Parmair" V1.87, registers 1–245). The
integration verifies the map during setup and refuses units it does not
recognize rather than creating garbage entities. The register-map layer is
pluggable, so other firmware families can be added — PRs welcome.

Connection facts the integration handles for you (useful to know when
debugging):

- On-wire holding-register address = spec register number + 1000
  (`IV01_CONTROLSTATE_FO`, register 185, lives at address 1185).
- Modbus **unit id 0**.
- The controller is slow: transactions are serialized, paced ≥ 0.3 s apart
  and retried with reconnects. Values are polled in a handful of bulk block
  reads (default every 10 s).
- ⚠ The Multi24's TCP stack misbehaves with **multiple simultaneous Modbus
  clients** (responses can leak between connections). Remove any other
  Modbus integration/poller for the unit before setting this one up.

## Installation

### HACS (custom repository)

1. HACS → ⋮ → *Custom repositories* → add
   `machadolucas/ha-parmair` with category **Integration**.
2. Install **Parmair MAC**, restart Home Assistant.
3. Settings → Devices & services → *Add integration* → **Parmair MAC**.

### Manual

Copy `custom_components/parmair/` into your Home Assistant `config/custom_components/`
directory and restart.

## Configuration

The config flow asks for:

| Field | Default | Notes |
|---|---|---|
| Host | — | IP/hostname of the unit |
| Port | 502 | Modbus TCP port |
| Name | Parmair | Device name |
| Scan interval | 10 s | Polling period (5–120 s) |

The integration then connects, validates the register map, detects features
and shows a confirmation screen (model, software version, detected sensors)
before creating the entry.

**Options** (Settings → Devices & services → Parmair MAC → *Configure*):

- **Scan interval** — polling period.
- **CO₂ offset** — calibration added to the raw CO₂ reading. Some CO₂
  transmitters report with a fixed bias; compare the panel reading with the
  `Carbon dioxide` sensor and set the difference here (can be negative).
- **Summer auto temperature source** — the temperature entity that drives
  the summer-mode automation. Empty = the unit's own fresh-air sensor.
- **Air-quality sensors for cooking detection** — external kitchen sensors
  (VOC index, humidity, PM…) that feed the cooking detector (see below).
  Empty = feature disabled.
- **Re-detect device features** — re-probe the unit after hardware changes
  (e.g. a CO₂ sensor was installed).

### Summer mode automation

The unit's built-in summer-mode logic is a simple outdoor-temperature limit.
This integration adds an optional dwell-based automation on top:

1. Turn on the **Summer mode automation** switch.
2. Set the four numbers: *on temperature* / *on dwell time* (summer mode
   turns ON after the source temperature stays ≥ threshold for the dwell)
   and *off temperature* / *off dwell time* (mirror image).

Manual toggles of the **Summer mode** switch are never blocked; the
automation re-asserts its decision only after the opposite dwell completes.

### Cooking detection

Point the integration at fast-updating kitchen air-quality sensors (an
ESPHome SEN55 works great) and it detects cooking within a few seconds of
the first fumes — no thresholds to tune per season:

1. Add the sensors in the integration options (e.g. a VOC index, a humidity
   sensor and **one** particulate sensor — PM1/2.5/4/10 move together, so
   adding several would double-count the same evidence).
2. The integration learns a per-sensor baseline and noise level online
   (persisted across restarts) and scores each sample as a deviation from
   baseline, fusing all sensors: one strong spike triggers alone, two
   moderate ones together. The baseline is frozen while cooking is detected
   so long cooks can't blend into it, and re-learned automatically after
   sensor reboots or outages.
3. `binary_sensor.*_cooking_detected` turns on/off with the detection (its
   attributes show per-sensor baselines and z-scores; the diagnostic
   `Cooking score` sensor is graphable for tuning).
4. Optional: turn on the **Cooking boost automation** switch and the
   integration itself raises the unit's Boost preset while cooking and
   restores Home/Away afterwards. It never steals a boost it didn't start:
   manual/CO₂ boosts are left alone, and a manual boost-off is respected.
5. Tune with three numbers: *sensitivity* (1–10), *off delay* and *minimum
   boost time*. With a single configured sensor, sensitivities 1–2 are
   effectively "never" — fusion needs more than one sensor to reach the bar.

If you'd rather keep boost decisions in your own automations (presence or
time-of-day gating, arbitration with other boost causes), leave the switch
off and trigger on the binary sensor instead.

## Entities

The most important ones (all names are translatable / renameable):

| Entity | What it is |
|---|---|
| `fan.<device>` | Main control: speed 1–5 (20 % steps), presets Home/Away/Boost/Fireplace, on/off |
| `climate.<device>_supply_temperature` | Supply air setpoint (15–25 °C) with current temperature |
| `climate.<device>_extract_temperature_summer` | Extract setpoint used in summer (18–26 °C) |
| `switch.*` boost / fireplace / summer mode | The three modes as simple switches (boost/fireplace show remaining minutes as sensors) |
| `sensor.*` temperatures | Fresh, supply (before/after HRU), extract, waste |
| `sensor.*` air quality | Humidity (HRU + 24 h average), CO₂ (if fitted), heat-recovery efficiency |
| `binary_sensor.*` | Defrosting, home/away, filter change required, alarm |
| `button.*` | Acknowledge alarms, mark filter changed (stamps today's date) |
| `number.*` / `select.*` | Default speeds when home/away, boost duration/speed, CO₂ & humidity boost limits, defrost tuning, filter interval… |

Diagnostic sensors (fan outputs, defrost internals, fault codes per sensor,
timers…) exist but many are **disabled by default** — enable them from the
device page if you need them.

## Lovelace card

The integration serves and registers `parmair-card` automatically. Add it
from the card picker ("Parmair Card") or via YAML:

```yaml
type: custom:parmair-card
# All optional:
device_id: <your parmair device>   # auto-detected when you have one unit
title: Ventilation
show_fan_row: true
show_airflow: true
show_metrics: true
show_alerts: true
compact: false
```

**Split-panel layout**: the outer card is chromeless (no background,
border, or shadow of its own), with a bare header above sub-cards that
render with your theme's regular card look — controls on the left,
airflow on the right — stacking to a single column on narrow cards
(≤440px).

- **Header** — title (click for the fan's more-info dialog), then compact
  live chips for fan power, humidity, and CO₂ (each only when its sensor
  exists), status chips (a labeled Summer chip with a small "ᴬ" marker
  when summer-auto is active; icon-only chips for HRU temp control,
  post-heating, and defrosting while active; and an always-visible
  home/away chip, dimmed when away), and a round "…" button that expands
  the settings section. Every chip opens its entity's more-info dialog on
  click. While the unit is off, the body panels dim and the
  speed/Boost/Fireplace controls are disabled.
- **Controls panel** — an AUTO pill + −/+ stepper for fan speed (1–5), and
  Boost mode / Fireplace mode buttons with a countdown and a draining
  progress bar (driven by each feature's `*_time_remaining` sensor against
  its `*_duration` select).
- **Airflow panel** (`show_airflow`) — an animated SVG cross-flow diagram
  with wide gradient flow channels (Fresh→Supply and Extract→Waste crossing
  through the heat-exchanger core). Endpoint and core temperatures sit on
  frosted-glass overlay labels, and each channel's blue↔amber gradient
  follows the *measured* temperatures — the colder end is blue, the warmer
  end amber, flipping automatically between winter and summer. The dash
  animation speed tracks the current fan speed (faster at higher speeds,
  frozen when the unit is off). `show_metrics` adds the labeled "Heat
  recovery" row below the diagram.
- **Settings section** (the "…" button) — expands below the panels: a
  full-width Power row (turning off needs a second tap on a red
  "Turn off?" pill within 4 s; turning on is a single tap and stays
  available while everything else is dimmed), then a two-column grid of
  paired rows: Speed when home / away (1–5), Supply target (15–25 °C) and
  Extract target (18–26 °C) in 0.5° steps, Defrost min efficiency
  (0–100 %), plus On/Off toggles for HRU temp control, Summer mode, and
  Post-heating. Rows appear only when their entity exists, and each row's
  label opens its entity's more-info dialog.
- **Alert banner** — amber "Filter change due" / red "Active alarm — tap to
  acknowledge", hidden when clear.
- **`compact: true`** shows only the header and controls panel (the
  settings section stays reachable via the "…" button).

It follows entity renames automatically (it resolves entities via the
registry, not by id). Older configs using `show_temperatures`/`show_chips`
still work as aliases for `show_airflow`/`show_metrics`.

## Migrating from the built-in `modbus:` integration

If you previously integrated the unit with HA's built-in Modbus YAML:

1. **Remove the `modbus:` YAML** (or at least this unit's hub) and restart —
   the Multi24 does not reliably serve two clients.
2. Add this integration.
3. Old automations keep working if you rename the new entities to your old
   entity ids (Settings → entities → rename), or update the automations to
   the new ids. Registers you used to write directly map to entities:
   register 1187 → `fan` speed, 1185 → `fan` presets / boost & fireplace
   switches, 1208 → `fan` on/off, 1104/1105 → *Speed when home/away*
   numbers, 1097 → *Defrost minimum efficiency* number.

Note: *Speed when home/away* display speeds 1–5 while the device stores 0–4
internally — the integration handles the offset (matches the panel).

## Troubleshooting

- **cannot_connect** — check IP/port 502, VLAN/firewall rules, and that no
  other Modbus client is connected to the unit.
- **not_parmair** — the device answered but the register map didn't match
  (machine type / software version implausible). Open an issue with your
  unit model and, if possible, a register dump.
- **Entities unavailable after working** — the integration retries and
  reconnects automatically; a *connection lost* repair issue appears after
  ~5 failed cycles. Check the network path and other Modbus clients.
- **CO₂ looks ~500 ppm too high/low** — set the CO₂ offset option (see
  above).
- Download **diagnostics** from the device page and attach it to bug
  reports (the host is redacted).

## Development

```bash
uv venv --python 3.13 .venv313
uv pip install --python .venv313/bin/python -r requirements_test.txt
.venv313/bin/python -m pytest --cov=custom_components/parmair --cov-fail-under=90
.venv313/bin/ruff check . && .venv313/bin/ruff format --check .
```

Pure logic (register map, capability detection, summer-mode state machine)
lives in HA-free modules with plain pytest tests; HA behavior is tested with
`pytest-homeassistant-custom-component` against a fake Modbus client seeded
with values captured from a real REXO 120. See `CLAUDE.md` for architecture
notes and contribution conventions.

## License

MIT — see [LICENSE](LICENSE). Parmair, My Air Control and MAC are trademarks
of their respective owners; this project is an independent community
integration.
