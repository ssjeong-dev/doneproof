"""Trusted execution harness: run commands, capture bounded observed evidence.

This is the ONLY path that may mark evidence ``trusted=True`` / ``source='observed'``.
It observes commands invoked through it (CLI wrapper); it cannot observe commands a
host agent (Cursor/Claude/Codex) runs natively, and it never claims otherwise.

Guarantees:
- argv is executed directly (never shell=True, never a string command).
- stdout/stderr are captured through temp files and truncated to a bound.
- A stable content snapshot of the code tree is taken before/after; mutation
  during the run marks the evidence invalid.
- Optional gate runs before launch and can block the command.
"""
from __future__ import annotations

import hashlib
import os
import signal
import sys
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Sequence

# Directories that never represent auditable code state.
IGNORED_DIRS = frozenset({
    ".venv", "venv", ".git", ".hg", ".svn", ".audit", ".coord",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".idea", ".vscode",
})
# Database files carry runtime state, not code state.
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".sqlite-journal", ".db-journal", "-wal", "-shm")
DEFAULT_MAX_OUTPUT = 64 * 1024
DEFAULT_TIMEOUT = 300.0

TRUNCATION_MARKER = "\n...[truncated]"


class HarnessError(Exception):
    """Harness-level failure (bad argv, missing cwd, ...)."""


class GateBlocked(Exception):
    """Raised when the audit policy gate refuses a command."""


def _iter_code_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.endswith(".egg-info"))
        for name in sorted(dirnames[:]):
            p = Path(dirpath) / name
            if p.is_symlink():
                dirnames.remove(name)
                yield p
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if name.endswith(DB_SUFFIXES) or name == ".DS_Store":
                continue
            yield p


def snapshot_tree(root: str | os.PathLike) -> str:
    """Stable content hash of the code tree under ``root``.

    Hashes relative path + file content, so the value is unchanged by mtimes
    and includes dirty/untracked files (it walks the filesystem, not git).
    """
    root = Path(root).resolve()
    if not root.is_dir():
        raise HarnessError(f"snapshot root does not exist: {root}")
    digest = hashlib.sha256()
    for path in _iter_code_files(root):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8", "surrogateescape"))
        try:
            if path.is_symlink():
                digest.update(b"symlink:")
                digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
                continue
            file_hash = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    file_hash.update(chunk)
            digest.update(file_hash.digest())
        except OSError as exc:
            raise HarnessError(f"snapshot incomplete: {rel}") from exc
    return digest.hexdigest()


def _read_bounded(fileobj, limit: int) -> tuple[str, bool]:
    raw = fileobj.read(limit + 1)
    if len(raw) > limit:
        return raw[:limit].decode("utf-8", "replace") + TRUNCATION_MARKER, True
    return raw.decode("utf-8", "replace"), False


def run_command(
    argv: Sequence[str],
    *,
    root: str | os.PathLike | None = None,
    cwd: str | os.PathLike | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_output: int = DEFAULT_MAX_OUTPUT,
    env: dict[str, str] | None = None,
    gate: Callable[[], None] | None = None,
) -> dict:
    """Run ``argv`` directly and return an observed evidence dict.

    Never uses a shell. On timeout the process group is killed and
    ``timed_out`` is set with ``exit_code=None``. If the code snapshot
    changed while the command ran, ``snapshot_mutated`` is True and the
    evidence is marked ``valid=False``.
    """
    if isinstance(argv, (str, bytes)):
        raise HarnessError("argv must be a sequence of arguments, not a string (shell=False)")
    argv = list(argv)
    if not argv or not all(isinstance(a, str) and a for a in argv):
        raise HarnessError("argv must be a non-empty sequence of non-empty strings")
    if timeout <= 0:
        raise HarnessError("timeout must be positive")
    if max_output <= 0:
        raise HarnessError("max_output must be positive")

    cwd_path = Path(cwd).resolve() if cwd is not None else None
    if cwd_path is not None and not cwd_path.is_dir():
        raise HarnessError(f"cwd does not exist: {cwd_path}")
    root_path = Path(root).resolve() if root is not None else (cwd_path or Path.cwd())

    if gate is not None:
        gate()  # may raise GateBlocked before anything runs

    snapshot_before = snapshot_tree(root_path)
    started = time.time()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd_path) if cwd_path else None,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,  # own process group so timeout can kill children
            )
        except OSError as exc:
            raise HarnessError(f"failed to launch {argv[0]!r}: {exc.strerror or exc}") from exc
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        exit_code = None if timed_out else proc.returncode
        out.seek(0)
        err.seek(0)
        stdout, stdout_truncated = _read_bounded(out, max_output)
        stderr, stderr_truncated = _read_bounded(err, max_output)
    finished = time.time()
    snapshot_after = snapshot_tree(root_path)
    mutated = snapshot_after != snapshot_before

    evidence: dict = {
        "source": "observed",
        "trusted": True,  # harness path only; server/CLI strip this for writer-reported evidence
        "command": argv,
        "cwd": str(cwd_path) if cwd_path else str(Path.cwd()),
        "root": str(root_path),
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "timeout_s": timeout,
        "snapshot_before": snapshot_before,
        "snapshot_after": snapshot_after,
        "snapshot_mutated": mutated,
        # A command that mutated the tree while running cannot vouch for a
        # stable code state; such evidence is marked invalid.
        "valid": not mutated,
        "started_at": started,
        "finished_at": finished,
        "duration_s": round(finished - started, 6),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }
    return evidence


def report_allows(report: dict | None) -> bool:
    """True when a task report decision counts as audit-approved."""
    if not isinstance(report, dict):
        return False
    decision = str(report.get("decision") or "").strip().lower()
    return decision in {"approved", "passed", "accept", "accepted", "recorded", "allow"}


def make_report_gate(report_provider: Callable[[], dict | None], action: str) -> Callable[[], None]:
    """Build a pre-launch gate from a callable returning the task report.

    ``action='block'`` refuses to run unless the report is approved;
    ``action='warn'`` prints a warning but allows. Any other action is a
    no-op. The gate covers ONLY commands invoked through this wrapper —
    it cannot gate a host agent's native tool calls.
    """
    if action not in ("warn", "block"):
        raise HarnessError(f"unknown gate action: {action!r}")

    def gate() -> None:
        report = report_provider()
        if report_allows(report):
            return
        if isinstance(report, dict) and report.get("audits") == [] and report.get("task_status") == "open":
            return
        decision = (report or {}).get("decision", "none")
        message = (
            f"audit gate ({action}): task report decision is {decision!r}, not approved; "
            "this gate only covers commands invoked through the execution-audit wrapper"
        )
        if decision in {"denied", "block"} or (action == "block" and decision not in {"warning", "warn"}):
            raise GateBlocked(message)
        print(f"WARNING: {message}", file=sys.stderr, flush=True)

    return gate
