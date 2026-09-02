#!/usr/bin/env python3
"""P0-compaction hand-patch v2 for billion-context-dsh (2026-09-09).

Installed plugin dist: ~/.dsh/profiles/web/node_modules/billion-context-dsh/
dist/index.js. Applies to the PRISTINE dist (restores from index.js.bak first
if the file already carries a previous iteration).

  A) assertNoActiveCompaction: a dangling compaction/start in the append-only
     event log is ALWAYS stale for this caller — runCompactionTransaction is
     synchronous, so while it is genuinely in flight no other compress call
     can run this guard; if the guard is executing, the writer is gone
     (crash/SIGKILL/throw). Old behavior: permanent poisoning of every later
     compress call for that session, across reboots (the P0). New behavior:
     warn on stderr, proceed. The new transaction's own compaction/end re-pairs
     the log.
  B) runCompactionTransaction: self-heal — any throw between the start and end
     appends (stale-seq surface replace, serialization error, crash) appends a
     compensating compaction/end before rethrowing.

Run: python3 patch_billion_compaction.py <path-to-dist/index.js>
Verifies exact match counts against the pristine bundle; aborts without
writing on any mismatch.
"""
import os
import sys

path = sys.argv[1]
doc = open(path).read()

# Idempotence: if a previous iteration is present, restore the pristine copy.
backup = path + '.bak-p0-compaction'
if 'P0-compaction hand-patch' in doc and os.path.exists(backup):
    doc = open(backup).read()
    print('restored pristine copy from', backup)

old_assert = """function assertNoActiveCompaction(events) {
  let active = false;
  for (const event of events) {
    if (event.type === "compaction/start") active = true;
    else if (event.type === "compaction/end") active = false;
  }
  if (active) {
    throw new Error("billion-context-dsh: another compaction is already active for this session");
  }
}"""
new_assert = """function assertNoActiveCompaction(events) {
  let activeAt = -1;
  for (let i = 0; i < events.length; i++) {
    const event = events[i];
    if (event.type === "compaction/start") activeAt = i;
    else if (event.type === "compaction/end") activeAt = -1;
  }
  /* P0-compaction hand-patch (2026-09-09): a dangling compaction/start in the
   * append-only log is ALWAYS stale for this caller — the transaction that
   * writes start..end runs synchronously, so while it is genuinely in flight
   * no other compress call can execute this guard; if the guard is executing,
   * the writer is gone (crash, SIGKILL, or a throw inside
   * runCompactionTransaction before the end append). The old permanent-throw
   * poisoned every later compress call for the session, across reboots.
   * Warn and proceed: the new transaction's own compaction/end re-pairs the
   * log. */
  if (activeAt !== -1)
    console.warn(`billion-context-dsh: clearing stale compaction flag (compaction/start at event ${activeAt} has no matching end)`);
}"""
assert doc.count(old_assert) == 1, 'assertNoActiveCompaction shape drifted'
doc = doc.replace(old_assert, new_assert)

old_open = """  seqs.push(session.append("compaction/start", { compactionId, turn }).seq);
  seqs.push(session.append("compaction/summary", {"""
new_open = """  seqs.push(session.append("compaction/start", { compactionId, turn }).seq);
  try {
  seqs.push(session.append("compaction/summary", {"""
assert doc.count(old_open) == 1, 'transaction open shape drifted'
doc = doc.replace(old_open, new_open)

old_close = """  seqs.push(session.append("compaction/end", { compactionId, turn }).seq);
  return { compactionId, seqs };
}"""
new_close = """  } catch (error) {
    /* P0-compaction hand-patch (2026-09-09): self-heal — a throw between the
     * start and end appends (stale-seq surface replace, serialization error,
     * crash) previously left the start dangling in the append-only log and
     * poisoned all future compactions for this session. Append the
     * compensating end so the log stays pairable, then surface the error. */
    try {
      session.append("compaction/end", { compactionId, turn });
    } catch {
    }
    throw error;
  }
  seqs.push(session.append("compaction/end", { compactionId, turn }).seq);
  return { compactionId, seqs };
}"""
assert doc.count(old_close) == 1, 'transaction close shape drifted'
doc = doc.replace(old_close, new_close)

open(path, 'w').write(doc)
print('patched OK (v2): guard always self-heals + transaction pairs its own end')