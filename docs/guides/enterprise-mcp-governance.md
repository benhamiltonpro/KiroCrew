# Kiro Crew behind enterprise MCP governance

Applies when the Kiro account `kiro-cli` is signed in to is an **enterprise**
account — IAM Identity Center (or an external IdP such as Okta / Entra ID
fronting it), or an API key — and an administrator has configured an **MCP
registry**. Personal accounts (Builder ID, social sign-in) are not subject to
organization-level MCP controls and need nothing on this page.

## The symptom

Kiro Crew starts, the dashboard works, chat works — and a large part of the
product is quietly absent. `spawn_run` does nothing, `cron_add` is unavailable,
`learn_add` never saves, the knowledge tools are missing, the research agent has
no tools to work with. Nothing errors. `kirocrew doctor` reports the MCP servers
healthy.

That combination — healthy locally, absent in sessions — is the signature of MCP
governance, because the two checks are measuring different things:

- Kiro Crew's own probe **spawns each server directly** and completes an MCP
  handshake with it. That succeeds regardless of governance.
- `kiro-cli` applies governance **when it assembles a session**, after reading
  the agent spec. Every server it drops there is dropped silently.

## What governance actually does

The administrator sets two things on the Kiro profile (Kiro console → Settings →
Shared settings): an MCP on/off toggle, and an **MCP Registry URL** pointing at a
registry JSON file listing the allow-listed servers.

With a registry URL configured, the client is in **registry access mode**.

Kiro's own documentation describes the filter as keyed on the
`"type": "registry"` marker in both directions. The registry-mode row below does
not say that, because it records **behavior observed on kiro-cli 2.20.1** instead:
admission is keyed on the `mcpServers` **map key** alone. That is an observation
of one build rather than a contract, and a later build may narrow it back to the
documented form, so treat the marker as required and the map-key match as the
observed floor. The non-registry row is the documented behavior and is not part of
the observation — the host the experiment ran on was governed, so non-registry
mode could not be exercised on it.

| Access mode | Entries that connect | Entries that are dropped |
|---|---|---|
| registry (a registry URL is set) — observed on 2.20.1 | any entry whose `mcpServers` map key matches a catalog entry **of the same name**, marker or no marker | every entry whose map key is absent from the catalog |
| non-registry (no registry URL) — as documented | ordinary entries | entries carrying `"type": "registry"` |

Three consequences worth internalising, the first two of them the 2.20.1
observation:

- The match is on the **`mcpServers` map key**, and on nothing else — not the
  command, not a registry id, not the marker. `kirocrew-core` in your spec must
  be `kirocrew-core` in the registry file. An entry carrying no marker at all is
  admitted when its map key is in the catalog.
- **The catalog overrides a locally declared `command`.** An entry naming its own
  command still launches from the catalog's, so the local one is inert in a
  registry-mode session. A row that reads `Ignored (not in registry)` is a
  missing catalog entry, never a bad local command.
- `"type": "registry"` is **not a transport**. It declares "this entry is a
  pointer into the catalog", and only `env`, `headers` and `timeout` are carried
  over from your entry as overrides.

The marker is still worth stamping outbound, and Kiro Crew still stamps it. It
costs nothing, it is what the documented contract asks for, and it is what the
non-registry row above keys on. The observation is that registry mode admits
without it, not that it is safe to omit.

Governance also **fails closed**: if the client cannot reach the governance API,
MCP is disabled entirely rather than falling open.

## Fixing it — two halves, both required

### 1. Declare registry mode on the Kiro Crew side

```bash
kirocrew config set agent.mcp_registry_mode true
kirocrew restart
```

Kiro Crew then stamps `"type": "registry"` on the servers it manages, which is
what the documented registry filter asks for. On kiro-cli 2.20.1 the marker is
not what admits them — step 2 is. The marker's load-bearing effect is the
inverse: leave the setting `false` on a personal account, because outside
registry mode the filter inverts and the marked entries are the ones dropped.

The declaration is explicit rather than auto-detected on purpose: the client
fetches the toggle and the registry URL from `GetProfile` at startup and
**persists neither**, so nothing on disk distinguishes a governed account from an
ungoverned one.

Verify with `kirocrew doctor`, which grows an `MCP Governance (enterprise)`
section whenever the local identity came from Identity Center.

### 2. Have the administrator allow-list the servers

Kiro Crew needs four servers, and they must appear in the registry file under
**exactly** these names:

| Server | What is lost without it |
|---|---|
| `kirocrew-core` | `spawn_run`, `learn_add`, artifacts, knowledge, monitoring — the bulk of the product |
| `kirocrew-cron` | every scheduled job (`cron_add` and the whole cron surface) |
| `kirocrew-computer` | desktop automation (inert unless separately enabled, but still filtered) |
| `kirocrew-dashboard` | session control and chat-folder management: `session_create`, `session_send`, `session_stop`, `session_read_message`, `chat_folder_tree`, `chat_folder_create`, `chat_folder_move`, `chat_folder_move_session` |

`kirocrew-dashboard` is the opt-in per-agent set, so it needs allow-listing
whenever an agent references it — the `kirocrew-conductor` agent does. Without
it the goal conductor cannot create its folder, maintain its ledger, or dispatch
workers.

The registry file format is a subset of the MCP registry standard's server
schema. Each item in `servers` wraps its definition in a `server` object, so the
outer shape is `{"servers": [ { "server": { ... } } ]}`; an item that inlines
`name` and `packages` at the top level is schema-invalid. Each entry needs a
`packages` entry describing how to launch the server, and — because all four Kiro
Crew servers live behind one package — a `packageArguments` entry naming the
subcommand. For a `pypi` package the client derives
`uvx <identifier> <packageArguments>`, so an entry without the argument launches
`uvx kirocrew` with no subcommand, which prints CLI help instead of speaking MCP
and fails the handshake:

```json
{
  "servers": [
    {
      "server": {
        "name": "kirocrew-core",
        "description": "Kiro Crew orchestration: subagents, memory, artifacts, monitoring",
        "version": "0.6.0",
        "packages": [
          {
            "registryType": "pypi",
            "identifier": "kirocrew",
            "packageArguments": [{ "type": "positional", "value": "mcp-core" }],
            "transport": { "type": "stdio" }
          }
        ]
      }
    },
    {
      "server": {
        "name": "kirocrew-cron",
        "description": "Kiro Crew scheduled jobs",
        "version": "0.6.0",
        "packages": [
          {
            "registryType": "pypi",
            "identifier": "kirocrew",
            "packageArguments": [{ "type": "positional", "value": "mcp-cron" }],
            "transport": { "type": "stdio" }
          }
        ]
      }
    },
    {
      "server": {
        "name": "kirocrew-computer",
        "description": "Kiro Crew desktop automation (macOS, opt-in)",
        "version": "0.6.0",
        "packages": [
          {
            "registryType": "pypi",
            "identifier": "kirocrew",
            "packageArguments": [{ "type": "positional", "value": "mcp-computer" }],
            "transport": { "type": "stdio" }
          }
        ]
      }
    },
    {
      "server": {
        "name": "kirocrew-dashboard",
        "description": "Kiro Crew session control and chat folders",
        "version": "0.6.0",
        "packages": [
          {
            "registryType": "pypi",
            "identifier": "kirocrew",
            "packageArguments": [{ "type": "positional", "value": "mcp-dashboard" }],
            "transport": { "type": "stdio" }
          }
        ]
      }
    }
  ]
}
```

Set `version` to the Kiro Crew version your fleet runs. The published JSON Schema
constrains four fields, and each one rejects the file outright:

| Field | Constraint |
|---|---|
| `name` | matches `^[a-zA-Z0-9._-]+$` |
| `description` | maxLength 100 |
| `packages` | maxItems 1 |
| `version` | a concrete version; the ranges `^1.2.3`, `~1.2.3`, `>=1.2.3`, `1.x` and `1.*` are rejected |

## Known limitation: the registry launches the server, not your install

Kiro Crew's MCP servers are not standalone tools — they are the gateway's own
process, reached through subcommands (`kirocrew mcp-core`, `mcp-cron`,
`mcp-computer`, `mcp-dashboard`), and they share the gateway's data home and
version.

A registry-type entry hands the launch decision to the catalog: the client
resolves the package and, when a locally installed server's version differs from
the registry's, relaunches it at the registry's version. For a `pypi` entry that
means `uvx` resolving Kiro Crew into its own ephemeral environment — so the
process serving your MCP tools can be a *different* Kiro Crew from the gateway
serving your dashboard. Your `env` overrides (including `KIROCREW_HOME`) do flow
through, which keeps the data home aligned, but the code does not.

**`kirocrew` is not published on public PyPI.** Both `kirocrew` and `kiro-crew`
return HTTP 404 on the PyPI JSON API, so a plain `pypi` identifier entry resolves
to nothing today. The `--from` route in the next subsection is the only one that
works.

Keep the registry `version` in step with your fleet's installed version. If your
organisation pins Kiro Crew centrally, that pin now governs the MCP side too.

### Pointing the registry at a local working tree

A registry entry can launch a local checkout through the `Package`
`runtimeArguments` field, which the schema describes as the arguments passed to
the package's runtime command. For a `pypi` package the client derives
`uvx <runtimeArguments> <identifier> <packageArguments>`, so `runtimeArguments` of
`--from <path>` yields `uvx --from <path> kirocrew mcp-dashboard`. This is
verified working end to end on kiro-cli 2.20.1: the entry resolved and the
server's tools appeared in a session.

With `--from`, `identifier` MUST be the bare console-script name `kirocrew` and
MUST NOT be version-pinned. uvx treats the trailing argument as the command to
run from the given source, so `kirocrew==0.6.0` breaks the launch. This differs
from an ordinary `pypi` entry, where `identifier` is the package spec — contrast
it with the `awslabs.aws-api-mcp-server==1.3.31` style entry an administrator
writes for a published server.

```json
{
  "servers": [
    {
      "server": {
        "name": "kirocrew-dashboard",
        "description": "Kiro Crew session control and chat folders",
        "version": "0.6.0",
        "packages": [
          {
            "registryType": "pypi",
            "identifier": "kirocrew",
            "runtimeArguments": [
              { "type": "positional", "value": "--from" },
              { "type": "positional", "value": "/opt/kirocrew-src" }
            ],
            "packageArguments": [{ "type": "positional", "value": "mcp-dashboard" }],
            "transport": { "type": "stdio" }
          }
        ]
      }
    }
  ]
}
```

`uvx --from` builds a **wheel snapshot** into an ephemeral environment and caches
it. The gateway runs the editable install, so the registry-launched MCP server can
be a frozen older copy of the same tree. After a code change, clear the cache
before the registry-launched server picks it up:

```bash
uv cache clean kirocrew
```

Do not add an `environmentVariables` block to pin `KIROCREW_HOME`. The spawned
process inherits the environment, so the default resolution through `HOME` finds
the correct data home, and an explicit `KIROCREW_HOME` is inherited as well.
Hardcoding an absolute value reintroduces the machine-specific path the symlink
convention below exists to remove.

A fleet that wants a pinned shared source without publishing to PyPI can point
`--from` at `git+https://github.com/kirodotdev/KiroCrew@<sha>` instead. The
`runtimeArguments` mechanism itself is verified on kiro-cli 2.20.1; only the
local-path target was exercised, so the `git+https` target is untested here. It
also does not run a local tree.

### The symlink convention

A local absolute path in a registry served fleet-wide breaks every other
developer. Each developer creates the same fixed symlink to their own checkout,
so the registry file stays machine-independent and needs no variable
substitution:

```bash
sudo ln -sfn "$HOME/git/kiro-crew/KiroCrew" /opt/kirocrew-src
```

The registry then carries the literal `/opt/kirocrew-src`. uvx builds through the
symlink, verified on this host.

Substitution was not used because its support is unverified here. kiro-cli has a
`${VAR}` expander, and the registry format accepts `${VAR}` in `KeyValueInput`
values such as headers, but whether it expands inside a `PositionalArgument`
value is undocumented and unverified. The symlink removes the dependency on it
entirely.

## The other direction: registry servers an administrator installed

Everything above is **outbound** — Kiro Crew's own marker on Kiro Crew's own
servers. The **inbound** direction is a registry pointer someone else wrote.
Installing a server from the registry (from Kiro IDE or Kiro CLI) writes an entry
into `~/.kiro/settings/mcp.json` shaped like this:

```json
"atlassian": { "type": "registry", "disabled": false }
```

No `command`, no `url`, no version and no registry id. The `mcpServers` map key
is the entire resolution key, and the governed client resolves the transport from
the catalog.

**Kiro Crew carries every such pointer into `~/.kiro/agents/kirocrew.json`
unconditionally.** The entry is emitted under its original map key with the
marker retained and the local half of the entry the catalog cannot supply:
`disabled`, `env`, `headers`, `timeout`, and the OAuth hints. Kiro Crew
synthesizes no `command` and no `url`, because the catalog declares the transport
and, on 2.20.1, overrides a locally declared command anyway.

**`agent.mcp_registry_mode` does not gate inbound pointers.** That setting is
outbound-only: it controls the marker Kiro Crew stamps on `kirocrew-core`,
`kirocrew-cron`, `kirocrew-computer` and `kirocrew-dashboard`. A pointer is
carried whether the setting is `true`, `false` or absent, and no local setting
withholds one. The reason is
that the client's inbound access mode comes from `GetProfile` and is persisted
nowhere, so Kiro Crew has no local source for it and would be reporting its own
outbound preference in place of the client's inbound state.

**Kiro Crew cannot verify that a pointer resolves.** It never reads the catalog,
so the dashboard renders a pointer as **not verified** with an empty tool list
rather than as online or as an error. `kirocrew doctor` names the pointers under
its `MCP Governance (enterprise)` section — read from the emitted spec, which
carries every pointer the scan found — and points at the one command that can
answer the question:

```bash
PAGER=cat kiro-cli mcp list
```

A `✓` row is a resolved pointer. A row under `Ignored (not in registry)` is a
name the administrator has not allow-listed — and on an account with no registry
access at all, every pointer reads that way. Doctor lists enabled pointers only:
a pointer carrying `disabled: true` is emitted, but it is not carried for
mounting, so `kiro-cli mcp list` has nothing to say about it.

## Version floor

MCP registry governance requires Kiro CLI **1.23** or later (Kiro IDE 0.11.28).
Enforcement in the V2 TUI arrived in **2.2.2**, and **2.6.0** made personal
`mcp.json` servers load alongside registry-managed ones. Kiro Crew's servers
live in an agent spec (`~/.kiro/agents/kirocrew.json`), not in personal
`mcp.json`, so that last change does not exempt them.

**2.20.1** is the build the map-key admission above was observed on, and the
build on which pass-through of an inbound pointer was confirmed: a
`"type": "registry"` entry inlined in an agent spec's own `mcpServers` map
resolves from the catalog with `includeMcpJson: false`, over both a `remotes[]`
transport and a `packages[]` install. Those are observations of one build rather
than a documented contract. Re-run `PAGER=cat kiro-cli mcp list` after a kiro-cli
upgrade rather than assuming either holds.

## Related

- [../architecture/mcp.md](../architecture/mcp.md) — how Kiro Crew composes the
  agent spec's `mcpServers` map and which files it owns.
- [../../src/kiro_crew/docs/troubleshooting.md](../../src/kiro_crew/docs/troubleshooting.md)
  — the user-facing "MCP tools not working" checklist.
- Kiro's own documentation: `https://kiro.dev/docs/enterprise/governance/mcp/`
  (administrator setup) and `https://kiro.dev/docs/mcp/registry/` (registry mode
  and registry-type overrides).
