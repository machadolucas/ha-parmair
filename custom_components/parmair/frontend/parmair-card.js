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
 * sensor just doesn't show that row):
 *
 *   fan (domain fan)
 *   sensor.*      fresh_air_temperature, supply_temperature,
 *                 extract_temperature, waste_temperature,
 *                 supply_temperature_after_hru, heat_recovery_efficiency,
 *                 hru_humidity, co2, boost_time_remaining,
 *                 fireplace_time_remaining, fan_speed_state, control_state,
 *                 filter_next_change, temperature_mode
 *   binary_sensor.* defrosting, filter_change_required, alarm
 *   switch.*      boost, fireplace, summer_mode, summer_auto
 *   select.*      boost_duration, fireplace_duration
 *   button.*      acknowledge_alarms
 *
 * Layout: a "split panel" card — a controls panel (speed stepper, Boost,
 * Fireplace) beside an airflow panel (cross-flow SVG diagram + metrics),
 * stacking to a single column on narrow cards.
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

const CARD_CSS = `
  ha-card { padding: 0; overflow: hidden; }

  .header { display: flex; align-items: center; gap: 8px; padding: 12px 14px; }
  .title { flex: 1 1 auto; font-weight: 600; font-size: 1em; min-width: 0; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }

  .badge { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 4px; font-size: 0.76em;
    font-weight: 600; padding: 3px 9px; border-radius: 10px; white-space: nowrap; }
  .badge ha-icon { --mdc-icon-size: 14px; }
  .badge-cold { background: color-mix(in srgb, #2196f3 20%, transparent); color: #2196f3; }
  .badge-summer { background: color-mix(in srgb, #ff9800 20%, transparent); color: #ff9800; }
  .badge-heat { background: color-mix(in srgb, var(--error-color, #db4437) 18%, transparent);
    color: var(--error-color, #db4437); }
  .auto-dot { margin-left: 2px; font-size: 0.85em; opacity: 0.9; }

  .pwr-round { flex: 0 0 auto; width: 32px; height: 32px; border-radius: 50%; border: none;
    cursor: pointer; padding: 0; display: inline-flex; align-items: center; justify-content: center;
    background: var(--secondary-background-color, rgba(127,127,127,0.15));
    color: var(--secondary-text-color); }
  .pwr-round ha-icon { --mdc-icon-size: 18px; }
  .pwr-confirm-pill, .pwr-on-pill { flex: 0 0 auto; border: none; cursor: pointer; font-size: 0.78em;
    font-weight: 700; padding: 6px 12px; border-radius: 12px; white-space: nowrap; }
  .pwr-confirm-pill { background: color-mix(in srgb, var(--error-color, #db4437) 20%, transparent);
    color: var(--error-color, #db4437); }
  .pwr-on-pill { background: color-mix(in srgb, #4caf50 22%, transparent); color: #4caf50; }

  .body-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 2px 14px 12px; }
  .body-grid.single { grid-template-columns: 1fr; }
  .narrow .body-grid { grid-template-columns: 1fr; }

  .panel { background: var(--secondary-background-color, rgba(127,127,127,0.08));
    border-radius: 12px; padding: 10px; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
  .panel.dimmed { opacity: 0.45; }
  .airflow-panel.dimmed { pointer-events: none; }

  .speed-row { display: flex; align-items: center; gap: 8px; }
  .speed-label { flex: 0 0 auto; font-size: 0.78em; font-weight: 600; color: var(--secondary-text-color); }
  .auto-pill { flex: 0 0 auto; border: 1px solid var(--divider-color, rgba(127,127,127,0.35));
    background: transparent; color: var(--secondary-text-color); font-size: 0.74em; font-weight: 700;
    padding: 4px 10px; border-radius: 10px; cursor: pointer; }
  .auto-pill.active { background: color-mix(in srgb, var(--primary-color) 22%, transparent);
    color: var(--primary-color); border-color: transparent; }
  .stepper { display: flex; align-items: center; gap: 8px; margin-left: auto; }
  .step-btn { width: 28px; height: 28px; border-radius: 50%; border: none; cursor: pointer; padding: 0;
    background: var(--card-background-color, rgba(127,127,127,0.14)); color: var(--primary-text-color);
    font-size: 1.05em; font-weight: 700; line-height: 1; display: inline-flex; align-items: center;
    justify-content: center; }
  .speed-value { min-width: 22px; text-align: center; font-size: 1.5em; font-weight: 700;
    color: var(--primary-text-color); }
  .speed-value.dimmed { opacity: 0.5; }
  .speed-row button:disabled, .action-btn:disabled { opacity: 0.5; pointer-events: none; }

  .action-btn { position: relative; overflow: hidden; display: flex; align-items: center;
    justify-content: space-between; width: 100%; min-height: 56px; box-sizing: border-box;
    border-radius: 12px; border: none; cursor: pointer; padding: 0 14px;
    background: var(--card-background-color, rgba(127,127,127,0.14)); color: var(--primary-text-color);
    font-size: 0.92em; font-weight: 600; text-align: left; }
  .action-left { display: inline-flex; align-items: center; gap: 8px; }
  .action-icon { font-size: 1.2em; line-height: 1; }
  .action-right { font-size: 0.82em; opacity: 0.9; white-space: nowrap; }
  .action-btn.accent-boost.active { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .action-btn.accent-fireplace.active { background: var(--warning-color, #e6a23c); color: #fff; }
  .drain-bar { position: absolute; left: 0; bottom: 0; height: 4px;
    background: rgba(255,255,255,0.75); }

  .airflow-svg { width: 100%; height: auto; display: block; overflow: visible; }
  .flow-base { fill: none; stroke-width: 3; opacity: 0.25; }
  .flow-dash { fill: none; stroke-width: 3; stroke-dasharray: 4 8; animation: parmair-flow 1.6s linear infinite; }
  @keyframes parmair-flow { from { stroke-dashoffset: 0; } to { stroke-dashoffset: -24; } }
  @media (prefers-reduced-motion: reduce) { .flow-dash { animation: none; } }
  .core-box { fill: var(--ha-card-background, var(--card-background-color, #fff));
    stroke: var(--divider-color, rgba(127,127,127,0.4)); stroke-width: 1.5; }
  .core-temp { font-size: 12px; font-weight: 700; fill: var(--primary-text-color); }
  .core-label { font-size: 8px; fill: var(--secondary-text-color); }
  .ep-label { font-size: 10px; fill: var(--secondary-text-color); }
  .ep-value { font-size: 12px; font-weight: 600; fill: var(--primary-text-color); }

  .metrics { display: flex; flex-direction: column; gap: 4px; }
  .metric-row { display: flex; align-items: baseline; justify-content: space-between; font-size: 0.84em; }
  .metric-label { color: var(--secondary-text-color); }
  .metric-value { color: var(--primary-text-color); font-weight: 600; }

  .banner { margin: 0 14px 12px; padding: 8px 10px; border-radius: 8px; font-size: 0.82em;
    font-weight: 600; }
  .banner-warn { background: color-mix(in srgb, #ff9800 18%, transparent); color: #ff9800; }
  .banner-error { background: color-mix(in srgb, var(--error-color, #db4437) 16%, transparent);
    color: var(--error-color, #db4437); cursor: pointer; }

  .hint { color: var(--secondary-text-color); padding: 10px 14px 14px; font-size: 0.85em; }
`;

// Cross-flow HRV diagram geometry (viewBox 0 0 280 120). Fresh (top-left)
// and Supply (bottom-right) are one path; Extract (top-right) and Waste
// (bottom-left) are the other. Both diagonals pass through the same center
// point (140,60), which is exactly where the core sits.
const AIRFLOW_FRESH = { x: 25, y: 20 };
const AIRFLOW_EXTRACT = { x: 255, y: 20 };
const AIRFLOW_WASTE = { x: 25, y: 100 };
const AIRFLOW_SUPPLY = { x: 255, y: 100 };
const AIRFLOW_CORE = { x: 140, y: 60 };

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
    // Power button's 2-step "turn off?" confirmation: a deadline timestamp
    // (not just a bare setTimeout) so the visible state is always recomputed
    // from `Date.now()` at render time, however many renders happen while
    // it's pending.
    this._pwrConfirmUntil = null;
    this._pwrRevertTimer = null;
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
    let size = 5;
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

  _speedNumber() {
    const raw = this._raw("fan_speed_state");
    if (raw == null || raw === "") return null;
    const n = Number(raw);
    return Number.isNaN(n) ? null : n;
  }

  // ---- rendering: header ---------------------------------------------------

  _badgeHtml() {
    if (this._isOn("defrosting")) {
      return `<span class="badge badge-cold"><ha-icon icon="mdi:snowflake"></ha-icon>Defrosting</span>`;
    }
    if (this._isOn("summer_mode")) {
      const auto = this._isOn("summer_auto")
        ? `<span class="auto-dot" title="Automatic">ᴬ</span>`
        : "";
      return `<span class="badge badge-summer"><ha-icon icon="mdi:white-balance-sunny"></ha-icon>Summer${auto}</span>`;
    }
    if (this._raw("temperature_mode") === "heating") {
      return `<span class="badge badge-heat"><ha-icon icon="mdi:fire"></ha-icon>Heating</span>`;
    }
    return "";
  }

  _powerHtml() {
    const isOn = this._isOn("fan");
    if (!isOn) {
      // Not on: nothing to confirm, drop any stale pending confirmation.
      this._pwrConfirmUntil = null;
      return `<button type="button" class="pwr-on-pill" data-action="power"><ha-icon icon="mdi:power"></ha-icon> Turn on</button>`;
    }
    const pending = this._pwrConfirmUntil != null && Date.now() < this._pwrConfirmUntil;
    if (pending) {
      return `<button type="button" class="pwr-confirm-pill" data-action="power">Turn off?</button>`;
    }
    return `<button type="button" class="pwr-round" data-action="power" aria-label="Power">
      <ha-icon icon="mdi:power"></ha-icon>
    </button>`;
  }

  _headerHtml() {
    const title = this._config.title || "Ventilation";
    return `<div class="header">
      <span class="title" data-action="title">${esc(title)}</span>
      ${this._badgeHtml()}
      ${this._powerHtml()}
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
      <span class="action-left"><span class="action-icon">${icon}</span><span class="action-label">${esc(label)}</span></span>
      ${rightHtml}
      ${drainHtml}
    </button>`;
  }

  _controlsPanelHtml() {
    const cfg = this._config;
    const parts = [];
    if (cfg.show_fan_row) parts.push(this._speedRowHtml());
    parts.push(this._actionButtonHtml("boost", "⚡", "Boost", "boost_time_remaining", "boost_duration", 180, "accent-boost"));
    parts.push(this._actionButtonHtml("fireplace", "🔥", "Fireplace", "fireplace_time_remaining", "fireplace_duration", 15, "accent-fireplace"));
    const dimmed = !this._isOn("fan") ? " dimmed" : "";
    return `<div class="panel controls-panel${dimmed}">${parts.join("")}</div>`;
  }

  // ---- rendering: airflow panel ---------------------------------------------

  // A small filled triangle pointing along `angleDeg`, placed near (but
  // slightly before) the flow's exit point so it doesn't collide with the
  // endpoint label/value text.
  _arrowheadHtml(x, y, angleDeg, color) {
    return `<polygon class="arrowhead" points="0,-4 8,0 0,4" fill="${color}" transform="translate(${x},${y}) rotate(${angleDeg})"></polygon>`;
  }

  _airflowSvgHtml() {
    const fresh = fmtTemp(this._raw("fresh_air_temperature"));
    const extract = fmtTemp(this._raw("extract_temperature"));
    const waste = fmtTemp(this._raw("waste_temperature"));
    const supply = fmtTemp(this._raw("supply_temperature"));
    const core = fmtTemp(this._raw("supply_temperature_after_hru"));
    const cool = "var(--info-color, #4a90d9)";
    const warm = "var(--warning-color, #e6a23c)";
    // Fresh->Supply diagonal: outdoor (cool) -> indoor (warm). Arrow placed
    // at ~85% along the path, just short of the Supply endpoint.
    const freshSupplyPath = `M${AIRFLOW_FRESH.x},${AIRFLOW_FRESH.y} L${AIRFLOW_SUPPLY.x},${AIRFLOW_SUPPLY.y}`;
    // Extract->Waste diagonal: indoor (warm) -> outdoor (cool).
    const extractWastePath = `M${AIRFLOW_EXTRACT.x},${AIRFLOW_EXTRACT.y} L${AIRFLOW_WASTE.x},${AIRFLOW_WASTE.y}`;
    return `<svg class="airflow-svg" viewBox="0 0 280 120" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <linearGradient id="pg-fs" gradientUnits="userSpaceOnUse" x1="${AIRFLOW_FRESH.x}" y1="${AIRFLOW_FRESH.y}" x2="${AIRFLOW_SUPPLY.x}" y2="${AIRFLOW_SUPPLY.y}">
          <stop offset="0%" stop-color="${cool}"></stop>
          <stop offset="100%" stop-color="${warm}"></stop>
        </linearGradient>
        <linearGradient id="pg-ew" gradientUnits="userSpaceOnUse" x1="${AIRFLOW_EXTRACT.x}" y1="${AIRFLOW_EXTRACT.y}" x2="${AIRFLOW_WASTE.x}" y2="${AIRFLOW_WASTE.y}">
          <stop offset="0%" stop-color="${warm}"></stop>
          <stop offset="100%" stop-color="${cool}"></stop>
        </linearGradient>
      </defs>
      <path class="flow-base" d="${freshSupplyPath}" stroke="url(#pg-fs)"></path>
      <path class="flow-dash" d="${freshSupplyPath}" stroke="url(#pg-fs)"></path>
      <path class="flow-base" d="${extractWastePath}" stroke="url(#pg-ew)"></path>
      <path class="flow-dash" d="${extractWastePath}" stroke="url(#pg-ew)"></path>
      ${this._arrowheadHtml(220.5, 88, 19.1, warm)}
      ${this._arrowheadHtml(59.5, 88, 160.9, cool)}
      <rect class="core-box" x="${AIRFLOW_CORE.x - 18}" y="${AIRFLOW_CORE.y - 18}" width="36" height="36" rx="8" transform="rotate(45 ${AIRFLOW_CORE.x} ${AIRFLOW_CORE.y})"></rect>
      <text class="core-temp" x="${AIRFLOW_CORE.x}" y="${AIRFLOW_CORE.y - 3}" text-anchor="middle">${core}</text>
      <text class="core-label" x="${AIRFLOW_CORE.x}" y="${AIRFLOW_CORE.y + 10}" text-anchor="middle">core</text>
      <text class="ep-label" x="4" y="12">Fresh</text>
      <text class="ep-value" x="4" y="27">${fresh}</text>
      <text class="ep-label" x="276" y="12" text-anchor="end">Extract</text>
      <text class="ep-value" x="276" y="27" text-anchor="end">${extract}</text>
      <text class="ep-label" x="4" y="98">Waste</text>
      <text class="ep-value" x="4" y="113">${waste}</text>
      <text class="ep-label" x="276" y="98" text-anchor="end">Supply</text>
      <text class="ep-value" x="276" y="113" text-anchor="end">${supply}</text>
    </svg>`;
  }

  _metricRowHtml(label, value) {
    return `<div class="metric-row"><span class="metric-label">${esc(label)}</span><span class="metric-value">${esc(value)}</span></div>`;
  }

  _metricsHtml() {
    const rows = [];
    const heatRecovery = this._has("heat_recovery_efficiency") ? `${fmt0(this._raw("heat_recovery_efficiency"))}%` : "–";
    rows.push(this._metricRowHtml("Heat recovery", heatRecovery));
    const speed = this._speedNumber();
    const fanPower = speed == null ? "–" : `${fmt0(speed * 20)}%`;
    rows.push(this._metricRowHtml("Fan power", fanPower));
    if (this._has("hru_humidity")) rows.push(this._metricRowHtml("Humidity", `${fmt0(this._raw("hru_humidity"))}%`));
    if (this._has("co2")) rows.push(this._metricRowHtml("CO₂", `${fmt0(this._raw("co2"))} ppm`));
    return `<div class="metrics">${rows.join("")}</div>`;
  }

  _airflowPanelHtml() {
    const dimmed = !this._isOn("fan") ? " dimmed" : "";
    const parts = [this._airflowSvgHtml()];
    if (this._config.show_metrics) parts.push(this._metricsHtml());
    return `<div class="panel airflow-panel${dimmed}">${parts.join("")}</div>`;
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
      this._card = document.createElement("ha-card");
      shadow.appendChild(this._card);
      this._card.addEventListener("click", (e) => this._onClick(e));
      // Responsive stacking via ResizeObserver instead of a container query:
      // `container-type: inline-size` inside the sections grid can create a
      // layout feedback loop (observed as minutes-long blank paints).
      this._resizeObserver = new ResizeObserver((entries) => {
        const width = entries[0]?.contentRect?.width || 0;
        this._card.classList.toggle("narrow", width > 0 && width < 440);
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

  // Interaction-driven UI state (the power-button confirmation) doesn't come
  // from a hass state change, so it must bypass `_shouldRender`'s diffing —
  // this forces exactly one render regardless of what it reports.
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

  _onClick(e) {
    const el = e.target.closest("[data-action]");
    if (!el) return;
    const action = el.dataset.action;
    const fanId = this._entityId("fan");

    if (action === "title") {
      if (fanId) this._fireMoreInfo(fanId);
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
      show_metrics: "Show heat recovery / fan / humidity / CO₂ metrics",
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
