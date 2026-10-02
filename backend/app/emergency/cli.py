"""Standalone authenticated PAPER inhibition; no web server or re-arm path."""

import argparse
import asyncio
import getpass
import json
import sys
import warnings

from app.config import get_settings
from app.core.errors import SafetyError
from app.db import session as db_session
from app.emergency.audit import record_refusal
from app.emergency.controls import EmergencyControls
from app.modes import TradingMode
from app.security.auth import AuthError, OwnerAuth


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid command arguments; use --help")


async def kill(*, username, password, reason, confirmation):
    if not 1 <= len(username) <= 64 or not 10 <= len(reason.strip()) <= 500:
        raise ValueError("A username and substantive reason of 10-500 characters are required")
    if not password or len(password) > 4096:
        raise AuthError()
    auth = OwnerAuth()
    token, _refresh = await auth.login(username, password)
    principal = await auth.authenticate(token)
    try:
        if get_settings().trading_mode != TradingMode.PAPER:
            raise SafetyError("PAPER_MODE_REQUIRED")
        if confirmation != "KILL PAPER":
            raise SafetyError("EMERGENCY_CONFIRMATION_MISMATCH")
        state = await EmergencyControls().activate("KILL", actor=principal.username, reason=reason)
        return {**state, "mode": "PAPER", "execution": "ENTRY_INHIBITION_ONLY"}
    except SafetyError as error:
        await record_refusal(
            actor=principal.username,
            action="KILL",
            reason=reason,
            outcome="CLI_REQUEST_NOT_COMPLETED",
            code=error.message,
        )
        raise
    finally:
        await auth.logout(principal)


async def run(arguments, password):
    try:
        return await kill(
            username=arguments.username or get_settings().dashboard_username,
            password=password,
            reason=arguments.reason,
            confirmation=arguments.confirm,
        )
    finally:
        await db_session.dispose_engine()


def main():
    parser = Parser(
        description="Authenticated PAPER kill without the web server; does not close positions."
    )
    parser.add_argument("action", choices=["kill"])
    parser.add_argument("--username")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--confirm", required=True, help="Type KILL PAPER")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read one password line from stdin instead of a hidden terminal prompt",
    )
    try:
        arguments = parser.parse_args()
        if arguments.password_stdin:
            password = sys.stdin.readline(4098).rstrip("\r\n")
        elif sys.stdin.isatty():
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("Owner password: ")
        else:
            raise ValueError("Interactive terminal required, or explicitly use --password-stdin")
        result = asyncio.run(run(arguments, password))
        print(json.dumps(result))
        return 0
    except (AuthError, SafetyError) as error:
        print(f"Kill request not confirmed: {error.message}", file=sys.stderr)
    except ValueError as error:
        print(
            f"Kill request not confirmed: {type(error).__name__}; check arguments/configuration",
            file=sys.stderr,
        )
    except (EOFError, KeyboardInterrupt):
        print("Kill request interrupted; inspect persisted state", file=sys.stderr)
    except Exception as error:
        print(
            f"Kill request not confirmed: {type(error).__name__}; inspect persisted state",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
