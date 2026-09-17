"""Preservation baselines for registry-pointer pass-through.

Task 1.2 records unfixed behavior before production code changes. These tests
cover only inputs outside the pointer bug condition: ordinary commands, remote
URLs, unmarked command-less entries, managed Kiro Crew servers, disabled probes,
and the public registry install translator. The mixed-map property must remain
green before and after pointer support lands.

All files live below ``tmp_path``. Subprocess and HTTP boundaries are patched;
no real ``kiro-cli`` process or network request runs.
"""

from __future__ import annotations

import copy
import itertools
import json
import os
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew import agent as agent_mod
from kiro_crew.agent import _MCP_REGISTRY_TYPE, install_agent
from kiro_crew.env import emit_env
from kiro_crew.mcp_discovery import McpServerInfo, _server_from_spec, probe_server
from kiro_crew.mcp_providers.official import translate_install_plan

_MANAGED: dict[str, dict[str, Any]] = {
    "kirocrew-cron": {"command": "/opt/kirocrew", "args": ["mcp-cron"]},
    "kirocrew-core": {"command": "/opt/kirocrew", "args": ["mcp-core"]},
}
_BASE_TOOL = "ReadFile"
_CASE_SEQUENCE = itertools.count()
_FIXED_PROMPT = Path("/fixture/prompt.md")

# Observation-first baseline, recorded on unfixed code. An inbound pointer on an
# undeclared/ungoverned install is dropped, and fresh-install shared-ref sync adds
# neither ref. Task 5.2 intentionally asserts the post-fix superset. This suite
# only requires that future behavior lose nothing reachable in this baseline.
_UNGOVERNED_UNFIXED_BASELINE: dict[str, Any] = {
    "mcpServers": {},
    "tools": [_BASE_TOOL],
    "allowedTools": [_BASE_TOOL],
}


def _defaults_doc() -> dict[str, Any]:
    return {
        "name": "kirocrew",
        "model": "auto",
        "prompt": f"file://{_FIXED_PROMPT}",
        "includeMcpJson": False,
        "tools": [_BASE_TOOL],
        "allowedTools": [_BASE_TOOL],
        "mcpServers": {},
        "hooks": {"preToolUse": "audit"},
    }


def _rebuild(
    root: Path,
    *,
    kiro_servers: dict[str, Any],
    kirocrew_servers: dict[str, Any] | None = None,
    provider_servers: dict[str, Any] | None = None,
    registry_mode: bool | None = None,
    managed: dict[str, dict[str, Any]] | None = None,
    existing_spec: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Build one isolated agent spec and return parsed and exact bytes.

    ``registry_mode=None`` models an undeclared setting. ``False`` models an
    explicit false declaration. Both are false on unfixed code; keeping the
    distinction in the call sites records which observation each test made.
    """
    root.mkdir(parents=True)
    config_dir = root / "config"
    config_dir.mkdir()
    (config_dir / "defaults.json").write_text(json.dumps(_defaults_doc()), encoding="utf-8")

    agents_dir = root / "agents"
    agents_dir.mkdir()
    spec_path = agents_dir / agent_mod.AGENT_FILENAME
    if existing_spec is not None:
        spec_path.write_text(json.dumps(existing_spec, indent=2) + "\n", encoding="utf-8")

    data_home = root / "data"
    data_home.mkdir()
    if kirocrew_servers is not None:
        (data_home / "mcp.json").write_text(
            json.dumps({"mcpServers": kirocrew_servers}), encoding="utf-8"
        )

    kiro_mcp = root / "kiro-mcp.json"
    kiro_mcp.write_text(json.dumps({"mcpServers": kiro_servers}), encoding="utf-8")
    provider_mcp = root / "provider-mcp.json"
    provider_mcp.write_text(json.dumps({"mcpServers": provider_servers or {}}), encoding="utf-8")
    mc_config = root / "kirocrew-config.json"
    agent_config: dict[str, Any] = {"kiro_hooks_autoimport": False}
    if registry_mode is not None:
        agent_config["mcp_registry_mode"] = registry_mode
    mc_config.write_text(json.dumps({"agent": agent_config}), encoding="utf-8")

    managed_servers = copy.deepcopy(_MANAGED if managed is None else managed)
    patches = [
        patch.multiple(
            "kiro_crew.agent",
            KIRO_AGENTS_DIR=agents_dir,
            _BUNDLED_CFG_DIR=config_dir,
            _KIROCREW_BIN="/opt/kirocrew",
            _MANAGED_MCP_SERVERS=managed_servers,
            _KIRO_MCP_JSON=kiro_mcp,
            _CC_MCP_JSON=provider_mcp,
        ),
        patch("kiro_crew.agent._user_dir", return_value=data_home),
        patch("kiro_crew.agent._prompt_path", return_value=_FIXED_PROMPT),
        patch("kiro_crew.agent._shipped_defaults", return_value=config_dir / "defaults.json"),
        patch("kiro_crew.agent._project_dir", return_value=None),
        patch("kiro_crew.agent._aim_skill_paths", return_value=[]),
        patch("kiro_crew.agent._mc_config_path", return_value=mc_config),
        patch("kiro_crew.agent._extra_mcp_scope_globals", return_value=[provider_mcp]),
        patch("kiro_crew.agent._extra_mcp_servers", return_value={}),
        patch("kiro_crew.agent._collect_app_mcp_servers", return_value={}),
        patch("kiro_crew.agent._managed_mcp_env", return_value={}),
        patch("kiro_crew.agent._gated_off_servers", return_value=frozenset()),
        patch("kiro_crew.agent._mcp_registry_mode", return_value=registry_mode is True),
        patch("kiro_crew.agent._may_auto_approve", return_value=True),
        patch("kiro_crew.agent._seed_kas_permissions", return_value=None),
        patch("kiro_crew.agent.shutil.which", side_effect=lambda command, **_: command),
    ]
    with ExitStack() as stack:
        for active_patch in patches:
            stack.enter_context(active_patch)
        path = install_agent()

    raw = path.read_bytes()
    return json.loads(raw), raw


def _wire_remote(spec: dict[str, Any]) -> dict[str, Any]:
    """Recorded unfixed remote emission, independent of production translator."""
    expected = copy.deepcopy(spec)
    scopes = expected.pop("scopes", None)
    client_id = expected.pop("clientId", None)
    if scopes:
        expected["oauthScopes"] = scopes
    else:
        expected.pop("oauthScopes", None)
    oauth = expected.get("oauth")
    wire_oauth = dict(oauth) if isinstance(oauth, dict) else {}
    wire_oauth.pop("clientId", None)
    if client_id:
        wire_oauth["clientId"] = client_id
    if wire_oauth:
        expected["oauth"] = wire_oauth
    else:
        expected.pop("oauth", None)
    return expected


@st.composite
def _mixed_non_pointer_maps(draw: st.DrawFn) -> dict[str, dict[str, Any]]:
    """Arbitrary maps containing every non-pointer input class from Property 2."""
    suffixes = draw(
        st.lists(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=6),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    servers: dict[str, dict[str, Any]] = {}
    for suffix in suffixes:
        command_name = f"command-{suffix}"
        command_args = draw(
            st.lists(
                st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=8),
                max_size=3,
            )
        )
        servers[command_name] = {
            "command": f"/opt/{command_name}",
            "args": command_args,
            "env": {"PATH": f"/opt/{suffix}/bin", "CASE": suffix},
            "timeout": draw(st.integers(min_value=1, max_value=120)),
        }

        remote_name = f"remote-{suffix}"
        scopes = draw(
            st.lists(
                st.text(alphabet="abcdefghijklmnopqrstuvwxyz:. ", min_size=1, max_size=8).filter(
                    str.strip
                ),
                min_size=1,
                max_size=3,
            )
        )
        servers[remote_name] = {
            "url": f"https://{suffix}.example.test/mcp",
            "headers": {"X-Case": suffix},
            "scopes": scopes,
            "clientId": f"client-{suffix}",
            "oauth": {"redirectUri": f"http://localhost/{suffix}/callback"},
        }

        broken_name = f"broken-{suffix}"
        servers[broken_name] = {
            "args": ["--orphan", suffix],
            "env": {"CASE": suffix},
        }

    # A source file cannot replace managed definitions. Arbitrary source values
    # under these names are deliberately ignored by the rebuild.
    for managed_name in _MANAGED:
        servers[managed_name] = draw(
            st.sampled_from(
                [
                    {},
                    {"command": "/tmp/wrong", "args": ["wrong"]},
                    {"url": "https://wrong.example.test/mcp"},
                ]
            )
        )
    return servers


class TestRecordedCommandBaselines:
    @pytest.mark.parametrize(
        ("kirocrew_servers", "expected_command", "expected_args", "expected_source"),
        [
            (
                {"shared": {"command": "/crew/server", "args": ["crew"], "env": {"SRC": "crew"}}},
                "/crew/server",
                ["crew"],
                "crew",
            ),
            (None, "/kiro/server", ["kiro"], "kiro"),
        ],
    )
    def test_candidate_priority_and_argv_pairing(
        self,
        tmp_path: Path,
        kirocrew_servers: dict[str, Any] | None,
        expected_command: str,
        expected_args: list[str],
        expected_source: str,
    ) -> None:
        """Observed priority is Kiro Crew, Kiro global, then provider global."""
        config, _ = _rebuild(
            tmp_path / expected_source,
            kiro_servers={
                "shared": {
                    "command": "/kiro/server",
                    "args": ["kiro"],
                    "env": {"SRC": "kiro"},
                }
            },
            kirocrew_servers=kirocrew_servers,
            provider_servers={
                "shared": {
                    "command": "/provider/server",
                    "args": ["provider"],
                    "env": {"SRC": "provider"},
                }
            },
        )
        emitted = config["mcpServers"]["shared"]
        assert emitted["command"] == expected_command
        assert emitted["args"] == expected_args
        assert emitted["env"]["SRC"] == expected_source

    def test_declared_path_is_expanded_without_losing_other_env(self, tmp_path: Path) -> None:
        config, _ = _rebuild(
            tmp_path / "path",
            kiro_servers={
                "wrapped": {
                    "command": "/opt/wrapped",
                    "args": ["serve"],
                    "env": {"PATH": "/opt/shims", "TOKEN_FILE": "/fixture/token"},
                }
            },
        )
        emitted = config["mcpServers"]["wrapped"]
        assert emitted["env"] == emit_env({"PATH": "/opt/shims", "TOKEN_FILE": "/fixture/token"})
        assert emitted["env"]["PATH"].split(os.pathsep)[0] == "/opt/shims"


class TestRecordedRemoteBaselines:
    def test_url_entry_uses_oauth_wire_shape(self, tmp_path: Path) -> None:
        source = {
            "url": "https://example.test/mcp",
            "headers": {"X-Test": "value"},
            "scopes": ["read", "write"],
            "clientId": "public-client",
            "oauth": {"redirectUri": "http://localhost/callback"},
        }
        config, _ = _rebuild(tmp_path / "wire", kiro_servers={"remote": source})
        assert config["mcpServers"]["remote"] == _wire_remote(source)

    @pytest.mark.asyncio
    async def test_remote_probe_uses_post_for_the_full_handshake(self) -> None:
        server = _server_from_spec(
            "remote",
            {"url": "https://example.test/mcp", "scopes": ["read"]},
            "mcp.json",
        )
        initialize = MagicMock()
        initialize.status = 200
        initialize.content_type = "application/json"
        initialize.headers = {}
        initialize.json = AsyncMock(return_value={"jsonrpc": "2.0", "id": 1, "result": {}})
        initialize.__aenter__ = AsyncMock(return_value=initialize)
        initialize.__aexit__ = AsyncMock(return_value=False)

        initialized = MagicMock()
        initialized.status = 202
        initialized.__aenter__ = AsyncMock(return_value=initialized)
        initialized.__aexit__ = AsyncMock(return_value=False)

        tools = MagicMock()
        tools.status = 200
        tools.content_type = "application/json"
        tools.json = AsyncMock(return_value={"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
        tools.__aenter__ = AsyncMock(return_value=tools)
        tools.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(side_effect=[initialize, initialized, tools])
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        with patch("kiro_crew.mcp_discovery.aiohttp.ClientSession", return_value=session):
            result = await probe_server(server)

        assert result.status == "ok"
        assert [call.kwargs["json"].get("method") for call in session.post.call_args_list] == [
            "initialize",
            "notifications/initialized",
            "tools/list",
        ]
        assert {call.args[0] for call in session.post.call_args_list} == {server.url}


class TestPreservationProperty:
    @settings(
        max_examples=25,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(servers=_mixed_non_pointer_maps(), registry_mode=st.booleans())
    def test_mixed_non_pointer_map_matches_unfixed_output(
        self,
        servers: dict[str, dict[str, Any]],
        registry_mode: bool,
        tmp_path: Path,
    ) -> None:
        """**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**"""
        root = tmp_path / f"mixed-{next(_CASE_SEQUENCE)}"
        config, _ = _rebuild(
            root,
            kiro_servers=servers,
            registry_mode=registry_mode,
        )
        emitted = config["mcpServers"]

        for name, source in servers.items():
            if name in _MANAGED:
                expected = dict(_MANAGED[name])
                if registry_mode:
                    expected["type"] = _MCP_REGISTRY_TYPE
                assert emitted[name] == expected
            elif source.get("command"):
                expected = copy.deepcopy(source)
                expected["env"] = emit_env(expected["env"])
                assert emitted[name] == expected
            elif source.get("url"):
                assert emitted[name] == _wire_remote(source)
            else:
                assert name not in emitted


class TestExactAndSafetyBaselines:
    def test_pointer_free_agent_spec_is_byte_identical(self, tmp_path: Path) -> None:
        """**Validates: Requirement 3.10**

        This observed byte sequence is both fixture input and expected output.
        Pointer support may add a branch, but a pointer-free existing spec must
        still serialize with identical keys, ordering, indentation, and newline.
        """
        baseline = _defaults_doc()
        _, raw = _rebuild(
            tmp_path / "byte-baseline",
            kiro_servers={},
            managed={},
            existing_spec=baseline,
        )
        assert raw == (json.dumps(baseline, indent=2) + "\n").encode()

    @pytest.mark.asyncio
    async def test_disabled_entry_is_refused_before_transport_dispatch(self) -> None:
        """**Validates: Requirement 3.6**"""
        server = McpServerInfo(
            name="disabled",
            command="/opt/never-spawn",
            disabled=True,
            tools=["last-known"],
            error="stale",
        )
        with (
            patch(
                "kiro_crew.mcp_discovery.create_subprocess_limited",
                new_callable=AsyncMock,
            ) as spawn,
            patch("kiro_crew.mcp_discovery._probe_remote", new_callable=AsyncMock) as remote,
        ):
            result = await probe_server(server)
        spawn.assert_not_awaited()
        remote.assert_not_awaited()
        assert result.status == "disabled"
        assert result.tools == ["last-known"]
        assert result.error == ""

    def test_public_install_translation_priority_is_unchanged(self) -> None:
        """**Validates: Requirement 3.9**"""
        server = {
            "packages": [
                {"registryType": "oci", "identifier": "example.test/server:1"},
                {"registryType": "pypi", "identifier": "example-server"},
                {"registryType": "npm", "identifier": "@example/server"},
            ],
            "remotes": [{"type": "streamable-http", "url": "https://example.test/mcp"}],
        }
        plan, _ = translate_install_plan(server)
        assert plan is not None
        assert plan.method == "npx"
        assert plan.spec["command"] == "npx"

    def test_ungoverned_unfixed_baseline_loses_no_reachable_server(self, tmp_path: Path) -> None:
        """**Validates: Requirement 3.11**

        Exact unfixed projection is recorded in ``_UNGOVERNED_UNFIXED_BASELINE``.
        Assertion is intentionally subset-based: task 5.2 will pin pointer and
        ref additions as accepted while proving no server reachable today is lost.
        """
        config, _ = _rebuild(
            tmp_path / "ungoverned",
            kiro_servers={"catalog-only": {"type": _MCP_REGISTRY_TYPE}},
            registry_mode=None,
            managed={},
        )
        projection = {
            "mcpServers": config["mcpServers"],
            "tools": config["tools"],
            "allowedTools": config["allowedTools"],
        }
        for key in ("mcpServers", "tools", "allowedTools"):
            baseline_value = _UNGOVERNED_UNFIXED_BASELINE[key]
            if isinstance(baseline_value, dict):
                assert baseline_value.items() <= projection[key].items()
            else:
                assert set(baseline_value) <= set(projection[key])
