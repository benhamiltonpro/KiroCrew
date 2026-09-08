# Implementation Plan

Derived strictly from the corrected `bugfix.md` and `design.md`. The design's
experiment settled the open question affirmatively, so the implementation is
**pass-through**: D1, D2, D3, D6, D7, D9, D10. No catalog fetcher, no
operator-declared registry URL, no enterprise-auth surface (D8).

**D4 and D5 are withdrawn.** The dangling-reference removal guard and the
ungoverned-host ref gate are out of scope. Requirement 2.8 closes through
pass-through alone: every reference this defect strands names a server the defect
dropped, so restoring the pointer to `mcpServers` restores the reference. The
latent defect that analysis surfaced lives in `design.md` § "Out of scope" and
belongs to a separate issue. **No task in this plan removes a `@ref`, an
`allowedTools` grant, or a `permissions.rules` entry.** Task 5.2 asserts that
absence as a real test, so a removal appearing anywhere means the out-of-scope
work has leaked in.

`atlassian-crew` is not used as a case anywhere in this plan. It was a developer
diagnostic entry, not a registry server and not a product of the defect.

## How this plan is sequenced

**Implement locally, test locally, then verify on the live host — in that order.**
There is no branch work, no push, and no pull request in this task list. Groups 1
through 5 are entirely local. Group 6 is live-host verification of the locally
verified build. Opening a PR is a separate step the developer takes afterwards;
see § "After local verification" at the end of this file.

Per `AGENTS.md`, do not commit unless the developer asks. Do not touch
`CHANGELOG.md` — the changelog is written only at a version bump.

Groups are organized by **what they change**, not by pull request.

---

## Group 1: Exploration and preservation baselines (before any fix)

- [x] 1.1 Write the bug condition exploration test
  - **Property 1: Bug Condition** - A registry pointer is dropped instead of carried
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when
    it passes after implementation (task 5.1)
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: the bug is deterministic, so scope the property to the
    concrete failing pointer shapes recorded in design § Examples rather than
    generating arbitrary specs: `atlassian` (`{"type": "registry", "disabled": false}`,
    catalog declares a remote), `aws-api` (`{"type": "registry"}`, catalog declares a
    package), `playwright` (`env` override), `microsoft-teams` (`oauth.clientId` +
    `oauth.redirectUri` overrides)
  - Create a **new** suite `test/test_mcp_registry_pointer.py`; write the `mcp.json`
    fixture under `tmp_path` with `KIROCREW_HOME` pinned by the rootdir conftest, and
    invoke no real `kiro-cli`
  - Assert, for every input satisfying `isBugCondition` (design § Bug Condition): the
    map key is present in the rebuilt `mcpServers`; the entry carries
    `type == "registry"`; every local override it declared is present; no `command` or
    `url` was synthesized; and no surface reports the string `no command`
  - Assert the discovery-level and probe-level halves too, so the counterexamples
    localize the cause: `_server_from_spec` distinguishes a pointer from a malformed
    entry, and `probe_server` does not return `status == "error"` /
    `error == "no command"` for a pointer
  - **Fold the dangling-reference observation into this same property.** The corrected
    design closes 2.8 through pass-through, so this is not a separate defect and gets
    no separate property. Seed a spec with `includeMcpJson: false`, `@atlassian` in
    `tools` and `allowedTools`, and `atlassian/*` in `permissions.rules`, with
    `atlassian` absent from `mcpServers`. Assert that after the rebuild `atlassian` is
    **declared** in `mcpServers` and all three references are therefore legitimate.
    On unfixed code this fails because the pointer is dropped and the shared sync's
    `elif alias in valid_servers` add branch declines
  - Run the test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS - the pointer is absent from `mcpServers`,
    `_server_from_spec` yields `command == ""` and `url == ""` with no pointer
    identity, `probe_server` reports `no command`, and the seeded refs name a server
    the emitted spec does not declare
  - Document the counterexamples observed (which assertion fired first, the actual
    emitted `mcpServers` keys) to confirm design § Hypothesized Root Cause: `type` is
    never read at `mcp_discovery.py:913`
  - **Acceptance**: the new suite exists, is run, fails on unfixed code, and the
    failures are recorded. `test/test_agent.py` and
    `test/test_mcp_registry_governance.py` are **not** modified
  - _Requirements: 1.1, 1.2, 1.3, 1.7, 1.8, 2.1, 2.2, 2.3, 2.4, 2.7, 2.8_

- [x] 1.2 Write preservation property tests (BEFORE implementing the fix)
  - **Property 2: Preservation** - Every non-pointer entry is untouched
  - **IMPORTANT**: Follow observation-first methodology - observe the unfixed
    behavior, record it, then assert the recorded behavior
  - Observe on UNFIXED code and record the actual output for each: an ordinary
    `command` entry (candidate priority `kirocrew` > `kiro-global` > provider globals,
    argv pairing, `env`/`PATH` normalization); a `url` entry
    (`kiro_oauth_wire_entry` translation, POST probing); an unmarked command-less
    entry (`no command`, dropped); a managed `kirocrew-*` entry with and without
    `agent.mcp_registry_mode` declared
  - Write property-based tests with `hypothesis` generating arbitrary `mcp.json` maps
    that mix command entries, `url` entries, unmarked command-less entries and managed
    names, and assert the emitted non-pointer entries equal the recorded unfixed output
  - Add the strongest single preservation assertion: a pointer-free fixture home emits
    a **byte-for-byte identical** agent spec before and after the fix, covering key
    ordering and presence, not only values (3.10)
  - Add the disabled-entry assertion: a `disabled: true` entry is still refused inside
    `probe_server` (3.6)
  - Record the **ungoverned baseline** (design exploratory case 6): with
    `agent.mcp_registry_mode` undeclared, capture exactly what the unfixed code emits
    for a pointer and for any `tools`/`allowedTools` refs, so requirement 3.11's
    no-worse-off property is **measured rather than asserted** when task 5.2 checks it
  - Run the tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS - this is the baseline to preserve
  - **Acceptance**: tests live in `test/test_mcp_registry_pointer.py` (or a sibling new
    file), pass on unfixed code, the ungoverned baseline is recorded, and neither
    `test/test_agent.py` nor `test/test_mcp_registry_governance.py` is modified
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.9, 3.10, 3.11_

---

## Group 2: Carry the pointer through discovery and emit it (D1, D2, D9)

- [x] 2.1 Carry the pointer through discovery (D1, D9)
  - Add `is_registry_pointer: bool = False` to `McpServerInfo` in
    `src/kiro_crew/mcp_discovery.py`, set from
    `spec.get("type") == _MCP_REGISTRY_TYPE` in `_server_from_spec` (line 913)
  - Store a **boolean, not the raw `type` string**: `type` also legitimately carries
    `"stdio"`, which
    `test_mcp_registry_governance.py::test_refresh_preserves_a_user_transport_hint`
    pins as the user's own hint, and no consumer asks for anything but "is this a
    pointer"
  - Relocate the existing `_MCP_REGISTRY_TYPE` constant (currently `agent.py:598`)
    into `mcp_discovery`, keeping the name unchanged, and import it in `agent`.
    `mcp_discovery` is the lower-level module and `agent` already imports from it.
    **No new `"registry"` string literal is introduced anywhere** (D9, `AGENTS.md`
    constants rule)
  - Leave `is_remote` unchanged: a pointer has no `url`, so it is already `False`, and
    widening it would route pointers into `_probe_remote`
  - **Acceptance**: `_server_from_spec` sets the flag for a marked entry and clears it
    for `type: "stdio"`, absent `type`, and a malformed non-string `type`; a search
    for the `"registry"` literal under `src/kiro_crew/` shows one definition site
  - _Bug_Condition: isBugCondition(input) from design - input.spec.type = "registry"_
  - _Requirements: 1.1, 2.1_

- [x] 2.2 Emit the pointer instead of dropping it, and carry its overrides (D2, D9)
  - In `src/kiro_crew/agent.py::rebuild_agent_config`, add a pointer branch **before**
    the candidate/resolution loop at line 3628 and **beside** the existing
    `if spec.get("url")` branch at line 3552, so the "needs no command resolution"
    shape stays visible
  - The branch fires when `spec.get("type") == _MCP_REGISTRY_TYPE` **and** the name is
    not in `_MANAGED_MCP_SERVERS`, so a managed entry carrying Kiro Crew's own
    outbound marker continues down the existing path with its resolved `command` (3.5)
  - Emit the pointer verbatim plus nothing: map key, `type`, and the local overrides.
    `env` goes through the existing `emit_env` for the same `PATH` normalization;
    `oauth.clientId` and `oauth.redirectUri` go through the existing
    `kiro_oauth_wire_entry` for the same internal-to-wire rename the `url` branch
    performs; `headers` and `timeout` are copied as-is
  - Synthesize **no** `command` and **no** `url`. The experiment shows kiro-cli
    overrides a declared command from the catalog anyway, so writing one would be
    inert at best and misleading in the spec at worst
  - Leave the `not had_any_command` drop at **line 3671 exactly as it is**. The new
    branch `continue`s first, so that line is merely unreachable for a marked pointer.
    This is what keeps
    `test/test_agent.py::test_server_without_command_is_dropped` green unmodified
  - Name the local-override key tuple as a module constant in `agent` (D9)
  - **Same-commit documentation** (`AGENTS.md` read-before-you-touch):
    `docs/architecture/mcp.md` § "Merge order in rebuild_agent_config()" gains the
    pointer branch and its position relative to the `url` branch, and notes that a
    carried pointer joins `valid_servers` and so becomes eligible for the shared-ref
    sync's add branch; § "Discovery and probing" gains `is_registry_pointer`. **No
    ref-removal pass is documented, because none is added**
  - **Acceptance**: a marked pointer is emitted under its original map key with the
    marker retained; each override carries through individually and in combination,
    with the OAuth hints asserted in **wire** spelling; no `command`/`url` is
    synthesized; a managed `kirocrew-*` entry with the marker keeps its resolved
    `command` and `args`; `scripts/docs-lint.sh` passes
  - _Bug_Condition: isBugCondition(input) from design_
  - _Expected_Behavior: expectedBehavior(result) from design § Fix Checking - key
    present in mcpServers, type == "registry", every declared override present, no
    command or url synthesized, no surface reports "no command"_
  - _Preservation: Preservation Requirements from design (3.1, 3.2, 3.3, 3.4, 3.5, 3.10)_
  - _Requirements: 1.3, 1.7, 1.8, 2.1, 2.2, 2.3, 2.7, 2.8, 3.3, 3.5_

---

## Group 3: Probe status and dashboard rendering (D3, D6)

- [x] 3.1 Add the `registry_pointer` probe status and render it not-verifiable
  - In `mcp_discovery.py::probe_server` (line 1726), add a pointer branch **ahead of**
    the `not server.command` check and **after** the existing `server.disabled`
    refusal, so a disabled pointer still returns `disabled` and the consent gate is
    not bypassed (3.6). The branch returns without spawning anything
  - **The status is `registry_pointer`, rendered as not-verifiable.** Kiro Crew cannot
    resolve the pointer - that is the premise of pass-through - so it must not claim
    `ok`, and 2.4/2.5 forbid `no command`. Reuse the existing **`needs_auth`
    precedent** (the status comment on the dataclass field, and `McpTab.tsx`'s
    not-verified badge with a hover hint) rather than inventing a second vocabulary
    for "cannot tell"
  - Extend the dataclass status comment, `McpServerInfo.to_dict`, `mcpStatusLabel`,
    `mcpStatusHint` and `connectionStateFor`
  - `tools` stays empty and `probe_mode` is neither `handshake` nor `declared`: no
    handshake happened and Kiro Crew has no declaration to fall back on. Do not
    fabricate a tool list - an empty list beside a not-verified badge is the honest
    rendering
  - Add the badge label and hover hint to **every** locale catalog under
    `website/src/i18n/locales/` per the i18n gate; hardcode no user-facing English
    string
  - **Cause taxonomy (D6) - two states, not three.** With D5 withdrawn there is no
    withholding path, so the taxonomy is exactly: *carried to the client, not
    verifiable here* (this new status) and *pointer disabled* (the existing `disabled`
    status, unchanged). A pointer is never withheld. Catalog-side causes - name absent
    from the catalog, an entry declaring neither a usable remote nor a translatable
    package - are **not** Kiro Crew's to report, because Kiro Crew never reads the
    catalog; `kiro-cli mcp list` reports them as `⚠ Ignored (not in registry)`
  - Confirm `redact_mcp_headers` / `redact_mcp_error` cover every new log line and API
    payload, so a pointer's OAuth `clientId` or a header value never reaches a log or
    the dashboard (3.8)
  - **Same-commit documentation**: `docs/architecture/mcp.md` § "Discovery and
    probing" gains the `registry_pointer` status
  - **Acceptance**: `probe_server` returns `registry_pointer` for a pointer and
    `disabled` - not `registry_pointer` - for a disabled pointer; no surface produces
    the string `no command` for a pointer; the dashboard payload renders the
    not-verified state with its hint and an empty tool list, asserted through existing
    `McpTab` patterns rather than a new rendering path; the i18n gate passes; a
    redaction test proves the `clientId` and header values stay out of logs and
    payloads; exactly two pointer states are reachable
  - _Bug_Condition: isBugCondition(input) from design_
  - _Expected_Behavior: no surface reports "no command"; the status names what Kiro
    Crew can honestly distinguish (design D3, D6)_
  - _Preservation: 3.6, 3.8_
  - _Requirements: 1.2, 1.4, 1.5, 2.4, 2.5, 3.6, 3.8_

- [x] 3.2 Make the readiness preflight pointer-aware (live-verification finding)
  - **Found by live verification, not by the design.** On the reporter's host the built app gated the whole dashboard behind an `Agent specs rejected` card reading "The MCP server 'aio-tests' names neither a command to run nor a url to reach". The design enumerated `probe_server`, `McpTab` and `cli_doctor` as the surfaces that must not blame a pointer's absent command, and **missed this fourth one**
  - The gate is `kiro_prerequisite.py::_unlaunchable_mcp_servers` (line 1874). It requires every `mcpServers` entry to carry a non-empty `command` or `url`; a pointer carries neither by design, so every carried pointer fails it, the spec lands in `rejected_agent_specs`, and `ready` goes false with `repair_required` true
  - **This is requirement 2.4/2.5 violated in its most severe form.** The other three surfaces mislabel a row; this one makes the product unusable. It is also **unclearable**: `Check again` re-probes and fails identically, and `kirocrew setup --agent-only --clean` rebuilds the spec, re-emits the pointers and fails again
  - **The premise is NOT invalidated.** On a structural failure `_probe_spec_acceptance` appends and `continue`s, skipping the `kiro-cli agent validate` spawn, so kiro-cli never refused the spec. Task 6.2's stop-and-report condition has not triggered
  - Add the marker as the **third launchable shape** in that predicate: the catalog supplies a pointer's transport, so `_MCP_REGISTRY_TYPE` is launchable evidence exactly as `command` and `url` are. Import the constant from `mcp_discovery`; introduce no new `"registry"` literal (D9). The function's own docstring already carries the argument — it warns that treating a `url`-only entry as unlaunchable would "force a healthy install into an unclearable readiness gate", which is precisely what a pointer now does
  - Keep the check **narrow**: an entry that is not an object, or that names no transport **and** carries no marker, must still be reported. The gap this predicate closed is real and must stay closed
  - **Secondary, pre-existing**: the card's copy attributes a Kiro-Crew-side verdict to kiro-cli ("Kiro CLI refuses them"). That is wrong for every structural rejection, not only pointers, and it sends the reader to the wrong component. Correct the attribution for the structural path
  - **Same-commit documentation**: `docs/architecture/mcp.md` records this as the fourth pointer-aware surface, so the next reader finds all four in one place
  - **Acceptance**: a spec carrying the reporter's ten pointers passes the preflight; a genuinely transportless unmarked entry and a non-object entry are still reported; `kiro_prerequisite`'s own suite stays green; the dashboard reaches the normal UI on the live host
  - _Expected_Behavior: no surface reports "no command" for a pointer (design D3) — extended to the readiness preflight_
  - _Requirements: 1.2, 1.4, 2.4, 2.5_

---

## Group 4: Doctor finding and documentation corrections (D7, D10)

- [x] 4.1 Add the doctor pointer finding (D7)
  - Grow a pointer subsection inside `kirocrew doctor`'s existing
    `MCP Governance (enterprise)` section, naming the pointer server names found in
    the scanned `mcp.json` sources and what the operator must do
  - **One state, not two.** With the ref gate withdrawn there is a single thing to
    report: these pointers are carried through to kiro-cli, Kiro Crew cannot verify
    that they resolve, and `PAGER=cat kiro-cli mcp list` is the command that can - a
    `⚠ Ignored (not in registry)` row there is an allow-listing gap for the
    administrator, and on a host with no registry access at all every pointer reads
    that way
  - **The finding MUST NOT vary with `agent.mcp_registry_mode`.** That setting is
    outbound-only. The governed client's inbound mode comes from `GetProfile` and is
    persisted nowhere, so Kiro Crew has no local source for it, and keying the finding
    on `mcp_registry_mode` would report Kiro Crew's own outbound setting as though it
    were the client's inbound state. Doctor does not attempt to classify which case
    applies
  - It must **not** render as verified success, matching the section's existing
    `cannot verify` discipline that
    `test_mcp_registry_governance.py::test_declared_and_marked_reports_the_names_to_allow_list`
    pins - that test stays green unmodified
  - **Acceptance**: `cli_doctor` reports the pointer names it found; the output does
    not render as verified success; the output is **identical with
    `mcp_registry_mode` declared and undeclared** (a test asserts the two runs match);
    the existing governance-section tests stay green with
    `test/test_mcp_registry_governance.py` unmodified
  - _Requirements: 1.6, 2.6_

- [x] 4.2 Correct both documents in the same commit (D10)
  - **`docs/architecture/mcp.md`** - confirm the two edits made alongside their code
    in tasks 2.2 and 3.1 are present and consistent: the pointer branch and its
    position relative to the `url` branch in § "Merge order in
    rebuild_agent_config()", the note that a carried pointer joins `valid_servers` and
    becomes eligible for the shared-ref sync's add branch, and `is_registry_pointer`
    plus the `registry_pointer` status in § "Discovery and probing". **Document no
    ref-removal pass**, because none is added
  - **`docs/guides/enterprise-mcp-governance.md`** - correct its "What governance
    actually does" table. It currently claims a **symmetric** filter keyed on the
    `"type": "registry"` marker. Design variant 3 shows that in registry access mode
    kiro-cli admits on the `mcpServers` **map key alone**: an unmarked entry was
    admitted, and a locally declared `command` was **overridden** by the catalog's
  - **Attribute the correction to observed behavior, not to a contract**: state
    explicitly that this is behavior observed on **kiro-cli 2.20.1**, not a documented
    contract, and add the 2.20.1 observation to the version floor note. The marker is
    still carried outbound because it costs nothing and is what the documented
    contract asks for
  - Add the page's missing **inbound** section (it currently covers only the outbound
    marker): registry-installed pointers are carried through to the client
    **unconditionally**, `mcp_registry_mode` is outbound-only and **does not gate
    them**, and `PAGER=cat kiro-cli mcp list` is the verification command
  - **Acceptance**: the corrected table matches the variant-3 observation; the 2.20.1
    attribution is explicit; the inbound section states that `mcp_registry_mode` does
    not gate inbound pointers; `scripts/docs-lint.sh` passes; every index that points
    at a changed doc is still accurate
  - _Requirements: 1.4, 2.4, 2.6_

---

## Group 5: Local test verification

- [x] 5.1 Verify the exploration and preservation tests against the fix
  - **Property 1: Expected Behavior** - A registry pointer survives to the governed client
  - **IMPORTANT**: Re-run the SAME test from task 1.1 - do NOT write a new test. It
    encodes the expected behavior, so its passing is what confirms the bug is fixed
  - **EXPECTED OUTCOME**: the task 1.1 test PASSES, including its folded
    dangling-reference half: `atlassian` is now declared in `mcpServers`, so
    `@atlassian` in `tools`/`allowedTools` and `atlassian/*` in `permissions.rules`
    are legitimate
  - **Property 2: Preservation** - Every non-pointer entry is untouched
  - **IMPORTANT**: Re-run the SAME tests from task 1.2 - do NOT write new tests
  - **EXPECTED OUTCOME**: the task 1.2 tests PASS, including the byte-for-byte
    pointer-free spec assertion
  - Run `python -m pytest test/test_agent.py::test_server_without_command_is_dropped
    test/test_mcp_registry_governance.py` and confirm green
  - **Acceptance (explicit)**:
    `test/test_agent.py::test_server_without_command_is_dropped` and **every** test in
    `test/test_mcp_registry_governance.py` are green **with both files unmodified**.
    `git diff --stat` must show neither path. **Needing to edit either file means the
    marker scoping in D2 is wrong** - fix the scoping, do not adjust the test
  - _Requirements: Expected Behavior Properties 2.1, 2.2, 2.3, 2.4, 2.7, 2.8;
    Preservation 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.10_

- [x] 5.2 Pin the descope: nothing is removed, and the ungoverned inversion is accepted
  - **Property 1: Expected Behavior** - the descope boundary is a test, not a comment
  - **The negative assertion (D4/D5 withdrawn).** Assert that **no reference is
    removed by this fix**: a `@server` ref, an `allowedTools` grant and a
    `permissions.rules` match for a server that happens to be absent from
    `mcpServers` are left **exactly as the unfixed code leaves them**. If a removal
    shows up here, the out-of-scope work has leaked in and must come back out
  - **The ungoverned-host inversion test (3.11).** This stays a **test, not a manual
    check**, precisely because the developer's host is governed and the design's
    experiment could not observe non-registry mode at all
  - With `agent.mcp_registry_mode` **undeclared**: assert the pointer entry is emitted
    anyway - the inbound path never reads that setting - and assert the shared sync
    **adds** its `@ref` because the entry is now in `valid_servers`
  - **Pin the stranded `@ref` as the ACCEPTED outcome.** Do **not** assert the ref is
    absent. On an ungoverned host the client's inverted filter drops the marked entry
    and the newly added reference is stranded; kiro-cli discards it silently, the tool
    is no more available than today, and an auto-approve grant for an unmounted server
    grants nothing. Assert instead that **no server is lost** relative to today's drop.
    This is the test that would catch a silent reintroduction of the withdrawn D5 gate
  - Assert the emitted `mcpServers` is **byte-identical** between the declared-mode and
    undeclared-mode runs, since nothing on the inbound path reads that setting
  - **Acceptance**: the no-removal assertion passes; the inversion test passes with the
    ref pinned as present-and-accepted rather than absent; the two-mode `mcpServers`
    byte-identity holds; a comment in the test names the withdrawn D5 gate as the thing
    it guards against
  - _Expected_Behavior: no removal mechanism exists; a carried pointer's ref is emitted
    regardless of mode (design § Out of scope → "The ungoverned host, re-examined")_
  - _Preservation: 3.11 - no server lost on an ungoverned host_
  - _Requirements: 2.8, 3.11_

- [x] 5.3 Add the property-based and integration coverage
  - **Property-based**: over arbitrary `mcp.json` maps mixing command entries, `url`
    entries, unmarked command-less entries, managed names and marked pointers, assert
    the emitted **non-pointer** entries are identical to the unfixed code's output -
    the preservation property over the whole input domain
  - **Property-based**: over arbitrary override combinations on a pointer, assert every
    declared override appears on the emitted entry and **nothing else is added**
  - **Property-based**: over arbitrary `tools`/`allowedTools`/`permissions.rules` sets
    against arbitrary pointer maps, assert the invariant 2.8 actually rests on here -
    **every pointer in the input is declared in the emitted `mcpServers`**, so no
    reference naming a *pointer* is dangling. Do **not** assert the stronger
    unconditional form (no `@server` ref and no `server/*` rule names any absent
    server); no removal pass exists to make it true and it belongs to the out-of-scope
    work
  - **Integration**: a full `install_agent` run on a fixture home holding the ten
    pointer shapes plus `fetch` - all eleven reach `mcpServers`, the ten pointers keep
    their markers and overrides, and `fetch` keeps its resolved command. This is the
    disjoint-sets defect closed end to end
  - **Integration**: the same run with `mcp_registry_mode` undeclared - entries present
    identically, refs added identically, spec internally consistent
  - **Integration**: the reporter's exact `kirocrew.json` shape -
    `includeMcpJson: false`, `@atlassian` in `tools` and `allowedTools`, `atlassian/*`
    in `permissions.rules`, `atlassian` absent from `mcpServers` - ends with
    `atlassian` declared and all three references legitimate, **with nothing removed**
  - **Integration**: idempotence - two consecutive rebuilds produce an identical spec,
    so the sync settles rather than oscillating once a pointer joins `valid_servers`
  - **Acceptance**: all of the above pass; no test touches the operator's machine
    (`KIROCREW_HOME` pinned by the rootdir conftest, every fixture under `tmp_path`, no
    real `kiro-cli` invoked); child processes, if any, run with `cwd=` under `tmp_path`
  - _Requirements: 2.1, 2.2, 2.3, 2.7, 2.8, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.9, 3.10, 3.11_

- [ ] 5.4 Run the full local gate
  - Format **only the files this work touched**:
    `black --target-version py310 <touched .py files>`. **Do not run a bare
    `black src/kiro_crew test`** - 1,420 files are not black-clean and it would
    reformat ~95,800 lines on top of the change
  - Run the gate exactly as `AGENTS.md` specifies, with `--platform linux` because
    this is macOS (typeshed guards `os.listxattr`/`getxattr`/`setxattr` behind
    `sys.platform == "linux"`, so a bare local run reports 4 unrelated errors **and
    misses Linux-only errors CI fails on**):
    - `python3 scripts/check_black_formatting.py && python3 scripts/check_subprocess_encoding.py && isort src/kiro_crew test`
    - `flake8 src/kiro_crew test && mypy --platform linux src/kiro_crew`
    - `python -m pytest`
  - Run `scripts/scrub-lint.sh`, `scripts/docs-lint.sh`,
    `HARNESS_BASE_REF=origin/main python3 scripts/check_harness_parity.py`, and
    `BRAND_BASE_REF=origin/main python3 scripts/check_brand_name.py`
  - Run the **frontend gate**, because group 3 touches `McpTab` and the locale
    catalogs: `cd website && npm run build && npm run test`
  - Confirm **`CHANGELOG.md` is untouched**
  - **Acceptance**: every command above exits 0; `git status` shows no change to
    `CHANGELOG.md`, `test/test_agent.py`, or `test/test_mcp_registry_governance.py`
  - _Requirements: repository gate compliance (`AGENTS.md`)_

---

## Group 6: Live-host verification — **mutates live configuration**

This group is the only one that writes outside the repository. It runs **after** the
local gate in group 5 is green.

- [x] 6.1 Back up the live configuration before anything is written
  - **This group mutates the developer's LIVE configuration.** The design's experiment
    deliberately avoided writing to `~/.kiro/agents/kirocrew.json` and
    `~/.kiro/settings/mcp.json`; verification **cannot**, because the whole point is
    that Kiro Crew regenerates that spec
  - Copy `~/.kiro/agents/kirocrew.json` and `~/.kiro/settings/mcp.json` to a
    timestamped location **outside the repository** (so nothing can be committed) and
    record the **SHA-256** of each
  - Record the SHA-256 of every file in `~/.kiro/agents/`, as the design's experiment
    did, so a restore can be proven byte-identical
  - **Write down the exact restore command before proceeding.** Do not rely on
    reconstructing it later
  - **Acceptance**: both backups exist outside the repo, both digests are recorded, the
    per-file digests for `~/.kiro/agents/` are recorded, and the restore command is
    written down and known-good. **No backup file, and no content copied from either
    live file, is added to the repository or to this spec**
  - _Requirements: verification precondition for 2.2, 2.5_

- [ ] 6.2 Reproduce the before/after on the live governed host, then restore
  - **Before**: on a build without this fix, capture `PAGER=cat kiro-cli mcp list` and
    the emitted `mcpServers`, `tools`, `allowedTools` and `permissions.rules` from the
    regenerated `~/.kiro/agents/kirocrew.json`. Expect the design's disjoint sets: ten
    pointers `✓` under kiro-cli's own resolution but **absent** from Kiro Crew's
    emitted map, and `fetch` present in the emitted map but
    `⚠ Ignored (not in registry)`
  - **After**: install the locally verified build, let Kiro Crew regenerate the spec,
    and capture `PAGER=cat kiro-cli mcp list` again. Expect the ten pointers present in
    the emitted `mcpServers` with their markers and overrides, and `fetch` still
    carrying its resolved command
  - **Close the handshake gap.** `PAGER=cat kiro-cli mcp list` proves **resolution, not
    a completed handshake** - it reports what kiro-cli will mount and does not prove the
    remote answers. So additionally **start a real Kiro Crew session**, confirm an
    **`atlassian`-namespaced tool actually mounts** in the session's available tools,
    then **invoke one read-only Atlassian tool** and confirm it returns
  - **If tools do not mount despite a `✓` row, that is a finding against the design's
    pass-through conclusion. Record it and STOP.** Do not paper over it and do not
    resolve it unilaterally - it invalidates the premise the whole fix rests on
  - **Restore**: restore both live files from the task 6.1 backups and verify every
    recorded SHA-256 digest matches, proving byte-identity
  - **Redaction on captured evidence**: redact everything requirement 3.8 covers - **no
    OAuth `clientId`, no header value, no registry URL, no token** in any captured
    output, note, or transcript
  - **Acceptance**: before and after captures both exist; the after state shows the ten
    pointers emitted with markers and overrides; a real session mounts at least one
    `atlassian`-namespaced tool and a read-only call succeeds; both live files are
    restored **byte-identical** against the recorded digests; the recorded evidence
    contains no credential, header value, or registry URL
  - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.7, 2.8, 3.8_

---

## Checkpoint

- [ ] 7.1 Checkpoint - ensure everything is green and locally verified
  - `python -m pytest` is fully green
  - `cd website && npm run build && npm run test` is green
  - `test/test_agent.py::test_server_without_command_is_dropped` and every test in
    `test/test_mcp_registry_governance.py` are green with **both files unmodified**
  - The no-removal assertion (task 5.2) passes, so the D4/D5 descope held
  - The live host was verified and restored byte-identical
  - `CHANGELOG.md` is untouched
  - Ask the developer if questions arise; do **not** resolve a design-level finding
    from task 6.2 unilaterally

---

## After local verification

Not a task. Opening a pull request against the fork is a separate step the developer
takes when they choose, after the work above is locally verified. Two things to carry
into it: the upstream issue to reference is **kirodotdev/KiroCrew#3308**, and
`origin`'s URL currently carries an **embedded credential** that should be scrubbed
with `git remote set-url` before any push.

---

## Notes

- **No branch, push, or PR work in this plan.** Groups 1-5 are local. Group 6 verifies
  on the live host. Nothing here creates a branch, pushes, opens a pull request, or
  requests a token.

- **Do not commit unless asked.** `AGENTS.md` is explicit: no proactive `git commit`,
  and being asked to commit is not permission to push.

- **Property numbering.** Property 1 (Bug Condition / Expected Behavior) is the
  pass-through defect, and the dangling-reference observation is **folded into it**
  because the corrected design closes 2.8 through pass-through. Property 2
  (Preservation) is the non-pointer regression surface. **There is no Property 3** -
  the withdrawn D4/D5 work no longer has a defect property of its own.

- **D4 and D5 are withdrawn, and task 5.2 is what keeps them out.** No task removes a
  `@ref`, an `allowedTools` grant, or a `permissions.rules` entry. The negative
  assertion is a real test, not a comment, so a reintroduced removal fails the suite.

- **The stranded ungoverned `@ref` is an accepted outcome, not a bug.** After D2 the
  pointer joins `valid_servers`, so the shared sync's add branch fires and appends
  `@alias`. On an ungoverned host the inverted filter drops the marked entry and that
  ref is stranded. kiro-cli discards it silently, no tool becomes available, and a
  grant for an unmounted server grants nothing. Task 5.2 pins it as present-and-accepted
  rather than asserting it is absent.

- **`atlassian-crew` is not a case anywhere.** It was a developer diagnostic entry, not
  a registry server and not a product of the defect. It appears in no fixture.

- **Tests never touch the operator's machine.** `KIROCREW_HOME` is pinned per test by
  the rootdir conftest, every spec fixture is written under `tmp_path`, and nothing in
  the suite invokes the real `kiro-cli`. Live-host work is confined to group 6, is
  explicitly labelled as mutating, and backs up with recorded digests before it writes.

- **The two suites that must stay green unmodified** are
  `test/test_agent.py::test_server_without_command_is_dropped` (its fixture carries no
  marker, so the new branch does not fire and it reaches the unchanged `agent.py:3671`
  drop) and the entirety of `test/test_mcp_registry_governance.py` (D2's
  `_MANAGED_MCP_SERVERS` exclusion keeps managed entries off the new path;
  `test_refresh_preserves_a_user_transport_hint` is the specific reason D1 stores a
  boolean rather than the raw `type` string). **Needing to edit either file means the
  marker scoping is wrong** - fix the scoping, not the test.

- **macOS mypy parity.** `mypy --platform linux src/kiro_crew` is the parity
  invocation. A bare local run reports 4 errors in untouched files and misses
  Linux-only errors CI fails on.

- **`black` scope.** Format only the files a task touched, with
  `black --target-version py310 <files>`. A bare `black src/kiro_crew test` reformats
  ~95,800 lines across the 1,420 files in `.github/black-baseline.txt`.

- **Documentation travels with its code.** `docs/architecture/mcp.md` is edited in the
  same commit as tasks 2.2 and 3.1; `docs/guides/enterprise-mcp-governance.md` in task
  4.2. Both are in the `AGENTS.md` read-before-you-touch table.

---

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["2.2"] },
    { "id": 3, "tasks": ["3.1", "3.2"] },
    { "id": 4, "tasks": ["4.1", "4.2"] },
    { "id": 5, "tasks": ["5.1"] },
    { "id": 6, "tasks": ["5.2", "5.3"] },
    { "id": 7, "tasks": ["5.4"] },
    { "id": 8, "tasks": ["6.1"] },
    { "id": 9, "tasks": ["6.2"] },
    { "id": 10, "tasks": ["7.1"] }
  ]
}
```
