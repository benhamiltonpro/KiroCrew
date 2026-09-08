# MCP Registry Server Resolution Bugfix Design

## Overview

Kiro Crew drops every MCP server an Enterprise MCP Registry install writes into
`mcp.json`. Such an entry carries `"type": "registry"` and no `command` and no
`url` — it is a pointer into the administrator's catalog, not a transport. Kiro
Crew reads it, finds no command, and stops: `mcp_discovery` reports `no command`,
`agent.rebuild_agent_config` drops the entry, and the emitted spec keeps `tools`
and `permissions.rules` entries naming a server it no longer declares.

**The fix is pass-through.** The experiment recorded below settles the open
question from `bugfix.md`: kiro-cli 2.20.1 resolves a `"type": "registry"` entry
inlined in an agent spec's own `mcpServers` map, with `includeMcpJson: false`, on
both the remote and the package-backed path. So Kiro Crew stops dropping the
pointer, carries it into `~/.kiro/agents/kirocrew.json` with its local overrides,
and lets the governed client — which already holds the registry URL from
`GetProfile` — resolve it. No registry URL in Kiro Crew, no catalog fetch, no
enterprise-auth surface.

Two consequences shape the rest of the design. The dashboard cannot verify a
pointer it does not resolve, so it gets an honest not-verifiable status modelled
on the existing `needs_auth` precedent rather than a fabricated `ok`. And the
dangling-tool-reference surface closes as a *consequence* of pass-through rather
than through a mechanism of its own: every reference this defect strands names a
server the defect dropped, so restoring the pointer to `mcpServers` restores the
reference.

**Scope correction.** An earlier revision of this design argued that a
dangling-reference removal guard was independently required, and that the emitted
`@ref` had to be gated on registry mode to keep that guard honest on an ungoverned
host. Both rested on `atlassian-crew` — a reference in the reporter's spec that no
scope declares, which pass-through therefore cannot rescue. The developer has since
confirmed `atlassian-crew` was a test entry they added themselves while diagnosing
this issue. It is not a registry server and not a product of the defect. With it
withdrawn, the only dangling reference the defect produces on the observed host is
`@atlassian` plus `atlassian/*`, and pass-through returns `atlassian` to
`mcpServers`, which resolves both. Pass-through alone closes requirement 2.8. The
guard and the ref gate are descoped; the latent defect the analysis found is real and
is recorded under § "Out of scope" for a separate issue.

## Glossary

- **Bug_Condition (C)**: an entry in a scanned `mcp.json` whose `type` is
  `"registry"`, which therefore carries no `command` and no `url`, and which Kiro
  Crew consequently drops instead of carrying to the client that can resolve it.
- **Property (P)**: the pointer survives discovery with its identity intact, is
  emitted into `mcpServers` with `"type": "registry"` and its local overrides, and
  is never reported as `no command`.
- **Preservation**: every non-pointer path — ordinary `command` entries, `url`
  entries, genuinely command-less unmarked entries, managed `kirocrew-*` servers,
  the outbound `agent.mcp_registry_mode` marker, and the dashboard's
  browse-and-install flow — behaves exactly as it does today.
- **Pointer**: an `mcpServers` entry carrying `"type": "registry"`. Its
  `mcpServers` map key is the entire resolution key.
- **Pass-through**: emitting a pointer into the agent spec unresolved, for
  kiro-cli to resolve against the catalog.
- **Registry access mode**: the client state when the administrator has set an MCP
  Registry URL on the Kiro profile. Read from `GetProfile` at startup and
  persisted nowhere.
- **Inbound vs outbound**: *inbound* is handling a pointer someone else wrote
  (this defect). *Outbound* is `agent.mcp_registry_mode` stamping the marker onto
  Kiro Crew's own managed servers. They must not be conflated — see `bugfix.md`
  § scope note.
- **`_MCP_REGISTRY_TYPE`**: the existing `"registry"` constant at `agent.py:598`.
  The one spelling of the marker; no new literal is introduced.
- **`rebuild_agent_config`**: `agent.py`, the function that assembles
  `mcpServers`. Its drop site is `agent.py:3671`.
- **`_server_from_spec`**: `mcp_discovery.py:913`, which builds an
  `McpServerInfo` from a spec dict and currently never reads `type`.
- **`probe_server`**: `mcp_discovery.py:1726`, whose `not server.command` branch
  produces the `no command` error.

## Bug Details

### Bug Condition

The bug manifests when a scanned `mcp.json` source holds an entry marked
`"type": "registry"`. `_server_from_spec` discards the `type` field, so the
resulting `McpServerInfo` is indistinguishable from a malformed entry; every
downstream consumer then treats the absent `command` as the defect rather than as
the entry's defining shape.

**Formal Specification:**

```
FUNCTION isBugCondition(input)
  INPUT: input of type McpJsonEntry   -- one (name, spec) pair from a scanned mcp.json
  OUTPUT: boolean

  RETURN input.spec.type = "registry"
         AND input.spec.command IS ABSENT_OR_EMPTY
         AND input.spec.url IS ABSENT_OR_EMPTY
         AND NOT isManagedKiroCrewServer(input.name)
END FUNCTION
```

`isManagedKiroCrewServer` excludes the `kirocrew-*` names in
`agent._MANAGED_MCP_SERVERS`. A managed entry that carries the marker carries it
because Kiro Crew stamped it outbound, and it always retains its own resolved
`command` — so it never satisfies the condition, and requirement 3.5 keeps it off
this path.

### Examples

- `"atlassian": {"type": "registry", "disabled": false}` — expected: reaches the
  session over the catalog's `https://mcp.atlassian.com/v1/mcp` remote. Actual:
  `Dropping MCP server 'atlassian': no command`, absent from the emitted spec.
- `"aws-api": {"type": "registry"}` — expected: reaches the session over the
  `uvx` command the catalog's `packages[]` declares. Actual: dropped identically.
- `"playwright": {"type": "registry", "env": {"HOME": "/Users/bhamilton"}}` —
  expected: resolved with the local `env` override carried onto the resolved
  entry, because the catalog cannot know it. Actual: dropped, override lost.
- `"microsoft-teams": {"type": "registry", "oauth": {"clientId": "...",
  "redirectUri": "http://localhost:7878/oauth/callback"}}` — expected: resolved
  with both OAuth hints preserved. Actual: dropped, hints lost.
- `"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}` — an ordinary
  command entry, correctly kept today and unchanged by this fix. On the
  reporter's governed host it is also the **only** entry Kiro Crew keeps, and the
  only one kiro-cli discards.
- `"broken": {}` — no marker, no command anywhere. Expected and actual: `no
  command`, dropped. Unchanged by this fix (requirement 3.3).

### The inversion is live, not hypothetical

Requirement 3.11's inversion concern is observable on the reporter's host today.
`kiro-cli mcp list` reports `fetch` under `Ignored (not in registry)` while
resolving all ten pointers. So the current behavior is not "misses registry
servers" — it is **"keeps only the entry the client discards"**. Kiro Crew's
emitted `mcpServers` and the governed client's accepted set are exactly disjoint:

| Entry | In Kiro Crew's emitted spec | Accepted by kiro-cli 2.20.1 |
|---|---|---|
| ten registry pointers | no (dropped) | yes (✓ resolved) |
| `fetch` (has a command) | yes | no (⚠ ignored) |

The defect therefore costs the reporter every MCP server they have, not a subset.

### Two corrections to the requirements, both applied

1. **The pointer count.** `bugfix.md` said "Eleven such entries exist on that
   host" and then listed ten names. The observed count is **ten** pointers,
   matching the ten ✓ rows in `mcp list`; `fetch` is the eleventh `mcpServers`
   entry but is an ordinary command entry, not a pointer. Corrected in
   `bugfix.md`. Nothing else in the requirements depended on the count.
2. **The `atlassian-crew` evidence.** It was a developer diagnostic entry, not a
   registry server and not a product of the defect. Withdrawn from clause 1.8's
   evidence, annotated in place, and its consequences carried through this
   document — see § Overview "Scope correction" and § "Out of scope".

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- An entry with an ordinary `command` resolves through `agent._resolve_command`
  with the same candidate priority (`kirocrew` > `kiro-global` > provider
  globals), argv pairing and `env`/`PATH` normalization (3.1).
- An entry with a `url` stays a remote Streamable HTTP server, probed over POST,
  with its OAuth hints translated by `kiro_oauth_wire_entry` (3.2).
- An entry with neither a command nor the marker still reports `no command` and
  is still dropped without crashing (3.3).
- The outbound `agent.mcp_registry_mode` marker still lands on, and is still
  removed from, the managed `kirocrew-*` entries while they keep their resolved
  `command` and `args` (3.4).
- A managed `kirocrew-*` entry still launches from its own resolved command and
  is never routed through catalog resolution (3.5).
- A `disabled: true` entry is still refused inside `probe_server`, pointer or not
  (3.6).
- A resolved command still passes the `hooks.py` denied-command rules, the
  sensitive-path matchers, the `effective = POLICY ∩ PROFILE` ceiling and
  `sandbox.wrap_argv`, with no waiver for having come from a registry (3.7).
- `redact_mcp_headers` and `redact_mcp_error` still cover every new log line and
  API payload (3.8).
- The dashboard's browse-and-install flow still uses `translate_install_plan`
  with its `npm > pypi > oci > remotes` **install** priority intact (3.9).
- A personal install with no registry entries still produces a byte-for-byte
  unchanged agent spec and emits no new diagnostic (3.10).

**Scope:**

Every input that does NOT satisfy `isBugCondition` is unaffected. That is: every
`command` entry, every `url` entry, every unmarked command-less entry, every
managed `kirocrew-*` entry, and every host with no pointers at all. The marker is
the whole scope of the change.

## Hypothesized Root Cause

The four surfaces in `bugfix.md` all descend from one omission: **`type` is never
read.** `_server_from_spec` (`mcp_discovery.py:913`) constructs `McpServerInfo`
from `command`, `args`, `env`, `url`, `headers`, `scopes` and `client_id` and
never looks at `type`, so the pointer's defining field is discarded at the first
boundary it crosses. Everything after that is a correct inference from wrong
input:

1. **`probe_server` blames the local entry.** With `command=""` and `url=""` the
   `not server.command` branch at `mcp_discovery.py:1726` is the only reachable
   one. `no command` is an accurate description of the `McpServerInfo` and a
   wrong description of the `mcp.json` entry.
2. **`rebuild_agent_config` drops the entry.** The candidate loop at
   `agent.py:3628` collects `command` from each source, finds none, and takes the
   `not had_any_command` branch at `agent.py:3671`. The branch is correct for its
   stated case ("no candidate defined a command at all") and does not know that a
   pointer legitimately has none.
3. **The dangling ref is the drop's shadow, and the drop is what fixes it.** The
   shared-server sync (`agent.py:3766ff`) adds a `@ref` only under
   `elif alias in valid_servers` — that is, only for a server present in the
   emitted `mcpServers`. A dropped pointer is not in `valid_servers`, so the add
   branch declines, an already-present ref is left in place, and the spec ends up
   naming a server it does not declare. Reading the reporter's live
   `~/.kiro/agents/kirocrew.json` confirms it: `tools` and `allowedTools` hold
   `"@atlassian"`, `permissions.rules` allows `atlassian/*`, `mcpServers` declares
   no `atlassian`, and `includeMcpJson` is `false` so kiro-cli cannot recover it.

   This resolves with surfaces 1 and 2 rather than separately. The stranded name is
   the name the drop removed, so restoring the pointer to `mcpServers` puts `alias`
   back in `valid_servers`, the add branch fires, and the ref is legitimate again.
   No removal mechanism is needed for anything this defect produces. There *is* a
   residual defect in the same code — a ref for a server no scope declares at all
   matches neither branch and survives indefinitely — but nothing in this issue
   produces that state, and it is recorded under § "Out of scope".

### Experiment: does pass-through work?

`bugfix.md` leaves exactly one open question: **does kiro-cli resolve a
`"type": "registry"` entry inlined in an agent spec's `mcpServers` map, as opposed
to one in `mcp.json`?** It was run on the reporter's live macOS host.

**Method.** A throwaway global agent spec at
`~/.kiro/agents/registry-passthrough-test.json`, modelled on the existing
`remote-mcp-test.json`. No existing spec was modified; `~/.kiro/agents/kirocrew.json`
and `~/.kiro/settings/mcp.json` were never written to. `PAGER=cat kiro-cli mcp list`
is the probe: it groups by agent, includes global agents, needs no TTY, and — unlike
`kiro-cli mcp status`, which only echoes the declared entry — it reports *resolution*
(`✓` plus the derived `uvx`/`npx` command).

**Host version.** `kiro-cli 2.20.1`. Every claim below is against 2.20.1, not the
2.18.0 in the issue text.

**Baseline.** `~/.kiro/agents/` held 12 files; SHA-256 of each was recorded, as was
the full `mcp list` output.

**Variant 1 — the shape pass-through would emit.** `"includeMcpJson": false`,
`mcpServers` holding three marked pointers with no command and no url: `atlassian`
(catalog declares a remote), `aws-api` (catalog declares a package), and
`kirocrew-not-in-catalog` as a negative control.

```bash
PAGER=cat kiro-cli mcp list
```

```text
  registry-passthrough-test
    ✓ atlassian
    ✓ aws-api      uvx
    Ignored (not in registry):
    ⚠ kirocrew-not-in-catalog
```

**Conclusive.** `atlassian` resolved with no command shown — the remote path.
`aws-api` resolved with `uvx` shown — kiro-cli derived the stdio command from the
catalog's `packages[]`, which is work Kiro Crew never did. The negative control
was ignored, so `✓` is a real catalog match rather than blanket acceptance. Both
transport paths of requirement 2.3 are satisfied by pass-through, from an agent
spec, with `includeMcpJson: false`.

**Variant 2 — `includeMcpJson: true`, plus local overrides.** The same map with
`env` and `timeout` added to `atlassian`. All ten of `mcp.json`'s pointers merged
in and resolved, `atlassian` still resolved with its overrides present, and the
warning block grew by exactly one line per collision:

```text
WARNING: MCP server 'salesforce-prod-sobject-reads' is already configured in agent config. Skipping duplicate from legacy mcp.json.
WARNING: MCP server 'aws-api' is already configured in agent config. Skipping duplicate from legacy mcp.json.
WARNING: MCP server 'atlassian' is already configured in agent config. Skipping duplicate from legacy mcp.json.
```

Only the first line is baseline noise, emitted for a different agent. So the
precedence the reporter's evidence implies is confirmed — **agent config wins,
the `mcp.json` duplicate is skipped, resolution still succeeds** — and the
collision is *visible*. Under variant 1 no extra warning appeared. Since Kiro Crew
pins `includeMcpJson: false` (`docs/architecture/mcp.md` § "`includeMcpJson` is
pinned false"), **pass-through introduces no new warning line on the reporter's
host.** That removes the concern raised against emitting a pointer Kiro Crew also
found in `mcp.json`.

**Variant 3 — is the marker load-bearing?** The same three names with the marker
removed: `atlassian` as bare `{}`, `aws-api` with `command: "/bin/false"`,
`datadog` with `type: "stdio"` and `command: "/bin/false"`.

```text
  registry-passthrough-test
    ✓ atlassian
    ✓ aws-api      uvx
    ✓ datadog
```

**In registry access mode, kiro-cli 2.20.1 matches on the map key alone.** All
three resolved from the catalog without the marker, and `aws-api` resolved to the
catalog's `uvx` command *in preference to* the local `/bin/false` — the catalog
overrides a declared command. This diverges from what
`docs/guides/enterprise-mcp-governance.md` documents (a symmetric filter keyed on
the marker), and the doc's table is therefore wrong for 2.20.1. It also explains a
baseline observation: `kirocrew-core` and `kirocrew-cron` show as `⚠ Ignored (not
in registry)` on this host purely because the administrator has not allow-listed
those names — the host's `kirocrew.json` carries no marker at all, and adding one
would not have admitted them.

**Cleanup.** The throwaway spec was deleted. `~/.kiro/agents/` is back to its 12
files with all SHA-256 digests byte-identical, and `mcp list` matches the baseline
as a set. (A line-order difference appeared within one unrelated agent's ignored
list; sorting both captures shows them identical, so it is kiro-cli's own
nondeterministic map ordering, not a state change.) No residue.

**Limits of the evidence — stated plainly.**

- The host is governed, so **non-registry mode could not be observed.** The
  inversion in requirement 3.11 is unverified on this host and stays a test in the
  suite rather than a manual check, because the developer's host cannot exercise
  that path at all.
- Marker-insensitivity is a 2.20.1 observation. Carrying the marker anyway costs
  nothing and is what the documented contract asks for, so D2 carries it rather
  than relying on the map-key match.
- `mcp list` proves *resolution*, not a completed tool handshake. It reports what
  kiro-cli will mount; it does not prove the remote answers.

**Conclusion: pass-through works, and no fallback is built.** The open question is
settled affirmatively, so the design does not add a Kiro-Crew-side catalog
fetcher, does not need an operator-declared registry URL, and does not touch the
enterprise-auth surface `AGENTS.md` forbids. The two non-blocking questions
carried in `bugfix.md` — static JSON versus the v0.1 REST API, and whether to
replicate the version-override relaunch — were both scoped to the fallback path
and are therefore moot. D8 records the one seam that keeps them answerable later
without building speculative machinery now.

## Correctness Properties

Property 1: Bug Condition - A registry pointer survives to the governed client

_For any_ input where the bug condition holds (`isBugCondition` returns true), the
fixed code SHALL carry the pointer through discovery with its `"type": "registry"`
marker intact, SHALL emit it into the agent spec's `mcpServers` under its original
map key with the marker and every local override it carried (`env`,
`oauth.clientId`, `oauth.redirectUri`, `headers`, `timeout`) preserved, and SHALL
NOT report `no command` for it on any surface.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.7, 2.8**

2.8 joins this property as a consequence of the scope correction. With the removal
guard descoped, the clause closes entirely through its first disjunct — the pointer is
carried into `mcpServers` — which is exactly what this property already asserts. No
separate property is needed for it, and none asserts the stronger unconditional form
the withdrawn guard would have provided.

Property 2: Preservation - Every non-pointer entry is untouched

_For any_ input where the bug condition does NOT hold (`isBugCondition` returns
false), the fixed code SHALL produce the same result as the original code,
preserving command resolution and its candidate priority, `url` handling and OAuth
wire translation, the `no command` drop for an unmarked command-less entry, the
managed `kirocrew-*` launch path and outbound marker behavior, the disabled-probe
refusal, and a byte-for-byte identical agent spec on a host with no pointers.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.10**

## Fix Implementation

### Changes Required

The experiment above establishes pass-through, so the change is a marked-pointer
branch at each of the four surfaces, plus the doctor finding and the two
documentation corrections. Nothing fetches a catalog, and nothing removes a tool
reference. Decision numbering is preserved across the scope correction: **D4 and D5
are withdrawn** and their numbers are not reused.

#### D1 — Carry the pointer through discovery

**File**: `src/kiro_crew/mcp_discovery.py`
**Function**: `_server_from_spec` (line 913), and the `McpServerInfo` dataclass

1. Add an `is_registry_pointer: bool = False` field to `McpServerInfo`, set from
   `spec.get("type") == _MCP_REGISTRY_TYPE`. A boolean, not the raw `type`
   string: `type` also legitimately carries `"stdio"` (which
   `test_mcp_registry_governance.py::test_refresh_preserves_a_user_transport_hint`
   pins as the user's own), and no consumer asks for anything but "is this a
   pointer".
2. Import the marker constant rather than re-spelling `"registry"`. It already
   exists as `agent._MCP_REGISTRY_TYPE`; both modules must read one spelling, so
   it moves to a shared home (`mcp_discovery` is the lower-level module and
   `agent` already imports from it — the constant goes there and `agent` imports
   it, keeping the existing name).
3. `is_remote` stays as it is. A pointer has no `url`, so it is already False,
   and widening that property would send pointers down `_probe_remote`.

#### D2 — Stop dropping the pointer, and carry its overrides

**File**: `src/kiro_crew/agent.py`
**Function**: `rebuild_agent_config`

4. Add a pointer branch **before** the candidate/resolution loop at line 3628,
   alongside the existing `if spec.get("url")` branch at line 3552 — both are
   "this entry needs no command resolution" cases, and putting the new one beside
   the old keeps that shape visible. The branch fires when
   `spec.get("type") == _MCP_REGISTRY_TYPE` and the name is not in
   `_MANAGED_MCP_SERVERS` (requirement 3.5: a managed entry carries the marker
   because Kiro Crew stamped it and keeps its own resolved command, so it must
   continue down the existing path).
5. The emitted entry is the pointer verbatim plus nothing: the map key, `type`,
   and the local overrides. `env` goes through the existing `emit_env` for the
   same `PATH` normalization every other emitted entry gets. The OAuth hints go
   through the existing `kiro_oauth_wire_entry` for the same internal-to-wire
   rename the `url` branch performs — the pointer's hints are the same fields in
   the same internal spelling, so reusing that one boundary is what keeps a
   pointer's hints from being the one shape written in the wrong spelling.
   `headers` and `timeout` are copied as-is. No `command` and no `url` is
   synthesized: the experiment shows kiro-cli overrides a declared command from
   the catalog anyway, so writing one would be inert at best and misleading in
   the spec at worst.
6. The `not had_any_command` drop at line 3671 is left **exactly as it is**. It
   is now simply unreachable for a marked pointer, because the new branch
   `continue`s first. That is what keeps
   `test_agent.py::test_server_without_command_is_dropped` green without
   modification — its fixture has no marker.

#### D3 — An honest probe status, never a fake `ok`

**File**: `src/kiro_crew/mcp_discovery.py`
**Function**: `probe_server` (line 1726), and `McpServerInfo.to_dict`

7. Add a pointer branch ahead of the `not server.command` check and **after** the
   existing `server.disabled` refusal, so requirement 3.6 keeps holding for a
   disabled pointer. It sets a new status and returns without spawning anything.
8. **The status is `registry_pointer`, rendered as not-verifiable.** Kiro Crew
   cannot resolve the pointer — that is the entire premise of pass-through — so
   it must not claim `ok`, and requirements 2.4/2.5 forbid `no command`. This is
   the same epistemic situation the codebase already has a precedent for:
   `needs_auth` exists because "the probe runs without the token kiro-cli holds",
   and `McpTab.tsx` renders it as *not-verified* with a hover hint rather than as
   an error or a success. A pointer is the same class — Kiro Crew structurally
   cannot see the answer — so it reuses that tone and that machinery rather than
   inventing a second vocabulary for "cannot tell". Extend the status comment on
   the dataclass field, `mcpStatusLabel`, `mcpStatusHint` and
   `connectionStateFor` accordingly.
9. `tools` stays empty for a pointer, and `probe_mode` is neither `handshake` nor
   `declared` — no handshake happened and Kiro Crew has no declaration to fall
   back on. An empty tool list next to a not-verified badge is the honest
   rendering; a fabricated list would be worse than none.
10. New user-facing strings go through the i18n catalog per `AGENTS.md`. The
    badge label and hover hint are added to all 12 locale catalogs.

#### D4 — Withdrawn: the dangling tool reference guard

Descoped by the `atlassian-crew` scope correction. The guard's independent
justification was a reference pass-through cannot rescue, and the only observed
instance of one was a developer diagnostic artifact rather than a product of the
defect. Every reference this defect strands names a server the defect dropped, so
D2 restores it and requirement 2.8 closes without a removal mechanism. The genuine
latent defect the analysis surfaced is preserved under § "Out of scope".

Nothing here is implemented, and no `@ref`, `allowedTools` grant or
`permissions.rules` entry is removed by this fix.

#### D5 — Withdrawn: the ungoverned-host ref gate

Descoped with D4. The gate existed to keep D4's guard honest on an ungoverned host:
with the guard gone there is nothing for it to gate. `_mcp_registry_mode()` is not
consulted anywhere on the inbound path, so the inbound behavior is marker-driven and
mode-independent throughout. The ungoverned-host analysis this decision carried is
re-derived, and one of its conclusions revised, under § "Out of scope" →
"The ungoverned host, re-examined".

#### D6 — Cause-naming errors

18. Requirement 2.4 forbids `no command` and demands the actual cause. With
    pass-through, and with D5's withholding path withdrawn, the taxonomy collapses
    to the two states Kiro Crew can honestly distinguish: *carried to the client,
    not verifiable here* (the normal pass-through outcome, D3's status) and
    *pointer disabled* (the existing `disabled` status, unchanged). A pointer is
    never withheld, so there is no third state to name. The catalog-side causes —
    name absent from the catalog, a
    catalog entry declaring neither a usable remote nor a translatable package —
    are **not** Kiro Crew's to report, because Kiro Crew never reads the catalog.
    They are reported by `kiro-cli mcp list` as `⚠ Ignored (not in registry)`,
    and D7 points the operator there rather than guessing.

#### D7 — The doctor finding

19. Requirement 2.6: `kirocrew doctor` grows a pointer subsection in its existing
    `MCP Governance (enterprise)` section, naming the pointer server names found in
    the scanned `mcp.json` sources and what the operator must do. With the ref gate
    withdrawn there is only **one** state to report, and it does not depend on
    `mcp_registry_mode`: these pointers are carried through to kiro-cli, Kiro Crew
    cannot verify that they resolve, and `kiro-cli mcp list` is the command that
    can — a `⚠ Ignored (not in registry)` row there is an allow-listing gap for the
    administrator, and on a host with no registry access at all every pointer will
    read that way. Doctor does not attempt to classify which of those two it is:
    the governed client's mode is read from `GetProfile` and persisted nowhere, so
    Kiro Crew has no local source for it, and guessing from `mcp_registry_mode`
    would report Kiro Crew's own outbound setting as though it were the client's
    inbound state. It must not render as a verified success, matching the existing
    section's `cannot verify` discipline that
    `test_mcp_registry_governance.py::test_declared_and_marked_reports_the_names_to_allow_list`
    pins.

#### D8 — The one seam, and what is deliberately not built

20. No catalog fetcher, no operator-declared registry URL, no version-override
    relaunch. The experiment removed the need, and `AGENTS.md`'s prohibition on
    re-introducing enterprise-auth surface makes the fallback the expensive path
    to have chosen speculatively.
21. The seam that keeps it answerable later is the single pointer branch in
    `rebuild_agent_config` (D2, step 4). It is the one place that decides what a
    pointer becomes in the emitted spec, so a future Kiro-Crew-side resolution
    would replace that branch's body and nothing else. If it is ever built,
    `mcp_providers/official.py::translate_install_plan` supplies the reusable
    half — camelCase/snake_case tolerance, the wrapped `{"server": {...}}` item
    shape, `_pick_remote`'s streamable-http-over-sse choice, and the refusal to
    translate publisher `runtimeArguments` for OCI — but **not** its
    `npm > pypi > oci > remotes` priority, which requirement 3.9 pins as correct
    for the install flow it serves and which is wrong for pointer resolution: a
    governed catalog entry *declares* its transport, so there is nothing to
    prioritize, and preferring packages would fail the reporter's `atlassian`
    entry outright since it has no `packages[]` at all.

#### D9 — Constants

22. No new string literals in business logic, per `AGENTS.md`. `_MCP_REGISTRY_TYPE`
    is reused (relocated per D1 step 2, name unchanged). The new status string and
    the local-override key tuple (`env`, `headers`, `timeout`, plus the OAuth keys
    handled via `kiro_oauth_wire_entry`) become named module constants in
    `mcp_discovery` and `agent` respectively, each owned by the module that acts
    on it.

#### D10 — Same-commit documentation updates

Both files are in the `AGENTS.md` read-before-you-touch table and MUST be updated
in the same commit:

23. **`docs/architecture/mcp.md`** — `### Merge order in rebuild_agent_config()`
    gains the pointer branch and its position relative to the `url` branch, and
    notes that a carried pointer joins `valid_servers` and so becomes eligible for
    the shared-ref sync's add branch; `## Discovery and probing` gains
    `is_registry_pointer` and the `registry_pointer` status. No ref-removal pass is
    documented, because none is added.
24. **`docs/guides/enterprise-mcp-governance.md`** — its "What governance
    actually does" table is **factually wrong for kiro-cli 2.20.1** and must be
    corrected: variant 3 shows admission keyed on the `mcpServers` map key
    matching a catalog entry, with an unmarked entry admitted and a declared
    command overridden by the catalog's. The page also gains an inbound section
    (it currently covers only the outbound marker) explaining that
    registry-installed pointers are carried through to the client unconditionally,
    that `mcp_registry_mode` is outbound-only and does not gate them, and that
    `kiro-cli mcp list` is the verification command. The version floor note gains
    the 2.20.1 observation.

## Out of scope: latent defect recorded for a separate issue

**Not required to close #3308.** This section is preserved analysis, not planned
work. It was surfaced by an *artifact* — `atlassian-crew`, a test entry the
developer added while diagnosing this issue — rather than by the defect. Nothing in
this issue produces the state it describes, and no clause in `bugfix.md` depends on
it: requirement 2.8 closes through pass-through (D2), because every reference this
defect strands names a server the defect dropped. The analysis is kept because the
defect it identifies is genuine and survives independently of #3308.

### The finding

A tool reference for a server that **no scope declares at all** is never cleaned up.
Two mechanisms are involved, and they are not equally culpable.

1. **The shared-ref sync leaves it (`agent.py:3766ff`).** The loop iterates the
   scanned `mcp.json` scopes and has exactly two branches: it *removes* a `@ref`
   under `if spec.get("disabled") or alias in _disabled_anywhere`, and it *adds* one
   under `elif alias in valid_servers`. A ref whose server appears in no scope is
   never visited by that loop at all — there is no `spec` to iterate — and even a
   visited entry that is neither disabled nor present in the emitted `mcpServers`
   matches neither branch. Either way an existing ref in `tools` or `allowedTools`
   is left untouched, indefinitely. There is no third branch for "this ref names
   nothing", so nothing in the rebuild ever retires one.

2. **`permissions.rules` is never re-derived — deliberately.** `_seed_kas_permissions`
   (`agent.py:3056`) returns early when `permissions` is already present. Unlike the
   sync gap above, this is a documented choice, not an oversight: its own docstring
   records that recognising Crew's output by shape was implemented and then removed,
   because a blanket `allow` is exactly what a user writes too, so the rule that keeps
   a derived policy current is the same rule that silently overwrites a hand-written
   one. It also bounds the cost: the wire projection re-derives from `allowedTools`
   every session and outranks the file, so the stale on-disk block applies only when
   Crew is not injecting an agent. A future fix must treat this as a constraint to
   work within rather than a bug to reverse.

### What a future fix would need to cover

- **The missing third case.** Retire a `@ref` and an `allowedTools` grant whose
  server the final `mcpServers` does not declare — the case neither existing branch
  owns. It has to run after the server map is final, and it cannot be folded into the
  existing loop, which is driven by scanned scopes rather than by the emitted spec.
- **A provenance answer before touching `permissions.rules`, or a decision not to.**
  The ambiguity that stopped `_seed_kas_permissions` from refreshing has not gone
  away. A removal pass there must distinguish a stale derived rule from an operator's
  own, or else confine itself to `tools`/`allowedTools` and accept the bounded
  staleness the docstring already accepts.
- **Diagnostic, never fatal.** `bridges._unresolvable_tool_refs` already establishes
  the facts and the disposition: kiro-cli resolves a `@` ref against the agent's own
  `mcpServers` plus the global `mcp.json`, drops a name in neither **silently** at
  mount time with no exception and no log line, and `includeMcpJson: false` means an
  ambient entry cannot rescue it. A dangling ref costs a tool and does not break the
  agent, so the app-agent path reports rather than raises. Reuse that reasoning
  instead of inventing a second mechanism.
- **Visibility.** Log each retirement through the existing `sel()` calls beside
  `mcp_tools_removed`, so an operator can see a grant disappear.
- **Tests.** A ref for a server no scope declares is retired; a ref for a declared
  server survives; two consecutive rebuilds are identical, so a retirement pass does
  not oscillate against the sync that adds refs back.
- **How it is actually reached.** Not through this defect. Uninstalling or renaming a
  server in `mcp.json` while its refs remain in the spec is the realistic trigger,
  which is also why it is worth its own issue rather than a note in this one.

### The ungoverned host, re-examined

Requirement 3.11 was re-derived after D5's withdrawal. One half of the original
conclusion holds and one does not.

**Holds: emitting the pointer entry unconditionally costs an ungoverned host
nothing.** A pointer carries no transport at all — no `command`, no `url` — so an
ungoverned host cannot launch it whether Kiro Crew emits it or drops it. No server
that today's drop retains is lost by emitting it, and the entry is already correct
the moment the host becomes governed. This is unchanged by the descope and needs no
gate.

**Does not hold: the claim that without a ref gate the reference behaves exactly as
it does today.** Checking the code rather than assuming it: the add branch is
`elif alias in valid_servers`, and `valid_servers` is the dict that becomes
`config["mcpServers"]` (`agent.py:3702`). Today a pointer is absent from it, the add
branch declines, and **no ref is created**. After D2 the pointer is present, so the
add branch fires and appends `@alias` to `tools` — and to `allowedTools` where
`_may_auto_approve` permits. On an ungoverned host the client's inverted filter then
drops the marked entry, leaving that newly added reference stranded. That is a real
behavior change, not a no-op, and stating otherwise would have been wrong.

**Why it is nonetheless accepted here.** The cost is confined to the reference:
kiro-cli discards a stranded ref silently, the tool is no more available than it is
today, and an `allowedTools` grant for an unmounted server grants nothing because
there is no server to call — no privilege is gained. So the host is not made worse
off in any way a user or an operator can observe, which is the property 3.11 exists
to protect. What is lost is the stronger guarantee the original clause asserted, that
no stranded reference is ever emitted at all. `bugfix.md` 3.11 has been revised to
say what is actually true rather than to keep asserting that guarantee, and the
stronger property — which the withdrawn D5 gate, or a narrower version of it, would
restore — belongs with the same future issue as the finding above.

## Testing Strategy

### Validation Approach

Two phases. First surface counterexamples on **unfixed** code to confirm the root
cause and the ungoverned-inversion behavior. Then verify the fix resolves the bug
condition and preserves everything else. Per `AGENTS.md`, no test may touch the
operator's machine: `KIROCREW_HOME` is pinned per test by the rootdir conftest,
and every spec fixture is written under `tmp_path`. Nothing in this suite invokes
the real `kiro-cli` — the experiment above is the evidence for kiro-cli's
behavior, and the suite asserts Kiro Crew's emitted artifact against it.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples demonstrating the bug BEFORE implementing the
fix. Confirm or refute the root cause. If refuted, re-hypothesize.

**Test Plan**: Write a `mcp.json` fixture holding the reporter's actual pointer
shapes under `tmp_path`, run discovery and `rebuild_agent_config` on the unfixed
code, and assert the observed failures.

**Test Cases**:

1. **Pointer is dropped**: a `{"type": "registry"}` entry is absent from the
   rebuilt `mcpServers` (will fail on unfixed code — i.e. the entry *is* absent,
   demonstrating the bug).
2. **Pointer identity is lost in discovery**: `_server_from_spec` returns an
   `McpServerInfo` with `command == ""`, `url == ""` and no way to tell it from a
   malformed entry (will fail on unfixed code).
3. **Probe reports `no command`**: `probe_server` sets `status == "error"` and
   `error == "no command"` for a pointer (will fail on unfixed code).
4. **Overrides are lost**: the `env` on a `playwright`-shaped pointer and the
   `oauth.clientId`/`redirectUri` on a `microsoft-teams`-shaped one reach nothing
   downstream (will fail on unfixed code).
5. **The refs survive the drop**: seed a spec whose `tools`, `allowedTools` and
   `permissions.rules` name a pointer that the rebuild then drops, and assert all
   three still name it afterwards — the dangling-reference surface, produced by the
   drop and resolved by D2 restoring the entry. Neither branch of the shared sync
   touches it while the server is absent from `valid_servers`.
6. **Edge case, the inversion**: a pointer on a host with `mcp_registry_mode`
   undeclared. Records the ungoverned baseline before the fix so 3.11's
   no-worse-off requirement is measured rather than asserted. The developer's host
   is governed and cannot exercise this path, so it stays a test in the suite
   rather than becoming a manual check.

**Expected Counterexamples**:

- Every pointer absent from the emitted `mcpServers`, with `no command` on every
  surface.
- Possible causes: `type` discarded in `_server_from_spec`; the
  `not had_any_command` branch owning a case it does not recognise; the shared
  sync's add/remove branches both declining to fire.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed
function produces the expected behavior.

**Pseudocode:**

```
FOR ALL input WHERE isBugCondition(input) DO
  result := rebuildAgentConfig_fixed(input)
  ASSERT expectedBehavior(result)
END FOR
```

Where `expectedBehavior` asserts: the map key is present in `mcpServers`, the
entry carries `type == "registry"`, every local override the input carried is
present on it, no `command` or `url` was synthesized, and no surface reports
`no command`.

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the
fixed function produces the same result as the original function.

**Pseudocode:**

```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT rebuildAgentConfig_original(input) = rebuildAgentConfig_fixed(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation
checking because it generates many cases across the input domain automatically,
catches edge cases manual unit tests miss, and gives a strong guarantee that
behavior is unchanged for every non-pointer entry — which is the whole regression
surface here, and much wider than the pointer path being added.

**Test Plan**: Observe behavior on UNFIXED code for command entries, url entries,
unmarked command-less entries and managed servers, then write property-based tests
capturing that behavior and re-run them against the fix.

**Test Cases**:

1. **Command-entry preservation**: observe candidate priority, argv pairing and
   `env`/`PATH` normalization on unfixed code, then assert unchanged (3.1).
2. **Url-entry preservation**: observe `kiro_oauth_wire_entry` translation and
   POST probing on unfixed code, then assert unchanged (3.2).
3. **Unmarked command-less preservation**: observe the `no command` drop on
   unfixed code, then assert unchanged (3.3).
4. **Byte-for-byte spec on a pointer-free host**: observe the emitted spec on
   unfixed code with no pointers present, then assert the fixed code emits an
   identical file (3.10) — the strongest single preservation assertion, since it
   covers ordering and key presence, not just values.

### Unit Tests

The two named suites must stay green **without modification**, and each is green
for a structural reason worth stating:

- **`test/test_agent.py::test_server_without_command_is_dropped`** — its fixture
  carries no marker, so the new branch does not fire and the entry reaches the
  unchanged line 3671 drop. If this test needs editing, the marker scoping in D2
  is wrong.
- **`test/test_mcp_registry_governance.py`, entire module** — every test in it
  concerns the *outbound* marker on managed servers, the IDC probe, or the doctor
  section. D2's `_MANAGED_MCP_SERVERS` exclusion keeps managed entries off the new
  path entirely, so all of it is untouched. `test_refresh_preserves_a_user_transport_hint`
  is the specific reason D1 stores a boolean rather than the raw `type` string.

New unit tests:

- `_server_from_spec` sets `is_registry_pointer` for a marked entry and clears it
  for `type: "stdio"`, absent `type`, and a malformed non-string `type`.
- `rebuild_agent_config` emits a marked pointer under its original map key with
  the marker retained.
- Each local override carries through individually and in combination: `env`
  (through `emit_env`), `oauth.clientId`, `oauth.redirectUri` (both through
  `kiro_oauth_wire_entry`, asserted in wire spelling), `headers`, `timeout`.
- No `command` and no `url` is synthesized onto an emitted pointer.
- A managed `kirocrew-*` entry carrying the marker keeps its resolved `command`
  and `args` and does not take the pointer branch (3.5).
- `probe_server` returns the `registry_pointer` status for a pointer, and returns
  `disabled` — not `registry_pointer` — for a disabled pointer, so the consent
  gate is not bypassed by the new path (3.6).
- No surface produces the string `no command` for a pointer (2.4).
- The reporter's exact shape recovers through pass-through alone: seed `@atlassian`
  in `tools` and `allowedTools` and `atlassian/*` in `permissions.rules` with
  `atlassian` absent from `mcpServers`, rebuild, and assert `atlassian` is declared
  and all three references are now legitimate. Assert the negative too — **no
  reference is removed by this fix**, so a ref for a server that happens to be
  absent is left exactly as the unfixed code leaves it. That pins the descope: if a
  removal appears here, the out-of-scope work has leaked in.
- **Ungoverned inversion (3.11)**: with `mcp_registry_mode` undeclared, the pointer
  entry is emitted anyway — the inbound path never reads that setting — and the
  shared sync adds its `@ref` because the entry is now in `valid_servers`. Assert no
  server is lost relative to today's drop, and pin the ref's presence as the
  accepted outcome rather than asserting it is absent. This is the test that would
  catch a silent reintroduction of D5's gate, and the one that documents the stranded
  reference as known rather than as a surprise.
- Redaction holds on the new paths: a pointer's OAuth `clientId` and any header
  value stay out of log lines and API payloads via `redact_mcp_headers` /
  `redact_mcp_error` (3.8).
- `cli_doctor` reports the pointer names it found, does not render as verified
  success, and its output does not vary with `mcp_registry_mode` — that setting is
  outbound-only and the finding must not imply otherwise (2.6).

### Property-Based Tests

- Generate arbitrary `mcp.json` maps mixing command entries, url entries,
  unmarked command-less entries, managed names and marked pointers; assert the
  emitted spec's non-pointer entries are identical to the unfixed code's output —
  the preservation property, over the whole input domain.
- Generate arbitrary override combinations on a pointer and assert every declared
  override appears on the emitted entry and nothing else is added.
- Generate arbitrary `tools`/`allowedTools`/`permissions.rules` sets against
  arbitrary pointer maps and assert the invariant 2.8 actually rests on here: every
  pointer in the input is declared in the emitted `mcpServers`, so no reference
  naming a *pointer* is dangling. The stronger unconditional form — no `@server` ref
  and no `server/*` rule names any absent server — is deliberately **not** asserted,
  because no removal pass exists to make it true; it belongs to the out-of-scope
  work.

### Integration Tests

- Full `install_agent` run on a fixture home holding the reporter's ten pointer
  shapes plus `fetch`: assert all eleven reach `mcpServers`, the ten pointers keep
  their markers, and `fetch` keeps its resolved command — the disjoint-sets defect
  closed end to end.
- The same run with `mcp_registry_mode` undeclared: entries present identically,
  refs added identically, spec internally consistent. The emitted `mcpServers` must
  be byte-identical to the declared-mode run, since nothing on the inbound path
  reads that setting.
- Dashboard payload for a pointer row renders the not-verified state with its
  hover hint and an empty tool list, asserted through the existing `McpTab`
  patterns rather than a new rendering path.
- A rebuild over the reporter's actual `kirocrew.json` shape — `includeMcpJson:
  false`, `@atlassian` in `tools` and `allowedTools`, `atlassian/*` in
  `permissions.rules`, `atlassian` absent from `mcpServers` — ends with `atlassian`
  declared and all three references legitimate, with nothing removed. (`atlassian-crew`
  is deliberately not in this fixture: it was a developer diagnostic entry, not part
  of the defect.)
- Idempotence: two consecutive rebuilds produce an identical spec, so the sync
  settles rather than oscillating once a pointer joins `valid_servers`.
