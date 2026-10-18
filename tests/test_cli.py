"""The command line a stranger meets first: bad input is one line and exit 2, replay prints one
summary line on stderr, exit codes follow the result status."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from agent_hands.cli import main
from agent_hands.secrets import SecretUnavailable, keychain_read

ROOT = Path(__file__).parents[1]
ART = str(ROOT / "artifacts" / "cu_core.open_sub_account@1.0.0.json")
P = ["-p", "member_id=912345", "-p", "account_type=Savings", "-p", "nickname=Rainy day", "-p", "initial_deposit=25.00"]


@pytest.mark.parametrize(
    "argv, says",
    [
        (["replay", "no/such/artifact.json", *P], "no such file"),
        (["replay", ART, *P, "--fault", "meteor_strike"], "unknown fault"),
        (["replay", ART, *P, "--fault", "http500:sometimes"], "fault mode"),
        (["replay", ART, "-p", "member_id"], "name=value"),
        (["replay", ART, *P, "--fault", "slow:once:abc"], "milliseconds"),
        (["replay", str(ROOT / "README.md"), *P], "not a valid capability artifact"),
        (["replay", ART, *P, "--tenant", "nowhere"], "invalid choice"),
        (["replay", ART, *P, "--no-such-flag"], "unrecognized arguments"),
        (["replay", str(ROOT / "tests/fixtures/discovered_open_sub_account.json"), *P, "--allow-draft",
          "--approve", "s13"], "use 'hands validate'"),
    ],
)
def test_bad_input_is_one_line_and_exit_2(argv, says, capsys):
    assert main(argv) == 2
    out, err = capsys.readouterr()
    assert out == ""  # nothing on stdout: that's how a caller tells bad input from an outcome (also 2)
    assert err.startswith("error: ") and says in err and "Traceback" not in err
    assert len(err.strip().splitlines()) == 1


def test_param_errors_never_echo_the_value(capsys):
    assert main(["replay", ART, "-p", "900001"]) == 2
    assert "900001" not in capsys.readouterr().err


def test_port_in_use_is_an_error_not_a_traceback(capsys):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    try:
        assert main(["target", "--port", str(s.getsockname()[1]), "--control-port", "0"]) == 2
    finally:
        s.close()
    assert capsys.readouterr().err.startswith("error: ")


@pytest.mark.browser
def test_replay_prints_a_summary_line_and_exits_by_status(capsys):
    assert main(["replay", ART, *P]) == 2  # member 912345 doesn't exist: a business outcome
    out, err = capsys.readouterr()
    assert '"status": "outcome"' in out
    assert err.strip().splitlines()[-1].startswith("outcome member_not_found at s05 | commit_state=none")


def test_no_keychain_on_this_os_says_so(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(SecretUnavailable, match="macOS Keychain"):
        keychain_read("openrouter-api-key")
