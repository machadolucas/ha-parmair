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
 * sensor just doesn't show that chip):
 *
 *   fan (domain fan)
 *   sensor.*      fresh_air_temperature, supply_temperature,
 *                 extract_temperature, waste_temperature,
 *                 heat_recovery_efficiency, hru_humidity, co2,
 *                 boost_time_remaining, fireplace_time_remaining,
 *                 fan_speed_state, control_state, alarm_state,
 *                 filter_next_change, temperature_mode
 *   binary_sensor.* defrosting, filter_change_required, alarm
 *   switch.*      boost, fireplace, summer_mode, summer_auto
 *   button.*      acknowledge_alarms
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

// One decimal place; "–" for anything that isn't a finite number (covers
// null/undefined/"unknown"/"unavailable" alike).
function fmt1(v) {
  if (v == null || v === "") return "–";
  const n = Number(v);
  return Number.isNaN(n) ? "–" : n.toFixed(1);
}

// Nearest whole number; same "–" fallback as fmt1.
function fmt0(v) {
  if (v == null || v === "") return "–";
  const n = Number(v);
  return Number.isNaN(n) ? "–" : String(Math.round(n));
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
  .pwr-btn { flex: 0 0 auto; display: inline-flex; align-items: center; justify-content: center;
    width: 32px; height: 32px; border-radius: 50%; border: none; cursor: pointer; padding: 0;
    background: var(--secondary-background-color, rgba(127,127,127,0.15));
    color: var(--secondary-text-color); }
  .pwr-btn.on { background: color-mix(in srgb, var(--primary-color) 22%, transparent);
    color: var(--primary-color); }
  .pwr-btn ha-icon { --mdc-icon-size: 18px; }

  .badge { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 4px; font-size: 0.76em;
    font-weight: 600; padding: 3px 9px; border-radius: 10px; white-space: nowrap; }
  .badge ha-icon { --mdc-icon-size: 14px; }
  .badge-cold { background: color-mix(in srgb, #2196f3 20%, transparent); color: #2196f3; }
  .badge-summer { background: color-mix(in srgb, #ff9800 20%, transparent); color: #ff9800; }
  .badge-heat { background: color-mix(in srgb, var(--error-color, #db4437) 18%, transparent);
    color: var(--error-color, #db4437); }
  .auto-dot { margin-left: 3px; font-size: 0.7em; opacity: 0.85; }

  .fan-row { padding: 0 14px 10px; display: flex; flex-direction: column; gap: 8px; }
  .segmented { display: flex; border-radius: 10px; overflow: hidden;
    border: 1px solid var(--divider-color, rgba(127,127,127,0.3)); }
  .seg { flex: 1 1 0; border: none; background: transparent; padding: 7px 0; cursor: pointer;
    font-size: 0.86em; font-weight: 600; color: var(--secondary-text-color);
    border-right: 1px solid var(--divider-color, rgba(127,127,127,0.3)); }
  .seg:last-child { border-right: none; }
  .seg.active { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .seg.subtle { color: var(--primary-color); }

  .switch-chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .switch-chip { display: inline-flex; align-items: center; gap: 5px; border: none; cursor: pointer;
    font-size: 0.78em; font-weight: 600; padding: 4px 10px; border-radius: 10px;
    background: var(--secondary-background-color, rgba(127,127,127,0.15));
    color: var(--secondary-text-color); }
  .switch-chip.active { background: color-mix(in srgb, var(--primary-color) 22%, transparent);
    color: var(--primary-color); }
  .countdown { font-size: 0.82em; opacity: 0.85; }

  .temp-row { display: flex; gap: 10px; padding: 2px 14px 8px; }
  .temp-col { flex: 1 1 0; display: flex; align-items: center; gap: 5px; font-size: 0.84em;
    color: var(--primary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .arrow { color: var(--secondary-text-color); flex: 0 0 auto; }

  .chips-row { display: flex; gap: 6px; flex-wrap: wrap; padding: 0 14px 10px; }
  .chip { font-size: 0.74em; font-weight: 600; padding: 2px 9px; border-radius: 8px;
    background: var(--secondary-background-color, rgba(127,127,127,0.15));
    color: var(--secondary-text-color); }

  .banner { margin: 0 14px 12px; padding: 8px 10px; border-radius: 8px; font-size: 0.82em;
    font-weight: 600; }
  .banner-warn { background: color-mix(in srgb, #ff9800 18%, transparent); color: #ff9800; }
  .banner-error { background: color-mix(in srgb, var(--error-color, #db4437) 16%, transparent);
    color: var(--error-color, #db4437); cursor: pointer; }

  .hint { color: var(--secondary-text-color); padding: 10px 14px 14px; font-size: 0.85em; }
`;

class ParmairCard extends HTMLElement {
  setConfig(config) {
    this._config = {
      show_fan_row: true,
      show_temperatures: true,
      show_chips: true,
      show_alerts: true,
      compact: false,
      ...(config || {}),
    };
    // key -> entity_id, built lazily once the registries load; null means
    // "not resolved yet", {} means "resolved, nothing found".
    this._entities = null;
    this._entitiesPromise = null;
    this._lastStates = null;
    this._lastConfigSig = null;
    this._lastEntitiesReady = null;
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
    if (this._config && this._config.compact) return 2;
    let size = 3;
    if (this._config && this._config.show_temperatures) size += 1;
    if (this._config && this._config.show_chips) size += 1;
    return size;
  }

  getGridOptions() {
    return { rows: this._config && this._config.compact ? 2 : "auto", columns: 12, min_columns: 6 };
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

  // ---- rendering ----------------------------------------------------------

  _badgeHtml() {
    if (this._isOn("defrosting")) {
      return `<span class="badge badge-cold"><ha-icon icon="mdi:snowflake"></ha-icon>Defrosting</span>`;
    }
    if (this._isOn("summer_mode")) {
      const auto = this._isOn("summer_auto")
        ? `<span class="auto-dot" title="Automatic">A</span>`
        : "";
      return `<span class="badge badge-summer"><ha-icon icon="mdi:white-balance-sunny"></ha-icon>Summer${auto}</span>`;
    }
    if (this._raw("temperature_mode") === "heating") {
      return `<span class="badge badge-heat"><ha-icon icon="mdi:fire"></ha-icon>Heating</span>`;
    }
    return "";
  }

  _headerHtml() {
    const title = this._config.title || "Ventilation";
    const isOn = this._isOn("fan");
    return `<div class="header">
      <span class="title" data-action="title">${esc(title)}</span>
      <button type="button" class="pwr-btn${isOn ? " on" : ""}" data-action="power" aria-label="Power">
        <ha-icon icon="mdi:power"></ha-icon>
      </button>
      ${this._badgeHtml()}
    </div>`;
  }

  _fanRowHtml() {
    const controlState = this._raw("control_state");
    const isManual = controlState === "manual";
    const autoActive = controlState != null && controlState !== "manual";
    const speedRaw = this._raw("fan_speed_state");
    const speed = speedRaw != null && speedRaw !== "" ? Number(speedRaw) : null;

    const segs = [
      `<button type="button" class="seg${autoActive ? " active" : ""}" data-action="preset-auto">A</button>`,
    ];
    for (let n = 1; n <= 5; n++) {
      const isCurrent = speed === n;
      let cls = "";
      if (isManual && isCurrent) cls = " active";
      else if (isCurrent) cls = " subtle";
      segs.push(`<button type="button" class="seg${cls}" data-action="speed" data-speed="${n}">${n}</button>`);
    }

    return `<div class="fan-row">
      <div class="segmented">${segs.join("")}</div>
      ${this._switchChipsHtml()}
    </div>`;
  }

  _switchChipHtml(key, label, timerKey) {
    if (!this._has(key)) return "";
    const on = this._isOn(key);
    let badge = "";
    if (timerKey && this._has(timerKey)) {
      const remaining = Number(this._raw(timerKey));
      if (!Number.isNaN(remaining) && remaining > 0) {
        badge = `<span class="countdown">${Math.round(remaining)} min</span>`;
      }
    }
    return `<button type="button" class="switch-chip${on ? " active" : ""}" data-action="toggle" data-switch="${key}">${esc(label)}${badge}</button>`;
  }

  _switchChipsHtml() {
    const chips = [
      this._switchChipHtml("boost", "Boost", "boost_time_remaining"),
      this._switchChipHtml("fireplace", "Fireplace", "fireplace_time_remaining"),
      this._switchChipHtml("summer_mode", "Summer", null),
    ].join("");
    return chips ? `<div class="switch-chips">${chips}</div>` : "";
  }

  _tempRowHtml() {
    return `<div class="temp-row">
      <div class="temp-col"><span>Fresh ${fmt1(this._raw("fresh_air_temperature"))}°</span><span class="arrow">→</span><span>Supply ${fmt1(this._raw("supply_temperature"))}°</span></div>
      <div class="temp-col"><span>Extract ${fmt1(this._raw("extract_temperature"))}°</span><span class="arrow">→</span><span>Waste ${fmt1(this._raw("waste_temperature"))}°</span></div>
    </div>`;
  }

  _chipsRowHtml() {
    const chips = [];
    if (this._has("heat_recovery_efficiency")) {
      chips.push(`<span class="chip">${fmt0(this._raw("heat_recovery_efficiency"))}%</span>`);
    }
    if (this._has("hru_humidity")) {
      chips.push(`<span class="chip">${fmt0(this._raw("hru_humidity"))}% RH</span>`);
    }
    if (this._has("co2")) {
      chips.push(`<span class="chip">${fmt0(this._raw("co2"))} ppm CO₂</span>`);
    }
    return chips.length ? `<div class="chips-row">${chips.join("")}</div>` : "";
  }

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
      return sections.join("");
    }
    if (cfg.show_fan_row) sections.push(this._fanRowHtml());
    if (!cfg.compact) {
      if (cfg.show_temperatures) sections.push(this._tempRowHtml());
      if (cfg.show_chips) sections.push(this._chipsRowHtml());
      if (cfg.show_alerts) sections.push(this._alertHtml());
    }
    return sections.join("");
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
      this._card = document.createElement("ha-card");
      this.appendChild(this._card);
      this._card.addEventListener("click", (e) => this._onClick(e));
    }
    if (!this._shouldRender()) return;
    this._card.innerHTML = `<style>${CARD_CSS}</style>${this._bodyHtml()}`;
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
      if (!fanId) return;
      const isOn = this._isOn("fan");
      this._callService("fan", isOn ? "turn_off" : "turn_on", { entity_id: fanId });
      return;
    }
    if (action === "preset-auto") {
      if (fanId) this._callService("fan", "set_preset_mode", { entity_id: fanId, preset_mode: "home" });
      return;
    }
    if (action === "speed") {
      if (!fanId) return;
      const n = Number(el.dataset.speed);
      if (!Number.isNaN(n)) {
        this._callService("fan", "set_percentage", { entity_id: fanId, percentage: n * 20 });
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
      { name: "show_fan_row", selector: { boolean: {} } },
      { name: "show_temperatures", selector: { boolean: {} } },
      { name: "show_chips", selector: { boolean: {} } },
      { name: "show_alerts", selector: { boolean: {} } },
      { name: "compact", selector: { boolean: {} } },
    ];
  }

  _labels() {
    return {
      device_id: "Parmair device (auto-detected if empty)",
      title: "Title (optional)",
      show_fan_row: "Show fan speed row",
      show_temperatures: "Show temperatures",
      show_chips: "Show efficiency / humidity / CO₂ chips",
      show_alerts: "Show filter / alarm banners",
      compact: "Compact (header + fan row only)",
    };
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
      show_fan_row: this._config.show_fan_row !== false,
      show_temperatures: this._config.show_temperatures !== false,
      show_chips: this._config.show_chips !== false,
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
