"""Parmair MAC config flow.

The user flow connects to the unit once (a Modbus TCP round trip), reads the
static configuration registers plus a one-shot probe of the optional
sensors, and derives :class:`~.capabilities.Capabilities` — the same
detection the coordinator relies on at every restart, done here once so
restarts don't need to re-probe. A confirm step shows what was detected
before the entry is created. The options flow lets the user tune the poll
interval, a CO2 calibration offset, an optional external temperature source
for the summer-mode automation, and re-run the same probe (e.g. after
physically adding a sensor) without deleting and re-adding the integration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import modbus
from .capabilities import (
    HEATER_TYPE_ELECTRIC,
    HEATER_TYPE_NONE,
    HEATER_TYPE_WATER,
    Capabilities,
    parse_capabilities,
)
from .const import (
    CONF_CAPABILITIES,
    CONF_CO2_OFFSET,
    CONF_REDETECT,
    CONF_REGISTER_MAP,
    CONF_SCAN_INTERVAL,
    CONF_SUMMER_AUTO_SOURCE,
    DEFAULT_CO2_OFFSET,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .modbus import ParmairConnectionError
from .registers import DEFAULT_MAP, MAP_V1_87, build_read_plan, decode

# Static registers that drive capability detection (heater/recovery/M10-M12
# wiring + firmware/software versions).
_STATIC_KEYS = [key for key, definition in MAP_V1_87.registers.items() if definition.static]

# One-shot probe of the optional sensors that aren't implied by the static
# wiring registers (see capabilities.parse_capabilities' docstring).
_PROBE_KEYS = ("co2", "wet_room_humidity", "humidity")

# Sanity bounds on the static registers used to decide "is this really a
# Parmair MAC unit" versus e.g. a random Modbus TCP endpoint that happened to
# answer function code 03. machine_type is documented 1-600; software_version
# decodes to a small positive float like 1.87.
_MACHINE_TYPE_MIN = 1
_MACHINE_TYPE_MAX = 600

_RECOVERY_TYPE_NAMES = {0: "rotary", 1: "plate"}
_HEATER_TYPE_NAMES = {
    HEATER_TYPE_WATER: "water",
    HEATER_TYPE_ELECTRIC: "electric",
    HEATER_TYPE_NONE: "none",
}


class NotParmairError(Exception):
    """The probed endpoint answered Modbus but doesn't look like a Parmair MAC unit."""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of a successful :func:`async_probe_device` call."""

    capabilities: Capabilities
    register_map: str


async def async_probe_device(host: str, port: int) -> ProbeResult:
    """Connect once, read the static + probe registers, and detect capabilities.

    Shared by the user flow and the options flow's redetect step. Raises
    :class:`~.modbus.ParmairConnectionError` for any transport failure
    (connect or read) and :class:`NotParmairError` when the connection
    succeeds but the static registers don't look like a Parmair MAC unit.
    """
    client = modbus.create_client(host, port)
    try:
        await client.connect()

        static_values: dict[str, float | int | None] = {}
        for block in build_read_plan(MAP_V1_87, _STATIC_KEYS):
            raw = await client.read_block(block.address, block.count)
            for key in block.keys:
                definition = MAP_V1_87.registers[key]
                offset = MAP_V1_87.address(definition) - block.address
                static_values[key] = decode(raw[offset], definition)

        probe_values: dict[str, int | None] = {}
        for block in build_read_plan(MAP_V1_87, _PROBE_KEYS):
            raw = await client.read_block(block.address, block.count)
            for key in block.keys:
                definition = MAP_V1_87.registers[key]
                offset = MAP_V1_87.address(definition) - block.address
                probe_values[key] = decode(raw[offset], definition)

        machine_type = static_values.get("machine_type")
        software_version = static_values.get("software_version")
        if (
            machine_type is None
            or not (_MACHINE_TYPE_MIN <= machine_type <= _MACHINE_TYPE_MAX)
            or software_version is None
            or software_version <= 0
        ):
            raise NotParmairError(
                f"unexpected static registers (machine_type={machine_type!r}, "
                f"software_version={software_version!r})"
            )

        capabilities = parse_capabilities(static_values, probe_values)
    finally:
        await client.close()

    return ProbeResult(capabilities=capabilities, register_map=DEFAULT_MAP)


def _features_summary(capabilities: Capabilities) -> str:
    """A short human-readable line for the confirm step's description placeholder."""
    parts = [
        f"CO₂ sensor: {'yes' if capabilities.has_co2 else 'no'}",
        f"Wet room humidity sensor: {'yes' if capabilities.has_wet_room_humidity else 'no'}",
        f"Heater: {_HEATER_TYPE_NAMES.get(capabilities.heater_type, 'unknown')}",
        f"Recovery: {_RECOVERY_TYPE_NAMES.get(capabilities.recovery_type, 'unknown')}",
    ]
    return " · ".join(parts)


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_HOST, description={"suggested_value": defaults.get(CONF_HOST)}
            ): selector.TextSelector(),
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=65535, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)
            ): selector.TextSelector(),
            vol.Required(
                CONF_SCAN_INTERVAL, default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5,
                    max=120,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    def suggest(key: str) -> dict[str, Any]:
        return {"suggested_value": defaults.get(key)}

    return vol.Schema(
        {
            vol.Required(
                CONF_SCAN_INTERVAL, description=suggest(CONF_SCAN_INTERVAL)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5,
                    max=120,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_CO2_OFFSET, description=suggest(CONF_CO2_OFFSET)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=-1000,
                    max=1000,
                    unit_of_measurement="ppm",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_SUMMER_AUTO_SOURCE, description=suggest(CONF_SUMMER_AUTO_SOURCE)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            vol.Required(CONF_REDETECT, default=False): selector.BooleanSelector(),
        }
    )


class ParmairConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Parmair MAC config flow: probe the unit, then confirm."""

    VERSION = 1

    def __init__(self) -> None:
        self._user_input: dict[str, Any] | None = None
        self._probe: ProbeResult | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect connection details and probe the unit."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = int(user_input[CONF_PORT])
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            try:
                probe = await async_probe_device(host, port)
            except NotParmairError:
                errors["base"] = "not_parmair"
            except ParmairConnectionError:
                errors["base"] = "cannot_connect"
            else:
                self._user_input = user_input
                self._probe = probe
                return await self.async_step_confirm()

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what was detected and create the entry on confirmation."""
        assert self._probe is not None
        assert self._user_input is not None
        capabilities = self._probe.capabilities

        if user_input is not None:
            data = {
                CONF_HOST: self._user_input[CONF_HOST],
                CONF_PORT: int(self._user_input[CONF_PORT]),
                CONF_REGISTER_MAP: self._probe.register_map,
                CONF_CAPABILITIES: capabilities.as_dict(),
            }
            options = {
                CONF_SCAN_INTERVAL: int(self._user_input[CONF_SCAN_INTERVAL]),
                CONF_CO2_OFFSET: DEFAULT_CO2_OFFSET,
            }
            return self.async_create_entry(
                title=self._user_input.get(CONF_NAME, DEFAULT_NAME),
                data=data,
                options=options,
            )

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "model": capabilities.model_name,
                "sw_version": capabilities.sw_version,
                "features": _features_summary(capabilities),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ParmairOptionsFlow:
        """This entry's options flow (scan interval, CO2 offset, summer-auto, redetect)."""
        return ParmairOptionsFlow()


class ParmairOptionsFlow(OptionsFlow):
    """Options flow: polling/calibration tuning, plus an on-demand redetect."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show/save the options form; optionally re-probe the unit first."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_REDETECT):
                try:
                    probe = await async_probe_device(
                        self.config_entry.data[CONF_HOST], self.config_entry.data[CONF_PORT]
                    )
                except (ParmairConnectionError, NotParmairError):
                    errors["base"] = "cannot_connect"
                else:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data={
                            **self.config_entry.data,
                            CONF_CAPABILITIES: probe.capabilities.as_dict(),
                        },
                    )

            if not errors:
                options = {
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_CO2_OFFSET: int(user_input[CONF_CO2_OFFSET]),
                }
                summer_auto_source = user_input.get(CONF_SUMMER_AUTO_SOURCE)
                if summer_auto_source:
                    options[CONF_SUMMER_AUTO_SOURCE] = summer_auto_source
                return self.async_create_entry(data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(user_input or dict(self.config_entry.options)),
            errors=errors,
        )
