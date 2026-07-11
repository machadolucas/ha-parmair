"""M7 bundled Lovelace card: static-file sanity + the ``__init__.py`` registration path.

``_async_register_frontend`` (see ``custom_components/parmair/__init__.py``) is
best-effort: it registers a static route for the card file, then either adds it
to the Lovelace *resource* registry (storage mode) or falls back to
``add_extra_js_url``. A bare ``hass`` test fixture has neither ``frontend`` nor
``lovelace`` set up, so the resource path can't run — that's exercised by
``test_setup_succeeds_when_frontend_is_unavailable`` below (mirroring what
``tests/ha/test_init.py::test_frontend_registration_is_a_noop_without_the_card_file``
checked before this file existed: setup must never fail because of the card).
``test_setup_registers_the_lovelace_resource_when_frontend_is_available`` spins
up ``http`` + ``lovelace`` first so the full registration path — including the
content-hash cache-buster — can be verified end to end.
"""

from __future__ import annotations

import re
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.parmair.const import CARD_FILENAME, CARD_URL_BASE, DOMAIN

# tests/ha/test_frontend.py -> tests/ha -> tests -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
CARD_PATH = _REPO_ROOT / "custom_components" / "parmair" / "frontend" / CARD_FILENAME


def _read_card_source() -> str:
    return CARD_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Static-file sanity: no HA instance needed, just the shipped JS source.
# ---------------------------------------------------------------------------


def test_card_file_exists_and_is_non_empty():
    source = _read_card_source()
    assert len(source) > 0


def test_card_file_is_under_100kb():
    source = _read_card_source()
    assert len(source.encode("utf-8")) < 100_000


def test_card_defines_the_custom_elements():
    source = _read_card_source()
    assert "parmair-card" in source
    assert "customElements.define" in source
    # Both the card and its editor are registered (M7 spec: two elements).
    assert "parmair-card-editor" in source


def test_card_registers_itself_with_the_lovelace_card_picker():
    source = _read_card_source()
    assert "window.customCards" in source
    assert re.search(r'type:\s*"parmair-card"', source)


def test_card_has_no_import_statements():
    """Self-contained: no build step, no external module imports."""
    source = _read_card_source()
    assert not re.search(r"^\s*import\s", source, re.MULTILINE)


def test_card_braces_and_parens_are_balanced():
    """Quick heuristic to catch a mis-paste/truncation, not a real parser."""
    source = _read_card_source()
    # Strip line comments and block comments so braces inside comments/URLs
    # (e.g. the file's own header comment) can't skew the count.
    without_block_comments = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    without_comments = re.sub(r"//[^\n]*", "", without_block_comments)
    assert without_comments.count("{") == without_comments.count("}")
    assert without_comments.count("(") == without_comments.count(")")


# ---------------------------------------------------------------------------
# `__init__.py` registration path, exercised against a real (test) HA core.
# ---------------------------------------------------------------------------


async def test_setup_succeeds_when_frontend_is_unavailable(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """Bare test ``hass`` has neither ``frontend`` nor ``lovelace`` set up.

    ``add_extra_js_url`` then fails (no ``frontend`` data key) inside the
    ``try/except`` in ``_async_register_frontend``, so registration silently
    no-ops — but setup itself must still succeed, and the card marker must
    stay unset (nothing was actually registered).
    """
    from homeassistant.config_entries import ConfigEntryState

    entry, _fake_client = await async_setup_integration(rexo120_bank)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.data.get(f"{DOMAIN}_card") is not True


async def test_setup_registers_the_lovelace_resource_when_frontend_is_available(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """With ``http`` + ``lovelace`` (storage mode) up front, the full path runs:

    static route registration, then the resource-registry write with a
    ``?v=<hash>`` cache-buster on the URL.
    """
    from homeassistant.components.lovelace.const import LOVELACE_DATA
    from homeassistant.config_entries import ConfigEntryState

    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "lovelace", {"lovelace": {"mode": "storage"}})
    await hass.async_block_till_done()

    entry, _fake_client = await async_setup_integration(rexo120_bank)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.data.get(f"{DOMAIN}_card") is True

    lovelace_data = hass.data[LOVELACE_DATA]
    items = lovelace_data.resources.async_items()
    base_url = f"{CARD_URL_BASE}/{CARD_FILENAME}"
    matches = [item for item in items if str(item["url"]).startswith(base_url)]
    assert len(matches) == 1
    assert matches[0]["type"] == "module"
    # Cache-buster: `?v=<8-hex-char content hash>`.
    assert re.fullmatch(rf"{re.escape(base_url)}\?v=[0-9a-f]{{8}}", matches[0]["url"])


async def test_setup_registration_is_idempotent_across_reloads(
    hass: HomeAssistant, async_setup_integration, rexo120_bank: dict[int, int]
) -> None:
    """Reloading the entry must not accumulate duplicate Lovelace resources."""
    from unittest.mock import patch

    from conftest import FakeModbusClient
    from homeassistant.components.lovelace.const import LOVELACE_DATA

    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "lovelace", {"lovelace": {"mode": "storage"}})
    await hass.async_block_till_done()

    entry, _fake_client = await async_setup_integration(rexo120_bank)
    assert hass.data.get(f"{DOMAIN}_card") is True

    # `async_setup_integration`'s Modbus-client patch only spans the initial
    # setup call, so re-patch it here for the reload (a real TCP connect
    # attempt would otherwise fail the reload for reasons unrelated to the
    # card registration this test is about).
    #
    # `_async_register_frontend` short-circuits once `hass.data[f"{DOMAIN}_card"]`
    # is set, so a reload must not attempt (or duplicate) registration again.
    with patch(
        "custom_components.parmair.modbus.create_client",
        return_value=FakeModbusClient(rexo120_bank),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    lovelace_data = hass.data[LOVELACE_DATA]
    base_url = f"{CARD_URL_BASE}/{CARD_FILENAME}"
    items = lovelace_data.resources.async_items()
    matches = [item for item in items if str(item["url"]).startswith(base_url)]
    assert len(matches) == 1
