"""MCP stdio server exposing the audit store under two mutually exclusive roles.

Roles are bound at launch from trusted arguments/environment — never from tool
input:

- ``writer`` (default): task/turn/report tools plus *reported* evidence only.
  A writer can never claim ``source='observed'`` or ``trusted=True``; observed
  evidence enters only through the trusted harness path (``cli.py run``).
- ``auditor``: scoped to ONE ``audit_id`` from launch; exposes only evidence /
  conversation reads and audit submission for that audit. The external audit
  model itself is a separate process (see ``worker.py``, owned by the lead).

Default deployment is ``tools_only`` + ``record``: the server records claims
and evidence but enforces nothing, because an MCP server cannot gate a host
agent's native tool calls. Real warn/block gating exists only where commands
are invoked through the CLI wrapper.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from copy import deepcopy
from .core import _DEFAULT_CONFIG, AuditStore, AuditError

DEFAULT_CONFIG = deepcopy(_DEFAULT_CONFIG)

ROLES = ("writer", "auditor")
ENV_ROLE = "EXECUTION_AUDIT_ROLE"
ENV_AUDIT_ID = "EXECUTION_AUDIT_AUDIT_ID"
ENV_DB = "EXECUTION_AUDIT_DB"
ENV_CONFIG = "EXECUTION_AUDIT_CONFIG"

_WRAPPER_COVERAGE = (
    "Coverage: only commands invoked through the execution-audit CLI wrapper "
    "(execution-audit run) are observed and gated. Host-native tool calls "
    "(Cursor/Claude/Codex built-ins) are NOT observed or gated by this server."
)


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | os.PathLike | None) -> dict:
    """Load config from a trusted local file over defaults.

    Only a local file path (CLI argument or trusted env) is honored; writers
    interacting over MCP can never change policy.
    """
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path is None:
        return config
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config file must contain a JSON object")
    return _merge(config, data)


def validate_config(config: dict, *, for_server: bool) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). ``for_server`` adds server-side limits."""
    errors, warnings = [], []
    try:
        AuditStore._validate_config(config)
    except (AuditError, KeyError, TypeError) as exc:
        errors.append(str(exc))
    if for_server and config.get("integration") == "observe":
        warnings.append("Observation and enforcement require the external wrapper; server alone does not intercept native host tools")
    return errors, warnings


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def create_server(store, config: dict, role: str = "writer", audit_id: str | None = None, auditor_model: str | None = None) -> FastMCP:
    """Build a FastMCP instance with tools for exactly one role."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    if role == "auditor" and not audit_id:
        raise ValueError("auditor role requires an audit_id bound at launch")
    if role == "auditor" and not auditor_model:
        raise ValueError("auditor role requires a trusted auditor_model at launch")
    errors, _ = validate_config(config, for_server=True)
    if errors:
        raise ValueError("invalid config for server: " + "; ".join(errors))

    app = FastMCP(
        name=f"execution-audit-{role}",
        instructions=(
            "Evidence-backed task auditing. Writer role: register tasks/turns, submit "
            "reported evidence and claims, request completion. Auditor role: read scoped "
            "evidence/conversation and submit an independent audit verdict. " + _WRAPPER_COVERAGE
        ),
    )

    def _request_id(value: str | None) -> str:
        return value if value else str(uuid.uuid4())

    @app.tool(structured_output=False)
    def audit_capabilities() -> str:
        """Effective policy and honestly-scoped capability statement for this deployment."""
        return _json({
            "ok": True,
            "role": role,
            "integration": config.get("integration"),
            "audit": config.get("audit"),
            "capabilities": config.get("capabilities"),
            "coverage": _WRAPPER_COVERAGE,
        })

    if role == "writer":

        @app.tool(structured_output=False)
        def create_task(goal: str, criteria: list[str], request_id: str | None = None) -> str:
            """Register a task with stable criterion ids (C1, C2, ...)."""
            try:
                return _json(store.create_task(goal=goal, criteria=criteria,
                                               request_id=_request_id(request_id)))
            except Exception as exc:
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        @app.tool(structured_output=False)
        def get_task(task_id: str) -> str:
            """Fetch a task with its turns and audits."""
            try:
                return _json(store.get_task(task_id=task_id))
            except Exception as exc:
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        @app.tool(structured_output=False)
        def begin_turn(task_id: str, request_id: str | None = None) -> str:
            """Open a new execution turn under a task."""
            try:
                return _json(store.begin_turn(task_id=task_id,
                                              request_id=_request_id(request_id)))
            except Exception as exc:
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        @app.tool(structured_output=False)
        def record_evidence(turn_id: str, evidence: dict) -> str:
            """Attach REPORTED evidence to a turn. Observed evidence is rejected here:
            it enters only via the trusted harness path (execution-audit run)."""
            evidence = dict(evidence or {})
            if evidence.get("trusted") or evidence.get("source") == "observed":
                return _json({
                    "ok": False,
                    "error": "capability: writer MCP tools cannot record observed/trusted "
                             "evidence; only the trusted harness path can",
                })
            evidence["source"] = "reported"
            evidence.pop("trusted", None)
            try:
                return _json(store.record_evidence(turn_id=turn_id, evidence=evidence, trusted=False))
            except Exception as exc:
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        @app.tool(structured_output=False)
        def submit_turn(turn_id: str, claims: list[dict], request_id: str | None = None) -> str:
            """Freeze a turn's claims ({criterion_id, text, evidence_ids}) and trigger audit."""
            try:
                return _json(store.submit_turn(turn_id=turn_id, claims=claims,
                                               request_id=_request_id(request_id)))
            except Exception as exc:
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        @app.tool(structured_output=False)
        def request_completion(task_id: str, request_id: str | None = None) -> str:
            """Ask for final completion; the decision reflects audits and coverage."""
            try:
                return _json(store.request_completion(task_id=task_id,
                                                      request_id=_request_id(request_id)))
            except Exception as exc:
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        @app.tool(structured_output=False)
        def get_report(task_id: str) -> str:
            """Current task report: decision, warnings, verification label."""
            try:
                return _json(store.get_report(task_id=task_id))
            except Exception as exc:
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        @app.tool(structured_output=False)
        def add_message(task_id: str, role: str, content: str) -> str:
            """Append a conversation message (auditor sees messages up to the frozen cutoff)."""
            try:
                return _json(store.add_message(task_id=task_id, role=role, content=content))
            except Exception as exc:
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    else:  # auditor — bound to exactly one audit_id from launch

        @app.tool(structured_output=False)
        def get_evidence() -> str:
            """Evidence and claims for the audit this server instance is bound to."""
            try:
                return _json(store.get_evidence(audit_id=audit_id))
            except Exception as exc:
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        @app.tool(structured_output=False)
        def search_conversation(query: str) -> str:
            """Search conversation messages frozen at this audit's submission cutoff."""
            try:
                return _json(store.search_conversation(audit_id=audit_id, query=query))
            except Exception as exc:
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        @app.tool(structured_output=False)
        def read_conversation(message_id: str) -> str:
            """Read one frozen conversation message by id."""
            try:
                return _json(store.read_conversation(audit_id=audit_id, message_id=message_id))
            except Exception as exc:
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        @app.tool(structured_output=False)
        def submit_audit(result: dict, request_id: str | None = None) -> str:
            """Submit the independent verdict {status, findings, summary?} for the bound audit."""
            try:
                return _json(store.submit_audit(audit_id=audit_id, result={**result, "model":auditor_model},
                                                request_id=_request_id(request_id)))
            except Exception as exc:
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    return app


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="execution-audit-server",
                                     description="Execution audit MCP server (stdio)")
    parser.add_argument("--role", choices=ROLES, default=None,
                        help="writer (default) or auditor; env " + ENV_ROLE)
    parser.add_argument("--audit-id", default=None,
                        help="auditor role only: bind to this audit; env " + ENV_AUDIT_ID)
    parser.add_argument("--auditor-model", help="trusted model identity for auditor role")
    parser.add_argument("--db", default=None, help="SQLite path; env " + ENV_DB)
    parser.add_argument("--config", default=None, help="trusted config JSON; env " + ENV_CONFIG)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    role = args.role or os.environ.get(ENV_ROLE) or "writer"
    audit_id = args.audit_id or os.environ.get(ENV_AUDIT_ID)
    db = args.db or os.environ.get(ENV_DB) or str(Path.cwd() / ".audit" / "audit.db")
    config_path = args.config or os.environ.get(ENV_CONFIG)

    if role == "auditor" and not audit_id:
        print("error: auditor role requires --audit-id or " + ENV_AUDIT_ID, file=sys.stderr)
        return 2
    try:
        config = load_config(config_path)
    except (OSError, ValueError) as exc:
        print(f"error: cannot load config: {exc}", file=sys.stderr)
        return 2

    from .core import AuditStore  # imported late so config errors surface first

    Path(db).parent.mkdir(parents=True, exist_ok=True)
    store = AuditStore(db, config)
    try:
        app = create_server(store, config, role=role, audit_id=audit_id, auditor_model=args.auditor_model)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        store.close()
        return 2
    try:
        app.run(transport="stdio")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
