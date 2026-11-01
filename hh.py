#!/usr/bin/env python3
"""herdr history: remember every herdr space, open or closed, and bring closed ones back.

  hh.py record     snapshot herdr's live state into the history (event and startup hooks)
  hh.py pick       fzf picker over every space; Enter focuses or reopens (plugin pane)
  hh.py rows       picker rows, one space per line
  hh.py show ID    details of one space (picker preview)
  hh.py open ID [--no-focus]   focus the space if open, otherwise recreate it and restart its agents

Agent-neutral: everything comes from herdr's own view (workspaces, tabs, panes, the agent
kind herdr detected, the terminal title). A native session id is used only when a herdr
integration reported one, through the per-agent RESUME table.
"""
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
STATE = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser("~/.local/state/herdr-history")
DB = os.path.join(STATE, "history.sqlite")

# How to resume a native session id per agent kind. Agents missing here restart fresh.
RESUME = {
    "claude": lambda sid: ["--resume", sid],
    "codex": lambda sid: ["resume", sid],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS spaces (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  cwd TEXT NOT NULL,
  created REAL NOT NULL,
  last_seen REAL NOT NULL,
  last_active REAL NOT NULL,
  closed_at REAL,
  open_ws TEXT,
  layout TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS runs (
  terminal_id TEXT PRIMARY KEY,
  space_id INTEGER NOT NULL REFERENCES spaces(id),
  tab TEXT,
  cwd TEXT,
  agent TEXT,
  title TEXT,
  status TEXT,
  session TEXT,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_space ON runs(space_id, last_seen);
"""


def herdr(*args):
    out = subprocess.run([HERDR, *args], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["result"]


def connect():
    os.makedirs(STATE, exist_ok=True)
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


# ---------- record ----------

def topic(title):
    # A bare shell sets "user@host:dir" as its title; that says nothing about the work.
    return "" if re.match(r"^\S+@\S+:", title) else title


def snapshot():
    workspaces = herdr("workspace", "list")["workspaces"]
    tabs = herdr("tab", "list")["tabs"]
    panes = herdr("pane", "list")["panes"]
    agents = {a["pane_id"]: a for a in herdr("agent", "list").get("agents", [])}
    out = []
    for ws in workspaces:
        wid = ws["workspace_id"]
        wtabs = sorted((t for t in tabs if t["workspace_id"] == wid), key=lambda t: t["number"])
        layout = []
        for t in wtabs:
            tp = sorted((p for p in panes if p["tab_id"] == t["tab_id"]), key=lambda p: p["pane_id"])
            layout.append({
                "label": t["label"],
                "panes": [{
                    "terminal_id": p["terminal_id"],
                    "cwd": p.get("cwd"),
                    "agent": p.get("agent"),
                    "status": p.get("agent_status"),
                    "title": topic(p.get("terminal_title_stripped") or ""),
                    "session": (agents.get(p["pane_id"]) or {}).get("agent_session") or p.get("agent_session"),
                } for p in tp],
            })
        cwd = next((p["cwd"] for t in layout for p in t["panes"] if p["cwd"]), "")
        out.append({"workspace_id": wid, "name": ws["label"], "cwd": cwd, "layout": layout})
    return out


def record_once(db):
    now = time.time()
    live = snapshot()
    live_ids = {w["workspace_id"] for w in live}
    for row in db.execute("SELECT id, open_ws FROM spaces WHERE open_ws IS NOT NULL").fetchall():
        if row["open_ws"] not in live_ids:
            db.execute("UPDATE spaces SET open_ws = NULL, closed_at = ? WHERE id = ?", (now, row["id"]))
    for w in live:
        row = db.execute("SELECT id FROM spaces WHERE open_ws = ?", (w["workspace_id"],)).fetchone()
        if row is None:
            # A reopened space: same name and folder as a closed one.
            row = db.execute(
                "SELECT id FROM spaces WHERE open_ws IS NULL AND name = ? AND cwd = ? ORDER BY last_seen DESC",
                (w["name"], w["cwd"])).fetchone()
            if row is None:
                sid = db.execute(
                    "INSERT INTO spaces (name, cwd, created, last_seen, last_active, open_ws) VALUES (?,?,?,?,?,?)",
                    (w["name"], w["cwd"], now, now, now, w["workspace_id"])).lastrowid
            else:
                sid = row["id"]
                db.execute("UPDATE spaces SET open_ws = ?, closed_at = NULL, last_active = ? WHERE id = ?",
                           (w["workspace_id"], now, sid))
        else:
            sid = row["id"]
        busy = any(p["status"] in ("working", "blocked") for t in w["layout"] for p in t["panes"])
        db.execute(
            "UPDATE spaces SET name = ?, cwd = ?, last_seen = ?, layout = ?,"
            " last_active = CASE WHEN ? THEN ? ELSE last_active END WHERE id = ?",
            (w["name"], w["cwd"], now, json.dumps(w["layout"]), busy, now, sid))
        for t in w["layout"]:
            for p in t["panes"]:
                if not p["agent"]:
                    continue
                db.execute(
                    "INSERT INTO runs (terminal_id, space_id, tab, cwd, agent, title, status, session, first_seen, last_seen)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(terminal_id) DO UPDATE SET"
                    " space_id = excluded.space_id, tab = excluded.tab, cwd = excluded.cwd, agent = excluded.agent,"
                    " title = CASE WHEN excluded.title != '' THEN excluded.title ELSE runs.title END,"
                    " status = excluded.status, session = COALESCE(excluded.session, runs.session),"
                    " last_seen = excluded.last_seen",
                    (p["terminal_id"], sid, t["label"], p["cwd"], p["agent"], p["title"], p["status"],
                     json.dumps(p["session"]) if p["session"] else None, now, now))
    db.commit()


def record():
    # Hooks fire in bursts: one process records, later callers just mark it dirty.
    os.makedirs(STATE, exist_ok=True)
    dirty = os.path.join(STATE, "dirty")
    open(dirty, "w").close()
    with open(os.path.join(STATE, "lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        db = connect()
        while os.path.exists(dirty):
            os.remove(dirty)
            record_once(db)


# ---------- pick ----------

def ago(t):
    s = time.time() - t
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return "now"


def rows():
    db = connect()
    home = os.path.expanduser("~")
    table = []
    for s in db.execute("SELECT * FROM spaces ORDER BY open_ws IS NULL, last_active DESC"):
        runs = db.execute("SELECT agent, title FROM runs WHERE space_id = ? ORDER BY last_seen DESC", (s["id"],)).fetchall()
        agents = ",".join(sorted({r["agent"] for r in runs})) or "-"
        topic = next((r["title"] for r in runs if r["title"]), "")
        state = "● open" if s["open_ws"] else "○ closed"
        table.append([str(s["id"]), state, s["name"], ago(s["last_active"]), agents, s["cwd"].replace(home, "~", 1), topic])
    widths = [max((len(r[i]) for r in table), default=0) for i in range(len(table[0]))] if table else []
    for r in table:
        # Field 1 is the id fzf hands back; field 2 is what the picker shows, in aligned columns.
        print(r[0] + "\t" + "  ".join(c.ljust(widths[i]) for i, c in enumerate(r) if i > 0).rstrip())


def show(sid):
    db = connect()
    s = db.execute("SELECT * FROM spaces WHERE id = ?", (sid,)).fetchone()
    if s is None:
        return
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
    print(f"{s['name']}  ({'open as ' + s['open_ws'] if s['open_ws'] else 'closed ' + fmt(s['closed_at'] or s['last_seen'])})")
    print(f"folder   {s['cwd']}")
    print(f"created  {fmt(s['created'])}\nactive   {fmt(s['last_active'])}\n")
    print("Tabs at last sight:")
    for t in json.loads(s["layout"]):
        for p in t["panes"]:
            print(f"  [{t['label']}] {p['agent'] or 'shell'}  {p['title']}  {p['cwd']}")
    print("\nAgent runs, newest first:")
    for r in db.execute("SELECT * FROM runs WHERE space_id = ? ORDER BY last_seen DESC LIMIT 50", (sid,)):
        resumable = " (resumable)" if r["session"] and r["agent"] in RESUME else ""
        print(f"  {fmt(r['first_seen'])} → {fmt(r['last_seen'])}  {r['agent']}{resumable}  {r['title']}")


def pick():
    me = os.path.abspath(__file__)
    lines = subprocess.run([sys.executable, me, "rows"], capture_output=True, text=True).stdout
    if not lines.strip():
        print("No spaces recorded yet.")
        time.sleep(2)
        return
    fzf = subprocess.run(
        ["fzf", "--delimiter", "\t", "--with-nth", "2", "--layout", "reverse",
         "--header", "Enter: focus or reopen   Esc: close", "--prompt", "space> ",
         "--preview", f"{sys.executable} {me} show {{1}}", "--preview-window", "down,50%,wrap"],
        input=lines, capture_output=False, stdout=subprocess.PIPE, text=True)
    choice = fzf.stdout.strip()
    if fzf.returncode != 0 or not choice:
        return
    # Detach so the popup closes while the space opens.
    log = open(os.path.join(STATE, "open.log"), "a")
    subprocess.Popen([sys.executable, me, "open", choice.split("\t")[0]], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, start_new_session=True)


# ---------- open ----------

def start_agent(pane, p, name):
    kind = p["agent"]
    args = []
    sess = p.get("session")
    if sess and sess.get("kind") == "id" and kind in RESUME:
        args = RESUME[kind](sess["value"])
    cmd = [HERDR, "agent", "start", name, "--kind", kind, "--pane", pane]
    if args:
        cmd += ["--", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(f"{' '.join(cmd)} -> {r.returncode} {r.stdout.strip()} {r.stderr.strip()}")


def open_space(sid, focus=True):
    db = connect()
    s = db.execute("SELECT * FROM spaces WHERE id = ?", (sid,)).fetchone()
    live = {w["workspace_id"] for w in herdr("workspace", "list")["workspaces"]}
    if s["open_ws"] in live:
        if focus:
            herdr("workspace", "focus", s["open_ws"])
        return
    layout = json.loads(s["layout"]) or [{"label": "1", "panes": [{"cwd": s["cwd"]}]}]
    cwd0 = next((p["cwd"] for p in layout[0]["panes"] if p.get("cwd")), s["cwd"])
    res = herdr("workspace", "create", "--cwd", cwd0, "--label", s["name"], "--focus" if focus else "--no-focus")
    wid = res["workspace"]["workspace_id"]
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", s["name"]).strip("-").lower() or "space"
    print(f"{time.ctime()} reopening space {sid} '{s['name']}' as {wid}")
    for i, t in enumerate(layout):
        if i == 0:
            tab_id, pane = res["tab"]["tab_id"], res["root_pane"]["pane_id"]
        else:
            r = herdr("tab", "create", "--workspace", wid, "--cwd", t["panes"][0].get("cwd") or s["cwd"], "--no-focus")
            tab_id, pane = r["tab"]["tab_id"], r["root_pane"]["pane_id"]
        if not t["label"].isdigit():
            herdr("tab", "rename", tab_id, t["label"])
        for j, p in enumerate(t["panes"]):
            if j > 0:
                pane = herdr("pane", "split", pane, "--direction", "right", "--cwd", p.get("cwd") or s["cwd"])["pane"]["pane_id"]
            if p.get("agent"):
                start_agent(pane, p, f"{slug}-{i + 1}-{j + 1}")
    # Record now so the space is linked to its new workspace before anything else.
    record()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "pick"
    if cmd == "record":
        record()
    elif cmd == "rows":
        rows()
    elif cmd == "show":
        show(int(sys.argv[2]))
    elif cmd == "open":
        open_space(int(sys.argv[2]), focus="--no-focus" not in sys.argv)
    elif cmd == "pick":
        pick()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
