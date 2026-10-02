# Audit chain AUDIT_V1

The writer uses the existing AuditEvent schema and never updates/deletes a row.
It supports the full contract field set but does not invent missing order, fill,
exit or result events. Decimal values become exact strings; aware timestamps are
canonical UTC. Every stored column except `record_hash` is included in a SHA-256
digest prefixed with `AUDIT_V1`, including sequence, actor, links and previous hash.

Snapshots are independent JSON copies. The existing secret-redaction policy runs
before persistence and hashing; secret-bearing values are intentionally redacted,
not claimed to be replayable raw secrets. Unsupported/nonfinite values fail.
Negative decisions retain trusted context, validation limits, numeric candidate
inputs when schema-valid and the binding reason. Unvalidated model text is not
copied into negative records. Approved pipeline records include sizing and risk.
Validated source payloads and the complete regime snapshot are copied into the
decision record, so later source changes cannot rewrite decision-time evidence.

Audit writes participate in the same transaction as pipeline records. Failure
cannot return approval. Chain sequence uniqueness prevents concurrent writers
from creating divergent records: a collision aborts the transaction, requiring
the caller to retry the whole transaction. No silent last-write-wins or local
in-memory lock is used. PostgreSQL locking/triggers remain runtime-unverified.

Verification checks sequence continuity, previous digest and every row digest.
Trusted external expected-count/head checkpoints detect tail truncation; without
such an anchor, deleting an entire suffix cannot be detected by a hash chain
alone. Hash chains do not protect against an administrator rewriting all hashes.
External archival/anchoring, full-trade e2e audit, authenticated queries and UI,
LLM call linkage and configuration-change integration remain pending.
