"""Diagnostics: host redaction + capabilities/read-plan/data/health surface."""

from __future__ import annotations

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant

from custom_components.parmair.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_redacts_host_and_reports_everything_else(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, _fake_client = await async_setup_integration(rexo120_bank)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry"]["data"]["host"] == REDACTED
    assert "192.168.101.56" not in str(diagnostics)
    assert diagnostics["entry"]["options"]["scan_interval"] == 10
    assert diagnostics["entry"]["options"]["co2_offset"] == -480

    assert diagnostics["capabilities"]["machine_type"] == 120
    assert diagnostics["capabilities"]["has_co2"] is True
    assert diagnostics["register_map"] == "v1_87"

    assert len(diagnostics["read_plan"]) == 6
    for block in diagnostics["read_plan"]:
        assert set(block) == {"address", "count", "n_keys"}
        assert block["n_keys"] <= block["count"]

    assert diagnostics["static_data"]["machine_type"] == 120
    assert diagnostics["data"]["supply_temperature"] == 24.9
    assert diagnostics["data"]["machine_type"] == 120  # static values merged in too

    stats = diagnostics["stats"]
    assert stats["block_failures"] == 0
    assert stats["consecutive_full_failures"] == 0
    assert stats["last_successful_update"] is not None
    assert stats["last_update_success"] is True
    assert stats["summer_auto_enabled"] is False
    assert stats["summer_auto_params"]["on_temp_c"] == 21.0
    assert stats["summer_auto_params"]["off_dwell_min"] == 120.0


async def test_diagnostics_reflects_a_failing_block(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    entry, fake_client = await async_setup_integration(rexo120_bank)
    fake_client.fail_reads_at = {1220}

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["stats"]["block_failures"] >= 1
    assert diagnostics["stats"]["consecutive_full_failures"] == 0
