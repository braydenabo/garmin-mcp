"""Reject files that must never be committed to this public repo.

Usage: check_forbidden_files.py FILE [FILE ...]   (pre-commit passes staged files)

Blocks health data (databases, FIT/GPX/TCX, archives), credentials, local
plans, and anything over the size limit. Exits 1 and lists every problem.
"""

import sys
from pathlib import Path, PurePosixPath

MAX_BYTES = 1_000_000

FORBIDDEN_SUFFIXES = {
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite3",
    ".fit",
    ".gpx",
    ".tcx",
    ".zip",
    ".key",
    ".pem",
}
ALLOWED_NAMES = {".env.example"}


def problem(path: str) -> str | None:
    posix = PurePosixPath(path.replace("\\", "/"))
    name = posix.name
    lower = name.lower()
    if name in ALLOWED_NAMES:
        return None
    if posix.suffix.lower() in FORBIDDEN_SUFFIXES:
        return "health data, archive or key file"
    if lower == ".env" or lower.startswith(".env."):
        return "environment file (may contain secrets)"
    if "connect-personal-api-key" in lower:
        return "Garmin API key file"
    if ".plans" in posix.parts or "build plan" in lower or lower.endswith(".plan.md"):
        return "development plan (keep it local)"
    file = Path(path)
    if file.is_file() and file.stat().st_size > MAX_BYTES:
        return f"larger than {MAX_BYTES // 1000} KB"
    return None


def main(argv: list[str]) -> int:
    failures = [(path, reason) for path in argv if (reason := problem(path))]
    for path, reason in failures:
        print(f"blocked: {path}: {reason}", file=sys.stderr)
    if failures:
        print(
            "These files must not be committed to this public repo. "
            "Unstage them with `git restore --staged <file>`.",
            file=sys.stderr,
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
