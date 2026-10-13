"""Say whether node_modules still matches the lockfile.

A start script that installs only when ``node_modules`` is missing is correct exactly once.
After that, every pull that changes a dependency is skipped, and the first symptom is Vite
failing to resolve an import that the source has been updated to use -- an error that points
at application code and says nothing about the install that never ran.

npm writes ``node_modules/.package-lock.json`` on every install, so comparing it against the
real lockfile answers the question without shelling out to npm.

Prints ``stale`` or ``fresh`` on stdout. Exit code matches: 1 for stale, 0 for fresh, so a
caller can branch on either. Anything it cannot determine is reported stale -- an unnecessary
install costs seconds, a skipped one costs a confusing failure.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCKFILE = os.path.join(ROOT, "package-lock.json")
MARKER = os.path.join(ROOT, "node_modules", ".package-lock.json")


def stale() -> bool:
    if not os.path.isdir(os.path.join(ROOT, "node_modules")):
        return True
    if not os.path.exists(MARKER):
        # Installed by an npm too old to write the marker, or a partial install.
        return True
    if not os.path.exists(LOCKFILE):
        return False  # Nothing to compare against; leave the tree alone.
    try:
        return os.path.getmtime(LOCKFILE) > os.path.getmtime(MARKER)
    except OSError:
        return True


if __name__ == "__main__":
    result = stale()
    sys.stdout.write("stale" if result else "fresh")
    raise SystemExit(1 if result else 0)
