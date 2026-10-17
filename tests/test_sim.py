"""The hostile stand-in app: happy flow and every fault switch, over plain HTTP."""

from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

import cu_sim


@pytest.fixture
def sim():
    s = cu_sim.start()
    yield s
    s.stop()


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, path: str) -> tuple[int, str]:
        try:
            r = self.opener.open(self.base + path, timeout=10)
            return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def post(self, path: str, data: dict) -> tuple[int, str]:
        try:
            r = self.opener.open(self.base + path, urlencode(data).encode(), timeout=10)
            return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def login(self) -> None:
        _, body = self.post("/core/login.do", {"uid": "teller01", "pwd": "synthetic-teller-pass"})
        assert "FRAMESET" in body


def faults(sim, spec: dict) -> None:
    req = urllib.request.Request(
        sim.control_url + "/faults", json.dumps(spec).encode(), {"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=5).read()


def test_login_rejects_bad_password(sim):
    c = Client(sim.url)
    _, body = c.post("/core/login.do", {"uid": "teller01", "pwd": "nope"})
    assert "Invalid user ID or password" in body


def test_pages_need_a_session(sim):
    _, body = Client(sim.url).get("/core/search.do")
    assert "session has expired" in body and "top.location" in body


def test_happy_flow(sim):
    c = Client(sim.url)
    c.login()
    _, body = c.get("/core/search.do?f1=900001&f2=")
    assert "900001" in body and "SAMPLE, ADA" in body
    _, body = c.get("/core/member.do?mid=900001")
    assert "Member Detail" in body and "$812.40" in body
    _, body = c.get("/core/subacct.do?mid=900001")
    assert "Open Sub-Account" in body
    _, body = c.post("/core/subacct.do", {"mid": "900001", "f7": "S", "f8": "Rainy day", "f9": "25.00", "f10": "CASH"})
    assert "Sub-Account Opened" in body and "SA-70001" in body
    assert sim.state.snapshot()["subaccounts"][0]["code"] == "S01"


def test_markup_is_hostile(sim):
    c = Client(sim.url)
    c.login()
    _, body = c.get("/core/search.do")
    lower = body.lower()
    assert "<!doctype" not in lower and " id=" not in lower and "data-testid" not in lower and "<label" not in lower
    _, frames = c.get("/core/frameset.do")
    assert "<FRAMESET" in frames


def test_unknown_member_is_not_found(sim):
    c = Client(sim.url)
    c.login()
    _, body = c.get("/core/search.do?f1=999999")
    assert "No member found" in body


@pytest.mark.parametrize(
    "fault, path, needle",
    [
        ("member_not_found", "/core/search.do?f1=900001", "No member found"),
        ("permission_denied", "/core/subacct.do?mid=900001", "not authorized"),
        ("announcement", "/core/member.do?mid=900001", "System Announcement"),
        ("unknown_dialog", "/core/member.do?mid=900001", "Supervisor Override Required"),
        ("session_expired", "/core/member.do?mid=900001", "session has expired"),
        ("http500", "/core/member.do?mid=900001", "HTTP Status 500"),
    ],
)
def test_fault_fires_once(sim, fault, path, needle):
    c = Client(sim.url)
    c.login()
    faults(sim, {fault: {"mode": "once"}})
    _, body = c.get(path)
    assert needle in body
    if fault == "session_expired":
        c.login()
    _, body = c.get(path)
    assert needle not in body


def test_always_mode_keeps_firing(sim):
    c = Client(sim.url)
    c.login()
    faults(sim, {"http500": {"mode": "always"}})
    assert c.get("/core/member.do?mid=900001")[0] == 500
    assert c.get("/core/member.do?mid=900001")[0] == 500


def test_validation_error_fault(sim):
    c = Client(sim.url)
    c.login()
    faults(sim, {"validation_error": {}})
    _, body = c.post("/core/subacct.do", {"mid": "900001", "f7": "S", "f8": "", "f9": "25.00", "f10": "CASH"})
    assert "CL-2210" in body
    assert sim.state.snapshot()["subaccounts"] == []


def test_real_validation(sim):
    c = Client(sim.url)
    c.login()
    _, body = c.post("/core/subacct.do", {"mid": "900001", "f7": "S", "f8": "", "f9": "1.00", "f10": "CASH"})
    assert "at least $5.00" in body


def test_500_after_commit_still_commits(sim):
    c = Client(sim.url)
    c.login()
    faults(sim, {"http500_after_commit": {}})
    status, _ = c.post("/core/subacct.do", {"mid": "900001", "f7": "S", "f8": "", "f9": "25.00", "f10": "CASH"})
    assert status == 500
    assert len(sim.state.snapshot()["subaccounts"]) == 1


def test_supervisor_override_needs_pin(sim):
    c = Client(sim.url)
    c.login()
    faults(sim, {"unknown_dialog": {"mode": "always"}})
    _, body = c.post("/core/override.do", {"pin": "0000", "back": "/core/member.do?mid=900001"})
    assert "invalid supervisor PIN" in body
    _, body = c.post("/core/override.do", {"pin": cu_sim.app.SUPERVISOR_PIN, "back": "/core/member.do?mid=900001"})
    assert "Member Detail" in body and "Supervisor Override" not in body


def test_unknown_fault_rejected(sim):
    with pytest.raises(urllib.error.HTTPError):
        faults(sim, {"meteor_strike": {}})


def test_tenant_variant_relabels():
    s = cu_sim.start(tenant="lakeside")
    try:
        c = Client(s.url)
        c.login()
        _, body = c.get("/core/subacct.do?mid=900001")
        assert "Initial Deposit" in body and "Share Account" in body and "Open Share" in body
        assert "Opening Deposit" not in body
    finally:
        s.stop()
