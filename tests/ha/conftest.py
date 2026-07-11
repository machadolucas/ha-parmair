"""Fixtures for the Home-Assistant-backed Parmair tests.

``rexo120_bank`` is a real register snapshot taken from a live probe of the
Tampere-house unit (see the M1 probe notes) — using real data means the
derived capabilities/read-plan fixtures below exercise the exact shape
production sees, rather than a synthetic stand-in that might not match how
gaps/capability-gating actually lay out in practice.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.parmair.capabilities import parse_capabilities
from custom_components.parmair.const import (
    CONF_CAPABILITIES,
    CONF_CO2_OFFSET,
    CONF_REGISTER_MAP,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.parmair.modbus import ParmairConnectionError
from custom_components.parmair.registers import MAP_V1_87, decode


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load the custom integration in every HA test."""
    yield


def w(value: int) -> int:
    """Two's-complement encode a (possibly negative) value into an int16 word."""
    return value & 0xFFFF


# Real register snapshot from a live probe of the Tampere Rexo 120 unit.
# Addresses not listed here read as 0 (see FakeModbusClient.read_block) — that
# covers every gap word skipped by registers.build_read_plan, plus any
# register genuinely at 0/off on the live unit.
_REXO120_BANK: dict[int, int] = {
    1003: 1,
    1016: 2,
    1017: 272,
    1018: 187,
    1019: 244,
    1020: 254,
    1022: 235,
    1023: 249,
    1024: 232,
    1025: 239,
    1026: 54,
    1028: 1,
    1029: 1,
    1030: 65535,
    1031: 969,
    1040: 288,
    1042: 400,
    1046: 1000,
    1060: 210,
    1061: 80,
    1062: 40,
    1065: 180,
    1078: 80,
    1079: 1,
    1080: w(-80),
    1085: 2,
    1086: 16,
    1087: 5,
    1088: 2026,
    1089: 16,
    1090: 11,
    1091: 2026,
    1092: 999,
    1093: 1200,
    1096: 150,
    1097: 60,
    1098: 100,
    1099: 15,
    1104: 2,
    1105: 1,
    1106: 4,
    1107: 0,
    1108: 1,
    1109: 1,
    1114: 10,
    1115: 20,
    1116: 0,
    1117: 4,
    1120: 10,
    1121: 20,
    1122: 40,
    1123: 65,
    1124: 80,
    1180: 65535,
    1183: 0,
    1184: 72,
    1185: 2,
    1186: 3,
    1187: 0,
    1188: 1,
    1189: 3,
    1190: 864,
    1191: 0,
    1192: 543,
    1193: 600,
    1194: 1,
    1200: 1,
    1201: 0,
    1202: 65535,
    1203: 0,
    1204: 65535,
    1205: 1,
    1206: 0,
    1207: 1,
    1208: 3,
    1209: 0,
    **{addr: 0 for addr in range(1220, 1238)},
    1238: 65535,
    1240: 1,
    1241: 0,
    1242: 1,
    1243: 3,
    1244: 120,
    1245: 0,
}


@pytest.fixture
def rexo120_bank() -> dict[int, int]:
    """A fresh copy of the live-probe register snapshot, safe for tests to mutate."""
    return dict(_REXO120_BANK)


@pytest.fixture
def rexo120_capabilities_dict(rexo120_bank: dict[int, int]) -> dict[str, Any]:
    """What config flow would have stored, derived from the raw bank via decode()."""

    def _value(key: str) -> float | int | None:
        definition = MAP_V1_87.registers[key]
        address = MAP_V1_87.address(definition)
        return decode(rexo120_bank.get(address, 0), definition)

    static_values = {
        key: _value(key)
        for key in (
            "machine_type",
            "heater_type",
            "recovery_type",
            "m10_sensor_type",
            "m12_usage",
            "m11_potentiometer_priority",
            "software_version",
            "firmware_version",
        )
    }
    probe_values = {key: _value(key) for key in ("co2", "wet_room_humidity", "humidity")}
    return parse_capabilities(static_values, probe_values).as_dict()


class FakeModbusClient:
    """In-memory stand-in for :class:`~custom_components.parmair.modbus.ParmairModbusClient`.

    Implements the exact surface the coordinator depends on (``connect``,
    ``close``, ``read_block``, ``write_register``, ``connected``) so
    ``__init__.py``/``coordinator.py`` never notice they're not talking to a
    real Modbus TCP connection. ``bank`` is keyed by on-wire address; writes
    are applied back into it so a subsequent verify-read observes them, just
    like the real controller.
    """

    def __init__(self, bank: dict[int, int]) -> None:
        self.bank = dict(bank)
        self.writes: list[tuple[int, int]] = []
        self.fail_reads_at: set[int] = set()
        self.fail_all = False
        self.fail_connect = False
        self.connect_calls = 0
        self.read_calls = 0
        self.close_calls = 0
        self.read_log: list[tuple[int, int]] = []
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.fail_connect:
            raise ParmairConnectionError("simulated connect failure")
        self._connected = True

    async def close(self) -> None:
        self.close_calls += 1
        self._connected = False

    async def read_block(self, address: int, count: int) -> list[int]:
        self.read_calls += 1
        self.read_log.append((address, count))
        if self.fail_all or address in self.fail_reads_at:
            raise ParmairConnectionError(f"simulated read failure at block {address}")
        return [self.bank.get(a, 0) for a in range(address, address + count)]

    async def write_register(self, address: int, value: int) -> None:
        self.writes.append((address, value))
        self.bank[address] = value


@pytest.fixture
def mock_config_entry(rexo120_capabilities_dict: dict[str, Any]) -> MockConfigEntry:
    """A config entry matching what M3's config flow would produce (not yet added to hass)."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.168.101.56",
            CONF_PORT: 502,
            CONF_REGISTER_MAP: "v1_87",
            CONF_CAPABILITIES: rexo120_capabilities_dict,
        },
        options={CONF_SCAN_INTERVAL: 10, CONF_CO2_OFFSET: -480},
        title="Parmair",
    )


@pytest.fixture
def async_setup_integration(hass, mock_config_entry: MockConfigEntry):
    """Factory: set up the integration against a ``FakeModbusClient`` over ``bank``.

    Patches ``custom_components.parmair.modbus.create_client`` — the module-level
    factory ``__init__.py`` calls — so no real Modbus TCP connection is attempted.
    Returns ``(entry, fake_client)`` regardless of whether setup actually
    succeeded, so callers can also exercise the ``fail_connect``/``ConfigEntryNotReady``
    path (check ``entry.state`` themselves).
    """

    async def _setup(bank: dict[int, int]) -> tuple[MockConfigEntry, FakeModbusClient]:
        fake_client = FakeModbusClient(bank)
        with patch("custom_components.parmair.modbus.create_client", return_value=fake_client):
            mock_config_entry.add_to_hass(hass)
            await hass.config_entries.async_setup(mock_config_entry.entry_id)
            await hass.async_block_till_done()
        return mock_config_entry, fake_client

    return _setup
