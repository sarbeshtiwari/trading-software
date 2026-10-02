"""An unsafe terminal must not silently fall back to echoing an owner password."""

import getpass
import warnings

from app.emergency import cli


def test_password_echo_fallback_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        ["cli", "kill", "--reason", "Owner requests safe inhibition", "--confirm", "KILL PAPER"],
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def unsafe_terminal(prompt):
        warnings.warn("Cannot disable terminal echo", getpass.GetPassWarning, stacklevel=2)
        raise AssertionError("Password read must never happen after echo warning")

    monkeypatch.setattr(cli.getpass, "getpass", unsafe_terminal)
    assert cli.main() == 1
    output = capsys.readouterr()
    assert not output.out
    assert "GetPassWarning" in output.err
