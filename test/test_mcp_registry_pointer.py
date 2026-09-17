"""A registry pointer must survive to the governed client, not be dropped.

Exploration suite for the ``mcp-registry-server-resolution`` bugfix spec
(upstream ``kirodotdev/KiroCrew#3308``).

An Enterprise MCP Registry install writes an ``mcpServers`` entry carrying
``"type": "registry"`` and NO ``command`` and NO ``url``. That entry is a pointer
into the administrator's catalog, not a transport, and its map key is the entire
resolution key. kiro-cli holds the registry URL from ``GetProfile`` and resolves
such a pointer; Kiro Crew reads it, finds no command, and stops.

**Property 1 (Bug Condition).** For every entry satisfying ``isBugCondition``
(``spec.type == "registry"``, no command, no url, not a managed ``kirocrew-*``
name), the pointer SHALL reach the emitted agent spec under its original map key
with its marker and every local override intact, and no surface SHALL report
``no command``.

Every assertion here encodes the EXPECTED behaviour, so on unfixed code this
file is red — that is the exploration task's success condition, and this same
file is what confirms the fix afterwards. The dangling-tool-reference surface
(clause 1.8) is FOLDED INTO this one property rather than given its own: the
corrected design closes requirement 2.8 through pass-through, because every
reference the defect strands names a server the defect itself dropped.

Scoped rather than arbitrary: the defect is deterministic, so the property is
quantified over the concrete pointer shapes recorded in the design's Examples
section instead of over generated specs. ``atlassian-crew`` appears nowhere — it
was a developer diagnostic entry, not a registry server and not a product of the
defect.

Nothing here touches the operator's machine: every fixture is written under
``tmp_path``, ``KIROCREW_HOME`` and the agent-spec home are pinned by the rootdir
conftest, and the real ``kiro-cli`` is never invoked. The experiment recorded in
the spec's ``design.md`` is the evidence for kiro-cli's own behaviour; this suite
asserts only the artifact Kiro Crew emits.
"""

from __future__ import annotations

import copy
import itertools
import json
import logging
import os
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew import agent as agent_mod
from kiro_crew import cli_doctor
from kiro_crew import mcp_discovery as discovery_mod
from kiro_crew.acp.kas_permissions import _MCP_CAPABILITY
from kiro_crew.agent import _MCP_REGISTRY_TYPE, install_agent
from kiro_crew.env import emit_env
from kiro_crew.mcp_discovery import (
    _STATUS_REGISTRY_POINTER,
    McpServerInfo,
    _server_from_spec,
    probe_server,
)

#: The diagnostic requirements 2.4/2.5 forbid for a pointer. It describes an
#: ``McpServerInfo`` accurately and the ``mcp.json`` entry wrongly.
_NO_COMMAND = "no command"

#: A transport hint a USER wrote, which is theirs and is not the registry marker.
#: ``test_mcp_registry_governance.py::test_refresh_preserves_a_user_transport_hint``
#: pins that it survives, which is why pointer identity has to be read from the
#: marker specifically rather than from ``type`` being present at all.
_USER_TRANSPORT_HINT = "stdio"

#: The four concrete failing pointer shapes recorded in design § Examples, taken
#: from the reporter's ``~/.kiro/settings/mcp.json``. Two transports and two
#: override kinds, which is the whole input domain this property is scoped to:
#:
#: * ``atlassian``       — catalog declares a ``remotes[]`` transport (first-class case)
#: * ``aws-api``         — catalog declares a ``packages[]`` install
#: * ``playwright``      — local ``env`` override the catalog cannot know
#: * ``microsoft-teams`` — local ``oauth`` hints, already in kiro-cli's WIRE spelling
_POINTERS: dict[str, dict[str, Any]] = {
    "atlassian": {"type": _MCP_REGISTRY_TYPE, "disabled": False},
    "aws-api": {"type": _MCP_REGISTRY_TYPE},
    # The reporter's real home path is replaced with a placeholder: the property is
    # that an ``env`` override survives, and the specific value carries no meaning.
    "playwright": {"type": _MCP_REGISTRY_TYPE, "env": {"HOME": "/home/user"}},
    "microsoft-teams": {
        "type": _MCP_REGISTRY_TYPE,
        "oauth": {
            "clientId": "teams-public-client-id",
            "redirectUri": "http://localhost:7878/oauth/callback",
        },
    },
}

#: The override keys a pointer may carry that the catalog cannot supply
#: (requirement 2.7). ``oauth`` is checked sub-key by sub-key because only
#: ``clientId`` is Kiro Crew's to manage — ``redirectUri`` must survive untouched.
_OVERRIDE_KEYS = ("env", "headers", "timeout")

#: The reporter's eleventh ``mcpServers`` entry: an ORDINARY command entry, not a
#: pointer. On their governed host it is the only entry Kiro Crew keeps and the
#: only one kiro-cli discards, so the two sets are exactly disjoint.
_FETCH: dict[str, Any] = {"command": "uvx", "args": ["mcp-server-fetch"]}

#: Managed-server stand-ins, same shape as ``test_agent.py``'s harness uses.
#: ``_MANAGED_MCP_SERVERS`` is patched to these so a rebuild does not depend on
#: the real ``kirocrew`` binary being resolvable on this host.
_MANAGED: dict[str, dict[str, Any]] = {
    "kirocrew-cron": {"command": "/usr/bin/kirocrew", "args": ["mcp-cron"]},
    "kirocrew-core": {"command": "/usr/bin/kirocrew", "args": ["mcp-core"]},
}

#: Model value seeded into an EXISTING spec purely as a base-detection sentinel.
#: ``_refresh_dynamic_fields`` leaves it alone (no managed-model tracking in a
#: per-test data home, no ``agent.model`` in the config), while a fresh build
#: would overwrite it from the bundled defaults — so its survival proves the
#: seeded spec really was the merge base and the assertions below mean something.
_EXISTING_BASE_MODEL = "sentinel-existing-base"

#: The reporter's live dangling reference: ``@atlassian`` in ``tools`` and
#: ``allowedTools``, ``atlassian/*`` in ``permissions.rules``, with ``atlassian``
#: absent from ``mcpServers`` and ``includeMcpJson: false`` making recovery from
#: ``mcp.json`` impossible.
_DANGLING_SERVER = "atlassian"
_DANGLING_REF = f"@{_DANGLING_SERVER}"
_DANGLING_RULE = f"{_DANGLING_SERVER}/*"

#: A builtin tool the bundled defaults grant, used to prove the seeded ``tools``
#: list survived rather than being rebuilt.
_BUILTIN_TOOL = "ReadFile"

#: The two credential-shaped locals a pointer legitimately carries and that
#: requirement 3.8 keeps out of every log line and API payload. Distinctive enough
#: that a substring search cannot match by accident.
_SECRET_CLIENT_ID = "pointer-oauth-client-id-2f7a"
_SECRET_HEADER_VALUE = "pointer-header-secret-9c1d"

#: Unique subdirectory names for the hypothesis examples, which all share one
#: function-scoped ``tmp_path``. Without a fresh root per rebuild the spec one
#: example emitted becomes the merge base for the next.
_CASE_SEQ = itertools.count()


def _pointer_spec(name: str) -> dict[str, Any]:
    """A fresh copy of one recorded pointer shape, safe to hand to a mutator."""
    return copy.deepcopy(_POINTERS[name])


def _pointer_map(names: list[str]) -> dict[str, dict[str, Any]]:
    """``{name: spec}`` for a subset of the recorded shapes."""
    return {name: _pointer_spec(name) for name in names}


def _shipped_defaults_doc() -> dict[str, Any]:
    """A minimal bundled ``defaults.json``, matching ``test_agent.py``'s harness.

    ``hooks`` is mandatory: ``_refresh_dynamic_fields`` raises ``RuntimeError``
    without it (deny-by-default on the security fields) and the caller would then
    silently rebuild from defaults, discarding the seeded base.
    """
    return {
        "model": "auto",
        "tools": [_BUILTIN_TOOL],
        "allowedTools": [_BUILTIN_TOOL],
        "mcpServers": {},
        "toolsSettings": {"execute_bash": {"deniedCommands": ["rm -rf /"]}},
        "hooks": {"preToolUse": "audit"},
    }


def _reporter_spec() -> dict[str, Any]:
    """The reporter's ``~/.kiro/agents/kirocrew.json`` shape, minus the defect.

    Seeds exactly the state clause 1.8 records: the three references to
    ``atlassian`` present, ``atlassian`` itself absent from ``mcpServers``, and
    ``includeMcpJson`` false so kiro-cli cannot recover the server from the
    global ``mcp.json``.
    """
    return {
        "name": "kirocrew",
        "model": _EXISTING_BASE_MODEL,
        "includeMcpJson": False,
        "prompt": "file:///stale/prompt.md",
        "tools": [_BUILTIN_TOOL, _DANGLING_REF],
        "allowedTools": [_BUILTIN_TOOL, _DANGLING_REF],
        "permissions": {
            "rules": [
                {
                    "capability": _MCP_CAPABILITY,
                    "match": [_DANGLING_RULE],
                    "effect": "allow",
                }
            ]
        },
        "toolsSettings": {"execute_bash": {"deniedCommands": ["rm -rf /"]}},
        "hooks": {"preToolUse": "audit"},
        "mcpServers": {"fetch": dict(_FETCH)},
    }


def _rebuild(
    root: Path,
    *,
    kiro_servers: dict[str, Any],
    kirocrew_servers: dict[str, Any] | None = None,
    existing_spec: dict[str, Any] | None = None,
    managed: dict[str, dict[str, Any]] | None = None,
    auto_approve: bool | None = None,
    passes: int = 1,
    emitted: list[bytes] | None = None,
) -> dict[str, Any]:
    """Run ``rebuild_agent_config`` against a fixture home and return the spec.

    Modelled on ``test_agent.py::_run_install_mcp_merge`` so this suite exercises
    the same seams the existing merge-priority tests do, with one addition: an
    EXISTING ``kirocrew.json`` can be seeded, which is what the dangling-reference
    half of the property needs.

    *managed* overrides the managed-server set, defaulting to ``_MANAGED``.
    ``{}`` is what isolates the INBOUND path from the outbound one: the managed
    entries are the only part of the emitted map that ``_mcp_registry_mode``
    legitimately changes, so a comparison across that declaration has to exclude
    them or it measures the outbound marker instead of the pointer branch.

    *auto_approve* pins ``_may_auto_approve`` — the governance ceiling — to a
    fixed answer. ``None`` leaves it unpatched, which is what every caller
    predating this parameter does and what keeps their assertions honest about
    whatever ceiling the host resolves. A caller asserting the PRESENCE of an
    ``allowedTools`` grant must pin it: that predicate is consulted twice on the
    emit path (the shared-ref sync, then the final pass over the assembled list),
    and on a host that grows a ceiling the grant is withheld by BOTH — so an
    unpinned presence assertion would report a governance outcome as a pointer
    regression.

    *passes* runs ``install_agent`` more than once inside ONE fixture home, which
    is the only faithful way to ask whether a rebuild settles: each pass reads the
    spec the previous pass wrote, and the fixture's prompt file, bundled defaults
    and scope files stay at the same paths. Two separate ``_rebuild`` calls under
    two roots cannot answer it — their emitted ``prompt`` values name their own
    roots, so the bytes differ for a reason that has nothing to do with the sync.
    *emitted* collects the exact bytes written by each pass, in order.

    Everything is written under *root* (a directory under the test's ``tmp_path``),
    including the agents dir, so nothing reaches the operator's ``~/.kiro``.
    ``shutil.which`` is stubbed to resolve any command, which is what lets an
    ordinary entry such as ``fetch`` survive on a host that does not have it.
    """
    root.mkdir(parents=True, exist_ok=True)

    cfg_dir = root / "config"
    cfg_dir.mkdir()
    (cfg_dir / "defaults.json").write_text(json.dumps(_shipped_defaults_doc()), encoding="utf-8")
    prompt = cfg_dir / "prompt.md"
    prompt.write_text("system prompt", encoding="utf-8")

    kiro_dir = root / "kiro_agents"
    kiro_dir.mkdir()
    if existing_spec is not None:
        (kiro_dir / agent_mod.AGENT_FILENAME).write_text(
            json.dumps(existing_spec), encoding="utf-8"
        )

    mc_config = root / "config.json"
    mc_config.write_text(json.dumps({"agent": {"kiro_hooks_autoimport": False}}), encoding="utf-8")

    kiro_mcp = root / "kiro_mcp.json"
    kiro_mcp.write_text(json.dumps({"mcpServers": kiro_servers}), encoding="utf-8")
    cc_mcp = root / "cc_mcp.json"
    cc_mcp.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    user_home = root / "kirocrew_home"
    user_home.mkdir()
    if kirocrew_servers is not None:
        (user_home / "mcp.json").write_text(
            json.dumps({"mcpServers": kirocrew_servers}), encoding="utf-8"
        )

    patches = [
        patch.multiple(
            "kiro_crew.agent",
            KIRO_AGENTS_DIR=kiro_dir,
            _BUNDLED_CFG_DIR=cfg_dir,
            _KIROCREW_BIN="/usr/bin/kirocrew",
            _MANAGED_MCP_SERVERS=copy.deepcopy(_MANAGED if managed is None else managed),
            _KIRO_MCP_JSON=kiro_mcp,
            _CC_MCP_JSON=cc_mcp,
        ),
        patch("kiro_crew.agent._user_dir", lambda: user_home),
        patch("kiro_crew.agent._prompt_path", return_value=prompt),
        patch("kiro_crew.agent._shipped_defaults", return_value=cfg_dir / "defaults.json"),
        patch("kiro_crew.agent._project_dir", return_value=None),
        patch("kiro_crew.agent._aim_skill_paths", return_value=[]),
        patch("kiro_crew.agent.shutil.which", side_effect=lambda c, **kw: c),
        patch("kiro_crew.agent._mc_config_path", return_value=mc_config),
        patch("kiro_crew.agent._extra_mcp_scope_globals", return_value=[]),
    ]
    if auto_approve is not None:
        patches.append(patch("kiro_crew.agent._may_auto_approve", return_value=auto_approve))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        for _ in range(passes):
            path = install_agent()
            if emitted is not None:
                emitted.append(path.read_bytes())
    return json.loads(path.read_text(encoding="utf-8"))


def _reads_as_pointer(info: McpServerInfo) -> bool:
    """Whether discovery kept this entry's registry-pointer identity.

    ``getattr`` with a default rather than a bare attribute read on purpose: on
    unfixed code the field does not exist, and an ``AttributeError`` would report
    a broken test instead of the assertion that names the missing behaviour.
    """
    return bool(getattr(info, "is_registry_pointer", False))


def _no_command_reports(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every captured log line that blames the local entry for having no command.

    Scoped to the two Kiro Crew loggers that produce it, because under
    ``-n auto`` an unrelated test's leaked record can land in this window.
    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.name in ("kiro_crew.agent", "kiro_crew.mcp_discovery")
        and _NO_COMMAND in record.getMessage()
    ]


def _assert_pointer_carried(
    config: dict[str, Any],
    declared: dict[str, dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The Property 1 expected-behaviour predicate over an emitted agent spec.

    Asserts, for every pointer in *declared*: the map key is present, the marker
    is retained, no ``command``/``url`` was synthesized, and every local override
    the input carried is on the emitted entry — then that no surface reported
    ``no command`` while producing it.
    """
    emitted = config.get("mcpServers", {})
    for name, spec in declared.items():
        assert name in emitted, (
            f"registry pointer {name!r} was dropped instead of carried into the "
            f"emitted spec; mcpServers keys = {sorted(emitted)}"
        )
        entry = emitted[name]
        assert (
            entry.get("type") == _MCP_REGISTRY_TYPE
        ), f"{name!r} lost its registry marker on emit: {entry!r}"
        # Nothing is invented: kiro-cli resolves the transport from the catalog
        # and overrides a locally declared command anyway, so a synthesized one
        # would be inert at best and misleading in the spec at worst.
        assert "command" not in entry, f"{name!r} had a command synthesized: {entry!r}"
        assert "url" not in entry, f"{name!r} had a url synthesized: {entry!r}"

        for key in _OVERRIDE_KEYS:
            if key in spec:
                assert entry.get(key) == spec[key], (
                    f"{name!r} lost its local {key!r} override, which the catalog "
                    f"cannot supply: {entry!r}"
                )
        # Asserted in WIRE spelling, which is the spelling the reporter's file
        # already uses and the one ``kiro_oauth_wire_entry`` must preserve.
        # ``redirectUri`` is the load-bearing half: only ``clientId`` is ours to
        # manage, so a surgical edit has to leave the sibling untouched.
        if "oauth" in spec:
            wire = entry.get("oauth")
            assert isinstance(wire, dict), f"{name!r} lost its oauth block: {entry!r}"
            for sub_key, value in spec["oauth"].items():
                assert wire.get(sub_key) == value, f"{name!r} lost oauth.{sub_key}: {wire!r}"

    offenders = _no_command_reports(caplog)
    assert not offenders, (
        "a registry pointer was reported as 'no command', which names a malformed "
        f"local entry rather than an unresolved catalog pointer: {offenders}"
    )


class TestPointerIdentitySurvivesDiscovery:
    """``_server_from_spec`` must tell a pointer from a malformed entry.

    This is the first boundary the pointer crosses, and the root cause the design
    hypothesizes: ``mcp_discovery._server_from_spec`` builds ``McpServerInfo``
    from ``command``/``args``/``env``/``url``/``headers``/``scopes``/``client_id``
    and never reads ``type``, so the pointer's defining field is discarded before
    any consumer can act on it.
    """

    @pytest.mark.parametrize("name", sorted(_POINTERS))
    def test_a_marked_entry_reads_as_a_pointer(self, name: str) -> None:
        info = _server_from_spec(name, _pointer_spec(name), "mcp.json")
        assert _reads_as_pointer(info), (
            f"{name!r} crossed discovery indistinguishable from a malformed "
            f"entry: command={info.command!r} url={info.url!r}"
        )

    def test_an_unmarked_command_less_entry_is_not_a_pointer(self) -> None:
        """Requirement 3.3: the marker is the whole scope of the new behaviour."""
        assert not _reads_as_pointer(_server_from_spec("broken", {}, "mcp.json"))

    def test_a_user_transport_hint_is_not_a_pointer(self) -> None:
        """``type`` legitimately carries the user's own hint, which is not ours."""
        spec = {"type": _USER_TRANSPORT_HINT, "command": "/opt/srv"}
        assert not _reads_as_pointer(_server_from_spec("srv", spec, "mcp.json"))

    @pytest.mark.parametrize("junk", [7, True, [], {}, None, ""])
    def test_a_malformed_type_is_not_a_pointer(self, junk: object) -> None:
        """On-disk specs are untrusted, so a non-string ``type`` must not arm it."""
        spec = {"type": junk, "command": "/opt/srv"}
        assert not _reads_as_pointer(_server_from_spec("srv", spec, "mcp.json"))

    def test_the_marker_has_one_spelling_across_both_modules(self) -> None:
        """The inbound reader and the outbound writer must read one constant.

        ``agent`` writes the marker onto its own managed entries and ``discovery``
        reads it off a scope entry. Two literals would let one side be corrected
        and the other left behind, which is silent: a managed entry would stop
        being admitted, or a pointer would stop being recognised, with nothing
        failing locally.
        """
        assert agent_mod._MCP_REGISTRY_TYPE is discovery_mod._MCP_REGISTRY_TYPE


class TestPointerProbeDoesNotBlameTheLocalEntry:
    """``probe_server`` must not answer ``no command`` for a pointer.

    Requirements 1.2 / 2.4: with ``command == ""`` and ``url == ""`` the
    ``not server.command`` branch is the only reachable one, so the probe reports
    a malformed local entry when what it has is an unresolved catalog pointer.

    No spawn happens on either side of the fix, so this drives the real function.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(_POINTERS))
    async def test_probe_does_not_report_no_command(
        self, name: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        info = _server_from_spec(name, _pointer_spec(name), "mcp.json")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_discovery"):
            probed = await probe_server(info)

        assert probed.error != _NO_COMMAND, (
            f"{name!r} probed as {_NO_COMMAND!r}, which describes the "
            f"McpServerInfo accurately and the mcp.json entry wrongly"
        )
        assert probed.status != "error", (
            f"{name!r} probed as an error; a pointer Kiro Crew cannot resolve is "
            f"not a broken server (status={probed.status!r} error={probed.error!r})"
        )
        assert not _no_command_reports(caplog)

    @pytest.mark.asyncio
    async def test_a_disabled_pointer_is_still_refused(self) -> None:
        """Requirement 3.6: the consent gate is not bypassed by the new path."""
        info = _server_from_spec("atlassian", _pointer_spec("atlassian"), "mcp.json")
        info.disabled = True
        probed = await probe_server(info)
        assert probed.status == "disabled"


class TestPointerReachesTheEmittedSpec:
    """Property 1 over the recorded pointer shapes, end to end.

    Requirements 1.1, 1.3, 1.7, 2.1, 2.2, 2.3, 2.4, 2.7: the pointer is carried
    into ``mcpServers`` with its marker and its local overrides, so the governed
    client — which already holds the registry URL from ``GetProfile`` — can
    resolve it. On unfixed code ``rebuild_agent_config`` finds no command in any
    candidate source, logs ``Dropping MCP server 'atlassian': no command``, and
    omits the entry.
    """

    @pytest.mark.parametrize("name", sorted(_POINTERS))
    def test_each_recorded_shape_is_carried(
        self, name: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        declared = _pointer_map([name])
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            config = _rebuild(tmp_path / f"case-{name}", kiro_servers=dict(declared))
        _assert_pointer_carried(config, declared, caplog)

    @settings(
        max_examples=15,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(names=st.lists(st.sampled_from(sorted(_POINTERS)), min_size=1, unique=True))
    def test_every_combination_of_the_recorded_shapes_is_carried(
        self, names: list[str], tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Scoped quantification: the property holds for any set of pointers.

        The domain is the four recorded shapes rather than arbitrary specs,
        because the defect is deterministic — what varies between hosts is WHICH
        pointers are installed, and the reporter had ten of them at once.
        """
        declared = _pointer_map(names)
        root = tmp_path / f"case-{next(_CASE_SEQ)}"
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            config = _rebuild(root, kiro_servers=dict(declared))
        _assert_pointer_carried(config, declared, caplog)

    def test_the_disjoint_sets_close(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """All ten pointers PLUS ``fetch`` reach ``mcpServers``.

        On the reporter's governed host Kiro Crew's emitted map and kiro-cli's
        accepted set are exactly disjoint: the pointers are dropped here and
        resolved there, while ``fetch`` is kept here and ignored there. The fix
        has to keep ``fetch`` working while adding the pointers, so both halves
        are asserted in one rebuild.
        """
        declared = _pointer_map(sorted(_POINTERS))
        servers: dict[str, Any] = dict(declared)
        servers["fetch"] = dict(_FETCH)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            config = _rebuild(tmp_path / "disjoint", kiro_servers=servers)

        _assert_pointer_carried(config, declared, caplog)
        # Requirement 3.1: the ordinary entry still resolves through the
        # candidate loop and keeps its argv paired with its command.
        fetch = config["mcpServers"]["fetch"]
        assert fetch["command"] == _FETCH["command"]
        assert fetch["args"] == _FETCH["args"]

    def test_a_pointer_in_the_kirocrew_scope_is_carried_too(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The bug condition names both scanned scopes, not just the Kiro global.

        ``~/.kiro/crew/mcp.json`` is empty on the reporter's host, so this is the
        half their evidence cannot show — and the clause covers it explicitly.
        """
        declared = _pointer_map(["aws-api"])
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            config = _rebuild(
                tmp_path / "crew-scope",
                kiro_servers={},
                kirocrew_servers=dict(declared),
            )
        _assert_pointer_carried(config, declared, caplog)

    def test_a_managed_server_keeps_its_own_resolved_command(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Requirement 3.5: the outbound marker must not become inbound resolution.

        ``agent.mcp_registry_mode`` stamps ``type: registry`` onto Kiro Crew's own
        managed servers so a governed client does not drop them. Such an entry
        already carries a Kiro-Crew-resolved command, and routing it through the
        catalog would relaunch a different Kiro Crew build.
        """
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            with patch.object(agent_mod, "_mcp_registry_mode", lambda: True):
                config = _rebuild(tmp_path / "managed", kiro_servers={})

        for name in _MANAGED:
            entry = config["mcpServers"][name]
            assert entry.get("type") == _MCP_REGISTRY_TYPE, name
            assert entry.get("command"), f"{name} lost its resolved command: {entry!r}"
            assert entry.get("args") == _MANAGED[name]["args"], name


class TestPointerReferencesAreLegitimateAfterTheRebuild:
    """Clause 1.8 / requirement 2.8, folded into Property 1.

    The reporter's spec ships ``@atlassian`` in ``tools`` and ``allowedTools`` and
    ``atlassian/*`` in ``permissions.rules`` while declaring no ``atlassian``
    server, with ``includeMcpJson: false`` so kiro-cli cannot recover it from
    ``mcp.json`` — and kiro-cli drops such a reference SILENTLY at mount time,
    with no exception and no log line.

    No removal mechanism is asserted, and none is wanted: the stranded name is the
    name the drop removed, so restoring the pointer to ``mcpServers`` puts the
    alias back in ``valid_servers``, the shared sync's ``elif alias in
    valid_servers`` add branch fires, and all three references become legitimate.
    On unfixed code the pointer is absent from ``valid_servers``, that branch
    declines, and the spec keeps naming a server it does not declare.
    """

    def _rebuilt_reporter_spec(self, tmp_path: Path) -> dict[str, Any]:
        servers: dict[str, Any] = {_DANGLING_SERVER: _pointer_spec(_DANGLING_SERVER)}
        servers["fetch"] = dict(_FETCH)
        return _rebuild(
            tmp_path / "reporter",
            kiro_servers=servers,
            existing_spec=_reporter_spec(),
        )

    def test_the_seeded_spec_really_was_the_merge_base(self, tmp_path: Path) -> None:
        """Guard on the fixture, not on the product.

        ``_load_existing_config`` silently falls back to a fresh
        ``build_agent_config`` when the refresh raises, and a fresh build carries
        none of the seeded references — so every assertion below would pass or
        fail for the wrong reason. The sentinel model and the preserved builtin
        tool are what prove the base survived.
        """
        config = self._rebuilt_reporter_spec(tmp_path)
        assert config.get("model") == _EXISTING_BASE_MODEL
        assert _BUILTIN_TOOL in config.get("tools", [])

    def test_the_referenced_server_ends_up_declared(self, tmp_path: Path) -> None:
        config = self._rebuilt_reporter_spec(tmp_path)
        emitted = config.get("mcpServers", {})
        assert _DANGLING_SERVER in emitted, (
            f"the spec still ships {_DANGLING_REF!r} in tools and "
            f"{_DANGLING_RULE!r} in permissions.rules for a server it does not "
            f"declare; mcpServers keys = {sorted(emitted)}"
        )

    def test_the_tool_reference_survives_and_is_backed(self, tmp_path: Path) -> None:
        config = self._rebuilt_reporter_spec(tmp_path)
        assert _DANGLING_REF in config.get("tools", [])
        assert _DANGLING_SERVER in config.get("mcpServers", {})

    def test_the_permission_rule_survives_and_is_backed(self, tmp_path: Path) -> None:
        rules = self._rebuilt_reporter_spec(tmp_path).get("permissions", {}).get("rules", [])
        matched = [r for r in rules if _DANGLING_RULE in (r.get("match") or [])]
        assert matched, f"the seeded permission rule was removed: {rules!r}"

    def test_recovery_does_not_depend_on_the_global_mcp_json(self, tmp_path: Path) -> None:
        """``includeMcpJson`` stays pinned false, so the fix cannot lean on it.

        An ambient ``mcp.json`` entry cannot rescue a reference kiro-cli will not
        consult — the same hazard ``apps/bridges.py`` already reasons about for
        app agents.
        """
        config = self._rebuilt_reporter_spec(tmp_path)
        assert config.get("includeMcpJson") is False
        assert _DANGLING_SERVER in config.get("mcpServers", {})

    def test_no_no_command_report_names_the_pointer(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            self._rebuilt_reporter_spec(tmp_path)
        assert not _no_command_reports(caplog)


class TestPointerOverridesCarryInCombination:
    """The override half of requirement 2.7, past the recorded shapes.

    The four shapes in ``_POINTERS`` are the reporter's own, so between them they
    exercise ``env`` and a wire-form ``oauth`` block and nothing else. These cases
    cover the rest of the override set the requirement names — ``headers`` and
    ``timeout`` — the combination of all of them on ONE pointer, and the
    internal-to-wire OAuth rename, which the reporter's file cannot show because
    a registry install already writes the wire spelling.

    ``disabled`` is asserted here too. It is not an override — the catalog has no
    opinion on it — but it is carried by the same emit path, and it is the consent
    gate: an emitted entry missing it is an entry kiro-cli will resolve.
    """

    #: One pointer carrying every override kind at once. ``env`` deliberately
    #: declares no ``PATH``, so ``emit_env`` passes it through and the assertion
    #: compares against the declared value rather than an expanded search path.
    _ALL_OVERRIDES: dict[str, Any] = {
        "type": _MCP_REGISTRY_TYPE,
        "env": {"HOME": "/home/user", "REGION": "us-east-1"},
        "headers": {"X-Tenant": "acme"},
        "timeout": 45,
        "oauth": {
            "clientId": "combined-client-id",
            "redirectUri": "http://localhost:7878/oauth/callback",
        },
    }

    @pytest.mark.parametrize("override_key", sorted(_ALL_OVERRIDES.keys() - {"type"}))
    def test_each_override_kind_carries_individually(
        self, override_key: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        spec = {"type": _MCP_REGISTRY_TYPE, override_key: self._ALL_OVERRIDES[override_key]}
        declared = {"solo": copy.deepcopy(spec)}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            config = _rebuild(tmp_path / f"solo-{override_key}", kiro_servers=dict(declared))
        _assert_pointer_carried(config, declared, caplog)

    def test_every_override_carries_in_combination(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One entry, every override kind — the case a per-key loop can still fail.

        Carrying each key in isolation does not prove the branch carries them
        together: a reconciliation step that rebuilds the entry from one field
        passes every solo case and drops the siblings here.
        """
        declared = {"combined": copy.deepcopy(self._ALL_OVERRIDES)}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            config = _rebuild(tmp_path / "combined", kiro_servers=dict(declared))
        _assert_pointer_carried(config, declared, caplog)

    def test_nothing_beyond_the_declared_keys_is_emitted(self, tmp_path: Path) -> None:
        """ "Verbatim plus nothing": the emitted key set is the declared one.

        The complement of the carry assertions. Without it the branch could
        satisfy every override case by forwarding the source entry wholesale,
        which would emit whatever else a registry install wrote into a file Kiro
        Crew does not author.
        """
        declared = {"exact": copy.deepcopy(self._ALL_OVERRIDES)}
        config = _rebuild(tmp_path / "exact", kiro_servers=dict(declared))
        assert set(config["mcpServers"]["exact"]) == set(self._ALL_OVERRIDES)

    def test_an_internal_oauth_spelling_is_renamed_to_the_wire_form(self, tmp_path: Path) -> None:
        """The pointer's hints cross the same internal-to-wire boundary as a url's.

        kiro-cli deserializes ``oauthScopes`` and ``oauth.clientId`` and drops
        anything else in the entry silently, so an internal spelling emitted as-is
        authorizes with the provider's default grant instead of the one on disk.
        Routing a pointer through ``kiro_oauth_wire_entry`` is what keeps it from
        being the one emitted shape written in the ignored spelling.
        """
        declared = {
            "internal": {
                "type": _MCP_REGISTRY_TYPE,
                "scopes": ["read", "write"],
                "clientId": "internal-client-id",
            }
        }
        config = _rebuild(tmp_path / "internal", kiro_servers=copy.deepcopy(declared))
        entry = config["mcpServers"]["internal"]
        assert entry["oauthScopes"] == ["read", "write"]
        assert entry["oauth"] == {"clientId": "internal-client-id"}
        # Both internal names are gone: leaving them would keep two spellings of
        # one fact around with only one of them load-bearing.
        assert "scopes" not in entry
        assert "clientId" not in entry

    def test_a_disabled_pointer_keeps_its_consent_gate(self, tmp_path: Path) -> None:
        """Requirement 3.6's premise at the emit boundary, not just in the probe.

        The emitted spec IS the mount decision, and every other branch in this
        loop preserves ``disabled``. A pointer emitted without it is one an
        operator switched off and the governed client resolves anyway.
        """
        declared = {"off": {"type": _MCP_REGISTRY_TYPE, "disabled": True}}
        config = _rebuild(tmp_path / "disabled", kiro_servers=copy.deepcopy(declared))
        entry = config["mcpServers"]["off"]
        assert entry.get("disabled") is True
        assert entry.get("type") == _MCP_REGISTRY_TYPE
        # Its tools ref is withheld for the same reason, by the shared sync's
        # existing disabled branch rather than by anything this fix added.
        assert "@off" not in config.get("tools", [])
        assert "@off" not in config.get("allowedTools", [])


def test_the_suite_writes_nothing_outside_its_own_tmp_path(tmp_path: Path) -> None:
    """The harness's own containment, asserted rather than assumed.

    Every rebuild in this file writes its agents dir under the test's ``tmp_path``,
    so the operator's ``~/.kiro/agents/kirocrew.json`` — the file that decides
    which MCP servers their real agent has — is never touched.
    """
    root = tmp_path / "containment"
    _rebuild(root, kiro_servers=_pointer_map(["atlassian"]))
    emitted = root / "kiro_agents" / agent_mod.AGENT_FILENAME
    assert emitted.is_file()
    assert os.path.commonpath([str(emitted), str(tmp_path)]) == str(tmp_path)


def _secret_pointer_spec() -> dict[str, Any]:
    """A pointer carrying both credential-shaped locals requirement 3.8 covers."""
    return {
        "type": _MCP_REGISTRY_TYPE,
        "headers": {"Authorization": f"Bearer {_SECRET_HEADER_VALUE}"},
        "oauth": {"clientId": _SECRET_CLIENT_ID},
    }


def _discovered_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    servers: dict[str, Any],
) -> dict[str, McpServerInfo]:
    """Run ``list_servers`` over a fixture ``mcp.json`` holding *servers*.

    The discovery half of the dashboard payload: ``GET /api/mcp`` serializes
    exactly these rows. Modelled on ``test_mcp_discovery.py::TestListServers`` —
    ``_MCP_JSON_PATHS`` is narrowed to the one fixture file and the agent-config
    lookup is pointed at *tmp_path*, so the operator's own scopes are never read.
    """
    discovery_mod._probe_cache.clear()
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "defaults.json").write_text(json.dumps({"name": "kirocrew"}), encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    kiro_dir = tmp_path / ".kiro" / "agents"
    kiro_dir.mkdir(parents=True, exist_ok=True)
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.mcp_discovery._MCP_JSON_PATHS", (mcp_json,))
    monkeypatch.setattr("kiro_crew.mcp_discovery.kiro_agents_dir", lambda: kiro_dir)
    monkeypatch.setattr("kiro_crew.mcp_discovery.Path.home", lambda: tmp_path)
    return {s.name: s for s in discovery_mod.list_servers()}


class TestPointerProbeIsHonestlyNotVerifiable:
    """Design D3: the probe reports what Kiro Crew can observe, which is nothing.

    Requirements 1.2 / 1.4 / 1.5 / 2.4 / 2.5. ``ok`` would claim a server answers
    that this process never contacted, ``error``/``no command`` blames a local
    entry that is exactly the shape a registry install writes. The third answer is
    the ``needs_auth`` precedent: not verifiable from here, rendered as a
    not-verified badge with a hover hint.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(_POINTERS))
    async def test_the_probe_answers_the_pointer_status(self, name: str) -> None:
        probed = await probe_server(_server_from_spec(name, _pointer_spec(name), "mcp.json"))
        assert probed.status == _STATUS_REGISTRY_POINTER
        assert probed.error == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(_POINTERS))
    async def test_no_tool_list_is_fabricated(self, name: str) -> None:
        """An empty list beside a not-verified badge is the honest rendering.

        The tools live in the administrator's catalog, which Kiro Crew never
        reads, so any list here would be invented.
        """
        info = _server_from_spec(name, _pointer_spec(name), "mcp.json")
        # Seeded to prove the branch CLEARS rather than merely leaves untouched: a
        # row arriving from ``list_servers`` carries whatever the probe cache held.
        info.tools = ["stale-tool"]
        probed = await probe_server(info)
        assert probed.tools == []

    @pytest.mark.asyncio
    async def test_the_probe_mode_is_neither_handshake_nor_declared(self) -> None:
        """No round trip happened, and there is no declaration to fall back on.

        ``declared`` is the managed-server fallback, whose tool list comes from
        this package's own ``_list_tools()``. A pointer has no such declaration,
        so claiming that mode would tell the dashboard to render the ``Declared``
        badge over a list nothing produced.
        """
        info = _server_from_spec("atlassian", _pointer_spec("atlassian"), "mcp.json")
        info.probe_mode = "declared"
        probed = await probe_server(info)
        assert probed.probe_mode not in ("handshake", "declared")

    @pytest.mark.asyncio
    async def test_the_branch_spawns_nothing_and_connects_to_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both dispatches are unreachable for a pointer, not merely unlikely.

        ``is_remote`` is already false for a url-less row, so the remote stub here
        pins that the branch does not depend on that staying true.
        """

        def _no_spawn(*a: object, **k: object) -> None:
            raise AssertionError("a registry pointer must never be spawned")

        async def _no_remote(*a: object, **k: object) -> None:
            raise AssertionError("a registry pointer must never be connected to")

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _no_spawn)
        monkeypatch.setattr("kiro_crew.mcp_discovery._probe_remote", _no_remote)
        probed = await probe_server(
            _server_from_spec("atlassian", _pointer_spec("atlassian"), "mcp.json")
        )
        assert probed.status == _STATUS_REGISTRY_POINTER

    @pytest.mark.asyncio
    async def test_the_disabled_pointer_answers_disabled_and_not_the_pointer_status(
        self,
    ) -> None:
        """Requirement 3.6: the branch order is the consent gate, not a detail.

        The pointer branch sits after the disabled refusal, so a disabled pointer
        keeps answering ``disabled``. Reversed, the new path would be the way
        around the gate.
        """
        info = _server_from_spec("atlassian", _pointer_spec("atlassian"), "mcp.json")
        info.disabled = True
        probed = await probe_server(info)
        assert probed.status == "disabled"
        assert probed.status != _STATUS_REGISTRY_POINTER

    @pytest.mark.asyncio
    async def test_the_pointer_status_is_not_written_to_the_probe_cache(self) -> None:
        """No probe ran, so there is nothing to record.

        The cache is TTL'd and reads as ``outdated`` once it lapses, which would
        say "this was verifiable half an hour ago" about a status that can never be
        verified. ``list_servers`` derives it structurally instead.
        """
        discovery_mod._probe_cache.clear()
        info = _server_from_spec("aws-api", _pointer_spec("aws-api"), "mcp.json")
        await probe_server(info)
        assert discovery_mod.probe_metadata("aws-api") is None


class TestExactlyTwoPointerStatesAreReachable:
    """Design D6's taxonomy, as a test rather than a comment.

    With D5 withdrawn there is no withholding path, so a pointer has exactly two
    states: *carried to the client, not verifiable here* and *pointer disabled*.
    The catalog-side causes — a name absent from the catalog, an entry declaring
    neither a usable remote nor a translatable package — are not Kiro Crew's to
    report, because Kiro Crew never reads the catalog. ``kiro-cli mcp list``
    reports those.
    """

    @pytest.mark.asyncio
    async def test_the_probe_reaches_exactly_those_two(self) -> None:
        seen = set()
        for name in sorted(_POINTERS):
            for disabled in (False, True):
                info = _server_from_spec(name, _pointer_spec(name), "mcp.json")
                info.disabled = disabled
                seen.add((await probe_server(info)).status)
        assert seen == {_STATUS_REGISTRY_POINTER, "disabled"}

    def test_discovery_reaches_exactly_those_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And discovery agrees with the probe on the same entry.

        ``list_servers`` overlays the probe cache onto every other row, which for a
        pointer would answer ``unknown`` before the first probe and ``outdated``
        after the TTL — two more states, neither of them true. The pointer's status
        is structural, so it is derived rather than read from the cache.
        """
        declared = {
            "on": {"type": _MCP_REGISTRY_TYPE},
            "off": {"type": _MCP_REGISTRY_TYPE, "disabled": True},
        }
        rows = _discovered_pointer(tmp_path, monkeypatch, declared)
        assert {rows["on"].status, rows["off"].status} == {
            _STATUS_REGISTRY_POINTER,
            "disabled",
        }
        assert rows["on"].status == _STATUS_REGISTRY_POINTER
        assert rows["off"].status == "disabled"


class TestPointerDashboardPayload:
    """Requirement 2.5: the row states the cause instead of blaming the entry.

    ``GET /api/mcp`` serializes ``list_servers()`` rows through ``to_dict``, so
    this is the payload ``McpTab`` renders: a not-verified badge (keyed on
    ``status``), a hover hint, and an empty tool list which the existing tools cell
    already renders as an em dash.
    """

    def test_the_payload_carries_the_status_the_dashboard_switches_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _discovered_pointer(tmp_path, monkeypatch, {"atlassian": _pointer_spec("atlassian")})
        payload = rows["atlassian"].to_dict()
        # The literal, not the constant: ``McpTab.tsx``'s status switch and
        # ``connectionStateFor`` compare against this exact string, and a rename on
        # one side alone would silently fall through to the "Unknown" badge.
        assert payload["status"] == "registry_pointer"
        assert payload["tools"] == []
        assert payload["error"] == ""
        # Neither of the two modes the ``Declared`` badge is gated on.
        assert payload["probeMode"] not in ("handshake", "declared")
        # No probe ran, so the row shows no "Last probed" line.
        assert not payload["probedAt"]

    def test_no_surface_reports_no_command_for_a_pointer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Requirement 2.4, over the whole discovery-plus-payload path."""
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.mcp_discovery"):
            rows = _discovered_pointer(
                tmp_path, monkeypatch, {"atlassian": _pointer_spec("atlassian")}
            )
            payload = json.dumps(rows["atlassian"].to_dict())
        assert _NO_COMMAND not in payload
        assert not _no_command_reports(caplog)


class TestPointerRedactionHoldsOnTheNewPath:
    """Requirement 3.8 on the surfaces this task adds.

    A pointer legitimately carries the two credential-shaped locals the catalog
    cannot supply: an OAuth ``clientId`` and header values. Neither may reach a log
    line or the dashboard.

    The payload property holds structurally rather than by scrubbing: ``to_dict``
    gates ``headers``, ``scopes`` and ``clientId`` behind ``if self.url``, and a
    pointer has no url — so those keys are never emitted for a pointer at all, and
    ``redact_mcp_headers`` has nothing to cover. ``error`` is the one field that
    always ships, always through ``redact_mcp_error``, and it is empty here. The
    log property holds because the pointer branch emits no log line: it interpolates
    no spec-derived value anywhere.
    """

    @pytest.mark.asyncio
    async def test_neither_secret_reaches_the_probe_payload(self) -> None:
        info = _server_from_spec("salesforce", _secret_pointer_spec(), "mcp.json")
        # The spec's own shape has to reach the object, or this test proves nothing.
        assert info.client_id == _SECRET_CLIENT_ID
        assert _SECRET_HEADER_VALUE in json.dumps(info.headers)

        payload = json.dumps((await probe_server(info)).to_dict())
        assert _SECRET_CLIENT_ID not in payload
        assert _SECRET_HEADER_VALUE not in payload

    @pytest.mark.asyncio
    async def test_neither_secret_reaches_a_log_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        info = _server_from_spec("salesforce", _secret_pointer_spec(), "mcp.json")
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.mcp_discovery"):
            await probe_server(info)
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert _SECRET_CLIENT_ID not in logged
        assert _SECRET_HEADER_VALUE not in logged

    def test_neither_secret_reaches_the_dashboard_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _discovered_pointer(tmp_path, monkeypatch, {"salesforce": _secret_pointer_spec()})
        row = rows["salesforce"]
        # Both guards keep the assertions below from passing vacuously: discovery
        # DOES read both values off the spec, so their absence from the payload is
        # the boundary holding rather than the fixture never carrying them.
        assert row.client_id == _SECRET_CLIENT_ID, "fixture no longer carries the hint"
        assert _SECRET_HEADER_VALUE in json.dumps(row.headers), "fixture lost its header"
        payload = json.dumps(row.to_dict())
        assert _SECRET_CLIENT_ID not in payload
        assert _SECRET_HEADER_VALUE not in payload
        # Named explicitly, because the absence above would also hold if the keys
        # were present and merely redacted — and a redacted header NAME set is
        # still more than a pointer row needs.
        emitted = row.to_dict()
        assert "headers" not in emitted
        assert "clientId" not in emitted

    def test_neither_secret_reaches_a_discovery_log_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.mcp_discovery"):
            _discovered_pointer(tmp_path, monkeypatch, {"salesforce": _secret_pointer_spec()})
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert _SECRET_CLIENT_ID not in logged
        assert _SECRET_HEADER_VALUE not in logged


#: The managed always-on names, spelled the way ``mcp_cleanup`` exports them. The
#: doctor section computes ``expected``/``marked`` from that tuple, so a spec whose
#: managed entries are all marked reaches the section's ``cannot verify`` branch.
_DOCTOR_MANAGED = tuple(cli_doctor._ALWAYS_ON_MCPS)

#: The first line of the pointer subsection. Its two-space indent is what separates
#: a subsection LABEL from its six-space continuation lines, which is how
#: :func:`_pointer_block` finds the block's end.
_POINTER_LABEL = "  registry pointers:"


def _doctor_cfg(*, registry_mode: bool) -> object:
    """Minimal stand-in for the loaded config: only the one field is read."""

    class _Agent:
        mcp_registry_mode = registry_mode

    class _Cfg:
        agent = _Agent()

    return _Cfg()


def _doctor_spec(tmp_path: Path, *, pointers: dict[str, Any], marked_managed: bool = True) -> Path:
    """An emitted ``kirocrew.json`` carrying managed entries plus *pointers*.

    Written under ``tmp_path``: doctor reads the spec it is handed and nothing
    else, so this stays clear of the operator's real ``~/.kiro/agents``.
    """
    managed: dict[str, Any] = {}
    for name in _DOCTOR_MANAGED:
        entry: dict[str, Any] = {"command": "kirocrew", "args": ["mcp-core"]}
        if marked_managed:
            entry["type"] = _MCP_REGISTRY_TYPE
        managed[name] = entry
    path = tmp_path / "kirocrew.json"
    path.write_text(
        json.dumps({"mcpServers": {**managed, **copy.deepcopy(pointers)}}), encoding="utf-8"
    )
    return path


def _pointer_block(out: str) -> str:
    """The pointer subsection, sliced out of a full governance-section render.

    Starts at the subsection label and runs to the next two-space label, so the
    slice is the whole block and nothing else — which is what makes a byte
    comparison of two renders meaningful rather than a substring check in disguise.
    Returns ``""`` when the subsection did not render at all.
    """
    lines = out.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(_POINTER_LABEL))
    except StopIteration:
        return ""
    end = start + 1
    while end < len(lines) and lines[end].startswith("      "):
        end += 1
    return "\n".join(lines[start:end])


def _pointer_label_index(out: str) -> int:
    """Line offset of the pointer subsection label, or ``-1`` when it is absent."""
    for i, line in enumerate(out.splitlines()):
        if line.startswith(_POINTER_LABEL):
            return i
    return -1


def _run_doctor_governance(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    declared: bool,
    governed_capable: bool = True,
) -> tuple[str, list[str]]:
    """Render the governance section once and return ``(stdout, issues)``."""
    monkeypatch.setattr(cli_doctor, "mcp_governance_may_apply", lambda: governed_capable)
    monkeypatch.setattr(
        cli_doctor.KiroCrewConfig,
        "load",
        staticmethod(lambda: _doctor_cfg(registry_mode=declared)),
    )
    issues: list[str] = []
    cli_doctor._doctor_mcp_governance(path, issues)
    return capsys.readouterr().out, issues


class TestDoctorReportsInboundPointers:
    """Requirements 1.6 / 2.6 and design D7: doctor names what it passed through.

    Kiro Crew carries a pointer to kiro-cli and cannot resolve it, so the finding
    names the servers and points at ``kiro-cli mcp list`` rather than claiming
    either success or failure. The set is read from the emitted spec, which after
    D2 carries every pointer the scan found — so the names doctor prints are the
    names that actually shipped.
    """

    def test_every_pointer_name_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _doctor_spec(tmp_path, pointers=_pointer_map(sorted(_POINTERS)))
        out, issues = _run_doctor_governance(path, monkeypatch, capsys, declared=True)
        block = _pointer_block(out)
        assert block, "the pointer subsection did not render"
        for name in _POINTERS:
            assert name in block, name
        assert f"{len(_POINTERS)} carried through to kiro-cli" in block
        # Informational, not a fault: a governed host with pointers is the NORMAL
        # state after this fix, so failing doctor on it would be permanent noise.
        assert issues == []

    def test_it_names_the_command_that_can_verify(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Requirement 2.6's "what the operator must do" half."""
        path = _doctor_spec(tmp_path, pointers=_pointer_map(["atlassian"]))
        out, _ = _run_doctor_governance(path, monkeypatch, capsys, declared=True)
        block = _pointer_block(out)
        assert "kiro-cli mcp list" in block
        assert "Ignored (not in registry)" in block

    def test_it_does_not_render_as_verified_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The section's existing ``cannot verify`` discipline, applied inbound.

        Kiro Crew never reads the catalog, so a green tick here would repeat exactly
        the overstatement the surrounding section exists to correct — the same reason
        ``test_mcp_registry_governance.py::test_declared_and_marked_reports_the_names
        _to_allow_list`` asserts no tick on the outbound branch.
        """
        path = _doctor_spec(tmp_path, pointers=_pointer_map(["atlassian", "aws-api"]))
        out, issues = _run_doctor_governance(path, monkeypatch, capsys, declared=True)
        block = _pointer_block(out)
        assert "✅" not in block
        assert "cannot verify" in block
        assert issues == []

    def test_the_subsection_is_identical_across_the_declaration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``agent.mcp_registry_mode`` MUST NOT change this finding (design D7).

        That setting is Kiro Crew's OUTBOUND marker switch. The governed client's
        INBOUND access mode comes from ``GetProfile`` and is persisted nowhere, so
        keying the finding on the local setting would report one as though it were
        the other. Asserted as byte equality of the whole rendered subsection, and
        of its position in the section, rather than as two substring checks — a
        substring pair would still pass if one mode grew or dropped a line.
        """
        path = _doctor_spec(tmp_path, pointers=_pointer_map(sorted(_POINTERS)))
        on, _ = _run_doctor_governance(path, monkeypatch, capsys, declared=True)
        off, _ = _run_doctor_governance(path, monkeypatch, capsys, declared=False)

        # Guard against a vacuous pass: the two runs must really have taken the two
        # different governance branches, or byte equality proves nothing.
        assert "registry mode: on" in on
        assert "registry mode: off" in off

        block_on = _pointer_block(on)
        assert block_on
        assert block_on == _pointer_block(off)
        # Position too: the block renders above every branch, so its offset within
        # the section cannot move with the declaration either.
        assert _pointer_label_index(on) == _pointer_label_index(off)

    def test_a_pointer_alone_reports_no_outbound_fault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A pointer brings the section into existence on an ungoverned identity.

        The pointer is inbound and the marker complaints are outbound. With no
        declaration and no marked managed entry there is no outbound fault, so the
        section must report the pointer and stop rather than inventing one.
        """
        path = _doctor_spec(tmp_path, pointers=_pointer_map(["atlassian"]), marked_managed=False)
        out, issues = _run_doctor_governance(
            path, monkeypatch, capsys, declared=False, governed_capable=False
        )
        assert _pointer_block(out)
        assert "❌" not in out
        assert issues == []

    def test_a_disabled_pointer_is_not_reported_as_carried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``disabled`` is the consent gate, so the entry is not carried for mounting.

        Reporting it as passed through to kiro-cli would be untrue of it, and it is
        the operator's own choice rather than a finding.
        """
        spec = _pointer_spec("atlassian")
        spec["disabled"] = True
        path = _doctor_spec(tmp_path, pointers={"atlassian": spec})
        out, _ = _run_doctor_governance(path, monkeypatch, capsys, declared=True)
        assert _pointer_block(out) == ""
        assert "atlassian" not in out

    def test_a_managed_outbound_marker_is_not_an_inbound_pointer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The two directions must not be conflated.

        A ``kirocrew-*`` entry carries this same marker because Kiro Crew stamped it
        outbound, and it keeps its own resolved command. Counting it inbound would
        report Kiro Crew's own servers as unresolvable catalog references — and would
        grow output on the specs
        ``test_mcp_registry_governance.py::TestDoctorGovernanceSection`` pins.
        """
        path = _doctor_spec(tmp_path, pointers={})
        out, _ = _run_doctor_governance(path, monkeypatch, capsys, declared=True)
        assert _pointer_block(out) == ""
        assert "cannot verify the registry itself" in out

    def test_a_pointer_free_personal_install_stays_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The ordinary case still prints nothing at all.

        Widening the silence guard with the pointer set must not make the section
        speak on a host that has none.
        """
        path = _doctor_spec(tmp_path, pointers={}, marked_managed=False)
        out, issues = _run_doctor_governance(
            path, monkeypatch, capsys, declared=False, governed_capable=False
        )
        assert out == ""
        assert issues == []

    def test_neither_pointer_secret_reaches_the_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Requirement 3.8 on this surface: the finding prints names and nothing else."""
        path = _doctor_spec(tmp_path, pointers={"salesforce": _secret_pointer_spec()})
        out, _ = _run_doctor_governance(path, monkeypatch, capsys, declared=True)
        assert "salesforce" in _pointer_block(out)
        assert _SECRET_CLIENT_ID not in out
        assert _SECRET_HEADER_VALUE not in out

    def test_a_malformed_entry_does_not_crash_the_finding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Doctor runs on the specs it exists to diagnose, so a non-dict entry —
        which ``spec.get("type")`` would raise on — must degrade to "not a pointer"."""
        path = tmp_path / "kirocrew.json"
        path.write_text(
            json.dumps(
                {"mcpServers": {"junk": "not-a-dict", "atlassian": _pointer_spec("atlassian")}}
            ),
            encoding="utf-8",
        )
        out, _ = _run_doctor_governance(path, monkeypatch, capsys, declared=True)
        block = _pointer_block(out)
        assert "atlassian" in block
        assert "junk" not in block


#: A server named by the seeded spec's references and declared by NO scanned
#: scope — not the Kiro global, not the Kiro Crew store, not a provider global.
#: Distinct from ``_DANGLING_SERVER``, which the defect itself dropped and which
#: pass-through restores: nothing restores this one, because there is nothing
#: anywhere on the host to restore it from.
_GHOST_SERVER = "uninstalled-server"
_GHOST_REF = f"@{_GHOST_SERVER}"
_GHOST_RULE = f"{_GHOST_SERVER}/*"


def _ghost_seeded_spec() -> dict[str, Any]:
    """The reporter's spec plus references to a server no scope declares.

    Built on :func:`_reporter_spec` so the two dispositions sit in one fixture:
    ``atlassian`` is the reference the defect stranded and D2 makes legitimate
    again, while ``uninstalled-server`` is the reference nothing on the host can
    back. Both are seeded into ``tools``, ``allowedTools`` and
    ``permissions.rules``, which are the three surfaces requirement 2.8 names.
    """
    spec = _reporter_spec()
    spec["tools"] = [*spec["tools"], _GHOST_REF]
    spec["allowedTools"] = [*spec["allowedTools"], _GHOST_REF]
    spec["permissions"]["rules"] = [
        *spec["permissions"]["rules"],
        {"capability": _MCP_CAPABILITY, "match": [_GHOST_RULE], "effect": "allow"},
    ]
    return spec


def _rules(config: dict[str, Any]) -> list[dict[str, Any]]:
    """The emitted ``permissions.rules`` list, or ``[]`` when the block is gone."""
    return config.get("permissions", {}).get("rules", [])


def _servers_block(config: dict[str, Any]) -> bytes:
    """The emitted ``mcpServers`` block, re-serialized the way the file writes it.

    ``_write_json`` emits the whole spec through ``json.dump(..., indent=2)``, so
    round-tripping this sub-object through the same serializer reproduces the
    block's own bytes apart from its leading indentation. Comparing these bytes
    rather than the parsed dicts is what makes key ORDER part of the assertion:
    two maps that differ only in the order the pointer branch inserted them are
    equal as dicts and different as emitted text.
    """
    return json.dumps(config.get("mcpServers", {}), indent=2).encode()


class TestNoReferenceIsRemovedByThisFix:
    """The descope boundary as a test rather than a comment: D4 and D5 are withdrawn.

    An earlier revision of this design carried a removal guard (D4) that retired a
    ``@server`` ref, an ``allowedTools`` grant and a ``permissions.rules`` match
    for a server the emitted ``mcpServers`` does not declare. It was withdrawn:
    its only justification was a reference in the reporter's spec that turned out
    to be a developer's own diagnostic entry, and every reference this defect
    genuinely strands names a server the defect itself dropped — which
    pass-through restores. **No removal mechanism exists in this fix**, and these
    tests fail if one appears.

    The unfixed disposition these assertions pin as correct is the one design
    § "Out of scope" derives from the code: the shared-ref sync
    (``agent.py``, the ``for name, spec in itertools.chain(...)`` loop) is driven
    by the SCANNED SCOPES, and it has exactly two branches — remove when the entry
    is disabled anywhere, add when the alias is in ``valid_servers``. A ref naming
    a server that appears in no scope is never visited by that loop at all, and a
    visited entry that is neither disabled nor in ``valid_servers`` matches neither
    branch. ``permissions.rules`` is never re-derived either, because
    ``_seed_kas_permissions`` returns early whenever ``permissions`` is present.
    So an unbacked reference survives indefinitely, and that is deliberately still
    true after this fix.
    """

    def test_a_reference_to_an_absent_server_is_left_exactly_as_it_was(
        self, tmp_path: Path
    ) -> None:
        """The unfixed baseline, executed rather than inferred.

        With NO marked entry in any scanned scope, the pointer branch's guard
        (``spec.get("type") == _MCP_REGISTRY_TYPE``) is false for every entry, and
        that guard plus a module constant is the entirety of what this fix adds to
        the emit path. So this input runs the same instructions the pre-fix build
        ran, and the three lists below are what the unfixed code leaves — an
        equality against the seeded values, not a membership check standing in for
        one.

        What this does NOT prove: it never loads a separately built pre-fix
        module, so it cannot catch a divergence introduced somewhere the marker
        guard does not gate. It is bounded to the claim it makes — for an input
        outside the bug condition, these three surfaces come out byte-for-byte as
        they went in.
        """
        seeded = _ghost_seeded_spec()
        config = _rebuild(
            tmp_path / "unmarked",
            kiro_servers={},
            existing_spec=copy.deepcopy(seeded),
        )

        assert config["tools"] == seeded["tools"]
        assert config["allowedTools"] == seeded["allowedTools"]
        assert _rules(config) == seeded["permissions"]["rules"]
        # Both referenced names really are absent, so the assertions above are
        # about an UNBACKED reference rather than a backed one.
        emitted = config.get("mcpServers", {})
        assert _GHOST_SERVER not in emitted
        assert _DANGLING_SERVER not in emitted

    def test_nothing_is_retired_while_the_pointer_branch_fires(self, tmp_path: Path) -> None:
        """The case a leaked removal pass would actually show up in.

        A removal pass has to run AFTER the server map is final — that is the one
        thing design § "Out of scope" says it cannot avoid, since the existing sync
        is driven by scanned scopes rather than by the emitted spec. So it would
        run on this input too, see ``uninstalled-server`` absent from the final
        ``mcpServers``, and retire its ref, its grant and its rule. All three are
        asserted present.
        """
        seeded = _ghost_seeded_spec()
        config = _rebuild(
            tmp_path / "marked",
            kiro_servers={_DANGLING_SERVER: _pointer_spec(_DANGLING_SERVER)},
            existing_spec=copy.deepcopy(seeded),
        )

        assert _GHOST_REF in config["tools"]
        assert _GHOST_REF in config["allowedTools"]
        assert _rules(config) == seeded["permissions"]["rules"]
        # Nothing else went either: the emitted lists are a SUPERSET of the seeded
        # ones. Addition is in scope for this fix (a carried pointer joins
        # ``valid_servers`` and the sync's add branch fires); removal is not.
        assert set(seeded["tools"]) <= set(config["tools"])
        assert set(seeded["allowedTools"]) <= set(config["allowedTools"])
        # And the pointer's own reference is now backed, which is the whole of how
        # requirement 2.8 closes here.
        assert _DANGLING_SERVER in config["mcpServers"]

    def test_the_unbacked_reference_survives_a_second_rebuild(self, tmp_path: Path) -> None:
        """Idempotence of the descope: two passes retire nothing either.

        A removal pass added later would most plausibly be reached on the SECOND
        rebuild, where the spec it reads is one Kiro Crew wrote rather than one a
        fixture seeded. Feeding the first emission back in as the merge base is
        what covers that.
        """
        seeded = _ghost_seeded_spec()
        first = _rebuild(
            tmp_path / "pass-one",
            kiro_servers={_DANGLING_SERVER: _pointer_spec(_DANGLING_SERVER)},
            existing_spec=copy.deepcopy(seeded),
        )
        second = _rebuild(
            tmp_path / "pass-two",
            kiro_servers={_DANGLING_SERVER: _pointer_spec(_DANGLING_SERVER)},
            existing_spec=copy.deepcopy(first),
        )

        assert _GHOST_REF in second["tools"]
        assert _GHOST_REF in second["allowedTools"]
        assert _rules(second) == _rules(first)
        assert second["tools"] == first["tools"]
        assert second["allowedTools"] == first["allowedTools"]


class TestUngovernedHostKeepsEveryServerItCanReach:
    """Requirement 3.11 on a host with no registry access mode in effect.

    This is a TEST and not a manual check for a specific reason: the design's
    experiment ran on the reporter's governed host, where non-registry mode could
    not be observed at all. Nothing in the recorded evidence covers this path, so
    the suite is the only place it is pinned.

    Outside registry mode the governed client's filter INVERTS: the marked entries
    are the ones it drops. Carrying a pointer into ``mcpServers`` therefore has two
    consequences on such a host, and only the first is neutral. The entry itself
    costs nothing — a pointer declares no transport, so an ungoverned host could
    not launch it whether Kiro Crew emits it or drops it. The reference is a real
    change: the pointer is now in ``valid_servers``, so the shared sync's
    ``elif alias in valid_servers`` add branch appends ``@alias`` to ``tools``, and
    to ``allowedTools`` where the governance ceiling permits, where today it
    appends nothing. The client then drops the marked entry and that reference is
    stranded.

    That stranded reference is the ACCEPTED outcome, which is why nothing below
    asserts its absence. See :meth:`test_the_reference_is_added_and_that_is_accepted`
    for why, and for the withdrawn decision this class exists to keep withdrawn.
    """

    _POINTER_NAME = "aws-api"
    _POINTER_REF = f"@{_POINTER_NAME}"

    def _pointer_servers(self) -> dict[str, Any]:
        """A pointer plus an ordinary command entry, which is the realistic mix."""
        return {
            self._POINTER_NAME: _pointer_spec(self._POINTER_NAME),
            "fetch": dict(_FETCH),
        }

    def test_the_pointer_is_emitted_with_the_registry_mode_undeclared(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The inbound path never reads ``agent.mcp_registry_mode``.

        That setting is OUTBOUND — it stamps the marker onto Kiro Crew's own
        managed servers so a governed client does not drop them. Gating the
        inbound branch on it would mean a governed fleet had to declare a local
        setting before Kiro Crew would carry a pointer the administrator installed,
        and Kiro Crew has no local source for the client's actual mode: it comes
        from ``GetProfile`` and is persisted nowhere.

        ``_rebuild`` leaves the setting undeclared, so this is the ungoverned run.
        """
        declared = {self._POINTER_NAME: _pointer_spec(self._POINTER_NAME)}
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            config = _rebuild(tmp_path / "ungoverned", kiro_servers=dict(declared))
        _assert_pointer_carried(config, declared, caplog)

    def test_the_reference_is_added_and_that_is_accepted(self, tmp_path: Path) -> None:
        """Pins the stranded reference as correct, not as a bug.

        A future reader will find this surprising, so the reasoning in full. On an
        ungoverned host the client drops the marked entry, so the ``@aws-api``
        asserted here names a server that will not mount. An earlier revision of
        the design closed that with **decision D5, an ungoverned-host reference
        gate**: emit the reference only when ``agent.mcp_registry_mode`` is
        declared, so no host ever ships a reference its own client will strand.
        **D5 was withdrawn**, together with the D4 removal guard it existed to keep
        honest, and its number is not reused. This test is what catches a silent
        reintroduction of it — a gate would make the assertions below fail on an
        ungoverned host, which is exactly the run this class performs.

        Why the stranded reference is acceptable, stated as the cost it actually
        carries: kiro-cli drops a reference it cannot resolve SILENTLY at mount
        time, with no exception and no log line, so the tool is no more available
        than it is today; and an ``allowedTools`` grant for a server that never
        mounts grants nothing, because there is no server to call and therefore no
        call to auto-approve. No privilege is gained and no capability is lost. The
        property requirement 3.11 exists to protect is that the host is not made
        worse off in a way anyone can observe, and it holds — see
        :meth:`test_no_server_is_lost_relative_to_todays_drop`, which measures that
        half rather than asserting it.
        """
        config = _rebuild(tmp_path / "ref-added", kiro_servers=self._pointer_servers())

        assert self._POINTER_NAME in config["mcpServers"]
        assert self._POINTER_REF in config["tools"]
        # ``allowedTools`` tracks the governance ceiling, which is the same
        # predicate every other writer of that list consults. Asserted as an
        # EQUALITY against the predicate rather than as a one-way membership check,
        # so neither outcome can pass the test vacuously: a ceilingless host must
        # carry the grant, and a governed one must withhold it.
        granted = self._POINTER_REF in config["allowedTools"]
        assert granted is agent_mod._may_auto_approve(self._POINTER_REF)

    def test_no_server_is_lost_relative_to_todays_drop(self, tmp_path: Path) -> None:
        """Requirement 3.11's no-worse-off clause, measured against today's outcome.

        Today's drop leaves the pointer out of the emitted spec entirely, so the
        same input with the pointer removed from the scanned scope IS the artifact
        the unfixed code produces for the governed input. Every server that
        artifact retains must still be emitted identically once the pointer is
        carried, and every reference it carried must still be there. The pointer
        adds; it must not displace.
        """
        without_pointer = _rebuild(
            tmp_path / "todays-drop",
            kiro_servers={"fetch": dict(_FETCH)},
        )
        with_pointer = _rebuild(
            tmp_path / "carried",
            kiro_servers=self._pointer_servers(),
        )

        # Guard against a vacuous comparison: the baseline has to retain something.
        assert "fetch" in without_pointer["mcpServers"]
        for name, entry in without_pointer["mcpServers"].items():
            assert with_pointer["mcpServers"].get(name) == entry, name
        for key in ("tools", "allowedTools"):
            assert set(without_pointer.get(key, [])) <= set(with_pointer.get(key, []))
        # The pointer is the ONLY difference in the server map, which is what
        # "adds, does not displace" means when stated as a set.
        assert set(with_pointer["mcpServers"]) - set(without_pointer["mcpServers"]) == {
            self._POINTER_NAME
        }

    def test_the_emitted_servers_are_byte_identical_across_the_declaration(
        self, tmp_path: Path
    ) -> None:
        """Nothing on the inbound path reads ``agent.mcp_registry_mode``.

        Asserted as byte equality of the whole emitted ``mcpServers`` block rather
        than as two per-entry checks: a per-entry pair would still pass if one mode
        emitted an extra entry, dropped one, or inserted them in a different order.

        ``managed={}`` is load-bearing. The managed ``kirocrew-*`` entries are the
        outbound half of this setting — declaring the mode legitimately stamps the
        marker onto them — so leaving them in would make the two blocks differ for
        a reason that has nothing to do with the pointer branch.
        """
        servers = self._pointer_servers()

        undeclared = _rebuild(tmp_path / "mode-undeclared", kiro_servers=servers, managed={})
        with patch.object(agent_mod, "_mcp_registry_mode", lambda: True):
            declared = _rebuild(tmp_path / "mode-declared", kiro_servers=servers, managed={})

        assert _servers_block(declared) == _servers_block(undeclared)
        # The block is not empty, so the equality above is about carried content.
        assert self._POINTER_NAME in declared["mcpServers"]
        # Same for the references: adding them is mode-independent too, which is
        # the withdrawn D5 gate restated as the thing that must NOT be true.
        assert declared["tools"] == undeclared["tools"]
        assert declared["allowedTools"] == undeclared["allowedTools"]

    def test_the_declaration_patch_really_changes_the_outbound_marker(self, tmp_path: Path) -> None:
        """Vacuity guard for the byte-identity test above.

        With ``managed={}`` the two runs are indistinguishable by construction, so
        the equality there would also hold if the ``_mcp_registry_mode`` patch had
        no effect at all. This run keeps the managed servers and asserts the
        declaration DOES move them, which is what proves the patch is wired and
        that the identity above is a statement about the inbound path specifically.
        """
        undeclared = _rebuild(tmp_path / "outbound-off", kiro_servers={})
        with patch.object(agent_mod, "_mcp_registry_mode", lambda: True):
            declared = _rebuild(tmp_path / "outbound-on", kiro_servers={})

        for name in _MANAGED:
            assert declared["mcpServers"][name].get("type") == _MCP_REGISTRY_TYPE, name
            assert "type" not in undeclared["mcpServers"][name], name


# ─────────────────────────────────────────────────────────────────────────────
# Task 5.3: property-based coverage over the whole input domain, plus the
# end-to-end integration runs on the reporter's actual host shape.
#
# ``install_agent`` is what ``_rebuild`` above already calls, and it is not a
# higher-level entry point than ``rebuild_agent_config``: ``agent.py`` ends with
# ``install_agent = rebuild_agent_config``, a backward-compat alias for the same
# function object. So the integration runs below are full ``install_agent`` runs
# in the sense the task asks for, and they inherit that harness's containment —
# every path the function writes is resolved through ``KIRO_AGENTS_DIR`` /
# ``kiro_agents_dir_path()``, which is patched to a directory under the test's
# ``tmp_path`` (and independently pinned by the rootdir conftest's
# ``_AGENT_SPEC_HOOKS`` fixture). ``TestTheIntegrationRunsStayOffTheOperatorsHost``
# asserts that rather than leaving it to the harness's docstring.
# ─────────────────────────────────────────────────────────────────────────────

#: The reporter's full inbound set: all TEN pointers named in ``bugfix.md``,
#: three of them carrying the local-only overrides the catalog cannot supply.
#: ``_POINTERS`` holds the four shapes the exploration property is scoped to and
#: stays the single definition of those four — this map extends it rather than
#: restating them, so a correction to one lands in both.
#:
#: The other six are bare ``{"type": "registry"}`` entries, which is the minimal
#: shape and the one the reporter's own file mostly holds. They are not filler:
#: the defect dropped all ten, so an integration run that covers four proves
#: nothing about the map size the disjoint-sets table actually describes.
_REPORTER_POINTERS: dict[str, dict[str, Any]] = {
    **{name: copy.deepcopy(spec) for name, spec in _POINTERS.items()},
    "salesforce-prod-sobject-reads": {
        "type": _MCP_REGISTRY_TYPE,
        "oauth": {"clientId": "salesforce-public-client-id"},
    },
    "datadog": {"type": _MCP_REGISTRY_TYPE},
    "gitlab": {"type": _MCP_REGISTRY_TYPE},
    "github": {"type": _MCP_REGISTRY_TYPE},
    "aio-tests": {"type": _MCP_REGISTRY_TYPE},
    "snowflake": {"type": _MCP_REGISTRY_TYPE},
}

#: The eleventh entry, which is not a pointer. Named so the "eleven reach
#: ``mcpServers``" assertions read as the table in design § "The inversion is live".
_REPORTER_COMMAND_ENTRY = "fetch"


def _reporter_pointer_map() -> dict[str, dict[str, Any]]:
    """A fresh copy of all ten reporter pointer shapes."""
    return copy.deepcopy(_REPORTER_POINTERS)


def _reporter_scope() -> dict[str, Any]:
    """The reporter's eleven ``mcpServers`` entries: ten pointers plus ``fetch``."""
    servers: dict[str, Any] = _reporter_pointer_map()
    servers[_REPORTER_COMMAND_ENTRY] = dict(_FETCH)
    return servers


def _server_refs(config: dict[str, Any], key: str) -> set[str]:
    """The ``@server`` refs in one of the emitted tool lists.

    Builtin grants (``ReadFile``, ``execute_bash``, …) are not ``@``-prefixed, so
    filtering on the sigil is what separates a reference to an MCP server from a
    reference to a tool the agent template ships.
    """
    return {ref for ref in config.get(key, []) if isinstance(ref, str) and ref.startswith("@")}


#: Alphabet for generated server-name suffixes and argv members. Deliberately
#: excludes ``/``: a slashed key is rewritten to its alias by
#: ``_normalize_mcp_server_keys``, so a generated one would make a property fail
#: over key normalization rather than over the pointer branch.
_GEN_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

_gen_suffix = st.text(alphabet=_GEN_ALPHABET, min_size=1, max_size=6)

#: Per-class name prefixes. Each generated input class lives in its own
#: namespace so a draw can never collide with another class, with a managed
#: ``kirocrew-*`` name, or with an app-contributed ``app:server`` key — a
#: collision would fail a property for a reason the property is not about.
_CMD_PREFIX = "cmd-"
_URL_PREFIX = "rem-"
_BARE_PREFIX = "bare-"
_PTR_PREFIX = "ptr-"


@st.composite
def _mixed_scope_maps(draw: st.DrawFn) -> tuple[dict[str, Any], frozenset[str]]:
    """Arbitrary ``mcp.json`` maps mixing all five input classes.

    Returns ``(servers, pointer_names)``. The classes are exactly the ones
    design § Preservation Requirements enumerates, so between them the draw spans
    the whole regression surface:

    * an ordinary ``command`` entry (3.1),
    * a ``url`` entry (3.2),
    * an unmarked command-less entry, which is still dropped (3.3),
    * a managed ``kirocrew-*`` name whose source values the rebuild ignores
      (3.4 / 3.5) — including one drawn WITH the marker, which is the case D2's
      managed-name exclusion exists for,
    * a marked pointer, enabled or disabled (the branch under test).

    The ``url`` entries are drawn already in kiro-cli's WIRE spelling
    (``oauth.clientId``, no internal ``scopes``/``clientId``). With no dashboard
    store present that entry is unmanaged, so ``kiro_oauth_wire_entry`` preserves
    the wire values verbatim and the expected output is the input — which keeps
    this property's expectation independent of the translator it is not testing.
    The internal-to-wire rename itself is pinned exactly by
    ``test_mcp_registry_pointer_preservation.py::TestRecordedRemoteBaselines``.
    """
    suffixes = draw(st.lists(_gen_suffix, min_size=1, max_size=3, unique=True))
    servers: dict[str, Any] = {}
    pointers: set[str] = set()

    for suffix in suffixes:
        servers[f"{_CMD_PREFIX}{suffix}"] = {
            "command": f"/opt/{suffix}/server",
            "args": draw(
                st.lists(
                    st.text(alphabet=_GEN_ALPHABET + "-", min_size=1, max_size=8),
                    max_size=3,
                )
            ),
            "env": {"CASE": suffix},
        }
        servers[f"{_URL_PREFIX}{suffix}"] = {
            "url": f"https://{suffix}.example.test/mcp",
            "headers": {"X-Case": suffix},
            "oauth": {"redirectUri": f"http://localhost/{suffix}/callback"},
        }
        servers[f"{_BARE_PREFIX}{suffix}"] = {"args": ["--orphan", suffix], "env": {"CASE": suffix}}

        pointer: dict[str, Any] = {"type": _MCP_REGISTRY_TYPE}
        if draw(st.booleans()):
            pointer["env"] = {"CASE": suffix}
        if draw(st.booleans()):
            pointer["disabled"] = draw(st.booleans())
        name = f"{_PTR_PREFIX}{suffix}"
        servers[name] = pointer
        pointers.add(name)

    for managed_name in _MANAGED:
        servers[managed_name] = draw(
            st.sampled_from(
                [
                    {},
                    {"command": "/tmp/wrong", "args": ["wrong"]},
                    {"url": "https://wrong.example.test/mcp"},
                    {"type": _MCP_REGISTRY_TYPE},
                ]
            )
        )
    return servers, frozenset(pointers)


class TestNonPointerEntriesSurviveTheWholeInputDomain:
    """Property 2 (Preservation) quantified over arbitrary mixed maps WITH pointers.

    ``test_mcp_registry_pointer_preservation.py``'s mixed-map property covers the
    same five classes with no pointer present, which is the pre-fix baseline. This
    one adds pointers to the draw and asserts the non-pointer half of the emitted
    map is untouched by their presence — requirements 3.1, 3.2, 3.3, 3.4, 3.5.

    **How the unfixed baseline is established, and what it does not prove.**
    Unfixed code cannot be executed from this tree, so "identical to the unfixed
    code's output" is established DIFFERENTIALLY: the same generated map with the
    pointer keys removed from the scanned scope is, for the emitted
    ``mcpServers``, byte-for-byte what the unfixed build produces for the full
    map. That equivalence is not an assumption about the diff — it is the drop's
    own semantics. Unfixed code reaches the ``not had_any_command`` branch for
    every marked pointer, logs ``Dropping MCP server %r: no command``, and adds
    nothing to ``valid_servers``; a key absent from the source is likewise absent
    from ``valid_servers``, and removing it leaves the relative insertion order of
    every other key unchanged. So the two inputs converge on one artifact, and
    :meth:`test_the_stripped_run_really_is_a_different_input` keeps that from
    being a comparison of a run against itself.

    What it does NOT prove: it never loads a separately built pre-fix module, so
    it cannot detect a divergence introduced somewhere the pointer keys do not
    reach — a change to ``emit_env``, to the candidate loop, or to the shared-ref
    sync would move BOTH runs together and stay invisible here. That class is
    covered from the other side by the preservation suite's recorded expectations
    and by its byte-identical pointer-free spec, and the second half of this
    property (the per-class recomputed expectation below) is the local guard
    against it: a differential alone would pass if both runs were wrong in the
    same way.

    The comparison is deliberately restricted to ``mcpServers``. ``tools`` is out
    of scope for it because a carried pointer legitimately ADDS its ``@ref`` — the
    accepted outcome task 5.2 pins — so the two runs differ there by design.

    Two further boundaries, so a later reader does not mistake them for gaps. The
    pointer's OWN position in the emitted map is excluded along with its entry, and
    nothing constrains it: no requirement speaks to where in the key order a
    carried entry lands, and requirement 3.10's byte-identity clause is about a
    host with no pointers at all. The RELATIVE order of the non-pointer entries is
    constrained, and is what the byte comparison pins — a mutation that moves one
    of them past another fails here, while one that moves only the pointer does
    not, which is the intended split.
    """

    @settings(
        max_examples=30,
        # ``too_slow`` matches the profile registered in ``test/conftest.py``,
        # which suppresses it globally; an explicit ``@settings`` replaces the
        # profile's list wholesale, so re-stating it is what keeps a two-rebuild
        # example from tripping a health check the suite has already opted out of.
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(drawn=_mixed_scope_maps())
    def test_the_non_pointer_entries_are_what_todays_drop_emits(
        self, drawn: tuple[dict[str, Any], frozenset[str]], tmp_path: Path
    ) -> None:
        """**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**"""
        servers, pointers = drawn
        case = next(_CASE_SEQ)

        with_pointers = _rebuild(
            tmp_path / f"mixed-{case}-with", kiro_servers=copy.deepcopy(servers)
        )
        stripped = {name: spec for name, spec in servers.items() if name not in pointers}
        todays_drop = _rebuild(
            tmp_path / f"mixed-{case}-drop", kiro_servers=copy.deepcopy(stripped)
        )

        # Half one — differential. Byte equality of the non-pointer sub-map, so
        # key order is part of the assertion: two maps differing only in the
        # position the pointer branch inserted an entry are equal as dicts.
        carried = {
            name: entry
            for name, entry in with_pointers["mcpServers"].items()
            if name not in pointers
        }
        assert json.dumps(carried, indent=2) == json.dumps(todays_drop["mcpServers"], indent=2)
        # The pointers are the ONLY difference, in both directions: nothing was
        # gained beyond them and nothing the drop retained was displaced.
        assert set(with_pointers["mcpServers"]) - set(todays_drop["mcpServers"]) == set(pointers)
        assert not set(todays_drop["mcpServers"]) - set(with_pointers["mcpServers"])

        # Half two — the per-class expectation, recomputed rather than compared to
        # a sibling run, so a change that moves both runs together still fails.
        emitted = with_pointers["mcpServers"]
        for name, source in servers.items():
            if name in _MANAGED:
                # 3.4 / 3.5: the managed definition wins over whatever a source
                # file says under that name, INCLUDING a source that carries the
                # marker — which is the case D2's managed-name exclusion is scoped
                # by, and the one that would relaunch a different Kiro Crew build
                # if a managed name ever took the pointer branch.
                #
                # Asserted on command/args/marker rather than as whole-entry
                # equality: a managed entry also carries the environment
                # ``_managed_mcp_env`` contributes, which this harness leaves
                # unpatched and which has nothing to do with the pointer branch.
                # The entry's exact bytes are still pinned — by the differential
                # half above, where a managed name is a non-pointer key like any
                # other.
                assert emitted[name]["command"] == _MANAGED[name]["command"], name
                assert emitted[name]["args"] == _MANAGED[name]["args"], name
                assert "type" not in emitted[name], name
                assert "url" not in emitted[name], name
            elif name in pointers:
                continue
            elif source.get("command"):
                expected = copy.deepcopy(source)
                expected["env"] = emit_env(expected["env"])
                assert emitted[name] == expected, name
            elif source.get("url"):
                assert emitted[name] == source, name
            else:
                # 3.3: an unmarked command-less entry is still dropped. The marker
                # is the whole scope of the new behaviour.
                assert name not in emitted, name

    def test_the_stripped_run_really_is_a_different_input(self, tmp_path: Path) -> None:
        """Vacuity guard for the differential above.

        If a draw ever produced no pointer, or if the strip failed to remove one,
        the property would compare a run against itself and pass unconditionally.
        This pins both halves on a concrete map: the stripped run emits no pointer,
        the full run emits it, and the two artifacts therefore differ.
        """
        servers = {"fetch": dict(_FETCH), "ptr-x": {"type": _MCP_REGISTRY_TYPE}}
        stripped = {"fetch": dict(_FETCH)}

        full = _rebuild(tmp_path / "vacuity-full", kiro_servers=copy.deepcopy(servers))
        drop = _rebuild(tmp_path / "vacuity-drop", kiro_servers=copy.deepcopy(stripped))

        assert "ptr-x" in full["mcpServers"]
        assert "ptr-x" not in drop["mcpServers"]
        assert full["mcpServers"] != drop["mcpServers"]


#: Keys a registry install, or a hand edit, can legitimately leave on a pointer
#: and which the emit branch must NOT forward. They are what makes "verbatim plus
#: nothing" a testable claim rather than a restatement of the carry assertions: a
#: branch that emitted the source entry wholesale satisfies every carry assertion
#: and fails only on these.
#:
#: ``autoApprove`` leads because it is the one with a privilege attached. It is a
#: SECOND route to the exemption ``allowedTools`` grants, and a more direct one —
#: kiro-cli approves an autoApproved MCP tool locally and emits no permission
#: request, so ``hooks.on_tool_call`` (the deny floor, the sensitive-path check,
#: the governance ceiling) never runs for it. ``mcp.json`` is a file Kiro Crew
#: does not author, so forwarding that key would let whoever writes a registry
#: pointer hand themselves a permanent gate exemption.
#:
#: The rest are catalog-side metadata (``version``, ``identifier``,
#: ``registryUrl``) that the reporter's entries do not carry and that says nothing
#: to kiro-cli, which resolves on the map key alone.
_FOREIGN_POINTER_KEYS: dict[str, Any] = {
    "autoApprove": ["*"],
    "version": "1.0.0",
    "identifier": "example/atlassian",
    "registryUrl": "https://registry.example.test/v0",
}


@st.composite
def _pointer_override_specs(draw: st.DrawFn) -> tuple[dict[str, Any], frozenset[str]]:
    """An arbitrary pointer entry, plus the set of keys it must not pass through.

    Returns ``(spec, foreign_keys)``. Requirement 2.7 names ``env``,
    ``oauth.clientId``, ``oauth.redirectUri``, ``headers`` and ``timeout``;
    ``disabled`` is drawn with them because it rides the same emit path and is the
    consent gate rather than an override. The draw is over the POWER SET, not a
    fixed shape: carrying each key in isolation does not prove the branch carries
    them together, and carrying all of them at once does not prove it carries a
    lone middle one.

    An arbitrary subset of :data:`_FOREIGN_POINTER_KEYS` is drawn onto the SAME
    entry, so every example asks both questions at once — what must survive and
    what must not.

    ``env`` never declares a ``PATH``. A declared ``PATH`` is expanded by
    ``emit_env`` into the full effective search path, so an expectation of "the
    emitted value equals the declared value" would be wrong for it by design —
    that expansion is asserted directly by
    ``test_mcp_registry_pointer_preservation.py::test_declared_path_is_expanded_without_losing_other_env``.
    """
    spec: dict[str, Any] = {"type": _MCP_REGISTRY_TYPE}
    if draw(st.booleans()):
        spec["env"] = draw(
            st.dictionaries(
                st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ_", min_size=1, max_size=8).filter(
                    lambda key: key != "PATH"
                ),
                st.text(alphabet=_GEN_ALPHABET + "-/.", min_size=1, max_size=12),
                min_size=1,
                max_size=3,
            )
        )
    if draw(st.booleans()):
        spec["headers"] = draw(
            st.dictionaries(
                st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ-", min_size=1, max_size=10),
                st.text(alphabet=_GEN_ALPHABET + "- ", min_size=1, max_size=12),
                min_size=1,
                max_size=2,
            )
        )
    if draw(st.booleans()):
        spec["timeout"] = draw(st.integers(min_value=1, max_value=600))
    if draw(st.booleans()):
        spec["disabled"] = draw(st.booleans())

    oauth: dict[str, str] = {}
    if draw(st.booleans()):
        oauth["clientId"] = draw(
            st.text(alphabet=_GEN_ALPHABET + "-", min_size=1, max_size=16).filter(str.strip)
        )
    if draw(st.booleans()):
        oauth["redirectUri"] = draw(
            st.sampled_from(
                [
                    "http://localhost:7878/oauth/callback",
                    "http://127.0.0.1:9000/cb",
                    "https://app.example.test/oauth",
                ]
            )
        )
    if oauth:
        spec["oauth"] = oauth

    foreign = draw(
        st.lists(st.sampled_from(sorted(_FOREIGN_POINTER_KEYS)), unique=True),
    )
    for key in foreign:
        spec[key] = copy.deepcopy(_FOREIGN_POINTER_KEYS[key])
    return spec, frozenset(foreign)


class TestArbitraryOverrideCombinationsCarryAndAddNothing:
    """Requirement 2.7 over the power set of the override keys, not a fixed shape.

    Two halves, and the second is the one a naive implementation fails. Every
    declared override must appear on the emitted entry — and NOTHING else may,
    because "verbatim plus nothing" is what keeps the branch from forwarding a
    registry install's whole entry out of a file Kiro Crew does not author. A
    branch that emitted the source dict wholesale would satisfy every carry
    assertion ever written and fail this one.

    **``disabled: false`` is the one declared key that legitimately does not
    survive**, and this class pins that as uniform rather than hardcoding it. The
    shared-ref sync ends an enabled server's visit with
    ``valid_servers[alias].pop("disabled", None)``, so an explicitly-false flag is
    normalized away for EVERY entry class — a command entry and a ``url`` entry
    lose it identically, and absent means enabled in the file kiro-cli reads. The
    reporter's own ``atlassian`` entry is ``{"type": "registry", "disabled":
    false}``, so this is the disposition their live pointer gets. ``disabled:
    true`` is a different matter entirely: it is the consent gate and is asserted
    to survive. Rather than assert the pop directly, each example emits a CONTROL
    command entry carrying the same declaration and asserts the pointer's
    disposition matches it, which states the property as "the pointer branch is
    not special here" instead of encoding today's normalization as a requirement.
    """

    #: Control entry name, and an ordinary command entry so the ``disabled``
    #: comparison is against a path this fix does not touch.
    _CONTROL = "control-command"
    _POINTER = "overridden"

    @settings(
        max_examples=40,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(drawn=_pointer_override_specs())
    def test_every_declared_override_appears_and_nothing_else_is_added(
        self,
        drawn: tuple[dict[str, Any], frozenset[str]],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """**Validates: Requirements 2.1, 2.7**"""
        spec, foreign = drawn
        declared = {self._POINTER: copy.deepcopy(spec)}
        control: dict[str, Any] = {"command": "/opt/control/server", "args": ["serve"]}
        if "disabled" in spec:
            control["disabled"] = spec["disabled"]

        root = tmp_path / f"overrides-{next(_CASE_SEQ)}"
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            config = _rebuild(
                root,
                kiro_servers={**copy.deepcopy(declared), self._CONTROL: control},
            )

        # The carry half, plus the marker, the absent command/url and the
        # no-``no command`` guarantee — the shared Property 1 predicate.
        _assert_pointer_carried(config, declared, caplog)

        emitted = config["mcpServers"][self._POINTER]

        # "Verbatim plus NOTHING", in the direction that actually constrains the
        # branch: a key the entry carried but that is not a local override must
        # not reach the spec. ``autoApprove`` is the one with a privilege behind
        # it, so it is named in the message rather than left to a set difference.
        for key in foreign:
            assert key not in emitted, (
                f"{key!r} was forwarded from a file Kiro Crew does not author; "
                f"emitted entry = {emitted!r}"
            )

        # Nothing was added beyond the marker and the local overrides. Stated as a
        # subset of the declared keys MINUS the foreign ones, so a wholesale
        # forward fails here too. The drawn override keys are already in kiro-cli's
        # wire spelling, so ``kiro_oauth_wire_entry`` preserves them under their
        # own names and no rename shifts the set; an internal spelling arriving
        # here IS renamed, which is why that case lives in
        # ``test_an_internal_oauth_spelling_is_renamed_to_the_wire_form``.
        assert set(emitted) <= set(spec) - foreign
        # And the only override key that may legitimately go is ``disabled``.
        assert (set(spec) - foreign) - set(emitted) <= {"disabled"}

        # The uniformity claim: whatever the rebuild does with ``disabled`` here,
        # it does to an ordinary command entry that declared the same thing.
        assert emitted.get("disabled") == config["mcpServers"][self._CONTROL].get("disabled")
        # And the consent gate specifically, spelled out rather than left to the
        # comparison above: a switched-off pointer is emitted switched off.
        if spec.get("disabled") is True:
            assert emitted["disabled"] is True


@st.composite
def _seeded_reference_specs(draw: st.DrawFn) -> tuple[dict[str, Any], dict[str, Any]]:
    """An arbitrary pointer map plus an existing spec seeded with arbitrary refs.

    Returns ``(pointer_servers, existing_spec)``. The refs are drawn from three
    populations on purpose, because the invariant has to hold in the presence of
    all of them: refs naming a pointer in the map (the case the defect stranded),
    refs naming a server nothing on the host declares (the case nothing restores,
    and which this fix deliberately leaves alone), and no ref at all.
    """
    names = draw(st.lists(_gen_suffix, min_size=1, max_size=4, unique=True))
    pointer_servers = {f"{_PTR_PREFIX}{name}": {"type": _MCP_REGISTRY_TYPE} for name in names}

    referenced = draw(st.lists(st.sampled_from(sorted(pointer_servers)), unique=True))
    ghosts = draw(st.lists(st.sampled_from([_GHOST_SERVER, "never-installed"]), unique=True))
    refs = [f"@{name}" for name in referenced + ghosts]

    # ``allowedTools`` is drawn as a SUBSET of ``tools``, matching the shape a real
    # spec has: a grant is an addition to a mounted ref, never a ref that is
    # auto-approved without being mounted. Guarded on ``refs`` being non-empty
    # because ``sampled_from`` rejects an empty population.
    granted = draw(st.lists(st.sampled_from(refs), unique=True)) if refs else []

    spec = _reporter_spec()
    spec["tools"] = [*spec["tools"], *refs]
    spec["allowedTools"] = [*spec["allowedTools"], *granted]
    spec["permissions"]["rules"] = [
        *spec["permissions"]["rules"],
        *[
            {"capability": _MCP_CAPABILITY, "match": [f"{name}/*"], "effect": "allow"}
            for name in referenced + ghosts
        ],
    ]
    return pointer_servers, spec


class TestEveryPointerInTheInputIsDeclared:
    """The invariant requirement 2.8 actually rests on here, over arbitrary inputs.

    2.8 is satisfied by its FIRST disjunct alone: the pointer is carried into
    ``mcpServers``. So the property to quantify is that every pointer the scan
    found is declared in the emitted map — which makes every reference naming a
    POINTER backed, and closes the clause for every state this defect produces.

    **The stronger unconditional form is deliberately not asserted here.** "No
    ``@server`` ref and no ``server/*`` rule names any absent server" would
    require a removal pass, and none exists: decisions D4 (the dangling-reference
    removal guard) and D5 (the ungoverned-host ref gate) are WITHDRAWN, their
    numbers are not reused, and the latent defect that would need them is recorded
    in design § "Out of scope" for a separate issue. Asserting the stronger form
    would re-encode that withdrawn work as a requirement of this fix — the exact
    leak ``TestNoReferenceIsRemovedByThisFix`` exists to catch from the other
    side. The generator above therefore seeds unbacked refs on purpose and this
    class says nothing about them.
    """

    @settings(
        max_examples=30,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(drawn=_seeded_reference_specs())
    def test_no_reference_naming_a_pointer_is_left_dangling(
        self, drawn: tuple[dict[str, Any], dict[str, Any]], tmp_path: Path
    ) -> None:
        """**Validates: Requirements 2.2, 2.8**"""
        pointer_servers, seeded = drawn
        config = _rebuild(
            tmp_path / f"refs-{next(_CASE_SEQ)}",
            kiro_servers=copy.deepcopy(pointer_servers),
            existing_spec=copy.deepcopy(seeded),
        )

        emitted = config.get("mcpServers", {})
        for name in pointer_servers:
            assert name in emitted, (
                f"pointer {name!r} was not declared, so any reference naming it "
                f"dangles; mcpServers keys = {sorted(emitted)}"
            )
            assert emitted[name].get("type") == _MCP_REGISTRY_TYPE, name

        # Restated as the set inclusion the clause is about, so the failure
        # message names the whole missing group rather than the first member.
        assert set(pointer_servers) <= set(emitted)

        # Every seeded rule that names a pointer is now backed too — the third
        # surface 2.8 names, alongside ``tools`` and ``allowedTools``.
        for rule in _rules(config):
            for match in rule.get("match") or []:
                server = match.split("/", 1)[0]
                if server in pointer_servers:
                    assert server in emitted, server


class TestTheReporterHostEndToEnd:
    """The disjoint-sets defect closed on the reporter's whole inbound set.

    Design § "The inversion is live, not hypothetical" records that Kiro Crew's
    emitted ``mcpServers`` and the governed client's accepted set are EXACTLY
    disjoint on that host: the ten pointers resolve under kiro-cli and are dropped
    here, while ``fetch`` — the one entry with a command — is kept here and
    ``⚠ Ignored (not in registry)`` there. So the fix is not "misses a subset"; it
    costs the reporter every MCP server they have.

    Closing it means all eleven entries reach one emitted map at once. The
    existing ``test_the_disjoint_sets_close`` asserts that over the FOUR shapes
    the exploration property is scoped to; this runs the full ten plus ``fetch``,
    which is the map size the table actually describes.
    """

    def test_all_eleven_entries_reach_the_emitted_map(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """**Validates: Requirements 2.1, 2.2, 2.3, 3.1**"""
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            config = _rebuild(tmp_path / "reporter-full", kiro_servers=_reporter_scope())

        emitted = config["mcpServers"]
        expected = set(_REPORTER_POINTERS) | {_REPORTER_COMMAND_ENTRY}
        assert expected <= set(emitted), sorted(expected - set(emitted))
        assert len(_REPORTER_POINTERS) == 10, "bugfix.md records ten pointers, not eleven"

    def test_the_ten_pointers_keep_their_markers_and_overrides(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """**Validates: Requirements 2.1, 2.4, 2.7**

        The three override-carrying shapes are the load-bearing ones here:
        ``playwright``'s ``env``, ``salesforce-prod-sobject-reads``'s
        ``oauth.clientId``, and ``microsoft-teams``'s ``clientId`` plus
        ``redirectUri``. The catalog cannot supply any of them, so a resolution
        that dropped them would leave the server reachable and misconfigured.
        """
        declared = _reporter_pointer_map()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            config = _rebuild(tmp_path / "reporter-overrides", kiro_servers=_reporter_scope())
        _assert_pointer_carried(config, declared, caplog)

    def test_fetch_keeps_its_resolved_command(self, tmp_path: Path) -> None:
        """**Validates: Requirement 3.1**

        The half a fix could break while satisfying every pointer assertion. The
        pointer branch sits ahead of the candidate loop, so an over-broad guard
        would divert ``fetch`` into it and emit the one entry that HAS a transport
        without one.
        """
        config = _rebuild(tmp_path / "reporter-fetch", kiro_servers=_reporter_scope())
        fetch = config["mcpServers"][_REPORTER_COMMAND_ENTRY]
        assert fetch["command"] == _FETCH["command"]
        assert fetch["args"] == _FETCH["args"]
        assert "type" not in fetch, "an ordinary command entry must not gain the marker"

    def test_every_pointer_is_mounted_by_a_reference(self, tmp_path: Path) -> None:
        """A declared server with no ``@ref`` contributes no tools to the session.

        ``tools`` is a CLOSED allowlist with no wildcard, so requirement 2.2 —
        the server's tools are available in the session — needs the ref as well as
        the entry. Carrying the pointer into ``valid_servers`` is what makes the
        shared sync's ``elif alias in valid_servers`` add branch fire for it.
        """
        config = _rebuild(tmp_path / "reporter-refs", kiro_servers=_reporter_scope())
        refs = _server_refs(config, "tools")
        for name in _REPORTER_POINTERS:
            assert f"@{name}" in refs, name

    def test_no_entry_is_reported_as_no_command(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """**Validates: Requirement 2.4** over the whole eleven-entry rebuild."""
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _rebuild(tmp_path / "reporter-quiet", kiro_servers=_reporter_scope())
        assert not _no_command_reports(caplog)


class TestTheReporterHostIsIdenticalAcrossTheRegistryDeclaration:
    """The same eleven-entry run with ``agent.mcp_registry_mode`` undeclared.

    Nothing on the INBOUND path reads that setting: it is Kiro Crew's outbound
    marker switch for its own managed servers, and the governed client's inbound
    access mode comes from ``GetProfile`` and is persisted nowhere, so there is no
    local source to key on. Task 5.2 pins this for a two-entry map; this pins it
    for the reporter's whole set, which is where an entry gained, dropped or
    reordered under one mode would actually show up.

    ``managed={}`` throughout. The managed ``kirocrew-*`` entries are the outbound
    half of this setting — declaring the mode legitimately stamps the marker onto
    them — so leaving them in would make the two blocks differ for a reason that
    has nothing to do with the pointer branch.
    :meth:`test_the_declaration_patch_really_changes_the_outbound_marker` in
    ``TestUngovernedHostKeepsEveryServerItCanReach`` is the vacuity guard proving
    that patch is wired.
    """

    def _both_modes(self, tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        undeclared = _rebuild(
            tmp_path / "eleven-undeclared", kiro_servers=_reporter_scope(), managed={}
        )
        with patch.object(agent_mod, "_mcp_registry_mode", lambda: True):
            declared = _rebuild(
                tmp_path / "eleven-declared", kiro_servers=_reporter_scope(), managed={}
            )
        return undeclared, declared

    def test_the_eleven_entries_are_byte_identical_across_the_declaration(
        self, tmp_path: Path
    ) -> None:
        """**Validates: Requirements 2.1, 3.11**

        Byte equality of the whole ``mcpServers`` block rather than a per-entry
        loop: a per-entry comparison would still pass if one mode emitted an extra
        entry, dropped one, or inserted them in a different order.
        """
        undeclared, declared = self._both_modes(tmp_path)
        assert _servers_block(declared) == _servers_block(undeclared)
        # Not vacuous: the block carries all eleven under both modes.
        expected = set(_REPORTER_POINTERS) | {_REPORTER_COMMAND_ENTRY}
        assert expected <= set(undeclared["mcpServers"])

    def test_the_references_are_added_identically_across_the_declaration(
        self, tmp_path: Path
    ) -> None:
        """The withdrawn D5 gate, restated as the thing that must NOT be true.

        D5 would have emitted a pointer's ``@ref`` only under a declared registry
        mode, so that no host ships a reference its own client will strand. It was
        withdrawn with D4 and its number is not reused. A silent reintroduction
        would make these two lists differ, which is what this asserts against.
        """
        undeclared, declared = self._both_modes(tmp_path)
        assert declared["tools"] == undeclared["tools"]
        assert declared["allowedTools"] == undeclared["allowedTools"]
        for name in _REPORTER_POINTERS:
            assert f"@{name}" in undeclared["tools"], name

    def test_the_spec_is_internally_consistent_under_both_modes(self, tmp_path: Path) -> None:
        """Every ``@ref`` the emitted spec carries names a server it declares.

        Scoped to THIS fixture, which seeds no existing spec and therefore no
        unbacked reference: the rebuild starts from the bundled defaults, so every
        ``@ref`` in the result was put there by a writer that also declared the
        entry. It is NOT the unconditional 2.8 form — that would require the
        removal pass D4 was withdrawn with, and
        ``TestNoReferenceIsRemovedByThisFix`` pins its absence. Stated as internal
        consistency of a clean rebuild, which is what the integration bullet asks
        for and all this fixture can honestly support.
        """
        for config in self._both_modes(tmp_path):
            declared_servers = set(config["mcpServers"])
            for key in ("tools", "allowedTools"):
                for ref in _server_refs(config, key):
                    assert ref[1:] in declared_servers, f"{key}: {ref}"


class TestTheReporterSpecShapeRecoversWithNothingRemoved:
    """The reporter's live ``kirocrew.json``, end to end, including ``allowedTools``.

    Their spec sets ``includeMcpJson: false`` and carries ``@atlassian`` in BOTH
    ``tools`` and ``allowedTools`` plus ``atlassian/*`` in ``permissions.rules``,
    while declaring no ``atlassian`` server — and kiro-cli drops such a reference
    silently at mount time, with no exception and no log line, which
    ``includeMcpJson: false`` guarantees no ambient ``mcp.json`` entry can rescue.

    ``TestPointerReferencesAreLegitimateAfterTheRebuild`` above covers ``tools``,
    ``permissions.rules`` and ``mcpServers``. ``allowedTools`` was the one seeded
    surface with no assertion on it, and it is the one that matters most: it is
    kiro-cli's blanket auto-approve list, the single path that never reaches the
    PreToolUse gate. This class closes that gap and adds the "nothing removed"
    half the integration bullet names.
    """

    def _rebuilt(self, tmp_path: Path, name: str, **kwargs: Any) -> dict[str, Any]:
        servers: dict[str, Any] = {_DANGLING_SERVER: _pointer_spec(_DANGLING_SERVER)}
        servers[_REPORTER_COMMAND_ENTRY] = dict(_FETCH)
        return _rebuild(
            tmp_path / name,
            kiro_servers=servers,
            existing_spec=_reporter_spec(),
            **kwargs,
        )

    def test_the_auto_approve_grant_survives_and_is_backed(self, tmp_path: Path) -> None:
        """**Validates: Requirements 2.2, 2.8** — the gap in the existing coverage.

        ``auto_approve=True`` pins the governance ceiling open, and that choice is
        deliberate rather than convenient. The reporter's own live spec CARRIES
        this grant, which is itself the evidence their host has no ceiling
        withholding it — so an unceilinged run is the state their report describes.
        Left unpinned, ``_may_auto_approve`` is consulted twice on the emit path
        (the shared-ref sync, then the final pass over the assembled list) and a
        host that later grows a ceiling would fail this assertion for a governance
        reason, reporting a correctly-applied ceiling as a pointer regression.

        The ceiling's own behaviour is not thereby left unasserted — see
        :meth:`test_the_grant_tracks_the_governance_ceiling`.
        """
        config = self._rebuilt(tmp_path, "grant-open", auto_approve=True)
        assert _DANGLING_SERVER in config["mcpServers"]
        assert _DANGLING_REF in config["allowedTools"]
        # And in ``tools`` as well: mounting and auto-approving are two lists, and
        # a grant for a server that is not mounted would be the wrong recovery.
        assert _DANGLING_REF in config["tools"]

    def test_the_grant_tracks_the_governance_ceiling(self, tmp_path: Path) -> None:
        """The complement: a ceiling withholds the grant and still mounts the server.

        Asserted in both directions in one test, because the interesting property
        is the RELATIONSHIP between the two lists rather than either membership on
        its own. A ceilinged host mounts the recovered pointer and routes its calls
        through the PreToolUse gate; ``tools`` is unaffected either way, since
        mounting a tool is not auto-approving it. The withholding is the
        pre-existing ceiling machinery, not the withdrawn D4 removal guard: it is
        keyed on the predicate, not on the server's absence from ``mcpServers``.
        """
        open_ceiling = self._rebuilt(tmp_path, "ceiling-open", auto_approve=True)
        closed_ceiling = self._rebuilt(tmp_path, "ceiling-closed", auto_approve=False)

        assert _DANGLING_REF in open_ceiling["allowedTools"]
        assert _DANGLING_REF not in closed_ceiling["allowedTools"]
        # The server is mounted under both, so a ceiling costs the grant and never
        # the recovery this fix performs.
        for config in (open_ceiling, closed_ceiling):
            assert _DANGLING_SERVER in config["mcpServers"]
            assert _DANGLING_REF in config["tools"]

    def test_all_three_seeded_references_end_up_backed(self, tmp_path: Path) -> None:
        """**Validates: Requirement 2.8** across the three surfaces it names."""
        config = self._rebuilt(tmp_path, "three-surfaces", auto_approve=True)
        assert _DANGLING_SERVER in config["mcpServers"]
        assert _DANGLING_REF in config["tools"]
        assert _DANGLING_REF in config["allowedTools"]
        matched = [rule for rule in _rules(config) if _DANGLING_RULE in (rule.get("match") or [])]
        assert matched, f"the seeded permission rule was removed: {_rules(config)!r}"

    def test_nothing_is_removed_from_any_of_the_three(self, tmp_path: Path) -> None:
        """The negative half: this fix ADDS, and removes nothing.

        Every seeded reference is still present and the rule list is unchanged.
        Stated as a superset for the two tool lists, because addition IS in scope
        — a carried pointer joins ``valid_servers`` and the sync's add branch fires
        — while removal is the withdrawn D4 work.
        """
        seeded = _reporter_spec()
        config = self._rebuilt(tmp_path, "nothing-removed", auto_approve=True)

        assert set(seeded["tools"]) <= set(config["tools"])
        assert set(seeded["allowedTools"]) <= set(config["allowedTools"])
        assert _rules(config) == seeded["permissions"]["rules"]
        # The builtin the defaults grant is still there too, which is what proves
        # the seeded lists were merged rather than rebuilt from the template.
        assert _BUILTIN_TOOL in config["tools"]

    def test_recovery_does_not_lean_on_the_global_mcp_json(self, tmp_path: Path) -> None:
        """``includeMcpJson`` stays pinned false, so pass-through cannot rely on it."""
        config = self._rebuilt(tmp_path, "no-ambient", auto_approve=True)
        assert config["includeMcpJson"] is False
        assert _DANGLING_SERVER in config["mcpServers"]


class TestConsecutiveRebuildsSettle:
    """Idempotence: the sync settles once a pointer joins ``valid_servers``.

    The concern is specific rather than general. Before this fix a pointer was
    absent from ``valid_servers``, so the shared-ref sync's add branch declined
    for it on every rebuild. After it, the branch fires — and a rebuild reads the
    spec the previous rebuild wrote, so an add that is not stable against its own
    output oscillates: a ref appended on every pass, an entry reinserted at a new
    position, a rule list that grows. Two consecutive rebuilds producing identical
    BYTES is what rules all three out at once, ordering included.
    """

    def _chain(self, tmp_path: Path, name: str) -> tuple[list[bytes], dict[str, Any]]:
        """Two rebuilds in ONE fixture home, so the second reads the first's output.

        ``passes=2`` rather than two ``_rebuild`` calls under two roots. The
        emitted spec records the absolute path of the fixture's prompt file, so
        two roots differ in their ``prompt`` value and a byte comparison across
        them fails on the harness rather than on the sync. One home is also the
        more faithful model: a real second rebuild reads back the file the first
        one wrote, in place.
        """
        emitted: list[bytes] = []
        config = _rebuild(
            tmp_path / name,
            kiro_servers=_reporter_scope(),
            existing_spec=_reporter_spec(),
            auto_approve=True,
            passes=2,
            emitted=emitted,
        )
        return emitted, config

    def test_two_consecutive_rebuilds_produce_an_identical_spec(self, tmp_path: Path) -> None:
        """**Validates: Requirements 2.8, 3.10**"""
        emitted, _ = self._chain(tmp_path, "settle-bytes")
        assert len(emitted) == 2, "the chain did not run twice"
        assert emitted[1] == emitted[0]

    def test_the_second_rebuild_still_declares_every_pointer(self, tmp_path: Path) -> None:
        """Idempotence is only worth having if the settled state is the right one.

        Byte equality alone would also hold for two rebuilds that both dropped
        every pointer, so the settled artifact is asserted to be the one that
        carries them — with their markers, and with the refs the sync added.
        """
        _, second = self._chain(tmp_path, "settle-content")
        refs = _server_refs(second, "tools")
        for name in _REPORTER_POINTERS:
            assert name in second["mcpServers"], name
            assert second["mcpServers"][name].get("type") == _MCP_REGISTRY_TYPE, name
            assert f"@{name}" in refs, name

    def test_no_reference_list_grows_between_the_passes(self, tmp_path: Path) -> None:
        """The oscillation stated directly, in case a future change breaks bytes only.

        A duplicate append is the most likely regression here and the one a byte
        comparison reports least legibly, so it is also asserted as list identity
        plus the absence of duplicates.
        """
        emitted, second = self._chain(tmp_path, "settle-lists")
        first = json.loads(emitted[0])
        for key in ("tools", "allowedTools"):
            assert second[key] == first[key], key
            assert len(second[key]) == len(set(second[key])), f"{key} carries a duplicate"
        assert _rules(second) == _rules(first)


class TestTheIntegrationRunsStayOffTheOperatorsHost:
    """The acceptance clause, asserted rather than delegated to the harness docstring.

    ``install_agent`` writes more than ``kirocrew.json``: it also installs the
    ``kirocrew-lite``, ``-knowledge``, ``-research``, ``-heartbeat`` and
    ``-conductor`` specs, and repairs every spec in the agents directory. All of
    those resolve their target through ``kiro_agents_dir_path()``, which honours
    the ``KIRO_AGENTS_DIR`` override hook this suite patches — so the whole set
    lands under ``tmp_path``. That is the claim these tests pin, on the largest
    run in the file rather than on a one-entry rebuild.
    """

    def test_every_spec_the_eleven_entry_run_writes_lands_under_tmp_path(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "containment-eleven"
        _rebuild(root, kiro_servers=_reporter_scope(), existing_spec=_reporter_spec())

        written = sorted((root / "kiro_agents").rglob("*"))
        assert written, "the run wrote no spec at all, so containment proves nothing"
        for path in written:
            assert os.path.commonpath([str(path), str(tmp_path)]) == str(tmp_path), str(path)
        # The primary spec specifically, since it is the file that decides which
        # MCP servers the operator's real agent has.
        assert (root / "kiro_agents" / agent_mod.AGENT_FILENAME).is_file()

    def test_the_live_agent_spec_home_is_never_the_write_target(self, tmp_path: Path) -> None:
        """The override hook is set for the duration of the run, not merely nearby.

        Read INSIDE the rebuild through a spy on the spec writer's own resolver, so
        this observes the value production actually used rather than re-deriving it
        afterwards. A patch that leaked or was applied to the wrong module would
        show up here as the live home.
        """
        live_home = Path.home() / ".kiro" / "agents"
        observed: list[Path] = []
        real_resolver = agent_mod.kiro_agents_dir_path

        def _spy() -> Path:
            resolved = real_resolver()
            observed.append(resolved)
            return resolved

        root = tmp_path / "containment-resolver"
        with patch.object(agent_mod, "kiro_agents_dir_path", _spy):
            _rebuild(root, kiro_servers=_reporter_scope())

        assert observed, "the run never resolved the agents dir, so nothing was proven"
        for resolved in observed:
            assert resolved != live_home
            assert os.path.commonpath([str(resolved), str(tmp_path)]) == str(tmp_path)

    def test_no_real_kiro_cli_is_invoked(self, tmp_path: Path) -> None:
        """No child process at all, so the ``cwd=`` clause has nothing to constrain.

        The acceptance criteria require that no real ``kiro-cli`` runs and that any
        child process gets a ``cwd=`` under ``tmp_path``. The second is vacuous here
        because the first is absolute: a rebuild assembles and writes JSON, and
        spawning is what the PROBE does, on a path no rebuild reaches. Both
        spawners are replaced with raisers rather than recorders — a spawn is not
        merely unwanted here, it is unreachable, and a recorder would let one
        through while logging it.

        Scoped honestly: patching the ``subprocess`` module attributes catches a
        call made through them, which is how this codebase spawns, but would not
        catch a module that bound ``run`` at import time via ``from subprocess
        import run``. The stronger guarantee for this path is structural — the
        rebuild is synchronous and has no spawn site — and the containment tests
        above are what pin where its writes land.
        """

        def _no_spawn(*args: object, **kwargs: object) -> None:
            raise AssertionError(f"a rebuild spawned a process: {args!r} {kwargs!r}")

        with (
            patch("subprocess.run", _no_spawn),
            patch("subprocess.Popen", _no_spawn),
        ):
            config = _rebuild(tmp_path / "no-spawn", kiro_servers=_reporter_scope())
        assert set(_REPORTER_POINTERS) <= set(config["mcpServers"])
