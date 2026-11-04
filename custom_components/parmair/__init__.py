"""The Parmair MAC integration.

One config entry per physical unit. The Modbus unit is *borrowed* from the
``modbus`` integration, which shares and refcounts one connection per endpoint,
wrapped in a ``ParmairModbusClient`` for the Multi24's transaction policy, and
fed to a single :class:`~.coordinator.ParmairCoordinator` that every platform
module reads through :class:`~.entity.ParmairEntity`.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
from typing import TYPE_CHECKING, Any

from homeassistant.components.modbus import async_get_unit
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from . import modbus
from .capabilities import Capabilities
from .const import (
    CARD_FILENAME,
    CARD_URL_BASE,
    CONF_CAPABILITIES,
    CONF_REGISTER_MAP,
    COOKING_STORAGE_KEY,
    COOKING_STORAGE_VERSION,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import ParmairConfigEntry, ParmairCoordinator, async_delete_issues
from .modbus import ParmairConnectionError
from .registers import DEFAULT_MAP, REGISTER_MAPS

if TYPE_CHECKING:
    from homeassistant.components.lovelace.resources import ResourceStorageCollection

_LOGGER = logging.getLogger(__name__)

# Tracked separately: the static route can only ever be registered once per
# process (aiohttp rejects a duplicate), while the resource step may fail and
# must stay retryable on the next setup/reload.
_CARD_ROUTE_KEY = f"{DOMAIN}_card_route"
_CARD_RESOURCE_KEY = f"{DOMAIN}_card"


def _card_version(path: pathlib.Path) -> str:
    """A short content hash of the card file, for cache-busting its URL."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


async def _async_resource_collection(hass: HomeAssistant) -> ResourceStorageCollection | None:
    """The Lovelace storage-mode resource collection, loaded; None if unusable.

    ``None`` covers YAML resource mode (can't be edited programmatically) and
    Lovelace not being set up at all.
    """
    try:
        from homeassistant.components.lovelace.const import LOVELACE_DATA
        from homeassistant.components.lovelace.resources import ResourceStorageCollection
    except ImportError:
        return None
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        return None
    resources = data.resources
    if not isinstance(resources, ResourceStorageCollection):
        return None
    if not resources.loaded:
        await resources.async_load()
        resources.loaded = True
    return resources


def _card_resource_items(resources: ResourceStorageCollection, base: str) -> list[dict[str, Any]]:
    """Resource items pointing at the card, whatever cache-buster they carry."""
    return [
        item
        for item in resources.async_items()
        if str(item.get("url", "")).split("?", 1)[0] == base
    ]


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
    resources = await _async_resource_collection(hass)
    if resources is None:
        return False
    existing = _card_resource_items(resources, url.split("?", 1)[0])
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

    The route and the resource are flagged separately so a resource step that
    failed is retried on the next setup without re-adding the route, which
    aiohttp would refuse as a duplicate — failing that retry before it began.
    """
    if hass.data.get(_CARD_RESOURCE_KEY):
        return
    path = pathlib.Path(__file__).parent / "frontend" / CARD_FILENAME
    if not path.exists():
        _LOGGER.debug("Parmair card not registered: %s not found yet", path)
        return
    card_url = f"{CARD_URL_BASE}/{CARD_FILENAME}"
    if not hass.data.get(_CARD_ROUTE_KEY):
        try:
            from homeassistant.components.http import StaticPathConfig

            # The static route comes *first*: serving the card must never
            # depend on the cache-buster or the resource registry below.
            await hass.http.async_register_static_paths(
                [StaticPathConfig(card_url, str(path), True)]
            )
        except Exception as err:  # noqa: BLE001 - best-effort; core may be partial
            _LOGGER.debug("Parmair card route not registered: %s", err)
            return
        hass.data[_CARD_ROUTE_KEY] = True
    try:
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
        hass.data[_CARD_RESOURCE_KEY] = True
    except Exception as err:  # noqa: BLE001 - best-effort; core may be partial
        _LOGGER.debug("Parmair card not registered: %s", err)


async def _async_unregister_resource(hass: HomeAssistant) -> None:
    """Remove the card from the Lovelace resource registry (storage mode, best-effort).

    Only the resource: aiohttp cannot drop a static route, and an
    ``add_extra_js_url`` fallback (YAML mode) is left as is — its flag then
    stays set, so a re-added entry doesn't inject the script a second time.
    """
    try:
        resources = await _async_resource_collection(hass)
        if resources is None:
            return
        for item in _card_resource_items(resources, f"{CARD_URL_BASE}/{CARD_FILENAME}"):
            await resources.async_delete_item(item["id"])
        hass.data.pop(_CARD_RESOURCE_KEY, None)
    except Exception as err:  # noqa: BLE001 - best-effort; core may be partial
        _LOGGER.debug("Parmair card resource not removed: %s", err)


async def async_setup_entry(hass: HomeAssistant, entry: ParmairConfigEntry) -> bool:
    """Set up Parmair MAC from a config entry."""
    host = entry.data[CONF_HOST]
    port = int(entry.data[CONF_PORT])

    # Borrowing does no I/O, so an unreachable unit does not fail here — the
    # first read in async_setup() below is what proves it answers.
    try:
        unit = async_get_unit(hass, entry, modbus.tcp_params(host, port), modbus.UNIT_ID_DEFAULT)
    except HomeAssistantError as err:
        # Something else already holds this endpoint with different link
        # settings, which one connection cannot honour. Retrying cannot fix
        # that; the user has to reconcile the two configurations.
        raise ConfigEntryError(
            f"Cannot share the Modbus connection to {host}:{port}: {err}"
        ) from err

    client = modbus.create_client(unit)
    # Registered before anything can fail (and after async_get_unit's own
    # release, so LIFO runs it before that): a ConfigEntryNotReady from
    # async_setup() or the first refresh still unsubscribes the lost-link
    # callback. On a normal
    # unload the on_unload stack runs listeners -> coordinator.async_shutdown
    # (DataUpdateCoordinator registers it) -> client.close -> modbus release.
    entry.async_on_unload(client.close)
    capabilities = Capabilities.from_dict(entry.data[CONF_CAPABILITIES])
    register_map = REGISTER_MAPS[entry.data.get(CONF_REGISTER_MAP, DEFAULT_MAP)]
    coordinator = ParmairCoordinator(hass, entry, client, register_map, capabilities)

    try:
        await coordinator.async_setup()
    except ParmairConnectionError as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to Parmair unit at {host}:{port}: {err}"
        ) from err

    # Registered explicitly (not left to an entity's device_info) so the
    # device exists from setup on regardless of which entities get created
    # placeholders with no real entities yet to trigger it implicitly.
    if coordinator.device_info is not None:
        registry = dr.async_get(hass)
        if hasattr(registry, "async_get_or_create_from_info"):
            # HA 2026.11+: DeviceInfo is a dataclass and can no longer be
            # unpacked with ** (contributors blog 2026-10-13, "DeviceInfo is
            # now a dataclass"). Drop the else branch once hacs.json requires
            # 2026.11.
            registry.async_get_or_create_from_info(
                config_entry_id=entry.entry_id, device_info=coordinator.device_info
            )
        else:
            registry.async_get_or_create(
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
    """Unload a config entry's platforms.

    Everything else rides ``entry.async_on_unload`` (see async_setup_entry):
    the coordinator shutdown (timers, cooking flush), then ``client.close()`` —
    which only gives up our hold — then the ``modbus`` integration's release,
    which closes the shared connection only when no other entry still holds a
    unit on it.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ParmairConfigEntry) -> None:
    """Clean up what outlives an unload: repair issues, learned baselines, the card.

    The card's resource is shared by every entry, so it goes only with the
    last one.
    """
    async_delete_issues(hass, entry.entry_id)
    try:
        await Store(
            hass, COOKING_STORAGE_VERSION, COOKING_STORAGE_KEY.format(entry.entry_id)
        ).async_remove()
    except OSError as err:
        _LOGGER.debug("Parmair cooking baselines not removed: %s", err)
    if not any(
        other.entry_id != entry.entry_id for other in hass.config_entries.async_entries(DOMAIN)
    ):
        await _async_unregister_resource(hass)


async def _async_reload_entry(hass: HomeAssistant, entry: ParmairConfigEntry) -> None:
    """Reload on options changes (e.g. scan interval, summer-auto source)."""
    await hass.config_entries.async_reload(entry.entry_id)
