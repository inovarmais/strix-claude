# Cross-scan memory for incremental re-scanning

Status: approved for planning
Date: 2026-08-31

## Motivation

Today every `strix` invocation is a blank slate. `strix_runs/<run_name>/`
(`strix/core/paths.py`) is namespaced by a run name that always ends in a
random suffix (`generate_run_name()`, `strix/interface/utils.py:519`), so a
second scan of the exact same application has no way to find, and never reads,
anything from the first: not the vulnerabilities it found, not the endpoints
it mapped, not the threat model it built. `--resume <run_name>` only continues
the *same* run directory by name — it is not a target-based lookup.

The one piece of code that comes close to a stable "target identity"
(`_target_identity()` in `strix/tools/threat_model/tools.py:139`) says so
directly in its own docstring: it does not outlive the scan it was built for.

We want repeat scans of the same application — especially "scan again after a
code change" — to reuse what a prior scan already learned: the attack
surface (endpoints/assets), the threat model, and the status of previously
found vulnerabilities (still open / fixed / regressed), and to let a local-
checkout re-scan automatically focus on what changed since the last scan
instead of re-discovering the whole app from scratch.

## Non-goals (v1)

- No code-entity-level graph (files/functions/classes). Memory tracks
  assets at the endpoint/route/page/integration level — the granularity a
  pentester already works at in the threat model and coverage tools today.
  Not a general knowledge-graph engine; no dependency on the `graphify`
  Claude Code skill or any other external graph tool. `graphify`'s
  nodes/edges/queryable-structure shape is the inspiration for
  `memory.json`, nothing more.
- No cross-run merge for concurrent scans of the same app. The store uses
  atomic (temp-file + rename) writes so it is never corrupted, but two scans
  racing each other still resolve last-write-wins.
- No auto-migration when a target's identity changes (repo forked/moved to a
  new remote). The old memory is simply orphaned; `strix memory clear` lets a
  user clean it up by hand.
- No new OAuth/credential surface — memory is local file state only.
- Memory does not replace or change `--diff-base`/`--scope-mode`; it only
  supplies a default `diff_base` (the last remembered commit) when the user
  hasn't set one explicitly.

## Architecture

### New package: `strix/memory/`

- **`identity.py`** — the target-identity resolver, promoted out of
  `strix/tools/threat_model/tools.py` (`_target_identity()` and its helpers
  `_normalize_remote_target`, `_normalize_git_remote`, `_local_directory`)
  into a shared function used by both the threat-model tool (unchanged
  behavior) and the new memory subsystem, so "the same app" means one thing
  everywhere in the codebase. Git remote (normalized, scheme/credentials/
  `.git` suffix stripped) is the primary key; normalized URL host is the
  fallback when no git checkout is present. Returns `None` when neither is
  resolvable (e.g. a bare local path with no git remote) — memory is then
  silently disabled for that run.
- **`store.py`** — `MemoryStore`: loads/saves one app's `memory.json`.
  Location: `~/.strix/memory/<key>/memory.json`, where `<key>` is a short
  human-readable slug of the target identity (same slugify approach
  `generate_run_name()` already uses) plus a short hash of the full
  normalized identity string appended for collision-safety — mirroring how
  `run_dir_for()` names things today, just without the random suffix, since
  this key must be stable run to run. The root directory is resolved the
  same way `strix_runs/` is resolved today in `strix/core/paths.py`
  (overridable via an env var, `STRIX_HOME`).
  Centralized rather than inside the scanned checkout, so it works
  identically for pure web/API targets with no local checkout and never
  writes pentest artifacts into the target repo. Writes are atomic
  (temp file + `os.replace`).
- **`reconcile.py`** — matches one finished run's findings against the
  memory's prior findings and computes each one's new status. Structured
  pre-filter (same `asset_id` + same CWE/vuln class) narrows candidates
  before an LLM confirmation call, reusing `strix/report/dedupe.py`'s
  existing LLM-similarity check (refactored to accept an arbitrary
  candidate-pair list) rather than a second bespoke matcher. See "Status
  transitions" below.

### Data model — `memory.json`

```json
{
  "target_key": "github.com/org/sigeweb",
  "targets_seen": ["https://pentest.inovarmais.com/sige", "C:\\...\\sigeweb"],
  "last_scan": {
    "run_name": "pentest-inovarmais-com_33cb",
    "timestamp": "2026-08-31T12:00:00Z",
    "commit": "a1b2c3d"
  },
  "threat_model": "<markdown, carried forward from strix/tools/threat_model>",
  "assets": [
    {
      "id": "ep_1",
      "type": "endpoint",
      "route": "/api/login",
      "method": "POST",
      "auth": "none",
      "notes": "...",
      "first_seen_run": "pentest-inovarmais-com_1a2b",
      "last_seen_run": "pentest-inovarmais-com_33cb"
    }
  ],
  "findings": [
    {
      "id": "f_1",
      "title": "SQL injection in login",
      "severity": "high",
      "cwe": "CWE-89",
      "asset_id": "ep_1",
      "status": "open",
      "first_seen_run": "pentest-inovarmais-com_1a2b",
      "last_seen_run": "pentest-inovarmais-com_33cb",
      "history": [
        {"run": "pentest-inovarmais-com_1a2b", "status": "new"},
        {"run": "pentest-inovarmais-com_33cb", "status": "open"}
      ]
    }
  ]
}
```

### Status transitions

`new` (no match against memory) → `open` (matched an existing finding, still
reported this run) → `fixed` (matched finding's asset was actually covered
this run, per `coverage.json`, but the finding wasn't reported again) →
`regressed` (was `fixed` in a prior scan, reappears now).

The coverage gate matters because of diff-scoping: a repeat scan may only
deep-test *changed* files, so a prior finding whose asset wasn't touched this
run must stay `open` (unverified) rather than flip to `fixed` just because it
wasn't re-reported.

## Scan lifecycle integration

**Start** (`strix/interface/scan_setup.py:prepare_run()`):

1. Resolve target identity via `strix/memory/identity.py`.
2. If a store exists at that key and `--no-memory` was not passed, load it.
3. If a local git checkout is among the targets and memory has
   `last_scan.commit`, auto-set `diff_base` to that commit via the existing
   `resolve_diff_scope_context()` — unless the user explicitly passed
   `--scope-mode`/`--diff-base`, which always takes precedence.
4. Build a short memory summary (known asset count, open/fixed finding
   counts, what changed since the last commit) and thread it through to
   `runner.py` alongside the existing `instruction`/`diff_scope`, injected
   into the root agent's first turn the same way diff-scope context is
   today.

**During the run** (`strix/core/runner.py` + new `strix/tools/memory/tools.py`):

5. Hydrate a run-scoped `assets` store from memory (same pattern as
   `hydrate_threat_models_from_disk`), pre-seeded with known assets so
   `record_asset` calls are additive, not a rebuild from scratch.
6. New tools, registered alongside `notes`/`threat_model`/`coverage`:
   - `record_asset(route, method, auth, notes)` — agents log discovered
     endpoints/assets during the run.
   - `query_memory(question)` — read-only lookup over the loaded memory
     snapshot (assets, prior findings, threat model) for on-demand digging
     beyond what was auto-seeded.

**End** (teardown in `runner.py`, alongside existing report writing):

7. `reconcile.py` matches this run's findings (from `ReportState`) against
   memory's prior findings, using this run's `coverage.json` to gate
   `fixed`/`regressed`.
8. Merge assets (new + updated `last_seen_run`), carry forward the amended
   threat model, record `last_scan` (run name, timestamp, current HEAD
   commit if a local checkout is present), and write `memory.json` back via
   `MemoryStore`.
9. The delta (N new, N still-open, N fixed, N regressed) is written into
   this run's own `strix_runs/<run_name>/` output (a section in the
   executive report), not just silently folded into memory.

## CLI surface

- `--no-memory` — skip both reading and writing memory for this invocation
  (one-off/CI scans that shouldn't pollute an app's history).
- `strix memory list` — list known apps (target keys) with last-scan time
  and finding counts.
- `strix memory show <target>` — print a summary (assets, findings by
  status, threat model) for the app resolved from `<target>`.
- `strix memory clear <target>` — delete that app's store.

These follow the same subcommand-dispatch pattern `view`/`auth` already use
in `strix/interface/main.py`.

## Error handling

Fail open, always — memory must never block a scan:

- No git remote and no resolvable URL host → identity resolution returns
  `None` → memory silently disabled for this run, one-line note in output.
- Corrupted/unreadable `memory.json` → log a warning, proceed as if no
  memory existed for this run (do not overwrite the corrupted file until a
  successful new write).
- Concurrent scans of the same app → atomic write (temp file + rename)
  prevents corruption; last writer wins, no merge (accepted v1 gap).
- Target identity changes (fork/move) → treated as a new app; the old
  store is orphaned and left for manual `strix memory clear`.

## Testing

Matching the existing `tests/test_*.py` style:

- `identity.py` — git-remote normalization edge cases (extends whatever
  already covers `_target_identity()`).
- `store.py` — load/save round-trip, atomic write, fail-open on a
  corrupted file.
- `reconcile.py` — status-transition matrix (new/open/fixed/regressed),
  including the coverage-gating logic.
- `prepare_run()` — auto-diff-scope-from-memory applies when a remembered
  commit exists and correctly yields to an explicit `--diff-base`.
