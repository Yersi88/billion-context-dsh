# COMPACTON-SELF-HEAL — stuck-compaction self-heal hand-patch for `billion-context-dsh`

This branch documents and carries a Layer-3 hand-patch for the npm-installed DSH plugin
`billion-context-dsh` (version 0.2.13 at patch time), installed at
`~/.dsh/profiles/web/node_modules/billion-context-dsh`. The patch repairs a permanent,
reboot-surviving poisoning of the `compress` tool caused by a dangling `compaction/start`
event in the plugin's append-only session log.

**Repo status:** this repository is a fork of the upstream
[https://github.com/Tyan66666/billion-context-dsh](https://github.com/Tyan66666/billion-context-dsh).
Upstream identification: the npm-published package declares no `repository` field; the
upstream repo was confirmed by name + description + content agreement —
`src/region.ts` contains `assertNoActiveCompaction` (line 64) and the exact error string
verbatim (line 71), `src/region.ts:363` contains `runCompactionTransaction`, and
`src/index.ts` contains `AcpCompactionEngine`; the repo's `package.json` matches the
installed package's name, version, and description exactly. The upstream repo ships
source only (`dist/` is built into the npm tarball and not committed).

**Files on this branch:** `patch_billion_compaction.py` (the patcher, idempotent) and
`COMPACTON-SELF-HEAL.md` (this document).

## Symptom

Every `compress` call failed for the session with this error, verbatim:

```
billion-context-dsh: another compaction is already active for this session
```

...on every attempt, across reboots. (User report: "earlier compaction got interrupted I
think, so that's why it says already active. makes sense. remedy needed.")

## Root cause

- `runCompactionTransaction` (bundled `dist/index.js` ~2995–3029; upstream `src/region.ts:363`)
  appends, in order: `compaction/start` → `compaction/summary` → `user/message`
  (surfaceOp REPLACE), and finally `compaction/end`.
- A throw between the start and end appends — recorded case:
  `surface replace: start seq 163808 not found in surface` (stale seqs) — leaves the
  `compaction/start` dangling in the append-only session log.
- `assertNoActiveCompaction` (bundled `dist/index.js` ~2865; upstream `src/region.ts:64`)
  scans the log and throws whenever a `compaction/start` lacks its matching
  `compaction/end`.
- Net effect: every later `compress` call for that session is permanently poisoned,
  surviving reboots (the session log persists; there is no recovery or reset path).

## The fix — `patch_billion_compaction.py` (P0-compaction hand-patch v2, 2026-09-09)

The patcher edits the bundled `dist/index.js` in place, in two layers:

1. **Guard self-heal — `assertNoActiveCompaction` never throws anymore.** A dangling
   `compaction/start` in the append-only log is provably stale by construction: the
   transaction that writes start..end runs synchronously, so while it is genuinely in
   flight no other `compress` call can execute this guard; if the guard is executing,
   the writer is gone (crash, SIGKILL, or a throw inside `runCompactionTransaction`
   before the end append). The guard now warns on stderr —
   `billion-context-dsh: clearing stale compaction flag (compaction/start at event N has no matching end)`
   — and proceeds; the new transaction's own `compaction/end` re-pairs the log.
   (Old behavior: permanent poisoning of every later compress call, across reboots.)

2. **Transaction self-heal — `runCompactionTransaction` wraps everything after the
   `compaction/start` append in try/catch.** Any throw between start and end (stale-seq
   surface replace, serialization error, crash) appends a compensating `compaction/end`
   before rethrowing, so new dangling starts cannot be created. (Outside SIGKILL — and
   even a SIGKILL leaves a stale start that layer 1 self-heals on next use.)

The patcher restores the pristine dist first if a previous patch iteration is present
(idempotence, below), then applies exact string replacements and verifies match counts
against the pristine bundle; it aborts **without writing** on any shape mismatch.

## Verification

- `node --check` on the patched dist: clean.
- Standalone semantics tests: **5/5 pass**
  1. dangling `compaction/start` + trailing events → allowed (no throw);
  2. dangling `compaction/start` at the tail → self-heals, with warn;
  3. closed `start`/`end` pair → silent;
  4. failed transaction → appended `compaction/end`, then rethrows the original error
     (`surface replace: ...`);
  5. happy path → appends exactly `compaction/start` | `compaction/summary` |
     `user/message` (+surfaceOp) | `compaction/end`.

## Activation

Needs one `dsh web` restart: the dist bundle loads at boot, and the patched code takes
effect only after the restart. Until then `compress` remains un-runnable; after the
restart, compression-driven context hygiene resumes. (This documentation and the branch
push made no DSH restarts and touched no `~/.dsh` state.)

## MAINTENANCE RULE — re-apply after every plugin update

npm/plugin updates overwrite `dist/index.js`, so the patch disappears silently. After
**every** `billion-context-dsh` upgrade, re-run:

```sh
python3 patch_billion_compaction.py ~/.dsh/profiles/web/node_modules/billion-context-dsh/dist/index.js
```

The script is idempotent: it detects its own marker (`P0-compaction hand-patch`) in the
dist, restores the pristine `index.js.bak-p0-compaction` (auto-created from the untouched
bundle on first run), re-applies all three shape asserts (guard shape, transaction open
shape, transaction close shape), and re-verifies — safe to run repeatedly. Note: the
pristine backup still contains the original error string; the patched dist does not
(the guard now warns instead of throwing).
## 2026-09-12 — branch rebased onto v0.2.17, fix ported to source

This branch now carries the fix at BOTH levels:

- `src/region.ts` — the real source port (guard warns instead of throwing on a
  dangling compaction/start; `runCompactionTransaction` wraps everything after
  the start append in try/catch and appends a compensating `compaction/end`
  before rethrowing). Regression tests in `tests/region.test.ts` cover both
  layers, including a live-crash reproduction (replace range naming seqs that
  are not in the surface → the checkpoint append throws → the transaction
  pairs its own end → the session stays usable).
- `dist/` rebuilt from the patched source (the repo ships dist for git-source
  installs, so src and dist must move together).
- `patch_billion_compaction.py` — the in-place patcher for npm-installed dists
  (verified to apply cleanly on the 0.2.13 AND 0.2.17 bundles; standalone
  semantics 5/5, live-verified in production: a session poisoned since 2026-09-09
  recovered immediately after the patch + restart).

Upgrade maintenance for npm installs: after every plugin upgrade, rotate the
pristine backup FIRST (the script restores from `index.js.bak-p0-compaction`
when it sees its own marker — a stale backup would silently downgrade), then
re-run the patcher:

```sh
mv dist/index.js.bak-p0-compaction dist/index.js.bak-p0-compaction-<oldver>
cp dist/index.js dist/index.js.bak-p0-compaction
python3 patch_billion_compaction.py dist/index.js && node --check dist/index.js
```

Pre-flight at rebase time: `npm run typecheck && npm test && npm run build`
all green (190/190).
