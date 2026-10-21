"""Checks on the public build: neutral defaults, annotations, and a manifest
that agrees with the server and with itself."""

import asyncio
import json
import re
import tomllib
from pathlib import Path

import pytest

import server

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
MARKETPLACE = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
MCP_CONFIG = json.loads((ROOT / ".mcp.json").read_text())
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
TOOLS = asyncio.run(server.mcp.list_tools())


def test_versions_agree():
    assert server.__version__ == MANIFEST["version"] == PYPROJECT["project"]["version"]


def test_no_tenant_defaults_ship():
    assert server.DEFAULT_SHOPIFY_SUB_SOURCE == ""
    assert server._INTERNAL_SKUS == set()
    assert server._PICKWAVE_READY_TAG == ""


def test_every_tool_is_annotated():
    assert len(TOOLS) >= 100
    for tool in TOOLS:
        assert tool.title, tool.name
        assert tool.annotations is not None and tool.annotations.readOnlyHint in (True, False)


def test_readme_tool_count_and_disclosure_are_current():
    readme = (ROOT / "README.md").read_text()
    assert f"with {len(TOOLS)} tools" in readme
    start, end = "<!-- DISCLOSURE:START -->", "<!-- DISCLOSURE:END -->"
    section = readme.split(start, 1)[1].split(end, 1)[0]
    listed = set(re.findall(r"`([a-z_]+)`", section))
    assert {t.name for t in TOOLS} <= listed


def test_mcp_config_only_references_declared_settings():
    declared = set(MANIFEST["userConfig"])
    env = MCP_CONFIG["mcpServers"]["linnworks"]["env"]
    referenced = {m for v in env.values() for m in re.findall(r"\$\{user_config\.(\w+)\}", v)}
    assert referenced == declared


@pytest.mark.parametrize("key", ["application_id", "application_secret", "installation_token"])
def test_credentials_are_required_and_sensitive(key):
    field = MANIFEST["userConfig"][key]
    assert field["required"] is True and field["sensitive"] is True


def test_launch_command_is_plain_and_locked():
    server_cfg = MCP_CONFIG["mcpServers"]["linnworks"]
    assert server_cfg["command"] == "uv"
    assert server_cfg["args"][:2] == ["run", "--frozen"]
    assert (ROOT / "uv.lock").exists()


def test_marketplace_lists_this_plugin():
    [entry] = MARKETPLACE["plugins"]
    assert entry["name"] == MANIFEST["name"] and entry["source"] == "./"


def test_unsubstituted_optional_settings_are_ignored(monkeypatch):
    monkeypatch.setenv("X_PLUGIN_TEST", "${user_config.default_shopify_store}")
    assert server._env("X_PLUGIN_TEST") is None


def test_listing_tools_ask_for_a_store_when_none_is_set(monkeypatch):
    monkeypatch.setattr(server, "_fetch_glt_configurators", lambda _t: [
        {"name": "Decks", "id": 1, "channel_id": 18, "sub_source": "Store A"},
    ])
    out = server.refresh_channel_listing(skus=["ABC-1"])
    assert "sub_source is required" in out["error"]
    assert out["available_sub_sources"] == ["Store A"]


ACCOUNT_EVIDENCE = re.compile(
    r"\b6[01]\d{4}\b|vnm[-_]|\bven[-_]|catnip|\bB0[0-9A-Z]{8}\b|this tenant"
    r"|order-sync-service|Skatewarehouse|\bSWH\b|Venom|Rock Solid|Shiner"
    r"|linnworks-mcp|CLAUDE\.md|development repo|(?<![\w&])#\d{1,3}\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize("name", [
    "server.py", "README.md", "CHANGELOG.md", ".env.example",
    ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
])
def test_nothing_describes_one_particular_account(name):
    text = (ROOT / name).read_text()
    found = sorted({m.group(0) for m in ACCOUNT_EVIDENCE.finditer(text)})
    assert not found, f"{name} names account-specific details: {found}"
