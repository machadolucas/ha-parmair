"""Coordinator: decoding, partial/full failure, repairs, writes, summer-auto."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from conftest import FakeModbusClient
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.parmair.const import (
    CONNECTION_LOST_THRESHOLD,
    DOMAIN,
    ISSUE_ACTIVE_ALARM,
    ISSUE_CONNECTION_LOST,
    ISSUE_FILTER_CHANGE_DUE,
)
from custom_components.parmair.coordinator import ParmairCoordinator, issue_id
from custom_components.parmair.modbus import ParmairConnectionError
from custom_components.parmair.summer_auto import SummerAutoParams


async def _tick(hass: HomeAssistant, seconds: float) -> None:
    """Advance the loop clock and let any scheduled callback run.

    Coordinator timers (``async_track_time_interval``) and the write-verify
    timer (``async_call_later``) both settle only after a ``block_till_done``
    following the fired time — hence once before (drain anything already
    pending) and once after (let the fired callback's own coroutine finish).
    """
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_first_refresh_decodes_and_merges_static_data(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data

    data = coordinator.data
    assert data["supply_temperature"] == 24.9
    assert data["extract_temperature"] == 23.2
    assert data["fresh_air_temperature"] == 25.4
    assert data["co2"] == 969
    assert data["heat_recovery_efficiency"] == 86.4
    assert data["boost_duration"] == 4

    # Gated off (has_main_humidity is False for this unit) — not polled at all.
    assert "humidity" not in data

    # Static registers, read once at setup, merged into every cycle's data.
    assert data["machine_type"] == 120
    assert data["software_version"] == 1.87


async def test_partial_block_failure_keeps_old_values_without_update_failed(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    assert coordinator.data["fault_fresh_air_sensor"] == 0

    # Change a value in a healthy block and fail the 1220 (fault/alarm) block.
    fake_client.bank[1023] = 300  # supply_temperature -> 30.0
    fake_client.fail_reads_at = {1220}

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data["supply_temperature"] == 30.0
    # The failed block's keys keep their previous cycle's values.
    assert coordinator.data["fault_fresh_air_sensor"] == 0
    assert coordinator.block_failures == 1
    assert coordinator.consecutive_full_failures == 0


async def test_all_blocks_failing_raises_update_failed(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    assert coordinator.last_update_success is True

    fake_client.fail_all = True
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert coordinator.consecutive_full_failures == 1


async def test_recovery_after_full_failure_clears_state(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data

    fake_client.fail_all = True
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    assert coordinator.consecutive_full_failures == 1

    fake_client.fail_all = False
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.consecutive_full_failures == 0


async def test_scan_interval_from_options_is_respected(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    assert entry.runtime_data.update_interval == timedelta(seconds=10)


# ── Writes ───────────────────────────────────────────────────────────────


async def test_async_write_optimistic_update_then_verify_read(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    reads_before = fake_client.read_calls

    await coordinator.async_write("supply_temperature_target", 19.5)

    assert (1065, 195) in fake_client.writes
    # Optimistic update lands immediately, before the verify read.
    assert coordinator.data["supply_temperature_target"] == 19.5

    await _tick(hass, 1.0)

    assert fake_client.read_calls > reads_before


async def test_verify_key_reads_the_mapped_register_block(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    fake_client.read_log.clear()

    # control_state (185) -> VERIFY_KEY maps to boost_active (201), whose
    # block starts at 1183.
    await coordinator.async_write("control_state", 3)
    await _tick(hass, 1.0)

    assert any(address == 1183 for address, _count in fake_client.read_log)


async def test_async_write_sequence_verifies_only_the_last_key(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data

    await coordinator.async_write_sequence([("home_speed", 3), ("away_speed", 2)])

    assert (1104, 3) in fake_client.writes
    assert (1105, 2) in fake_client.writes
    assert coordinator.data["home_speed"] == 3
    assert coordinator.data["away_speed"] == 2


async def test_async_write_rejects_non_writable_register(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data

    try:
        await coordinator.async_write("supply_temperature", 20.0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a read-only register")


# ── Repairs ──────────────────────────────────────────────────────────────


async def test_filter_change_due_issue_raised_and_cleared(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, issue_id(ISSUE_FILTER_CHANGE_DUE, entry.entry_id)
        )
        is None
    )

    fake_client.bank[1205] = 0  # filter_state -> due
    await coordinator.async_refresh()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, issue_id(ISSUE_FILTER_CHANGE_DUE, entry.entry_id)
        )
        is not None
    )

    fake_client.bank[1205] = 1
    await coordinator.async_refresh()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, issue_id(ISSUE_FILTER_CHANGE_DUE, entry.entry_id)
        )
        is None
    )


async def test_active_alarm_issue_raised_and_cleared(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, issue_id(ISSUE_ACTIVE_ALARM, entry.entry_id))
        is None
    )

    fake_client.bank[1005] = 1  # summary_alarm
    fake_client.bank[1004] = 2  # active_alarm_count
    await coordinator.async_refresh()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id(ISSUE_ACTIVE_ALARM, entry.entry_id))
    assert issue is not None
    assert issue.translation_placeholders == {"count": "2"}

    fake_client.bank[1005] = 0
    await coordinator.async_refresh()

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, issue_id(ISSUE_ACTIVE_ALARM, entry.entry_id))
        is None
    )


async def test_connection_lost_issue_after_threshold_consecutive_failures(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """5 consecutive fully-failed cycles raise the repair; one success clears it.

    Driven by calling ``async_refresh()`` directly rather than the
    coordinator's own polling timer: with no real platforms yet (M4/M5 add
    them), nothing has subscribed as a listener, and ``DataUpdateCoordinator``
    only reschedules its internal timer while ``self._listeners`` is
    non-empty — so ``async_fire_time_changed`` alone wouldn't drive a second
    cycle here. Each call to ``async_refresh()`` is exactly one poll cycle
    regardless, which is all this test needs.
    """
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data

    fake_client.fail_all = True
    for _ in range(CONNECTION_LOST_THRESHOLD):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.consecutive_full_failures >= CONNECTION_LOST_THRESHOLD
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, issue_id(ISSUE_CONNECTION_LOST, entry.entry_id))
        is not None
    )

    fake_client.fail_all = False
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, issue_id(ISSUE_CONNECTION_LOST, entry.entry_id))
        is None
    )


# ── Summer-auto (minimal here; M6 covers the entities that drive it) ─────


async def test_summer_auto_no_write_when_already_at_target_state(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    coordinator.summer_auto_enabled = True
    coordinator.summer_auto_params = SummerAutoParams(
        on_temp_c=20.0, on_dwell_min=1.0, off_temp_c=10.0, off_dwell_min=1.0
    )

    # fresh_air_temperature is 25.4 (>= on_temp_c) and summer_mode is already 1.
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert (1079, 1) not in fake_client.writes


async def test_summer_auto_writes_on_after_dwell(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int], freezer
) -> None:
    """Two refreshes 2 minutes apart satisfy the 1-minute on-dwell and fire a write.

    ``freezer`` (freezegun, via pytest-homeassistant-custom-component) is needed
    here because the dwell timer compares real ``dt_util.utcnow()`` values —
    ``async_fire_time_changed`` alone only fools HA's own event-scheduling
    helpers, not application code that reads ``dt_util.utcnow()`` directly.
    """
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    coordinator.summer_auto_enabled = True
    coordinator.summer_auto_params = SummerAutoParams(
        on_temp_c=20.0, on_dwell_min=1.0, off_temp_c=10.0, off_dwell_min=1.0
    )
    fake_client.bank[1079] = 0  # summer_mode currently off

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert (1079, 1) not in fake_client.writes  # dwell not satisfied yet

    freezer.tick(timedelta(minutes=2))
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert (1079, 1) in fake_client.writes


# ── Outage fast-fail ─────────────────────────────────────────────────────


def _unreachable(address: int) -> ParmairConnectionError:
    err = ParmairConnectionError(f"read {address}: warm-up failed")
    err.link_unreachable = True
    return err


async def test_unreachable_link_skips_the_rest_of_the_cycle(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """A block that couldn't even warm the link up fails the others without I/O.

    Before, each of the 6 blocks paid the full ~32 s retry budget against a unit
    that was gone, so a failed cycle took ~189 s and the repair ~16.5 min.
    """
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    before = dict(coordinator.data)
    attempted: list[int] = []

    async def unit_gone(address: int, count: int) -> list[int]:
        attempted.append(address)
        raise _unreachable(address)

    fake_client.read_block = unit_gone
    await coordinator.async_refresh()

    assert attempted == [coordinator.read_plan[0].address]
    assert coordinator.last_update_success is False
    assert coordinator.consecutive_full_failures == 1
    assert coordinator.block_failures == len(coordinator.read_plan)
    assert coordinator.data == before  # previous values kept


async def test_unreachable_mid_cycle_keeps_what_was_already_read(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    plan = coordinator.read_plan
    dead_from = plan[2].address
    real_read = fake_client.read_block
    attempted: list[int] = []

    async def dies_mid_cycle(address: int, count: int) -> list[int]:
        attempted.append(address)
        if address >= dead_from:
            raise _unreachable(address)
        return await real_read(address, count)

    fake_client.bank[1023] = 300  # supply_temperature (first block) -> 30.0
    fake_client.read_block = dies_mid_cycle
    await coordinator.async_refresh()

    assert attempted == [block.address for block in plan[:3]]
    assert coordinator.last_update_success is True  # partial, not a full failure
    assert coordinator.data["supply_temperature"] == 30.0
    assert coordinator.block_failures == len(plan) - 2


async def test_ordinary_block_failure_still_reads_the_remaining_blocks(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    fake_client.read_log.clear()
    fake_client.fail_reads_at = {coordinator.read_plan[0].address}

    await coordinator.async_refresh()

    assert [a for a, _ in fake_client.read_log] == [b.address for b in coordinator.read_plan]


# ── Per-entry repair issues ──────────────────────────────────────────────


async def test_repair_issues_are_per_entry(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    rexo120_bank: dict[int, int],
) -> None:
    """Two units: one's clean poll must not delete the other's issue."""
    second = MockConfigEntry(
        domain=DOMAIN,
        data={**mock_config_entry.data, CONF_HOST: "192.168.101.57"},
        options=dict(mock_config_entry.options),
        title="Parmair 2",
    )
    clients = [FakeModbusClient(rexo120_bank), FakeModbusClient(rexo120_bank)]
    with patch("custom_components.parmair.modbus.create_client", side_effect=clients):
        for entry in (mock_config_entry, second):
            entry.add_to_hass(hass)
            await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

    clients[1].bank[1205] = 0  # second unit: filter due
    await second.runtime_data.async_refresh()
    await mock_config_entry.runtime_data.async_refresh()  # first unit: all clear

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id(ISSUE_FILTER_CHANGE_DUE, second.entry_id))
    assert (
        registry.async_get_issue(
            DOMAIN, issue_id(ISSUE_FILTER_CHANGE_DUE, mock_config_entry.entry_id)
        )
        is None
    )


async def test_setup_deletes_legacy_global_issue_ids(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    for base in (ISSUE_FILTER_CHANGE_DUE, ISSUE_ACTIVE_ALARM, ISSUE_CONNECTION_LOST):
        ir.async_create_issue(
            hass,
            DOMAIN,
            base,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=base,
        )

    await async_setup_integration(rexo120_bank)

    for base in (ISSUE_FILTER_CHANGE_DUE, ISSUE_ACTIVE_ALARM, ISSUE_CONNECTION_LOST):
        assert ir.async_get(hass).async_get_issue(DOMAIN, base) is None


# ── Poll/write race ──────────────────────────────────────────────────────


async def test_write_landing_mid_poll_is_not_overwritten_by_the_older_read(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """The block read began before the write, so its (older) value must lose."""
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    target_block = coordinator._find_block("supply_temperature_target")
    real_read = fake_client.read_block

    async def write_lands_during_read(address: int, count: int) -> list[int]:
        raw = await real_read(address, count)  # the unit answers with the old value...
        if address == target_block.address:
            fake_client.read_block = real_read
            await coordinator.async_write("supply_temperature_target", 19.5)  # ...then this lands
        return raw

    fake_client.read_block = write_lands_during_read
    await coordinator.async_refresh()

    assert coordinator.data["supply_temperature_target"] == 19.5


async def test_poll_after_the_write_takes_the_device_value(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """The stamp only protects against *older* reads, never against newer truth."""
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    await coordinator.async_write("supply_temperature_target", 19.5)
    fake_client.bank[1065] = 200  # the unit (or its panel) settled on 20.0

    await coordinator.async_refresh()

    assert coordinator.data["supply_temperature_target"] == 20.0


async def test_stale_poll_cannot_drop_a_just_claimed_cooking_boost(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    coordinator._cooking_boost_owner = True  # as right after the claim
    coordinator._cooking_boost_confirmed = False

    fake_client.bank[1201] = 0  # a read that predates the boost taking effect
    await coordinator.async_refresh()
    assert coordinator._cooking_boost_owner is True

    fake_client.bank[1201] = 1  # the unit shows the boost...
    await coordinator.async_refresh()
    fake_client.bank[1201] = 0  # ...then it is gone (user or timer ended it)
    await coordinator.async_refresh()
    assert coordinator._cooking_boost_owner is False


# ── Write atomicity, derived state, verify batching ──────────────────────


async def test_write_sequences_do_not_interleave(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    real_write = fake_client.write_register

    async def slow_write(address: int, value: int) -> None:
        await asyncio.sleep(0)  # yield, as a real round trip does
        await real_write(address, value)

    fake_client.write_register = slow_write
    fake_client.writes.clear()
    await asyncio.gather(
        coordinator.async_write_sequence([("speed_control", 0), ("control_state", 4)]),
        coordinator.async_write_sequence([("speed_control", 0), ("control_state", 2)]),
    )

    assert fake_client.writes == [(1187, 0), (1185, 4), (1187, 0), (1185, 2)]


async def test_control_state_write_patches_the_derived_flags_optimistically(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """The boost/fireplace switches read *_active; without this they bounce to off."""
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data

    async def unit_ignores_it(address: int, value: int) -> None:
        fake_client.writes.append((address, value))  # no side effects: prove it's ours

    fake_client.write_register = unit_ignores_it

    await coordinator.async_write("control_state", 3)
    assert (coordinator.data["boost_active"], coordinator.data["fireplace_active"]) == (1, 0)
    await coordinator.async_write("control_state", 4)
    assert (coordinator.data["boost_active"], coordinator.data["fireplace_active"]) == (0, 1)
    await coordinator.async_write("control_state", 2)
    assert (coordinator.data["boost_active"], coordinator.data["fireplace_active"]) == (0, 0)

    # The verify-read then replaces the guess with what the unit reports.
    await _tick(hass, 1.0)
    assert coordinator.data["boost_active"] == fake_client.bank.get(1201, 0)


async def test_writes_to_different_blocks_verify_each_block_once(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """A second write used to cancel the first one's pending verify."""
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    target_block = coordinator._find_block("supply_temperature_target").address
    boost_block = coordinator._find_block("boost_active").address
    assert target_block != boost_block

    await coordinator.async_write("supply_temperature_target", 19.5)
    await coordinator.async_write("control_state", 3)
    await coordinator.async_write("control_state", 2)
    fake_client.read_log.clear()
    await _tick(hass, 1.0)

    assert sorted(a for a, _ in fake_client.read_log) == sorted([target_block, boost_block])


async def test_shutdown_refuses_new_writes_and_timers(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    await coordinator.async_shutdown()
    fake_client.writes.clear()

    with pytest.raises(ParmairConnectionError, match="unloading"):
        await coordinator.async_write("supply_temperature_target", 19.5)
    coordinator._schedule_verify("supply_temperature_target")

    assert fake_client.writes == []
    assert coordinator._verify_cancel is None


def test_as_float_rejects_non_finite_values() -> None:
    assert ParmairCoordinator._as_float("nan") is None
    assert ParmairCoordinator._as_float("inf") is None
    assert ParmairCoordinator._as_float(float("-inf")) is None
    assert ParmairCoordinator._as_float("21.5") == 21.5


# ── Summer-auto write failure ────────────────────────────────────────────


async def test_summer_auto_write_failure_is_logged_not_unretrieved(
    hass: HomeAssistant,
    async_setup_integration,
    rexo120_bank: dict[int, int],
    freezer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    coordinator.summer_auto_enabled = True
    coordinator.summer_auto_params = SummerAutoParams(
        on_temp_c=20.0, on_dwell_min=1.0, off_temp_c=10.0, off_dwell_min=1.0
    )
    fake_client.bank[1079] = 0

    async def link_down(address: int, value: int) -> None:
        raise ParmairConnectionError("simulated write failure")

    fake_client.write_register = link_down
    await coordinator.async_refresh()
    freezer.tick(timedelta(minutes=2))
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert "summer-auto write failed" in caplog.text
    assert "never retrieved" not in caplog.text
