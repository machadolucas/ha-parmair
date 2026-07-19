"""The Parmair MAC integration.

One config entry per physical unit: a single ``ParmairModbusClient`` Modbus
TCP connection feeds a single :class:`~.coordinator.ParmairCoordinator`, which
every platform module reads through
:class:`~.entity.ParmairEntity`.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from . import modbus
from .capabilities import Capabilities
from .const import (
    CARD_FILENAME,
    CARD_URL_BASE,
    CONF_CAPABILITIES,
    CONF_REGISTER_MAP,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import ParmairConfigEntry, ParmairCoordinator
from .modbus import ParmairConnectionError
from .registers import DEFAULT_MAP, REGISTER_MAPS

_LOGGER = logging.getLogger(__name__)


def _card_version(path: pathlib.Path) -> str:
    """A short content hash of the card file, for cache-busting its URL."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


async def _async_register_resource(hass: HomeAssistant, url: str) -> bool:
    """Add/refresh the card in the Lovelace resource registry (storage mode).

    Preferred over ``add_extra_js_url``: the resource registry is fetched by the
    frontend at runtime over WebSocket, so the card survives a stale app shell —
    a CDN edge or the service worker serving cached index HTML that omits an
    injected ``<script>`` (the failure mode where the browser never even requests
    the card). This is how HACS registers its cards. Returns ``True`` when
    handled, ``False`` when the registry isn't usable (YAML resource mode, or
    Lovelace not ready) so the caller can fall back to ``add_extra_js_url``.

    Idempotent across restarts: it matches on the URL path and updates the version
    in place, so the resource list never accumulates duplicates.
    """
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA
        from homeassistant.components.lovelace.resources import ResourceStorageCollection
    except ImportError:
        return False
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        return False
    resources = data.resources
    if not isinstance(resources, ResourceStorageCollection):
        return False  # YAML resource mode — can't be edited programmatically
    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True
    base = url.split("?", 1)[0]
    existing = [
        item
        for item in resources.async_items()
        if str(item.get("url", "")).split("?", 1)[0] == base
    ]
    if existing:
        keep, *dupes = existing
        if keep.get("url") != url:
            await resources.async_update_item(keep["id"], {"url": url})
        for dupe in dupes:  # collapse duplicates from older registrations
            await resources.async_delete_item(dupe["id"])
    else:
        await resources.async_create_item({"res_type": "module", "url": url})
    return True


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Register the bundled Lovelace card (best-effort).

    The card is added to the Lovelace **resource registry** (like HACS) rather
    than injected into the index HTML via ``add_extra_js_url``: an injected
    ``<script>`` is dropped whenever a cached app shell — a CDN edge or the
    frontend service worker serving stale index HTML — is used, which makes the
    card vanish ("Custom element doesn't exist") until a hard refresh. The
    resource registry is fetched by the frontend at runtime, so it is immune;
    ``add_extra_js_url`` remains a fallback for YAML resource mode.

    The file is served with long-lived cache headers (``cache_headers=True``) and
    the URL carries a ``?v=<content-hash>`` query so a changed card refetches
    while unchanged files stay cached. The static route matches on path only, so
    the query is ignored server-side; the bare path is what's registered.

    When the frontend file is missing (e.g. stripped installs) the
    file doesn't exist, and this silently no-ops (debug log) rather than
    registering a route/resource that would 404.
    """
    if hass.data.get(f"{DOMAIN}_card"):
        return
    path = pathlib.Path(__file__).parent / "frontend" / CARD_FILENAME
    if not path.exists():
        _LOGGER.debug("Parmair card not registered: %s not found yet", path)
        return
    try:
        from homeassistant.components.http import StaticPathConfig

        card_url = f"{CARD_URL_BASE}/{CARD_FILENAME}"
        # Register the static route *first*: serving the card must never depend on
        # the cache-buster below.
        await hass.http.async_register_static_paths([StaticPathConfig(card_url, str(path), True)])
        url = card_url
        try:
            version = await hass.async_add_executor_job(_card_version, path)
            url = f"{card_url}?v={version}"
        except Exception as err:  # noqa: BLE001 - cache-bust is best-effort
            _LOGGER.debug("Parmair card cache-buster skipped: %s", err)
        # Prefer the resource registry; fall back to extra-JS for YAML mode.
        if not await _async_register_resource(hass, url):
            from homeassistant.components.frontend import add_extra_js_url

            add_extra_js_url(hass, url)
        hass.data[f"{DOMAIN}_card"] = True
    except Exception as err:  # noqa: BLE001 - best-effort; core may be partial
        _LOGGER.debug("Parmair card not registered: %s", err)


async def async_setup_entry(hass: HomeAssistant, entry: ParmairConfigEntry) -> bool:
    """Set up Parmair MAC from a config entry."""
    client = modbus.create_client(entry.data[CONF_HOST], entry.data[CONF_PORT])
    capabilities = Capabilities.from_dict(entry.data[CONF_CAPABILITIES])
    register_map = REGISTER_MAPS[entry.data.get(CONF_REGISTER_MAP, DEFAULT_MAP)]
    coordinator = ParmairCoordinator(hass, entry, client, register_map, capabilities)

    try:
        await coordinator.async_setup()
    except ParmairConnectionError as err:
        raise ConfigEntryNotReady(f"Cannot connect to Parmair unit: {err}") from err

    # Registered explicitly (not left to an entity's device_info) so the
    # device exists from setup on regardless of which entities get created
    # placeholders with no real entities yet to trigger it implicitly.
    if coordinator.device_info is not None:
        dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, **coordinator.device_info
        )

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # After the first refresh (so self.data backs the boost guard) and before
    # platforms are forwarded (so the cooking entities find a live detector).
    await coordinator.async_setup_cooking()

    await _async_register_frontend(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ParmairConfigEntry) -> bool:
    """Unload a config entry: platforms, then the coordinator's connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = entry.runtime_data
        await coordinator.async_shutdown()  # cancels any pending verify timer
        await coordinator.client.close()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ParmairConfigEntry) -> None:
    """Reload on options changes (e.g. scan interval, summer-auto source)."""
    await hass.config_entries.async_reload(entry.entry_id)
