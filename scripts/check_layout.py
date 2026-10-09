#!/usr/bin/env python3
"""scripts/check_layout.py — the repository layout guard (R0).

Two modes:

  python scripts/check_layout.py --write-manifest
      SHA-256 of every git-tracked file under detectors/ (``git ls-files -z
      detectors``; detectors/results/ is untracked runtime data and is not
      covered), one "<sha256>  <posix path>" line per file, sorted by path,
      written to scripts/detectors_manifest.sha256. Run once on the clean
      checkout before R0's first move. Regenerated only on the owner's
      explicit instruction when the detector changes through the A3 gate.

  python scripts/check_layout.py
      Recomputes the manifest and compares it; checks every "From" path of
      the R0 moves is no longer tracked and every "To" path is; checks the
      files R0 creates exist; greps the production packages for imports of
      the archived or research trees. Prints "layout ok" and exits 0, or
      lists every failure and exits 1.

Hashing: file bytes are hashed with CRLF normalised to LF, so a Windows
checkout (core.autocrlf) and a Linux checkout agree. A change that only adds
or removes carriage returns is therefore invisible; every other byte counts.
Standard library only; runs on Windows and Linux.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "detectors_manifest.sha256"
PROTECTED = "detectors"

# R0 moves (ТЗ §4 + handoff §2). (old, new); new=None when the file leaves the
# tree (kept in history, gitignored on disk).
MOVES: list[tuple[str, str | None]] = [
    ("backtesting/__init__.py", "research/backtesting/__init__.py"),
    ("backtesting/level_features.py", "research/backtesting/level_features.py"),
    ("backtesting/secondary_only_engine.py", "research/backtesting/secondary_only_engine.py"),
    ("paper_trade/__init__.py", "archive/paper_trade/__init__.py"),
    ("ml_models/README.md", "archive/ml_models/README.md"),
    ("openclaw/bridge.py", "archive/openclaw/bridge.py"),
    ("strategies/strategy_log.md", "archive/strategies/strategy_log.md"),
    ("exchange/bybit_client.py", "archive/exchange_stubs/bybit_client.py"),
    ("exchange/paper_trading.py", "archive/exchange_stubs/paper_trading.py"),
    ("MEMORY_HYGIENE.md", "docs/runbooks/memory_hygiene.md"),
    ("VALIDATION_STATUS.md", "docs/runbooks/validation_status.md"),
    ("memory.md", "docs/agent/memory.md"),
    ("personality.md", "docs/agent/personality.md"),
    ("deploy.sh", "deploy/deploy.sh"),
    ("avax_parity.log", None),
    ("link_parity.log", None),
    ("sol_parity.log", None),
    ("sol_3rr_trade_log.csv", None),
    ("requirements.txt", "archive/requirements.txt"),
    ("deploy/health_check.sh", "archive/paper_trade/deploy/health_check.sh"),
    ("deploy/health_check.cron", "archive/paper_trade/deploy/health_check.cron"),
    ("deploy/paper_trade.service", "archive/paper_trade/deploy/paper_trade.service"),
    ("docs/app_shell.md", "docs/runbooks/app_shell.md"),
    ("tests/test_alerts.py", "tests/detectors/test_alerts.py"),
    ("tests/test_core_range_end.py", "tests/detectors/test_core_range_end.py"),
    ("tests/test_range_memory.py", "tests/detectors/test_range_memory.py"),
    ("tests/test_sandbox_and_io.py", "tests/detectors/test_sandbox_and_io.py"),
    ("tests/test_scale_invariance.py", "tests/detectors/test_scale_invariance.py"),
    ("tests/test_strength.py", "tests/detectors/test_strength.py"),
    ("tests/test_wrappers_config.py", "tests/detectors/test_wrappers_config.py"),
    ("tests/test_api_v1.py", "tests/service/test_api_v1.py"),
    ("tests/test_app_shell.py", "tests/service/test_app_shell.py"),
    ("tests/test_stages.py", "tests/service/test_stages.py"),
    ("tests/test_worker_log.py", "tests/service/test_worker_log.py"),
    ("tests/test_worker_logic.py", "tests/service/test_worker_logic.py"),
    ("tests/test_csvtail.py", "tests/shared/test_csvtail.py"),
    ("tests/test_pricefmt.py", "tests/shared/test_pricefmt.py"),
]

# service/README.md is the one "From" that stays: a one-line pointer.
POINTER = ("service/README.md", "docs/runbooks/service.md")

MUST_EXIST = [
    "exchange/__init__.py",
    "archive/README.md",
    "docs/runbooks/service.md",
    "docs/agent/memory.md",
    "docs/agent/personality.md",
    "deploy/deploy.sh",
    "tests/__init__.py",
    "tests/detectors/__init__.py",
    "tests/service/__init__.py",
    "tests/shared/__init__.py",
    "tests/exchange/__init__.py",
]

# Nothing in production may import the archived or research trees.
IMPORT_GUARD_DIRS = ["service", "shared", "detectors", "exchange", "data"]
IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(?:paper_trade|backtesting|archive|research)\b")


def git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True)
    return out.stdout.decode("utf-8", errors="replace")


def tracked(prefix: str) -> list[str]:
    """Tracked posix paths under a prefix (a file or a directory), sorted."""
    raw = subprocess.run(["git", "ls-files", "-z", "--", prefix], cwd=ROOT, capture_output=True, check=True).stdout
    return sorted(p.decode("utf-8") for p in raw.split(b"\0") if p)


def file_sha256(rel: str) -> str:
    data = (ROOT / rel).read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def compute_manifest() -> dict[str, str]:
    return {rel: file_sha256(rel) for rel in tracked(PROTECTED)}


def write_manifest() -> int:
    manifest = compute_manifest()
    if not manifest:
        print(f"no tracked files under {PROTECTED}/", file=sys.stderr)
        return 1
    lines = [f"{sha}  {rel}\n" for rel, sha in sorted(manifest.items())]
    MANIFEST.write_text("".join(lines), encoding="utf-8", newline="\n")
    print(f"wrote {MANIFEST.relative_to(ROOT).as_posix()}: {len(lines)} files")
    return 0


def read_manifest() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, rel = line.partition("  ")
        out[rel] = sha
    return out


def check_manifest(failures: list[str]) -> None:
    if not MANIFEST.exists():
        failures.append(f"manifest missing: {MANIFEST.relative_to(ROOT).as_posix()} (run --write-manifest)")
        return
    expected = read_manifest()
    actual = compute_manifest()
    for rel in sorted(set(expected) - set(actual)):
        failures.append(f"{PROTECTED}: tracked file missing: {rel}")
    for rel in sorted(set(actual) - set(expected)):
        failures.append(f"{PROTECTED}: file not in manifest: {rel}")
    for rel in sorted(set(actual) & set(expected)):
        if actual[rel] != expected[rel]:
            failures.append(f"{PROTECTED}: content changed: {rel}")


def check_moves(failures: list[str]) -> None:
    for old, new in MOVES:
        if tracked(old):
            failures.append(f"still tracked at the old path: {old}")
        if new is not None and not tracked(new):
            failures.append(f"missing at the new path: {new}")
        if new is None:
            name = Path(old).name
            for hit in tracked("research/backtesting/results/" + name):
                failures.append(f"must not be tracked (gitignored output): {hit}")
    old, new = POINTER
    if not tracked(new):
        failures.append(f"missing at the new path: {new}")
    if not tracked(old):
        failures.append(f"pointer missing: {old}")
    else:
        text = (ROOT / old).read_text(encoding="utf-8", errors="replace")
        if new not in text or len(text.splitlines()) > 5:
            failures.append(f"{old} must be a one-line pointer to {new}")


def check_must_exist(failures: list[str]) -> None:
    for rel in MUST_EXIST:
        if not tracked(rel):
            failures.append(f"missing: {rel}")


def check_imports(failures: list[str]) -> None:
    for d in IMPORT_GUARD_DIRS:
        for rel in tracked(d):
            if not rel.endswith(".py"):
                continue
            try:
                lines = (ROOT / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for n, line in enumerate(lines, 1):
                if IMPORT_RE.match(line):
                    failures.append(f"production code imports a moved tree: {rel}:{n}: {line.strip()}")


def main(argv: list[str]) -> int:
    if "--write-manifest" in argv:
        return write_manifest()
    failures: list[str] = []
    check_manifest(failures)
    check_moves(failures)
    check_must_exist(failures)
    check_imports(failures)
    if failures:
        print(f"layout check FAILED ({len(failures)} problem(s)):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("layout ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
