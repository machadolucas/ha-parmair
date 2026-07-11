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
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .capabilities import Capabilities
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_SUMMER_AUTO_SOURCE,
    CONNECTION_LOST_THRESHOLD,
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
    VERIFY_KEY,
)
from .modbus import ParmairConnectionError, ParmairModbusClient
from .registers import ReadBlock, RegisterMap, build_read_plan, decode, encode
from .summer_auto import SummerAutoLogic, SummerAutoParams

_LOGGER = logging.getLogger(__name__)

ParmairData = dict[str, float | int | None]

# Verify-read delay after a write: long enough for the controller to have
# applied the change and for the reflected register(s) to settle.
VERIFY_DELAY = 1.0


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
        """Cancel any pending verify timer, then defer to the base shutdown."""
        if self._verify_cancel is not None:
            self._verify_cancel()
            self._verify_cancel = None
        await super().async_shutdown()


type ParmairConfigEntry = ConfigEntry[ParmairCoordinator]
