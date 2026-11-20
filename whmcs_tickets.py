#!/usr/bin/env python3
"""WHMCS Tickets — Linux console inbox. Python 3.9+, no pip packages.

Try:   python3 whmcs-tickets.py --demo
Setup: python3 whmcs-tickets.py --setup
Start: python3 whmcs-tickets.py

Setup asks for the HTTPS WHMCS base URL (subdirectories are fine) or the
full includes/api.php URL, the API Identifier, and the API Secret. It saves
them in ${XDG_CONFIG_HOME:-~/.config}/whmcs-tickets/config.json (mode 0600).
Alternatively use WHMCS_URL, WHMCS_IDENTIFIER and WHMCS_SECRET from your
environment. WHMCS_ACCESS_KEY is optional for installations configured to
use WHMCS's separate access key. The note editor uses VISUAL, EDITOR, or nano.

WHMCS permissions: GetTickets, GetTicket, and optionally AddTicketNote.
Existing notes are included in GetTicket. No public reply action exists here.
"""
import argparse
import curses
import getpass
import html
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import shlex
import ssl
import stat
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


VIEWS = ("Open", "All Active Tickets", "Awaiting Reply", "All tickets")
PAGE_SIZE = 25
MAX_RESULT = 32 * 1024 * 1024


class APIError(Exception):
    pass


class UncertainNote(APIError):
    """The request may have reached WHMCS, but was not confirmed."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def clean(value, multiline=False, decode=True):
    s = str(value) if value is not None else ""
    if decode:
        s = html.unescape(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    s = "".join(c for c in s if c == "\n" or not unicodedata.category(c).startswith("C"))
    return s if multiline else " ".join(s.split())


def crop(s, width, ellipsis=False):
    if width < 1:
        return ""
    def cells(c):
        return 0 if unicodedata.combining(c) else (2 if unicodedata.east_asian_width(c) in "WF" else 1)
    out = []
    slots = []
    used = 0
    for c in s:
        size = cells(c)
        if used + size > width:
            if ellipsis:
                while out and used >= width:
                    out.pop()
                    used -= slots.pop()
                out.append("…")
            break
        out.append(c)
        slots.append(size)
        used += size
    return "".join(out)


def safe_int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def records(value, key):
    if isinstance(value, dict):
        value = value.get(key, [value])
        if isinstance(value, dict):
            value = [value]
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def endpoint(url):
    try:
        p = urlsplit(url.strip())
        if (p.scheme != "https" or not p.hostname or p.username is not None
                or p.query or p.fragment or any(c.isspace() for c in url.strip())):
            raise ValueError
        _ = p.port
    except ValueError:
        raise APIError("Use the HTTPS WHMCS base URL or its full includes/api.php URL.") from None
    path = p.path.rstrip("/")
    if not path.endswith("/includes/api.php"):
        path += "/includes/api.php"
    return urlunsplit((p.scheme, p.netloc, path, "", ""))


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "whmcs-tickets" / "config.json"


def load_config():
    path = config_path()
    names = {"url": "WHMCS_URL", "identifier": "WHMCS_IDENTIFIER",
             "secret": "WHMCS_SECRET", "accesskey": "WHMCS_ACCESS_KEY"}
    config = {}
    if not all(os.environ.get(names[k]) for k in ("url", "identifier", "secret")):
        if path.exists():
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise APIError(f"Restrict credentials first: chmod 600 {shlex.quote(str(path))}")
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(config, dict):
                    raise ValueError
            except ValueError:
                raise APIError("Invalid credentials file. Run again with --setup.") from None
    # Keep each endpoint and its credentials together. A partial set of
    # environment overrides must not send file credentials to a different host.
    for k, env in names.items():
        if os.environ.get(env) and (k == "accesskey" or all(
                os.environ.get(names[n]) for n in ("url", "identifier", "secret"))):
            config[k] = os.environ[env]
    if not all(isinstance(config.get(k), str) and config[k].strip()
               for k in ("url", "identifier", "secret")):
        raise APIError("Run with --setup, or set WHMCS_URL, WHMCS_IDENTIFIER and WHMCS_SECRET.")
    config["url"] = endpoint(config["url"])
    return config


def setup():
    if not sys.stdin.isatty():
        raise APIError("Run --setup in a terminal so the secret can be entered without echo.")
    path = config_path()
    if path.exists() and input(f"Replace credentials in {path}? [y/N] ").strip().lower() != "y":
        print("Setup cancelled.")
        return
    url = endpoint(input("WHMCS HTTPS URL: "))
    identifier = input("API Identifier: ").strip()
    secret = getpass.getpass("API Secret (hidden): ").strip()
    if not identifier or not secret:
        raise APIError("Both an API Identifier and an API Secret are required.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(dict(url=url, identifier=identifier, secret=secret), stream, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    print(f"Saved to {path}. Run again without --setup to open the inbox.")


class WHMCS:
    def __init__(self, config):
        self.url = config["url"]
        self.host = urlsplit(self.url).netloc
        self.credentials = {k: config[k] for k in ("identifier", "secret", "accesskey") if config.get(k)}
        self.http = build_opener(NoRedirect())

    def call(self, action, **fields):
        error = UncertainNote if action == "AddTicketNote" else APIError
        data = urlencode(dict(self.credentials, action=action, responsetype="json", **fields)).encode()
        request = Request(self.url, data=data, headers={"Accept": "application/json",
                                                             "User-Agent": "whmcs-tickets/1.0"})
        try:
            with self.http.open(request, timeout=12) as resp:
                raw = resp.read(MAX_RESULT + 1)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise APIError("API redirected. Set the final HTTPS URL, including its WHMCS subdirectory.") from None
            if exc.code in (401, 403):
                raise APIError(f"HTTP {exc.code}. Check the API role and IP allowlist or web firewall.") from None
            raise error(f"WHMCS returned HTTP {exc.code}.") from None
        except (URLError, OSError, HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise APIError("TLS certificate validation failed. Check the configured WHMCS HTTPS URL.") from None
            raise error("Could not complete the request to WHMCS. Check the connection and try refreshing.") from None
        if len(raw) > MAX_RESULT:
            raise error("WHMCS response exceeded the 32 MiB size limit.")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise error("Expected JSON from WHMCS. Check the API URL and any proxy/login page.") from None
        if not isinstance(result, dict):
            raise error("WHMCS sent an unexpected response.")
        if result.get("result") == "error":
            msg = str(result.get("message") or "Request rejected.")
            for value in self.credentials.values():
                msg = msg.replace(value, "[redacted]")
            msg = clean(msg)[:240]
            raise APIError(f"{action}: {msg}")
        if result.get("result") != "success":
            raise error("WHMCS sent an unrecognised result.")
        return result

    def list(self, view, subject, page):
        params = {"limitstart": page * PAGE_SIZE, "limitnum": PAGE_SIZE}
        if view != "All tickets":
            params["status"] = view
        if subject:
            params["subject"] = subject
        data = self.call("GetTickets", **params)
        items = records(data.get("tickets"), "ticket")
        return items, safe_int(data.get("totalresults"), page * PAGE_SIZE + len(items))

    def ticket(self, *, ticketid=None, ticketnum=None):
        lookup = {"ticketid": ticketid} if ticketid is not None else {"ticketnum": ticketnum}
        return self.call("GetTicket", repliessort="ASC", **lookup)

    def note(self, ticketid, message):
        self.call("AddTicketNote", ticketid=ticketid, message=message)


class Demo:
    host = "demo · no network"

    def __init__(self):
        self.tickets = []
        subjects = ["DNS change for the new site", "Invoice shows the old company name",
                    "Contact form not sending", "Move staging site to production",
                    "SSL renewal question", "Update the opening hours"]
        for i in range(36):
            status = ("Customer-Reply", "Open", "Answered", "In Progress", "On Hold", "Closed")[i % 6]
            self.tickets.append(dict(id=i + 1, ticketid=i + 1, tid=f"CH-{831204 - i}",
                subject=subjects[i % 6], deptname="Hosting" if i % 2 == 0 else "Billing",
                deptid=1 + i % 2, requestor_name=("Jamie Reed", "Sam Patel", "Alex Chen")[i % 3],
                status=status, priority=("High", "Medium", "Low")[i % 3],
                lastreply=f"2026-11-{17 - i // 6:02} 09:{50 - i:02}:00",
                replies={"reply": [dict(replyid=0, date="2026-11-12 08:31:00",
                    message="Hi there, could you check this for us when you get a moment?\n\nThanks!"),
                    dict(replyid=1, admin="Support Team", date="2026-11-12 09:02:00",
                    message="Thanks for flagging this. We'll take a look and get back to you.")]},
                notes={"note": [dict(admin="Hosting Ops", date="2026-11-12 09:05:00",
                    message="Checked the account history. Follow up after the scheduled maintenance.")]}))

    def list(self, view, subject, page):
        items = [t for t in self.tickets if (not subject or subject.casefold() in t["subject"].casefold())
                 and (view == "All tickets" or t["status"] == view
                      or view == "All Active Tickets" and t["status"] != "Closed"
                      or view == "Awaiting Reply" and t["status"] in ("Open", "Customer-Reply"))]
        return items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], len(items)

    def ticket(self, *, ticketid=None, ticketnum=None):
        for ticket in self.tickets:
            if (ticketid is not None and ticket["id"] == ticketid
                    or ticketnum is not None and ticket["tid"].casefold() == ticketnum.casefold()):
                return ticket
        raise APIError("No demo ticket has that number. Try CH-831204.")

    def note(self, ticketid, message):
        self.ticket(ticketid=ticketid)["notes"]["note"].append(
            dict(admin="You (demo)", date="This session", message=message))


def status_style(status):
    return {"open": "green", "customer-reply": "yellow", "answered": "blue",
            "in progress": "cyan", "on hold": "magenta", "closed": "muted"}.get(status.casefold(), "text")


def priority_style(priority):
    return {"high": "red", "medium": "yellow", "low": "muted"}.get(priority.casefold(), "text")


def who(row):
    return clean(row.get("admin") or row.get("requestor_name") or row.get("name")
                 or row.get("requestor_email") or row.get("email"))


def ticket_label(row):
    num = clean(row.get("tid") or row.get("ticketid") or row.get("id"))
    return num if num.startswith("#") else "#" + num


class Inbox:
    def __init__(self, screen, api):
        self.s, self.api = screen, api
        self.items, self.total, self.page, self.selected = [], 0, 0, 0
        self.view, self.subject = 0, ""
        self.ticket = None
        self.scroll = 0
        self.tab = "thread"
        self.drafts = {}
        self.uncertain = set()
        self.notice = ("", "muted")
        self.styles = {"text": curses.A_NORMAL, "muted": curses.A_DIM,
                       "select": curses.A_REVERSE, "header": curses.A_BOLD}
        if curses.has_colors() and "NO_COLOR" not in os.environ:
            try:
                curses.start_color()
                curses.use_default_colors()
                palette = {"cyan": 6, "blue": 4, "green": 2, "yellow": 3, "red": 1, "magenta": 5}
                if curses.COLORS >= 256:
                    palette.update(cyan=81, blue=111, green=78, yellow=221, red=203, magenta=177)
                for i, (name, colour) in enumerate(palette.items(), 1):
                    curses.init_pair(i, colour, -1)
                    self.styles[name] = curses.color_pair(i) | curses.A_BOLD
                if curses.COLORS >= 256:
                    curses.init_pair(7, 231, 24)
                    self.styles["select"] = curses.color_pair(7) | curses.A_BOLD
            except curses.error:
                pass
        self.cursor(False)
        self.s.keypad(True)

    @staticmethod
    def cursor(show):
        try:
            curses.curs_set(int(show))
        except curses.error:
            pass

    def put(self, y, x, value, style="text", width=None, ellipsis=False):
        h, w = self.s.getmaxyx()
        if not (0 <= y < h and 0 <= x < w):
            return
        available = w - x - 1
        if width is not None:
            available = min(available, width)
        try:
            self.s.addstr(y, x, crop(value, available, ellipsis),
                          self.styles.get(style, curses.A_NORMAL))
        except curses.error:
            pass

    def set_notice(self, msg, style="muted"):
        self.notice = (clean(msg, decode=False), style)

    def load_list(self, page=None, view=None, subject=None):
        page = self.page if page is None else page
        view = self.view if view is None else view
        subject = self.subject if subject is None else subject
        self.set_notice("Loading tickets…", "cyan")
        self.draw()
        try:
            items, total = self.api.list(VIEWS[view], subject, page)
            last = max(0, math.ceil(total / PAGE_SIZE) - 1)
            if page > last:
                page = last
                items, total = self.api.list(VIEWS[view], subject, page)
        except APIError as exc:
            self.set_notice(str(exc), "red")
            return
        if (page, view, subject) != (self.page, self.view, self.subject):
            self.selected = 0
        self.page, self.view, self.subject = page, view, subject
        self.items, self.total = items, total
        self.selected = min(self.selected, max(0, len(items) - 1))
        self.set_notice("")

    def load_ticket(self, *, ticketid=None, ticketnum=None, refresh=False):
        self.set_notice("Loading ticket…", "cyan")
        self.draw()
        try:
            ticket = self.api.ticket(ticketid=ticketid, ticketnum=ticketnum)
        except APIError as exc:
            self.set_notice(str(exc), "red")
            return False
        self.ticket = ticket
        if not refresh:
            self.scroll, self.tab = 0, "thread"
        self.set_notice("")
        return True

    def ticket_id(self):
        return safe_int(self.ticket.get("ticketid") or self.ticket.get("id"))

    def footer(self, keys):
        h, _ = self.s.getmaxyx()
        self.put(h - 2, 2, *self.notice, ellipsis=True)
        self.put(h - 1, 2, keys, "header", ellipsis=True)

    def draw_list(self, h, w):
        pages = max(1, math.ceil(self.total / PAGE_SIZE))
        tag = f"  ·  Subject matches “{self.subject}”" if self.subject else ""
        self.put(2, 2, f"{VIEWS[self.view]}  ·  {self.total} tickets  ·  Page {self.page + 1}/{pages}{tag}",
                 "cyan", ellipsis=True)
        specs = [("Ticket", 15), ("Status", 17), ("Priority", 9)]
        if w >= 100:
            specs.append(("Last reply", 18))
        fixed = sum(size for _, size in specs)
        for i, (label, size) in enumerate(specs):
            self.put(4, 2 + sum(n for _, n in specs[:i]), label.upper(), "muted", size - 1)
        self.put(4, 2 + fixed, "SUBJECT", "muted")
        room = max(1, h - 10)
        start = min(max(0, self.selected - room + 1), max(0, len(self.items) - room))
        if not self.items:
            self.put(6, 2, "No tickets to display. Press r to reload.", "muted")
        for j, row in enumerate(self.items[start:start + room]):
            y = 5 + j
            status = clean(row.get("status")) or "—"
            priority = clean(row.get("priority")) or "—"
            marked = j + start == self.selected
            fields = [(ticket_label(row), "cyan"), (status, status_style(status)),
                      (priority, priority_style(priority))]
            if w >= 100:
                fields.append((clean(row.get("lastreply"))[:16], "muted"))
            if marked:
                self.put(y, 0, " " * (w - 1), "select")
                self.put(y, 0, "›", "select")
            x = 2
            for (value, style), (_, size) in zip(fields, specs):
                self.put(y, x, value, "select" if marked else style, size - 1, True)
                x += size
            self.put(y, x, clean(row.get("subject")) or "(No subject)",
                     "select" if marked else "text", ellipsis=True)
        if self.items:
            row = self.items[self.selected]
            dept = clean(row.get("deptname")) or ("Dept " + clean(row.get("deptid")) if row.get("deptid") else "")
            info = "  ·  ".join(x for x in (ticket_label(row), who(row), dept, clean(row.get("lastreply"))) if x)
            self.put(h - 4, 2, info, "muted", ellipsis=True)
        self.footer("↵ Open   / Search   g Go to #   f Filter   [ ] Pages   r Reload   ? Help   q Quit")

    def detail_lines(self, width):
        ticket = self.ticket
        rows = records(ticket.get("notes"), "note") if self.tab == "notes" else records(ticket.get("replies"), "reply")
        if self.tab == "thread" and not any(str(row.get("replyid")) == "0" for row in rows) and ticket.get("message"):
            rows.insert(0, ticket)
        if not rows:
            return [("No private notes yet." if self.tab == "notes" else "No messages returned.", "muted")]
        lines = []
        for row in rows:
            if self.tab == "notes":
                badge, style = "PRIVATE NOTE", "magenta"
            elif str(row.get("replyid")) == "0" or row is ticket:
                badge, style = "ORIGINAL", "cyan"
            else:
                badge, style = ("STAFF", "blue") if row.get("admin") or row.get("requestor_type") == "Operator" else ("REPLY", "green")
            author = who(row) or (who(ticket) if badge == "ORIGINAL" else "") or "Unknown"
            lines.append((f"{badge}  ·  {author}  ·  {clean(row.get('date'))}", style))
            lines.extend((line, "text") for line in wrapped(clean(row.get("message"), multiline=True) or "(Empty message)", width))
            files = [clean(a.get("filename")) for a in records(row.get("attachments"), "attachment")
                     if a.get("filename")]
            if not files and row.get("attachment"):
                files = [clean(name) for name in str(row["attachment"]).split("|") if name]
            if files:
                lines.extend((line, "muted") for line in wrapped("Attachments · " + ", ".join(files), width))
            lines.append(("", "text"))
        return lines

    def draw_ticket(self, h, w):
        t = self.ticket
        self.put(2, 2, f"{ticket_label(t)}   {clean(t.get('subject')) or '(No subject)'}", "header", ellipsis=True)
        status = clean(t.get("status")) or "—"
        priority = clean(t.get("priority")) or "—"
        self.put(3, 2, status, status_style(status), 18, True)
        self.put(3, 22, f"Priority {priority}", priority_style(priority), 17, True)
        self.put(3, 40, "  ·  ".join(x for x in (clean(t.get("deptname")), who(t)) if x), "muted", ellipsis=True)
        self.put(5, 2, " THREAD ", "select" if self.tab == "thread" else "muted")
        self.put(5, 13, f" PRIVATE NOTES ({len(records(t.get('notes'), 'note'))}) ",
                 "select" if self.tab == "notes" else "magenta")
        lines = self.detail_lines(w - 5)
        room = max(1, h - 10)
        self.scroll = max(0, min(self.scroll, len(lines) - room))
        for y, (line, style) in enumerate(lines[self.scroll:self.scroll + room], 7):
            self.put(y, 2, line, style, ellipsis=True)
        self.put(h - 3, 2, f"Lines {self.scroll + 1}–{min(len(lines), self.scroll + room)} of {len(lines)}", "muted")
        self.footer("Tab Thread/Notes   ↑↓ Scroll   n Private note   r Reload   Esc Back   ? Help")

    def draw(self):
        self.s.erase()
        h, w = self.s.getmaxyx()
        self.put(0, 2, "WHMCS  /  TICKETS", "cyan")
        if w >= 44:
            host = crop(clean(self.api.host, decode=False), min(36, w - 25), True)
            self.put(0, max(23, w - len(host) - 2), host, "muted")
        if h < 14 or w < 55:
            self.put(2, 2, "Resize the terminal to at least 55 × 14.", "yellow")
        elif self.ticket is None:
            self.draw_list(h, w)
        else:
            self.draw_ticket(h, w)
        self.s.refresh()

    def prompt(self, label, initial=""):
        value = initial
        self.cursor(True)
        try:
            while True:
                h, w = self.s.getmaxyx()
                self.s.move(max(0, h - 2), 0)
                self.s.clrtoeol()
                prefix = "  " + crop(label, max(8, w // 2), True) + " › "
                room = max(1, w - len(prefix) - 4)
                tail = value[-room:]
                if tail != value:
                    tail = "…" + tail
                self.put(h - 2, 0, prefix + tail, "cyan")
                _, cursor_x = self.s.getyx()
                self.s.move(max(0, h - 2), min(w - 1, cursor_x))
                self.s.refresh()
                key = self.s.get_wch()
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return value.strip()
                if key == "\x1b":
                    return None
                if key in ("\x7f", "\b", curses.KEY_BACKSPACE):
                    value = value[:-1]
                elif key == "\x15":
                    value = ""
                elif isinstance(key, str) and key.isprintable() and len(value) < 200:
                    value += key
        finally:
            self.cursor(False)

    def help(self):
        lines = [
            ("INBOX", "cyan"), ("↑ / ↓ or j / k  Choose ticket     Enter  Open", "text"),
            ("[ and ]  Previous / next API page", "text"),
            ("/  Subject search (Ctrl-U clears)   g  Go to visible ticket #", "text"),
            ("f  Cycle Open, Active, Awaiting Reply, All   r  Refresh", "text"),
            ("TICKET", "cyan"), ("Tab  Switch thread / private notes", "text"),
            ("↑ / ↓  Scroll   PgUp / PgDn   Home / End", "text"),
            ("n  Write private note using your editor   Esc  Back", "text"),
        ]
        self.dialog("Keyboard shortcuts", lines, "Press Esc, Enter or ? to close.", {"\x1b", "\n", "\r", "?", "q"})

    def dialog(self, title, lines, prompt, keys):
        offset = 0
        while True:
            self.s.erase()
            h, _ = self.s.getmaxyx()
            self.put(1, 2, title, "magenta", ellipsis=True)
            room = max(1, h - 5)
            offset = max(0, min(offset, len(lines) - room))
            for y, (line, style) in enumerate(lines[offset:offset + room], 3):
                self.put(y, 2, line, style, ellipsis=True)
            self.put(h - 2, 2, prompt, "header", ellipsis=True)
            self.s.refresh()
            key = self.s.get_wch()
            if key in keys:
                return key
            if key in (curses.KEY_UP, "k"):
                offset -= 1
            elif key in (curses.KEY_DOWN, "j"):
                offset += 1
            elif key == curses.KEY_PPAGE:
                offset -= room
            elif key == curses.KEY_NPAGE:
                offset += room

    def edit_note(self):
        ticketid = self.ticket_id()
        if ticketid < 1:
            self.set_notice("WHMCS did not return an internal ticket ID. Notes disabled for this ticket.", "red")
            return
        if ticketid in self.uncertain:
            self.set_notice("Previous save is unconfirmed. Inspect Private Notes before attempting another save.", "yellow")
            confirm = self.dialog("Previous save is unconfirmed", [
                ("Open or refresh Private Notes first to check whether the last draft was saved.", "yellow"),
                ("If the note is already there, do not send it again.", "text")],
                "e  Continue to editor   Esc  Cancel", {"e", "\x1b"})
            if confirm != "e":
                return
        try:
            args = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano")
            with tempfile.TemporaryDirectory(prefix="whmcs-note-") as directory:
                path = Path(directory) / "private-note.txt"
                path.touch(mode=0o600)
                path.write_text(self.drafts.get(ticketid, ""), encoding="utf-8")
                curses.def_prog_mode()
                curses.endwin()
                try:
                    print(f"Private note for {ticket_label(self.ticket)}. Save and exit the editor to review.", flush=True)
                    result = subprocess.run([*args, str(path)], check=False) if args else None
                finally:
                    curses.reset_prog_mode()
                    self.s.clear()
                    self.s.refresh()
                    self.cursor(False)
                message = path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            self.set_notice("Could not open the note editor. Set VISUAL or EDITOR to nano, vim, etc.", "red")
            return
        if result is None or result.returncode:
            if message:
                self.drafts[ticketid] = message
            self.set_notice("Editor did not exit cleanly. Nothing sent.", "yellow")
            return
        if not message:
            self.drafts.pop(ticketid, None)
            self.set_notice("Empty note. Nothing sent.")
            return
        self.drafts[ticketid] = message
        preview = [(line, "text") for line in wrapped(clean(message, multiline=True, decode=False),
                                                     max(10, self.s.getmaxyx()[1] - 5))]
        key = self.dialog(f"PRIVATE NOTE  /  {ticket_label(self.ticket)}", preview,
                          "y  Save internal note   Esc  Keep draft   ↑↓ Scroll", {"y", "\x1b"})
        if key != "y":
            self.set_notice("Nothing sent. Draft kept in memory for this session.", "yellow")
            return
        self.set_notice("Saving private note…", "magenta")
        self.draw()
        try:
            self.api.note(ticketid, message)
        except UncertainNote:
            self.uncertain.add(ticketid)
            self.set_notice("Save unconfirmed. Refresh Private Notes before retrying. Draft kept.", "yellow")
            return
        except APIError as exc:
            self.set_notice(str(exc) + "  ·  Draft kept.", "red")
            return
        self.drafts.pop(ticketid, None)
        self.uncertain.discard(ticketid)
        self.tab, self.scroll = "notes", 10**8
        if self.load_ticket(ticketid=ticketid, refresh=True):
            self.set_notice("Private note saved.", "green")
        else:
            self.set_notice("Private note saved. Could not reload the ticket. Press r to refresh.", "yellow")

    def run(self):
        self.load_list()
        while True:
            self.draw()
            key = self.s.get_wch()
            if key == "?":
                self.help()
                continue
            if self.ticket is not None:
                h, _ = self.s.getmaxyx()
                if key in ("\x1b", "b", "q"):
                    self.ticket = None
                    self.set_notice("")
                elif key in ("\t", curses.KEY_BTAB):
                    self.tab = "thread" if self.tab == "notes" else "notes"
                    self.scroll = 0
                elif key in (curses.KEY_DOWN, "j", curses.KEY_UP, "k", curses.KEY_NPAGE, curses.KEY_PPAGE):
                    step = max(1, h - 11) if key in (curses.KEY_NPAGE, curses.KEY_PPAGE) else 1
                    self.scroll += step if key in (curses.KEY_DOWN, "j", curses.KEY_NPAGE) else -step
                elif key == curses.KEY_HOME:
                    self.scroll = 0
                elif key == curses.KEY_END:
                    self.scroll = 10**8
                elif key == "r" and self.ticket_id():
                    self.load_ticket(ticketid=self.ticket_id(), refresh=True)
                elif key == "n":
                    self.edit_note()
                continue
            if key in ("q", "\x1b"):
                if self.drafts and self.dialog("Discard unsent drafts?", [
                        ("Private note drafts live only in this session.", "yellow")],
                        "y  Discard drafts and quit   Esc  Stay", {"y", "\x1b"}) != "y":
                    continue
                return
            if key in (curses.KEY_UP, "k") and self.items:
                self.selected = max(0, self.selected - 1)
            elif key in (curses.KEY_DOWN, "j") and self.items:
                self.selected = min(len(self.items) - 1, self.selected + 1)
            elif key == curses.KEY_HOME:
                self.selected = 0
            elif key == curses.KEY_END and self.items:
                self.selected = len(self.items) - 1
            elif key in ("\n", "\r", curses.KEY_ENTER) and self.items:
                row = self.items[self.selected]
                ticketid = safe_int(row.get("id") or row.get("ticketid"))
                if ticketid:
                    self.load_ticket(ticketid=ticketid)
                elif row.get("tid"):
                    self.load_ticket(ticketnum=str(row["tid"]))
            elif key == "g":
                number = self.prompt("Visible ticket number")
                if number and number.lstrip("#").strip():
                    self.load_ticket(ticketnum=number.lstrip("#").strip())
            elif key == "/":
                query = self.prompt("Subject search", self.subject)
                if query is not None:
                    self.load_list(page=0, subject=query)
            elif key == "f":
                self.load_list(page=0, view=(self.view + 1) % len(VIEWS))
            elif key in ("[", curses.KEY_PPAGE) and self.page:
                self.load_list(page=self.page - 1)
            elif key in ("]", curses.KEY_NPAGE) and (self.page + 1) * PAGE_SIZE < self.total:
                self.load_list(page=self.page + 1)
            elif key == "r":
                self.load_list()


def wrapped(s, width):
    # Wrap on words where possible. Rewrap wide characters by terminal cells.
    for paragraph in s.split("\n"):
        for line in textwrap.wrap(paragraph, max(1, width), replace_whitespace=False,
                                  break_on_hyphens=False) or [""]:
            while line:
                part = crop(line, width)
                if not part:
                    break
                yield part
                line = line[len(part):]
            if line == "" and not paragraph.strip():
                yield ""


def main():
    parser = argparse.ArgumentParser(description="Read WHMCS tickets and add staff-only notes from a Linux terminal.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup", action="store_true", help="set up your HTTPS URL, API Identifier and API Secret")
    mode.add_argument("--demo", action="store_true", help="try the interface offline with sample tickets and notes")
    args = parser.parse_args()
    try:
        if args.setup:
            setup()
        else:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise APIError("Open this app in an interactive Linux terminal.")
            api = Demo() if args.demo else WHMCS(load_config())
            curses.wrapper(lambda screen: Inbox(screen, api).run())
    except (KeyboardInterrupt, EOFError):
        return 130
    except (APIError, OSError, curses.error) as exc:
        print(f"whmcs-tickets: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
