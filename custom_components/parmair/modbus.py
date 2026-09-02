"""Modbus transport policy for the Parmair MAC Multi24 controller.

HA-free by design (imports ``modbus_connection`` + asyncio + stdlib only) so it
can be unit tested without pulling in Home Assistant, and so nothing above this
module needs to know which Modbus library is underneath — every
``modbus_connection`` failure is translated to :class:`ParmairConnectionError`
here.

This module owns no socket. Home Assistant's ``modbus`` integration hands out
:class:`~modbus_connection.ModbusUnit` handles over connections it shares and
refcounts; ``__init__.py`` and ``config_flow.py`` are the only places that
borrow one. What lives here is the Multi24-specific policy that shared
connection does not provide:

* The unit cannot pipeline, so requests are spaced at least
  :data:`INTER_TRANSACTION_DELAY` apart (``set_message_spacing``, which the
  library enforces connection-wide), and an :class:`asyncio.Lock` keeps our own
  multi-step sequences atomic against a concurrent verify-read.
* The controller's first reply after a fresh connect is flaky, so a
  tolerated-failure warm-up read settles the link whenever it is down.
* Another client's traffic on the same unit can desync the link, so every
  transaction gets a bounded retry that discards the link between attempts.
* Core fixes the shared connection's timeout at 10 s — longer than a whole poll
  cycle — so each request is bounded by :data:`REQUEST_TIMEOUT` instead.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from modbus_connection import (
    ModbusError,
    ModbusExceptionError,
    ModbusTcpParams,
    ModbusUnit,
)

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

UNIT_ID_DEFAULT = 0  # the Multi24 answers on unit/slave id 0, not 1

# Per-request bound. Core builds the shared connection with timeout=10, which
# consumers cannot override and which exceeds a whole 10 s poll cycle.
REQUEST_TIMEOUT = 5.0  # seconds

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
    """Any Modbus-transport failure: timeout, link loss, or error response.

    ``device_answered`` is True when the controller replied with a Modbus
    exception response. The link itself is healthy in that case, so the retry
    must not tear it down — it is shared, and dropping it would punish any
    other integration on the same bus for what is our own bad request.
    """

    def __init__(self, message: str, *, device_answered: bool = False) -> None:
        super().__init__(message)
        self.device_answered = device_answered


def tcp_params(host: str, port: int) -> ModbusTcpParams:
    """The link settings a Parmair MAC endpoint is borrowed with.

    One definition shared by both borrow sites: Home Assistant keys shared
    connections by endpoint and refuses a second holder whose link settings
    differ, so the two must not drift apart.
    """
    return ModbusTcpParams(host=host, port=port)


def create_client(unit: ModbusUnit) -> ParmairModbusClient:
    """Module-level factory for :class:`ParmairModbusClient`.

    A seam for tests: patch this name (not the class) to substitute a fake
    client without touching the real transport.
    """
    return ParmairModbusClient(unit)


class ParmairModbusClient:
    """Parmair transaction policy over a borrowed, shared ``ModbusUnit``."""

    def __init__(self, unit: ModbusUnit) -> None:
        self._unit = unit
        self._lock = asyncio.Lock()
        self._closed = False
        # Counts only drops we did not cause: _drop_link() does not fire this.
        self.link_drops = 0
        unit.set_message_spacing(INTER_TRANSACTION_DELAY)
        self._unsub_lost: Callable[[], None] | None = unit.on_connection_lost(
            self._on_connection_lost
        )

    @property
    def connected(self) -> bool:
        return self._unit.connected

    async def connect(self) -> None:
        """Settle the link with a warm-up read, proving the unit answers.

        Not a socket open: borrowing a unit does no I/O and the shared
        connection dials lazily, so without one eager round trip setup would
        succeed against an unreachable unit and only fail on the first real
        read. Callers holding the lock use :meth:`_ensure_link` instead.
        """
        async with self._lock:
            await self._warm_up()

    async def close(self) -> None:
        """Release our hold: refuse further I/O and stop watching the link.

        Never disconnects. The connection is shared and refcounted by the
        ``modbus`` integration, which closes it once the last config entry
        holding a unit on it unloads; that release runs from
        ``entry.async_on_unload`` after this.
        """
        self._closed = True
        if self._unsub_lost is not None:
            self._unsub_lost()
            self._unsub_lost = None

    def _on_connection_lost(self) -> None:
        """Note a link drop; deliberately neither reloads nor reconnects.

        The link comes back on the next request (:meth:`_ensure_link` re-warms
        it), so a reload would rebuild every entity for a routine event on a
        controller that is documented flaky — and, because the connection is
        shared, would thrash it for any other holder too.
        """
        self.link_drops += 1
        _LOGGER.debug("Parmair Modbus link dropped (%d so far)", self.link_drops)

    async def read_block(self, address: int, count: int) -> list[int]:
        async with self._lock:
            return await self._retry(lambda: self._read_once(address, count))

    async def write_register(self, address: int, value: int) -> None:
        async with self._lock:
            await self._retry(lambda: self._write_once(address, value))

    async def _retry(self, op: Callable[[], Awaitable[T]]) -> T:
        """Run ``op`` with bounded retries, discarding a wedged link as it goes.

        Every failure this module can see is a translated
        :class:`ParmairConnectionError`, and most of them may mean the link
        itself is unusable, so it is dropped after each failed attempt — the
        last one included, so a caller that gives up doesn't leave the next one
        a link we already concluded was broken. The next attempt (or the next
        transaction) warms a fresh one. An error *response* is the exception:
        the controller demonstrably answered, so the link is kept.

        A link that cannot even be warmed up short-circuits the attempt, so
        ``op`` is never issued onto one known to be down.
        """
        last_err: ParmairConnectionError | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                await self._ensure_link()
                return await op()
            except ParmairConnectionError as err:
                last_err = err
                await self._drop_link(err)
                if attempt == MAX_ATTEMPTS - 1:
                    break
                _LOGGER.debug("Parmair transaction failed (attempt %d): %s", attempt + 1, err)
                await asyncio.sleep(RETRY_BACKOFF[attempt])
        assert last_err is not None
        raise last_err

    async def _ensure_link(self) -> None:
        """Warm the link up whenever it is down.

        Gating on ``connected`` rather than on a "we saw a drop" flag is
        deliberate and strictly stronger: :meth:`_drop_link` does not fire the
        lost-link callback, the backend drops the link itself on a desync, and
        another holder can drop it too. ``connected`` observes all three.
        """
        if not self._unit.connected:
            await self._warm_up()

    async def _warm_up(self) -> None:
        """One tolerated-failure warm-up read (the fresh-connect reply is flaky).

        A single register is read twice at most: the first failure is
        swallowed after a short pause, the second attempt's failure (if any)
        propagates. Not lock-guarded — every caller already holds the lock.
        """
        try:
            await self._read_once(WARM_UP_ADDRESS, 1)
        except ParmairConnectionError as err:
            _LOGGER.debug("Parmair warm-up read failed once (tolerated): %s", err)
            await asyncio.sleep(WARM_UP_PAUSE)
            await self._read_once(WARM_UP_ADDRESS, 1)

    async def _drop_link(self, err: ParmairConnectionError) -> None:
        """Discard the link so the next attempt dials and warms a fresh one."""
        if err.device_answered:
            return
        try:
            await self._unit.disconnect()
        except Exception as drop_err:  # noqa: BLE001 - teardown is best-effort
            _LOGGER.debug("Parmair link disconnect failed: %s", drop_err)

    async def _read_once(self, address: int, count: int) -> list[int]:
        return list(
            await self._call(
                f"read {address}/{count}",
                lambda: self._unit.read_holding_registers(address, count),
            )
        )

    async def _write_once(self, address: int, value: int) -> None:
        await self._call(
            f"write {address}={value}",
            lambda: self._unit.write_register(address, value),
        )

    async def _call(self, what: str, op: Callable[[], Awaitable[T]]) -> T:
        """One request, bounded in time and with every failure translated.

        ``op`` is a zero-argument callable rather than a coroutine so the
        released-client guard can refuse without leaving one un-awaited.
        """
        if self._closed:
            raise ParmairConnectionError(f"{what} attempted after release")
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                return await op()
        except ModbusExceptionError as err:
            raise ParmairConnectionError(f"{what} rejected: {err}", device_answered=True) from err
        except (ModbusError, TimeoutError, OSError) as err:
            raise ParmairConnectionError(f"{what} failed: {type(err).__name__}: {err}") from err
