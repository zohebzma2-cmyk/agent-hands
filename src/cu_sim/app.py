"""The CoreLink Teller simulator. Standard library only.

Flow: login → frameset (banner / nav / main) → member search → member detail with balances →
open sub-account form → confirmation.

Two ports: the app (what automation may touch) and a control API (faults, reset), which is never
in an agent allowlist.
"""

from __future__ import annotations

import html
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

# --- synthetic data ---------------------------------------------------------

# Synthetic demo credentials, published on purpose (the `sim:` secret store resolves these).
CREDENTIALS = {"teller01": "synthetic-teller-pass"}
SUPERVISOR_PIN = "2468"  # for the "supervisor override" dialog a human resolves

MEMBERS: dict[str, dict[str, Any]] = {
    "900001": {"last": "SAMPLE", "first": "ADA", "dob": "04/12/1961", "phone": "(603) 555-0101",
               "addr": "1 Example Way, Testville NH 03000", "ssn4": "0001",
               "shares": [("S00", "Base Share", "812.40"), ("S10", "Share Draft", "1204.11")]},
    "900002": {"last": "PLACEHOLDER", "first": "BOB", "dob": "09/30/1975", "phone": "(603) 555-0102",
               "addr": "22 Fixture Rd, Mockford NH 03001", "ssn4": "0002",
               "shares": [("S00", "Base Share", "25.00")]},
    "900003": {"last": "EXAMPLE", "first": "CAROL", "dob": "01/05/1988", "phone": "(603) 555-0103",
               "addr": "300 Stub St, Dummyton NH 03002", "ssn4": "0003",
               "shares": [("S00", "Base Share", "5310.77"), ("S10", "Share Draft", "88.02"),
                          ("S20", "Holiday Club", "150.00")]},
    "900004": {"last": "SAMPLE", "first": "DAN", "dob": "07/22/1990", "phone": "(603) 555-0104",
               "addr": "4 Example Way, Testville NH 03000", "ssn4": "0004",
               "shares": [("S00", "Base Share", "64.19")]},
}

PRODUCTS = {"Savings": "S", "Holiday Club": "H", "Certificate": "C"}

# Two "tenants" running the same vendor product with different branding and vocabulary.
TENANTS: dict[str, dict[str, str]] = {
    "ridgeline": {
        "cu_name": "Ridgeline Example Credit Union (SYNTHETIC)",
        "color": "#003366",
        "member_no": "Member #",
        "savings": "Savings",
        "deposit": "Opening Deposit",
        "open_btn": "Open Account",
        "sub_account": "Sub-Account",
        "base_share": "Base Share",
    },
    "lakeside": {
        "cu_name": "Lakeside Sample Federal Credit Union (SYNTHETIC)",
        "color": "#2E5E1E",
        "member_no": "Member No.",
        "savings": "Share Account",
        "deposit": "Initial Deposit",
        "open_btn": "Open Share",
        "sub_account": "Share",
        "base_share": "Primary Share",
    },
}

FAULTS: dict[str, str] = {
    "member_not_found": "Search returns no members (business outcome)",
    "validation_error": "Sub-account submit is rejected with a field error (business outcome)",
    "permission_denied": "Open sub-account page says the teller lacks authority (business outcome)",
    "announcement": "A known 'System Announcement' overlay covers member detail (recoverable)",
    "unknown_dialog": "A 'Supervisor Override' overlay needs a supervisor PIN (escalate to a human)",
    "session_expired": "The session is dropped on the next load of a page (at=, default member detail) (re-login)",
    "slow": "A page (at=, default member detail) responds after a delay (ms, default 3000) (recoverable)",
    "http500": "A page (at=, default member detail) returns HTTP 500 (recoverable on a safe step: retry)",
    "http500_after_commit": "Sub-account submit commits, then returns HTTP 500 (commit state unknown)",
}

# --- state ------------------------------------------------------------------


@dataclass
class Fault:
    mode: str = "once"  # "once" fires a single time, "always" until cleared
    ms: int = 3000
    at: str = "/core/member.do"  # for page-level faults (slow, session_expired, http500): which GET route
    fired: int = 0


@dataclass
class Session:
    user: str
    flags: set[str] = field(default_factory=set)


class SimState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.sessions: dict[str, Session] = {}
            self.faults: dict[str, Fault] = {}
            self.subaccounts: list[dict[str, str]] = []
            self.next_conf = 70001

    def set_faults(self, spec: dict[str, Any]) -> None:
        with self.lock:
            self.faults = {}
            for name, cfg in spec.items():
                if name not in FAULTS:
                    raise ValueError(f"unknown fault {name!r}")
                cfg = cfg if isinstance(cfg, dict) else {}
                self.faults[name] = Fault(mode=cfg.get("mode", "once"), ms=int(cfg.get("ms", 3000)),
                                          at=cfg.get("at", "/core/member.do"))

    def take(self, name: str, path: str | None = None) -> Fault | None:
        """Return the fault if it should fire now, consuming it if it's a one-shot."""
        with self.lock:
            f = self.faults.get(name)
            if f is None or (f.mode == "once" and f.fired) or (path is not None and path != f.at):
                return None
            f.fired += 1
            return f

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "faults": {k: {"mode": v.mode, "ms": v.ms, "at": v.at, "fired": v.fired} for k, v in self.faults.items()},
                "subaccounts": list(self.subaccounts),
                "sessions": len(self.sessions),
            }


# --- markup -----------------------------------------------------------------

E = html.escape


def money(v: str | Decimal) -> str:
    return f"${Decimal(v):,.2f}"


def page(body: str, title: str = "CoreLink Teller", head: str = "") -> str:
    # No doctype: quirks mode, like the real thing.
    return (
        f"<HTML><HEAD><TITLE>{E(title)}</TITLE>{head}</HEAD>"
        f'<BODY BGCOLOR="#F4F1E8" LEFTMARGIN=4 TOPMARGIN=4><FONT FACE="Arial,Helvetica" SIZE=2>'
        f"{body}</FONT></BODY></HTML>"
    )


def heading(text: str, t: dict[str, str]) -> str:
    return (
        f'<TABLE WIDTH="100%" CELLPADDING=3 CELLSPACING=0><TR><TD BGCOLOR="{t["color"]}">'
        f'<FONT COLOR="#FFFFFF" SIZE=3><B>{E(text)}</B></FONT></TD></TR></TABLE><BR>'
    )


def error_line(text: str) -> str:
    return f'<FONT COLOR="#CC0000"><B>{E(text)}</B></FONT><BR><BR>'


def login_page(t: dict[str, str], message: str = "") -> str:
    # Frame-busting, as legacy apps do when the session dies inside a frame.
    head = "<SCRIPT>if (top != self) { top.location.href = '/core/login.do?expired=1'; }</SCRIPT>"
    msg = error_line(message) if message else ""
    return page(
        f"<CENTER><BR><BR>"
        f'<TABLE BORDER=1 CELLPADDING=6 CELLSPACING=0 BGCOLOR="#FFFFFF" WIDTH=380>'
        f'<TR><TD COLSPAN=2 BGCOLOR="{t["color"]}"><FONT COLOR="#FFFFFF"><B>{E(t["cu_name"])}</B><BR>'
        f"CoreLink Teller 4.6.2 &mdash; Sign On</FONT></TD></TR>"
        f'<TR><TD COLSPAN=2>{msg}<FORM METHOD=POST ACTION="/core/login.do">'
        f"<TABLE CELLPADDING=2>"
        f"<TR><TD ALIGN=right>User&nbsp;ID:</TD><TD><INPUT TYPE=text NAME=uid SIZE=16></TD></TR>"
        f"<TR><TD ALIGN=right>Password:</TD><TD><INPUT TYPE=password NAME=pwd SIZE=16></TD></TR>"
        f'<TR><TD></TD><TD><INPUT TYPE=submit VALUE="Sign On"></TD></TR>'
        f"</TABLE></FORM></TD></TR></TABLE>"
        f'<BR><FONT SIZE=1 COLOR="#666666">Authorized use only. Synthetic demo system.</FONT></CENTER>',
        head=head,
    )


def frameset() -> str:
    return (
        "<HTML><HEAD><TITLE>CoreLink Teller</TITLE></HEAD>"
        '<FRAMESET ROWS="58,*" BORDER=1>'
        '<FRAME NAME="banner" SRC="/core/banner.do" SCROLLING=no NORESIZE>'
        '<FRAMESET COLS="170,*">'
        '<FRAME NAME="nav" SRC="/core/nav.do">'
        '<FRAME NAME="main" SRC="/core/search.do">'
        "</FRAMESET></FRAMESET></HTML>"
    )


def banner(t: dict[str, str], user: str) -> str:
    return page(
        f'<TABLE WIDTH="100%" CELLPADDING=2 CELLSPACING=0 BGCOLOR="{t["color"]}"><TR>'
        f'<TD><FONT COLOR="#FFFFFF" SIZE=3><B>{E(t["cu_name"])}</B></FONT></TD>'
        f'<TD ALIGN=right><FONT COLOR="#FFFFFF" SIZE=1>CoreLink Teller 4.6.2<BR>Operator: {E(user)}</FONT></TD>'
        f"</TR></TABLE>"
    )


def nav(t: dict[str, str]) -> str:
    link = "<A HREF=\"javascript:parent.main.location.href='{0}'\">{1}</A><BR><BR>"
    return page(
        '<TABLE CELLPADDING=4><TR><TD><FONT SIZE=2><B>Functions</B></FONT><HR SIZE=1>'
        + link.format("/core/search.do", "Member Search")
        + '<FONT COLOR="#999999">Teller Drawer</FONT><BR><BR>'
        + '<FONT COLOR="#999999">Reports</FONT><BR><BR>'
        + '<A HREF="javascript:top.location.href=\'/core/logout.do\'">Sign Off</A>'
        + "</TD></TR></TABLE>"
    )


def search_page(t: dict[str, str], q: dict[str, str], results: list[tuple[str, dict]] | None, not_found: bool) -> str:
    form = (
        '<FORM NAME="sf" METHOD=GET ACTION="/core/search.do">'
        '<TABLE BORDER=0 CELLPADDING=3>'
        f'<TR><TD>{E(t["member_no"])}:</TD><TD><INPUT TYPE=text NAME=f1 SIZE=10 VALUE="{E(q.get("f1", ""))}"></TD>'
        f'<TD>Last&nbsp;Name:</TD><TD><INPUT TYPE=text NAME=f2 SIZE=14 VALUE="{E(q.get("f2", ""))}"></TD>'
        '<TD><INPUT TYPE=button VALUE="Find" ONCLICK="document.sf.submit()"></TD></TR>'
        "</TABLE></FORM>"
    )
    body = heading("Member Search", t) + form
    if not_found:
        body += error_line("No member found matching the search criteria. (CL-1404)")
    elif results:
        rows = "".join(
            f'<TR BGCOLOR="{"#FFFFFF" if i % 2 else "#EEEEEE"}">'
            f"<TD ONCLICK=\"location.href='/core/member.do?mid={mid}'\" STYLE=\"cursor:pointer\">"
            f'<FONT COLOR="#0000CC"><U>{mid}</U></FONT></TD>'
            f'<TD>{E(m["last"])}, {E(m["first"])}</TD><TD>Main</TD></TR>'
            for i, (mid, m) in enumerate(results)
        )
        body += (
            '<TABLE BORDER=1 CELLPADDING=3 CELLSPACING=0 WIDTH="70%">'
            f'<TR BGCOLOR="#DDDDDD"><TD><B>{E(t["member_no"])}</B></TD><TD><B>Name</B></TD><TD><B>Branch</B></TD></TR>'
            f"{rows}</TABLE><BR><FONT SIZE=1>Select a member number to continue.</FONT>"
        )
    return page(body)


def overlay(inner: str) -> str:
    return (
        '<DIV ID="ov1" STYLE="position:absolute;left:0;top:0;width:100%;height:100%;'
        'background-color:#555555;opacity:0.6;z-index:10"></DIV>'
        '<DIV ID="ov2" STYLE="position:absolute;left:12%;top:18%;width:70%;z-index:11;'
        'background-color:#FFFFE0;border:2px outset #999999">' + inner + "</DIV>"
    )


ANNOUNCEMENT = overlay(
    '<TABLE WIDTH="100%" CELLPADDING=6><TR><TD BGCOLOR="#000080"><FONT COLOR="#FFFFFF"><B>System Announcement</B>'
    "</FONT></TD></TR><TR><TD>Core processing will be unavailable Sunday 02:00&ndash;04:00 ET for scheduled "
    "maintenance. Please complete member maintenance before then.</TD></TR>"
    "<TR><TD ALIGN=center><A HREF=\"javascript:void(0)\" ONCLICK=\"document.getElementById('ov1').style.display='none';"
    "document.getElementById('ov2').style.display='none';\">Acknowledge</A></TD></TR></TABLE>"
)

SUPERVISOR_DIALOG = overlay(
    '<FORM METHOD=POST ACTION="/core/override.do">'
    '<TABLE WIDTH="100%" CELLPADDING=6><TR><TD COLSPAN=2 BGCOLOR="#800000"><FONT COLOR="#FFFFFF">'
    "<B>Supervisor Override Required</B></FONT></TD></TR>"
    "<TR><TD COLSPAN=2>Teller drawer TLR-01 is out of balance. A supervisor must approve before "
    "member maintenance can continue.</TD></TR>"
    "<TR><TD ALIGN=right>Supervisor&nbsp;PIN:</TD><TD><INPUT TYPE=password NAME=pin SIZE=8></TD></TR>"
    '<TR><TD></TD><TD><INPUT TYPE=submit VALUE="Approve Override"></TD></TR></TABLE>'
    '<INPUT TYPE=hidden NAME=back VALUE="{back}"></FORM>'
)


def member_page(t: dict[str, str], mid: str, m: dict, extra_shares: list[tuple[str, str, str]], dialog: str) -> str:
    shares = [(code, t["base_share"] if desc == "Base Share" else desc, bal) for code, desc, bal in m["shares"]]
    shares += extra_shares
    rows = "".join(
        f"<TR><TD>{E(desc)}</TD><TD>{code}</TD><TD ALIGN=right>{money(bal)}</TD></TR>" for code, desc, bal in shares
    )
    info = (
        '<TABLE BORDER=0 CELLPADDING=2>'
        f'<TR><TD><B>Name:</B></TD><TD>{E(m["first"])} {E(m["last"])}</TD>'
        f'<TD WIDTH=30></TD><TD><B>{E(t["member_no"])}:</B></TD><TD>{mid}</TD></TR>'
        f'<TR><TD><B>Address:</B></TD><TD>{E(m["addr"])}</TD><TD></TD><TD><B>DOB:</B></TD><TD>{m["dob"]}</TD></TR>'
        f'<TR><TD><B>Phone:</B></TD><TD>{m["phone"]}</TD><TD></TD><TD><B>SSN:</B></TD><TD>***-**-{m["ssn4"]}</TD></TR>'
        "</TABLE><BR>"
    )
    balances = (
        '<TABLE BORDER=1 CELLPADDING=3 CELLSPACING=0 WIDTH="70%">'
        '<TR BGCOLOR="#DDDDDD"><TD><B>Description</B></TD><TD><B>ID</B></TD><TD ALIGN=right><B>Balance</B></TD></TR>'
        f"{rows}</TABLE><BR>"
    )
    buttons = (
        f'<INPUT TYPE=button VALUE="Open {E(t["sub_account"])}" '
        f"ONCLICK=\"location.href='/core/subacct.do?mid={mid}'\">&nbsp;"
        '<INPUT TYPE=button VALUE="Print" ONCLICK="window.print()">'
    )
    return page(heading("Member Detail", t) + info + balances + buttons + dialog)


def subacct_form(t: dict[str, str], mid: str, m: dict, err: str = "", vals: dict[str, str] | None = None) -> str:
    vals = vals or {}
    opts = "".join(
        f'<OPTION VALUE="{code}"{" SELECTED" if vals.get("f7") == code else ""}>'
        f'{E(t["savings"] if name == "Savings" else name)}</OPTION>'
        for name, code in PRODUCTS.items()
    )
    fund = "".join(
        f'<OPTION VALUE="{v}"{" SELECTED" if vals.get("f10") == v else ""}>{E(label)}</OPTION>'
        for v, label in (("CASH", "Cash"), ("S00", f"Transfer from {t['base_share']}"))
    )
    form = (
        '<FORM METHOD=POST ACTION="/core/subacct.do">'
        f'<INPUT TYPE=hidden NAME=mid VALUE="{mid}">'
        '<TABLE BORDER=0 CELLPADDING=3>'
        f'<TR><TD>{E(t["member_no"])}:</TD><TD>{mid} &nbsp; {E(m["last"])}, {E(m["first"])}</TD></TR>'
        f'<TR><TD>Account&nbsp;Type:</TD><TD><SELECT NAME=f7><OPTION VALUE="">-- select --</OPTION>{opts}</SELECT></TD></TR>'
        f'<TR><TD>Nickname:</TD><TD><INPUT TYPE=text NAME=f8 SIZE=20 MAXLENGTH=20 VALUE="{E(vals.get("f8", ""))}"></TD></TR>'
        f'<TR><TD>{E(t["deposit"])}:</TD><TD><INPUT TYPE=text NAME=f9 SIZE=10 VALUE="{E(vals.get("f9", ""))}"></TD></TR>'
        f"<TR><TD>Funding&nbsp;Source:</TD><TD><SELECT NAME=f10>{fund}</SELECT></TD></TR>"
        f'<TR><TD></TD><TD><INPUT TYPE=submit VALUE="{E(t["open_btn"])}"> &nbsp; '
        f"<INPUT TYPE=button VALUE=\"Cancel\" ONCLICK=\"location.href='/core/member.do?mid={mid}'\"></TD></TR>"
        "</TABLE></FORM>"
    )
    return page(heading(f"Open {t['sub_account']}", t) + (error_line(err) if err else "") + form)


def denied_page(t: dict[str, str]) -> str:
    return page(
        heading("Access Denied", t)
        + error_line("You are not authorized to perform this function. Contact your supervisor. (CL-0403)")
        + "<A HREF=\"javascript:history.back()\">Back</A>"
    )


def confirm_page(t: dict[str, str], rec: dict[str, str]) -> str:
    return page(
        heading(f"{t['sub_account']} Opened", t)
        + '<TABLE BORDER=1 CELLPADDING=4 CELLSPACING=0>'
        f'<TR><TD BGCOLOR="#DDDDDD">Confirmation&nbsp;#</TD><TD><B>{rec["conf"]}</B></TD></TR>'
        f'<TR><TD BGCOLOR="#DDDDDD">{E(t["member_no"])}</TD><TD>{rec["mid"]}</TD></TR>'
        f'<TR><TD BGCOLOR="#DDDDDD">Account</TD><TD>{rec["code"]} {E(rec["product"])}'
        f'{" &ldquo;" + E(rec["nick"]) + "&rdquo;" if rec["nick"] else ""}</TD></TR>'
        f'<TR><TD BGCOLOR="#DDDDDD">Opening Deposit</TD><TD>{money(rec["deposit"])}</TD></TR>'
        "</TABLE><BR>"
        f"<INPUT TYPE=button VALUE=\"Return to Member\" ONCLICK=\"location.href='/core/member.do?mid={rec['mid']}'\">"
    )


def error_500() -> str:
    return (
        "<html><head><title>HTTP Status 500 – Internal Server Error</title></head><body>"
        "<h1>HTTP Status 500 – Internal Server Error</h1><hr><p><b>Type</b> Exception Report</p>"
        "<p><b>Message</b> Request processing failed</p><pre>java.lang.NullPointerException\n"
        "\tat com.corelink.teller.web.MemberAction.execute(MemberAction.java:212)\n"
        "\tat org.apache.struts.action.RequestProcessor.processActionPerform(RequestProcessor.java:431)\n"
        "</pre><hr><h3>Apache Tomcat/7.0.109</h3></body></html>"
    )


# --- handler ----------------------------------------------------------------


class AppHandler(BaseHTTPRequestHandler):
    server: _AppServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        pass

    # -- plumbing --

    @property
    def st(self) -> SimState:
        return self.server.state

    @property
    def t(self) -> dict[str, str]:
        return TENANTS[self.server.tenant]

    def send(self, body: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def session(self) -> tuple[str | None, Session | None]:
        c = SimpleCookie(self.headers.get("Cookie", ""))
        sid = c["JSESSIONID"].value if "JSESSIONID" in c else None
        with self.st.lock:
            return sid, self.st.sessions.get(sid or "")

    def form(self) -> dict[str, str]:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    # -- routing --

    def do_GET(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")

    def route(self, method: str) -> None:
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        path = u.path
        if path in ("/", "/index.html"):
            return self.redirect("/core/login.do")
        if path == "/core/login.do":
            return self.login(method, q)
        if path == "/core/logout.do":
            sid, _ = self.session()
            with self.st.lock:
                self.st.sessions.pop(sid or "", None)
            return self.redirect("/core/login.do")
        sid, sess = self.session()
        if sess is None:
            msg = "Your session has expired. Please sign on again."
            return self.send(login_page(self.t, msg))
        handlers = {
            ("GET", "/core/frameset.do"): lambda: self.send(frameset()),
            ("GET", "/core/banner.do"): lambda: self.send(banner(self.t, sess.user)),
            ("GET", "/core/nav.do"): lambda: self.send(nav(self.t)),
            ("GET", "/core/search.do"): lambda: self.search(q),
            ("GET", "/core/member.do"): lambda: self.member(sid, sess, q),
            ("POST", "/core/override.do"): lambda: self.override(sess),
            ("GET", "/core/subacct.do"): lambda: self.subacct_get(q),
            ("POST", "/core/subacct.do"): lambda: self.subacct_post(),
            ("GET", "/core/confirm.do"): lambda: self.confirm(q),
        }
        h = handlers.get((method, path))
        if h is not None and method == "GET":
            if (f := self.st.take("slow", path)) is not None:
                time.sleep(f.ms / 1000)
            if self.st.take("session_expired", path):
                with self.st.lock:
                    self.st.sessions.pop(sid or "", None)
                return self.send(login_page(self.t, "Your session has expired. Please sign on again."))
            if self.st.take("http500", path):
                return self.send(error_500(), status=500)
        if h is None:
            return self.send(page(error_line("Page not found. (CL-0404)")), status=404)
        h()

    def login(self, method: str, q: dict[str, str]) -> None:
        if method == "GET":
            msg = "Your session has expired. Please sign on again." if q.get("expired") else ""
            return self.send(login_page(self.t, msg))
        f = self.form()
        if CREDENTIALS.get(f.get("uid", "")) != f.get("pwd"):
            return self.send(login_page(self.t, "Invalid user ID or password. (CL-0401)"))
        sid = secrets.token_hex(12)
        with self.st.lock:
            self.st.sessions[sid] = Session(user=f["uid"])
        self.redirect("/core/frameset.do", {"Set-Cookie": f"JSESSIONID={sid}; Path=/; HttpOnly"})

    def search(self, q: dict[str, str]) -> None:
        mid, last = q.get("f1", "").strip(), q.get("f2", "").strip().upper()
        if not mid and not last:
            return self.send(search_page(self.t, q, None, False))
        hits = [
            (k, m) for k, m in sorted(MEMBERS.items())
            if (not mid or k == mid) and (not last or m["last"] == last)
        ]
        if self.st.take("member_not_found"):
            hits = []
        self.send(search_page(self.t, q, hits, not hits))

    def member(self, sid: str | None, sess: Session, q: dict[str, str]) -> None:
        mid = q.get("mid", "")
        m = MEMBERS.get(mid)
        if m is None:
            return self.send(page(error_line("No member found matching the search criteria. (CL-1404)")))
        dialog = ""
        if "override_ok" not in sess.flags and self.st.take("unknown_dialog"):
            dialog = SUPERVISOR_DIALOG.replace("{back}", E(f"/core/member.do?mid={mid}"))
        elif self.st.take("announcement"):
            dialog = ANNOUNCEMENT
        with self.st.lock:
            extra = [(r["code"], r["product"], r["deposit"]) for r in self.st.subaccounts if r["mid"] == mid]
        self.send(member_page(self.t, mid, m, extra, dialog))

    def override(self, sess: Session) -> None:
        f = self.form()
        back = f.get("back", "/core/search.do")
        if not back.startswith("/core/"):
            back = "/core/search.do"
        if f.get("pin") != SUPERVISOR_PIN:
            return self.send(page(error_line("Override rejected: invalid supervisor PIN. (CL-0409)")
                                  + f"<A HREF=\"{E(back)}\">Back</A>"))
        sess.flags.add("override_ok")
        self.redirect(back)

    def subacct_get(self, q: dict[str, str]) -> None:
        if self.st.take("permission_denied"):
            return self.send(denied_page(self.t))
        mid = q.get("mid", "")
        m = MEMBERS.get(mid)
        if m is None:
            return self.send(page(error_line("No member found matching the search criteria. (CL-1404)")))
        self.send(subacct_form(self.t, mid, m))

    def subacct_post(self) -> None:
        f = self.form()
        mid = f.get("mid", "")
        m = MEMBERS.get(mid)
        if m is None:
            return self.send(page(error_line("No member found matching the search criteria. (CL-1404)")))
        err = ""
        try:
            dep = Decimal(f.get("f9", "").replace("$", "").replace(",", "").strip())
        except InvalidOperation:
            dep = None
        product = next((n for n, c in PRODUCTS.items() if c == f.get("f7")), None)
        if product is None:
            err = "Account type is required. (CL-2201)"
        elif dep is None or not dep.is_finite():
            err = f"{self.t['deposit']} must be a dollar amount. (CL-2205)"
        elif dep < Decimal("5.00"):
            err = f"{self.t['deposit']} must be at least $5.00. (CL-2206)"
        elif dep > Decimal("5000.00"):
            err = f"{self.t['deposit']} exceeds the teller limit of $5,000.00. (CL-2207)"
        elif len(f.get("f8", "")) > 20:
            err = "Nickname must be 20 characters or fewer. (CL-2208)"
        elif self.st.take("validation_error"):
            err = "Funding source is restricted for this member. (CL-2210)"
        if err:
            return self.send(subacct_form(self.t, mid, m, err, f))
        with self.st.lock:
            conf = f"SA-{self.st.next_conf}"
            self.st.next_conf += 1
            n = sum(1 for r in self.st.subaccounts if r["mid"] == mid)
            rec = {
                "conf": conf, "mid": mid, "code": f"{PRODUCTS[product]}{n + 1:02d}",
                "product": self.t["savings"] if product == "Savings" else product,
                "nick": f.get("f8", ""), "deposit": f"{dep:.2f}",
            }
            self.st.subaccounts.append(rec)
        if self.st.take("http500_after_commit"):
            return self.send(error_500(), status=500)
        self.redirect("/core/confirm.do?" + urlencode({"c": conf}))

    def confirm(self, q: dict[str, str]) -> None:
        with self.st.lock:
            rec = next((r for r in self.st.subaccounts if r["conf"] == q.get("c", "")), None)
        if rec is None:
            return self.send(page(error_line("Confirmation not found. (CL-1405)")), status=404)
        self.send(confirm_page(self.t, rec))

    def do_HEAD(self) -> None:  # pragma: no cover
        self.send_response(405)
        self.end_headers()


class ControlHandler(BaseHTTPRequestHandler):
    """Fault switches and reset. Separate port; never allowlisted for automation."""

    server: _ControlServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def reply(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/faults":
            return self.reply({"available": FAULTS, **self.server.state.snapshot()})
        self.reply({"error": "not found"}, 404)

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/faults":
            try:
                self.server.state.set_faults(body)
            except ValueError as e:
                return self.reply({"error": str(e)}, 400)
            return self.reply(self.server.state.snapshot())
        if self.path == "/reset":
            self.server.state.reset()
            return self.reply(self.server.state.snapshot())
        self.reply({"error": "not found"}, 404)


class _AppServer(ThreadingHTTPServer):
    daemon_threads = True
    state: SimState
    tenant: str


class _ControlServer(ThreadingHTTPServer):
    daemon_threads = True
    state: SimState


@dataclass
class SimServer:
    app: _AppServer
    control: _ControlServer
    state: SimState
    threads: list[threading.Thread]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.app.server_address[1]}"

    @property
    def control_url(self) -> str:
        return f"http://127.0.0.1:{self.control.server_address[1]}"

    def set_faults(self, spec: dict[str, Any]) -> None:
        self.state.set_faults(spec)

    def stop(self) -> None:
        for s in (self.app, self.control):
            s.shutdown()
            s.server_close()


def start(port: int = 0, control_port: int = 0, tenant: str = "ridgeline", host: str = "127.0.0.1") -> SimServer:
    """Start the app and control servers on background threads. Port 0 picks a free port."""
    if tenant not in TENANTS:
        raise ValueError(f"unknown tenant {tenant!r}; choose from {sorted(TENANTS)}")
    state = SimState()
    app = _AppServer((host, port), AppHandler)
    app.state, app.tenant = state, tenant
    control = _ControlServer((host, control_port), ControlHandler)
    control.state = state
    threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True) for s in (app, control)]
    for th in threads:
        th.start()
    return SimServer(app, control, state, threads)
