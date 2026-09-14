#!/usr/bin/env python3
"""
Unit tests for planner.py's PlannerAPI and cleanup logic.

Runs against a throwaway SQLite DB in a temp directory — never touches the
real ~/Library/Application Support/DailyPlanner install.

Usage: python3 -m unittest discover -s tests
   or: python3 tests/test_planner.py
"""
import sys
import tempfile
import shutil
import unittest
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import planner


class PlannerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="planner_test_"))
        self._orig_data_dir = planner.DATA_DIR
        self._orig_db_path = planner.DB_PATH
        planner.DATA_DIR = self.tmpdir
        planner.DB_PATH = self.tmpdir / "tasks.db"
        self.conn = planner.open_db()
        planner.init_db(self.conn)
        self.api = planner.PlannerAPI(self.conn)

    def tearDown(self):
        self.conn.close()
        planner.DATA_DIR = self._orig_data_dir
        planner.DB_PATH = self._orig_db_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def reopen(self):
        """Simulate a fresh process re-opening the DB (as run_cleanup does)."""
        self.conn.close()
        self.conn = planner.open_db()
        self.api = planner.PlannerAPI(self.conn)

    def backdate(self, task_id, days_ago=1):
        d = str(date.today() - timedelta(days=days_ago))
        self.conn.execute("UPDATE tasks SET today_date=? WHERE id=?", (d, task_id))
        self.conn.commit()


class TestAddTask(PlannerTestCase):
    def test_add_defaults_not_repeatable(self):
        self.api.add_task("buy milk", 2, "today")
        t = self.api.get_today_tasks()[0]
        self.assertEqual(t["repeatable"], 0)

    def test_add_repeatable(self):
        self.api.add_task("water plants", 2, "today", None, 1)
        t = self.api.get_today_tasks()[0]
        self.assertEqual(t["repeatable"], 1)

    def test_add_empty_name_rejected(self):
        res = self.api.add_task("   ", 2, "today")
        self.assertFalse(res["ok"])

    def test_add_weekly_has_no_today_date(self):
        self.api.add_task("someday task", 2, "weekly")
        t = self.api.get_weekly_backlog()[0]
        self.assertIsNone(self.conn.execute(
            "SELECT today_date FROM tasks WHERE id=?", (t["id"],)
        ).fetchone()[0])


class TestToggles(PlannerTestCase):
    def test_set_repeatable_toggle(self):
        self.api.add_task("gym", 2, "today")
        tid = self.api.get_today_tasks()[0]["id"]
        r1 = self.api.set_repeatable(tid, 1)
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["repeatable"], 1)
        r2 = self.api.set_repeatable(tid, 0)
        self.assertEqual(r2["repeatable"], 0)

    def test_mark_and_unmark_done(self):
        self.api.add_task("task", 2, "today")
        tid = self.api.get_today_tasks()[0]["id"]
        self.api.mark_done(tid)
        self.assertEqual(self.api.get_today_tasks()[0]["completed"], 1)
        self.api.unmark_done(tid)
        self.assertEqual(self.api.get_today_tasks()[0]["completed"], 0)


class TestCleanupRollover(PlannerTestCase):
    """Unfinished today-tasks (repeatable or not) roll into weekly, unchanged."""

    def test_unfinished_task_rolls_to_weekly(self):
        self.api.add_task("unfinished", 2, "today")
        tid = self.api.get_today_tasks()[0]["id"]
        self.backdate(tid)
        planner.run_cleanup()
        self.reopen()
        weekly = [t for t in self.api.get_weekly_backlog() if t["name"] == "unfinished"]
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0]["completed"], 0)


class TestCleanupRepeat(PlannerTestCase):
    """Completed repeatable tasks spawn a fresh weekly copy; history is kept."""

    def test_completed_repeatable_respawns(self):
        self.api.add_task("water plants", 2, "today", None, 1)
        tid = self.api.get_today_tasks()[0]["id"]
        self.api.mark_done(tid)
        self.backdate(tid)

        planner.run_cleanup()
        self.reopen()

        weekly = self.api.get_weekly_backlog()
        self.assertEqual(len(weekly), 1)
        self.assertEqual(weekly[0]["name"], "water plants")
        self.assertEqual(weekly[0]["completed"], 0)
        self.assertEqual(weekly[0]["repeatable"], 1)

        rows = self.conn.execute(
            "SELECT scope, completed, today_date FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        self.assertEqual(rows["completed"], 1)          # history preserved
        self.assertIsNotNone(rows["today_date"])          # still shows in week grid
        self.assertEqual(rows["scope"], "weekly")          # moved out of scope='today'

    def test_no_duplicate_respawn_on_repeated_cleanup_runs(self):
        self.api.add_task("water plants", 2, "today", None, 1)
        tid = self.api.get_today_tasks()[0]["id"]
        self.api.mark_done(tid)
        self.backdate(tid)

        planner.run_cleanup()
        planner.run_cleanup()
        planner.run_cleanup()
        self.reopen()

        weekly = self.api.get_weekly_backlog()
        self.assertEqual(len(weekly), 1, "cleanup respawned duplicates on repeated runs")

    def test_completed_non_repeatable_not_respawned(self):
        self.api.add_task("one-off errand", 2, "today", None, 0)
        tid = self.api.get_today_tasks()[0]["id"]
        self.api.mark_done(tid)
        self.backdate(tid)

        planner.run_cleanup()
        self.reopen()

        weekly = [t for t in self.api.get_weekly_backlog() if t["name"] == "one-off errand"]
        self.assertEqual(len(weekly), 0)


class TestMigration(PlannerTestCase):
    """init_db must safely add repeatable/sort_order to a pre-existing DB."""

    def test_legacy_schema_gets_new_columns(self):
        self.conn.execute("DROP TABLE tasks")
        self.conn.executescript("""
            CREATE TABLE tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                priority     INTEGER NOT NULL DEFAULT 2,
                scope        TEXT    NOT NULL DEFAULT 'today',
                completed    INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT,
                created_date TEXT    NOT NULL,
                today_date   TEXT,
                week_start   TEXT    NOT NULL
            );
        """)
        self.conn.execute(
            "INSERT INTO tasks (name, created_date, week_start) VALUES ('legacy', '2026-01-01', '2026-01-01')"
        )
        self.conn.commit()

        planner.init_db(self.conn)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        self.assertIn("repeatable", cols)
        self.assertIn("sort_order", cols)
        self.assertIn("category_id", cols)


if __name__ == "__main__":
    unittest.main()
