"""Command-line interface: init/config/doctor plus the trusted wrapper path.

The ``run`` subcommand is the only path that records *observed, trusted*
evidence (via ``harness.py``), and the only place warn/block gating is real —
it gates commands invoked through this wrapper, checked against the task
report before launch. It cannot gate a host agent's native tool calls, and
says so.

``submit`` is also the hook-compatible entrypoint for turn submission: it
accepts ``--turn-id`` or the ``EXECUTION_AUDIT_TURN_ID`` environment variable
so an external hook script can call it without inventing any vendor-specific
hook configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from .harness import (
    DEFAULT_MAX_OUTPUT,
    DEFAULT_TIMEOUT,
    GateBlocked,
    HarnessError,
    make_report_gate,
    run_command,
    snapshot_tree,
)
from .server import (
    ENV_AUDIT_ID,
    ENV_CONFIG,
    ENV_DB,
    ENV_ROLE,
    DEFAULT_CONFIG,
    load_config,
    validate_config,
)

ENV_TURN_ID = "EXECUTION_AUDIT_TURN_ID"
AUDIT_DIR = ".audit"
CONFIG_NAME = "config.json"
DB_NAME = "audit.db"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _fail(message: str, code: int = EXIT_ERROR) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _config_path(args) -> Path | None:
    if getattr(args, "config", None):
        return Path(args.config)
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(env)
    candidate = Path(getattr(args, "root", None) or Path.cwd()) / AUDIT_DIR / CONFIG_NAME
    return candidate if candidate.exists() else None


def _open_store(args):
    """Load config and open the store. Returns (store, config)."""
    from .core import AuditStore  # late import: config errors surface first

    config = load_config(_config_path(args))
    db = getattr(args, "db", None) or os.environ.get(ENV_DB) \
        or str(Path(getattr(args, "root", None) or Path.cwd()) / AUDIT_DIR / DB_NAME)
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    return AuditStore(db, config), config


def _parse_claims(value: str) -> list[dict]:
    text = Path(value[1:]).read_text(encoding="utf-8") if value.startswith("@") else value
    claims = json.loads(text)
    if not isinstance(claims, list):
        raise ValueError("claims must be a JSON list")
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("criterion_id"), str) \
                or not isinstance(claim.get("text"), str):
            raise ValueError("each claim needs criterion_id (str) and text (str)")
        ids = claim.get("evidence_ids", [])
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError("claim evidence_ids must be a list of strings")
    return claims


# ---------------------------------------------------------------- subcommands

def cmd_init(args) -> int:
    root = Path(args.root or Path.cwd()).resolve()
    audit_dir = root / AUDIT_DIR
    audit_dir.mkdir(parents=True, exist_ok=True)
    config_path = audit_dir / CONFIG_NAME
    created = []
    if not config_path.exists():
        config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        created.append(str(config_path))
    store, _ = _open_store(args)
    try:
        store.close()
    finally:
        created.append(str(audit_dir / DB_NAME))
    _print({"ok": True, "created": created,
            "note": "default policy is tools_only + record; change config with execution-audit-admin"})
    return EXIT_OK


def cmd_config(args) -> int:
    path = _config_path(args)
    try:
        config = load_config(path)
    except (OSError, ValueError) as exc:
        return _fail(f"cannot load config: {exc}", EXIT_USAGE)
    errors, warnings = validate_config(config, for_server=False)
    _print({"ok": not errors, "source": str(path) if path else "defaults",
            "config": config, "errors": errors, "warnings": warnings})
    return EXIT_OK if not errors else EXIT_USAGE


def cmd_doctor(args) -> int:
    checks: dict[str, object] = {}
    ok = True
    try:
        config = load_config(_config_path(args))
        checks["config"] = "loaded"
    except (OSError, ValueError) as exc:
        return _fail(f"config: {exc}", EXIT_USAGE)

    errors, warnings = validate_config(config, for_server=False)
    server_errors, _ = validate_config(config, for_server=True)
    checks["policy_errors"] = errors
    checks["server_launch_errors"] = server_errors

    try:
        import mcp  # noqa: F401
        checks["mcp_sdk"] = "available"
    except ImportError:
        checks["mcp_sdk"] = "MISSING"
        ok = False

    try:
        checks["snapshot"] = snapshot_tree(args.root or Path.cwd())[:16]
    except OSError as exc:
        checks["snapshot"] = f"FAILED: {exc}"
        ok = False

    try:
        store, _ = _open_store(args)
        try:
            checks["store"] = {"effects": len(store.list_effects())}
        finally:
            store.close()
    except Exception as exc:
        checks["store"] = f"FAILED: {type(exc).__name__}: {exc}"
        ok = False

    checks["coverage"] = ("observe/gate coverage is limited to commands invoked through "
                          "'execution-audit run'; host-native agent tool calls are not observed")
    ok = ok and not errors
    _print({"ok": ok, "checks": checks, "warnings": warnings})
    return EXIT_OK if ok else EXIT_ERROR


def cmd_task(args) -> int:
    criteria = args.criteria or []
    if isinstance(criteria, str):
        criteria = [c.strip() for c in criteria.split(",") if c.strip()]
    store, _ = _open_store(args)
    try:
        _print(store.create_task(goal=args.goal, criteria=criteria,
                                 request_id=args.request_id or str(uuid.uuid4())))
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")
    finally:
        store.close()
    return EXIT_OK


def cmd_begin(args) -> int:
    store, _ = _open_store(args)
    try:
        _print(store.begin_turn(task_id=args.task_id,
                                request_id=args.request_id or str(uuid.uuid4())))
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")
    finally:
        store.close()
    return EXIT_OK


def cmd_run(args) -> int:
    argv = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not argv:
        return _fail("no command given; usage: execution-audit run --turn-id T -- cmd arg...", EXIT_USAGE)
    turn_id = args.turn_id or os.environ.get(ENV_TURN_ID)
    if not turn_id:
        return _fail(f"--turn-id or {ENV_TURN_ID} is required", EXIT_USAGE)
    try:
        store, config = _open_store(args)
    except (OSError, ValueError) as exc:
        return _fail(f"config: {exc}", EXIT_USAGE)
    try:
        turn = store.get_turn(turn_id=turn_id)
        if turn["status"] != "pending":
            return _fail("turn already submitted; command not executed", EXIT_BLOCKED)
        if args.task_id and args.task_id != turn["task_id"]:
            return _fail("task-id does not own this turn; command not executed", EXIT_BLOCKED)
        if config.get("integration") != "observe":
            return _fail("observed execution requires integration=observe", EXIT_USAGE)
        policy = config["audit"]
        action = "block" if "block" in (policy["action"],policy["on_error"]) else "warn"
        gate = make_report_gate(lambda: store.get_report(task_id=turn["task_id"]), action) if policy["enabled"] and any(policy[k] != "record" for k in ("action", "on_error")) else None
        try:
            evidence = run_command(
                argv,
                root=args.root,
                cwd=args.cwd or args.root,
                timeout=args.timeout,
                max_output=args.max_output,
                gate=gate,
            )
        except GateBlocked as exc:
            return _fail(str(exc), EXIT_BLOCKED)
        except HarnessError as exc:
            return _fail(str(exc), EXIT_USAGE)
        try:
            record = store.record_evidence(turn_id=turn_id, evidence=evidence, trusted=True)
        except Exception as exc:
            return _fail(f"evidence not recorded: {type(exc).__name__}: {exc}")
        _print({"evidence": evidence, "recorded": record})
        return EXIT_OK if evidence["exit_code"] == 0 else EXIT_ERROR
    finally:
        store.close()


def cmd_submit(args) -> int:
    turn_id = args.turn_id or os.environ.get(ENV_TURN_ID)
    if not turn_id:
        return _fail(f"--turn-id or {ENV_TURN_ID} is required", EXIT_USAGE)
    try:
        claims = _parse_claims(args.claims)
    except (OSError, ValueError) as exc:
        return _fail(f"claims: {exc}", EXIT_USAGE)
    store, _ = _open_store(args)
    try:
        _print(store.submit_turn(turn_id=turn_id, claims=claims,
                                 request_id=args.request_id or str(uuid.uuid4())))
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")
    finally:
        store.close()
    return EXIT_OK


def cmd_audit(args) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        return _fail("no auditor command given; usage: execution-audit audit --audit-id A -- cmd...", EXIT_USAGE)
    try:
        from .worker import run_audit  # lead-owned external auditor adapter
    except ImportError as exc:
        return _fail(f"auditor worker unavailable: {exc}")
    try:
        store, _ = _open_store(args)
    except (OSError, ValueError) as exc:
        return _fail(f"config: {exc}", EXIT_USAGE)
    try:
        _print(run_audit(store, args.audit_id, command, args.model, args.timeout))
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")
    finally:
        store.close()
    return EXIT_OK


def cmd_report(args) -> int:
    store, _ = _open_store(args)
    try:
        _print(store.get_report(task_id=args.task_id))
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")
    finally:
        store.close()
    return EXIT_OK


def cmd_complete(args) -> int:
    store, _ = _open_store(args)
    try:
        result = store.request_completion(task_id=args.task_id, request_id=args.request_id or str(uuid.uuid4()))
        _print(result)
        if result.get("status") != "approved":
            return EXIT_BLOCKED
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")
    finally:
        store.close()
    return EXIT_OK


# ------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="execution-audit",
        description="Evidence-backed task auditing CLI (trusted wrapper path).")
    parser.add_argument("--root", default=None, help="project root (default: cwd)")
    parser.add_argument("--config", default=None, help="trusted config JSON path")
    parser.add_argument("--db", default=None, help="SQLite path (default: <root>/.audit/audit.db)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create .audit/ with default config and DB")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("config", help="show effective config and validation result")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("doctor", help="check config, SDK, snapshot, and store health")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("task", help="create a task")
    p.add_argument("--goal", required=True)
    p.add_argument("--criteria", action="append",
                   help="criterion text; repeat for C1, C2, ...")
    p.add_argument("--request-id")
    p.set_defaults(func=cmd_task)

    p = sub.add_parser("begin", help="begin a turn on a task")
    p.add_argument("--task-id", required=True)
    p.add_argument("--request-id")
    p.set_defaults(func=cmd_begin)

    p = sub.add_parser("run", help="run a command via the trusted harness and record observed evidence")
    p.add_argument("--turn-id", default=None, help=f"turn to attach evidence to (env {ENV_TURN_ID})")
    p.add_argument("--task-id", default=None, help="task whose report gates this command under warn/block")
    p.add_argument("--cwd", default=None)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--max-output", type=int, default=DEFAULT_MAX_OUTPUT)
    p.add_argument("command", nargs=argparse.REMAINDER, help="command after '--'")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("submit", help="submit a turn's claims (hook-compatible entrypoint)")
    p.add_argument("--turn-id", default=None, help=f"turn to submit (env {ENV_TURN_ID})")
    p.add_argument("--claims", required=True,
                   help="JSON list or @file: [{criterion_id, text, evidence_ids}]")
    p.add_argument("--request-id")
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("audit", help="run the explicitly configured external auditor")
    p.add_argument("--audit-id", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("command", nargs=argparse.REMAINDER, help="auditor command after '--'")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("report", help="print the task report")
    p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("complete", help="request task completion")
    p.add_argument("--task-id", required=True)
    p.add_argument("--request-id")
    p.set_defaults(func=cmd_complete)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
