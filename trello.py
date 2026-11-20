#!/usr/bin/env python3
"""Trello Cards — a Linux terminal inbox. Python 3.9+, no pip packages.

Try:    python3 trello-cards.py --demo
Setup:  python3 trello-cards.py --setup
Launch: python3 trello-cards.py
Pin:    python3 trello-cards.py --board "Support" --list "To Do"

Setup takes an API key from https://trello.com/apps/admin, gives you a
Trello authorization URL, and asks for the resulting user token. Choose
read-only or read/write. The credentials file is private (mode 0600).
Set TRELLO_KEY and TRELLO_TOKEN together to use environment credentials.
With environment credentials posting is opt-in via --enable-comments.
Use --read-only to prevent posting with any credentials.

Downloads default to the XDG Downloads folder or ~/Downloads. Override with
--download-dir. VISUAL or EDITOR selects your comment editor (default nano).
"""
import argparse
import curses
from datetime import datetime, timezone
from http.client import HTTPException
import html
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import getpass

API = "https://api.trello.com/1"
ACTIVITY_LIMIT = 50
TABS = ("Description", "Comments", "Attachments")
ENTER = ("\n", "\r", curses.KEY_ENTER)
ESC = "\x1b"
UP = ("k", curses.KEY_UP)
DOWN = ("j", curses.KEY_DOWN)
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]{1,512}")
TRELLO_ID = re.compile(r"[A-Za-z0-9]{1,64}")
COLOR = {"green": "green", "yellow": "yellow", "orange": "yellow",
         "red": "red", "purple": "magenta", "blue": "blue", "sky": "cyan",
         "lime": "green", "pink": "magenta", "black": "muted"}

class APIError(Exception):
    pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def clean(value, multiline=False, decode=False):
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


class Unconfirmed(APIError):
    pass


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "trello-cards"


def save_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".trello-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def read_json(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected an object.")
    return value


def valid_token(s):
    return isinstance(s, str) and TOKEN_PATTERN.fullmatch(s) is not None


def setup():
    path = config_dir() / "config.json"
    if not sys.stdin.isatty():
        raise APIError("Run --setup in an interactive terminal.")
    if path.exists() and input("Replace existing Trello credentials? [y/N] ").strip().lower() != "y":
        return
    key = input("Trello API key: ").strip()
    if not valid_token(key):
        raise APIError("That doesn't look like a Trello API key.")
    write = input("Enable posting card comments? [y/N] ").strip().lower() == "y"
    params = urlencode(dict(key=key, name="Trello Cards Console", expiration="30days",
                            response_type="token", scope="read,write" if write else "read"))
    print("\nOpen this in your browser, allow access, and copy the token Trello displays.\n")
    print("https://trello.com/1/authorize?" + params + "\n")
    token = getpass.getpass("Trello user token (hidden): ").strip()
    if not valid_token(token):
        raise APIError("That doesn't look like a Trello user token.")
    Trello(key, token).boards()  # Validate before replacing existing credentials.
    save_json(path, dict(key=key, token=token, write=write))
    print("Credentials saved. Run again without --setup to open Trello Cards.")


def credentials(args):
    key, token = os.environ.get("TRELLO_KEY"), os.environ.get("TRELLO_TOKEN")
    if key is not None or token is not None:
        cfg = dict(key=key, token=token, write=args.enable_comments)
    else:
        path = config_dir() / "config.json"
        if not path.exists():
            raise APIError("Run with --setup or set TRELLO_KEY and TRELLO_TOKEN.")
        if not stat.S_ISREG(path.stat().st_mode) or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise APIError("Trello credentials must be a private file (chmod 600).")
        try:
            cfg = read_json(path)
        except ValueError:
            raise APIError("Invalid credentials file. Run --setup again.") from None
    if not valid_token(cfg.get("key")) or not valid_token(cfg.get("token")):
        raise APIError("A valid Trello API key and user token are both required.")
    return Trello(cfg["key"], cfg["token"], cfg.get("write") is True and not args.read_only)


def ident(value):
    if not isinstance(value, str) or not TRELLO_ID.fullmatch(value):
        raise APIError("Invalid Trello ID.")
    return value


def card_ref(value):
    if value.startswith(("https://", "http://")):
        p = urlsplit(value)
        match = re.fullmatch(r"/c/([A-Za-z0-9]+)(?:/.*)?", p.path)
        if p.scheme != "https" or p.netloc.lower() not in ("trello.com", "www.trello.com") or not match:
            raise APIError("Use a Trello card URL, short ID, or #number on this board.")
        value = match[1]
    return ident(value)


def child_environment():
    return {k: v for k, v in os.environ.items() if k not in ("TRELLO_KEY", "TRELLO_TOKEN")}


def download_dir():
    if shutil.which("xdg-user-dir"):
        try:
            result = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True,
                                    text=True, timeout=2, check=True, env=child_environment())
            path = Path(result.stdout.strip())
            if path.is_absolute():
                return path
        except (OSError, subprocess.SubprocessError):
            pass
    return Path.home() / "Downloads"


def safe_filename(name):
    name = clean(name).replace("\\", "/").rsplit("/", 1)[-1].strip(" .")
    name = "".join(c for c in name if c not in "\x00/\\")
    while len(name.encode("utf-8")) > 160:
        name = name[:-1]
    return name or "attachment"


def save_download(directory, name, source):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = Path(safe_filename(name))
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".trello-download-")
    try:
        with os.fdopen(fd, "wb") as stream:
            remaining = 512 * 1024 * 1024
            written = 0
            while True:
                block = source.read(128 * 1024)
                if not block:
                    break
                remaining -= len(block)
                if remaining < 0:
                    raise APIError("Attachment is over this client's 512 MiB limit.")
                stream.write(block)
                written += len(block)
            expected = getattr(source, "headers", {}).get("Content-Length", "")
            if expected.isdecimal() and len(expected) < 16 and written != int(expected):
                raise APIError("Attachment download was incomplete.")
        for i in range(10_000):
            chosen = directory / (base.name if i == 0 else f"{base.stem} ({i}){base.suffix}")
            try:
                os.link(temp, chosen)  # Publish a complete file without replacing an existing one.
                return chosen
            except FileExistsError:
                continue
        raise APIError("Too many files with that name in the download folder.")
    finally:
        Path(temp).unlink(missing_ok=True)


class Trello:
    demo = False

    def __init__(self, key, token, write=False):
        self.auth = f'OAuth oauth_consumer_key="{key}", oauth_token="{token}"'
        self.write = write
        self.http = build_opener(NoRedirect())

    def json(self, path, params=None, data=None):
        url = API + path + ("?" + urlencode(params) if params else "")
        request = Request(url, data=urlencode(data).encode() if data is not None else None,
                          headers={"Authorization": self.auth, "Accept": "application/json",
                                   "User-Agent": "trello-cards-console/1.0"})
        uncertain = Unconfirmed if data is not None else APIError
        try:
            with self.http.open(request, timeout=15) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 401 or status == 403:
                raise APIError(f"Trello denied access ({status}). Check the token, board access and comment permissions.") from None
            if status == 404:
                raise APIError("Trello couldn't find that board or card, or your account can't see it.") from None
            if status == 429:
                raise APIError("Trello rate limit reached. Wait a moment and try again.") from None
            if 400 <= status < 500:
                raise APIError(f"Trello rejected the request (HTTP {status}).") from None
            raise uncertain(f"Trello returned HTTP {status}.") from None
        except (HTTPException, URLError, OSError):
            raise uncertain("The connection to Trello did not complete.") from None
        if len(raw) > 32 * 1024 * 1024:
            raise uncertain("Trello's response exceeded the size limit.")
        try:
            value = json.loads(raw)
        except ValueError:
            raise uncertain("Trello returned an unreadable response.") from None
        if not isinstance(value, (dict, list)):
            raise uncertain("Trello returned an unexpected response.")
        return value

    def boards(self):
        return rows(self.json("/members/me/boards", dict(filter="open", fields="name,closed,starred,shortLink,shortUrl,desc")))

    def board(self, board_id):
        path = f"/boards/{ident(board_id)}"
        lists = rows(self.json(path + "/lists", dict(filter="open", fields="name,closed,pos")))
        cards = rows(self.json(path + "/cards", dict(filter="open", fields="name,idList,idBoard,idShort,shortLink,shortUrl,labels,due,dueComplete,pos,closed")))
        return lists, cards

    def card(self, card_id):
        path = f"/cards/{ident(card_id)}"
        card = self.json(path, dict(fields="name,desc,idList,idBoard,idShort,shortLink,shortUrl,labels,due,dueComplete,closed"))
        if not isinstance(card, dict) or not card.get("id"):
            raise APIError("Trello didn't return a card.")
        return (card,
                rows(self.json(path + "/actions", dict(filter="commentCard", limit=100))),
                rows(self.json(path + "/attachments")))

    def activity(self, kind, target):
        if kind not in ("boards", "cards"):
            raise APIError("Invalid activity scope.")
        return rows(self.json(f"/{kind}/{ident(target)}/actions",
                              dict(filter="all", limit=ACTIVITY_LIMIT, memberCreator="true")))

    def comment(self, card_id, message):
        if not self.write:
            raise APIError("Comment posting is disabled.")
        result = self.json(f"/cards/{ident(card_id)}/actions/comments", data=dict(text=message))
        if not isinstance(result, dict) or not result.get("id"):
            raise Unconfirmed("Trello didn't return a comment ID.")

    def download(self, card, attachment, directory):
        if attachment.get("isUpload") is not True:
            raise APIError("That attachment is an external link.")
        name = filename(attachment)
        if name in (".", ".."):
            raise APIError("Trello returned an invalid attachment filename.")
        url = API + f"/cards/{ident(card['id'])}/attachments/{ident(attachment.get('id'))}/download/{quote(name, safe='')}"
        for hop in range(6):
            # The token goes only to the Trello API, never to a redirect target.
            headers = {"Authorization": self.auth} if hop == 0 else {}
            try:
                with self.http.open(Request(url, headers=headers), timeout=20) as response:
                    return save_download(directory, name, response)
            except HTTPError as exc:
                if exc.code not in (301, 302, 303, 307, 308) or not exc.headers.get("Location"):
                    status = exc.code
                    exc.close()
                    raise APIError(f"Trello attachment download failed (HTTP {status}).") from None
                url = urljoin(url, exc.headers["Location"])
                exc.close()
                p = urlsplit(url)
                if p.scheme != "https" or not p.hostname or p.username is not None:
                    raise APIError("Trello gave an invalid download redirect.") from None
            except (URLError, HTTPException):
                raise APIError("The attachment download connection did not complete.") from None
        raise APIError("Too many attachment redirects.")


def rows(value):
    if not isinstance(value, list):
        raise APIError("Trello did not return the expected list.")
    return [r for r in value if isinstance(r, dict)]


def filename(attachment):
    name = attachment.get("fileName")
    if not name:
        try:
            path = urlsplit(str(attachment.get("url") or "")).path
        except ValueError:
            path = ""
        name = unquote(path.split("/download/", 1)[1]) if "/download/" in path else attachment.get("name")
    return str(name or "attachment")


class Demo:
    demo = True
    write = True

    def __init__(self):
        self._boards = [dict(id="support01", name="Hosting Support", starred=True, shortLink="support01"),
                        dict(id="studio02", name="Website Projects", shortLink="studio02"),
                        dict(id="ops00003", name="Operations", shortLink="ops00003")]
        names = [
            ["Check DNS for the staging site", "Investigate contact form delivery",
             "Review the renewal list", "Update hosting docs", "Confirm SSL cutover"],
            ["Review homepage mockup", "Audit mobile spacing", "Prepare launch checklist",
             "Swap out hero photo", "Review proof from design"],
            ["Review weekly deployment notes", "Check backup retention",
             "Schedule maintenance window", "Rotate dashboard credentials"],
        ]
        self._lists, self._cards, self._comments = {}, {}, {}
        for i, board in enumerate(self._boards):
            bid = board["id"]
            self._lists[bid] = [dict(id=f"demo{i}list{j}", name=name, pos=j)
                                for j, name in enumerate(("To Do", "In Progress", "Done"))]
            self._cards[bid] = []
            for j, title in enumerate(names[i], 1):
                cid = f"demo{i}card{j}"
                self._cards[bid].append(dict(id=cid, idBoard=bid, idShort=j,
                    shortLink=cid, idList=self._lists[bid][(j - 1) % 3]["id"], name=title,
                    desc="## Context\n\nThis is a sample Trello card. Nothing in demo mode hits the network.\n\n"
                         "Use Tab for comments and attachments. You can post a demo comment or download the sample file.",
                    labels=[dict(name=("Support", "Review", "Ready")[j % 3],
                                 color=("green", "yellow", "blue")[j % 3])],
                    due="2026-11-25T12:00:00.000Z" if j == 1 else None, pos=j))
                self._comments[cid] = [dict(id=f"comment{i}{j}", date="2026-11-18T09:00:00Z",
                                            memberCreator=dict(fullName="Alex Reed"),
                                            data=dict(text="I added the background and a sample attachment."))]

    def boards(self):
        return self._boards

    def board(self, board_id):
        return self._lists[board_id], self._cards[board_id]

    def card(self, card_id):
        for cards in self._cards.values():
            for card in cards:
                if card["id"] == card_id:
                    return card, list(self._comments[card_id]), [
                        dict(id="sample", isUpload=True, name="sample-notes.txt", fileName="sample-notes.txt", bytes=52)]
        raise APIError("No demo card matched. Try #1 on the selected board.")

    def activity(self, kind, target):
        cards = (self._cards.get(target, []) if kind == "boards" else
                 [c for group in self._cards.values() for c in group if c["id"] == target])
        events = []
        for i, card in enumerate(cards):
            card_id = card["id"]
            info = {k: card[k] for k in ("id", "idShort", "name", "shortLink")}
            person = dict(fullName="Alex Reed")
            events += [
                dict(id=card_id + "move", type="updateCard", date=f"2026-11-18T1{4 - i % 4}:10:00Z",
                     memberCreator=person, data=dict(card=info, listBefore=dict(name="Inbox"),
                         listAfter=dict(name=next(title(l) for l in self._lists[card["idBoard"]]
                                                  if l["id"] == card["idList"])))),
                dict(id=card_id + "file", type="addAttachmentToCard", date="2026-11-18T08:45:00Z",
                     memberCreator=person, data=dict(card=info, attachment=dict(name="sample-notes.txt"))),
                dict(id=card_id + "new", type="createCard", date="2026-11-17T08:45:00Z",
                     memberCreator=person, data=dict(card=info)),
            ]
            for comment in self._comments[card_id]:
                events.append(dict(comment, type="commentCard", data=dict(comment["data"], card=info)))
        return sorted(events, key=lambda e: e["date"], reverse=True)[:ACTIVITY_LIMIT]

    def comment(self, card_id, message):
        self._comments[card_id].insert(0, dict(id=f"{card_id}post{len(self._comments[card_id])}",
            date=datetime.now(timezone.utc).isoformat(), memberCreator=dict(fullName="You (demo)"),
            data=dict(text=message)))

    def download(self, card, attachment, directory):
        from io import BytesIO
        return save_download(directory, filename(attachment),
                             BytesIO(b"Sample file from the offline Trello Cards demo.\n"))


def label_text(card):
    for label in card.get("labels") or []:
        if isinstance(label, dict):
            name = clean(label.get("name") or label.get("color"))
            if name:
                yield name, COLOR.get(str(label.get("color")).split("_", 1)[0], "cyan")


def pos(row):
    return row.get("pos") if isinstance(row.get("pos"), (int, float)) else 0


def title(row):
    return clean(row.get("name")) or "(Untitled)"


def file_size(value):
    if not isinstance(value, int):
        return ""
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value} B"

def object_field(value):
    return value if isinstance(value, dict) else {}


def event_parts(event):
    data = object_field(event.get("data"))
    card = object_field(data.get("card"))
    member = object_field(event.get("memberCreator"))
    author = clean(member.get("fullName") or member.get("username")) or "Unknown author"
    date = clean(event.get("date"))
    if date:
        try:
            date = datetime.fromisoformat(date.replace("Z", "+00:00")).astimezone().strftime("%d %b %H:%M")
        except ValueError:
            date = date[:16]
    else:
        date = "Unknown date"
    kind = clean(event.get("type"))
    details = ""
    label = {
        "createCard": "Created card", "copyCard": "Copied card",
        "commentCard": "Commented", "updateComment": "Edited comment",
        "deleteCard": "Deleted card", "addAttachmentToCard": "Added attachment",
        "deleteAttachmentFromCard": "Removed attachment",
        "addMemberToCard": "Added member", "removeMemberFromCard": "Removed member",
        "addLabelToCard": "Added label", "removeLabelFromCard": "Removed label",
        "moveCardFromBoard": "Moved card off board", "moveCardToBoard": "Moved card onto board",
        "updateCard": "Updated card", "updateList": "Updated list",
        "addChecklistToCard": "Added checklist", "removeChecklistFromCard": "Removed checklist",
        "createList": "Created list", "updateCheckItemStateOnCard": "Updated checklist item",
    }.get(kind) or (re.sub(r"(?<=[a-z])(?=[A-Z])", " ", kind).capitalize() if kind else "Activity")
    if kind in ("commentCard", "updateComment"):
        details = clean(data.get("text"), multiline=True)
    elif "Attachment" in kind:
        details = clean(object_field(data.get("attachment")).get("name"))
    elif kind == "updateCard":
        before = clean(object_field(data.get("listBefore")).get("name"))
        after = clean(object_field(data.get("listAfter")).get("name"))
        old = object_field(data.get("old"))
        if before or after:
            label, details = "Moved card", f"{before or 'Unknown list'} → {after or 'Unknown list'}"
        elif "closed" in old and "closed" in card:
            label = "Archived card" if card["closed"] else "Unarchived card"
        elif "name" in old:
            label, details = "Renamed card", clean(old["name"]) + " → " + title(card)
        elif "desc" in old:
            label = "Edited description"
        elif "due" in old:
            label, details = "Changed due date", clean(card.get("due"))[:10] or "No due date"
        elif "dueComplete" in old and "dueComplete" in card:
            label = "Marked due date complete" if card["dueComplete"] else "Reopened due date"
        elif "pos" in old:
            label = "Reordered card"
    elif kind in ("addMemberToCard", "removeMemberFromCard"):
        person = object_field(event.get("member")) or object_field(data.get("member"))
        details = clean(person.get("fullName") or person.get("username"))
    else:
        for field in ("checkItem", "checklist", "label", "list"):
            details = clean(object_field(data.get(field)).get("name"))
            if details:
                break
    return author, date, label, details, card


class Inbox:
    def __init__(self, screen, api):
        self.s, self.api = screen, api
        self.boards, self.lists, self.cards = [], [], []
        self.board = None
        self.list_id = None
        self.query = ""
        self.selected = self.top = 0
        self.card = None
        self.comments, self.attachments = [], []
        self.scroll = self.attachment = self.tab = 0
        self.notice = ("", "muted")
        self.drafts, self.unconfirmed = {}, set()
        self.board_filters = {}
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

    def footer(self, keys):
        h, _ = self.s.getmaxyx()
        self.put(h - 2, 2, *self.notice, ellipsis=True)
        self.put(h - 1, 2, keys, "header", ellipsis=True)

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

    def reload_boards(self):
        items = self.api.boards()
        self.boards = sorted((b for b in items if b.get("id") and not b.get("closed")),
                             key=lambda b: (not b.get("starred", False), title(b).casefold(), b["id"]))

    def filtered(self):
        needle = self.query.casefold()
        return [c for c in self.cards if (self.list_id is None or c.get("idList") == self.list_id)
                and needle in title(c).casefold()]

    def load_board(self, board, wanted_list=None, reset=True):
        self.set_notice("Loading board…", "cyan")
        self.draw()
        try:
            lists, cards = self.api.board(board["id"])
        except APIError as exc:
            self.set_notice(str(exc), "red")
            return False
        old = self.filtered()
        old_id = old[self.selected].get("id") if self.selected < len(old) and not reset else None
        self.board = board
        self.lists = sorted((l for l in lists if l.get("id") and not l.get("closed")), key=pos)
        order = {l["id"]: i for i, l in enumerate(self.lists)}
        self.cards = sorted((c for c in cards if c.get("id") and not c.get("closed") and c.get("idList") in order),
                            key=lambda c: (order[c["idList"]], pos(c)))
        self.list_id = wanted_list if wanted_list in order else None
        if reset:
            self.query = ""
            self.selected = self.top = 0
        else:
            visible = self.filtered()
            self.selected = next((i for i,c in enumerate(visible) if c.get("id") == old_id),
                                 min(self.selected, max(0, len(visible) - 1)))
        self.set_notice("")
        return True

    def open_card(self, cid, refresh=False):
        self.set_notice("Loading card…", "cyan")
        self.draw()
        try:
            card, comments, attachments = self.api.card(cid)
        except APIError as exc:
            self.set_notice(str(exc), "red")
            return False
        self.card, self.comments, self.attachments = card, comments, attachments
        if not refresh:
            self.tab = self.scroll = self.attachment = 0
        self.attachment = min(self.attachment, max(0, len(attachments) - 1))
        self.set_notice("")
        return True

    def list_name(self, lid):
        return next((title(l) for l in self.lists if l["id"] == lid), "")

    def draw(self):
        self.s.erase()
        h, w = self.s.getmaxyx()
        self.put(0, 2, "TRELLO  /  CARDS", "cyan")
        mode = "OFFLINE DEMO" if self.api.demo else ("COMMENTS ENABLED" if self.api.write else "READ ONLY")
        if w >= 55:
            self.put(0, w - len(mode) - 2, mode, "muted")
        if h < 14 or w < 55:
            self.put(3, 2, "Resize the terminal to at least 55 × 14.", "yellow")
        elif self.card is not None:
            self.draw_card(h, w)
        else:
            self.draw_board(h, w)
        self.s.refresh()

    def draw_board(self, h, w):
        cards = self.filtered()
        self.put(2, 2, title(self.board) if self.board else "No board selected", "header", ellipsis=True)
        query = f"  ·  Search: {self.query}" if self.query else ""
        self.put(3, 2, f"{self.list_name(self.list_id) or 'All lists'}  ·  {len(cards)} cards{query}",
                 "cyan", ellipsis=True)
        list_w = 19 if w >= 74 else 13
        self.put(5, 2, "CARD", "muted")
        self.put(5, 10, "LIST", "muted")
        self.put(5, 10 + list_w, "TITLE", "muted")
        room = h - 11
        self.top = min(max(0, self.top), max(0, len(cards) - room))
        if self.selected < self.top:
            self.top = self.selected
        elif self.selected >= self.top + room:
            self.top = self.selected - room + 1
        if not cards:
            self.put(7, 2, "No cards here. Press b for boards or f for lists.", "muted", ellipsis=True)
        for i, card in enumerate(cards[self.top:self.top + room], self.top):
            y = 6 + i - self.top
            selected = i == self.selected
            style = "select" if selected else "text"
            if selected:
                self.put(y, 0, " " * (w - 1), "select")
                self.put(y, 0, "›", "select")
            self.put(y, 2, f"#{clean(card.get('idShort'))}", style if selected else "cyan", 7, True)
            self.put(y, 10, self.list_name(card.get("idList")), style if selected else "muted", list_w - 1, True)
            self.put(y, 10 + list_w, title(card), style, ellipsis=True)
        if cards:
            card = cards[self.selected]
            self.put(h - 4, 2, f"{self.selected + 1}/{len(cards)}", "muted")
            x = 15
            due = clean(card.get("due"))[:10]
            if due:
                info = f"Due {due}" + (" ✓" if card.get("dueComplete") else "")
                self.put(h - 4, x, info, "green" if card.get("dueComplete") else "yellow")
                x += len(info) + 3
            for name, style in label_text(card):
                self.put(h - 4, x, name, style, ellipsis=True)
                x += len(name) + 3
        self.footer("↵ Open  b Boards  a Activity  f Lists  / Search  g Go to  r Reload  ? Help  q Quit")

    def draw_card(self, h, w):
        c = self.card
        marker = "#" + clean(c.get("idShort"))
        self.put(2, 2, f"{marker}   {title(c)}", "header", ellipsis=True)
        board = next((b for b in self.boards if b["id"] == c.get("idBoard")), None)
        list_name = self.list_name(c.get("idList"))
        details = "  ·  ".join(filter(None, (title(board) if board else "", list_name,
                    "Archived" if c.get("closed") else "",
                    ("Due " + clean(c.get("due"))[:10] + (" ✓" if c.get("dueComplete") else "")) if c.get("due") else "",
                    ", ".join(name for name, _ in label_text(c)))))
        self.put(3, 2, details, "muted", ellipsis=True)
        self.put(5, 2, " Description ", "select" if self.tab == 0 else "cyan")
        self.put(5, 17, f" Comments ({len(self.comments)}) ", "select" if self.tab == 1 else "blue")
        self.put(5, 35, f" Attachments ({len(self.attachments)}) ", "select" if self.tab == 2 else "magenta")
        room = h - 10
        if self.tab == 2:
            if not self.attachments:
                self.put(7, 2, "No attachments on this card.", "muted")
            start = max(0, self.attachment - room + 1)
            for i, attachment in enumerate(self.attachments[start:start + room], start):
                uploaded = attachment.get("isUpload") is True
                mark = "›" if i == self.attachment else " "
                badge = "FILE" if uploaded else "LINK"
                size = file_size(attachment.get("bytes")) if uploaded else ""
                name = clean(attachment.get("name")) or clean(filename(attachment))
                self.put(7 + i - start, 2, f"{mark}  {badge}  {name}" + (f"  ·  {size}" if size else ""),
                         "select" if i == self.attachment else "text", ellipsis=True)
            if self.attachments:
                self.put(h - 3, 2, "d or Enter downloads a file or opens a link in your browser.", "muted", ellipsis=True)
        else:
            lines = self.card_lines(w - 5)
            self.scroll = max(0, min(self.scroll, len(lines) - room))
            for y, (line, style) in enumerate(lines[self.scroll:self.scroll + room], 7):
                self.put(y, 2, line, style, ellipsis=True)
            self.put(h - 3, 2, f"Lines {self.scroll + 1}–{min(len(lines), self.scroll + room)} of {len(lines)}", "muted")
        self.footer("Tab Views  a Activity  n Comment  d Attachments  r Reload  Esc Back  ? Help")

    def card_lines(self, width):
        if self.tab == 0:
            body = clean(self.card.get("desc"), multiline=True)
            return [(line, "text") for line in wrapped(body, width)] if body.strip() else [("No description.", "muted")]
        if not self.comments:
            return [("No comments on this card.", "muted")]
        lines = []
        if len(self.comments) >= 100:
            lines += [("Showing the latest 100 comments.", "muted"), ("", "text")]
        for comment in reversed(self.comments):
            person = comment.get("memberCreator") or {}
            data = comment.get("data") or {}
            name = clean(person.get("fullName") or person.get("username")) or "Unknown author"
            date = clean(comment.get("date")).replace("T", " ")[:16]
            lines.append((f"{name}  ·  {date}", "cyan"))
            lines.extend((line, "text") for line in wrapped(clean(data.get("text"), multiline=True), width))
            lines.append(("", "text"))
        return lines

    def pick_board(self):
        self.set_notice("Loading boards…", "cyan")
        self.draw()
        try:
            self.reload_boards()
        except APIError as exc:
            self.set_notice(str(exc), "red")
            return
        self.set_notice("")
        if not self.boards:
            self.set_notice("No open Trello boards found for this account.", "yellow")
            return
        current = self.board["id"] if self.board else None
        selected = next((i for i,b in enumerate(self.boards) if b["id"] == current), 0)
        while True:
            self.s.erase()
            h, _ = self.s.getmaxyx()
            room = max(1, h - 7)
            start = max(0, min(selected - room // 2, len(self.boards) - room))
            self.put(1, 2, "SWITCH BOARD", "cyan")
            self.put(2, 2, f"{selected + 1}/{len(self.boards)}  ·  ★ Starred boards first", "muted")
            for i, board in enumerate(self.boards[start:start + room], start):
                mark = "✓" if board["id"] == current else " "
                star = "★" if board.get("starred") else " "
                self.put(4 + i - start, 2, f" {mark} {star}  {title(board)}",
                         "select" if i == selected else "text", ellipsis=True)
            self.put(h - 2, 2, "↑↓ Select   ↵ Open board   Esc Cancel", "header")
            self.s.refresh()
            key = self.s.get_wch()
            if key == ESC:
                return
            if key in ENTER:
                if current is not None:
                    self.board_filters[current] = self.list_id
                board = self.boards[selected]
                if self.load_board(board, self.board_filters.get(board["id"])):
                    self.remember_board()
                return
            if key in UP or key in DOWN:
                selected = max(0, min(len(self.boards) - 1, selected + (1 if key in DOWN else -1)))
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = len(self.boards) - 1

    def remember_board(self):
        if self.api.demo:
            return
        try:
            save_json(config_dir() / "state.json", {"board": self.board["id"]})
        except OSError:
            self.set_notice("Board opened, but couldn't remember it for next time.", "yellow")

    def filter_lists(self):
        values = [None] + [l["id"] for l in self.lists]
        self.list_id = values[(values.index(self.list_id) + 1) % len(values)]
        self.selected = self.top = 0
        self.set_notice("")

    def jump(self):
        value = self.prompt("Card link, short ID or #number")
        if not value:
            return
        try:
            if value.startswith("#"):
                number = value[1:]
                matching = next((c for c in self.cards if str(c.get("idShort")) == number), None)
                if not number.isdecimal() or not matching:
                    raise APIError("No open card with that number on this board. Try the Trello link.")
                cid = matching["id"]
            else:
                cid = card_ref(value)
            self.open_card(cid)
        except (APIError, ValueError) as exc:
            self.set_notice(str(exc) if isinstance(exc, APIError) else "Invalid Trello card link.", "red")

    def handle_attachment(self):
        if self.tab != 2:
            self.tab, self.scroll = 2, 0
            return
        if not self.attachments:
            self.set_notice("No attachments to download.")
            return
        a = self.attachments[self.attachment]
        if a.get("isUpload") is not True:
            try:
                url = str(a.get("url") or "")
                p = urlsplit(url)
                if p.scheme not in ("http", "https") or not p.hostname or p.username is not None:
                    raise ValueError
                if self.dialog("EXTERNAL ATTACHMENT", [(clean(title(a)), "text"),
                                                       (clean(p.hostname), "cyan")],
                               "o  Open in browser   Esc  Cancel", {"o", ESC}) == "o":
                    if not shutil.which("xdg-open"):
                        self.set_notice("No browser opener found. Open the attachment in Trello.", "yellow")
                        return
                    subprocess.Popen(["xdg-open", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True, env=child_environment())
                    self.set_notice("Opened the link in your browser.", "green")
            except (ValueError, OSError):
                self.set_notice("Could not open this external attachment link.", "red")
            return
        self.set_notice("Downloading attachment…", "cyan")
        self.draw()
        try:
            saved = self.api.download(self.card, a, self.directory)
            self.set_notice(f"Saved {saved}", "green")
        except (APIError, OSError, ValueError) as exc:
            self.set_notice(str(exc) if isinstance(exc, APIError) else "Could not save the attachment. Check the destination and available space.", "red")

    def edit_comment(self):
        if not self.api.write:
            self.set_notice("Read-only mode. Run --setup to enable posting.", "yellow")
            return
        cid = self.card["id"]
        if cid in self.unconfirmed:
            if self.dialog("PREVIOUS POST UNCONFIRMED", [
                ("Refresh and check the card's comments before retrying.", "yellow"),
                ("If the comment is already there, do not post it again.", "text")],
                "e  Continue to editor   Esc  Cancel", {"e", ESC}) != "e":
                return
        try:
            editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano")
            if not editor:
                raise ValueError
            with tempfile.TemporaryDirectory(prefix="trello-comment-") as directory:
                path = Path(directory) / "comment.txt"
                path.touch(mode=0o600)
                path.write_text(self.drafts.get(cid, ""), encoding="utf-8")
                curses.def_prog_mode()
                curses.endwin()
                try:
                    print("Write your Trello comment, then save and exit the editor to review.", flush=True)
                    # Do not hand environment credentials to a user-selected editor.
                    code = subprocess.run([*editor, str(path)], env=child_environment(), check=False).returncode
                finally:
                    curses.reset_prog_mode()
                    self.s.clear()
                    self.s.refresh()
                    self.cursor(False)
                draft = path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            self.set_notice("Could not use the editor. Set VISUAL or EDITOR to nano, vim, etc.", "red")
            return
        if draft:
            self.drafts[cid] = draft
        if code:
            self.set_notice("Editor did not exit cleanly. Nothing posted.", "yellow")
            return
        if not draft:
            self.drafts.pop(cid, None)
            self.set_notice("Empty comment. Nothing posted.")
            return
        label = f"POST COMMENT  /  #{clean(self.card.get('idShort'))}  {title(self.card)}"
        preview = [("Visible to people who can see this Trello card.", "yellow"), ("", "text")]
        preview.extend((line, "text") for line in wrapped(clean(draft, multiline=True), max(10, self.s.getmaxyx()[1] - 5)))
        if self.dialog(label, preview, "y  Post comment   Esc  Keep draft   ↑↓ Scroll", {"y", ESC}) != "y":
            self.set_notice("Nothing posted. Draft kept for this session.", "yellow")
            return
        self.set_notice("Posting comment…", "cyan")
        self.draw()
        try:
            self.api.comment(cid, draft)
        except Unconfirmed:
            self.unconfirmed.add(cid)
            self.tab, self.scroll = 1, 10**8
            self.set_notice("Post unconfirmed. Refresh comments before retrying. Draft kept.", "yellow")
            return
        except APIError as exc:
            self.set_notice(str(exc) + " Draft kept.", "red")
            return
        self.drafts.pop(cid, None)
        self.unconfirmed.discard(cid)
        self.tab, self.scroll = 1, 10**8
        if self.open_card(cid, refresh=True):
            self.set_notice("Comment posted.", "green")
        else:
            self.set_notice("Comment posted. Press r to reload the card.", "yellow")

    def show_activity(self):
        is_card = self.card is not None
        target = self.card if is_card else self.board
        if not target:
            self.set_notice("Choose a board first (b).", "yellow")
            return
        kind = "cards" if is_card else "boards"
        events = []
        selected = top = 0
        message = ("", "muted")

        def fetch():
            nonlocal events, selected, top, message
            keep = events[selected].get("id") if events else None
            h, _ = self.s.getmaxyx()
            self.s.move(max(0, h - 2), 0)
            self.s.clrtoeol()
            self.put(h - 2, 2, "Loading activity…", "cyan")
            self.s.refresh()
            try:
                events = sorted(self.api.activity(kind, target["id"]),
                                key=lambda event: clean(event.get("date")), reverse=True)
            except APIError as exc:
                message = (str(exc), "red")
                return
            selected = next((i for i, event in enumerate(events) if keep and event.get("id") == keep),
                            min(selected, max(0, len(events) - 1)))
            top = 0
            message = ("", "muted")

        fetch()
        while True:
            self.s.erase()
            h, w = self.s.getmaxyx()
            room = max(1, (h - 10) // 2)
            self.put(0, 2, f"{'CARD' if is_card else 'BOARD'}  /  RECENT ACTIVITY", "cyan")
            self.put(2, 2, title(target), "header", ellipsis=True)
            self.put(3, 2, f"{len(events)} events  ·  Newest first  ·  Up to {ACTIVITY_LIMIT}", "muted")
            top = max(0, min(top, selected, max(0, len(events) - room)))
            if selected >= top + room:
                top = selected - room + 1
            if not events:
                self.put(5, 2, "No activity to display.", "muted")
            for i, event in enumerate(events[top:top + room], top):
                y = 5 + (i - top) * 2
                author, date, action, detail, card = event_parts(event)
                active = i == selected
                if active:
                    self.put(y, 0, " " * (w - 1), "select")
                self.put(y, 2, f"{'›' if active else ' '} {date}  {author} · {action}",
                         "select" if active else "cyan", ellipsis=True)
                subtitle = ("#" + clean(card.get("idShort")) + " " if card.get("idShort") is not None else "")
                if not is_card and card:
                    subtitle += title(card)
                else:
                    subtitle = ""
                visible = "  ·  ".join(v for v in (subtitle, detail) if v)
                self.put(y + 1, 5, visible, "text" if active else "muted", ellipsis=True)
            if events:
                self.put(h - 4, 2, f"Event {selected + 1}/{len(events)}", "muted")
            self.put(h - 2, 2, *message, ellipsis=True)
            keys = "↑↓ Select   Space Details   " + ("" if is_card else "↵ Open card   ") + "r Refresh   Esc Back"
            self.put(h - 1, 2, keys, "header", ellipsis=True)
            self.s.refresh()
            key = self.s.get_wch()
            if key in (ESC, "a", "q"):
                self.set_notice("")
                return
            if key == "r":
                fetch()
                continue
            if not events:
                continue
            if key in UP + DOWN + (curses.KEY_PPAGE, curses.KEY_NPAGE, "[", "]"):
                step = 1 if key in UP + DOWN else room
                step *= 1 if key in DOWN + (curses.KEY_NPAGE, "]") else -1
                selected = max(0, min(selected + step, len(events) - 1))
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = len(events) - 1
            elif key == " " or (is_card and key in ENTER):
                author, date, action, detail, card = event_parts(events[selected])
                lines = [(f"{author} · {date}", "cyan"), (title(card) if card else title(target), "header"), ("", "text")]
                lines += [(line, "text") for line in wrapped(detail or action, max(10, w - 5))]
                self.dialog(action, lines, "↑↓ Scroll   Esc or Enter to close", {ESC, " ", *ENTER})
            elif key in ENTER:
                card = event_parts(events[selected])[4]
                cid = card.get("id") or card.get("shortLink")
                if not cid:
                    message = ("This event isn't linked to a card. Press Space for details.", "yellow")
                elif self.open_card(cid):
                    return
                else:
                    message = self.notice

    def help(self):
        self.dialog("Keyboard shortcuts", [
            ("BOARD & CARDS", "cyan"),
            ("↑ / ↓ or j / k  Select    Enter  Open    Esc  Back", "text"),
            ("b  Switch boards    f  Cycle lists on this board", "text"),
            ("/  Filter titles (Ctrl-U clears)    g  Link, short ID or #number", "text"),
            ("[ / ] or PgUp / PgDn  Page   Home / End  First / last", "text"),
            ("a  Recent activity for the board or current card", "text"),
            ("r  Refresh    q  Quit from the board view", "text"),
            ("CARD DETAIL", "cyan"),
            ("Tab  Description / Comments / Attachments", "text"),
            ("n  Draft and preview a comment before posting", "text"),
            ("d  Attachments, then d or Enter to download/open", "text"),
        ], "Esc, Enter or ? to close", {ESC, "?", "q", *ENTER})

    def select_initial(self, args):
        try:
            self.reload_boards()
        except APIError as exc:
            self.set_notice(str(exc), "red")
            return
        if not self.boards:
            self.set_notice("No open Trello boards found. Press b to retry.", "yellow")
            return
        remembered = None
        if not self.api.demo:
            try:
                remembered = read_json(config_dir() / "state.json").get("board")
            except (OSError, ValueError):
                pass
        board = next((b for b in self.boards if b["id"] == remembered), self.boards[0])
        if args.board:
            target = args.board.casefold()
            matches = [b for b in self.boards
                       if target in (title(b).casefold(), b["id"].casefold(),
                                     str(b.get("shortLink") or "").casefold())]
            if len(matches) != 1:
                self.set_notice("Default board not found or ambiguous. Press b to choose a board.", "yellow")
                return
            board = matches[0]
        if self.load_board(board) and args.list:
            match = next((l for l in self.lists if args.list.casefold() in (title(l).casefold(), l["id"].casefold())), None)
            if match:
                self.list_id = match["id"]
            else:
                self.set_notice("Default list not found. Showing all open lists.", "yellow")

    def run(self, args):
        self.directory = args.download_dir or download_dir()
        self.select_initial(args)
        while True:
            self.draw()
            key = self.s.get_wch()
            h, _ = self.s.getmaxyx()
            room = max(1, h - 11)
            if key == "?":
                self.help()
                continue
            if key == "a":
                self.show_activity()
                continue
            if self.card is not None:
                if key in (ESC, "q"):
                    self.card = None
                    self.set_notice("")
                elif key in ("\t", curses.KEY_BTAB):
                    self.tab = (self.tab + (1 if key == "\t" else -1)) % len(TABS)
                    self.scroll = 0
                elif key in UP + DOWN + (curses.KEY_PPAGE, curses.KEY_NPAGE, "[", "]", curses.KEY_HOME, curses.KEY_END):
                    if key == curses.KEY_HOME:
                        target = 0
                    elif key == curses.KEY_END:
                        target = 10**8
                    else:
                        step = 1 if key in UP + DOWN else room
                        delta = step if key in DOWN + (curses.KEY_NPAGE, "]") else -step
                        target = (self.attachment if self.tab == 2 else self.scroll) + delta
                    if self.tab == 2:
                        self.attachment = max(0, min(target, len(self.attachments) - 1))
                    else:
                        self.scroll = target
                elif key == "r":
                    self.open_card(self.card["id"], refresh=True)
                elif key == "n":
                    self.edit_comment()
                elif key == "d" or (key in ENTER and self.tab == 2):
                    self.handle_attachment()
                continue
            if key in (ESC, "q"):
                if self.drafts and self.dialog("Discard unsent comments?",
                        [("Drafts only live in this session.", "yellow")],
                        "y  Discard drafts and quit   Esc  Stay", {"y", ESC}) != "y":
                    continue
                return
            if key == "b" or (key == "r" and self.board is None):
                self.pick_board()
            elif key == "r" and self.board is not None:
                self.load_board(self.board, self.list_id, reset=False)
            elif key == "f":
                self.filter_lists()
            elif key == "/":
                query = self.prompt("Card title search", self.query)
                if query is not None:
                    self.query = query
                    self.selected = self.top = 0
            elif key == "g":
                self.jump()
            else:
                visible = self.filtered()
                if not visible:
                    continue
                if key in ENTER:
                    self.open_card(visible[self.selected]["id"])
                elif key == curses.KEY_HOME:
                    self.selected = 0
                elif key == curses.KEY_END:
                    self.selected = len(visible) - 1
                elif key in UP + DOWN + (curses.KEY_PPAGE, curses.KEY_NPAGE, "[", "]"):
                    step = 1 if key in UP + DOWN else room
                    delta = step if key in DOWN + (curses.KEY_NPAGE, "]") else -step
                    self.selected = max(0, min(len(visible) - 1, self.selected + delta))


def main():
    parser = argparse.ArgumentParser(description="Browse Trello boards, read cards, comment and download attachments.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup", action="store_true", help="configure your Trello API key and user token")
    mode.add_argument("--demo", action="store_true", help="offline sample boards, cards, comments and attachments")
    parser.add_argument("--board", help="startup board name or ID (otherwise last selected or first starred board)")
    parser.add_argument("--list", help="startup list name or ID, optional")
    parser.add_argument("--download-dir", type=lambda p: Path(p).expanduser(), help="attachment download directory")
    posting = parser.add_mutually_exclusive_group()
    posting.add_argument("--read-only", action="store_true", help="disable comment posting")
    posting.add_argument("--enable-comments", action="store_true", help="allow posting with write-capable environment credentials")
    args = parser.parse_args()
    try:
        if args.setup:
            setup()
        else:
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise APIError("Run in an interactive Linux terminal.")
            api = Demo() if args.demo else credentials(args)
            if args.read_only:
                api.write = False
            curses.wrapper(lambda screen: Inbox(screen, api).run(args))
    except (EOFError, KeyboardInterrupt):
        return 130
    except (APIError, OSError, curses.error) as exc:
        print(f"trello-cards: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
