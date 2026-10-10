#!/usr/bin/env python3
"""friday-ssh — terminal-only signup for the Friday house thing.

Identity = your SSH public key fingerprint. No passwords, no web.
Each signup is keyed to the upcoming Friday, so every week starts fresh
without a reset job.

Confirmed vs waitlist is not stored: it's derived from signup order.
The first CAPACITY people (by signed_up_at) are in, the next WAITLIST
are waitlisted. A cancellation promotes the next person automatically.

Usage:
    python server.py              run the SSH server
    python server.py purge        delete signups for past Fridays
    python server.py purge all    delete every signup (host key is kept)
"""
import asyncio
import os
import re
import sqlite3
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncssh

DATA = Path(os.environ.get("DATA_DIR", "./data"))
PORT = int(os.environ.get("PORT", "2222"))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Singapore"))
CAPACITY = int(os.environ.get("CAPACITY", "4"))           # confirmed people
WAITLIST = int(os.environ.get("WAITLIST", "10"))          # waitlist slots after that
CUTOFF = time(int(os.environ.get("CUTOFF_HOUR", "18")))   # Friday signups close at this hour
EVENT_NAME = os.environ.get("EVENT_NAME", "Friday at the house")
DETAILS_FILE = DATA / "details.txt"                       # location/time, shown to confirmed only

# Soft gate: these clients identify themselves as non-Linux in the SSH version string.
BLOCKED_CLIENTS = re.compile(r"PuTTY|for_Windows|WinSCP|Bitvise|SecureCRT|MobaXterm", re.I)

DATA.mkdir(parents=True, exist_ok=True)
DB = sqlite3.connect(DATA / "signups.db")
DB.execute("""CREATE TABLE IF NOT EXISTS signups (
    friday       TEXT NOT NULL,   -- YYYY-MM-DD of the Friday signed up for
    fingerprint  TEXT NOT NULL,   -- SSH key fingerprint = identity
    name         TEXT NOT NULL,
    contact      TEXT NOT NULL,   -- normalised +65 XXXX XXXX
    dietary      TEXT,
    prototyping  TEXT,            -- optional: tools/parts/help they'd like
    signed_up_at TEXT NOT NULL,   -- set once; decides queue position
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (friday, fingerprint))""")
DB.commit()


def now():
    return datetime.now(TZ)


def upcoming_friday(at=None):
    at = at or now()
    days = (4 - at.weekday()) % 7
    fri = (at + timedelta(days=days)).date()
    if days == 0 and at.time() >= CUTOFF:
        fri += timedelta(days=7)
    return fri.isoformat()


def queue(friday):
    """Fingerprints in signup order. Index < CAPACITY means confirmed."""
    return [fp for (fp,) in DB.execute(
        "SELECT fingerprint FROM signups WHERE friday = ? ORDER BY signed_up_at, fingerprint",
        (friday,))]


def standing(friday, fp):
    """('in', None), ('waitlist', n) or (None, None) if not signed up."""
    q = queue(friday)
    if fp not in q:
        return None, None
    i = q.index(fp)
    return ("in", None) if i < CAPACITY else ("waitlist", i - CAPACITY + 1)


def counts_line(friday):
    n = len(queue(friday))
    taken, waiting = min(n, CAPACITY), max(0, n - CAPACITY)
    line = f"{taken}/{CAPACITY} spots taken"
    if waiting or taken >= CAPACITY:
        line += f" · {waiting}/{WAITLIST} on waitlist"
    return line


def details():
    try:
        return DETAILS_FILE.read_text().strip()
    except FileNotFoundError:
        return os.environ.get("EVENT_DETAILS", "(details coming soon)")


def sg_number(raw):
    """Return '+65 XXXX XXXX' for a valid Singapore number, else None."""
    s = re.sub(r"[\s\-()]", "", raw)
    if s.startswith("+65"):
        s = s[3:]
    if re.fullmatch(r"[3689]\d{7}", s):
        return f"+65 {s[:4]} {s[4:]}"
    return None


class Server(asyncssh.SSHServer):
    def connection_made(self, conn):
        self._conn = conn

    def begin_auth(self, username):
        self._conn.send_auth_banner(
            "friday-ssh: public key only. No key yet? Run: ssh-keygen -t ed25519\n")
        return True

    def password_auth_supported(self):
        return False

    def kbdint_auth_supported(self):
        return False

    def public_key_auth_supported(self):
        return True

    def validate_public_key(self, username, key):
        # Any key is welcome; the fingerprint becomes the guest's identity.
        self._conn.set_extra_info(fingerprint=key.get_fingerprint())
        return True


async def ask(process, prompt, default=""):
    while True:
        process.stdout.write(prompt)
        try:
            line = await process.stdin.readline()
        except asyncssh.TerminalSizeChanged:
            continue
        if line == "":          # Ctrl-D
            raise EOFError
        return line.strip() or default


def show_standing(w, friday, fp, nice_date):
    status, pos = standing(friday, fp)
    if status == "in":
        w(f"  You're in for {nice_date}.\n\n")
        for line in details().splitlines():
            w(f"    {line}\n")
        w("\n")
    elif status == "waitlist":
        w(f"  You're #{pos} on the waitlist. If a spot opens before Friday "
          f"{CUTOFF:%H:%M}, you move up automatically — reconnect to check.\n")


async def session(process):
    w = process.stdout.write
    client = process.get_extra_info("client_version") or ""

    if not process.get_terminal_type():
        w("friday-ssh needs an interactive terminal (try: ssh -t ...)\n")
        return 1
    if BLOCKED_CLIENTS.search(client):
        w("This door only opens for Linux terminals. 🐧\n")
        return 1

    fp = process.get_extra_info("fingerprint")
    user = process.get_extra_info("username") or ""
    friday = upcoming_friday()
    nice_date = datetime.fromisoformat(friday).strftime("%a %d %b %Y")
    mine = DB.execute(
        "SELECT name, contact, dietary, prototyping FROM signups WHERE friday=? AND fingerprint=?",
        (friday, fp)).fetchone()

    w(f"\n  {EVENT_NAME} — {nice_date}\n")
    w(f"  {counts_line(friday)}\n\n")

    if mine:
        name, contact, dietary, prototyping = mine
        w(f"  Signed up as {name}, {contact}{', ' + dietary if dietary else ''}\n")
        if prototyping:
            w(f"  Prototyping: {prototyping}\n")
        show_standing(w, friday, fp, nice_date)
        choice = (await ask(process, "  [k]eep  [e]dit  [c]ancel > ", "k")).lower()[:1]
        if choice == "c":
            DB.execute("DELETE FROM signups WHERE friday=? AND fingerprint=?", (friday, fp))
            DB.commit()
            w("  Cancelled. Maybe next Friday.\n")
            return 0
        if choice != "e":
            w("  Bye.\n")
            return 0
    else:
        n = len(queue(friday))
        if n >= CAPACITY + WAITLIST:
            w("  Sorry, this Friday and its waitlist are full.\n")
            return 0
        if n >= CAPACITY:
            w(f"  This Friday is full — you'd be #{n - CAPACITY + 1} on the waitlist.\n\n")
        name, contact, dietary, prototyping = user, "", "", ""

    name = (await ask(process, f"  Name [{name}]: ", name))[:40]
    while True:
        raw = await ask(process, f"  Contact no. (Singapore) [{contact}]: ", contact)
        if (cleaned := sg_number(raw)):
            contact = cleaned
            break
        w("  Needs a Singapore number, e.g. 9123 4567 or +65 9123 4567\n")
    dietary = (await ask(process, f"  Dietary needs [{dietary or 'none'}]: ", dietary))[:120]
    prototyping = (await ask(process,
        f"  Prototyping needs, optional (tools, parts, help) [{prototyping or 'none'}]: ",
        prototyping))[:200]

    if (await ask(process, f"\n  Confirm {name}, {contact}? [Y/n] ", "y")).lower().startswith("n"):
        w("  Nothing saved.\n")
        return 0

    # Re-check right before writing: others may have signed up while we were prompting.
    # (No await between this check and the INSERT, so it can't race.)
    if not mine and len(queue(friday)) >= CAPACITY + WAITLIST:
        w("  Sorry, the last waitlist spot just went.\n")
        return 0

    stamp = now().isoformat()
    DB.execute("""INSERT INTO signups
                  (friday, fingerprint, name, contact, dietary, prototyping, signed_up_at, updated_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                  ON CONFLICT(friday, fingerprint) DO UPDATE SET
                  name=excluded.name, contact=excluded.contact, dietary=excluded.dietary,
                  prototyping=excluded.prototyping,
                  updated_at=excluded.updated_at""",          # signed_up_at kept on edit
               (friday, fp, name, contact, dietary, prototyping, stamp, stamp))
    DB.commit()
    w("\n")
    show_standing(w, friday, fp, nice_date)
    return 0


async def handle(process):
    code = 0
    try:
        code = await session(process)
    except (EOFError, asyncssh.BreakReceived, asyncssh.SignalReceived):
        process.stdout.write("\n  bye\n")
    process.exit(code or 0)


def purge(everything=False):
    if everything:
        cur = DB.execute("DELETE FROM signups")
    else:
        cur = DB.execute("DELETE FROM signups WHERE friday < ?", (upcoming_friday(),))
    DB.commit()
    DB.execute("VACUUM")    # actually shrink the file so deleted numbers don't linger on disk
    print(f"purged {cur.rowcount} signup(s)")


async def main():
    keyfile = DATA / "ssh_host_ed25519_key"
    if not keyfile.exists():
        asyncssh.generate_private_key("ssh-ed25519").write_private_key(keyfile)
    await asyncssh.create_server(Server, "", PORT, server_host_keys=[str(keyfile)],
                                 process_factory=handle, line_editor=True)
    print(f"friday-ssh listening on :{PORT}", flush=True)
    await asyncio.Future()


if __name__ == "__main__":
    if sys.argv[1:2] == ["purge"]:
        purge(everything=sys.argv[2:3] == ["all"])
    else:
        asyncio.run(main())