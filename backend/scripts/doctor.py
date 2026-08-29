"""Report the sign-in credentials this database will actually accept.

The seed hashes ``BOOTSTRAP_ADMIN_PASSWORD`` once, the first time the users table is
empty. Editing ``.env`` afterwards changes nothing already stored, so the value in the
file and the value that logs in silently diverge -- the API answers "Invalid credentials"
for a password the operator can plainly read in their own config.

Run from the ``backend`` directory:

    .venv\\Scripts\\python.exe scripts\\doctor.py            print the credentials
    .venv\\Scripts\\python.exe scripts\\doctor.py --quiet    exit 1 on drift, print nothing

Exit codes: 0 usable, 1 drifted (reseed), 2 not seeded yet.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from app.core.config import settings
from app.core.security import verify_password
from app.db.models.identity import User
from app.db.session import session_scope

NOT_SEEDED = 2
DRIFTED = 1


async def main() -> int:
    quiet = "--quiet" in sys.argv

    async with session_scope() as session:
        users = (await session.execute(select(User).order_by(User.created_at))).scalars().all()

    if not users:
        if not quiet:
            print("No users exist yet. Run:  python -m app.cli init-db")
        return NOT_SEEDED

    password = settings.bootstrap_admin_password
    admin = next((u for u in users if u.email.lower() == settings.bootstrap_admin_email.lower()), None)
    stored = admin.hashed_password if admin else None
    matches = bool(stored) and verify_password(password, stored or "")

    if not matches:
        if not quiet:
            print("=" * 66)
            print("  The password in .env is NOT the password this database accepts.")
            print("=" * 66)
            print()
            print("  The seed runs only while the users table is empty, so a later edit to")
            print("  BOOTSTRAP_ADMIN_PASSWORD never reached the stored hash.")
            print()
            print("  Rebuild the database with the current .env:   start.bat reseed")
        return DRIFTED

    if quiet:
        return 0

    locked = [u.email for u in users if u.locked_until]
    width = max(len(u.email) for u in users)

    print("=" * 66)
    print("  Sign in with any of these - they share one password")
    print("=" * 66)
    print()
    print(f"  Password:  {password}")
    print()
    for user in users:
        roles = ", ".join(user.roles or []) or "none"
        flag = "  [LOCKED]" if user.locked_until else ""
        print(f"    {user.email:<{width}}   {roles}{flag}")
    print()
    if locked:
        print("  A locked account clears itself 15 minutes after the fifth failed attempt.")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
