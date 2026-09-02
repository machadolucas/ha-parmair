"""Pure tests for the Modbus transport policy: warm-up, retries, translation.

``modbus.py`` is HA-free, so these run with plain pytest (no
pytest-homeassistant-custom-component) against ``modbus_connection``'s own
in-memory ``MockModbusUnit`` — the same double the library ships for device
libraries built on it, which keeps the fidelity of the stand-in on the library
authors rather than on a hand-rolled fake.

The client under test does *not* own the link: it wraps a unit borrowed from a
shared connection. So the invariants here are about policy, not about sockets —
that a downed link is warmed up before use, that a wedged one is discarded
between retries but a *answering* one is not, and that closing gives up our
hold without taking the shared link down.
"""

from __future__ import annotations

import asyncio

import pytest
from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusDesyncError,
)
from modbus_connection.mock import MockModbusConnection, MockModbusUnit

from custom_components.parmair import modbus
from custom_components.parmair.modbus import ParmairConnectionError, ParmairModbusClient

# The Multi24's unit id; the mock does not care, but reading the real constant
# keeps this file honest if it ever changes.
UNIT_ID = modbus.UNIT_ID_DEFAULT


@pytest.fixture
def connection() -> MockModbusConnection:
    """A fresh in-memory connection (starts disconnected, dials on first use)."""
    return MockModbusConnection()


@pytest.fixture
def unit(connection: MockModbusConnection) -> MockModbusUnit:
    """The Parmair unit handle, with the warm-up register present."""
    handle = connection.for_unit(UNIT_ID)
    handle.holding[modbus.WARM_UP_ADDRESS] = 1
    return handle


@pytest.fixture
def client(unit: MockModbusUnit, monkeypatch: pytest.MonkeyPatch) -> ParmairModbusClient:
    """The client under test, with the real sleeps removed."""
    monkeypatch.setattr(modbus, "WARM_UP_PAUSE", 0)
    monkeypatch.setattr(modbus, "RETRY_BACKOFF", (0, 0))
    return modbus.create_client(unit)


def reads(unit: MockModbusUnit) -> list[tuple[int, int]]:
    """The (address, count) of every holding-register block read so far."""
    return [
        (event.address, event.count)
        for event in unit.read_events
        if event.register_type == "holding"
    ]


# ── construction ─────────────────────────────────────────────────────────


def test_construction_sets_the_inter_transaction_spacing(client, unit):
    """Pacing is delegated to the shared connection, which enforces it bus-wide."""
    assert unit.message_spacing == modbus.INTER_TRANSACTION_DELAY


def test_construction_does_no_io(client, unit):
    """Borrowing and wrapping a unit must not touch the link."""
    assert unit.read_events == []
    assert client.connected is False


# ── connect: the eager warm-up round trip ────────────────────────────────


async def test_connect_warms_up_the_link(client, unit):
    await client.connect()
    assert reads(unit) == [(modbus.WARM_UP_ADDRESS, 1)]
    assert client.connected is True


async def test_connect_tolerates_one_warm_up_failure(client, unit):
    """The controller's first reply after a fresh connect is flaky by design."""
    failures = iter([ModbusConnectionError("flaky first reply")])

    original = unit.read_holding_registers

    async def flaky(address: int, count: int) -> list[int]:
        result = await original(address, count)
        if (err := next(failures, None)) is not None:
            raise err
        return result

    unit.read_holding_registers = flaky

    await client.connect()
    assert reads(unit) == [(modbus.WARM_UP_ADDRESS, 1)] * 2


async def test_connect_propagates_a_second_warm_up_failure(client, unit):
    unit.fail_requests(ModbusConnectionError("unit is down"))
    with pytest.raises(ParmairConnectionError, match="unit is down"):
        await client.connect()


async def test_connect_failure_reports_the_operation(client, unit):
    """The message names the read, so a log line says what was attempted."""
    unit.fail_requests(ModbusConnectionError("no route"))
    with pytest.raises(ParmairConnectionError, match=rf"read {modbus.WARM_UP_ADDRESS}/1 failed"):
        await client.connect()


# ── close: gives up our hold, never the shared link ──────────────────────


async def test_close_leaves_the_shared_link_up(client, unit):
    """The connection is refcounted by the modbus integration, not by us."""
    await client.connect()
    await client.close()
    assert unit.connected is True


async def test_close_stops_watching_the_link(client, connection, unit):
    await client.connect()
    await client.close()
    connection.simulate_connection_lost()
    assert client.link_drops == 0


async def test_io_after_close_is_refused(client, unit):
    await client.connect()
    await client.close()
    with pytest.raises(ParmairConnectionError, match="after release"):
        await client.read_block(1200, 2)


# ── reads ────────────────────────────────────────────────────────────────


async def test_read_block_returns_raw_words(client, unit):
    unit.holding.update({1200: 7, 1201: 0xFFFF, 1202: 42})
    await client.connect()
    assert await client.read_block(1200, 3) == [7, 0xFFFF, 42]


async def test_read_block_warms_up_a_downed_link_first(client, connection, unit):
    """A drop we did not cause is noticed via ``connected``, not via a flag."""
    await client.connect()
    connection.simulate_connection_lost()
    unit.read_events.clear()

    await client.read_block(1200, 1)

    assert reads(unit) == [(modbus.WARM_UP_ADDRESS, 1), (1200, 1)]
    assert client.link_drops == 1


async def test_read_block_does_not_rewarm_a_live_link(client, unit):
    await client.connect()
    unit.read_events.clear()
    await client.read_block(1200, 1)
    assert reads(unit) == [(1200, 1)]


# ── retries ──────────────────────────────────────────────────────────────


async def test_transient_failure_is_retried_after_a_fresh_warm_up(client, unit):
    """A dropped link is discarded, so the retry dials and re-warms."""
    await client.connect()
    unit.read_events.clear()
    unit.fail_read(1200, ModbusConnectionError("link wedged"))

    async def recover() -> None:
        await asyncio.sleep(0)
        unit.fail_read(1200, None)

    task = asyncio.create_task(recover())
    assert await client.read_block(1200, 1) == [0]
    await task

    # attempt 1 fails -> disconnect -> attempt 2 re-warms, then succeeds.
    assert reads(unit) == [(1200, 1), (modbus.WARM_UP_ADDRESS, 1), (1200, 1)]


async def test_read_gives_up_after_max_attempts(client, unit):
    """The unit answers the warm-up but never this block."""
    await client.connect()
    unit.fail_read(1200, ModbusConnectionError("gone"))
    with pytest.raises(ParmairConnectionError, match="gone"):
        await client.read_block(1200, 1)
    assert len([r for r in reads(unit) if r == (1200, 1)]) == modbus.MAX_ATTEMPTS


async def test_a_link_that_cannot_be_warmed_up_short_circuits_the_read(client, unit):
    """No point issuing the real read onto a link that just failed to settle."""
    await client.connect()
    unit.read_events.clear()
    unit.fail_requests(ModbusConnectionError("unit is down"))

    with pytest.raises(ParmairConnectionError, match="unit is down"):
        await client.read_block(1200, 1)

    # Attempt 1 tries the read on the still-live link; every later attempt only
    # gets as far as the warm-up.
    assert [r for r in reads(unit) if r == (1200, 1)] == [(1200, 1)]


async def test_a_wedged_link_is_dropped_after_the_last_attempt(client, unit):
    """Giving up must not hand the next transaction a link known to be broken."""
    await client.connect()
    unit.fail_requests(ModbusDesyncError("reply for someone else"))
    with pytest.raises(ParmairConnectionError):
        await client.read_block(1200, 1)
    assert unit.connected is False


# ── error responses: the device answered, so keep the link ───────────────


async def test_error_response_is_translated_and_flagged(client, unit):
    await client.connect()
    unit.fail_read(1200, IllegalDataAddressError())
    with pytest.raises(ParmairConnectionError, match="rejected") as excinfo:
        await client.read_block(1200, 1)
    assert excinfo.value.device_answered is True


async def test_error_response_does_not_drop_the_shared_link(client, unit):
    """A bad request of ours must not cost co-tenants their connection."""
    await client.connect()
    unit.fail_read(1200, IllegalDataAddressError())
    with pytest.raises(ParmairConnectionError):
        await client.read_block(1200, 1)
    assert unit.connected is True
    assert client.link_drops == 0


# ── timeout ──────────────────────────────────────────────────────────────


async def test_a_hanging_request_is_bounded_and_drops_the_link(
    client, unit, monkeypatch: pytest.MonkeyPatch
):
    """Core fixes the connection timeout at 10 s, so the bound has to be ours."""
    monkeypatch.setattr(modbus, "REQUEST_TIMEOUT", 0.01)
    await client.connect()

    async def hang(address: int, count: int) -> list[int]:
        await asyncio.sleep(10)
        return [0]

    unit.read_holding_registers = hang

    with pytest.raises(ParmairConnectionError, match="TimeoutError"):
        await client.read_block(1200, 1)
    assert unit.connected is False


# ── writes ───────────────────────────────────────────────────────────────


async def test_write_register_applies_the_value(client, unit):
    await client.connect()
    await client.write_register(1187, 3)
    assert unit.holding[1187] == 3


async def test_write_register_retries_then_gives_up(client, unit):
    await client.connect()
    unit.fail_write(1187, ModbusConnectionError("write refused"))
    with pytest.raises(ParmairConnectionError, match="write 1187=3 failed"):
        await client.write_register(1187, 3)


# ── the lost-link callback ───────────────────────────────────────────────


async def test_a_dropped_link_is_counted_not_reconnected(client, connection, unit):
    """No reload, no eager redial: recovery happens on the next transaction."""
    await client.connect()
    unit.read_events.clear()

    connection.simulate_connection_lost()

    assert client.link_drops == 1
    assert unit.connected is False
    assert unit.read_events == []


async def test_a_failing_disconnect_does_not_mask_the_real_error(client, unit):
    """Tearing a link down can itself fail; the transaction error is what matters."""
    await client.connect()
    unit.fail_read(1200, ModbusConnectionError("link wedged"))

    async def refuse_to_disconnect() -> None:
        raise ModbusConnectionError("could not tear the link down")

    unit.disconnect = refuse_to_disconnect

    with pytest.raises(ParmairConnectionError, match="link wedged"):
        await client.read_block(1200, 1)
