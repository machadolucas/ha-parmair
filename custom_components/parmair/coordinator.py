"""Polling + write coordinator for the Parmair MAC integration.

One coordinator per config entry, holding the single ``ParmairModbusClient``
connection to the unit. Each poll tick reads the dynamic-register blocks
planned in :meth:`ParmairCoordinator.async_setup` (``registers.build_read_plan``,
gated by the unit's detected :class:`~.capabilities.Capabilities`), decodes
them, and merges in the one-shot static-register snapshot. A block read is
allowed to fail on its own — the previous cycle's values for its keys are kept
— and only a fully-failed cycle (every block failed) raises ``UpdateFailed``;
this matches the controller's own flakiness (see ``modbus.py``) without
flapping every entity to unavailable over one bad block.

Writes are optimistic (the target value is pushed immediately via
``async_set_updated_data``) and followed by a delayed read-back of the block
that reflects the write's effect (``const.VERIFY_KEY``), since some settings
only take effect a moment after being written.

Also owns the summer-mode auto-toggle (:mod:`summer_auto`): the coordinator
holds the enable flag and thresholds (mutable — the summer-auto switch/number entities
drive them) and evaluates them once per cycle, firing a fire-and-forget write
when the dwell-gated logic requests a change.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .capabilities import Capabilities
from .const import (
    CONF_COOKING_SENSORS,
    CONF_SCAN_INTERVAL,
    CONF_SUMMER_AUTO_SOURCE,
    CONNECTION_LOST_THRESHOLD,
    CONTROL_STATE_AWAY,
    CONTROL_STATE_BOOST,
    CONTROL_STATE_HOME,
    COOKING_HEARTBEAT_S,
    COOKING_SAVE_INTERVAL_S,
    COOKING_STORAGE_KEY,
    COOKING_STORAGE_VERSION,
    DEFAULT_COOKING_MIN_BOOST_MIN,
    DEFAULT_COOKING_OFF_DELAY_MIN,
    DEFAULT_COOKING_SENSITIVITY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SUMMER_AUTO_OFF_DWELL_MIN,
    DEFAULT_SUMMER_AUTO_OFF_TEMP_C,
    DEFAULT_SUMMER_AUTO_ON_DWELL_MIN,
    DEFAULT_SUMMER_AUTO_ON_TEMP_C,
    DOMAIN,
    ISSUE_ACTIVE_ALARM,
    ISSUE_CONNECTION_LOST,
    ISSUE_FILTER_CHANGE_DUE,
    MANUFACTURER,
    POWER_STATE_ON,
    POWER_STATE_TURNING_ON,
    SIGNAL_COOKING_UPDATE,
    SPEED_CONTROL_AUTO,
    VERIFY_KEY,
)
from .cooking_detect import (
    DEFAULT_SIGMA_FLOOR,
    CookingDetector,
    CookingParams,
    CookingResult,
    SensorSpec,
)
from .modbus import ParmairConnectionError, ParmairModbusClient
from .registers import ReadBlock, RegisterMap, build_read_plan, decode, encode
from .summer_auto import SummerAutoLogic, SummerAutoParams

_LOGGER = logging.getLogger(__name__)

ParmairData = dict[str, float | int | None]

# Verify-read delay after a write: long enough for the controller to have
# applied the change and for the reflected register(s) to settle.
VERIFY_DELAY = 1.0

# power_state values that count as "unit running" for the cooking auto-boost
# guard — same interpretation the fan platform uses (2 = turning on, 3 = on).
_POWER_ON_STATES = (POWER_STATE_TURNING_ON, POWER_STATE_ON)


def restore_control_state(data: ParmairData) -> int:
    """The control-state value that returns the unit to its prior home/away mode."""
    return CONTROL_STATE_HOME if data.get("home_state") == 1 else CONTROL_STATE_AWAY


class ParmairCoordinator(DataUpdateCoordinator[ParmairData]):
    """Polls one Parmair MAC unit and serializes writes back to it."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ParmairModbusClient,
        register_map: RegisterMap,
        capabilities: Capabilities,
    ) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self._map = register_map
        self._capabilities = capabilities
        self._static_data: ParmairData = {}
        self._read_plan: list[ReadBlock] = []
        self.device_info: DeviceInfo | None = None

        # Health/observability, surfaced in diagnostics.
        self.block_failures = 0
        self.consecutive_full_failures = 0
        self.last_successful_update: datetime | None = None

        self._verify_cancel: Callable[[], None] | None = None

        # Summer-auto: the switch/number entities drive these two attributes; the
        # feature simply stays disabled.
        self.summer_auto_enabled: bool = False
        self.summer_auto_params: SummerAutoParams = SummerAutoParams(
            on_temp_c=DEFAULT_SUMMER_AUTO_ON_TEMP_C,
            on_dwell_min=DEFAULT_SUMMER_AUTO_ON_DWELL_MIN,
            off_temp_c=DEFAULT_SUMMER_AUTO_OFF_TEMP_C,
            off_dwell_min=DEFAULT_SUMMER_AUTO_OFF_DWELL_MIN,
        )
        self._summer_auto_logic = SummerAutoLogic()

        # Cooking detection. The switch/number entities drive the tunables; the
        # detector itself is built only when source sensors are configured (see
        # async_setup_cooking), so the feature stays inert otherwise. Cooking
        # state lives on these attributes — never in ``self.data`` — and reaches
        # its entities via the SIGNAL_COOKING_UPDATE dispatcher.
        self.cooking_params: CookingParams = CookingParams(
            sensitivity=DEFAULT_COOKING_SENSITIVITY,
            off_delay_min=DEFAULT_COOKING_OFF_DELAY_MIN,
        )
        self.cooking_auto_boost_enabled: bool = False
        self.cooking_min_boost_run_min: float = DEFAULT_COOKING_MIN_BOOST_MIN
        self.cooking_detector: CookingDetector | None = None
        self._cooking_boost_owner: bool = False
        self._cooking_boost_started: datetime | None = None
        self._cooking_restore_cancel: Callable[[], None] | None = None
        self._cooking_heartbeat_cancel: Callable[[], None] | None = None
        self._cooking_store: Store | None = None
        self._cooking_last_sent_score: float = 0.0
        # Source entities whose noise floor couldn't be classified at setup (no
        # state yet); reclassified on their first event.
        self._cooking_unclassified: set[str] = set()

    @property
    def read_plan(self) -> list[ReadBlock]:
        """The dynamic-register read plan built in :meth:`async_setup` (diagnostics)."""
        return self._read_plan

    @property
    def static_data(self) -> ParmairData:
        """The one-shot static-register snapshot (diagnostics)."""
        return dict(self._static_data)

    @property
    def capabilities(self) -> Capabilities:
        """The unit's detected capability set (diagnostics)."""
        return self._capabilities

    @property
    def register_map_name(self) -> str:
        """The register map in use, e.g. ``"v1_87"`` (diagnostics)."""
        return self._map.name

    @property
    def cooking_active(self) -> bool:
        """Whether the detector currently holds a cooking detection (False if off)."""
        return self.cooking_detector is not None and self.cooking_detector.active

    @property
    def cooking_score(self) -> float:
        """The detector's most recent fused score (0.0 when the detector is off)."""
        return self.cooking_detector.score if self.cooking_detector is not None else 0.0

    @property
    def cooking_configured(self) -> bool:
        """Whether the options list any cooking source sensors."""
        return bool(self.config_entry.options.get(CONF_COOKING_SENSORS))

    async def async_setup(self) -> None:
        """Connect, snapshot the static registers, and plan the dynamic poll.

        Called once from ``__init__.py`` before the first refresh. Raises
        :class:`~.modbus.ParmairConnectionError` on a failed connect, which
        the caller translates to ``ConfigEntryNotReady``.
        """
        await self.client.connect()

        static_keys = [key for key, definition in self._map.registers.items() if definition.static]
        static_data: ParmairData = {}
        for block in build_read_plan(self._map, static_keys):
            raw = await self.client.read_block(block.address, block.count)
            static_data.update(self._decode_block(block, raw))
        self._static_data = static_data

        included = self._capabilities.included_keys(self._map)
        dynamic_keys = [key for key in included if not self._map.registers[key].static]
        self._read_plan = build_read_plan(self._map, dynamic_keys)

        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self.config_entry.entry_id)},
            manufacturer=MANUFACTURER,
            model=self._capabilities.model_name,
            sw_version=self._capabilities.sw_version,
            hw_version=self._capabilities.fw_version,
            name=self.config_entry.title,
        )

    def _decode_block(self, block: ReadBlock, raw: list[int]) -> ParmairData:
        """Decode one block's raw words into its defined register values."""
        result: ParmairData = {}
        for key in block.keys:
            definition = self._map.registers[key]
            offset = self._map.address(definition) - block.address
            result[key] = decode(raw[offset], definition)
        return result

    def _find_block(self, key: str) -> ReadBlock | None:
        """The dynamic-plan block that would refresh ``key``, if any."""
        for block in self._read_plan:
            if key in block.keys:
                return block
        return None

    async def _async_update_data(self) -> ParmairData:
        data: ParmairData = dict(self.data) if self.data is not None else {}
        data.update(self._static_data)

        failed_blocks = 0
        for block in self._read_plan:
            try:
                raw = await self.client.read_block(block.address, block.count)
            except ParmairConnectionError as err:
                failed_blocks += 1
                self.block_failures += 1
                _LOGGER.debug("Parmair block %s/%s failed: %s", block.address, block.count, err)
                # Keep whatever we had (None on a first-cycle failure).
                for key in block.keys:
                    data.setdefault(key, None)
                continue
            data.update(self._decode_block(block, raw))

        if self._read_plan and failed_blocks == len(self._read_plan):
            self.consecutive_full_failures += 1
            self._update_repairs(data)
            raise UpdateFailed(f"all {failed_blocks} Parmair read block(s) failed")

        self.consecutive_full_failures = 0
        self.last_successful_update = dt_util.utcnow()
        self._update_repairs(data)
        self._evaluate_summer_auto(data)
        self._reconcile_cooking_boost(data)
        return data

    # ── Repairs ──────────────────────────────────────────────────────────

    def _update_repairs(self, data: ParmairData) -> None:
        """Raise/clear the three coordinator-owned repair issues."""
        if data.get("filter_state") == 0 or data.get("alarm_state") == 2:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_FILTER_CHANGE_DUE,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="filter_change_due",
                translation_placeholders={"next_change": self._next_filter_change(data)},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_FILTER_CHANGE_DUE)

        if data.get("summary_alarm") == 1:
            count = data.get("active_alarm_count")
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_ACTIVE_ALARM,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="active_alarm",
                translation_placeholders={"count": str(count) if count is not None else "?"},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_ACTIVE_ALARM)

        if self.consecutive_full_failures >= CONNECTION_LOST_THRESHOLD:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_CONNECTION_LOST,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="connection_lost",
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_CONNECTION_LOST)

    @staticmethod
    def _next_filter_change(data: ParmairData) -> str:
        day = data.get("filter_next_day")
        month = data.get("filter_next_month")
        year = data.get("filter_next_year")
        if day is None or month is None or year is None:
            return "?"
        return f"{int(day)}.{int(month)}.{int(year)}"

    # ── Summer-auto ──────────────────────────────────────────────────────

    def _evaluate_summer_auto(self, data: ParmairData) -> None:
        """Run the dwell-gated summer-mode logic for one tick.

        Deliberately defensive (broad except): a bug here must never break
        the polling cycle that carries every other entity's data.
        """
        if not self.summer_auto_enabled:
            self._summer_auto_logic.reset()
            return
        try:
            source_entity = self.config_entry.options.get(CONF_SUMMER_AUTO_SOURCE)
            temp = (
                self._read_source_temperature(source_entity)
                if source_entity
                else self._as_float(data.get("fresh_air_temperature"))
            )
            summer_on = bool(data.get("summer_mode"))
            action = self._summer_auto_logic.update(
                dt_util.utcnow(), temp, summer_on, self.summer_auto_params
            )
            if action is not None and action != summer_on:
                self.hass.async_create_task(self.async_write("summer_mode", int(action)))
        except Exception:  # noqa: BLE001 - never let this break the update cycle
            _LOGGER.exception("Parmair summer-auto evaluation failed")

    def _read_source_temperature(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        return self._as_float(state.state)

    @staticmethod
    def _as_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ── Cooking detection ────────────────────────────────────────────────

    async def async_setup_cooking(self) -> None:
        """Build the detector and subscribe its listeners, if any sensors are set.

        Called once from ``__init__.async_setup_entry`` after the first refresh
        (so ``self.data`` is populated for the boost guard) and before platforms
        are forwarded. A no-op when the option is empty — the detector stays
        ``None`` and every cooking property/entity reads inert. Listener/timer
        teardown rides the config-entry unload (options changes reload the entry).
        """
        entity_ids: list[str] = self.config_entry.options.get(CONF_COOKING_SENSORS) or []
        if not entity_ids:
            return

        specs: dict[str, SensorSpec] = {}
        for entity_id in entity_ids:
            floor = self._cooking_sigma_floor(entity_id)
            if floor is None:
                # No state yet (e.g. ESPHome sensor still booting): seed the
                # fallback floor and reclassify on the sensor's first event.
                self._cooking_unclassified.add(entity_id)
                floor = DEFAULT_SIGMA_FLOOR
            specs[entity_id] = SensorSpec(sigma_floor=floor)
        self.cooking_detector = CookingDetector(specs)

        self._cooking_store = Store(
            self.hass,
            COOKING_STORAGE_VERSION,
            COOKING_STORAGE_KEY.format(self.config_entry.entry_id),
        )
        stored = await self._cooking_store.async_load()
        if stored:
            self.cooking_detector.restore(stored, dt_util.utcnow())

        self.config_entry.async_on_unload(
            async_track_state_change_event(self.hass, entity_ids, self._async_cooking_event)
        )
        self.config_entry.async_on_unload(
            async_track_time_interval(
                self.hass,
                self._async_cooking_save,
                timedelta(seconds=COOKING_SAVE_INTERVAL_S),
            )
        )

    def _cooking_sigma_floor(self, entity_id: str) -> float | None:
        """Classify a source sensor's noise floor from its device_class/unit.

        Returns ``None`` when the entity has no state yet, so the caller can
        retry on the first event. Floors mirror the plan's per-signal noise
        estimates; unitless Sensirion VOC/NOx indices fall through to the
        detector's default.
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        device_class = state.attributes.get("device_class")
        unit = state.attributes.get("unit_of_measurement")
        if device_class == "humidity" or unit == "%":
            return 1.0
        if unit in ("µg/m³", "μg/m³"):  # both micro-sign code points seen in the wild
            return 1.0
        if device_class == "carbon_dioxide" or unit == "ppm":
            return 25.0
        return DEFAULT_SIGMA_FLOOR

    @callback
    def _async_cooking_event(self, event: Event[EventStateChangedData]) -> None:
        """Feed one source-sensor state change into the detector.

        Broad-except like :meth:`_evaluate_summer_auto`: a malformed state must
        never tear down the state-change listener.
        """
        if self.cooking_detector is None:
            return
        try:
            entity_id = event.data["entity_id"]
            state = event.data.get("new_state")
            if entity_id in self._cooking_unclassified:
                floor = self._cooking_sigma_floor(entity_id)
                if floor is not None:
                    self.cooking_detector.set_sigma_floor(entity_id, floor)
                    self._cooking_unclassified.discard(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                value: float | None = None
            else:
                value = self._as_float(state.state)
            result = self.cooking_detector.update(
                dt_util.utcnow(), entity_id, value, self.cooking_params
            )
            self._handle_cooking_result(result)
        except Exception:  # noqa: BLE001 - never let a bad state break the listener
            _LOGGER.exception("Parmair cooking event handling failed")

    @callback
    def _async_cooking_heartbeat(self, _now: datetime) -> None:
        """Tick the state machine while a detection is active (all-silent timeout)."""
        if self.cooking_detector is None:
            return
        try:
            result = self.cooking_detector.tick(dt_util.utcnow(), self.cooking_params)
            self._handle_cooking_result(result)
        except Exception:  # noqa: BLE001 - never let a tick break the heartbeat timer
            _LOGGER.exception("Parmair cooking heartbeat failed")

    @callback
    def _handle_cooking_result(self, result: CookingResult) -> None:
        """React to one detector outcome: heartbeat, auto-boost, and dispatch."""
        if result.transition is True:
            self._start_cooking_heartbeat()
            if self._cooking_restore_cancel is not None:
                # Re-triggered during the min-boost restore delay: the boost we
                # own is still on, so just cancel the pending restore and keep it.
                self._cooking_restore_cancel()
                self._cooking_restore_cancel = None
            elif self.cooking_auto_boost_enabled:
                self.hass.async_create_task(self._async_cooking_boost_start())
        elif result.transition is False:
            self._stop_cooking_heartbeat()
            if self._cooking_boost_owner:
                self._schedule_cooking_restore()

        # Dispatcher-only: cooking entities never ride async_set_updated_data
        # (that would fake a register poll every ~2 s). Nudge on every edge and
        # on a meaningful score move.
        score_moved = abs(result.score - self._cooking_last_sent_score) >= 0.05
        if result.transition is not None or score_moved:
            self._cooking_last_sent_score = result.score
            async_dispatcher_send(
                self.hass, SIGNAL_COOKING_UPDATE.format(self.config_entry.entry_id)
            )

    def _start_cooking_heartbeat(self) -> None:
        if self._cooking_heartbeat_cancel is not None:
            return
        self._cooking_heartbeat_cancel = async_track_time_interval(
            self.hass, self._async_cooking_heartbeat, timedelta(seconds=COOKING_HEARTBEAT_S)
        )

    def _stop_cooking_heartbeat(self) -> None:
        if self._cooking_heartbeat_cancel is not None:
            self._cooking_heartbeat_cancel()
            self._cooking_heartbeat_cancel = None

    async def _async_cooking_boost_start(self) -> None:
        """Claim the boost for a fresh detection — only if safe to own it.

        Skips (no ownership) when the unit is off or boost is already active
        (manual or CO₂-auto): turning that boost off later isn't ours to do.
        Ownership is set only after the write succeeds, so a failed write leaves
        no phantom claim.
        """
        data = self.data
        if not data:
            return
        if data.get("boost_active") or data.get("power_state") not in _POWER_ON_STATES:
            return
        try:
            await self.async_write_sequence(
                [("speed_control", SPEED_CONTROL_AUTO), ("control_state", CONTROL_STATE_BOOST)]
            )
        except ParmairConnectionError as err:
            _LOGGER.warning("Parmair cooking auto-boost start failed: %s", err)
            return
        self._cooking_boost_owner = True
        self._cooking_boost_started = dt_util.utcnow()

    def _schedule_cooking_restore(self) -> None:
        """Restore the prior mode after the minimum boost run-time has elapsed."""
        elapsed = (
            (dt_util.utcnow() - self._cooking_boost_started).total_seconds()
            if self._cooking_boost_started is not None
            else 0.0
        )
        delay = max(0.0, self.cooking_min_boost_run_min * 60.0 - elapsed)
        if self._cooking_restore_cancel is not None:
            self._cooking_restore_cancel()
        self._cooking_restore_cancel = async_call_later(
            self.hass, delay, self._async_cooking_restore
        )

    async def _async_cooking_restore(self, _now: datetime) -> None:
        """Return the unit to home/away after a cooking auto-boost.

        Re-checks ownership and that the boost we started is still active: if the
        user switched to fireplace/manual/off or the unit's timer already ended
        the boost, we just drop ownership and write nothing.
        """
        self._cooking_restore_cancel = None
        data = self.data
        if not self._cooking_boost_owner or not data or not data.get("boost_active"):
            self._cooking_boost_owner = False
            self._cooking_boost_started = None
            return
        try:
            await self.async_write("control_state", restore_control_state(data))
        except ParmairConnectionError as err:
            _LOGGER.warning("Parmair cooking auto-boost restore failed: %s", err)
        self._cooking_boost_owner = False
        self._cooking_boost_started = None

    def _reconcile_cooking_boost(self, data: ParmairData) -> None:
        """Drop boost ownership once the boost we started is gone.

        Respects a manual boost-off or the unit's own boost timer expiring —
        we stop trying to restore a boost that's no longer there. Tiny and
        non-raising by construction (runs inside the poll cycle).
        """
        if self._cooking_boost_owner and not data.get("boost_active"):
            self._cooking_boost_owner = False
            self._cooking_boost_started = None
            if self._cooking_restore_cancel is not None:
                self._cooking_restore_cancel()
                self._cooking_restore_cancel = None

    async def _async_cooking_save(self, _now: datetime) -> None:
        """Persist learned baselines on the periodic interval."""
        if self.cooking_detector is None or self._cooking_store is None:
            return
        try:
            await self._cooking_store.async_save(self.cooking_detector.snapshot(dt_util.utcnow()))
        except Exception:  # noqa: BLE001 - a failed save must not break the timer
            _LOGGER.warning("Parmair cooking baseline save failed", exc_info=True)

    # ── Writes ───────────────────────────────────────────────────────────

    async def async_write(self, key: str, value: float | int) -> None:
        """Write one register and optimistically update + schedule a verify."""
        await self._write_one(key, value)
        self._schedule_verify(VERIFY_KEY.get(key, key))

    async def async_write_sequence(self, pairs: list[tuple[str, float | int]]) -> None:
        """Write several registers in sequence, verifying only the last one."""
        for key, value in pairs:
            await self._write_one(key, value)
        if pairs:
            last_key = pairs[-1][0]
            self._schedule_verify(VERIFY_KEY.get(last_key, last_key))

    async def _write_one(self, key: str, value: float | int) -> None:
        definition = self._map.registers.get(key)
        if definition is None or not definition.writable:
            raise ValueError(f"{key!r} is not a writable Parmair register")
        raw = encode(value, definition)
        await self.client.write_register(self._map.address(definition), raw)
        updated: ParmairData = dict(self.data) if self.data is not None else {}
        updated[key] = decode(raw, definition)
        self.async_set_updated_data(updated)

    def _schedule_verify(self, verify_key: str) -> None:
        if self._verify_cancel is not None:
            self._verify_cancel()
        self._verify_cancel = async_call_later(
            self.hass, VERIFY_DELAY, partial(self._async_verify, verify_key)
        )

    async def _async_verify(self, verify_key: str, _now: datetime) -> None:
        self._verify_cancel = None
        block = self._find_block(verify_key)
        if block is None:
            return
        try:
            raw = await self.client.read_block(block.address, block.count)
        except ParmairConnectionError as err:
            _LOGGER.debug("Parmair verify read for %s failed: %s", verify_key, err)
            return
        updated: ParmairData = dict(self.data) if self.data is not None else {}
        updated.update(self._decode_block(block, raw))
        self.async_set_updated_data(updated)

    async def async_shutdown(self) -> None:
        """Cancel pending timers, flush cooking baselines, then base shutdown."""
        if self._verify_cancel is not None:
            self._verify_cancel()
            self._verify_cancel = None
        self._stop_cooking_heartbeat()
        if self._cooking_restore_cancel is not None:
            self._cooking_restore_cancel()
            self._cooking_restore_cancel = None
        if self.cooking_detector is not None and self._cooking_store is not None:
            try:
                await self._cooking_store.async_save(
                    self.cooking_detector.snapshot(dt_util.utcnow())
                )
            except Exception:  # noqa: BLE001 - best-effort flush on unload
                _LOGGER.warning("Parmair cooking baseline final save failed", exc_info=True)
        await super().async_shutdown()


type ParmairConfigEntry = ConfigEntry[ParmairCoordinator]
