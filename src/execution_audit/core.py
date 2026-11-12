"""
Execution Audit Core — transactional SQLite ledger for evidence-backed task auditing.

Owned by Cursor worker. Exports AuditError and AuditStore.
"""

from __future__ import annotations

import functools
import threading
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class AuditError(Exception):
    """Raised for all domain-level violations and policy errors."""


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict = {
    "integration": "tools_only",
    "audit": {
        "enabled": True,
        "action": "record",
        "on_error": "record",
        "require_different_model": False,
    },
    "capabilities": {
        "observe_execution": False,
        "detect_turn_end": False,
        "read_conversation": False,
        "block_next_action": False,
        "enforce_final_report": False,
    },
    "writer_model": None,
}

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    goal        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS criteria (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    stable_id   TEXT NOT NULL,
    text        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(id),
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    submitted_at TEXT
);

CREATE TABLE IF NOT EXISTS claims (
    id           TEXT PRIMARY KEY,
    turn_id      TEXT NOT NULL REFERENCES turns(id),
    criterion_id TEXT NOT NULL,
    text         TEXT NOT NULL,
    evidence_ids TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id          TEXT PRIMARY KEY,
    turn_id     TEXT NOT NULL REFERENCES turns(id),
    source      TEXT NOT NULL,
    trusted     INTEGER NOT NULL DEFAULT 0,
    data        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audits (
    id                   TEXT PRIMARY KEY,
    task_id              TEXT NOT NULL REFERENCES tasks(id),
    turn_id              TEXT NOT NULL REFERENCES turns(id),
    conversation_cutoff  TEXT,
    status               TEXT NOT NULL DEFAULT 'pending',
    result               TEXT,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS effects (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key          TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    result       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _hash(payload: Any) -> str:
    s = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(s.encode()).hexdigest()


def _deep_merge(base: dict, override: dict) -> dict:
    import copy
    result = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and k in result and isinstance(result[k], dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# AuditStore
# ---------------------------------------------------------------------------

class AuditStore:
    """
    Transactional SQLite-backed ledger for execution auditing.

    All state-changing methods that accept a request_id are idempotent:
    - Same key + same payload → return cached result.
    - Same key + different payload → raise AuditError (conflict).
    """

    def __init__(self, path: str, config: dict | None = None):
        self._lock = threading.RLock()
        self._supplied_config = config is not None
        self._config = _deep_merge(_DEFAULT_CONFIG, config or {})
        self._validate_config(self._config)
        # isolation_level=None → autocommit; we manage transactions explicitly.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # ------------------------------------------------------------------
    # Config validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_config(cfg: dict) -> None:
        if cfg.get("integration") not in {"tools_only", "observe"}:
            raise AuditError("Unknown integration mode")
        audit, caps = cfg["audit"], cfg["capabilities"]
        for key in ("action", "on_error"):
            if audit.get(key) not in {"record", "warn", "block"}:
                raise AuditError(f"Unknown audit.{key}")
            if audit[key] == "block" and not caps.get("block_next_action"):
                raise AuditError("block requires block_next_action capability")
            if audit[key] == "warn" and not caps.get("enforce_final_report"):
                raise AuditError("warn requires enforce_final_report capability")
        for key in ("enabled", "require_different_model"):
            if not isinstance(audit.get(key), bool):
                raise AuditError(f"audit.{key} must be boolean")
        if any(not isinstance(v, bool) for v in caps.values()):
            raise AuditError("capabilities must be boolean")
        if cfg["integration"] == "tools_only" and any(caps.get(k) for k in ("observe_execution", "detect_turn_end", "block_next_action")):
            raise AuditError("observed execution and blocking require observe integration")
        if cfg["integration"] == "observe" and not all(caps.get(k) for k in ("observe_execution", "detect_turn_end")):
            raise AuditError("observe requires execution and turn-end capabilities")
        if audit["require_different_model"] and not cfg.get("writer_model"):
            raise AuditError("different-model policy requires writer_model")

    # ------------------------------------------------------------------
    # Schema init
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ledger_events (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL,
          operation TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger_events
          BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger_events
          BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
        """)
        # Persistent triggers make state changes and their history atomic.
        for table in ("tasks", "criteria", "turns", "claims", "evidence", "audits", "messages", "effects", "policy"):
            columns = [r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")]
            for operation in ("INSERT", "UPDATE", "DELETE"):
                prefix = "OLD" if operation == "DELETE" else "NEW"
                fields = ",".join(f"'{c}',{prefix}.{c}" for c in columns)
                self._conn.execute(f"""CREATE TRIGGER IF NOT EXISTS ledger_{table}_{operation}
                    AFTER {operation} ON {table} BEGIN
                    INSERT INTO ledger_events(entity,operation,payload,created_at)
                    VALUES('{table}','{operation}',json_object({fields}),strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                    END""")
        self._begin()
        try:
            row = self._conn.execute("SELECT config FROM policy WHERE id=1").fetchone()
            if row:
                saved = json.loads(row[0])
                if self._supplied_config and saved != self._config:
                    raise AuditError("Stored policy differs; implicit policy changes are forbidden")
                self._config = saved
                self._validate_config(saved)
            else:
                self._conn.execute("INSERT INTO policy(id,config) VALUES(1,?)", (json.dumps(self._config,sort_keys=True),))
            self._commit()
        except Exception:
            self._rollback()
            self._conn.close()
            raise

    # ------------------------------------------------------------------
    # Transaction helpers
    # ------------------------------------------------------------------

    def _begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._conn.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Idempotency gate (must be called inside an open transaction)
    # ------------------------------------------------------------------

    def _idempotency_check(self, key: str, payload: Any) -> tuple[bool, Any]:
        """
        Returns (hit, value):
          hit=True  → cached; value is the deserialized result.
          hit=False → miss;   value is the payload_hash for later insert.
        Raises AuditError on key/payload conflict.
        """
        ph = _hash(payload)
        row = self._conn.execute(
            "SELECT payload_hash, result FROM idempotency_keys WHERE key=?", (key,)
        ).fetchone()
        if row is not None:
            if row[0] != ph:
                raise AuditError(
                    f"Idempotency conflict: key '{key}' already used with a different payload"
                )
            return True, json.loads(row[1])
        return False, ph

    def _idempotency_store(self, key: str, payload_hash: str, result: Any) -> None:
        self._conn.execute(
            "INSERT INTO idempotency_keys(key, payload_hash, result, created_at) "
            "VALUES(?,?,?,?)",
            (key, payload_hash, json.dumps(result), _now()),
        )

    # ------------------------------------------------------------------
    # create_task
    # ------------------------------------------------------------------

    def create_task(self, *, goal: str, criteria: list[str], request_id: str) -> dict:
        payload = {"goal": goal, "criteria": criteria}
        self._begin()
        try:
            hit, cached = self._idempotency_check(request_id, {"method":"create_task","payload":payload})
            if hit:
                self._commit()
                return cached

            ph = cached  # payload hash on miss

            task_id = _new_id()
            now = _now()
            self._conn.execute(
                "INSERT INTO tasks(id, goal, status, created_at) VALUES(?,?,?,?)",
                (task_id, goal, "open", now),
            )
            criterion_objs: list[dict] = []
            for i, text in enumerate(criteria):
                crit_id = _new_id()
                stable_id = f"C{i + 1}"
                self._conn.execute(
                    "INSERT INTO criteria(id, task_id, stable_id, text) VALUES(?,?,?,?)",
                    (crit_id, task_id, stable_id, text),
                )
                criterion_objs.append(
                    {"id": crit_id, "stable_id": stable_id, "text": text}
                )

            result = {
                "id": task_id,
                "goal": goal,
                "status": "open",
                "criteria": criterion_objs,
                "created_at": now,
            }
            self._idempotency_store(request_id, ph, result)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------------
    # get_task
    # ------------------------------------------------------------------

    def get_task(self, *, task_id: str) -> dict:
        row = self._conn.execute(
            "SELECT id, goal, status, created_at FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not row:
            raise AuditError(f"Task not found: {task_id}")

        task: dict = {
            "id": row[0],
            "goal": row[1],
            "status": row[2],
            "created_at": row[3],
        }

        task["criteria"] = [
            {"id": r[0], "stable_id": r[1], "text": r[2]}
            for r in self._conn.execute(
                "SELECT id, stable_id, text FROM criteria WHERE task_id=? ORDER BY stable_id",
                (task_id,),
            ).fetchall()
        ]

        task["turns"] = [
            {"id": r[0], "status": r[1], "created_at": r[2], "submitted_at": r[3]}
            for r in self._conn.execute(
                "SELECT id, status, created_at, submitted_at FROM turns "
                "WHERE task_id=? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        ]

        audits_raw = self._conn.execute(
            "SELECT id, turn_id, status, result, created_at FROM audits "
            "WHERE task_id=? ORDER BY created_at",
            (task_id,),
        ).fetchall()
        task["audits"] = []
        for r in audits_raw:
            entry: dict = {
                "id": r[0],
                "turn_id": r[1],
                "status": r[2],
                "created_at": r[4],
            }
            if r[3]:
                entry["result"] = json.loads(r[3])
            task["audits"].append(entry)

        return task

    # ------------------------------------------------------------------
    # begin_turn
    # ------------------------------------------------------------------

    def begin_turn(self, *, task_id: str, request_id: str) -> dict:
        payload = {"task_id": task_id}
        self._begin()
        try:
            hit, cached = self._idempotency_check(request_id, {"method":"begin_turn","payload":payload})
            if hit:
                self._commit()
                return cached

            task_row = self._conn.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not task_row:
                raise AuditError(f"Task not found: {task_id}")
            if task_row[0] not in ("open",):
                raise AuditError(
                    f"Cannot begin turn: task {task_id} status is '{task_row[0]}'"
                )
            ph = cached
            turn_id = _new_id()
            now = _now()
            self._conn.execute(
                "INSERT INTO turns(id, task_id, status, created_at) VALUES(?,?,?,?)",
                (turn_id, task_id, "pending", now),
            )
            result = {
                "id": turn_id,
                "task_id": task_id,
                "status": "pending",
                "created_at": now,
            }
            self._idempotency_store(request_id, ph, result)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------------
    # record_evidence
    # ------------------------------------------------------------------

    def record_evidence(
        self, *, turn_id: str, evidence: dict, trusted: bool = False
    ) -> dict:
        turn_row = self._conn.execute(
            "SELECT status FROM turns WHERE id=?", (turn_id,)
        ).fetchone()
        if not turn_row:
            raise AuditError(f"Turn not found: {turn_id}")
        if turn_row[0] != "pending":
            raise AuditError(
                f"Turn {turn_id} is already submitted (immutable); "
                "cannot record more evidence"
            )

        source = evidence.get("source", "reported")
        if source not in ("reported", "observed"):
            raise AuditError(f"Invalid evidence source: {source!r}")

        # Role-trust enforcement: 'observed' source requires harness path (trusted=True)
        if source == "observed" and not trusted:
            raise AuditError(
                "Evidence source='observed' requires trusted=True "
                "(harness path only; MCP writer cannot claim observed)"
            )

        self._begin()
        try:
            row = self._conn.execute("SELECT status FROM turns WHERE id=?",(turn_id,)).fetchone()
            if not row or row[0] != "pending":
                raise AuditError("Turn is immutable")
            ev_id = _new_id()
            now = _now()
            self._conn.execute(
                "INSERT INTO evidence(id, turn_id, source, trusted, data, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (ev_id, turn_id, source, int(trusted), json.dumps(evidence), now),
            )
            result = {
                "id": ev_id,
                "turn_id": turn_id,
                "source": source,
                "trusted": trusted,
                "created_at": now,
            }
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------------
    # submit_turn
    # ------------------------------------------------------------------

    def submit_turn(
        self, *, turn_id: str, claims: list[dict], request_id: str
    ) -> dict:
        # Validate turn + task exist before opening transaction
        turn_row = self._conn.execute(
            "SELECT task_id, status FROM turns WHERE id=?", (turn_id,)
        ).fetchone()
        if not turn_row:
            raise AuditError(f"Turn not found: {turn_id}")
        task_id = turn_row[0]

        # Validate claim criterion_ids and evidence_ids reference real entities
        valid_stable_ids = {
            r[0]
            for r in self._conn.execute(
                "SELECT stable_id FROM criteria WHERE task_id=?", (task_id,)
            ).fetchall()
        }
        if not isinstance(claims, list):
            raise AuditError("claims must be a list")
        for claim in claims:
            if not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or not claim["text"].strip():
                raise AuditError("claim text required")
            if not isinstance(claim.get("evidence_ids", []), list):
                raise AuditError("claim evidence_ids must be a list")
        for claim in claims:
            cid = claim.get("criterion_id", "")
            if cid and cid not in valid_stable_ids:
                raise AuditError(
                    f"Claim references unknown criterion_id '{cid}'. "
                    f"Valid: {sorted(valid_stable_ids)}"
                )
            for eid in claim.get("evidence_ids", []):
                ev_row = self._conn.execute(
                    "SELECT turn_id FROM evidence WHERE id=?", (eid,)
                ).fetchone()
                if not ev_row:
                    raise AuditError(
                        f"Claim references unknown evidence_id: {eid}"
                    )
                if ev_row[0] != turn_id:
                    raise AuditError(
                        f"Evidence {eid} belongs to a different turn"
                    )

        payload = {"turn_id": turn_id, "claims": claims}
        self._begin()
        try:
            hit, cached = self._idempotency_check(request_id, {"method":"submit_turn","payload":payload})
            if hit:
                self._commit()
                return cached

            ph = cached

            # Re-check turn status inside transaction (TOCTOU guard)
            cur_status = self._conn.execute(
                "SELECT status FROM turns WHERE id=?", (turn_id,)
            ).fetchone()[0]
            if cur_status == "submitted":
                raise AuditError(
                    f"Turn {turn_id} is already submitted (immutable)"
                )

            now = _now()

            # Persist claims
            for claim in claims:
                self._conn.execute(
                    "INSERT INTO claims(id, turn_id, criterion_id, text, evidence_ids) "
                    "VALUES(?,?,?,?,?)",
                    (
                        _new_id(),
                        turn_id,
                        claim.get("criterion_id", ""),
                        claim.get("text", ""),
                        json.dumps(claim.get("evidence_ids", [])),
                    ),
                )

            # Freeze turn
            self._conn.execute(
                "UPDATE turns SET status='submitted', submitted_at=? WHERE id=?",
                (now, turn_id),
            )

            # Create audit record (frozen conversation cutoff)
            audit_id: str | None = None
            audit_cfg = self._config["audit"]
            if audit_cfg["enabled"]:
                cutoff_row = self._conn.execute(
                    "SELECT id FROM messages WHERE task_id=? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                cutoff = cutoff_row[0] if cutoff_row else None

                audit_id = _new_id()
                self._conn.execute(
                    "INSERT INTO audits(id, task_id, turn_id, conversation_cutoff, "
                    "status, created_at) VALUES(?,?,?,?,?,?)",
                    (audit_id, task_id, turn_id, cutoff, "pending", now),
                )

                # Pending effect
                self._conn.execute(
                    "INSERT INTO effects(id, type, payload, created_at) VALUES(?,?,?,?)",
                    (
                        _new_id(),
                        "audit_required",
                        json.dumps(
                            {
                                "audit_id": audit_id,
                                "turn_id": turn_id,
                                "task_id": task_id,
                            }
                        ),
                        now,
                    ),
                )

            result = {
                "turn_id": turn_id,
                "audit_id": audit_id,
                "status": "submitted",
            }
            self._idempotency_store(request_id, ph, result)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------------
    # request_completion
    # ------------------------------------------------------------------

    def request_completion(self, *, task_id: str, request_id: str) -> dict:
        task_row = self._conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not task_row:
            raise AuditError(f"Task not found: {task_id}")

        payload = {"task_id": task_id}
        self._begin()
        try:
            hit, cached = self._idempotency_check(request_id, {"method":"request_completion","payload":payload})
            if hit:
                self._commit()
                return cached

            ph = cached
            result = self._evaluate_completion(task_id)
            self._idempotency_store(request_id, ph, result)
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    def _evaluate_completion(self, task_id: str) -> dict:
        turns = self._conn.execute("SELECT id,status FROM turns WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
        if not turns or any(t[1] != "submitted" for t in turns):
            return {"status":"rejected", "decision":"denied", "reason":"Missing or unsubmitted turns"}
        # Final turn must revalidate final acceptance criteria; earlier snapshots
        # do not establish correctness of later edits.
        latest = turns[-1][0]
        required = {r[0] for r in self._conn.execute("SELECT stable_id FROM criteria WHERE task_id=?", (task_id,))}
        covered = {r[0] for r in self._conn.execute("SELECT criterion_id FROM claims WHERE turn_id=?", (latest,))}
        if required - covered:
            return {"status":"rejected", "decision":"denied", "reason":f"Criteria not covered by final turn: {sorted(required-covered)}"}
        if self._config["audit"]["enabled"]:
            rows = self._conn.execute("SELECT status,result FROM audits WHERE task_id=? ORDER BY rowid",(task_id,)).fetchall()
            if not rows or any(r[0] == "pending" for r in rows):
                return {"status":"pending_audit", "decision":"pending", "reason":"Audit(s) not yet complete"}
            # Latest turn supersedes earlier findings, which remain in history.
            result = json.loads(rows[-1][1])
            decision = self._audit_decision(result)
            if decision == "denied":
                return {"status":"rejected", "decision":"denied", "reason":"Latest audit blocks completion"}
            warnings = [] if result["status"] == "passed" else [f"Latest audit: {result['status']}"]
            warnings.extend(f["message"] for f in result.get("findings", []))
        else:
            decision, warnings = "recorded", ["Audit skipped"]
        self._conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
        label = "verified" if not warnings else "completed_with_warnings"
        reported = self._conn.execute("SELECT count(*) FROM evidence WHERE turn_id=? AND source='reported'",(latest,)).fetchone()[0]
        if reported:
            label = "self_reported"
            warnings.append("Evidence is self-reported; runtime execution is not verified")
        if not self._config["audit"]["enabled"]:
            label = "audit_skipped"
        return {"status":"approved", "decision":decision, "warnings":warnings, "verification_label":label}

    # ------------------------------------------------------------------
    # get_evidence  (for auditor: bound task + claims + evidence)
    # ------------------------------------------------------------------

    def _criteria_covered_by_claims(self, task_id: str) -> set[str]:
        return {r[0] for r in self._conn.execute(
            "SELECT criterion_id FROM claims WHERE turn_id=(SELECT id FROM turns WHERE task_id=? AND status='submitted' ORDER BY rowid DESC LIMIT 1)", (task_id,))}

    def get_evidence(self, *, audit_id: str) -> dict:
        audit_row = self._conn.execute(
            "SELECT id, task_id, turn_id, status, result, created_at "
            "FROM audits WHERE id=?",
            (audit_id,),
        ).fetchone()
        if not audit_row:
            raise AuditError(f"Audit not found: {audit_id}")

        task_id = audit_row[1]
        turn_id = audit_row[2]

        task_row = self._conn.execute(
            "SELECT goal FROM tasks WHERE id=?", (task_id,)
        ).fetchone()

        out: dict = {
            "audit_id": audit_row[0],
            "task_id": task_id,
            "task_goal": task_row[0] if task_row else None,
            "turn_id": turn_id,
            "audit_status": audit_row[3],
            "created_at": audit_row[5],
        }
        if audit_row[4]:
            out["result"] = json.loads(audit_row[4])

        # Claims
        claim_rows = self._conn.execute(
            "SELECT id, criterion_id, text, evidence_ids FROM claims WHERE turn_id=?",
            (turn_id,),
        ).fetchall()
        all_ev_ids: set[str] = set()
        claims_out: list[dict] = []
        for r in claim_rows:
            ev_ids: list[str] = json.loads(r[3])
            claims_out.append(
                {
                    "id": r[0],
                    "criterion_id": r[1],
                    "text": r[2],
                    "evidence_ids": ev_ids,
                }
            )
            all_ev_ids.update(ev_ids)
        out["claims"] = claims_out

        # Evidence
        ev_out: list[dict] = []
        for ev_id in sorted(all_ev_ids):
            ev_row = self._conn.execute(
                "SELECT id, source, trusted, data, created_at FROM evidence WHERE id=?",
                (ev_id,),
            ).fetchone()
            if ev_row:
                ev_out.append(
                    {
                        "id": ev_row[0],
                        "source": ev_row[1],
                        "trusted": bool(ev_row[2]),
                        "data": json.loads(ev_row[3]),
                        "created_at": ev_row[4],
                    }
                )
        out["evidence"] = ev_out
        return out

    # ------------------------------------------------------------------
    # submit_audit
    # ------------------------------------------------------------------

    def submit_audit(
        self, *, audit_id: str, result: dict, request_id: str
    ) -> dict:
        # Structural validation before any DB access
        self._validate_audit_result(result)

        audit_row = self._conn.execute(
            "SELECT task_id, turn_id, status FROM audits WHERE id=?", (audit_id,)
        ).fetchone()
        if not audit_row:
            raise AuditError(f"Audit not found: {audit_id}")

        task_id = audit_row[0]
        turn_id = audit_row[1]

        payload = {"audit_id": audit_id, "result": result}
        self._begin()
        try:
            hit, cached = self._idempotency_check(request_id, {"method":"submit_audit","payload":payload})
            if hit:
                self._commit()
                return cached

            ph = cached

            # Re-check status inside transaction
            cur_status = self._conn.execute(
                "SELECT status FROM audits WHERE id=?", (audit_id,)
            ).fetchone()[0]
            if cur_status == "complete":
                raise AuditError(f"Audit {audit_id} is already complete")

            # Require-different-model check
            writer_model = self._config.get("writer_model")
            if self._config["audit"].get("require_different_model"):
                audit_model = result.get("model", "")
                if not audit_model:
                    raise AuditError(
                        "Audit result must include 'model' when "
                        "require_different_model=True"
                    )
                if writer_model and audit_model == writer_model:
                    raise AuditError(
                        f"Audit model '{audit_model}' must differ from "
                        f"writer_model '{writer_model}'"
                    )

            # Stale-audit check: the bound turn must be submitted
            turn_row = self._conn.execute(
                "SELECT status, task_id FROM turns WHERE id=?", (turn_id,)
            ).fetchone()
            if not turn_row:
                raise AuditError(
                    f"Audit references non-existent turn: {turn_id}"
                )
            if turn_row[0] != "submitted":
                raise AuditError(
                    f"Audit references a turn that is not yet submitted: {turn_id}"
                )

            # Newer-turns warning (stale: newer turns exist that this audit doesn't cover)
            audit_created = self._conn.execute(
                "SELECT created_at FROM audits WHERE id=?", (audit_id,)
            ).fetchone()[0]
            newer_turns = self._conn.execute(
                "SELECT id FROM turns "
                "WHERE task_id=? AND status='submitted' "
                "AND submitted_at > ? AND id != ?",
                (task_id, audit_created, turn_id),
            ).fetchall()
            newer_turn_ids = [r[0] for r in newer_turns]

            # Evidence integrity: 'passed' with no evidence backing claims is disallowed
            if result.get("status") == "passed":
                all_ev_ids: list[str] = []
                for r in self._conn.execute(
                    "SELECT evidence_ids FROM claims WHERE turn_id=?", (turn_id,)
                ).fetchall():
                    claim_evidence = json.loads(r[0])
                    if not claim_evidence:
                        raise AuditError("Every passed claim requires evidence")
                    all_ev_ids.extend(claim_evidence)
                if not all_ev_ids:
                    raise AuditError(
                        "Audit result 'passed' is invalid: no evidence backing any claim"
                    )

            if result.get("status") == "passed":
                for eid in all_ev_ids:
                    erow = self._conn.execute("SELECT data FROM evidence WHERE id=?", (eid,)).fetchone()
                    ev = json.loads(erow[0])
                    if (ev.get("exit_code") not in (None, 0) or ev.get("timed_out")
                        or ev.get("valid") is False or ev.get("snapshot_mutated")
                        or (ev.get("snapshot_before") and ev.get("snapshot_after") != ev["snapshot_before"])):
                        raise AuditError("Failed, timed-out or changed evidence cannot support pass")
            now = _now()
            self._conn.execute(
                "UPDATE audits SET status='complete', result=? WHERE id=?",
                (json.dumps(result), audit_id),
            )

            # Effect
            self._conn.execute(
                "INSERT INTO effects(id, type, payload, created_at) VALUES(?,?,?,?)",
                (
                    _new_id(),
                    "audit_complete",
                    json.dumps(
                        {
                            "audit_id": audit_id,
                            "turn_id": turn_id,
                            "task_id": task_id,
                            "result_status": result.get("status"),
                            "newer_turns": newer_turn_ids,
                        }
                    ),
                    now,
                ),
            )

            decision = self._audit_decision(result)
            out = {
                "audit_id": audit_id,
                "status": "complete",
                "decision": decision,
            }
            if newer_turn_ids:
                out["stale_warning"] = (
                    f"Newer turns submitted after this audit was created: "
                    f"{newer_turn_ids}"
                )
            self._idempotency_store(request_id, ph, out)
            self._commit()
            return out
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------------
    # Audit result validation
    # ------------------------------------------------------------------

    _VALID_STATUSES = frozenset({"passed", "findings", "inconclusive", "error"})

    @classmethod
    def _validate_audit_result(cls, result: dict) -> None:
        if not isinstance(result, dict):
            raise AuditError("Audit result must be a dict")
        status = result.get("status")
        if status not in cls._VALID_STATUSES:
            raise AuditError(
                f"Invalid audit result status: {status!r}. "
                f"Must be one of {sorted(cls._VALID_STATUSES)}"
            )
        if "model" not in result:
            raise AuditError("Audit result must include 'model'")
        if "findings" not in result:
            raise AuditError("Audit result must include 'findings' list")
        findings = result["findings"]
        if not isinstance(findings, list):
            raise AuditError("Audit result 'findings' must be a list")
        for f in findings:
            if not isinstance(f, dict):
                raise AuditError("Each finding must be a dict")
            if "message" not in f:
                raise AuditError("Each finding must have a 'message' key")
        if status == "passed" and findings:
            raise AuditError("Passed audit cannot contain findings")
        if not isinstance(result.get("model"), str) or not result["model"].strip():
            raise AuditError("Audit model must be nonempty")
        if status == "findings" and not findings:
            raise AuditError(
                "Audit status='findings' must include at least one finding"
            )

    def _audit_decision(self, result: dict) -> str:
        status = result["status"]
        action = self._config["audit"]["action"]
        on_error = self._config["audit"]["on_error"]
        if status == "passed":
            return "approved"
        if status == "error":
            return "denied" if on_error == "block" else (
                "warning" if on_error == "warn" else "recorded"
            )
        if status == "findings":
            return "denied" if action == "block" else (
                "warning" if action == "warn" else "recorded"
            )
        # inconclusive
        return "denied" if action == "block" else ("warning" if action == "warn" else "recorded")

    # ------------------------------------------------------------------
    # get_report
    # ------------------------------------------------------------------

    def get_report(self, *, task_id: str) -> dict:
        task_row = self._conn.execute(
            "SELECT id, goal, status, created_at FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not task_row:
            raise AuditError(f"Task not found: {task_id}")

        task_status = task_row[2]

        criteria_rows = self._conn.execute(
            "SELECT stable_id, text FROM criteria WHERE task_id=? ORDER BY stable_id",
            (task_id,),
        ).fetchall()
        covered = self._criteria_covered_by_claims(task_id)
        criteria_coverage = [
            {"stable_id": r[0], "text": r[1], "covered": r[0] in covered}
            for r in criteria_rows
        ]

        audit_rows = self._conn.execute(
            "SELECT id, turn_id, status, result, created_at FROM audits "
            "WHERE task_id=? ORDER BY created_at",
            (task_id,),
        ).fetchall()
        audits_summary: list[dict] = []
        warnings: list[str] = []
        for r in audit_rows:
            res = json.loads(r[3]) if r[3] else None
            audits_summary.append(
                {
                    "audit_id": r[0],
                    "turn_id": r[1],
                    "status": r[2],
                    "result": res,
                    "created_at": r[4],
                }
            )
            if r[2] == "pending":
                warnings.append(f"Audit {r[0]} still pending")
            elif res and res.get("status") in ("findings", "error", "inconclusive"):
                warnings.append(f"Audit {r[0]}: {res['status']}")

        # Verification label
        complete_audits = [a for a in audits_summary if a["status"] == "complete"]
        all_passed = complete_audits and all(
            (a["result"] or {}).get("status") == "passed" for a in complete_audits
        )
        if task_status == "completed":
            verification_label = "verified" if all_passed else "completed_with_warnings"
        elif task_status == "failed":
            verification_label = "failed"
        else:
            verification_label = "incomplete"

        latest = audits_summary[-1] if audits_summary else None
        if latest and latest["status"] == "pending":
            decision = "denied" if self._config["audit"]["action"] == "block" else "pending"
        elif latest and latest["result"]:
            decision = self._audit_decision(latest["result"])
        else:
            decision = "approved"  # initial work can execute before any audit
        reported = self._conn.execute("SELECT count(*) FROM evidence e JOIN turns t ON e.turn_id=t.id WHERE t.task_id=? AND e.source='reported'",(task_id,)).fetchone()[0]
        if verification_label == "verified" and reported:
            verification_label = "self_reported"
        if not self._config["audit"]["enabled"]:
            verification_label = "audit_skipped"
        return {
            "task_id": task_id,
            "task_status": task_status,
            "policy_version": _hash(self._config),
            "decision": decision,
            "criteria_coverage": criteria_coverage,
            "audits": audits_summary,
            "warnings": warnings,
            "verification_label": verification_label,
        }

    # ------------------------------------------------------------------
    # add_message
    # ------------------------------------------------------------------

    def add_message(self, *, task_id: str, role: str, content: str) -> dict:
        if not self._conn.execute(
            "SELECT 1 FROM tasks WHERE id=?", (task_id,)
        ).fetchone():
            raise AuditError(f"Task not found: {task_id}")

        self._begin()
        try:
            msg_id = _new_id()
            now = _now()
            self._conn.execute(
                "INSERT INTO messages(id, task_id, role, content, created_at) "
                "VALUES(?,?,?,?,?)",
                (msg_id, task_id, role, content, now),
            )
            result = {
                "id": msg_id,
                "task_id": task_id,
                "role": role,
                "content": content,
                "created_at": now,
            }
            self._commit()
            return result
        except Exception:
            self._rollback()
            raise

    # ------------------------------------------------------------------
    # search_conversation  (frozen at audit creation cutoff)
    # ------------------------------------------------------------------

    def search_conversation(self, *, audit_id: str, query: str) -> list[dict]:
        audit_row = self._conn.execute(
            "SELECT task_id, conversation_cutoff FROM audits WHERE id=?",
            (audit_id,),
        ).fetchone()
        if not audit_row:
            raise AuditError(f"Audit not found: {audit_id}")

        task_id, cutoff_id = audit_row

        if not cutoff_id:
            return []  # No messages existed at audit creation time

        cutoff_row = self._conn.execute(
            "SELECT rowid FROM messages WHERE id=?", (cutoff_id,)
        ).fetchone()
        if not cutoff_row:
            return []

        cutoff_ts = cutoff_row[0]
        rows = self._conn.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE task_id=? AND rowid <= ? ORDER BY rowid",
            (task_id, cutoff_ts),
        ).fetchall()

        ql = query.lower()
        return [
            {"id": r[0], "role": r[1], "content": r[2], "created_at": r[3], "source":"reported"}
            for r in rows
            if ql in r[2].lower()
        ]

    # ------------------------------------------------------------------
    # read_conversation  (frozen at audit creation cutoff)
    # ------------------------------------------------------------------

    def read_conversation(self, *, audit_id: str, message_id: str) -> dict:
        audit_row = self._conn.execute(
            "SELECT task_id, conversation_cutoff FROM audits WHERE id=?",
            (audit_id,),
        ).fetchone()
        if not audit_row:
            raise AuditError(f"Audit not found: {audit_id}")

        task_id, cutoff_id = audit_row

        msg_row = self._conn.execute(
            "SELECT id, role, content, created_at FROM messages "
            "WHERE id=? AND task_id=?",
            (message_id, task_id),
        ).fetchone()
        if not msg_row:
            raise AuditError(
                f"Message {message_id} not found in task {task_id}"
            )

        if not cutoff_id:
            raise AuditError(
                "No conversation cutoff set for this audit "
                "(no messages existed at audit creation)"
            )

        cutoff_row = self._conn.execute(
            "SELECT rowid FROM messages WHERE id=?", (cutoff_id,)
        ).fetchone()
        message_sequence = self._conn.execute("SELECT rowid FROM messages WHERE id=?",(message_id,)).fetchone()[0]
        if cutoff_row and message_sequence > cutoff_row[0]:
            raise AuditError(
                f"Message {message_id} is beyond the frozen audit conversation cutoff"
            )

        return {
            "source": "reported",
            "id": msg_row[0],
            "role": msg_row[1],
            "content": msg_row[2],
            "created_at": msg_row[3],
        }

    # ------------------------------------------------------------------
    # list_effects
    # ------------------------------------------------------------------

    def list_effects(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT e.id,e.type,e.payload,e.created_at FROM effects e JOIN audits a ON a.id=json_extract(e.payload, '$.audit_id') WHERE e.type='audit_required' AND a.status='pending' ORDER BY e.rowid"
        ).fetchall()
        return [
            {
                "id": r[0],
                "type": r[1],
                "payload": json.loads(r[2]),
                "created_at": r[3],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # close
    # ------------------------------------------------------------------

    def get_turn(self, *, turn_id: str) -> dict:
        row = self._conn.execute("SELECT id,task_id,status FROM turns WHERE id=?",(turn_id,)).fetchone()
        if not row:
            raise AuditError("Unknown turn")
        return {"id":row[0],"task_id":row[1],"status":row[2]}

    def update_policy(self, *, config: dict, request_id: str) -> dict:
        """Trusted local administration only; never exposed as a writer tool.

        An explicit change applies to subsequent decisions, including existing
        tasks. Previous events and audit verdicts remain unchanged.
        """
        updated = _deep_merge(_DEFAULT_CONFIG, config)
        self._validate_config(updated)
        self._begin()
        try:
            hit, cached = self._idempotency_check(request_id, {"method":"update_policy", "config":updated})
            if hit:
                self._commit()
                return cached
            old_version = _hash(self._config)
            self._conn.execute("UPDATE policy SET config=? WHERE id=1",(json.dumps(updated,sort_keys=True),))
            result = {"old_version":old_version,"policy_version":_hash(updated),"scope":"subsequent decisions for all tasks"}
            self._idempotency_store(request_id,cached,result)
            self._commit()
            self._config = updated
            return result
        except Exception:
            self._rollback()
            raise

    def close(self) -> None:
        self._conn.close()


def _serialized(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            if method.__name__ != "close":
                row = self._conn.execute("SELECT config FROM policy WHERE id=1").fetchone()
                if row:
                    self._config = json.loads(row[0])
            return method(self, *args, **kwargs)
    return wrapped


for _method in ("create_task", "get_task", "begin_turn", "record_evidence", "submit_turn", "request_completion", "get_evidence", "submit_audit", "get_report", "add_message", "search_conversation", "read_conversation", "list_effects", "update_policy", "get_turn", "close"):
    setattr(AuditStore, _method, _serialized(getattr(AuditStore, _method)))
