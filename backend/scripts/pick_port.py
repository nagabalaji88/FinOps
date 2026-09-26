"""Choose a TCP port the API can actually bind, and say why the first choice failed.

Windows reserves whole blocks of TCP ports for Hyper-V, and anything using it -- WSL 2,
Docker Desktop, Windows Sandbox, the container runtime -- can move those blocks on reboot.
A reserved port is not *in use*: nothing is listening, ``netstat`` shows it free, and the
bind still fails with WinError 10013, "an attempt was made to access a socket in a way
forbidden by its access permissions". Port 8000 lands inside a reserved range often enough
that a start script which hardcodes it fails on a machine that worked yesterday.

Prints the chosen port on stdout so a caller can capture it; everything explanatory goes to
stderr so it stays out of that capture.

    python scripts/pick_port.py 8000        -> "8000", or the next port that binds
"""

from __future__ import annotations

import errno
import socket
import sys

HOST = "127.0.0.1"
ATTEMPTS = 40


def bind_error(port: int) -> OSError | None:
    """None when the port is bindable, otherwise the error that says it is not."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # No SO_REUSEADDR: the question is whether uvicorn can take the port for real,
        # and that option would paper over a socket still in TIME_WAIT.
        sock.bind((HOST, port))
    except OSError as exc:
        return exc
    else:
        return None
    finally:
        sock.close()


def explain(port: int, exc: OSError) -> str:
    win = getattr(exc, "winerror", None)
    if win == 10013 or exc.errno == errno.EACCES:
        return (
            f"Port {port} is reserved by Windows, not in use by another program.\n"
            f"  Hyper-V (WSL 2, Docker Desktop, Windows Sandbox) holds blocks of ports;\n"
            f"  nothing is listening, but binding is refused with WinError 10013.\n"
            f"\n"
            f"  See the reserved blocks:\n"
            f"      netsh interface ipv4 show excludedportrange protocol=tcp\n"
            f"\n"
            f"  Release them (Administrator prompt), which usually frees {port} until reboot:\n"
            f"      net stop winnat\n"
            f"      net start winnat\n"
            f"\n"
            f"  Or claim it permanently, before Hyper-V can (Administrator, port must be free):\n"
            f"      netsh int ipv4 add excludedportrange protocol=tcp startport={port} numberofports=1\n"
        )
    if win == 10048 or exc.errno == errno.EADDRINUSE:
        return (
            f"Port {port} is already in use -- most likely an API window from an earlier run.\n"
            f"  Find it:   netstat -ano | findstr :{port}\n"
            f"  Stop it:   taskkill /PID <pid> /F\n"
        )
    return f"Port {port} could not be bound: {exc}\n"


def main() -> int:
    try:
        preferred = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    except ValueError:
        print("usage: pick_port.py [port]", file=sys.stderr)
        return 2

    first = bind_error(preferred)
    if first is None:
        print(preferred)
        return 0

    print(explain(preferred, first), file=sys.stderr)

    for candidate in range(preferred + 1, preferred + 1 + ATTEMPTS):
        if bind_error(candidate) is None:
            print(
                f"  Falling back to port {candidate} for this run. To pin it, set "
                f"FINOPS_API_PORT before starting.\n",
                file=sys.stderr,
            )
            print(candidate)
            return 0

    print(
        f"  No free port between {preferred} and {preferred + ATTEMPTS}. Fix the reservation "
        f"above, or set FINOPS_API_PORT to a port outside the reserved blocks.",
        file=sys.stderr,
    )
    print(preferred)  # Let the caller fail loudly on the port the operator asked for.
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
