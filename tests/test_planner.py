#!/usr/bin/env python3
"""
Unit tests for planner.py's PlannerAPI and cleanup logic.

Runs against a throwaway SQLite DB in a temp directory — never touches the
real ~/Library/Application Support/DailyPlanner install.

Usage: python3 -m unittest discover -s tests
   or: python3 tests/test_planner.py
"""
import io
import sys
import tempfile
import shutil
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

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

    def backdate_run(self, run_id, days_ago):
        d = str(date.today() - timedelta(days=days_ago))
        self.conn.execute("UPDATE runs SET start_date=? WHERE id=?", (d, run_id))
        self.conn.commit()

    def complete_on(self, task_id, days_ago):
        d = str(date.today() - timedelta(days=days_ago))
        self.conn.execute(
            "UPDATE tasks SET completed=1, completed_at=? WHERE id=?", (d, task_id)
        )
        self.conn.commit()

    def add_and_complete(self, name, days_ago, category_id=None):
        self.api.add_task(name, 2, "today", category_id)
        tid = self.conn.execute(
            "SELECT id FROM tasks WHERE name=?", (name,)
        ).fetchone()["id"]
        self.complete_on(tid, days_ago)
        return tid

    def latest_run_id(self):
        return self.conn.execute(
            "SELECT id FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]


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

    def test_add_task_with_future_target_date_is_not_in_today(self):
        future = str(date.today() + timedelta(days=5))
        self.api.add_task("future task", 2, "today", target_date=future)
        self.assertEqual(self.api.get_today_tasks(), [])
        row = self.conn.execute(
            "SELECT today_date FROM tasks WHERE name='future task'"
        ).fetchone()
        self.assertEqual(row["today_date"], future)

    def test_add_task_without_target_date_still_defaults_to_today(self):
        self.api.add_task("plain today task", 2, "today")
        row = self.conn.execute(
            "SELECT today_date FROM tasks WHERE name='plain today task'"
        ).fetchone()
        self.assertEqual(row["today_date"], str(date.today()))

    def test_invalid_target_date_falls_back_to_today(self):
        self.api.add_task("bad date task", 2, "today", target_date="not-a-date")
        row = self.conn.execute(
            "SELECT today_date FROM tasks WHERE name='bad date task'"
        ).fetchone()
        self.assertEqual(row["today_date"], str(date.today()))


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


class TestSearch(PlannerTestCase):
    """Finder: a keyword should surface a task whether it matches the task's
    own text or only its category's name, regardless of scope or completion."""

    def test_matches_task_name_case_insensitively(self):
        self.api.add_task("Buy milk", 2, "today")
        self.assertEqual(len(self.api.search_tasks("MILK")), 1)
        self.assertEqual(self.api.search_tasks("bread"), [])

    def test_matches_by_category_even_when_task_name_does_not(self):
        cid = self.api.add_category("Questrade", "#6aaef0")["id"]
        self.api.add_task("fund account", 2, "weekly", category_id=cid)
        results = self.api.search_tasks("questrade")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["cat_name"], "Questrade")

    def test_finds_tasks_across_both_scopes(self):
        self.api.add_task("today thing", 2, "today")
        self.api.add_task("weekly thing", 2, "weekly")
        results = self.api.search_tasks("thing")
        self.assertEqual({r["scope"] for r in results}, {"today", "weekly"})

    def test_includes_completed_tasks(self):
        self.api.add_task("archived task", 2, "today")
        tid = self.api.get_today_tasks()[0]["id"]
        self.api.mark_done(tid)
        results = self.api.search_tasks("archived")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["completed"], 1)

    def test_blank_query_returns_nothing(self):
        self.api.add_task("anything", 2, "today")
        self.assertEqual(self.api.search_tasks(""), [])
        self.assertEqual(self.api.search_tasks("   "), [])


class TestHopscotch(PlannerTestCase):
    """Hopscotch tasks need a full empty day between completions."""

    def test_non_hopscotch_task_completes_freely(self):
        self.api.add_task("plain", 2, "today")
        tid = self.api.get_today_tasks()[0]["id"]
        res = self.api.mark_done(tid)
        self.assertTrue(res["ok"])

    def test_completing_again_the_next_day_is_blocked(self):
        self.api.add_task("pushups", 2, "today", None, 0, 1)
        tid = self.api.get_today_tasks()[0]["id"]
        self.complete_on(tid, days_ago=1)  # done yesterday
        res = self.api.mark_done(tid)
        self.assertFalse(res["ok"])
        self.assertIn("hop_locked_until", res)

    def test_remarking_done_same_day_after_undo_is_allowed(self):
        self.api.add_task("pushups", 2, "today", None, 0, 1)
        tid = self.api.get_today_tasks()[0]["id"]
        self.assertTrue(self.api.mark_done(tid)["ok"])
        self.api.unmark_done(tid)
        # completed_at cleared by unmark, so re-completing the same (empty) day is fine
        self.assertTrue(self.api.mark_done(tid)["ok"])

    def test_completing_after_a_full_rest_day_is_allowed(self):
        self.api.add_task("pushups", 2, "today", None, 0, 1)
        tid = self.api.get_today_tasks()[0]["id"]
        self.complete_on(tid, days_ago=2)
        res = self.api.mark_done(tid)
        self.assertTrue(res["ok"])

    def test_gate_follows_the_task_across_a_repeat_respawn(self):
        self.api.add_task("pushups", 2, "today", None, 1, 1)
        tid = self.api.get_today_tasks()[0]["id"]
        self.api.mark_done(tid)
        self.backdate(tid)
        planner.run_cleanup()
        self.reopen()
        respawned = next(t for t in self.api.get_weekly_backlog() if t["name"] == "pushups")
        self.assertTrue(respawned["hopscotch"])
        self.api.promote_task(respawned["id"])
        res = self.api.mark_done(respawned["id"])
        self.assertFalse(res["ok"])

    def test_toggling_hopscotch_on_assigns_a_recur_group(self):
        self.api.add_task("stretch", 2, "today")
        tid = self.api.get_today_tasks()[0]["id"]
        self.api.set_hopscotch(tid, 1)
        group = self.conn.execute(
            "SELECT recur_group FROM tasks WHERE id=?", (tid,)
        ).fetchone()["recur_group"]
        self.assertEqual(group, str(tid))


class TestCategories(PlannerTestCase):
    def test_rename_category(self):
        cat = self.api.add_category("reading", "#6aaef0")
        res = self.api.rename_category(cat["id"], "books")
        self.assertTrue(res["ok"])
        names = [c["name"] for c in self.api.get_categories()]
        self.assertIn("books", names)

    def test_rename_to_existing_name_is_rejected(self):
        self.api.add_category("reading", "#6aaef0")
        cat2 = self.api.add_category("fitness", "#74cc7c")
        res = self.api.rename_category(cat2["id"], "reading")
        self.assertFalse(res["ok"])


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
        self.assertIn("history_dates", cols)


class TestPlannedHistory(PlannerTestCase):
    """history_dates is a small capped (last 5) log of every day a task was
    actually planned, so the Week-at-a-Glance grid keeps showing it on earlier
    days even after it's re-promoted on a later one — which otherwise
    overwrites the single today_date field and erases that earlier history."""

    def test_reprompted_task_still_shows_on_the_earlier_day(self):
        self.api.add_task("write", 2, "weekly")
        tid = self.conn.execute("SELECT id FROM tasks WHERE name='write'").fetchone()["id"]

        two_days_ago = str(date.today() - timedelta(days=2))
        self.conn.execute(
            "UPDATE tasks SET scope='weekly', today_date=?, history_dates=? WHERE id=?",
            (two_days_ago, two_days_ago, tid)
        )
        self.conn.commit()

        res = self.api.promote_task(tid)  # re-promote to today
        self.assertTrue(res["ok"])

        days_by_date = {d["date"]: d for d in self.api.get_period_overview(2)}
        self.assertTrue(any(t["id"] == tid for t in days_by_date[two_days_ago]["tasks"]))
        today_str = str(date.today())
        self.assertTrue(any(t["id"] == tid for t in days_by_date[today_str]["tasks"]))

    def test_demoted_task_disappears_from_the_grid(self):
        self.api.add_task("write", 2, "today")
        tid = self.conn.execute("SELECT id FROM tasks WHERE name='write'").fetchone()["id"]

        res = self.api.demote_task(tid)
        self.assertTrue(res["ok"])

        today_str = str(date.today())
        today_cell = next(d for d in self.api.get_period_overview(1) if d["date"] == today_str)
        self.assertFalse(any(t["id"] == tid for t in today_cell["tasks"]))

    def test_history_caps_at_five_most_recent_days(self):
        self.api.add_task("write", 2, "weekly")
        tid = self.conn.execute("SELECT id FROM tasks WHERE name='write'").fetchone()["id"]
        days = [str(date.today() - timedelta(days=d)) for d in range(7, 0, -1)]  # oldest first
        for d in days:
            self.api._append_history(tid, d)

        history = self.conn.execute(
            "SELECT history_dates FROM tasks WHERE id=?", (tid,)
        ).fetchone()["history_dates"].split(",")
        self.assertEqual(history, list(reversed(days))[:5])  # newest-first; two oldest evicted


class TestScheduleTask(PlannerTestCase):
    """schedule_task is the backlog-side 'send to a specific day' action —
    like promote_task but to an arbitrary future day instead of always today."""

    def test_schedules_backlog_task_to_a_future_day(self):
        self.api.add_task("write", 2, "weekly")
        tid = self.conn.execute("SELECT id FROM tasks WHERE name='write'").fetchone()["id"]

        target = str(date.today() + timedelta(days=3))
        res = self.api.schedule_task(tid, target)
        self.assertTrue(res["ok"])

        row = self.conn.execute(
            "SELECT scope, today_date FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        self.assertEqual(row["scope"], "today")
        self.assertEqual(row["today_date"], target)

    def test_rejects_a_past_date(self):
        self.api.add_task("write", 2, "weekly")
        tid = self.conn.execute("SELECT id FROM tasks WHERE name='write'").fetchone()["id"]

        yesterday = str(date.today() - timedelta(days=1))
        res = self.api.schedule_task(tid, yesterday)
        self.assertFalse(res["ok"])

        row = self.conn.execute("SELECT scope FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["scope"], "weekly")

    def test_rejects_an_invalid_date_string(self):
        self.api.add_task("write", 2, "weekly")
        tid = self.conn.execute("SELECT id FROM tasks WHERE name='write'").fetchone()["id"]

        res = self.api.schedule_task(tid, "not-a-date")
        self.assertFalse(res["ok"])

    def test_only_applies_to_weekly_scoped_tasks(self):
        self.api.add_task("write", 2, "today")
        tid = self.conn.execute("SELECT id FROM tasks WHERE name='write'").fetchone()["id"]
        original_date = self.conn.execute(
            "SELECT today_date FROM tasks WHERE id=?", (tid,)
        ).fetchone()["today_date"]

        target = str(date.today() + timedelta(days=3))
        res = self.api.schedule_task(tid, target)
        self.assertTrue(res["ok"])  # no-op, not an error — matches promote_task's own behavior

        row = self.conn.execute(
            "SELECT scope, today_date FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        self.assertEqual(row["scope"], "today")
        self.assertEqual(row["today_date"], original_date)  # untouched


class TestConcurrentApiCalls(PlannerTestCase):
    """Regression test for the pywebview threading model: every JS->Python
    call runs in its own freshly spawned thread with no serialization of its
    own (webview/util.py's js_bridge_call does `Thread(target=_call).start()`
    per call), and the UI fires several calls at once via Promise.all
    (loadAll(), every 60s). sqlite3.Connection isn't safe for concurrent use
    across threads without an explicit lock, which is what PlannerAPI's
    @_locked decorator provides. This hammers the shared connection from
    many threads the way the real app does, to catch any regression of that
    lock (it manifested as a stray IndexError/InterfaceError out of nowhere)."""

    def test_many_threads_hitting_the_shared_connection_do_not_raise(self):
        self.api.add_task("task one", 2, "today")
        self.api.add_category("reading", "#6aaef0")
        self.api.start_run("read", 1, 10, 2)

        errors = []

        def hammer():
            try:
                for _ in range(20):
                    self.api.get_period_overview(2)
                    self.api.get_today_tasks()
                    self.api.get_weekly_backlog()
                    self.api.get_stats()
                    self.api.get_runs()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])


class TestRuns(PlannerTestCase):
    """x tasks/day for x days, with x allowances covering missed days."""

    def test_hit_day_counts_and_uses_no_allowance(self):
        self.api.start_run("read", 1, 10, 0)
        rid = self.latest_run_id()
        self.backdate_run(rid, 1)
        self.add_and_complete("read 10 pages", 1)

        planner.evaluate_runs(self.conn)
        run = self.api.get_runs()[0]
        self.assertEqual(run["status"], "active")
        self.assertEqual(run["allowances_used"], 0)
        self.assertEqual(len(run["days"]), 1)
        self.assertEqual(run["days"][0]["hit"], 1)

    def test_miss_day_consumes_one_allowance(self):
        self.api.start_run("read", 1, 10, 2)
        rid = self.latest_run_id()
        self.backdate_run(rid, 1)

        planner.evaluate_runs(self.conn)
        run = self.api.get_runs()[0]
        self.assertEqual(run["status"], "active")
        self.assertEqual(run["allowances_used"], 1)
        self.assertEqual(run["days"][0]["used_allowance"], 1)

    def test_run_breaks_once_allowances_are_exhausted(self):
        self.api.start_run("read", 1, 10, 1)
        rid = self.latest_run_id()
        self.backdate_run(rid, 2)

        planner.evaluate_runs(self.conn)
        run = self.api.get_runs()[0]
        self.assertEqual(run["status"], "broken")
        self.assertEqual(run["allowances_used"], 1)
        self.assertEqual(len(run["days"]), 2)
        self.assertEqual([d["hit"] for d in run["days"]], [0, 0])

    def test_run_completes_after_total_days(self):
        self.api.start_run("read", 1, 2, 0)
        rid = self.latest_run_id()
        self.backdate_run(rid, 2)
        self.add_and_complete("day one", 2)
        self.add_and_complete("day two", 1)

        planner.evaluate_runs(self.conn)
        run = self.api.get_runs()[0]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(run["days"]), 2)

    def test_category_filter_matching_task_counts_as_one_full_task(self):
        cat = self.api.add_category("reading", "#6aaef0")
        self.api.start_run("read", 1, 5, 0, cat["id"])
        rid = self.latest_run_id()
        self.backdate_run(rid, 1)
        self.add_and_complete("read 10 pages", 1, cat["id"])

        planner.evaluate_runs(self.conn)
        run = self.api.get_runs()[0]
        self.assertEqual(run["status"], "active")
        self.assertEqual(run["days"][0]["count"], 1)

    def test_category_filter_other_category_completion_counts_as_half_a_task(self):
        cat = self.api.add_category("reading", "#6aaef0")
        self.api.start_run("read", 1, 5, 0, cat["id"])
        rid = self.latest_run_id()
        self.backdate_run(rid, 1)
        self.add_and_complete("unrelated errand", 1)  # other category -> half credit, not enough alone

        planner.evaluate_runs(self.conn)
        run = self.api.get_runs()[0]
        self.assertEqual(run["status"], "broken")
        self.assertEqual(run["days"][0]["count"], 0.5)

    def test_category_filter_two_other_category_completions_add_up_to_a_hit(self):
        cat = self.api.add_category("reading", "#6aaef0")
        self.api.start_run("read", 1, 5, 0, cat["id"])
        rid = self.latest_run_id()
        self.backdate_run(rid, 1)
        self.add_and_complete("errand one", 1)
        self.add_and_complete("errand two", 1)  # two half-credits = one full task

        planner.evaluate_runs(self.conn)
        run = self.api.get_runs()[0]
        self.assertEqual(run["status"], "active")
        self.assertEqual(run["days"][0]["hit"], 1)
        self.assertEqual(run["days"][0]["count"], 1.0)

    def test_rest_day_category_satisfies_run_without_target_or_allowance(self):
        rest_cat = self.api.add_category("rest", "#999999")
        self.api.set_category_rest(rest_cat["id"], 1)
        self.api.start_run("read", 5, 10, 0)  # unmet target_per_day on its own
        rid = self.latest_run_id()
        self.backdate_run(rid, 1)
        self.add_and_complete("nothing today", 1, rest_cat["id"])

        planner.evaluate_runs(self.conn)
        run = self.api.get_runs()[0]
        self.assertEqual(run["status"], "active")
        self.assertEqual(run["allowances_used"], 0)
        self.assertEqual(run["days"][0]["hit"], 1)
        self.assertEqual(run["days"][0]["used_allowance"], 0)
        self.assertEqual(run["days"][0]["is_rest"], 1)

    def test_unflagged_category_completion_does_not_act_as_rest_day(self):
        cat = self.api.add_category("errands", "#999999")  # counts_as_rest defaults to 0
        self.api.start_run("read", 5, 10, 0)
        rid = self.latest_run_id()
        self.backdate_run(rid, 1)
        self.add_and_complete("nothing today", 1, cat["id"])

        planner.evaluate_runs(self.conn)
        run = self.api.get_runs()[0]
        self.assertEqual(run["status"], "broken")
        self.assertEqual(run["days"][0]["hit"], 0)
        self.assertEqual(run["days"][0]["is_rest"], 0)

    def test_evaluate_runs_is_idempotent(self):
        self.api.start_run("read", 1, 10, 3)
        rid = self.latest_run_id()
        self.backdate_run(rid, 1)

        planner.evaluate_runs(self.conn)
        planner.evaluate_runs(self.conn)
        planner.evaluate_runs(self.conn)

        run = self.api.get_runs()[0]
        self.assertEqual(len(run["days"]), 1)
        self.assertEqual(run["allowances_used"], 1)

    def test_cancel_and_delete_run(self):
        self.api.start_run("read", 1, 10, 0)
        rid = self.latest_run_id()

        res = self.api.cancel_run(rid)
        self.assertTrue(res["ok"])
        run = next(r for r in self.api.get_runs() if r["id"] == rid)
        self.assertEqual(run["status"], "cancelled")

        res2 = self.api.delete_run(rid)
        self.assertTrue(res2["ok"])
        self.assertEqual(self.api.get_runs(), [])


class TestMonthOverview(PlannerTestCase):
    def test_grid_is_full_weeks_covering_the_month(self):
        overview = self.api.get_month_overview(2026, 11)
        days = overview["days"]
        self.assertEqual(len(days) % 7, 0)
        self.assertEqual(date.fromisoformat(days[0]["date"]).weekday(), 0)   # Monday
        self.assertEqual(date.fromisoformat(days[-1]["date"]).weekday(), 6)  # Sunday
        first_of_month = next(d for d in days if d["date"] == "2026-11-01")
        self.assertTrue(first_of_month["in_month"])

    def test_tasks_appear_under_their_date(self):
        # Day 20-ish of the current month, clamped to stay inside short months.
        year, month = date.today().year, date.today().month
        target = str(date(year, month, 1) + timedelta(days=min(19, calendar_len(year, month) - 1)))
        self.api.add_task("mid-month item", 1, "today", target_date=target)
        overview = self.api.get_month_overview(year, month)
        day = next(d for d in overview["days"] if d["date"] == target)
        self.assertEqual([t["name"] for t in day["tasks"]], ["mid-month item"])

    def test_invalid_year_month_falls_back_to_current_month(self):
        overview = self.api.get_month_overview("bad", "bad")
        today = date.today()
        self.assertEqual(overview["year"], today.year)
        self.assertEqual(overview["month"], today.month)


def calendar_len(year, month):
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    return (nxt - date(year, month, 1)).days


class TestScheduleCLI(PlannerTestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            planner.run_schedule(argv)
        return buf.getvalue()

    def test_schedule_adds_task_on_given_date(self):
        future = str(date.today() + timedelta(days=3))
        self._run(["--name", "cli task", "--date", future, "--priority", "high"])
        row = self.conn.execute(
            "SELECT priority, today_date FROM tasks WHERE name='cli task'"
        ).fetchone()
        self.assertEqual(row["today_date"], future)
        self.assertEqual(row["priority"], 1)

    def test_schedule_weekly_scope_ignores_missing_date(self):
        self._run(["--name", "cli weekly task", "--scope", "weekly"])
        row = self.conn.execute(
            "SELECT scope, today_date FROM tasks WHERE name='cli weekly task'"
        ).fetchone()
        self.assertEqual(row["scope"], "weekly")
        self.assertIsNone(row["today_date"])

    def test_schedule_requires_date_for_today_scope(self):
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit):
            planner.run_schedule(["--name", "no date task"])
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM tasks WHERE name='no date task'"
        ).fetchone())

    def test_schedule_rejects_invalid_date(self):
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit):
            planner.run_schedule(["--name", "bad date task", "--date", "nope"])
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM tasks WHERE name='bad date task'"
        ).fetchone())

    def test_schedule_resolves_existing_category_without_changing_its_color(self):
        cat = self.api.add_category("Driver Prep", "#123456")
        future = str(date.today() + timedelta(days=1))
        self._run(["--name", "quiz review", "--date", future, "--category", "driver prep"])
        row = self.conn.execute("""
            SELECT t.category_id, c.color FROM tasks t JOIN categories c ON t.category_id = c.id
            WHERE t.name='quiz review'
        """).fetchone()
        self.assertEqual(row["category_id"], cat["id"])
        self.assertEqual(row["color"], "#123456")

    def test_schedule_creates_missing_category(self):
        future = str(date.today() + timedelta(days=1))
        self._run(["--name", "new cat task", "--date", future, "--category", "Brand New Cat"])
        row = self.conn.execute("""
            SELECT c.name FROM tasks t JOIN categories c ON t.category_id = c.id
            WHERE t.name='new cat task'
        """).fetchone()
        self.assertEqual(row["name"], "Brand New Cat")


class TestEnsureAgentsLoaded(unittest.TestCase):
    """launchd can silently drop a RunAtLoad=false agent from its job table
    (observed in practice: the morning agent's plist was valid and correctly
    scheduled, but simply wasn't loaded, so it never fired). These tests
    cover the self-healing check that catches that without a real launchd."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="agents_test_"))
        self.morning = self.tmpdir / "com.dailyplanner.morning.plist"
        self.evening = self.tmpdir / "com.dailyplanner.evening.plist"
        self.cleanup = self.tmpdir / "com.dailyplanner.cleanup.plist"
        for p in (self.morning, self.evening, self.cleanup):
            p.write_text("")
        self._orig = (planner.PLIST_MORNING, planner.PLIST_EVENING, planner.PLIST_CLEANUP)
        planner.PLIST_MORNING, planner.PLIST_EVENING, planner.PLIST_CLEANUP = (
            self.morning, self.evening, self.cleanup,
        )

    def tearDown(self):
        planner.PLIST_MORNING, planner.PLIST_EVENING, planner.PLIST_CLEANUP = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _load_calls(mock_run):
        return [c.args[0] for c in mock_run.call_args_list if "load" in c.args[0]]

    @patch("planner.subprocess.run")
    def test_reloads_only_the_missing_label(self, mock_run):
        list_result = MagicMock()
        list_result.stdout = (
            "-\t0\tcom.dailyplanner.evening\n"
            "-\t0\tcom.dailyplanner.cleanup\n"
        )
        mock_run.return_value = list_result

        planner.ensure_agents_loaded()

        loaded_paths = {call[-1] for call in self._load_calls(mock_run)}
        self.assertEqual(loaded_paths, {str(self.morning)})

    @patch("planner.subprocess.run")
    def test_noop_when_everything_already_loaded(self, mock_run):
        list_result = MagicMock()
        list_result.stdout = (
            "-\t0\tcom.dailyplanner.morning\n"
            "-\t0\tcom.dailyplanner.evening\n"
            "-\t0\tcom.dailyplanner.cleanup\n"
        )
        mock_run.return_value = list_result

        planner.ensure_agents_loaded()

        self.assertEqual(self._load_calls(mock_run), [])

    @patch("planner.subprocess.run")
    def test_launchctl_list_failure_does_not_raise(self, mock_run):
        mock_run.side_effect = OSError("launchctl not found")
        try:
            planner.ensure_agents_loaded()
        except OSError:
            self.fail("ensure_agents_loaded() must not raise if launchctl is unavailable")


class TestSingleInstanceIPC(unittest.TestCase):
    """A launchd fire (or terminal `planner morning`) while the window is
    already open should focus+switch tabs there, not spawn a second window."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(dir="/tmp", prefix="ipc_test_"))
        self.sock_path = self.tmpdir / "planner.sock"
        self._orig_sock = planner.SOCK_PATH
        planner.SOCK_PATH = self.sock_path

    def tearDown(self):
        planner.SOCK_PATH = self._orig_sock
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_socket_file_means_no_running_instance(self):
        self.assertFalse(planner._notify_running_instance("morning"))

    def test_stale_socket_file_is_cleaned_up(self):
        self.sock_path.write_text("")  # left behind by a crashed process; nothing is listening
        self.assertFalse(planner._notify_running_instance("evening"))
        self.assertFalse(self.sock_path.exists())

    @patch("planner._activate_app")
    def test_running_instance_receives_mode_and_switches_tab(self, mock_activate):
        window = MagicMock()
        srv = planner._open_ipc_socket()
        self.assertIsNotNone(srv)
        server_thread = threading.Thread(target=planner._serve_ipc, args=(window, srv), daemon=True)
        server_thread.start()
        for _ in range(50):  # wait for the server to bind and start listening
            if self.sock_path.exists():
                break
            time.sleep(0.02)

        handled = planner._notify_running_instance("evening")
        self.assertTrue(handled)

        for _ in range(50):  # wait for the accept loop to process the message
            if window.evaluate_js.called:
                break
            time.sleep(0.02)
        window.evaluate_js.assert_called_once()
        self.assertIn("evening", window.evaluate_js.call_args.args[0])
        mock_activate.assert_called_once()

    def test_closing_the_socket_unblocks_the_accept_loop(self):
        """Simulates what window.events.closed now does: closing the server
        socket from outside must make the thread's accept() return/raise so
        the (non-daemon, in real life) thread actually exits instead of
        hanging the process — the bug behind this fix.
        """
        window = MagicMock()
        srv = planner._open_ipc_socket()
        self.assertIsNotNone(srv)
        server_thread = threading.Thread(target=planner._serve_ipc, args=(window, srv))
        server_thread.start()
        for _ in range(50):
            if self.sock_path.exists():
                break
            time.sleep(0.02)

        srv.close()
        server_thread.join(timeout=2)
        self.assertFalse(server_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
