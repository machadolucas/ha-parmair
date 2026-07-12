/*
 * Parmair Card — a single dependency-free vanilla-JS Lovelace card:
 * `custom:parmair-card`.
 *
 * Rather than requiring the user to hand-pick a dozen entity_ids, the card
 * discovers everything it needs from the *device*: point it at a Parmair
 * device (or let it auto-pick the first one) and it walks the entity
 * registry for entities that belong to that device, keying each one by the
 * suffix of its `unique_id` after the config-entry-id prefix (hex or ULID)
 * (`<entry_id>_<key>`, e.g. `abc...def_fresh_air_temperature` ->
 * `fresh_air_temperature`). That key is exactly the translation_key/register
 * key the integration uses (see custom_components/parmair/entity.py), so it
 * is stable across the user renaming entity_ids — only survives a device
 * merge/replace, which is rare and always deliberate.
 *
 * Keys the card knows about (all optional — anything missing degrades
 * gracefully, so a unit whose Capabilities detection didn't find e.g. a CO2
 * sensor just doesn't show that chip/row):
 *
 *   fan (domain fan)
 *   sensor.*      fresh_air_temperature, supply_temperature,
 *                 extract_temperature, waste_temperature,
 *                 supply_temperature_after_hru, heat_recovery_efficiency,
 *                 hru_humidity, co2, boost_time_remaining,
 *                 fireplace_time_remaining, fan_speed_state, control_state,
 *                 filter_next_change
 *   binary_sensor.* defrosting, filter_change_required, alarm, home
 *   switch.*      boost, fireplace, summer_mode, summer_auto,
 *                 hru_temperature_control, post_heating
 *   select.*      boost_duration, fireplace_duration
 *   number.*      home_speed, away_speed, defrost_min_efficiency
 *   climate.*     supply_temperature_target, extract_temperature_target
 *   button.*      acknowledge_alarms
 *
 * Layout: a chromeless "split panel" card — header (title, live chips,
 * status chips, expand button), a controls panel (speed stepper, Boost,
 * Fireplace) beside an airflow panel (cross-flow SVG diagram), an
 * expandable settings section (power, home/away speeds, temperature
 * targets, defrost efficiency, mode toggles), stacking to a single column
 * on narrow cards.
 *
 * Two custom elements: `parmair-card` (the card itself) and
 * `parmair-card-editor` (its visual editor), both registered against
 * `customElements` and the card advertised via `window.customCards` so it
 * shows up in the "Add card" picker.
 */

function esc(s) {
  return String(s == null ? "" : s).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
}

// Nearest whole number; "–" for anything that isn't a finite number (covers
// null/undefined/"unknown"/"unavailable" alike).
function fmt0(v) {
  if (v == null || v === "") return "–";
  const n = Number(v);
  return Number.isNaN(n) ? "–" : String(Math.round(n));
}

// One decimal place with a trailing degree sign; "–" (no degree sign) when
// the value is missing/non-numeric — a bare dash reads better than "–°".
function fmtTemp(v) {
  if (v == null || v === "") return "–";
  const n = Number(v);
  return Number.isNaN(n) ? "–" : `${n.toFixed(1)}°`;
}

// unique_ids are `${config_entry_id}_${key}` where the entry id is a 32-hex
// string or a 26-char ULID — strip that prefix to recover the per-entity key.
function keyFromUniqueId(uniqueId) {
  return String(uniqueId || "").replace(/^[0-9A-Za-z]{26,32}_/, "");
}

function define(name, cls) {
  if (!customElements.get(name)) customElements.define(name, cls);
}

function registerCard(card) {
  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c.type === card.type)) {
    window.customCards.push(card);
  }
}

/*
 * Theming: the outer root is a chromeless plain div (no background, border,
 * or shadow) so the header blends with themed/blurred dashboard backgrounds,
 * while the inner panels (controls, airflow, settings) are real <ha-card>
 * elements that pick up the theme's exact card chrome. Controls inside use
 * translucent theme-derived fills (color-mix over --primary-text-color /
 * accent colors), never solid opaque colors.
 *
 * This stylesheet is injected into the shadow root exactly ONCE (see
 * `_render`); everything that changes per render — gradient stops, drain-bar
 * widths, the flow-animation duration — is inline in the markup instead.
 */
const CARD_CSS = `
  :host { display: block; }
  .root { background: none; border: none; outline: none; box-shadow: none; padding: 0; margin: 0; }
  button { font-family: inherit; }
  button:disabled { opacity: 0.5; pointer-events: none; }
  [data-more-info] { cursor: pointer; }

  .header { display: flex; align-items: center; gap: 6px; padding: 0 2px 8px; }
  .title { flex: 0 1 auto; font-weight: 600; font-size: 1em; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; cursor: pointer; color: var(--primary-text-color); }
  .hchips { flex: 1 1 auto; display: flex; align-items: center; gap: 4px; min-width: 0;
    overflow: hidden; }
  .hchip { display: inline-flex; align-items: center; gap: 3px; font-size: 0.74em; font-weight: 600;
    padding: 3px 7px; border-radius: 9px; white-space: nowrap;
    background: color-mix(in srgb, var(--primary-text-color) 6%, transparent);
    color: var(--secondary-text-color); }
  .hchip ha-icon { --mdc-icon-size: 13px; }
  .hchip.icon-only { padding: 3px 6px; }
  .hchip.chip-summer { background: color-mix(in srgb, #ff9800 20%, transparent); color: #ff9800; }
  .hchip.chip-cold { background: color-mix(in srgb, #2196f3 20%, transparent); color: #2196f3; }
  .hchip.chip-dim { opacity: 0.55; }
  .auto-dot { margin-left: 2px; font-size: 0.85em; opacity: 0.9; }

  .more-btn { flex: 0 0 auto; width: 30px; height: 30px; border-radius: 50%; border: none;
    cursor: pointer; padding: 0; display: inline-flex; align-items: center; justify-content: center;
    background: color-mix(in srgb, var(--primary-text-color) 6%, transparent);
    color: var(--secondary-text-color); }
  .more-btn ha-icon { --mdc-icon-size: 18px; transition: transform 0.2s ease; }
  .more-btn.open { background: color-mix(in srgb, var(--primary-color) 18%, transparent);
    color: var(--primary-color); }
  @media (prefers-reduced-motion: reduce) { .more-btn ha-icon { transition: none; } }

  .body-grid { display: grid; grid-template-columns: minmax(150px, 4fr) 5fr; gap: 8px; }
  .body-grid.single { grid-template-columns: 1fr; }
  .narrow .body-grid { grid-template-columns: 1fr; }

  /* The panels are real <ha-card> elements so they render with the theme's
     exact card chrome — --ha-card-* background/border/radius/blur inherit
     through the shadow boundary as CSS vars. No fills of our own here, only
     internal layout. */
  ha-card.panel { padding: 8px; display: flex; flex-direction: column; gap: 8px; min-width: 0;
    box-sizing: border-box; }
  .panel.dimmed { opacity: 0.45; }
  .airflow-panel.dimmed { pointer-events: none; }

  .speed-row { display: flex; align-items: center; gap: 6px; }
  .speed-label { flex: 0 0 auto; font-size: 0.78em; font-weight: 600; color: var(--secondary-text-color); }
  .auto-pill { flex: 0 0 auto; border: 1px solid var(--divider-color, rgba(127,127,127,0.35));
    background: transparent; color: var(--secondary-text-color); font-size: 0.74em; font-weight: 700;
    padding: 4px 10px; border-radius: 10px; cursor: pointer; }
  .auto-pill.active { background: color-mix(in srgb, var(--primary-color) 22%, transparent);
    color: var(--primary-color); border-color: transparent; }
  .stepper { display: flex; align-items: center; gap: 4px; margin-left: auto; }
  .step-btn { width: 28px; height: 28px; border-radius: 50%; border: none; cursor: pointer; padding: 0;
    background: color-mix(in srgb, var(--primary-text-color) 9%, transparent);
    color: var(--primary-text-color); font-size: 1.05em; font-weight: 700; line-height: 1;
    display: inline-flex; align-items: center; justify-content: center; }
  .speed-value { min-width: 20px; text-align: center; font-size: 1.3em; font-weight: 700;
    color: var(--primary-text-color); }
  .speed-value.dimmed { opacity: 0.5; }

  .action-btn { position: relative; overflow: hidden; display: flex; align-items: center;
    justify-content: space-between; width: 100%; min-height: 40px; box-sizing: border-box;
    border-radius: 10px; border: none; cursor: pointer; padding: 0 12px;
    background: color-mix(in srgb, var(--primary-text-color) 6%, transparent);
    color: var(--primary-text-color); font-size: 0.9em; font-weight: 600; text-align: left; }
  .action-left { display: inline-flex; align-items: center; gap: 8px; }
  .action-left ha-icon { --mdc-icon-size: 18px; }
  .action-right { font-size: 0.8em; opacity: 0.9; white-space: nowrap; }
  .action-btn.accent-boost.active { background: color-mix(in srgb, var(--primary-color) 32%, transparent);
    color: var(--primary-color); }
  .action-btn.accent-fireplace.active {
    background: color-mix(in srgb, var(--warning-color, #e6a23c) 30%, transparent);
    color: var(--warning-color, #e6a23c); }
  .drain-bar { position: absolute; left: 0; bottom: 0; height: 4px; border-radius: 2px;
    background: currentColor; opacity: 0.7; }

  .airflow-wrap { position: relative; }
  .airflow-svg { width: 100%; height: auto; display: block; overflow: visible; }
  .flow-base { fill: none; stroke-width: 14; stroke-linecap: round; opacity: 0.22; }
  /* Dash travel speed follows the fan: each render sets --flow-duration
     inline on .airflow-wrap (4s / speed, so speed 1 ≈ 4s … speed 5 = 0.8s);
     .flow-paused (fan off / speed 0) freezes the dashes in place. */
  .flow-dash { fill: none; stroke-width: 5; stroke-linecap: round; stroke-dasharray: 4 8;
    animation: parmair-flow 1.6s linear infinite;
    animation-duration: var(--flow-duration, 1.6s); }
  .flow-paused .flow-dash { animation-play-state: paused; }
  @keyframes parmair-flow { from { stroke-dashoffset: 0; } to { stroke-dashoffset: -24; } }
  @media (prefers-reduced-motion: reduce) { .flow-dash { animation: none; } }

  .ep { position: absolute; display: inline-flex; align-items: baseline; gap: 4px;
    padding: 3px 8px; border-radius: 10px; white-space: nowrap;
    background: color-mix(in srgb, var(--card-background-color, #808080) 45%, transparent);
    backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); }
  .ep-l { font-size: 12px; color: var(--secondary-text-color); }
  .ep-v { font-size: 14px; font-weight: 600; color: var(--primary-text-color); }
  .ep-tl { top: 0; left: 0; }
  .ep-tr { top: 0; right: 0; }
  .ep-bl { bottom: 0; left: 0; }
  .ep-br { bottom: 0; right: 0; }
  .ep-core { top: 50%; left: 50%; transform: translate(-50%, -50%); flex-direction: column;
    align-items: center; gap: 0; padding: 4px 10px; border-radius: 12px; }
  .ep-core .ep-l { font-size: 10px; }

  .metrics { display: flex; flex-direction: column; gap: 4px; }
  .metric-row { display: flex; align-items: baseline; justify-content: space-between;
    font-size: 0.84em; }
  .metric-label { color: var(--secondary-text-color); }
  .metric-value { color: var(--primary-text-color); font-weight: 600; }

  ha-card.more-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 14px;
    margin-top: 10px; padding: 8px 10px; box-sizing: border-box; }
  .narrow .more-grid { grid-template-columns: 1fr; }
  .set-row { display: flex; align-items: center; gap: 8px; min-height: 34px; }
  .set-row.full { grid-column: 1 / -1; }
  .set-label { flex: 1 1 auto; font-size: 0.82em; color: var(--secondary-text-color); min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .set-value { min-width: 40px; text-align: center; font-size: 0.92em; font-weight: 600;
    color: var(--primary-text-color); }
  .step-btn.sm { width: 24px; height: 24px; font-size: 0.95em; }
  .stepper.mini { gap: 4px; margin-left: 0; flex: 0 0 auto; }

  .toggle-pill, .pwr-pill { flex: 0 0 auto; border: none; cursor: pointer; font-size: 0.76em;
    font-weight: 700; padding: 5px 12px; border-radius: 10px; white-space: nowrap;
    background: color-mix(in srgb, var(--primary-text-color) 8%, transparent);
    color: var(--secondary-text-color); }
  .toggle-pill.active { background: color-mix(in srgb, var(--primary-color) 22%, transparent);
    color: var(--primary-color); }
  .pwr-pill.confirm { background: color-mix(in srgb, var(--error-color, #db4437) 22%, transparent);
    color: var(--error-color, #db4437); }
  .pwr-pill.turn-on { background: color-mix(in srgb, #4caf50 22%, transparent); color: #4caf50; }

  .banner { margin-top: 10px; padding: 7px 10px; border-radius: 10px; font-size: 0.82em;
    font-weight: 600; }
  .banner-warn { background: color-mix(in srgb, #ff9800 18%, transparent); color: #ff9800; }
  .banner-error { background: color-mix(in srgb, var(--error-color, #db4437) 16%, transparent);
    color: var(--error-color, #db4437); cursor: pointer; }

  .hint { color: var(--secondary-text-color); padding: 4px 2px 8px; font-size: 0.85em; }
`;

// Cross-flow HRV diagram geometry (viewBox 0 0 280 120). Fresh (top-left)
// and Supply (bottom-right) are one path; Extract (top-right) and Waste
// (bottom-left) are the other. Both diagonals pass through the same center
// point (140,60), which is where the core temperature pill overlays them.
const AIRFLOW_FRESH = { x: 25, y: 20 };
const AIRFLOW_EXTRACT = { x: 255, y: 20 };
const AIRFLOW_WASTE = { x: 25, y: 100 };
const AIRFLOW_SUPPLY = { x: 255, y: 100 };

const COOL = "var(--info-color, #4a90d9)";
const WARM = "var(--warning-color, #e6a23c)";
// When the two ends of a stream are within 0.3 °C there is no meaningful
// hot/cold side — both ends get this neutral mid blend instead.
const NEUTRAL = `color-mix(in srgb, ${COOL} 50%, ${WARM})`;

class ParmairCard extends HTMLElement {
  setConfig(config) {
    const cfg = { ...(config || {}) };
    // Back-compat: older configs used `show_temperatures`/`show_chips`; only
    // consulted when the new key is absent so an explicit new value always
    // wins.
    if (cfg.show_airflow === undefined && cfg.show_temperatures !== undefined) {
      cfg.show_airflow = cfg.show_temperatures;
    }
    if (cfg.show_metrics === undefined && cfg.show_chips !== undefined) {
      cfg.show_metrics = cfg.show_chips;
    }
    this._config = {
      show_fan_row: true,
      show_airflow: true,
      show_metrics: true,
      show_alerts: true,
      compact: false,
      ...cfg,
    };
    // key -> entity_id, built lazily once the registries load; null means
    // "not resolved yet", {} means "resolved, nothing found".
    this._entities = null;
    this._entitiesPromise = null;
    this._lastStates = null;
    this._lastConfigSig = null;
    this._lastEntitiesReady = null;
    // Power pill's 2-step "turn off?" confirmation: a deadline timestamp
    // (not just a bare setTimeout) so the visible state is always recomputed
    // from `Date.now()` at render time, however many renders happen while
    // it's pending.
    this._pwrConfirmUntil = null;
    this._pwrRevertTimer = null;
    // Settings section visibility — pure per-instance UI state, resets when
    // the card is recreated, deliberately not persisted in config.
    this._expanded = false;
    this._forceNextRender = false;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._entitiesPromise) {
      this._entitiesPromise = this._discoverEntities();
    }
    this._render();
  }

  getCardSize() {
    if (this._config && this._config.compact) return 3;
    let size = 4;
    if (!this._config || this._config.show_airflow !== false) size += 2;
    if (this._config && this._config.show_alerts !== false) size += 1;
    return size;
  }

  getGridOptions() {
    return { rows: this._config && this._config.compact ? 3 : "auto", columns: 12, min_columns: 6 };
  }

  static getConfigElement() {
    return document.createElement("parmair-card-editor");
  }

  static getStubConfig() {
    return {};
  }

  // ---- entity discovery -------------------------------------------------

  async _discoverEntities() {
    try {
      const [entities, devices] = await Promise.all([
        this._hass.callWS({ type: "config/entity_registry/list" }),
        this._hass.callWS({ type: "config/device_registry/list" }),
      ]);
      let device = null;
      if (this._config.device_id) {
        device = devices.find((d) => d.id === this._config.device_id) || null;
      }
      if (!device) {
        device =
          devices.find(
            (d) => Array.isArray(d.identifiers) && d.identifiers.some((ident) => ident[0] === "parmair"),
          ) || null;
      }
      const map = {};
      if (device) {
        entities
          .filter((e) => e.device_id === device.id && e.unique_id)
          .forEach((e) => {
            map[keyFromUniqueId(e.unique_id)] = e.entity_id;
          });
      }
      this._entities = map;
    } catch (err) {
      // Best-effort: an older HA without these WS commands, or a transient
      // failure, just leaves the card showing placeholders.
      this._entities = {};
    }
    this._render();
  }

  // ---- entity/state helpers ----------------------------------------------

  _entityId(key) {
    return this._entities ? this._entities[key] : undefined;
  }

  _has(key) {
    return !!this._entityId(key);
  }

  _state(key) {
    const id = this._entityId(key);
    return id ? this._hass.states[id] : undefined;
  }

  _raw(key) {
    const st = this._state(key);
    return st ? st.state : null;
  }

  _isOn(key) {
    return this._raw(key) === "on";
  }

  _num(key) {
    const raw = this._raw(key);
    if (raw == null || raw === "") return null;
    const n = Number(raw);
    return Number.isNaN(n) ? null : n;
  }

  _speedNumber() {
    return this._num("fan_speed_state");
  }

  // Current climate setpoint: `attributes.temperature` on the climate
  // entity, not its state string (which is the HVAC mode).
  _climateTarget(key) {
    const st = this._state(key);
    const t = st && st.attributes ? Number(st.attributes.temperature) : NaN;
    return Number.isNaN(t) ? null : t;
  }

  // ---- rendering: header ---------------------------------------------------

  // Compact live-value chips right after the title: fan power, humidity,
  // CO₂ — icon + value only, each rendered only when its entity exists.
  // Clicking a chip opens more-info for its entity (the fan-power chip
  // opens the fan itself, the most useful target for that number).
  _headerChipsHtml() {
    const chips = [];
    if (this._has("fan_speed_state")) {
      const speed = this._speedNumber();
      chips.push({ icon: "mdi:fan", value: speed == null ? "–" : `${fmt0(speed * 20)}%`, key: "fan", tip: "Fan power" });
    }
    if (this._has("hru_humidity")) {
      chips.push({ icon: "mdi:water-percent", value: `${fmt0(this._raw("hru_humidity"))}%`, key: "hru_humidity", tip: "Humidity" });
    }
    if (this._has("co2")) {
      chips.push({ icon: "mdi:molecule-co2", value: fmt0(this._raw("co2")), key: "co2", tip: "CO₂ (ppm)" });
    }
    return chips
      .map((c) => `<span class="hchip" data-more-info="${c.key}" title="${esc(c.tip)}"><ha-icon icon="${c.icon}"></ha-icon>${c.value}</span>`)
      .join("");
  }

  _iconChipHtml(key, icon, tip, extraClass) {
    return `<span class="hchip icon-only${extraClass ? ` ${extraClass}` : ""}" data-more-info="${key}" title="${esc(tip)}"><ha-icon icon="${icon}"></ha-icon></span>`;
  }

  // Mode/status chips between the value chips and the more-button. The
  // Summer chip keeps its label; the rest are icon-only with a tooltip and
  // only appear while their state is on — except the home/away chip, which
  // is always shown (dimmed when away).
  _statusChipsHtml() {
    const chips = [];
    if (this._isOn("summer_mode")) {
      const auto = this._isOn("summer_auto")
        ? `<span class="auto-dot" title="Automatic">ᴬ</span>`
        : "";
      chips.push(
        `<span class="hchip chip-summer" data-more-info="summer_mode" title="Summer mode"><ha-icon icon="mdi:weather-sunny"></ha-icon>Summer${auto}</span>`,
      );
    }
    if (this._isOn("hru_temperature_control")) {
      chips.push(this._iconChipHtml("hru_temperature_control", "mdi:home-thermometer", "HRU temperature control", ""));
    }
    if (this._isOn("post_heating")) {
      chips.push(this._iconChipHtml("post_heating", "mdi:radiator", "Post-heating", ""));
    }
    if (this._isOn("defrosting")) {
      chips.push(this._iconChipHtml("defrosting", "mdi:snowflake", "Defrosting", "chip-cold"));
    }
    if (this._has("home")) {
      const home = this._isOn("home");
      chips.push(
        this._iconChipHtml("home", home ? "mdi:home" : "mdi:home-export-outline", home ? "Home" : "Away", home ? "" : "chip-dim"),
      );
    }
    return chips.join("");
  }

  _headerHtml() {
    const title = this._config.title || "Ventilation";
    return `<div class="header">
      <span class="title" data-action="title">${esc(title)}</span>
      <span class="hchips">${this._headerChipsHtml()}</span>
      ${this._statusChipsHtml()}
      <button type="button" class="more-btn${this._expanded ? " open" : ""}" data-action="more"
        aria-label="${this._expanded ? "Hide settings" : "Show settings"}" aria-expanded="${this._expanded}">
        <ha-icon icon="${this._expanded ? "mdi:chevron-up" : "mdi:dots-horizontal"}"></ha-icon>
      </button>
    </div>`;
  }

  // ---- rendering: controls panel -------------------------------------------

  _speedRowHtml() {
    const controlState = this._raw("control_state");
    const isManual = controlState === "manual";
    const autoActive = controlState != null && controlState !== "manual";
    const n = this._speedNumber();
    const disabledAll = !this._isOn("fan");
    const minusDisabled = disabledAll || n == null || n <= 1;
    const plusDisabled = disabledAll || n == null || n >= 5;
    const display = n == null ? "–" : String(n);
    return `<div class="speed-row">
      <span class="speed-label">Speed</span>
      <button type="button" class="auto-pill${autoActive ? " active" : ""}" data-action="preset-auto"${disabledAll ? " disabled" : ""}>AUTO</button>
      <div class="stepper">
        <button type="button" class="step-btn" data-action="speed-step" data-dir="-1" aria-label="Decrease speed"${minusDisabled ? " disabled" : ""}>−</button>
        <span class="speed-value${!isManual ? " dimmed" : ""}">${display}</span>
        <button type="button" class="step-btn" data-action="speed-step" data-dir="1" aria-label="Increase speed"${plusDisabled ? " disabled" : ""}>+</button>
      </div>
    </div>`;
  }

  // Shared markup for the Boost / Fireplace big action buttons: an icon +
  // label on the left, remaining minutes on the right when active, and a
  // drain bar along the bottom edge showing time-remaining / total-duration.
  _actionButtonHtml(key, icon, label, timerKey, durationKey, fallbackDuration, accentClass) {
    if (!this._has(key)) return "";
    const on = this._isOn(key);
    const disabledAll = !this._isOn("fan");
    let remaining = null;
    if (this._has(timerKey)) {
      const v = Number(this._raw(timerKey));
      if (!Number.isNaN(v) && v > 0) remaining = v;
    }
    let drainHtml = "";
    if (on && remaining != null) {
      let total = fallbackDuration;
      if (this._has(durationKey)) {
        const d = parseInt(this._raw(durationKey), 10);
        if (!Number.isNaN(d) && d > 0) total = d;
      }
      const pct = Math.max(0, Math.min(100, (remaining / total) * 100));
      drainHtml = `<span class="drain-bar" style="width:${pct.toFixed(1)}%"></span>`;
    }
    const rightHtml =
      on && remaining != null ? `<span class="action-right">${Math.round(remaining)} min</span>` : "";
    return `<button type="button" class="action-btn ${accentClass}${on ? " active" : ""}" data-action="toggle" data-switch="${key}"${disabledAll ? " disabled" : ""}>
      <span class="action-left"><ha-icon icon="${icon}"></ha-icon><span class="action-label">${esc(label)}</span></span>
      ${rightHtml}
      ${drainHtml}
    </button>`;
  }

  _controlsPanelHtml() {
    const cfg = this._config;
    const parts = [];
    if (cfg.show_fan_row) parts.push(this._speedRowHtml());
    parts.push(this._actionButtonHtml("boost", "mdi:fan-plus", "Boost mode", "boost_time_remaining", "boost_duration", 180, "accent-boost"));
    parts.push(this._actionButtonHtml("fireplace", "mdi:fireplace", "Fireplace mode", "fireplace_time_remaining", "fireplace_duration", 15, "accent-fireplace"));
    const dimmed = !this._isOn("fan") ? " dimmed" : "";
    return `<ha-card class="panel controls-panel${dimmed}">${parts.join("")}</ha-card>`;
  }

  // ---- rendering: airflow panel ---------------------------------------------

  // Gradient endpoint colors for one air stream, decided by ACTUAL measured
  // temperatures each render: the colder end is blue, the warmer end amber.
  // In summer the fresh air can be warmer than the supply air, flipping the
  // gradient — hardcoded sides would color that wrong. Below a 0.3 °C
  // difference both ends get a neutral mid tone; missing readings fall back
  // to the physical default for that stream.
  _flowColors(rawStart, rawEnd, fallback) {
    const a = Number(rawStart);
    const b = Number(rawEnd);
    if (rawStart == null || rawStart === "" || rawEnd == null || rawEnd === "" ||
        Number.isNaN(a) || Number.isNaN(b)) {
      return fallback;
    }
    if (Math.abs(a - b) < 0.3) return [NEUTRAL, NEUTRAL];
    return a < b ? [COOL, WARM] : [WARM, COOL];
  }

  // A small filled triangle pointing along `angleDeg`, placed near (but
  // slightly before) the flow's exit point so it doesn't collide with the
  // endpoint overlays.
  _arrowheadHtml(x, y, angleDeg, color) {
    return `<polygon class="arrowhead" points="0,-4 8,0 0,4" fill="${color}" transform="translate(${x},${y}) rotate(${angleDeg})"></polygon>`;
  }

  _epOverlayHtml(posClass, label, value) {
    return `<div class="ep ${posClass}"><span class="ep-l">${esc(label)}</span><span class="ep-v">${value}</span></div>`;
  }

  _airflowHtml() {
    const freshRaw = this._raw("fresh_air_temperature");
    const supplyRaw = this._raw("supply_temperature");
    const extractRaw = this._raw("extract_temperature");
    const wasteRaw = this._raw("waste_temperature");
    const core = fmtTemp(this._raw("supply_temperature_after_hru"));
    // Fresh->Supply: outdoor default cool->warm; Extract->Waste the reverse.
    const [fsStart, fsEnd] = this._flowColors(freshRaw, supplyRaw, [COOL, WARM]);
    const [ewStart, ewEnd] = this._flowColors(extractRaw, wasteRaw, [WARM, COOL]);
    const freshSupplyPath = `M${AIRFLOW_FRESH.x},${AIRFLOW_FRESH.y} L${AIRFLOW_SUPPLY.x},${AIRFLOW_SUPPLY.y}`;
    const extractWastePath = `M${AIRFLOW_EXTRACT.x},${AIRFLOW_EXTRACT.y} L${AIRFLOW_WASTE.x},${AIRFLOW_WASTE.y}`;
    // Dash animation speed follows the fan: 4s / speed (speed 1 ≈ 4s,
    // speed 5 = 0.8s), paused entirely at speed 0 or when the fan is off.
    const speed = this._speedNumber();
    const running = this._isOn("fan") && speed != null && speed > 0;
    const duration = running ? (4 / speed).toFixed(2) : "1.6";
    const pausedCls = running ? "" : " flow-paused";
    // The temperature labels are HTML overlays (not SVG <text>) so they can
    // carry a backdrop-filter blur — SVG text can't — keeping them readable
    // where they sit on top of the wide flow channels.
    return `<div class="airflow-wrap${pausedCls}" style="--flow-duration:${duration}s">
      <svg class="airflow-svg" viewBox="0 0 280 120" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <defs>
          <linearGradient id="pg-fs" gradientUnits="userSpaceOnUse" x1="${AIRFLOW_FRESH.x}" y1="${AIRFLOW_FRESH.y}" x2="${AIRFLOW_SUPPLY.x}" y2="${AIRFLOW_SUPPLY.y}">
            <stop offset="0%" stop-color="${fsStart}"></stop>
            <stop offset="100%" stop-color="${fsEnd}"></stop>
          </linearGradient>
          <linearGradient id="pg-ew" gradientUnits="userSpaceOnUse" x1="${AIRFLOW_EXTRACT.x}" y1="${AIRFLOW_EXTRACT.y}" x2="${AIRFLOW_WASTE.x}" y2="${AIRFLOW_WASTE.y}">
            <stop offset="0%" stop-color="${ewStart}"></stop>
            <stop offset="100%" stop-color="${ewEnd}"></stop>
          </linearGradient>
        </defs>
        <path class="flow-base" d="${freshSupplyPath}" stroke="url(#pg-fs)"></path>
        <path class="flow-dash" d="${freshSupplyPath}" stroke="url(#pg-fs)"></path>
        <path class="flow-base" d="${extractWastePath}" stroke="url(#pg-ew)"></path>
        <path class="flow-dash" d="${extractWastePath}" stroke="url(#pg-ew)"></path>
        ${this._arrowheadHtml(220.5, 88, 19.1, fsEnd)}
        ${this._arrowheadHtml(59.5, 88, 160.9, ewEnd)}
      </svg>
      ${this._epOverlayHtml("ep-tl", "Fresh", fmtTemp(freshRaw))}
      ${this._epOverlayHtml("ep-tr", "Extract", fmtTemp(extractRaw))}
      ${this._epOverlayHtml("ep-bl", "Waste", fmtTemp(wasteRaw))}
      ${this._epOverlayHtml("ep-br", "Supply", fmtTemp(supplyRaw))}
      <div class="ep ep-core"><span class="ep-v">${core}</span><span class="ep-l">core</span></div>
    </div>`;
  }

  _metricsHtml() {
    const heatRecovery = this._has("heat_recovery_efficiency")
      ? `${fmt0(this._raw("heat_recovery_efficiency"))}%`
      : "–";
    const moreInfo = this._has("heat_recovery_efficiency")
      ? ` data-more-info="heat_recovery_efficiency"`
      : "";
    return `<div class="metrics">
      <div class="metric-row"><span class="metric-label"${moreInfo}>Heat recovery</span><span class="metric-value">${heatRecovery}</span></div>
    </div>`;
  }

  _airflowPanelHtml() {
    const dimmed = !this._isOn("fan") ? " dimmed" : "";
    const parts = [this._airflowHtml()];
    if (this._config.show_metrics) parts.push(this._metricsHtml());
    return `<ha-card class="panel airflow-panel${dimmed}">${parts.join("")}</ha-card>`;
  }

  // ---- rendering: expandable settings section -------------------------------

  _powerRowHtml() {
    if (!this._has("fan")) return "";
    const isOn = this._isOn("fan");
    let pill;
    if (!isOn) {
      // Not on: nothing to confirm, drop any stale pending confirmation.
      this._pwrConfirmUntil = null;
      pill = `<button type="button" class="pwr-pill turn-on" data-action="power">Turn on</button>`;
    } else {
      const pending = this._pwrConfirmUntil != null && Date.now() < this._pwrConfirmUntil;
      pill = pending
        ? `<button type="button" class="pwr-pill confirm" data-action="power">Turn off?</button>`
        : `<button type="button" class="pwr-pill" data-action="power">Turn off</button>`;
    }
    // Full-width row so the setting rows below pair up two-per-line.
    return `<div class="set-row full"><span class="set-label" data-more-info="fan">Power</span>${pill}</div>`;
  }

  // A labeled −/value/+ row driving either a number entity
  // (number.set_value) or a climate setpoint (climate.set_temperature) —
  // which one is decided by `action` and resolved in `_onClick`. The label
  // (and only the label — the buttons carry data-action, which wins) opens
  // the entity's more-info dialog.
  _stepRowHtml(label, key, action, current, min, max, step, formatFn) {
    if (!this._has(key)) return "";
    const minusDisabled = current == null || current <= min;
    const plusDisabled = current == null || current >= max;
    const display = current == null ? "–" : formatFn(current);
    return `<div class="set-row">
      <span class="set-label" data-more-info="${key}">${esc(label)}</span>
      <div class="stepper mini">
        <button type="button" class="step-btn sm" data-action="${action}" data-key="${key}" data-dir="-1" data-min="${min}" data-max="${max}" data-step="${step}" aria-label="Decrease ${esc(label)}"${minusDisabled ? " disabled" : ""}>−</button>
        <span class="set-value">${display}</span>
        <button type="button" class="step-btn sm" data-action="${action}" data-key="${key}" data-dir="1" data-min="${min}" data-max="${max}" data-step="${step}" aria-label="Increase ${esc(label)}"${plusDisabled ? " disabled" : ""}>+</button>
      </div>
    </div>`;
  }

  _toggleRowHtml(label, key) {
    if (!this._has(key)) return "";
    const on = this._isOn(key);
    return `<div class="set-row">
      <span class="set-label" data-more-info="${key}">${esc(label)}</span>
      <button type="button" class="toggle-pill${on ? " active" : ""}" data-action="toggle" data-switch="${key}">${on ? "On" : "Off"}</button>
    </div>`;
  }

  _expandedHtml() {
    if (!this._expanded) return "";
    const pct = (v) => `${fmt0(v)}%`;
    const rows = [
      this._powerRowHtml(),
      this._stepRowHtml("Speed when home", "home_speed", "num-step", this._num("home_speed"), 1, 5, 1, fmt0),
      this._stepRowHtml("Speed when away", "away_speed", "num-step", this._num("away_speed"), 1, 5, 1, fmt0),
      this._stepRowHtml("Supply target", "supply_temperature_target", "climate-step",
        this._climateTarget("supply_temperature_target"), 15, 25, 0.5, fmtTemp),
      this._stepRowHtml("Extract target (summer)", "extract_temperature_target", "climate-step",
        this._climateTarget("extract_temperature_target"), 18, 26, 0.5, fmtTemp),
      this._stepRowHtml("Defrost min efficiency", "defrost_min_efficiency", "num-step",
        this._num("defrost_min_efficiency"), 0, 100, 1, pct),
      this._toggleRowHtml("HRU temp control", "hru_temperature_control"),
      this._toggleRowHtml("Summer mode", "summer_mode"),
      this._toggleRowHtml("Post-heating", "post_heating"),
    ].join("");
    if (!rows) return "";
    // Deliberately NOT dimmed/disabled when the fan is off — the Power row
    // living here must stay usable, and the rest are settings registers that
    // are safe to adjust while the unit is stopped.
    return `<ha-card class="more-grid">${rows}</ha-card>`;
  }

  // ---- rendering: alerts + assembly -----------------------------------------

  _fmtDate(v) {
    const d = new Date(v);
    if (Number.isNaN(d.getTime())) return esc(v);
    return esc(d.toLocaleDateString());
  }

  _alertHtml() {
    const parts = [];
    if (this._isOn("filter_change_required")) {
      const next = this._has("filter_next_change") ? this._raw("filter_next_change") : null;
      const suffix = next ? ` (next: ${this._fmtDate(next)})` : "";
      parts.push(`<div class="banner banner-warn">Filter change due${suffix}</div>`);
    }
    if (this._isOn("alarm")) {
      parts.push(
        `<div class="banner banner-error" data-action="ack-alarm">Active alarm — tap to acknowledge</div>`,
      );
    }
    return parts.join("");
  }

  _bodyHtml() {
    const cfg = this._config;
    const sections = [this._headerHtml()];
    if (this._entities === null) {
      sections.push(`<div class="hint">Loading Parmair entities…</div>`);
      return `<div class="card-wrap">${sections.join("")}</div>`;
    }
    const panels = [this._controlsPanelHtml()];
    if (!cfg.compact && cfg.show_airflow) panels.push(this._airflowPanelHtml());
    const singleCls = panels.length === 1 ? " single" : "";
    sections.push(`<div class="body-grid${singleCls}">${panels.join("")}</div>`);
    sections.push(this._expandedHtml());
    if (!cfg.compact && cfg.show_alerts) sections.push(this._alertHtml());
    return `<div class="card-wrap">${sections.join("")}</div>`;
  }

  // ---- change detection ---------------------------------------------------

  // Only the entities this card actually references matter for a re-render;
  // comparing their state *objects* by reference (HA replaces the object on
  // every state change) avoids rebuilding the DOM on unrelated bus noise.
  _relevantStates() {
    if (!this._entities) return [];
    return Object.values(this._entities).map((id) => this._hass.states[id]);
  }

  _shouldRender() {
    const states = this._relevantStates();
    const configSig = JSON.stringify(this._config);
    const entitiesReady = this._entities !== null;
    const changed =
      configSig !== this._lastConfigSig ||
      entitiesReady !== this._lastEntitiesReady ||
      !this._lastStates ||
      states.length !== this._lastStates.length ||
      states.some((s, i) => s !== this._lastStates[i]);
    this._lastConfigSig = configSig;
    this._lastEntitiesReady = entitiesReady;
    this._lastStates = states;
    return changed;
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._card) {
      // Shadow DOM keeps CARD_CSS fully scoped (a light-DOM <style> would
      // leak rules like `ha-card {}` to sibling cards in the same section),
      // and the style element is created ONCE — re-injecting it on every
      // hass tick forces a full restyle pass and can stall dashboard paint.
      const shadow = this.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      style.textContent = CARD_CSS;
      shadow.appendChild(style);
      // A plain <div>, deliberately NOT <ha-card>: ha-card carries themed
      // host chrome (border, background, shadow via --ha-card-* vars) that
      // kept painting an outline around the "chromeless" card even with our
      // own overrides. Nothing in HA requires the root to be an ha-card.
      this._card = document.createElement("div");
      this._card.className = "root";
      shadow.appendChild(this._card);
      this._card.addEventListener("click", (e) => this._onClick(e));
      // Responsive stacking via ResizeObserver instead of a container query:
      // `container-type: inline-size` inside the sections grid can create a
      // layout feedback loop (observed as minutes-long blank paints).
      this._resizeObserver = new ResizeObserver((entries) => {
        const width = entries[0]?.contentRect?.width || 0;
        this._card.classList.toggle("narrow", width > 0 && width < 360);
      });
      this._resizeObserver.observe(this._card);
    }
    if (this._forceNextRender) {
      this._forceNextRender = false;
    } else if (!this._shouldRender()) {
      return;
    }
    this._card.innerHTML = this._bodyHtml();
  }

  disconnectedCallback() {
    clearTimeout(this._pwrRevertTimer);
    if (this._resizeObserver) this._resizeObserver.disconnect();
  }

  connectedCallback() {
    if (this._resizeObserver && this._card) this._resizeObserver.observe(this._card);
  }

  // Interaction-driven UI state (the power confirmation, the expanded
  // section) doesn't come from a hass state change, so it must bypass
  // `_shouldRender`'s diffing — this forces exactly one render regardless
  // of what it reports.
  _forceRender() {
    this._forceNextRender = true;
    this._render();
  }

  // ---- interaction ----------------------------------------------------

  _callService(domain, service, data) {
    this._hass.callService(domain, service, data);
  }

  _fireMoreInfo(entityId) {
    this.dispatchEvent(
      new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }),
    );
  }

  _onPowerClick() {
    const fanId = this._entityId("fan");
    if (!fanId) return;
    const isOn = this._isOn("fan");
    if (!isOn) {
      this._callService("fan", "turn_on", { entity_id: fanId });
      return;
    }
    const now = Date.now();
    const pending = this._pwrConfirmUntil != null && now < this._pwrConfirmUntil;
    clearTimeout(this._pwrRevertTimer);
    if (pending) {
      this._pwrConfirmUntil = null;
      this._callService("fan", "turn_off", { entity_id: fanId });
      this._forceRender();
      return;
    }
    this._pwrConfirmUntil = now + 4000;
    this._pwrRevertTimer = setTimeout(() => {
      this._pwrConfirmUntil = null;
      this._forceRender();
    }, 4000);
    this._forceRender();
  }

  // Shared −/+ stepping for the settings rows: clamp to the row's bounds
  // and skip the service call when nothing would change.
  _stepValue(el, current) {
    const dir = Number(el.dataset.dir);
    const min = Number(el.dataset.min);
    const max = Number(el.dataset.max);
    const step = Number(el.dataset.step);
    if ([dir, min, max, step].some(Number.isNaN)) return null;
    const base = current == null ? (dir > 0 ? min - step : max + step) : current;
    const next = Math.max(min, Math.min(max, base + dir * step));
    return next === current ? null : next;
  }

  _onClick(e) {
    const el = e.target.closest("[data-action]");
    if (!el) {
      // No action target: fall back to more-info hotspots (header chips,
      // setting-row labels, the Heat recovery label). Checked AFTER
      // data-action so a row's control buttons always win over its label.
      const moreEl = e.target.closest("[data-more-info]");
      if (moreEl) {
        const id = this._entityId(moreEl.dataset.moreInfo);
        if (id) this._fireMoreInfo(id);
      }
      return;
    }
    const action = el.dataset.action;
    const fanId = this._entityId("fan");

    if (action === "title") {
      if (fanId) this._fireMoreInfo(fanId);
      return;
    }
    if (action === "more") {
      this._expanded = !this._expanded;
      this._forceRender();
      return;
    }
    if (action === "power") {
      this._onPowerClick();
      return;
    }
    if (action === "preset-auto") {
      if (fanId) this._callService("fan", "set_preset_mode", { entity_id: fanId, preset_mode: "home" });
      return;
    }
    if (action === "speed-step") {
      if (!fanId) return;
      const dir = Number(el.dataset.dir);
      if (Number.isNaN(dir)) return;
      const current = this._speedNumber();
      const base = current == null ? (dir > 0 ? 0 : 6) : current;
      const next = Math.max(1, Math.min(5, base + dir));
      this._callService("fan", "set_percentage", { entity_id: fanId, percentage: next * 20 });
      return;
    }
    if (action === "num-step") {
      const id = this._entityId(el.dataset.key);
      if (!id) return;
      const next = this._stepValue(el, this._num(el.dataset.key));
      if (next != null) this._callService("number", "set_value", { entity_id: id, value: next });
      return;
    }
    if (action === "climate-step") {
      const id = this._entityId(el.dataset.key);
      if (!id) return;
      const next = this._stepValue(el, this._climateTarget(el.dataset.key));
      if (next != null) {
        this._callService("climate", "set_temperature", { entity_id: id, temperature: next });
      }
      return;
    }
    if (action === "toggle") {
      const key = el.dataset.switch;
      const id = this._entityId(key);
      if (!id) return;
      const isOn = this._isOn(key);
      this._callService("switch", isOn ? "turn_off" : "turn_on", { entity_id: id });
      return;
    }
    if (action === "ack-alarm") {
      const id = this._entityId("acknowledge_alarms");
      if (!id) return;
      if (!window.confirm("Acknowledge the active alarm?")) return;
      this._callService("button", "press", { entity_id: id });
    }
  }
}

/* ------------------------------------------------------------------ *
 * UI editor
 * ------------------------------------------------------------------ */

class ParmairCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...(config || {}) };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _schema() {
    return [
      { name: "device_id", selector: { device: { integration: "parmair" } } },
      { name: "title", selector: { text: {} } },
      { name: "show_airflow", selector: { boolean: {} } },
      { name: "show_metrics", selector: { boolean: {} } },
      { name: "show_alerts", selector: { boolean: {} } },
      { name: "compact", selector: { boolean: {} } },
    ];
  }

  _labels() {
    return {
      device_id: "Parmair device (auto-detected if empty)",
      title: "Title (optional)",
      show_airflow: "Show airflow diagram",
      show_metrics: "Show heat recovery row",
      show_alerts: "Show filter / alarm banners",
      compact: "Compact (header + controls only)",
    };
  }

  // Back-compat defaults: an old config that only has `show_temperatures` /
  // `show_chips` should still show the right toggle state under the new
  // names (which aren't rendered — only `show_airflow`/`show_metrics` are).
  _defaultAirflow() {
    if (this._config.show_airflow !== undefined) return this._config.show_airflow !== false;
    if (this._config.show_temperatures !== undefined) return this._config.show_temperatures !== false;
    return true;
  }

  _defaultMetrics() {
    if (this._config.show_metrics !== undefined) return this._config.show_metrics !== false;
    if (this._config.show_chips !== undefined) return this._config.show_chips !== false;
    return true;
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this.appendChild(this._form);
      this._form.computeLabel = (s) => this._labels()[s.name] || s.name;
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        const next = { ...ev.detail.value };
        if (JSON.stringify(next) === JSON.stringify(this._config)) return;
        this._config = next;
        this.dispatchEvent(
          new CustomEvent("config-changed", { detail: { config: next }, bubbles: true, composed: true }),
        );
      });
    }
    this._form.hass = this._hass;
    this._form.schema = this._schema();
    this._form.data = {
      show_airflow: this._defaultAirflow(),
      show_metrics: this._defaultMetrics(),
      show_alerts: this._config.show_alerts !== false,
      compact: this._config.compact === true,
      ...this._config,
    };
  }
}

define("parmair-card", ParmairCard);
define("parmair-card-editor", ParmairCardEditor);

registerCard({
  type: "parmair-card",
  name: "Parmair Card",
  description: "Control and monitor a Parmair MAC ventilation unit.",
  preview: true,
});
