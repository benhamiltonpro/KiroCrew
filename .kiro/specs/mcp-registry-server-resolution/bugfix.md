# Bugfix Requirements Document

## Introduction

Kiro Crew does not resolve MCP server entries that an Enterprise MCP Registry
install writes into `mcp.json`. Upstream issue: kirodotdev/KiroCrew#3308.

A registry install (performed from Kiro IDE or Kiro CLI against a registry URL set
in Kiro console → Settings → Shared settings) writes an entry carrying
`"type": "registry"` and **no** `command` and **no** `url`. That entry is a pointer
into the administrator's catalog, not a transport. Kiro IDE 1.0.293 and Kiro CLI
2.18.0 resolve the pointer. Kiro Crew 0.2.0 does not: it reads the entry, finds no
`command`, and stops.

### The local entry shape is minimal

Confirmed on the reporter's macOS machine. The entry in `~/.kiro/settings/mcp.json`
is exactly:

```json
"atlassian": { "type": "registry", "disabled": false }
```

No `version`, no `identifier`, no `registryUrl`, no `command`, no `url`. **The
`mcpServers` map key is the entire resolution key.** Ten such pointers exist on
that host: `playwright`, `datadog`, `gitlab`, `github`, `aio-tests`,
`salesforce-prod-sobject-reads`, `snowflake`, `atlassian`, `microsoft-teams`,
`aws-api`. An eleventh `mcpServers` entry, `fetch`, is present but is an ordinary
command entry rather than a pointer, so it is not one of the ten.

Some pointers carry local-only overrides that the catalog cannot know and that any
resolution MUST carry onto the resolved entry:

- `playwright`: `"env": {"HOME": "/Users/bhamilton"}`
- `salesforce-prod-sobject-reads`: `"oauth": {"clientId": "..."}`
- `microsoft-teams`: `"oauth": {"clientId": "...", "redirectUri": "http://localhost:7878/oauth/callback"}`

`~/.kiro/crew/mcp.json` is empty (`{"mcpServers": {}}`) on that host, so the Kiro
Crew scope is not involved in the report.

### A catalog entry may declare a remote, not a command

The reporter's actual catalog entry for `atlassian`:

```json
{"server": {"name": "atlassian", "title": "Atlassian", "description": "...",
  "version": "1.0.0",
  "remotes": [{"type": "streamable-http", "url": "https://mcp.atlassian.com/v1/mcp"}]}}
```

There is no `packages[]` at all. **The remote case is the first-class case**, and a
resolution that only derives a stdio command is wrong for the reporting host. A
pointer resolves to *either* a `url` (from `remotes[]`) *or* a `command` (from
`packages[]`), whichever the catalog entry declares. The `pypi`/`aws-iac-mcp` example
that shaped the original framing came from an AWS support engineer rather than the
reporter; it stays as the secondary case.

This also constrains reuse of existing code.
`mcp_providers/official.py::translate_install_plan` prioritizes
`npm` > `pypi` > `oci` > `remotes`. That ordering is correct for the **install** flow
it serves, where the caller is choosing how to install a server it has just browsed.
It is wrong for **pointer resolution**: a governed catalog entry *declares* the
transport, so there is nothing to prioritize, and a preference for packages over
remotes would pick a package the administrator did not publish or fail an entry that
has none. Reusing `translate_install_plan` unchanged for resolution is therefore not
correct. Its other behavior remains reusable and should not be restated: camelCase
and snake_case field tolerance, the wrapped `{"server": {...}}` item shape, and the
deliberate refusal to translate publisher-supplied `runtimeArguments` for OCI.

### The registry URL is not available locally (settled)

Confirmed on the reporter's machine, not inferred:

- `grep -ril` for `registryUrl`, `registry_url`, `mcpRegistry` and `mcp-registry`
  across `~/.kiro/settings`, `~/.kiro/agents` and `~/.kiro/crew` returned nothing.
- `~/.kiro/settings/cli.json` contains only `{"mcp.loadedBefore": true}`.
- kiro-cli's state store at
  `~/Library/Application Support/kiro-cli/data.sqlite3` holds 22 keys, of which the
  only MCP-, registry- or profile-related ones are `api.codewhisperer.profile` and
  `profile.Migrated`.

So **no persisted local source for the registry URL exists**, which matches
`docs/guides/enterprise-mcp-governance.md`: the client reads the MCP toggle and the
registry URL from `GetProfile` at startup and persists neither. Obtaining the URL
from the governance API would re-introduce the enterprise-auth surface `AGENTS.md`
forbids. Any Kiro-Crew-side resolution therefore requires an **operator-declared**
registry URL. This is recorded as settled evidence; it is not an open question.

### The defect has four surfaces

All four are reached from the same missing step.

- `mcp_discovery._server_from_spec` never reads `type`, so the `McpServerInfo` it
  builds carries `command=""` and `url=""`.
- `mcp_discovery.probe_server` takes its `not server.command` branch, sets
  `status="error"` and `error="no command"`, and logs
  `MCP probe failed [atlassian]: no command configured`. The dashboard renders
  STATUS: Error, TOOLS: "no command".
- `agent.rebuild_agent_config` drops the entry with
  `Dropping MCP server 'atlassian': no command`, so it never reaches
  `~/.kiro/agents/kirocrew.json`. All ten pointers were dropped on the reporter's
  host; the only surviving non-managed entry is `fetch`, the one that had a `command`.
- **The emitted spec ships a dangling tool reference.** The reporter's
  `~/.kiro/agents/kirocrew.json` sets `"includeMcpJson": false` and carries only
  `kirocrew-cron`, `kirocrew-core` and `fetch` in `mcpServers` — yet its `tools` list
  still holds `"@atlassian"` and `permissions.rules` still allows `atlassian/*`. The
  spec references the tool namespace of the very server this defect dropped, and
  `includeMcpJson: false` guarantees kiro-cli cannot recover it from `mcp.json`.
  `src/kiro_crew/apps/bridges.py` (around line 631) already reasons about exactly
  this hazard for app agents — "the spec opts out of the global mcp.json, so kiro-cli
  will not consult it at mount time — an ambient entry cannot rescue these refs" —
  and notes that kiro-cli drops such a reference **silently** at mount time, with no
  exception and no log line. Restoring `atlassian` to `mcpServers` restores the
  reference, so this surface closes with the other three rather than needing a
  mechanism of its own.

  Evidence note: the same spec, as originally captured, also held `"@atlassian-crew"`
  and `atlassian-crew/*`. The developer has since confirmed `atlassian-crew` was a
  test entry they added themselves while diagnosing this issue. It is not a registry
  server, no scope declares it, and it is **not evidence of this defect**. It is
  excluded from the evidence above, and the analysis it originally motivated is
  recorded as out of scope in `design.md`.

The impact is silent and total for a governed fleet: every MCP server an
administrator publishes through the registry is unavailable in Kiro Crew, the one
diagnostic the user sees ("no command") describes a malformed local entry rather than
an unresolved catalog pointer, and the spec that ships as a result is internally
inconsistent.

Scope note: this is distinct from the existing `agent.mcp_registry_mode` setting.
That setting is **outbound** — it stamps `"type": "registry"` onto Kiro Crew's own
managed servers so the governed client does not drop them. This defect is
**inbound** — handling a registry pointer that someone else wrote. The two must not
be conflated: a managed entry that Kiro Crew marked itself already carries a
Kiro-Crew-resolved `command`, and resolving it through the catalog would relaunch a
different Kiro Crew build (see
`docs/guides/enterprise-mcp-governance.md` § "the registry launches the server, not
your install").

`docs/architecture/mcp.md` and `docs/guides/enterprise-mcp-governance.md` document
the current discovery, probing, spec-composition and governance behavior. Both are
covered by the read-before-you-touch table in `AGENTS.md` and MUST be updated in the
same commit as any change to the behavior they document.

### Leading candidate: pass-through

Recorded here as a requirement-level constraint on the solution space, not as an
implementation.

kiro-cli is the governed client. It already holds the registry URL from `GetProfile`,
and it already filters `mcpServers` on `"type": "registry"` — which is precisely why
`agent.mcp_registry_mode` exists to stamp that marker outbound. So the cheapest
correct fix may be **pass-through**: stop dropping the pointer, emit it into
`~/.kiro/agents/kirocrew.json` with `"type": "registry"` and its local overrides
preserved, and let kiro-cli resolve it. Pass-through needs no registry URL in Kiro
Crew, no network fetch, and no forbidden auth surface. It would also satisfy 2.1–2.3
without Kiro Crew performing any catalog resolution, which is why those clauses are
written as outcomes.

One precondition is unproven and design MUST settle it before choosing:

**Open question (the only one remaining): does kiro-cli resolve a `"type": "registry"`
entry inlined in an agent spec's `mcpServers` map, as opposed to one in `mcp.json`?**
This is empirically testable on the reporter's machine: add the `atlassian` pointer to
`kirocrew.json` and launch kiro-cli. If it resolves, pass-through is the fix. If it
does not, the fallback is Kiro-Crew-side resolution against an **operator-declared**
registry URL, per the settled evidence above.

Two further decisions carry into design, neither of them blocking:

1. **Is the catalog a static JSON document or the v0.1 REST API?**
   `enterprise-mcp-governance.md` describes "a registry JSON file", while
   `mcp_providers/official.py` speaks the `registry.modelcontextprotocol.io` v0.1
   REST API. These are different fetch contracts. Relevant only on the fallback path.
2. **Should Kiro Crew replicate the client's version-override relaunch?** The client
   relaunches at the catalog's version when it differs from the local install. That
   behavior is what makes the documented known limitation dangerous for Kiro Crew's
   own servers, so replicating it is a decision, not a default.

## Bug Analysis

### Current Behavior (Defect)

Bug condition: an entry in a scanned `mcp.json` source (`~/.kiro/settings/mcp.json`
Kiro-global scope, or `~/.kiro/crew/mcp.json` Kiro Crew scope) whose `type` is
`"registry"`, and which therefore carries no `command` and no `url`.

1.1 WHEN a scanned `mcp.json` holds an entry with `"type": "registry"` THEN
`mcp_discovery._server_from_spec` discards the `type` field and produces an
`McpServerInfo` with an empty `command` and an empty `url`, so no consumer can tell
the entry apart from a malformed one.

1.2 WHEN such an entry is probed THEN `mcp_discovery.probe_server` takes its
`not server.command` branch, sets `status="error"` and `error="no command"`, logs
`MCP probe failed [atlassian]: no command configured`, and never treats the entry as
a pointer.

1.3 WHEN the agent spec is rebuilt THEN `agent.rebuild_agent_config` finds no
`command` in any candidate source, logs
`Dropping MCP server 'atlassian': no command`, and omits the entry from
`~/.kiro/agents/kirocrew.json`, so the server's tools are unavailable in every Kiro
Crew session.

1.4 WHEN the failure is reported — because no registry URL is known to Kiro Crew, the
pointer was not carried anywhere that can resolve it, the catalog holds no entry of
that map key, or the catalog entry declares neither a usable `remotes[]` nor a
translatable `packages[]` — THEN every one of those distinct causes surfaces as the
same `no command` error, so the user cannot tell which one applies.

1.5 WHEN the dashboard renders Agent Capabilities → Connections → MCP Servers THEN a
registry entry's row shows STATUS: Error and TOOLS: "no command", attributing the
failure to the local entry rather than to an unresolved catalog pointer.

1.6 WHEN `kirocrew doctor` runs on a host with registry-installed MCP servers THEN it
reports no finding about them, because its `MCP Governance (enterprise)` section
covers only the outbound `agent.mcp_registry_mode` marker on Kiro Crew's own managed
servers.

1.7 WHEN the entry is dropped THEN its local-only overrides are dropped with it —
`env` (as on `playwright`), `oauth.clientId` and `oauth.redirectUri` (as on
`salesforce-prod-sobject-reads` and `microsoft-teams`), `headers` and `timeout` —
even though the catalog cannot supply them and no other source on the host carries
them.

1.8 WHEN a registry pointer is dropped from the emitted spec but the spec's `tools`
list and `permissions.rules` still reference that server's namespace THEN the spec
ships a dangling tool reference — on the reporter's host, `"@atlassian"` in `tools`
plus `atlassian/*` in `permissions.rules`, with `atlassian` absent from `mcpServers`
and `"includeMcpJson": false` making recovery from `mcp.json` impossible — and
kiro-cli drops those references silently at mount time with no exception and no log
line. The referenced server is one the defect itself dropped, so the reference
becomes legitimate again as soon as the pointer is carried into `mcpServers`.
(`"@atlassian-crew"` and `atlassian-crew/*` appeared alongside these in the
originally captured spec. They came from a developer diagnostic entry, not from the
defect, and are not part of this clause's evidence.)

### Expected Behavior (Correct)

These clauses state outcomes. They do not presuppose that Kiro Crew itself performs
catalog resolution: the pass-through candidate satisfies 2.1–2.3 by carrying the
pointer to the governed client that already resolves it.

2.1 WHEN a scanned `mcp.json` holds an entry with `"type": "registry"` THEN the
system SHALL carry the entry's registry-pointer identity — its `mcpServers` map key,
its `"type": "registry"` marker, and its local overrides — through discovery intact,
so that every downstream consumer can distinguish a pointer from a malformed entry.

2.2 WHEN a registry pointer is present and enabled THEN the system SHALL make that
server's tools available in the Kiro Crew session, whether the pointer is resolved by
the governed client from the emitted spec or resolved by Kiro Crew against an
operator-declared registry URL.

2.3 WHEN the pointer's catalog entry declares a `remotes[]` transport THEN the
session SHALL reach the server over that remote URL, and WHEN it declares a
`packages[]` install instead THEN the session SHALL reach it over the derived stdio
command — with neither form preferred over the other, since the catalog entry
declares the transport.

2.4 WHEN making a registry server available fails THEN the system SHALL report an
error naming the actual cause — no registry URL known to Kiro Crew, pointer not
resolvable by the governed client, name absent from the catalog, or a catalog entry
declaring neither a usable remote nor a translatable package — and SHALL NOT report
`no command`.

2.5 WHEN the dashboard renders a registry entry's row THEN the system SHALL show the
server's resolved transport and tool list on success, or the cause-naming error from
2.4 on failure.

2.6 WHEN `kirocrew doctor` runs on a host with registry-installed MCP servers that
cannot be made available THEN the system SHALL report that state as a finding naming
the affected server names and what the operator must supply.

2.7 WHEN a registry pointer carries local-only overrides — `env`, `oauth.clientId`,
`oauth.redirectUri`, `headers`, `timeout` — THEN the system SHALL carry those
overrides onto whatever entry it emits or resolves, because the catalog cannot supply
them.

2.8 WHEN the agent spec is emitted THEN either the registry pointer SHALL be carried
into `mcpServers`, or the spec's `tools` entries and `permissions.rules` naming that
server SHALL be omitted alongside it. A spec SHALL NEVER ship a `@server` tool
reference or a `server/*` permission rule for a server it does not declare.

Satisfied here by the first disjunct, with no dedicated mechanism. Every dangling
reference this defect produces names a server the defect itself dropped, so carrying
the pointer into `mcpServers` makes the reference legitimate and closes the clause. A
removal guard would only be required for a reference to a server that no scope
declares at all — a state this defect does not produce, and one the only observed
instance of came from a developer diagnostic entry rather than from the defect. No
guard is implemented in this fix; see `design.md` § "Out of scope" for the latent
defect that would need one.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a `mcp.json` entry carries an ordinary `command` and no `"type": "registry"`
THEN the system SHALL CONTINUE TO resolve that command through
`agent._resolve_command`, probe it, and emit it into the agent spec with the same
candidate priority (`kirocrew` > `kiro-global` > provider globals), argv pairing, and
`env`/`PATH` normalization it uses today.

3.2 WHEN a `mcp.json` entry carries a `url` THEN the system SHALL CONTINUE TO treat it
as a remote Streamable HTTP server, probe it over POST, preserve its OAuth `scopes`
and `clientId` hints through `kiro_oauth_wire_entry`, and never attempt registry
resolution on it.

3.3 WHEN an entry carries **neither** a `command` in any source **nor** the
`"type": "registry"` marker THEN the system SHALL CONTINUE TO report `no command` and
SHALL CONTINUE TO drop the entry from the agent spec without crashing, keeping
`test/test_agent.py::test_server_without_command_is_dropped` green. The new behavior
is scoped by the marker: a genuinely command-less entry with no marker is still
dropped, while a marked pointer is not.

3.4 WHEN `agent.mcp_registry_mode` is declared THEN the system SHALL CONTINUE TO stamp
`"type": "registry"` onto its own `kirocrew-core`, `kirocrew-cron`,
`kirocrew-computer` and `kirocrew-dashboard` entries while keeping their
Kiro-Crew-resolved `command` and `args`, and SHALL CONTINUE TO remove that marker when
the declaration is absent, keeping the outbound-marker, IDC-detection and doctor-section
behavior pinned by `test/test_mcp_registry_governance.py` green.

3.5 WHEN a managed `kirocrew-*` entry carries the `"type": "registry"` marker that
Kiro Crew stamped on it THEN the system SHALL CONTINUE TO launch it from its own
resolved command and SHALL NOT route it through catalog resolution, so the process
serving Kiro Crew's MCP tools stays the same build as the gateway.

3.6 WHEN an entry carries `disabled: true` in any scope THEN the system SHALL CONTINUE
TO refuse to probe it inside `probe_server`, including when that entry is a registry
pointer, so the consent gate is not bypassed by the new path.

3.7 WHEN a resolved registry command is spawned THEN the system SHALL CONTINUE TO
apply the existing security controls to it — the `hooks.py` PreToolUse denied-command
rules, the sensitive-path matchers, the governance ceiling
(`effective = POLICY ∩ PROFILE`), and `sandbox.wrap_argv` — with no waiver granted for
having come from a registry.

3.8 WHEN a registry fetch or a resolution error is logged or returned in an API
payload THEN the system SHALL CONTINUE TO redact credentials and header values through
`redact_mcp_headers` and `redact_mcp_error`, so a registry URL, an OAuth `clientId`, or
a token carried in a header never reaches a log line or the dashboard.

3.9 WHEN the dashboard's own MCP browse-and-install flow runs against the public
official registry THEN the system SHALL CONTINUE TO translate a `server.json` document
through `translate_install_plan` with unchanged **install-flow** method priority
(`npm` > `pypi` > `oci` > remotes), unchanged argv bucketing, unchanged refusal to
translate publisher `runtimeArguments` for OCI, and SHALL CONTINUE TO land the install
`disabled: true` pending consent. That priority is correct for choosing how to install
a browsed server and SHALL NOT be changed to suit pointer resolution, which has no
priority to apply.

3.10 WHEN Kiro Crew is installed on a personal account with no registry configured
THEN the system SHALL CONTINUE TO produce a byte-for-byte unchanged agent spec and
SHALL NOT emit any registry-resolution warning or diagnostic.

3.11 WHEN the host is **ungoverned** — no registry access mode in effect — THEN
carrying an inbound pointer through to the emitted spec SHALL NOT cost that host a
server it can otherwise reach. Outside registry mode the client's filter **inverts**
and marked entries are the ones dropped
(`test/test_mcp_registry_governance.py` module docstring). A pointer carries no
transport at all, so an ungoverned host cannot launch it whether Kiro Crew emits it
or drops it, and emitting it therefore loses nothing that today's drop retains.

This clause no longer forbids emitting a reference the inverted filter would strand,
and that is a deliberate weakening from its earlier wording. Carrying the pointer
into `mcpServers` makes it a member of the set the shared-ref sync tests against, so
that sync appends the server's `@ref` to `tools` — and to `allowedTools` where the
governance ceiling permits — where today it appends nothing. On an ungoverned host
the inverted filter then drops the marked entry, leaving that reference stranded.
The cost is bounded to the reference itself: kiro-cli discards a stranded reference
silently, the tool is no more available than it is today, and an auto-approve grant
for an unmounted server grants nothing, since there is no server to call. Preventing
the stranded reference would require gating the reference on a declared registry
mode, which this fix does not do — see `design.md` § "Out of scope". A test pins the
emitted shape on an ungoverned host rather than leaving it asserted.
