"""Pure tests for the Modbus transport: pacing, retries, warm-up, translation.

``modbus.py`` is HA-free, so these run with plain pytest (no
pytest-homeassistant-custom-component) against a small pymodbus double
(``FakePyModbusClient``) monkeypatched in for ``AsyncModbusTcpClient``. The
double always returns the *same* instance regardless of constructor args —
standing in for "the same physical controller" across the module's
close+reconnect cycle, since ``connect()`` always builds a fresh
``AsyncModbusTcpClient`` object.
"""

from __future__ import annotations

import time

import pytest
from pymodbus.exceptions import ModbusException

from custom_components.parmair import modbus
from custom_components.parmair.modbus import ParmairConnectionError, ParmairModbusClient


class _FakeResult:
    def __init__(self, registers: list[int] | None = None, *, error: bool = False) -> None:
        self.registers = registers or []
        self._error = error

    def isError(self) -> bool:
        return self._error


class FakePyModbusClient:
    """Stands in for pymodbus's ``AsyncModbusTcpClient``.

    Scripted via ``read_script``/``write_script``: each entry is either an
    ``Exception`` instance (raised) or the "wire" outcome (a list of raw
    words for a read, a bool for a write's success/failure).
    """

    def __init__(self) -> None:
        self.connected = False
        self.connect_should_fail = False
        self.read_script: list[Exception | list[int]] = []
        self.write_script: list[Exception | bool] = []
        self.read_calls: list[tuple[int, int, int]] = []
        self.write_calls: list[tuple[int, int, int]] = []
        self.connect_calls = 0

    async def connect(self) -> bool:
        self.connect_calls += 1
        if self.connect_should_fail:
            return False
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False

    async def read_holding_registers(
        self,
        address: int,
        *,
        count: int = 1,
        device_id: int = 1,
        no_response_expected: bool = False,
    ) -> _FakeResult:
        self.read_calls.append((address, count, device_id))
        outcome = self.read_script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResult(registers=outcome)

    async def write_register(
        self, address: int, value: int, *, device_id: int = 1, no_response_expected: bool = False
    ) -> _FakeResult:
        self.write_calls.append((address, value, device_id))
        outcome = self.write_script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResult(error=not outcome)


@pytest.fixture
def fake_pymodbus(monkeypatch: pytest.MonkeyPatch) -> FakePyModbusClient:
    fake = FakePyModbusClient()
    monkeypatch.setattr(modbus, "AsyncModbusTcpClient", lambda *a, **kw: fake)
    # Keep the pure suite fast: real backoff/pacing delays aren't the point here
    # (a dedicated timing test below re-enables INTER_TRANSACTION_DELAY).
    monkeypatch.setattr(modbus, "WARM_UP_PAUSE", 0)
    monkeypatch.setattr(modbus, "RETRY_BACKOFF", (0, 0))
    monkeypatch.setattr(modbus, "INTER_TRANSACTION_DELAY", 0)
    return fake


async def test_connect_success_runs_one_warm_up_read(fake_pymodbus: FakePyModbusClient) -> None:
    fake_pymodbus.read_script = [[42]]
    client = ParmairModbusClient("host", 502)

    await client.connect()

    assert client.connected is True
    assert fake_pymodbus.read_calls == [(modbus.WARM_UP_ADDRESS, 1, modbus.UNIT_ID_DEFAULT)]


async def test_connect_tolerates_one_warm_up_failure(fake_pymodbus: FakePyModbusClient) -> None:
    fake_pymodbus.read_script = [ModbusException("first reply flaky"), [42]]
    client = ParmairModbusClient("host", 502)

    await client.connect()

    assert client.connected is True
    assert len(fake_pymodbus.read_calls) == 2


async def test_connect_warm_up_second_failure_propagates(
    fake_pymodbus: FakePyModbusClient,
) -> None:
    fake_pymodbus.read_script = [ModbusException("one"), ModbusException("two")]
    client = ParmairModbusClient("host", 502)

    with pytest.raises(ParmairConnectionError):
        await client.connect()


async def test_connect_failure_raises_parmair_connection_error(
    fake_pymodbus: FakePyModbusClient,
) -> None:
    fake_pymodbus.connect_should_fail = True
    client = ParmairModbusClient("host", 502)

    with pytest.raises(ParmairConnectionError):
        await client.connect()


async def test_close_resets_connected_state(fake_pymodbus: FakePyModbusClient) -> None:
    fake_pymodbus.read_script = [[1]]
    client = ParmairModbusClient("host", 502)
    await client.connect()
    assert client.connected is True

    await client.close()

    assert client.connected is False


async def test_read_block_returns_raw_words(fake_pymodbus: FakePyModbusClient) -> None:
    fake_pymodbus.read_script = [[1], [10, 20, 30]]  # warm-up, then the real read
    client = ParmairModbusClient("host", 502)
    await client.connect()

    values = await client.read_block(1000, 3)

    assert values == [10, 20, 30]


async def test_read_block_retries_after_failures_and_succeeds(
    fake_pymodbus: FakePyModbusClient,
) -> None:
    fake_pymodbus.read_script = [
        [1],  # connect() warm-up
        ModbusException("boom"),  # attempt 1 fails
        [1],  # reconnect warm-up
        [10, 20],  # attempt 2 succeeds
    ]
    client = ParmairModbusClient("host", 502)
    await client.connect()

    values = await client.read_block(2000, 2)

    assert values == [10, 20]
    assert client.connected is True


async def test_read_block_exhausts_retries_and_raises(
    fake_pymodbus: FakePyModbusClient,
) -> None:
    fake_pymodbus.read_script = [
        [1],  # connect() warm-up
        ModbusException("1"),  # attempt 1
        [1],  # reconnect warm-up
        ModbusException("2"),  # attempt 2
        [1],  # reconnect warm-up
        ModbusException("3"),  # attempt 3 (final)
    ]
    client = ParmairModbusClient("host", 502)
    await client.connect()

    with pytest.raises(ParmairConnectionError):
        await client.read_block(3000, 1)


async def test_read_block_translates_error_response(fake_pymodbus: FakePyModbusClient) -> None:
    """An error *response* (``isError()`` True) is translated just like an exception."""
    fake_pymodbus.read_script = [[1]]  # connect warm-up
    client = ParmairModbusClient("host", 502)
    await client.connect()

    async def _error_response(*_args, **_kwargs) -> _FakeResult:
        return _FakeResult(error=True)

    fake_pymodbus.read_holding_registers = _error_response  # type: ignore[method-assign]

    with pytest.raises(ParmairConnectionError):
        await client.read_block(4000, 1)


async def test_write_register_success(fake_pymodbus: FakePyModbusClient) -> None:
    fake_pymodbus.read_script = [[1]]  # connect warm-up
    fake_pymodbus.write_script = [True]
    client = ParmairModbusClient("host", 502)
    await client.connect()

    await client.write_register(5000, 123)

    assert fake_pymodbus.write_calls == [(5000, 123, modbus.UNIT_ID_DEFAULT)]


async def test_write_register_error_response_exhausts_retries(
    fake_pymodbus: FakePyModbusClient,
) -> None:
    fake_pymodbus.read_script = [[1], [1], [1]]  # connect + 2 reconnect warm-ups
    fake_pymodbus.write_script = [False, False, False]  # every attempt errors
    client = ParmairModbusClient("host", 502)
    await client.connect()

    with pytest.raises(ParmairConnectionError):
        await client.write_register(6000, 1)


async def test_inter_transaction_delay_paces_consecutive_reads(
    fake_pymodbus: FakePyModbusClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(modbus, "INTER_TRANSACTION_DELAY", 0.2)
    fake_pymodbus.read_script = [[1], [10], [20]]
    client = ParmairModbusClient("host", 502)
    await client.connect()

    start = time.monotonic()
    await client.read_block(1000, 1)
    await client.read_block(1001, 1)
    elapsed = time.monotonic() - start

    assert elapsed >= 0.2
