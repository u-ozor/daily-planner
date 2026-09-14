#!/usr/bin/env python3
"""
Daily Planner — windowed task planner for macOS.
Usage: planner [morning|evening|pin|cleanup|status|install]
"""
import sys
import json
import sqlite3
import subprocess
import plistlib
import re
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

PLIST_MORNING = AGENTS_DIR / "com.dailyplanner.morning.plist"
PLIST_EVENING = AGENTS_DIR / "com.dailyplanner.evening.plist"
PLIST_CLEANUP = AGENTS_DIR / "com.dailyplanner.cleanup.plist"

# Single source of truth for theme names — CSS definitions live in ui/themes.css,
# injected into every window's HTML at launch (see run_window).
VALID_THEMES = ("dark", "warm", "nebula", "silvergold", "starlight", "ember")

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
            repeatable   INTEGER NOT NULL DEFAULT 0
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
    conn.commit()


# ── Date helpers ──────────────────────────────────────────────────────────────
def week_start_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


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


# ── JS API (only public methods are exposed) ──────────────────────────────────
class PlannerAPI:
    def __init__(self, conn: sqlite3.Connection):
        self._db = conn

    def get_today_tasks(self):
        today = str(date.today())
        rows = self._db.execute("""
            SELECT t.id, t.name, t.priority, t.completed, t.completed_at, t.sort_order, t.repeatable,
                   c.name AS cat_name, c.color AS cat_color
            FROM tasks t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE t.scope='today' AND t.today_date=?
        """, (today,)).fetchall()
        return self._order_tasks([dict(r) for r in rows])

    def get_weekly_backlog(self):
        rows = self._db.execute("""
            SELECT t.id, t.name, t.priority, t.created_date, t.sort_order, t.completed, t.repeatable,
                   c.name AS cat_name, c.color AS cat_color
            FROM tasks t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE t.scope='weekly' AND t.completed=0
        """).fetchall()
        return self._order_tasks([dict(r) for r in rows])

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
            d["reordered"] = natural_rank[t["id"]] != i
            out.append(d)
        return out

    def get_week_overview(self):
        return self.get_period_overview(1)

    def get_period_overview(self, num_weeks=1):
        try:
            n = int(num_weeks)
        except (TypeError, ValueError):
            n = 1
        n = max(1, min(n, 5))  # cap ~5 weeks so a bad value can't trigger an unbounded scan
        today = date.today()
        start = week_start_of(today) - timedelta(weeks=n - 1)
        result = []
        for i in range(n * 7):
            d = start + timedelta(days=i)
            ds = str(d)
            rows = self._db.execute("""
                SELECT t.id, t.name, t.priority, t.completed,
                       c.color AS cat_color
                FROM tasks t
                LEFT JOIN categories c ON t.category_id = c.id
                WHERE t.today_date=?
                ORDER BY t.completed, t.priority
            """, (ds,)).fetchall()
            result.append({
                "date":     ds,
                "label":    d.strftime("%a").upper(),
                "day_num":  d.day,
                "is_today": ds == str(today),
                "tasks":    [dict(r) for r in rows],
            })
        return result

    def add_task(self, name, priority, scope, category_id=None, repeatable=0):
        name = clean_name(name)
        if not name:
            return {"ok": False, "error": "empty name"}
        priority   = valid_priority(priority)
        scope      = valid_scope(scope)
        today      = str(date.today())
        ws         = str(week_start_of(date.today()))
        today_date = today if scope == "today" else None
        cid        = valid_task_id(category_id)
        rep        = 1 if repeatable else 0
        self._db.execute("""
            INSERT INTO tasks (name, priority, scope, created_date, today_date, week_start, category_id, repeatable)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, priority, scope, today, today_date, ws, cid, rep))
        self._db.commit()
        return {"ok": True}

    def get_categories(self):
        rows = self._db.execute(
            "SELECT id, name, color FROM categories ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

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

    def set_repeatable(self, task_id, repeatable):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid task id"}
        rep = 1 if repeatable else 0
        self._db.execute("UPDATE tasks SET repeatable=? WHERE id=?", (rep, tid))
        self._db.commit()
        return {"ok": True, "repeatable": rep}

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

    def update_category_color(self, category_id, color):
        cid = valid_task_id(category_id)
        if cid is None:
            return {"ok": False, "error": "invalid category id"}
        color = valid_color(color)
        self._db.execute("UPDATE categories SET color=? WHERE id=?", (color, cid))
        self._db.commit()
        return {"ok": True}

    def set_task_category(self, task_id, category_id):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid task id"}
        cid = valid_task_id(category_id)
        self._db.execute("UPDATE tasks SET category_id=? WHERE id=?", (cid, tid))
        self._db.commit()
        return {"ok": True}

    def mark_done(self, task_id):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid id"}
        self._db.execute(
            "UPDATE tasks SET completed=1, completed_at=? WHERE id=?",
            (str(date.today()), tid)
        )
        self._db.commit()
        return {"ok": True}

    def unmark_done(self, task_id):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid id"}
        self._db.execute(
            "UPDATE tasks SET completed=0, completed_at=NULL WHERE id=?", (tid,)
        )
        self._db.commit()
        return {"ok": True}

    def promote_task(self, task_id):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid id"}
        self._db.execute("""
            UPDATE tasks
            SET scope='today', today_date=?, completed=0, completed_at=NULL
            WHERE id=? AND scope='weekly'
        """, (str(date.today()), tid))
        self._db.commit()
        return {"ok": True}

    def demote_task(self, task_id):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid id"}
        # User-initiated demote = mistake; clear today_date so it never appears
        # in the week grid (it was never actually planned)
        self._db.execute(
            "UPDATE tasks SET scope='weekly', today_date=NULL WHERE id=?", (tid,)
        )
        self._db.commit()
        return {"ok": True}

    def delete_task(self, task_id):
        tid = valid_task_id(task_id)
        if tid is None:
            return {"ok": False, "error": "invalid id"}
        self._db.execute("DELETE FROM tasks WHERE id=?", (tid,))
        self._db.commit()
        return {"ok": True}

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


# ── Modes ─────────────────────────────────────────────────────────────────────
def run_cleanup():
    conn = open_db()
    init_db(conn)
    yesterday = str(date.today() - timedelta(days=1))
    today     = str(date.today())
    ws        = str(week_start_of(date.today()))

    # Repeatable tasks completed on a past day: spawn a fresh, uncompleted copy
    # in the weekly backlog so it can be replanned, while leaving the completed
    # row untouched as history (today_date keeps it visible in the week grid).
    repeats = conn.execute("""
        SELECT name, priority, category_id FROM tasks
        WHERE scope='today' AND completed=1 AND repeatable=1 AND today_date <= ?
    """, (yesterday,)).fetchall()
    for r in repeats:
        conn.execute("""
            INSERT INTO tasks (name, priority, scope, completed, created_date, today_date, week_start, category_id, repeatable)
            VALUES (?, ?, 'weekly', 0, ?, NULL, ?, ?, 1)
        """, (r["name"], r["priority"], today, ws, r["category_id"]))
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
    conn.close()


def run_status():
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

    conn = open_db()
    init_db(conn)

    cfg   = load_config()
    today = str(date.today())
    if mode in ("morning", "evening"):
        cfg[f"last_{mode}_shown"] = today
        save_config(cfg)

    api = PlannerAPI(conn)

    titles = {
        "morning": "Daily Planner — Morning",
        "evening": "Daily Planner — Evening",
        "pin":     "Tasks — Today",
    }
    sizes = {
        "morning": (960, 720),
        "evening": (880, 660),
        "pin":     (300, 500),
    }
    min_sizes = {
        "morning": (720, 520),
        "evening": (680, 480),
        "pin":     (260, 360),
    }

    w, h       = sizes.get(mode, (880, 660))
    mw, mh     = min_sizes.get(mode, (680, 480))
    html_file  = UI_DIR / f"{mode}.html"

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

    import webview
    webview.create_window(
        titles.get(mode, "Daily Planner"),
        html=html_content,
        js_api=api,
        width=w,
        height=h,
        on_top=(mode == "pin"),
        resizable=True,
        min_size=(mw, mh),
    )
    webview.start(debug=False)
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
