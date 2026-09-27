"""Polling + write coordinator for the Parmair MAC integration.

One coordinator per config entry, holding the single ``ParmairModbusClient``
that wraps the Modbus unit borrowed from the ``modbus`` integration (see
``__init__.py``). Each poll tick reads the dynamic-register blocks
planned in :meth:`ParmairCoordinator.async_setup` (``registers.build_read_plan``,
gated by the unit's detected :class:`~.capabilities.Capabilities`), decodes
them, and merges in the one-shot static-register snapshot. A block read is
allowed to fail on its own — the previous cycle's values for its keys are kept
— and only a fully-failed cycle (every block failed) raises ``UpdateFailed``;
this matches the controller's own flakiness (see ``modbus.py``) without
flapping every entity to unavailable over one bad block. The exception is a
block that fails because the link could not even be warmed up
(``ParmairConnectionError.link_unreachable``): the unit is gone, so the rest of
the cycle is marked failed without spending a full retry budget on each block.

Writes are optimistic (the target value is pushed immediately via
``async_set_updated_data``) and followed by a delayed read-back of the block
that reflects the write's effect (``const.VERIFY_KEY``), since some settings
only take effect a moment after being written. Write sequences are atomic
against each other (``_write_lock``; polls don't take it), and a sequence
counter stamps every key a write or verify-read sets, so a poll block that was
read *before* that landed cannot overwrite the fresher value at the end of the
cycle.

Also owns the summer-mode auto-toggle (:mod:`summer_auto`): the coordinator
holds the enable flag and thresholds (mutable — the summer-auto switch/number entities
drive them) and evaluates them once per cycle, firing a background write
when the dwell-gated logic requests a change.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateReportedData, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .capabilities import Capabilities
from .const import (
    CONF_COOKING_SENSORS,
    CONF_SCAN_INTERVAL,
    CONF_SUMMER_AUTO_SOURCE,
    CONNECTION_LOST_THRESHOLD,
    CONTROL_STATE_AWAY,
    CONTROL_STATE_BOOST,
    CONTROL_STATE_FIREPLACE,
    CONTROL_STATE_HOME,
    CONTROL_STATE_MANUAL,
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

# A user write to any of these changes the unit's mode, which ends any claim
# the cooking auto-boost holds: its pending restore would otherwise overwrite
# the user's choice (e.g. a fireplace selection) when it fires.
_MODE_KEYS = frozenset({"control_state", "speed_control", "power_state"})

# What the unit derives into boost_active/fireplace_active from a
# control_state write. Patched in optimistically so the boost/fireplace
# switches don't bounce back to off until the verify-read lands; the
# verify-read then replaces the guess with the truth.
_CONTROL_STATE_DERIVED: dict[int, dict[str, int]] = {
    CONTROL_STATE_AWAY: {"boost_active": 0, "fireplace_active": 0},
    CONTROL_STATE_HOME: {"boost_active": 0, "fireplace_active": 0},
    CONTROL_STATE_BOOST: {"boost_active": 1, "fireplace_active": 0},
    CONTROL_STATE_FIREPLACE: {"boost_active": 0, "fireplace_active": 1},
}

_ISSUE_BASES = (ISSUE_FILTER_CHANGE_DUE, ISSUE_ACTIVE_ALARM, ISSUE_CONNECTION_LOST)


def restore_control_state(data: ParmairData) -> int:
    """The control-state value that returns the unit to its prior home/away mode."""
    return CONTROL_STATE_HOME if data.get("home_state") == 1 else CONTROL_STATE_AWAY


def issue_id(base: str, entry_id: str) -> str:
    """This entry's id for one repair issue (``base`` is also its translation key).

    Per entry, so with two units one entry's poll never deletes the issue the
    other just raised.
    """
    return f"{base}_{entry_id}"


@callback
def async_delete_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Delete every repair issue this entry may have raised."""
    for base in _ISSUE_BASES:
        ir.async_delete_issue(hass, DOMAIN, issue_id(base, entry_id))


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
        # Blocks to re-read when the verify timer fires, by address: a second
        # write before it fires adds its block instead of replacing the first.
        self._pending_verify: dict[int, ReadBlock] = {}

        # Poll/write race guard: every write or verify-read bumps _seq and
        # stamps the keys it set; a poll keeps the current value for any key
        # stamped after its block's read began (see _async_update_data).
        self._seq = 0
        self._key_seq: dict[str, int] = {}
        # Makes a multi-register write sequence atomic against another one
        # (e.g. a cooking restore landing between a user's speed_control and
        # control_state writes). Polls don't take it.
        self._write_lock = asyncio.Lock()
        # Set first thing in async_shutdown: no new timers, tasks or writes.
        self._shutting_down = False

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
        # Whether a poll has shown the claimed boost as active yet: until then
        # a boost_active of 0 may just be a read that predates our write.
        self._cooking_boost_confirmed: bool = False
        # Bumped whenever the claim is dropped or a user takes the mode over,
        # so a boost start that was already queued can tell it lost the race.
        self._cooking_epoch = 0
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
        """Settle the link, snapshot the static registers, plan the dynamic poll.

        Called once from ``__init__.py`` before the first refresh. The
        ``client.connect()`` below is the first round trip to the unit —
        borrowing one does no I/O — so it raises
        :class:`~.modbus.ParmairConnectionError` when the unit is unreachable,
        which the caller translates to ``ConfigEntryNotReady``.
        """
        await self.client.connect()

        # Issue ids used to be global (shared by every entry); a leftover from
        # before the per-entry ids would otherwise never be cleared.
        for base in _ISSUE_BASES:
            ir.async_delete_issue(self.hass, DOMAIN, base)

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
        poll_started = self._seq
        data: ParmairData = dict(self.data) if self.data is not None else {}
        data.update(self._static_data)

        # seq at the moment each successfully read key's block read began.
        read_at: dict[str, int] = {}
        failed_keys: set[str] = set()
        failed_blocks = 0
        unreachable = False
        for block in self._read_plan:
            if unreachable:
                # The unit did not answer even a warm-up: every remaining
                # block would burn the same ~32 s retry budget to learn the
                # same thing, so count them failed without any I/O.
                failed_blocks += 1
                self._fail_block(block, data, failed_keys)
                continue
            started = self._seq
            try:
                raw = await self.client.read_block(block.address, block.count)
            except ParmairConnectionError as err:
                failed_blocks += 1
                self._fail_block(block, data, failed_keys)
                _LOGGER.debug("Parmair block %s/%s failed: %s", block.address, block.count, err)
                if err.link_unreachable:
                    unreachable = True
                    _LOGGER.debug("Parmair unit unreachable; skipping the rest of this cycle")
                continue
            decoded = self._decode_block(block, raw)
            data.update(decoded)
            for key in decoded:
                read_at[key] = started

        self._keep_fresher_values(data, read_at, poll_started)

        if self._read_plan and failed_blocks == len(self._read_plan):
            self.consecutive_full_failures += 1
            self._update_repairs(data)
            raise UpdateFailed(f"all {failed_blocks} Parmair read block(s) failed")

        self.consecutive_full_failures = 0
        self.last_successful_update = dt_util.utcnow()
        self._update_repairs(data)
        self._reconcile_cooking_boost(data)
        self._evaluate_summer_auto(data, failed_keys)
        # Again: the summer-auto write above is started eagerly and may already
        # have pushed its optimistic value, which this cycle must not undo.
        self._keep_fresher_values(data, read_at, poll_started)
        return data

    def _fail_block(self, block: ReadBlock, data: ParmairData, failed_keys: set[str]) -> None:
        """Account one failed block: keep what we had (None on a first-cycle failure)."""
        self.block_failures += 1
        for key in block.keys:
            data.setdefault(key, None)
        failed_keys.update(block.keys)

    def _keep_fresher_values(
        self, data: ParmairData, read_at: dict[str, int], poll_started: int
    ) -> None:
        """Undo the poll's overwrite of anything a write/verify set after it read.

        Blocks are read with the client lock released in between, so a write's
        optimistic value (or a verify-read) can land mid-cycle; the block read
        that began before it carries the older value. A key not read this
        cycle carries the value copied at poll start, so it compares against
        that instead.
        """
        current = self.data or {}
        for key, stamped in self._key_seq.items():
            if stamped > read_at.get(key, poll_started) and key in current:
                data[key] = current[key]

    # ── Repairs ──────────────────────────────────────────────────────────

    def _update_repairs(self, data: ParmairData) -> None:
        """Raise/clear the three coordinator-owned repair issues."""
        entry_id = self.config_entry.entry_id
        if data.get("filter_state") == 0 or data.get("alarm_state") == 2:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id(ISSUE_FILTER_CHANGE_DUE, entry_id),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="filter_change_due",
                translation_placeholders={"next_change": self._next_filter_change(data)},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id(ISSUE_FILTER_CHANGE_DUE, entry_id))

        if data.get("summary_alarm") == 1:
            count = data.get("active_alarm_count")
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id(ISSUE_ACTIVE_ALARM, entry_id),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="active_alarm",
                translation_placeholders={"count": str(count) if count is not None else "?"},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id(ISSUE_ACTIVE_ALARM, entry_id))

        if self.consecutive_full_failures >= CONNECTION_LOST_THRESHOLD:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id(ISSUE_CONNECTION_LOST, entry_id),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="connection_lost",
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id(ISSUE_CONNECTION_LOST, entry_id))

    @staticmethod
    def _next_filter_change(data: ParmairData) -> str:
        day = data.get("filter_next_day")
        month = data.get("filter_next_month")
        year = data.get("filter_next_year")
        if day is None or month is None or year is None:
            return "?"
        return f"{int(day)}.{int(month)}.{int(year)}"

    # ── Summer-auto ──────────────────────────────────────────────────────

    def _evaluate_summer_auto(self, data: ParmairData, failed_keys: set[str]) -> None:
        """Run the dwell-gated summer-mode logic for one tick.

        A tick whose Modbus inputs failed to read this cycle is skipped rather
        than fed: ``data`` then holds the previous cycle's value, which is no
        fresh evidence to act on, and feeding ``None`` instead would reset
        the dwell timers — on a controller where one flaky block is routine,
        a 60-120 min dwell would then rarely complete.

        Deliberately defensive (broad except): a bug here must never break
        the polling cycle that carries every other entity's data.
        """
        if not self.summer_auto_enabled:
            self._summer_auto_logic.reset()
            return
        try:
            source_entity = self.config_entry.options.get(CONF_SUMMER_AUTO_SOURCE)
            stale = {"summer_mode"} if source_entity else {"summer_mode", "fresh_air_temperature"}
            if stale & failed_keys:
                return
            temp = (
                self._read_source_temperature(source_entity)
                if source_entity
                else self._as_float(data.get("fresh_air_temperature"))
            )
            summer_on = bool(data.get("summer_mode"))
            action = self._summer_auto_logic.update(
                dt_util.utcnow(), temp, summer_on, self.summer_auto_params
            )
            if action is not None and action != summer_on and not self._shutting_down:
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._async_summer_auto_write(int(action)),
                    name="parmair summer-auto write",
                )
        except Exception:  # noqa: BLE001 - never let this break the update cycle
            _LOGGER.exception("Parmair summer-auto evaluation failed")

    async def _async_summer_auto_write(self, value: int) -> None:
        """The summer-auto write, with its failure logged rather than unretrieved."""
        try:
            await self._async_write_pairs([("summer_mode", value)], user=False)
        except ParmairConnectionError as err:
            _LOGGER.warning("Parmair summer-auto write failed: %s", err)

    def _read_source_temperature(self, entity_id: str) -> float | None:
        """The external source's reading in °C (the thresholds are °C)."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        value = self._as_float(state.state)
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if (
            value is not None
            and unit != UnitOfTemperature.CELSIUS
            and unit in TemperatureConverter.VALID_UNITS
        ):
            return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
        return value

    @staticmethod
    def _as_float(value: object) -> float | None:
        """``value`` as a finite float, else None (nan/inf count as unavailable)."""
        if value is None:
            return None
        try:
            result = float(value)
        except TypeError, ValueError:
            return None
        return result if math.isfinite(result) else None

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
        # An unchanged re-report fires EVENT_STATE_REPORTED, not state_changed.
        # Without it a steady (e.g. integer-quantized humidity) sensor looks
        # silent to the detector, trips GAP_RESET_S, and the start of the next
        # cook is swallowed as warm-up.
        self.config_entry.async_on_unload(
            async_track_state_report_event(self.hass, entity_ids, self._async_cooking_event)
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
    def _async_cooking_event(
        self, event: Event[EventStateChangedData] | Event[EventStateReportedData]
    ) -> None:
        """Feed one source-sensor state change (or unchanged re-report) into the detector.

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
            elif self.cooking_auto_boost_enabled and not self._shutting_down:
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._async_cooking_boost_start(),
                    name="parmair cooking boost start",
                )
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
        if self._cooking_heartbeat_cancel is not None or self._shutting_down:
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

        Skips (no ownership) when the unit is off, boost is already active
        (manual or CO₂-auto), or the user has picked a mode of their own
        (fireplace, manual speed): turning that off later isn't ours to do.
        Ownership is set only after the write succeeds, so a failed write leaves
        no phantom claim — and not at all if a user mode write overtook us.
        """
        epoch = self._cooking_epoch
        async with self._write_lock:
            data = self.data
            if not data or self._shutting_down or epoch != self._cooking_epoch:
                return
            if (
                data.get("boost_active")
                or data.get("fireplace_active")
                or data.get("control_state") == CONTROL_STATE_MANUAL
                # A just-written manual speed/stop is only reflected in
                # control_state after its verify-read; speed_control has it
                # optimistically already.
                or data.get("speed_control") not in (None, SPEED_CONTROL_AUTO)
                or data.get("power_state") not in _POWER_ON_STATES
            ):
                return
            try:
                await self._async_write_pairs_locked(
                    [("speed_control", SPEED_CONTROL_AUTO), ("control_state", CONTROL_STATE_BOOST)]
                )
            except ParmairConnectionError as err:
                _LOGGER.warning("Parmair cooking auto-boost start failed: %s", err)
                return
            if epoch != self._cooking_epoch:
                return
            self._cooking_boost_owner = True
            self._cooking_boost_started = dt_util.utcnow()
            self._cooking_boost_confirmed = False

    def _drop_cooking_boost(self) -> None:
        """Give up the boost claim and any pending restore of it."""
        self._cooking_epoch += 1
        self._cooking_boost_owner = False
        self._cooking_boost_started = None
        self._cooking_boost_confirmed = False
        if self._cooking_restore_cancel is not None:
            self._cooking_restore_cancel()
            self._cooking_restore_cancel = None

    def _schedule_cooking_restore(self) -> None:
        """Restore the prior mode after the minimum boost run-time has elapsed."""
        if self._shutting_down:
            return
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
        if self._shutting_down:
            return
        # Checked under the write lock: a user mode write drops the claim
        # before it queues for the lock, so it can't be overwritten by us.
        async with self._write_lock:
            if self._cooking_boost_owner and self.cooking_active:
                # Cooking re-triggered while we queued for the lock, too late
                # for _handle_cooking_result to cancel us: keep the boost and
                # the claim; the next off-edge schedules a fresh restore.
                return
            data = self.data
            if self._cooking_boost_owner and data and data.get("boost_active"):
                try:
                    await self._async_write_pairs_locked(
                        [("control_state", restore_control_state(data))]
                    )
                except ParmairConnectionError as err:
                    _LOGGER.warning("Parmair cooking auto-boost restore failed: %s", err)
            self._drop_cooking_boost()

    def _reconcile_cooking_boost(self, data: ParmairData) -> None:
        """Drop boost ownership once the boost we started is gone.

        Respects a manual boost-off or the unit's own boost timer expiring —
        we stop trying to restore a boost that's no longer there. Only once a
        poll has shown the boost active, though: before that, a 0 may come
        from a read that predates our own write. Tiny and non-raising by
        construction (runs inside the poll cycle).
        """
        if not self._cooking_boost_owner:
            return
        if data.get("boost_active"):
            self._cooking_boost_confirmed = True
        elif self._cooking_boost_confirmed:
            self._drop_cooking_boost()

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
        await self._async_write_pairs([(key, value)], user=True)

    async def async_write_sequence(self, pairs: list[tuple[str, float | int]]) -> None:
        """Write several registers atomically in sequence, verifying only the last one."""
        await self._async_write_pairs(pairs, user=True)

    async def _async_write_pairs(self, pairs: list[tuple[str, float | int]], *, user: bool) -> None:
        """Take the write lock and write ``pairs``.

        A *user* write that changes the unit's mode drops the cooking boost
        claim first — before queueing for the lock, so a cooking restore that
        is already waiting on it sees the claim gone and writes nothing. The
        cooking/summer-auto glue passes ``user=False`` so its own writes never
        invalidate themselves.
        """
        if user and any(key in _MODE_KEYS for key, _value in pairs):
            self._drop_cooking_boost()
        async with self._write_lock:
            await self._async_write_pairs_locked(pairs)

    async def _async_write_pairs_locked(self, pairs: list[tuple[str, float | int]]) -> None:
        """Write ``pairs`` in order; the caller holds ``_write_lock``."""
        if self._shutting_down:
            raise ParmairConnectionError("write refused: the entry is unloading")
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
        written = decode(raw, definition)
        values: ParmairData = {key: written}
        if key == "control_state" and written is not None:
            derived = _CONTROL_STATE_DERIVED.get(int(written), {})
            values.update({k: v for k, v in derived.items() if self._find_block(k) is not None})
        self._async_apply_fresh(values)

    @callback
    def _async_apply_fresh(self, values: ParmairData, read_started: int | None = None) -> None:
        """Push write/verify values into ``self.data``, stamped for the poll merge.

        ``read_started`` is the seq a verify-read began at: a key a write
        stamped after that is newer than what the read saw, so it is left alone.
        """
        if read_started is not None:
            values = {k: v for k, v in values.items() if self._key_seq.get(k, 0) <= read_started}
        if not values:
            return
        self._seq += 1
        for key in values:
            self._key_seq[key] = self._seq
        updated: ParmairData = dict(self.data) if self.data is not None else {}
        updated.update(values)
        self.async_set_updated_data(updated)

    def _schedule_verify(self, verify_key: str) -> None:
        """Queue ``verify_key``'s block for the (debounced) verify-read."""
        if self._shutting_down:
            return
        block = self._find_block(verify_key)
        if block is None:
            return
        self._pending_verify[block.address] = block
        if self._verify_cancel is not None:
            self._verify_cancel()
        self._verify_cancel = async_call_later(self.hass, VERIFY_DELAY, self._async_verify)

    async def _async_verify(self, _now: datetime) -> None:
        """Re-read every block queued since the last verify, each once."""
        self._verify_cancel = None
        blocks = list(self._pending_verify.values())
        self._pending_verify.clear()
        for block in blocks:
            if self._shutting_down:
                return
            started = self._seq
            try:
                raw = await self.client.read_block(block.address, block.count)
            except ParmairConnectionError as err:
                _LOGGER.debug("Parmair verify read of block %s failed: %s", block.address, err)
                continue
            self._async_apply_fresh(self._decode_block(block, raw), read_started=started)

    async def async_shutdown(self) -> None:
        """Refuse new work, cancel pending timers, base shutdown, flush baselines."""
        self._shutting_down = True
        if self._verify_cancel is not None:
            self._verify_cancel()
            self._verify_cancel = None
        self._pending_verify.clear()
        self._stop_cooking_heartbeat()
        if self._cooking_restore_cancel is not None:
            self._cooking_restore_cancel()
            self._cooking_restore_cancel = None
        # Before the flush's await, so no refresh can be scheduled meanwhile.
        await super().async_shutdown()
        if self.cooking_detector is not None and self._cooking_store is not None:
            try:
                await self._cooking_store.async_save(
                    self.cooking_detector.snapshot(dt_util.utcnow())
                )
            except Exception:  # noqa: BLE001 - best-effort flush on unload
                _LOGGER.warning("Parmair cooking baseline final save failed", exc_info=True)


type ParmairConfigEntry = ConfigEntry[ParmairCoordinator]
