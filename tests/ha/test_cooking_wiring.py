"""End-to-end HA wiring for the cooking-detection feature.

Exercises the whole ``CONF_COOKING_SENSORS`` path through the real entities and
coordinator glue rather than the pure detector (``tests/test_cooking_detect.py``
covers the math). The detector is event-driven: kitchen source-sensor state
changes — fed here via ``hass.states.async_set`` — drive it, not the Modbus
poll, so the ``freezer`` clock is advanced between samples (the detector reads
``dt_util.utcnow()``) and every sample uses ``force_update=True`` so an
unchanged value still fires a state-change event.

Warm-up needs BOTH >=15 samples AND >=120 s of wall-clock before any evidence
counts, so ``_warmup`` feeds 20 quiescent samples spaced 10 s (190 s span)
before every trigger. A trigger is a fast rise (90 -> 195, 2 s apart) which at
the classified sigma-floor of 2.0 is a sustained z >= 5 held past the 3 s
persistence gate.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from conftest import FakeModbusClient
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.parmair.const import (
    CONF_CAPABILITIES,
    CONF_COOKING_SENSORS,
    CONF_REGISTER_MAP,
    CONF_SCAN_INTERVAL,
    COOKING_STORAGE_KEY,
    COOKING_STORAGE_VERSION,
    DOMAIN,
)

VOC = "sensor.test_voc"

# Fast rise (2 s apart). At sigma-floor 2.0 the first elevated sample is z=5,
# comfortably over Z_STRONG (4.5); held past the 3 s persistence gate -> ON.
ONSET = [90.0, 104.0, 119.0, 136.0, 156.0, 176.0, 195.0]

# The six cooking entities, keyed (platform, unique-id suffix).
COOKING_ENTITIES = (
    ("binary_sensor", "cooking_detected"),
    ("sensor", "cooking_score"),
    ("switch", "cooking_auto_boost"),
    ("number", "cooking_sensitivity"),
    ("number", "cooking_off_delay"),
    ("number", "cooking_min_boost_minutes"),
)


def _eid(hass: HomeAssistant, entry_id: str, platform: str, key: str) -> str | None:
    """Registry entity_id for one Parmair entity, or None if it wasn't created."""
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry_id}_{key}")


def _entity(hass: HomeAssistant, entity_id: str):
    """The live entity instance behind ``entity_id`` (to read fresh properties)."""
    for platform in async_get_platforms(hass, DOMAIN):
        if entity_id in platform.entities:
            return platform.entities[entity_id]
    return None


async def _setup_cooking(
    hass: HomeAssistant,
    bank: dict[int, int],
    capabilities: dict[str, Any],
    sensors: list[str],
    *,
    options: dict[str, Any] | None = None,
    entry_id: str | None = None,
) -> tuple[MockConfigEntry, FakeModbusClient]:
    """Set up the integration with ``CONF_COOKING_SENSORS`` populated.

    Patches only for the duration of setup: ``create_client`` is called once at
    setup, after which the returned ``FakeModbusClient`` is reached via
    ``coordinator.client`` for reads/writes.
    """
    opts: dict[str, Any] = {CONF_SCAN_INTERVAL: 10, CONF_COOKING_SENSORS: list(sensors)}
    if options:
        opts.update(options)
    kwargs: dict[str, Any] = {}
    if entry_id is not None:
        kwargs["entry_id"] = entry_id
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.168.101.56",
            CONF_PORT: 502,
            CONF_REGISTER_MAP: "v1_87",
            CONF_CAPABILITIES: capabilities,
        },
        options=opts,
        title="Parmair",
        **kwargs,
    )
    fake_client = FakeModbusClient(bank)
    with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, fake_client


async def _feed(hass: HomeAssistant, freezer, entity_id: str, values, step_s: float = 2.0) -> None:
    """Feed a value trace as source-sensor state changes at a fixed cadence.

    Advances the frozen clock BEFORE each set so the detector timestamps the
    sample correctly, and blocks after so the (@callback) event handler and any
    boost task it spawns settle.
    """
    for value in values:
        freezer.tick(timedelta(seconds=step_s))
        hass.states.async_set(entity_id, str(value), {"unit_of_measurement": ""}, force_update=True)
        await hass.async_block_till_done()


async def _warmup(hass: HomeAssistant, freezer, entity_id: str = VOC) -> None:
    """Feed enough quiescent samples to clear warm-up (>=15 samples AND >=120 s)."""
    await _feed(hass, freezer, entity_id, [80.0] * 20, step_s=10.0)


async def _fire_timers(hass: HomeAssistant, seconds: float = 2.0) -> None:
    """Fire HA time-driven callbacks (async_call_later restore, heartbeat).

    freezegun's clock move does not fire HA timers on its own — they are driven
    by async_fire_time_changed (block before AND after per the repo convention).
    """
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def _turn_on_auto_boost(hass: HomeAssistant, entry_id: str) -> None:
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": _eid(hass, entry_id, "switch", "cooking_auto_boost")},
        blocking=True,
    )


async def _set_number(hass: HomeAssistant, entry_id: str, key: str, value: float) -> None:
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _eid(hass, entry_id, "number", key), "value": value},
        blocking=True,
    )


# --------------------------------------------------------------------------- #
# Entity presence
# --------------------------------------------------------------------------- #


async def test_entities_absent_without_option(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """With no cooking_sensors configured, none of the six cooking entities exist."""
    entry, _fake = await async_setup_integration(rexo120_bank)
    for platform, key in COOKING_ENTITIES:
        assert _eid(hass, entry.entry_id, platform, key) is None, (platform, key)


async def test_entities_present_with_option(
    hass: HomeAssistant,
    rexo120_bank: dict[int, int],
    rexo120_capabilities_dict: dict[str, Any],
) -> None:
    """Configuring cooking_sensors creates all six entities; detector starts idle."""
    entry, _fake = await _setup_cooking(hass, rexo120_bank, rexo120_capabilities_dict, [VOC])
    for platform, key in COOKING_ENTITIES:
        assert _eid(hass, entry.entry_id, platform, key) is not None, (platform, key)

    bs = hass.states.get(_eid(hass, entry.entry_id, "binary_sensor", "cooking_detected"))
    assert bs.state == "off"
    score = hass.states.get(_eid(hass, entry.entry_id, "sensor", "cooking_score"))
    assert float(score.state) == 0.0


# --------------------------------------------------------------------------- #
# End-to-end detection
# --------------------------------------------------------------------------- #


async def test_detection_end_to_end(
    hass: HomeAssistant,
    rexo120_bank: dict[int, int],
    rexo120_capabilities_dict: dict[str, Any],
    freezer,
) -> None:
    """Warm-up + spike turns the binary sensor ON with a positive score; the
    off-delay decline turns it back OFF (default 4 min off-delay)."""
    entry, _fake = await _setup_cooking(hass, rexo120_bank, rexo120_capabilities_dict, [VOC])
    bs_id = _eid(hass, entry.entry_id, "binary_sensor", "cooking_detected")
    score_id = _eid(hass, entry.entry_id, "sensor", "cooking_score")

    await _warmup(hass, freezer)
    await _feed(hass, freezer, VOC, ONSET)

    assert hass.states.get(bs_id).state == "on"
    attrs = hass.states.get(bs_id).attributes
    assert "score" in attrs and "threshold" in attrs and "sensors" in attrs
    assert VOC in attrs["sensors"]
    assert attrs["threshold"] == 1.0  # 5 / default sensitivity 5
    assert float(hass.states.get(score_id).state) > 0.0

    # Decline to baseline and let the 4-minute off-delay + 60 s min-on elapse.
    await _feed(hass, freezer, VOC, [80.0] * 16, step_s=20.0)

    assert hass.states.get(bs_id).state == "off"
    assert float(hass.states.get(score_id).state) == 0.0


# --------------------------------------------------------------------------- #
# Auto-boost writes + restore
# --------------------------------------------------------------------------- #


async def test_auto_boost_writes_and_restores(
    hass: HomeAssistant,
    rexo120_bank: dict[int, int],
    rexo120_capabilities_dict: dict[str, Any],
    freezer,
) -> None:
    """Auto-boost on: a detection writes speed_control=0 then control_state=BOOST,
    and once it ends (after the min-boost delay) restores the prior home/away mode.

    rexo120_bank has home_state (1200) = 1 -> restore writes CONTROL_STATE_HOME (2).
    boost_active (1201) is emulated by hand: the fake unit doesn't set it from a
    control_state write, so we poke the bank + refresh so the restore path (which
    guards on ``data['boost_active']``) sees a live boost to return from.
    """
    entry, fake = await _setup_cooking(hass, rexo120_bank, rexo120_capabilities_dict, [VOC])
    coordinator = entry.runtime_data

    await _turn_on_auto_boost(hass, entry.entry_id)
    await _set_number(hass, entry.entry_id, "cooking_min_boost_minutes", 0)
    await _set_number(hass, entry.entry_id, "cooking_off_delay", 1)

    await _warmup(hass, freezer)
    await _feed(hass, freezer, VOC, ONSET)

    # speed_control (1187)=AUTO then control_state (1185)=BOOST, in that order.
    assert (1187, 0) in fake.writes
    assert (1185, 3) in fake.writes
    assert fake.writes.index((1187, 0)) < fake.writes.index((1185, 3))
    assert coordinator._cooking_boost_owner is True

    # The unit now reports the boost; a poll must keep our ownership.
    fake.bank[1201] = 1
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._cooking_boost_owner is True

    # Decline -> detection ends -> restore scheduled (min-boost 0 => immediate).
    await _feed(hass, freezer, VOC, [80.0] * 8, step_s=10.0)
    await _fire_timers(hass)

    assert (1185, 2) in fake.writes  # restored to HOME (home_state == 1)
    assert coordinator._cooking_boost_owner is False


async def test_auto_boost_skips_when_boost_already_active(
    hass: HomeAssistant,
    rexo120_bank: dict[int, int],
    rexo120_capabilities_dict: dict[str, Any],
    freezer,
) -> None:
    """A boost already active before the detection (manual/CO2-auto) is not ours to
    own: no BOOST write on start, and no restore write when the detection ends."""
    rexo120_bank[1201] = 1  # boost_active before setup
    entry, fake = await _setup_cooking(hass, rexo120_bank, rexo120_capabilities_dict, [VOC])
    coordinator = entry.runtime_data
    assert coordinator.data["boost_active"] == 1

    await _turn_on_auto_boost(hass, entry.entry_id)
    await _set_number(hass, entry.entry_id, "cooking_off_delay", 1)

    await _warmup(hass, freezer)
    await _feed(hass, freezer, VOC, ONSET)

    assert coordinator.cooking_active is True  # detection still fires
    assert (1187, 0) not in fake.writes  # but never claimed the boost
    assert (1185, 3) not in fake.writes
    assert coordinator._cooking_boost_owner is False

    # End the detection: still no restore write, ownership was never taken.
    await _feed(hass, freezer, VOC, [80.0] * 8, step_s=10.0)
    await _fire_timers(hass)
    assert (1185, 2) not in fake.writes


async def test_no_boost_writes_when_switch_off(
    hass: HomeAssistant,
    rexo120_bank: dict[int, int],
    rexo120_capabilities_dict: dict[str, Any],
    freezer,
) -> None:
    """Auto-boost switch OFF (default): a detection fires but writes no mode change."""
    entry, fake = await _setup_cooking(hass, rexo120_bank, rexo120_capabilities_dict, [VOC])
    coordinator = entry.runtime_data
    await _set_number(hass, entry.entry_id, "cooking_off_delay", 1)

    await _warmup(hass, freezer)
    await _feed(hass, freezer, VOC, ONSET)

    assert coordinator.cooking_active is True
    control_writes = [w for w in fake.writes if w[0] in (1185, 1187)]
    assert control_writes == []

    # End detection: still nothing written.
    await _feed(hass, freezer, VOC, [80.0] * 8, step_s=10.0)
    await _fire_timers(hass)
    assert [w for w in fake.writes if w[0] in (1185, 1187)] == []


async def test_manual_off_drops_ownership(
    hass: HomeAssistant,
    rexo120_bank: dict[int, int],
    rexo120_capabilities_dict: dict[str, Any],
    freezer,
) -> None:
    """If the user cancels the boost on the panel mid-cook (boost_active -> 0), the
    poll reconciliation drops ownership so the detection end writes no restore."""
    entry, fake = await _setup_cooking(hass, rexo120_bank, rexo120_capabilities_dict, [VOC])
    coordinator = entry.runtime_data

    await _turn_on_auto_boost(hass, entry.entry_id)
    await _set_number(hass, entry.entry_id, "cooking_off_delay", 1)

    await _warmup(hass, freezer)
    await _feed(hass, freezer, VOC, ONSET)
    assert coordinator._cooking_boost_owner is True
    assert (1185, 3) in fake.writes

    # Unit confirms the boost, then the user turns it off on the panel.
    fake.bank[1201] = 1
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._cooking_boost_owner is True

    fake.bank[1201] = 0  # user cancelled it on the unit
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator._cooking_boost_owner is False  # dropped by reconciliation

    # Detection ends -> no restore write (we no longer own the boost).
    await _feed(hass, freezer, VOC, [80.0] * 8, step_s=10.0)
    await _fire_timers(hass)
    assert (1185, 2) not in fake.writes


# --------------------------------------------------------------------------- #
# Unavailable source sensor
# --------------------------------------------------------------------------- #


async def test_unavailable_sensor_handled(
    hass: HomeAssistant,
    rexo120_bank: dict[int, int],
    rexo120_capabilities_dict: dict[str, Any],
    freezer,
) -> None:
    """An unavailable source sensor is handled without crashing, surfaces as such in
    the binary sensor's attributes, and its recovery jump re-warms (no false trigger)."""
    entry, _fake = await _setup_cooking(hass, rexo120_bank, rexo120_capabilities_dict, [VOC])
    coordinator = entry.runtime_data
    bs_id = _eid(hass, entry.entry_id, "binary_sensor", "cooking_detected")

    await _warmup(hass, freezer)

    # Sensor drops out.
    freezer.tick(timedelta(seconds=2))
    hass.states.async_set(VOC, "unavailable")
    await hass.async_block_till_done()

    assert coordinator.cooking_active is False
    # Live binary-sensor attributes report the sensor as unavailable (going
    # unavailable is a no-transition event, so read the entity property directly
    # rather than the last-dispatched state snapshot).
    attrs = _entity(hass, bs_id).extra_state_attributes
    assert attrs["sensors"][VOC]["status"] == "unavailable"

    # Comes back at a brand-new (rebooted) level: re-warm-up must swallow the jump.
    await _feed(hass, freezer, VOC, [150.0] * 15)
    assert coordinator.cooking_active is False


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


async def test_persistence_saved_and_restored(
    hass: HomeAssistant,
    rexo120_bank: dict[int, int],
    rexo120_capabilities_dict: dict[str, Any],
    hass_storage: dict[str, Any],
    freezer,
) -> None:
    """Unloading flushes the learned baseline to storage; a fresh entry pre-seeded
    with a baseline skips warm-up (a spike right after restore triggers at once)."""
    # -- save on unload --
    entry, _fake = await _setup_cooking(hass, rexo120_bank, rexo120_capabilities_dict, [VOC])
    await _warmup(hass, freezer)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    stored = hass_storage[COOKING_STORAGE_KEY.format(entry.entry_id)]["data"]
    assert VOC in stored["sensors"]
    assert 77.0 <= stored["sensors"][VOC]["mu"] <= 83.0

    # -- restore skips warm-up --
    seed_entry_id = "cooking_restore_entry"
    hass_storage[COOKING_STORAGE_KEY.format(seed_entry_id)] = {
        "version": COOKING_STORAGE_VERSION,
        "data": {
            "sensors": {
                VOC: {"mu": 80.0, "dev": 2.0, "n": 30, "saved_at": dt_util.utcnow().timestamp()}
            }
        },
    }
    hass.states.async_set(VOC, "80", {"unit_of_measurement": ""})
    entry2, _fake2 = await _setup_cooking(
        hass, rexo120_bank, rexo120_capabilities_dict, [VOC], entry_id=seed_entry_id
    )
    coordinator2 = entry2.runtime_data

    # One consistent sample confirms the restored baseline (no 120 s warm-up),
    # then an immediate spike triggers — impossible from a cold warm-up this fast.
    await _feed(hass, freezer, VOC, [81.0])
    await _feed(hass, freezer, VOC, ONSET)
    assert coordinator2.cooking_active is True


# --------------------------------------------------------------------------- #
# Options reload
# --------------------------------------------------------------------------- #


async def test_options_reload_rewires(
    hass: HomeAssistant,
    rexo120_bank: dict[int, int],
    rexo120_capabilities_dict: dict[str, Any],
    freezer,
) -> None:
    """Changing the cooking_sensors option reloads the entry onto the new sensor;
    the new sensor drives a fresh detector on the reloaded coordinator."""
    new_sensor = "sensor.test_voc2"
    fake = FakeModbusClient(rexo120_bank)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.168.101.56",
            CONF_PORT: 502,
            CONF_REGISTER_MAP: "v1_87",
            CONF_CAPABILITIES: rexo120_capabilities_dict,
        },
        options={CONF_SCAN_INTERVAL: 10, CONF_COOKING_SENSORS: [VOC]},
        title="Parmair",
    )
    # Patch spans the reload too: async_setup_entry calls create_client again.
    with patch("custom_components.parmair.modbus.create_client", return_value=fake):
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        old_coordinator = entry.runtime_data

        hass.states.async_set(new_sensor, "80", {"unit_of_measurement": ""})
        hass.config_entries.async_update_entry(
            entry, options={CONF_SCAN_INTERVAL: 10, CONF_COOKING_SENSORS: [new_sensor]}
        )
        await hass.async_block_till_done()

        new_coordinator = entry.runtime_data
        assert new_coordinator is not old_coordinator  # entry reloaded

        await _warmup(hass, freezer, new_sensor)
        await _feed(hass, freezer, new_sensor, ONSET)
        assert new_coordinator.cooking_active is True
