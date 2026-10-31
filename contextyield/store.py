"""Durable receipt ledger with a pre-dispatch commit boundary."""

from __future__ import annotations

import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout


@dataclass(frozen=True)
class InsertResult:
    receipt_id: str
    created: bool
    same_request: bool


class ReceiptStore:
    def __init__(self, database_url: str) -> None:
        self.pool = ConnectionPool(database_url, min_size=1, max_size=10, open=False,
                                   timeout=3, max_waiting=64, kwargs={"connect_timeout": 2})

    def open(self) -> None:
        self.pool.open(wait=True, timeout=15)

    def close(self) -> None:
        self.pool.close()

    def ready(self) -> bool:
        try:
            with self.pool.connection() as conn:
                row = conn.execute("SELECT to_regclass('public.receipts')").fetchone()
            return bool(row and row[0])
        except (psycopg.Error, PoolClosed, PoolTimeout):
            return False

    def insert_decision(
        self, tenant: str, idempotency_key: str | None, request_hash: str,
        decision: dict[str, Any],
    ) -> InsertResult:
        receipt_id = str(uuid.uuid4())
        with self.pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """INSERT INTO receipts (id, tenant, idempotency_key, request_hash, status, decision)
                       VALUES (%s, %s, %s, %s, 'pending', %s::jsonb)
                       ON CONFLICT (tenant, idempotency_key) DO NOTHING RETURNING id""",
                    (receipt_id, tenant, idempotency_key, request_hash, json.dumps(decision)),
                ).fetchone()
                if row:
                    return InsertResult(receipt_id, True, True)
                old = conn.execute(
                    "SELECT id, request_hash FROM receipts WHERE tenant=%s AND idempotency_key=%s",
                    (tenant, idempotency_key),
                ).fetchone()
                if old is None:
                    raise RuntimeError("idempotency conflict without receipt")
                return InsertResult(str(old[0]), False, hmac.compare_digest(old[1], request_hash))

    def finish(
        self, receipt_id: str, status: str, outcome: dict[str, Any],
        attempt: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"completed", "failed", "rejected", "unknown"}:
            raise ValueError("invalid terminal status")
        with self.pool.connection() as conn, conn.transaction():
            if attempt is not None:
                conn.execute(
                    """INSERT INTO attempts (receipt_id, target_id, status, elapsed_ms,
                              http_status, observation, error_code)
                           VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)""",
                    (receipt_id, attempt["target_id"], attempt["status"],
                     attempt["elapsed_ms"], attempt.get("http_status"),
                     json.dumps(attempt.get("observation", {})), attempt.get("error_code")),
                )
            updated = conn.execute(
                """UPDATE receipts SET status=%s, outcome=%s::jsonb, updated_at=now()
                       WHERE id=%s AND status='pending'""",
                (status, json.dumps(outcome), receipt_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError("receipt not pending")

    def get(self, tenant: str, receipt_id: str) -> dict[str, Any] | None:
        try:
            parsed = uuid.UUID(receipt_id)
        except ValueError:
            return None
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id, tenant, status, decision, outcome, created_at, updated_at
                       FROM receipts WHERE id=%s AND tenant=%s""",
                (parsed, tenant),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                """SELECT target_id, status, elapsed_ms, http_status, observation, error_code
                       FROM attempts WHERE receipt_id=%s ORDER BY id""",
                (parsed,),
            )
            attempts = cur.fetchall()
        return {**row, "id": str(row["id"]), "attempts": attempts}

    def insert_task_event(self, tenant: str, event: dict[str, Any]) -> bool:
        """Idempotently ingest one metadata-only task event.

        True means inserted; False means an identical event already exists.
        Reusing an event ID with different content is an error.
        """
        with self.pool.connection() as conn, conn.transaction():
            inserted = conn.execute(
                """INSERT INTO task_events (tenant, task_id, event_id, event)
                   VALUES (%s, %s, %s, %s::jsonb)
                   ON CONFLICT (tenant, task_id, event_id) DO NOTHING RETURNING id""",
                (tenant, event["task_id"], event["event_id"], json.dumps(event)),
            ).fetchone()
            if inserted:
                return True
            old = conn.execute(
                """SELECT event FROM task_events
                   WHERE tenant=%s AND task_id=%s AND event_id=%s""",
                (tenant, event["task_id"], event["event_id"]),
            ).fetchone()
            if old is None:
                raise RuntimeError("task event conflict without row")
            if old[0] != event:
                raise ValueError("event_id_reused_with_different_content")
            return False

    def get_task_events(self, tenant: str, task_id: str, *, limit: int = 1000) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                """SELECT event FROM task_events WHERE tenant=%s AND task_id=%s
                   ORDER BY id LIMIT %s""", (tenant, task_id, limit + 1),
            ).fetchall()
        if len(rows) > limit:
            raise ValueError("task has too many events for a bounded summary")
        return [row[0] for row in rows]

    def reconcile_pending(self, older_than_s: int = 300) -> int:
        """Mark orphaned pre-dispatch records unknown, without redispatching work."""
        with self.pool.connection() as conn:
            with conn.transaction():
                cur = conn.execute(
                    """UPDATE receipts SET status='unknown',
                         outcome='{"reason":"process_interrupted_or_ledger_gap"}'::jsonb,
                         updated_at=now()
                       WHERE status='pending' AND created_at < now() - (%s * interval '1 second')""",
                    (older_than_s,),
                )
                return cur.rowcount
