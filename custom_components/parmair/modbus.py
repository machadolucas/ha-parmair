"""Modbus TCP transport for the Parmair MAC Multi24 controller.

HA-free by design (imports pymodbus + asyncio + stdlib only) so it can be unit
tested without pulling in Home Assistant, and so nothing above this module
needs to know pymodbus exists — every pymodbus exception/error response is
translated to :class:`ParmairConnectionError` here.

The Multi24 is slow and cannot pipeline transactions: a single
``asyncio.Lock`` serializes every read/write, and consecutive transactions are
paced at least :data:`INTER_TRANSACTION_DELAY` apart. Another client's traffic
on the same unit can also leak a mismatched transaction id — pymodbus silently
skips those, but the read that should have gotten that reply then times out —
so every transaction gets a bounded retry with backoff, reconnecting (including
the warm-up read) between attempts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

UNIT_ID_DEFAULT = 0  # the Multi24 answers on unit/slave id 0, not 1
CONNECT_TIMEOUT = 5  # seconds

# The Multi24 cannot pipeline; back-to-back transactions faster than this
# observed to cause the controller to drop/garble a reply.
INTER_TRANSACTION_DELAY = 0.3  # seconds

# Arbitrary always-present register used only to settle the link right after
# a fresh connect (the controller's first reply then is flaky).
WARM_UP_ADDRESS = 1244
WARM_UP_PAUSE = 0.3  # seconds, between the tolerated failure and the retry

# Read/write retry policy: up to 2 retries (3 attempts total), with backoff
# before each retry.
RETRY_BACKOFF = (0.5, 1.0)
MAX_ATTEMPTS = 1 + len(RETRY_BACKOFF)


class ParmairConnectionError(Exception):
    """Any Modbus-transport failure: connect, timeout, or error response."""


def create_client(host: str, port: int) -> ParmairModbusClient:
    """Module-level factory for :class:`ParmairModbusClient`.

    A seam for tests: patch this name (not the class) to substitute a fake
    client without touching the real pymodbus transport.
    """
    return ParmairModbusClient(host, port)


class ParmairModbusClient:
    """Serializes Modbus TCP transactions to one Parmair MAC Multi24."""

    def __init__(self, host: str, port: int, *, unit_id: int = UNIT_ID_DEFAULT) -> None:
        self._host = host
        self._port = port
        self._unit_id = unit_id
        self._client: AsyncModbusTcpClient | None = None
        self._lock = asyncio.Lock()
        self._last_transaction_at: float | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.connected

    async def connect(self) -> None:
        """Open the TCP connection and settle the link with a warm-up read.

        Not lock-guarded: called both from the outside (initial setup) and
        from inside :meth:`_retry` while the lock is already held by the
        calling read/write, so it must never try to reacquire it.
        """
        client = AsyncModbusTcpClient(self._host, port=self._port, timeout=CONNECT_TIMEOUT)
        try:
            connected = await client.connect()
        except Exception as err:  # noqa: BLE001 - translate everything to our own type
            raise ParmairConnectionError(
                f"connect to {self._host}:{self._port} failed: {err}"
            ) from err
        if not connected or not client.connected:
            raise ParmairConnectionError(f"connect to {self._host}:{self._port} failed")
        self._client = client
        await self._warm_up()

    async def _warm_up(self) -> None:
        """One tolerated-failure warm-up read (the fresh-connect reply is flaky).

        A single register is read twice at most: the first failure is
        swallowed after a short pause, the second attempt's failure (if any)
        propagates as a real connect failure.
        """
        try:
            await self._read_once(WARM_UP_ADDRESS, 1)
        except ParmairConnectionError as err:
            _LOGGER.debug("Parmair warm-up read failed once (tolerated): %s", err)
            await asyncio.sleep(WARM_UP_PAUSE)
            await self._read_once(WARM_UP_ADDRESS, 1)

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    async def read_block(self, address: int, count: int) -> list[int]:
        async with self._lock:
            return await self._retry(lambda: self._read_once(address, count))

    async def write_register(self, address: int, value: int) -> None:
        async with self._lock:
            await self._retry(lambda: self._write_once(address, value))

    async def _retry(self, op: Callable[[], Awaitable[T]]) -> T:
        """Run ``op`` with bounded retries, reconnecting between attempts.

        Every failure this module can see is a translated
        :class:`ParmairConnectionError` (transport, timeout, or error
        response), and any of them may mean the link itself is wedged, so a
        close+reconnect (including the warm-up) happens before each retry.
        """
        last_err: ParmairConnectionError | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return await op()
            except ParmairConnectionError as err:
                last_err = err
                if attempt == MAX_ATTEMPTS - 1:
                    break
                _LOGGER.debug("Parmair transaction failed (attempt %d): %s", attempt + 1, err)
                await asyncio.sleep(RETRY_BACKOFF[attempt])
                try:
                    await self.close()
                    await self.connect()
                except ParmairConnectionError as reconnect_err:
                    last_err = reconnect_err
        assert last_err is not None
        raise last_err

    async def _pace(self) -> None:
        """Sleep the remainder of ``INTER_TRANSACTION_DELAY`` since the last transaction."""
        if self._last_transaction_at is not None:
            elapsed = time.monotonic() - self._last_transaction_at
            remaining = INTER_TRANSACTION_DELAY - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_transaction_at = time.monotonic()

    async def _read_once(self, address: int, count: int) -> list[int]:
        await self._pace()
        if self._client is None:
            raise ParmairConnectionError("read attempted while not connected")
        try:
            result = await self._client.read_holding_registers(
                address, count=count, device_id=self._unit_id
            )
        except (ModbusException, ConnectionException, TimeoutError) as err:
            raise ParmairConnectionError(f"read {address}/{count} failed: {err}") from err
        if result is None or result.isError():
            raise ParmairConnectionError(f"read {address}/{count} returned an error: {result}")
        return list(result.registers)

    async def _write_once(self, address: int, value: int) -> None:
        await self._pace()
        if self._client is None:
            raise ParmairConnectionError("write attempted while not connected")
        try:
            result = await self._client.write_register(address, value, device_id=self._unit_id)
        except (ModbusException, ConnectionException, TimeoutError) as err:
            raise ParmairConnectionError(f"write {address}={value} failed: {err}") from err
        if result is None or result.isError():
            raise ParmairConnectionError(f"write {address}={value} returned an error: {result}")
