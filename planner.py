#!/usr/bin/env python3
"""
Daily Planner — windowed task planner for macOS.
Usage: planner [morning|evening|pin|cleanup|status|install]
"""
import sys
import json
import sqlite3
import socket
import subprocess
import plistlib
import re
import threading
import functools
from datetime import date, timedelta
from pathlib import Path

# ── Paths (no hardcoded absolute paths) ───────────────────────────────────────
DATA_DIR      = Path.home() / "Library" / "Application Support" / "DailyPlanner"
DB_PATH       = DATA_DIR / "tasks.db"
CONFIG_PATH   = DATA_DIR / "config.json"
AGENTS_DIR    = Path.home() / "Library" / "LaunchAgents"
SCRIPT_PATH   = Path(__file__).resolve()
PYTHON_PATH   = Path(sys.executable)  # keep venv symlink — resolve() would follow it to base Homebrew Python
UI_DIR        = SCRIPT_PATH.parent / "ui"
ICON_PATH     = SCRIPT_PATH.parent / "assets" / "AppIcon.icns"

# Single-instance IPC: the morning/evening tabs live in one window, so a
# launchd fire (or a terminal `planner morning`) while that window is still
# open should focus it and switch tabs, not spawn a second process/window.
SOCK_PATH = DATA_DIR / "planner.sock"

PLIST_MORNING = AGENTS_DIR / "com.dailyplanner.morning.plist"
PLIST_EVENING = AGENTS_DIR / "com.dailyplanner.evening.plist"
PLIST_CLEANUP = AGENTS_DIR / "com.dailyplanner.cleanup.plist"

# Single source of truth for theme names — CSS definitions live in ui/themes.css,
# injected into every window's HTML at launch (see run_window).
VALID_THEMES = (
    "dark", "warm", "nebula", "silvergold", "starlight", "ember",
    "moss", "slate", "sandstone", "blush",
    "deepsea", "lavendermist", "pinegrove", "copper",
    "aurora", "sunsetcanyon",
)

DEFAULT_CONFIG = {
    "morning_time":        "07:30",
    "evening_time":        "21:00",
    "last_morning_shown":  "",
    "last_evening_shown":  "",
    "theme":               "dark",
    "recent_themes":       [],
}

# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return DEFAULT_CONFIG.copy()
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = DEFAULT_CONFIG.copy()
        for k, default in DEFAULT_CONFIG.items():
            if k not in raw:
                continue
            if isinstance(default, str) and isinstance(raw[k], str):
                cfg[k] = raw[k]
            elif isinstance(default, list) and isinstance(raw[k], list):
                cfg[k] = [str(x) for x in raw[k] if isinstance(x, str)]
        for key in ("morning_time", "evening_time"):
            if not re.fullmatch(r"\d{2}:\d{2}", cfg[key]):
                cfg[key] = DEFAULT_CONFIG[key]
        # Guard against a theme retired since this config was last written
        if cfg["theme"] not in VALID_THEMES:
            cfg["theme"] = DEFAULT_CONFIG["theme"]
        cfg["recent_themes"] = [t for t in cfg["recent_themes"] if t in VALID_THEMES]
        return cfg
    except (json.JSONDecodeError, OSError):
        return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ── Database ──────────────────────────────────────────────────────────────────
def open_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: pywebview 6 calls API methods from a GUI thread
    # separate from the main thread; WAL mode keeps concurrent access safe
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name  TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            color TEXT    NOT NULL DEFAULT '#6aaef0'
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL,
            priority     INTEGER NOT NULL DEFAULT 2,
            scope        TEXT    NOT NULL DEFAULT 'today',
            completed    INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT,
            created_date TEXT    NOT NULL,
            today_date   TEXT,
            week_start   TEXT    NOT NULL,
            category_id  INTEGER REFERENCES categories(id),
            sort_order   INTEGER,
            repeatable   INTEGER NOT NULL DEFAULT 0,
            history_dates TEXT   NOT NULL DEFAULT '',
            hopscotch    INTEGER NOT NULL DEFAULT 0,
            recur_group  TEXT
        );
        CREATE TABLE IF NOT EXISTS runs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT    NOT NULL,
            target_per_day   INTEGER NOT NULL,
            total_days       INTEGER NOT NULL,
            allowances_total INTEGER NOT NULL DEFAULT 0,
            allowances_used  INTEGER NOT NULL DEFAULT 0,
            category_id      INTEGER REFERENCES categories(id),
            start_date       TEXT    NOT NULL,
            last_evaluated   TEXT,
            status           TEXT    NOT NULL DEFAULT 'active',
            ended_date       TEXT,
            created_date     TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_days (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id         INTEGER NOT NULL REFERENCES runs(id),
            date           TEXT    NOT NULL,
            count          INTEGER NOT NULL,
            hit            INTEGER NOT NULL,
            used_allowance INTEGER NOT NULL DEFAULT 0,
            UNIQUE(run_id, date)
        );
    """)
    # Safe migrations: add columns to existing installs that predate them
    existing = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "category_id" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN category_id INTEGER REFERENCES categories(id)")
    if "sort_order" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN sort_order INTEGER")
    if "repeatable" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN repeatable INTEGER NOT NULL DEFAULT 0")
    if "history_dates" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN history_dates TEXT NOT NULL DEFAULT ''")
    if "hopscotch" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN hopscotch INTEGER NOT NULL DEFAULT 0")
    if "recur_group" not in existing:
        conn.execute("ALTER TABLE tasks ADD COLUMN recur_group TEXT")
    conn.commit()


# ── Date helpers ──────────────────────────────────────────────────────────────
def week_start_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ── Run evaluation ────────────────────────────────────────────────────────────
def evaluate_runs(conn: sqlite3.Connection):
    """Catches every active run up to yesterday: each unevaluated day either
    hit its target_per_day (qualifying completions that day), consumes one
    allowance (if any remain), or breaks the run. Idempotent via
    last_evaluated — safe to call on every read, not just at 2 AM, so a run's
    status is never stale just because the cleanup agent hasn't fired yet.
    """
    today = date.today()
    active = conn.execute("SELECT * FROM runs WHERE status='active'").fetchall()
    for r in active:
        start = date.fromisoformat(r["start_date"])
        last_eval = date.fromisoformat(r["last_evaluated"]) if r["last_evaluated"] else start - timedelta(days=1)
        allowances_used = r["allowances_used"]
        status = "active"
        ended = None
        day_cursor = last_eval + timedelta(days=1)
        while day_cursor < today and status == "active":
            ds = str(day_cursor)
            if r["category_id"]:
                # Matching-category completions count as a full task; everything else
                # completed that day still counts, just at half a task, so a category
                # run isn't all-or-nothing on days spent entirely on other things.
                matched = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE completed=1 AND completed_at=? AND category_id=?",
                    (ds, r["category_id"])
                ).fetchone()[0]
                other = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE completed=1 AND completed_at=? AND (category_id IS NULL OR category_id!=?)",
                    (ds, r["category_id"])
                ).fetchone()[0]
                count = matched + other * 0.5
            else:
                count = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE completed=1 AND completed_at=?", (ds,)
                ).fetchone()[0]
            hit = count >= r["target_per_day"]
            used_allowance = 0
            if not hit:
                if allowances_used < r["allowances_total"]:
                    allowances_used += 1
                    used_allowance = 1
                else:
                    status, ended = "broken", ds
            conn.execute("""
                INSERT OR REPLACE INTO run_days (run_id, date, count, hit, used_allowance)
                VALUES (?, ?, ?, ?, ?)
            """, (r["id"], ds, count, 1 if hit else 0, used_allowance))
            last_eval = day_cursor
            days_elapsed = (day_cursor - start).days + 1
            if status == "active" and days_elapsed >= r["total_days"]:
                status, ended = "completed", ds
            day_cursor += timedelta(days=1)
        conn.execute("""
            UPDATE runs SET allowances_used=?, last_evaluated=?, status=?, ended_date=?
            WHERE id=?
        """, (allowances_used, str(last_eval), status, ended, r["id"]))
    conn.commit()


# ── Input validation ──────────────────────────────────────────────────────────
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

def clean_name(raw) -> str:
    s = _CTRL.sub("", str(raw).strip())
    return s[:280]

def valid_priority(p) -> int:
    try:
        i = int(p)
        return i if i in (1, 2, 3) else 2
    except (TypeError, ValueError):
        return 2

def valid_scope(s) -> str:
    return s if s in ("today", "weekly") else "today"

def valid_task_id(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

_HEX_COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')

def valid_color(c) -> str:
    s = str(c).strip()
    return s if _HEX_COLOR.match(s) else '#6aaef0'

def valid_time(t: str) -> bool:
    if not re.fullmatch(r"\d{2}:\d{2}", t):
        return False
    h, m = int(t[:2]), int(t[3:])
    return 0 <= h <= 23 and 0 <= m <= 59

def positive_int(v, default, lo, hi) -> int:
    try:
        i = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(i, hi))


# pywebview spawns a brand-new thread for every single JS→Python call with no
# serialization of its own (confirmed in webview/util.py's js_bridge_call —
# `Thread(target=_call).start()`), and the UI fires several calls at once via
# Promise.all (loadAll(), every 60s). sqlite3.Connection is not safe for truly
# concurrent use across threads even with check_same_thread=False, so without
# this lock, two of those calls landing at the same instant can corrupt a
# fetch mid-flight (seen as "IndexError: tuple index out of range" or
# "sqlite3.InterfaceError" out of nowhere). One process-wide RLock serializes
# all DB-touching API calls; reentrant so a method can safely call another
# locked method (or a private helper) on the same thread without deadlocking.
_DB_LOCK = threading.RLock()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with _DB_LOCK:
            return fn(self, *args, **kwargs)
    return wrapper


# ── JS API (only public methods are exposed) ──────────────────────────────────
class PlannerAPI:
    def __init__(self, conn: sqlite3.Connection):
        self._db = conn

    @_locked
    def get_today_tasks(self):
        today = str(date.today())
        rows = self._db.execute("""
            SELECT t.id, t.name, t.priority, t.completed, t.completed_at, t.sort_order, t.repeatable,
                   t.hopscotch, t.recur_group,
                   c.name AS cat_name, c.color AS cat_color
            FROM tasks t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE t.scope='today' AND t.today_date=?
        """, (today,)).fetchall()
        return self._order_tasks(self._with_hopscotch_status([dict(r) for r in rows]))

    @_locked
    def get_weekly_backlog(self):
        rows = self._db.execute("""
            SELECT t.id, t.name, t.priority, t.created_date, t.sort_order, t.completed, t.repeatable,
                   t.hopscotch, t.recur_group,
                   c.name AS cat_name, c.color AS cat_color
            FROM tasks t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE t.scope='weekly' AND t.completed=0
        """).fetchall()
        return self._order_tasks(self._with_hopscotch_status([dict(r) for r in rows]))

    def _hopscotch_last_done(self, group: str):
        row = self._db.execute(
            "SELECT MAX(completed_at) AS d FROM tasks WHERE recur_group=? AND completed_at IS NOT NULL",
            (group,)
        ).fetchone()
        return row["d"] if row else None

    def _with_hopscotch_status(self, tasks):
        """Annotates hopscotch tasks with hop_locked_until (an ISO date, or None
        if it's currently eligible) so the UI can grey out the checkbox without
        a round trip through a rejected mark_done.
        """
        today = date.today()
        for t in tasks:
            if t.get("hopscotch") and not t["completed"]:
                group = t.get("recur_group") or str(t["id"])
                last = self._hopscotch_last_done(group)
                if last:
                    next_ok = date.fromisoformat(last) + timedelta(days=2)
                    t["hop_locked_until"] = str(next_ok) if today < next_ok else None
                else:
                    t["hop_locked_until"] = None
            else:
                t["hop_locked_until"] = None
        return tasks

    @staticmethod
    def _order_tasks(tasks):
        # Natural order = what plain priority/name sorting would produce.
        natural = sorted(tasks, key=lambda t: (t["completed"], t["priority"], t["name"].lower()))
        natural_rank = {t["id"]: i for i, t in enumerate(natural)}
        # Effective order = manual sort_order wins when set (persisted drag-reorder),
        # unordered tasks fall back to natural order and sort after ordered ones.
        effective = sorted(tasks, key=lambda t: (
            t["completed"],
            t["sort_order"] is None,
            t["sort_order"] if t["sort_order"] is not None else 0,
            t["priority"], t["name"].lower()
        ))
        out = []
        for i, t in enumerate(effective):
            d = dict(t)
            d.pop("sort_order", None)
            d.pop("recur_group", None)
            d["reordered"] = natural_rank[t["id"]] != i
            out.append(d)
        return out

    _HISTORY_CAP = 5  # how many distinct planned-on days we remember per task

    def _append_history(self, task_id: int, ds: str):
        row = self._db.execute(
            "SELECT history_dates FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        existing = [d for d in (row["history_dates"] or "").split(",") if d and d != ds]
        existing.insert(0, ds)
        self._db.execute(
            "UPDATE tasks SET history_dates=? WHERE id=?",
            (",".join(existing[:self._HISTORY_CAP]), task_id)
        )

    def _remove_history(self, task_id: int, ds: str):
        if not ds:
            return
        row = self._db.execute(
            "SELECT history_dates FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        existing = [d for d in (row["history_dates"] or "").split(",") if d and d != ds]
        self._db.execute(
            "UPDATE tasks SET history_dates=? WHERE id=?", (",".join(existing), task_id)
        )

    @_locked
    def get_week_overview(self):
        return self.get_period_overview(1)

    @_locked
    def get_period_overview(self, num_weeks=1):
        try:
            n = int(num_weeks)
        except (TypeError, ValueError):
            n = 1
        n = max(1, min(n, 12))  # cap ~3 months so a bad value can't trigger an unbounded scan
        today = date.today()
        start = week_start_of(today) - timedelta(weeks=n - 1)
        result = []
        for i in range(n * 7):
            d = start + timedelta(days=i)
            ds = str(d)
            # today_date alone only remembers the *latest* day a task was planned —
            # re-promoting the same backlog task on a later day overwrites it, which
            # would erase it from every earlier day's cell here. history_dates is a
            # small capped (last 5) comma-separated log of every day it was actually
            # planned, so past days keep showing it even after it moves again.
            rows = self._db.execute("""
                SELECT t.id, t.name, t.priority, t.completed,
                       c.color AS cat_color
                FROM tasks t
                LEFT JOIN categories c ON t.category_id = c.id
                WHERE t.today_date=? OR (',' || t.history_dates || ',') LIKE ('%,' || ? || ',%')
                ORDER BY t.completed, t.priority
            """, (ds, ds)).fetchall()
            result.append({
                "date":     ds,
                "label":    d.strftime("%a").upper(),
                "day_num":  d.day,
                "is_today": ds == str(today),
                "tasks":    [dict(r) for r in rows],
            })
        return result

    @_locked
    def add_task(self, name, priority, scope, category_id=None, repeatable=0, hopscotch=0):
        name = clean_name(name)
        if not name:
            return {"ok": False, "error": "empty name"}
        priority   = valid_priority(priority)
        scope      = valid_scope(scope)
        today      = str(date.today())
        ws         = str(week_start_of(date.today()))
        today_date = today if scope == "today" else None
        history    = today if scope == "today" else ""
        cid        = valid_task_id(category_id)
        rep        = 1 if repeatable else 0
        hop        = 1 if hopscotch else 0
        cur = self._db.execute("""
            INSERT INTO tasks (name, priority, scope, created_date, today_date, week_start, category_id, repeatable, history_dates, hopscotch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, priority, scope, today, today_date, ws, cid, rep, history, hop))
        if rep or hop:
            self._db.execute(
                "UPDATE tasks SET recur_group=? WHERE id=?", (str(cur.lastrowid), cur.lastrowid)
            )
        self._db.commit()
        return {"ok": True, "id": cur.lastrowid}

    @_locked
    def get_categories(self):
        rows = self._db.execute(
            "SELECT id, name, color FROM categories ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def add_category(self, name, color):
        name = clean_name(name)[:50]
        if not name:
            return {"ok": False, "error": "empty name"}
        color = valid_color(color)
        self._db.execute(
            "INSERT OR IGNORE INTO categories (name, color) VALUES (?, ?)", (name, color)
        )
        self._db.execute(
            "UPDATE categories SET color=? WHERE name=? COLLATE NOCASE", (color, name)
        )
        self._db.commit()
        row = self._db.execute(
            "SELECT id FROM categories WHERE name=? COLLATE NOCASE", (name,)
        ).fetchone()
        return {"ok": True, "id": row["id"]}

    @_locked
    def rename_category(self, category_id, name):
        cid = valid_task_id(category_id)
        if cid is None:
            return {"ok": False, "error": "invalid category id"}
        name = clean_name(name)[:50]
        if not name:
            return {"ok": False, "error": "empty name"}
        dupe = self._db.execute(
            "SELECT id FROM categories WHERE name=? COLLATE NOCASE AND id!=?", (name, cid)
        ).fetchone()
        if dupe:
            return {"ok": False, "error": "name already used"}
        self._db.execute("UPDATE categories SET name=? WHERE id=?", (name, cid))
        self._db.commit()
        return {"ok": True}

    @_locked
    def update_task_name(self, task_id, name):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid task id"}
        name = clean_name(name)
        if not name:
            return {"ok": False, "error": "empty name"}
        self._db.execute("UPDATE tasks SET name=? WHERE id=?", (name, tid))
        self._db.commit()
        return {"ok": True}

    @_locked
    def set_priority(self, task_id, priority):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid task id"}
        new_priority = valid_priority(priority)
        row = self._db.execute("SELECT priority FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return {"ok": False, "error": "not found"}
        if new_priority != row["priority"]:
            self._db.execute("UPDATE tasks SET priority=? WHERE id=?", (new_priority, tid))
            self._db.commit()
        return {"ok": True, "priority": new_priority}

    @_locked
    def set_repeatable(self, task_id, repeatable):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid task id"}
        rep = 1 if repeatable else 0
        self._db.execute("UPDATE tasks SET repeatable=? WHERE id=?", (rep, tid))
        if rep:
            self._ensure_recur_group(tid)
        self._db.commit()
        return {"ok": True, "repeatable": rep}

    def _ensure_recur_group(self, tid: int):
        """Stamps a stable lineage id the first time a task becomes repeatable
        or hopscotch, so later respawned copies (see run_cleanup) and hopscotch's
        cross-row 'last completed' lookup can find each other.
        """
        row = self._db.execute("SELECT recur_group FROM tasks WHERE id=?", (tid,)).fetchone()
        if row and not row["recur_group"]:
            self._db.execute("UPDATE tasks SET recur_group=? WHERE id=?", (str(tid), tid))

    @_locked
    def set_hopscotch(self, task_id, hopscotch):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid task id"}
        hop = 1 if hopscotch else 0
        self._db.execute("UPDATE tasks SET hopscotch=? WHERE id=?", (hop, tid))
        if hop:
            self._ensure_recur_group(tid)
        self._db.commit()
        return {"ok": True, "hopscotch": hop}

    @_locked
    def reorder_tasks(self, scope, ordered_ids):
        scope = valid_scope(scope)
        ids = [valid_task_id(i) for i in (ordered_ids or [])]
        ids = [i for i in ids if i is not None]
        today = str(date.today())
        for idx, tid in enumerate(ids):
            if scope == "today":
                self._db.execute(
                    "UPDATE tasks SET sort_order=? WHERE id=? AND scope='today' AND today_date=?",
                    (idx, tid, today)
                )
            else:
                self._db.execute(
                    "UPDATE tasks SET sort_order=? WHERE id=? AND scope='weekly' AND completed=0",
                    (idx, tid)
                )
        self._db.commit()
        return {"ok": True}

    @_locked
    def update_category_color(self, category_id, color):
        cid = valid_task_id(category_id)
        if cid is None:
            return {"ok": False, "error": "invalid category id"}
        color = valid_color(color)
        self._db.execute("UPDATE categories SET color=? WHERE id=?", (color, cid))
        self._db.commit()
        return {"ok": True}

    @_locked
    def set_task_category(self, task_id, category_id):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid task id"}
        cid = valid_task_id(category_id)
        self._db.execute("UPDATE tasks SET category_id=? WHERE id=?", (cid, tid))
        self._db.commit()
        return {"ok": True}

    @_locked
    def mark_done(self, task_id):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid id"}
        row = self._db.execute(
            "SELECT hopscotch, recur_group FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "not found"}
        if row["hopscotch"]:
            group = row["recur_group"] or str(tid)
            last = self._hopscotch_last_done(group)
            if last:
                next_ok = date.fromisoformat(last) + timedelta(days=2)
                if date.today() < next_ok:
                    return {
                        "ok": False,
                        "error": f"Hopscotch — needs a rest day, available {next_ok}",
                        "hop_locked_until": str(next_ok),
                    }
        self._db.execute(
            "UPDATE tasks SET completed=1, completed_at=? WHERE id=?",
            (str(date.today()), tid)
        )
        self._db.commit()
        return {"ok": True}

    @_locked
    def unmark_done(self, task_id):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid id"}
        self._db.execute(
            "UPDATE tasks SET completed=0, completed_at=NULL WHERE id=?", (tid,)
        )
        self._db.commit()
        return {"ok": True}

    @_locked
    def promote_task(self, task_id):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid id"}
        today = str(date.today())
        cur = self._db.execute("""
            UPDATE tasks
            SET scope='today', today_date=?, completed=0, completed_at=NULL
            WHERE id=? AND scope='weekly'
        """, (today, tid))
        if cur.rowcount:
            self._append_history(tid, today)
        self._db.commit()
        return {"ok": True}

    @_locked
    def demote_task(self, task_id):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid id"}
        row = self._db.execute("SELECT today_date FROM tasks WHERE id=?", (tid,)).fetchone()
        # User-initiated demote = mistake; clear today_date so it never appears
        # in the week grid (it was never actually planned)
        self._db.execute(
            "UPDATE tasks SET scope='weekly', today_date=NULL WHERE id=?", (tid,)
        )
        if row and row["today_date"]:
            self._remove_history(tid, row["today_date"])
        self._db.commit()
        return {"ok": True}

    @_locked
    def delete_task(self, task_id):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid id"}
        self._db.execute("DELETE FROM tasks WHERE id=?", (tid,))
        self._db.commit()
        return {"ok": True}

    @_locked
    def get_stats(self):
        today = str(date.today())
        ws    = week_start_of(date.today())
        we    = str(ws + timedelta(days=6))
        ws    = str(ws)
        def count(q, *args):
            return self._db.execute(q, args).fetchone()[0]
        return {
            "total_today":   count("SELECT COUNT(*) FROM tasks WHERE today_date=?", today),
            "done_today":    count("SELECT COUNT(*) FROM tasks WHERE today_date=? AND completed=1", today),
            "weekly_count":  count("SELECT COUNT(*) FROM tasks WHERE scope='weekly' AND completed=0"),
            # Filtered by today_date, not scope: the 2AM cleanup flips unfinished
            # past-day tasks to scope='weekly' but keeps today_date, so counting
            # by scope alone would silently drop abandoned tasks from the week rate.
            "week_total":    count("SELECT COUNT(*) FROM tasks WHERE today_date BETWEEN ? AND ?", ws, we),
            "week_done":     count("SELECT COUNT(*) FROM tasks WHERE today_date BETWEEN ? AND ? AND completed=1", ws, we),
        }

    def get_config(self):
        return load_config()

    def save_config(self, morning_time, evening_time):
        mt, et = str(morning_time), str(evening_time)
        if not valid_time(mt):
            return {"ok": False, "error": "invalid morning_time"}
        if not valid_time(et):
            return {"ok": False, "error": "invalid evening_time"}
        cfg = load_config()
        cfg["morning_time"] = mt
        cfg["evening_time"] = et
        save_config(cfg)
        write_plists(mt, et)
        reload_agents()
        return {"ok": True}

    def save_theme(self, theme):
        theme = str(theme)
        if theme not in VALID_THEMES:
            return {"ok": False, "error": "unknown theme"}
        cfg = load_config()
        cfg["theme"] = theme
        # Move to front of recency list so the theme picker can surface
        # the 5 most recently used themes and tuck the rest in a submenu.
        recent = [t for t in cfg["recent_themes"] if t != theme]
        recent.insert(0, theme)
        cfg["recent_themes"] = recent[:len(VALID_THEMES)]
        save_config(cfg)
        return {"ok": True}

    # ── Runs (x tasks/day for x days, with x allowances) ──
    @_locked
    def get_runs(self):
        evaluate_runs(self._db)
        rows = self._db.execute("""
            SELECT r.*, c.name AS cat_name, c.color AS cat_color
            FROM runs r LEFT JOIN categories c ON r.category_id = c.id
            ORDER BY (r.status='active') DESC, r.created_date DESC, r.id DESC
        """).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["days"] = [dict(x) for x in self._db.execute(
                "SELECT date, count, hit, used_allowance FROM run_days WHERE run_id=? ORDER BY date",
                (r["id"],)
            ).fetchall()]
            out.append(d)
        return out

    @_locked
    def start_run(self, name, target_per_day, total_days, allowances, category_id=None):
        name = clean_name(name)[:60]
        if not name:
            return {"ok": False, "error": "empty name"}
        tpd    = positive_int(target_per_day, 1, 1, 50)
        days   = positive_int(total_days, 1, 1, 365)
        allow  = positive_int(allowances, 0, 0, 60)
        cid    = valid_task_id(category_id)
        today  = str(date.today())
        self._db.execute("""
            INSERT INTO runs (name, target_per_day, total_days, allowances_total, category_id, start_date, created_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (name, tpd, days, allow, cid, today, today))
        self._db.commit()
        return {"ok": True}

    @_locked
    def cancel_run(self, run_id):
        rid = valid_task_id(run_id)
        if rid is None:
            return {"ok": False, "error": "invalid id"}
        self._db.execute(
            "UPDATE runs SET status='cancelled', ended_date=? WHERE id=? AND status='active'",
            (str(date.today()), rid)
        )
        self._db.commit()
        return {"ok": True}

    @_locked
    def delete_run(self, run_id):
        rid = valid_task_id(run_id)
        if rid is None:
            return {"ok": False, "error": "invalid id"}
        self._db.execute("DELETE FROM run_days WHERE run_id=?", (rid,))
        self._db.execute("DELETE FROM runs WHERE id=?", (rid,))
        self._db.commit()
        return {"ok": True}

    def open_pin_window(self):
        return self.open_window("pin")

    def open_window(self, mode):
        mode = str(mode)
        if mode not in ("morning", "evening", "pin"):
            return {"ok": False, "error": "invalid mode"}
        log = open(str(DATA_DIR / f"{mode}.err"), "w")
        subprocess.Popen(
            [str(PYTHON_PATH), str(SCRIPT_PATH), mode],
            start_new_session=True,
            stdout=log,
            stderr=log,
        )
        return {"ok": True}


# ── launchd ───────────────────────────────────────────────────────────────────
def write_plists(morning_time: str, evening_time: str):
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    def cal(t: str) -> dict:
        h, m = t.split(":")
        return {"Hour": int(h), "Minute": int(m)}

    for path, mode, t in [
        (PLIST_MORNING, "morning", morning_time),
        (PLIST_EVENING, "evening", evening_time),
    ]:
        pl = {
            "Label":                 path.stem,
            "ProgramArguments":      [str(PYTHON_PATH), str(SCRIPT_PATH), mode],
            "StartCalendarInterval": cal(t),
            "RunAtLoad":             False,
            "StandardOutPath":       str(DATA_DIR / f"{mode}.log"),
            "StandardErrorPath":     str(DATA_DIR / f"{mode}.err"),
        }
        with open(path, "wb") as f:
            plistlib.dump(pl, f)

    cleanup_pl = {
        "Label":                 PLIST_CLEANUP.stem,
        "ProgramArguments":      [str(PYTHON_PATH), str(SCRIPT_PATH), "cleanup"],
        "StartCalendarInterval": {"Hour": 2, "Minute": 0},
        "RunAtLoad":             False,
        "StandardOutPath":       str(DATA_DIR / "cleanup.log"),
        "StandardErrorPath":     str(DATA_DIR / "cleanup.err"),
    }
    with open(PLIST_CLEANUP, "wb") as f:
        plistlib.dump(cleanup_pl, f)


def reload_agents():
    for path in (PLIST_MORNING, PLIST_EVENING, PLIST_CLEANUP):
        if path.exists():
            subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
            subprocess.run(["launchctl", "load",   str(path)], capture_output=True)


def _loaded_agent_labels() -> set:
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {line.split()[-1] for line in out.splitlines() if line.strip()}


def ensure_agents_loaded():
    """Defends against launchd silently dropping a RunAtLoad=false agent from
    its job table — observed in practice: the plist on disk was valid and
    correctly scheduled, but launchd simply didn't have it loaded, so it
    never fired and nothing anywhere recorded an error. `launchctl load` is
    a no-op if a label is already loaded, so this is cheap and safe to call
    opportunistically rather than only right after an explicit install/
    settings-save.
    """
    loaded = _loaded_agent_labels()
    for path in (PLIST_MORNING, PLIST_EVENING, PLIST_CLEANUP):
        if path.exists() and path.stem not in loaded:
            try:
                subprocess.run(["launchctl", "load", str(path)], capture_output=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass  # best-effort — never let a broken launchctl block a window from opening


# ── Single-instance IPC ────────────────────────────────────────────────────────
def _notify_running_instance(mode: str) -> bool:
    """If a morning/evening window is already open, ask it to switch tab and
    come to front instead of opening a second one. Returns True if an
    existing instance picked this up (caller should exit without opening
    a window); False if there's nothing listening (stale/missing socket),
    meaning the caller should proceed to open its own window and become
    the listener.
    """
    if not SOCK_PATH.exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(1.0)
        s.connect(str(SOCK_PATH))
        s.sendall(mode.encode("utf-8"))
        return True
    except OSError:
        try:
            SOCK_PATH.unlink()  # stale socket left behind by a crashed process
        except OSError:
            pass
        return False
    finally:
        s.close()


def _activate_app():
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass  # best-effort — a missed activation still leaves the tab switched


def _open_ipc_socket():
    """Binds the single-instance IPC listener on the caller's thread so the
    socket object exists before the GUI loop starts — see _serve_ipc for why.
    Returns None on failure (best-effort — without this, later invocations
    just open a 2nd window instead of switching tabs on the existing one).
    """
    try:
        if SOCK_PATH.exists():
            SOCK_PATH.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCK_PATH))
        srv.listen(4)
        return srv
    except OSError:
        return None


def _serve_ipc(window, srv):
    """Runs on a background thread (via webview.start's func arg) for the
    lifetime of the main window, dispatching tab-switch requests from later
    `planner morning`/`planner evening` invocations (launchd or terminal).

    webview.start() spawns this thread as a plain, non-daemon thread, and
    srv.accept() blocks with no timeout — so on its own this thread would
    never return, and a non-daemon thread still running blocks the whole
    process from exiting after the window is closed (seen as the `planner`
    command and the window both hanging after clicking the close button).
    run_window() closes `srv` from the window's `closed` event, which makes
    accept() raise and this loop exit, so the process can exit normally.
    """
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        try:
            data = conn.recv(64).decode("utf-8", errors="ignore").strip()
        finally:
            conn.close()
        if data in ("morning", "evening"):
            try:
                window.evaluate_js(f"window.selectTab && selectTab('{data}')")
            except Exception:
                pass
        _activate_app()
        try:
            window.restore()
        except Exception:
            pass


# ── Modes ─────────────────────────────────────────────────────────────────────
def run_cleanup():
    ensure_agents_loaded()  # self-heal siblings if launchd dropped one of them
    conn = open_db()
    init_db(conn)
    yesterday = str(date.today() - timedelta(days=1))
    today     = str(date.today())
    ws        = str(week_start_of(date.today()))

    # Repeatable tasks completed on a past day: spawn a fresh, uncompleted copy
    # in the weekly backlog so it can be replanned, while leaving the completed
    # row untouched as history (today_date keeps it visible in the week grid).
    repeats = conn.execute("""
        SELECT name, priority, category_id, recur_group, hopscotch FROM tasks
        WHERE scope='today' AND completed=1 AND repeatable=1 AND today_date <= ?
    """, (yesterday,)).fetchall()
    for r in repeats:
        conn.execute("""
            INSERT INTO tasks (name, priority, scope, completed, created_date, today_date, week_start, category_id, repeatable, recur_group, hopscotch)
            VALUES (?, ?, 'weekly', 0, ?, NULL, ?, ?, 1, ?, ?)
        """, (r["name"], r["priority"], today, ws, r["category_id"], r["recur_group"], r["hopscotch"]))
    # Flip those completed rows out of scope='today' so a future cleanup run
    # doesn't match today_date<=yesterday again and respawn duplicates forever.
    conn.execute("""
        UPDATE tasks SET scope='weekly'
        WHERE scope='today' AND completed=1 AND repeatable=1 AND today_date <= ?
    """, (yesterday,))

    # Keep today_date so the week grid shows the task was planned that day
    # (planned-and-failed history). Only the scope flips to weekly.
    conn.execute("""
        UPDATE tasks SET scope='weekly'
        WHERE scope='today' AND completed=0 AND today_date <= ?
    """, (yesterday,))
    conn.commit()

    evaluate_runs(conn)
    conn.close()


def run_status():
    ensure_agents_loaded()
    conn = open_db()
    init_db(conn)
    today = str(date.today())
    print(f"\n── {date.today().strftime('%A, %b %d')} ──")
    rows = conn.execute("""
        SELECT name, priority, completed FROM tasks
        WHERE today_date=? ORDER BY completed, priority
    """, (today,)).fetchall()
    if not rows:
        print("  No tasks today.")
    for r in rows:
        m = "✓" if r["completed"] else "○"
        p = {1: "H", 2: "M", 3: "L"}[r["priority"]]
        print(f"  [{m}][{p}] {r['name']}")
    wc = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE scope='weekly' AND completed=0"
    ).fetchone()[0]
    print(f"\n  Weekly backlog: {wc} item(s)\n")
    conn.close()


def run_install():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    save_config(cfg)
    write_plists(cfg["morning_time"], cfg["evening_time"])
    reload_agents()
    print(f"  Installed launchd agents.")
    print(f"  Morning: {cfg['morning_time']}  Evening: {cfg['evening_time']}")
    print(f"  Data dir: {DATA_DIR}")


def run_window(mode: str):
    try:
        import webview
    except ImportError:
        print("pywebview not installed. Run: pip3 install pywebview")
        sys.exit(1)

    if mode in ("morning", "evening") and _notify_running_instance(mode):
        print(f"Daily Planner is already open — switched to the {mode} tab.")
        return

    try:
        from AppKit import NSApplication, NSImage
        NSApplication.sharedApplication().setApplicationIconImage_(
            NSImage.alloc().initByReferencingFile_(str(ICON_PATH))
        )
    except Exception:
        pass  # best-effort — a missing/odd icon file shouldn't block the window

    ensure_agents_loaded()  # self-heal siblings if launchd dropped one of them

    conn = open_db()
    init_db(conn)

    cfg   = load_config()
    today = str(date.today())
    if mode in ("morning", "evening"):
        cfg[f"last_{mode}_shown"] = today
        save_config(cfg)

    api = PlannerAPI(conn)

    # "morning" and "evening" are two entry points into the same unified
    # window (app.html, tabbed) — launchd/terminal fallback still pick which
    # tab is active on open, but it's one persistent app, not two popups.
    files = {
        "morning": "app.html",
        "evening": "app.html",
        "pin":     "pin.html",
    }
    titles = {
        "morning": "Daily Planner",
        "evening": "Daily Planner",
        "pin":     "Tasks — Today",
    }
    sizes = {
        "morning": (1000, 760),
        "evening": (1000, 760),
        "pin":     (300, 500),
    }
    min_sizes = {
        "morning": (760, 560),
        "evening": (760, 560),
        "pin":     (260, 360),
    }

    w, h       = sizes.get(mode, (1000, 760))
    mw, mh     = min_sizes.get(mode, (760, 560))
    html_file  = UI_DIR / files.get(mode, "app.html")

    if not html_file.exists():
        print(f"UI file not found: {html_file}")
        sys.exit(1)

    # Read content directly — avoids file:// URL encoding issues with spaces in path
    html_content = html_file.read_text(encoding="utf-8")

    # Theme CSS (and, where used, the theme-picker JS) live in one shared file each
    # under ui/ and get spliced into the marker comment every window carries —
    # keeps all theme definitions in a single place instead of copy-pasted per window.
    themes_css = (UI_DIR / "themes.css").read_text(encoding="utf-8")
    html_content = html_content.replace("/* @@THEMES_CSS@@ */", themes_css)
    if mode in ("morning", "evening"):
        theme_menu_js = (UI_DIR / "theme-menu.js").read_text(encoding="utf-8")
        html_content = html_content.replace("// @@THEME_MENU_JS@@", theme_menu_js)
        html_content = html_content.replace("@@DEFAULT_TAB@@", mode)

    import webview
    window = webview.create_window(
        titles.get(mode, "Daily Planner"),
        html=html_content,
        js_api=api,
        width=w,
        height=h,
        on_top=(mode == "pin"),
        resizable=True,
        min_size=(mw, mh),
    )
    ipc_srv = None
    if mode in ("morning", "evening"):
        ipc_srv = _open_ipc_socket()
        if ipc_srv is not None:
            # Unblocks _serve_ipc's accept() when the window closes, so that
            # background thread actually exits instead of leaving the process
            # (and the closed window) hanging forever — see _serve_ipc.
            window.events.closed += ipc_srv.close

    try:
        if ipc_srv is not None:
            webview.start(_serve_ipc, (window, ipc_srv), debug=False)
        else:
            webview.start(debug=False)
    finally:
        if mode in ("morning", "evening"):
            try:
                SOCK_PATH.unlink()
            except OSError:
                pass
    conn.close()


# ── Entry ─────────────────────────────────────────────────────────────────────
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    {
        "cleanup": run_cleanup,
        "status":  run_status,
        "install": run_install,
    }.get(mode, lambda: (
        run_window(mode) if mode in ("morning", "evening", "pin")
        else (print("Usage: planner [morning|evening|pin|cleanup|status|install]") or sys.exit(1))
    ))()


if __name__ == "__main__":
    main()
