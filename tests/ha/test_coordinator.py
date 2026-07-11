"""Coordinator: decoding, partial/full failure, repairs, writes, summer-auto."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.parmair.const import (
    CONNECTION_LOST_THRESHOLD,
    DOMAIN,
    ISSUE_ACTIVE_ALARM,
    ISSUE_CONNECTION_LOST,
    ISSUE_FILTER_CHANGE_DUE,
)
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
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_FILTER_CHANGE_DUE) is None

    fake_client.bank[1205] = 0  # filter_state -> due
    await coordinator.async_refresh()

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_FILTER_CHANGE_DUE) is not None

    fake_client.bank[1205] = 1
    await coordinator.async_refresh()

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_FILTER_CHANGE_DUE) is None


async def test_active_alarm_issue_raised_and_cleared(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    coordinator = entry.runtime_data
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ACTIVE_ALARM) is None

    fake_client.bank[1005] = 1  # summary_alarm
    fake_client.bank[1004] = 2  # active_alarm_count
    await coordinator.async_refresh()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ACTIVE_ALARM)
    assert issue is not None
    assert issue.translation_placeholders == {"count": "2"}

    fake_client.bank[1005] = 0
    await coordinator.async_refresh()

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ACTIVE_ALARM) is None


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
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CONNECTION_LOST) is not None

    fake_client.fail_all = False
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_CONNECTION_LOST) is None


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
