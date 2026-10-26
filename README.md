---
type: project
status: active
area:
tags: [macos, productivity, tool]
root: projects/daily-planner
deadline:
created: 2026-10-11
---
# Daily Planner

A local, windowed daily planning tool for macOS: one persistent app window with Morning and Evening tabs, backed by a SQLite task database and driven by `launchd` (macOS's native scheduler) for scheduled pop-opens and nightly maintenance.

No cloud, no subscriptions, no telemetry. Everything runs locally.

---

## Features

- **Two-bucket planning** — every task is either "today" or "weekly backlog"; nothing spans days
- **Week at a Glance grid** — scrollable week-by-week view (current week plus up to ~3 months of history) of what was planned/completed each day, with a hover popover for overflow days
- **Priorities** — High / Medium / Low, set on add or changed later via a click-to-pick badge
- **Categories** — named, colored tags shared across all tasks; both color and name are editable inline wherever a category is selectable, and recolor/rename updates every task using that category at once
- **Repeating tasks** — mark a task repeatable; completing it doesn't end it — the 2 AM agent re-adds it to the weekly backlog automatically
- **Hopscotch tasks** (🦘) — mark a task so it can't be completed again until a full day has passed since the last time — see [Hopscotch Tasks](#hopscotch-tasks)
- **Manual drag-reorder** — drag the `⋮⋮` handle to override priority sort order; a marker flags tasks moved out of natural order
- **Inline editing** — double-click any task name to rename it in place
- **Markdown-style formatting** — `**bold**`, `*italic*`, `~~strikethrough~~` in task names
- **Pinned always-on-top tracker** — a small floating window with live checkboxes for today's tasks, independently zoomable
- **Evening review** — completion stats, a "dopamine" summary message, and a Week/2 Weeks/Month completion chart with click-through day detail
- **Runs** — commit to x qualifying tasks a day for x days, with x allowances covering a missed day before the run breaks; see [Runs](#runs) below
- **Year countdown** — header chip showing days remaining in the current year
- **One persistent app, two tabs** — Morning and Evening live in the same window; both stay live-refreshed regardless of which tab is active, and the window keeps working correctly across a midnight rollover instead of going stale
- **6 animated themes** — Dark, Warm, Nebula, Silver & Gold, Starlight, Ember — shared across the app and the pinned tracker, with a recently-used quick-picker
- **2 AM auto-move** — unfinished today-tasks silently roll into the weekly backlog overnight, no popup
- **launchd-scheduled opens** — the app opens at the morning/evening times you set, defaulting to the matching tab, catching up on wake if your Mac was asleep; if the window is already open, it's brought to front and switched to that tab instead of opening a second window
- **Local-only SQLite storage** — no network calls, no accounts, inspectable with any SQLite tool

---

## How It Works

Two `launchd` agents fire at times you set — each one opens the same app window (`pywebview`, embedding macOS's WKWebView, the same rendering engine as Safari without any browser chrome), defaulting to the Morning or Evening tab depending on which agent fired. If your Mac was asleep at the scheduled time, the window appears when it wakes up. A third silent agent runs at 2:00 AM to move uncompleted today-tasks into the weekly backlog and evaluate active Runs.

Your data lives in `~/Library/Application Support/DailyPlanner/`:
- `tasks.db` — SQLite database (WAL mode, safe for concurrent access)
- `config.json` — scheduled times and theme preference
- `venv/` — isolated Python environment with pywebview

LaunchAgent plists live in `~/Library/LaunchAgents/`.

---

## The App

One window, shared header, two tabs — plus a separate pinned tracker. Opening via `planner morning` or `planner evening` (or launchd firing at your scheduled times) opens the same window and selects the matching tab; from there, click between tabs freely. Both tabs' data refresh in the background every 60 seconds and immediately after any action, so leaving the window open all day keeps it accurate.

**Shared header** — date, live clock, and a countdown chip showing days left in the current year.

**Runs bar** — sits just under the header, visible from either tab. See [Runs](#runs) below.

### Morning tab

**Week at a Glance** — top section, a 7-column grid per week, stacked and scrollable. It opens scrolled to the current Mon–Sun week at the bottom; scroll up to pull in older weeks (loaded in batches of 4, up to ~3 months back — it's all already in the database, just not fetched until you scroll there). Each column shows tasks that were in "today" scope on that day, as dots:
- Colored dot = planned, not completed (category color if assigned, otherwise priority color)
- Grey dot = completed

Each day column has a fixed height, capped at 4 visible rows. A day with more than 4 tasks shows its first 3 dots plus a `+N more` line instead of stretching the grid — hover `+N more` to see the rest of that day's tasks in a popover.

Tasks moved to weekly by the 2AM agent (planned but not done) stay visible as uncompleted dots — they're part of the historical record. Tasks demoted back to weekly by you (a mistake, not a failure) disappear from the grid.

**Today panel** — left side. Each task has:
- A checkbox to mark done/undone
- `⋮⋮` drag handle — drag the row to reorder; overrides the default priority sort. A red dot marks tasks moved out of natural order
- Priority badge (H / M / L) — click to open a picker and change it
- Double-click the task name to edit it in place — Enter or clicking away saves, Esc cancels
- `↻` button — toggles the task **repeatable** (see [Repeating Tasks](#repeating-tasks) below); highlighted when active
- `🦘` button — toggles **hopscotch** mode (see [Hopscotch Tasks](#hopscotch-tasks) below); highlighted when active
- `→w` button — moves the task back to weekly and removes it from the week grid (treats it as a planning mistake, not a failure)
- `✕` button — deletes the task entirely

**Weekly Backlog panel** — right side. Tasks accumulate here. Each has:
- `⋮⋮` drag handle, priority badge, and inline rename, same as the Today panel
- `↻` button — toggle repeatable, same as the Today panel
- `🦘` button — toggle hopscotch, same as the Today panel
- `↑ today` button — promotes to today's list
- `✕` button — deletes

When the backlog reaches 10 or more items, a red warning appears: `Clean out your weeklies`.

**Input area** — bottom. Type a task and press Enter or click Add:

| Input | Result |
|---|---|
| `buy milk` | Added to today (default) |
| `buy milk, h` | Added to today, high priority |
| `w call doctor` | Added to weekly backlog |
| `w call doctor, l` | Added to weekly, low priority |

`t ` prefix also works to explicitly mark today; without any prefix it defaults to today. Priority defaults to medium if omitted.

Optionally assign a **category** before adding: click the `🏷 Category` button next to the input, pick an existing category or create a new one with a name and color. The chosen category attaches to the task as a colored pill. On existing tasks, click the pill (or the tag icon if uncategorised) to change it inline.

Categories are shared across all tasks and persist in the database. Two tasks can share the same category; category names are unique — creating one with the same name updates its color. Both color and name are editable right where you pick a category — in the `🏷 Category` picker on the add-task bar, and in the per-task category popover (click a task's category pill): click a category's color dot to open the native color picker (applies instantly, to every task using that category), or click the `✎` next to its name to rename it in place (Enter to save, Esc to cancel) — also applied everywhere that category is used.

Optionally mark the task **repeatable** before adding: click the `↻ Repeat` button next to the input (it highlights when active). See [Repeating Tasks](#repeating-tasks).

Optionally mark the task **hopscotch** before adding: click the `🦘 Hopscotch` button next to the input (it highlights when active). See [Hopscotch Tasks](#hopscotch-tasks).

**Text formatting** — task names support a small set of Obsidian-style inline markers, rendered wherever a task name is displayed (Today, Weekly Backlog, Week at a Glance, Evening Review, and the pinned task tracker):

| Syntax | Result |
|---|---|
| `**bold**` | **bold** |
| `*italic*` | *italic* |
| `~~strikethrough~~` | ~~strikethrough~~ |

**Footer** (shared across both tabs)
- Theme switcher: 6+ themes — shows your 5 most recently used as direct buttons plus a "More" submenu for the rest
- Settings: adjust morning/evening popup times (rewrites and reloads launchd plists — only this app's agents are affected)
- Pin button: opens the pinned task tracker
- `planner morning` / `planner evening` — terminal fallback command shown for reference, updates as you switch tabs

---

### Pinned Task Tracker (`planner pin`)

A small always-on-top window, separate from the main app, that stays visible throughout the day. Shows today's tasks with live checkboxes. Can be opened from the app's Pin button, or directly from the terminal. Refreshes automatically every 60 seconds.

Zoom the text/layout with `Cmd +` / `Cmd -` (`Cmd 0` resets to 100%). The window title briefly shows the current zoom level, then reverts. Zoom is a client-side setting for that window session only — it isn't saved, so a freshly opened pin window always starts at 100%.

---

### Evening tab

- Stats bar: tasks done / total for the day, progress bar, completion percentage
- Dopamine summary: a completion message scaled to how much you got done
- Today tasks: check off anything you finished late, double-click a name to edit it in place
- Weekly backlog: promote tasks to today if needed, double-click a name to edit it in place
- Week progress bar at the bottom — `done/total` across everything planned this week (based on the day each task was scheduled for, so tasks the 2AM agent moved to the backlog for being unfinished still count against the week, not just completed ones)
- Week completion chart: a bar per day, height = % of that day's tasks completed; today's bar is a distinct color; hover a bar for the exact done/total count, click a bar to see the actual tasks scheduled that day (including ones never completed)
- Chart range toggle: Week / 2 Weeks / Month, switching how far back the completion chart reaches

---

## Runs

A Run commits you to completing **x tasks a day for x days**, with **x allowances** that cover a missed day before the run breaks.

**Setup** — click `+ Run` in the runs bar: name it, set tasks/day, total days, and how many allowances to grant. Optionally restrict it to one category (`Only count`) — leave it on "Any completed task" and every completion anywhere counts toward the day's quota; pick a category and only completions tagged with it count.

**Evaluation** — happens nightly at 2 AM (same agent as the backlog rollover) for every day up through yesterday, and opportunistically whenever the app reads Runs (so it's never stale just because the Mac was asleep at 2 AM). For each unevaluated day:
- Enough qualifying completions that day → **hit**, no allowance spent.
- Not enough → if an allowance remains, it's spent silently and the run continues; if none remain, the run **breaks** on that day.
- Once total_days worth of days have been evaluated without breaking, the run is marked **completed**.

**Why allowances rather than an unlimited streak freeze or a "do 2x tomorrow" makeup**: an allowance absorbs a missed day with zero added pressure — nothing extra is demanded of you the next day, which matters most on exactly the days a miss is likely (illness, a bad mental-health day, life). An unlimited freeze removes all stakes and stops meaning anything; a 2x makeup demands more effort right when you're least likely to have it, which tends to compound one bad day into two. A small, finite number of allowances keeps the run meaningful while absorbing normal life — tune the count to how forgiving you want a given run to be.

**Runs bar** — each run shows as a chip: name, a dot per evaluated day (green = hit, amber = allowance used, red = miss/break), and `elapsed/total_days`. Click a chip for detail — target, category filter, allowances remaining, status — and to cancel or delete it.

---

## Repeating Tasks

Any task — today or weekly — can be marked **repeatable** via the `↻` button on the task row, or the `↻ Repeat` toggle in the Morning tab's input bar before adding a new one.

A repeatable task behaves normally while it's active: check it off like any other task. The difference is what happens overnight — see [2 AM Auto-Move](#2-am-auto-move) below. Non-repeatable tasks that get completed are simply done; repeatable ones come back.

The repeat state is visible everywhere a task appears (Today, Weekly Backlog, Pinned Tracker, Evening Review) as a small `↻` marker, and is toggleable from the Today/Weekly panels in the Morning tab.

---

## Hopscotch Tasks

Any task can be marked **hopscotch** via the `🦘` button on the task row, or the `🦘 Hopscotch` toggle in the Morning tab's input bar before adding a new one. A hopscotch task needs at least one full empty day between completions — mark it done today, and it stays locked until the day after tomorrow (a rest day in between, like the game: hop, skip, hop).

Trying to check off a locked hopscotch task shows an error with the exact date it unlocks, instead of completing it; the checkbox also greys out once it's known to be locked, so you usually see this before clicking. This pairs naturally with **repeatable** tasks (e.g. an every-other-day workout) — the lock state follows the task across the nightly respawn that repeatable tasks go through, so completing the respawned copy too soon is still blocked. It also works on a standalone task on its own, tracking just that one task's last completion.

🦘 was picked over a monochrome glyph (to match `↻`) because it reads instantly as "hop" without a label — the same reasoning the category tag already uses 🏷 rather than a plain dingbat.

---

## 2 AM Auto-Move

A silent launchd agent runs at 2:00 AM and does two things:

1. **Unfinished today-tasks** (not marked complete) move to the weekly backlog. `today_date` is preserved, so the task still shows as an uncompleted dot in the week grid — a record that it was planned, not that it vanished.
2. **Completed repeatable tasks** get a fresh, unchecked copy added to the weekly backlog, ready to be replanned. The original completed task is left alone as history — it keeps showing as a completed dot on the day you finished it.

No window, no interruption either way.

---

## Known Behavior: Autocomplete on Inline Task Editing

Double-click-to-edit fields (Today panel, Weekly Backlog panel — both tabs) can show a greyed-out inline suggestion completing what you're typing, accepted with `Tab`. This is WKWebView's native browser form-autocomplete, not a feature built into the planner.

The main "add task" input at the bottom of the Morning tab explicitly opts out of this (`autocomplete="off"`), but the inline rename field doesn't, so WebKit falls back to its default behavior there. It only ever suggests text previously typed into that field — nothing is sent anywhere, and no task data is involved beyond what you've already typed locally in that same input. Left as-is rather than suppressed.

---

## Terminal Commands

If launchd has a hiccup or you want to trigger manually:

```
planner morning    # open the app, defaulting to the Morning tab
planner evening    # open the app, defaulting to the Evening tab
planner pin        # open the pinned task tracker
planner status     # print today's tasks and backlog count (no window)
planner install    # rewrite launchd plists and reload them
planner cleanup    # run the 2AM auto-move + Runs evaluation manually
```

The `planner` alias is installed to `~/.zshrc` by `install.sh`.

---

## Running Tests

```
python3 -m unittest discover -s tests
```

Stdlib-only, runs against a throwaway temp SQLite DB — never touches the real install under `~/Library/Application Support/DailyPlanner/`.

---

## Renaming the Directory

The script finds its own location at runtime via `Path(__file__).resolve()` — renaming the directory won't break any data or configuration. After renaming, run `bash install.sh` from the new location to update the alias in `~/.zshrc` and regenerate the launchd plists with the new path.

---

## Directory Choices

macOS has designated locations for different kinds of app data. This tool follows those conventions so nothing ends up in unexpected places.

| Location | Why |
|---|---|
| `~/Library/Application Support/DailyPlanner/` | Persistent structured app data — database, config, Python venv. Hidden from Finder, backed up by Time Machine. |
| `~/Library/LaunchAgents/` | User-level background job definitions (plist files). Loaded by `launchd` at login, run as your user — not root. |
| `/etc/` | System-wide config, owned by root. Not used here — user tools belong in `~/Library`. |

---

## Installation

```bash
cd "path/to/daily-planner"
bash install.sh
```

The install script:
1. Creates `~/Library/Application Support/DailyPlanner/` and sets up the Python venv there
2. Installs `pywebview` into the venv
3. Writes and loads the three launchd plist files
4. Adds the `planner` alias to `~/.zshrc`

After installation, open a new terminal tab for the alias to take effect.

---

## Adjusting Scheduled Times

Either tab → Settings (bottom) → change the time fields → Apply. The app validates the times, rewrites its launchd plists, and reloads them. Only this app's three agents are touched.

---

## Self-Healing Against launchd Drops

launchd can silently drop a scheduled (non-`RunAtLoad`) agent from its job table — the plist on disk stays correct, but the agent just never fires again until something reloads it, with no error anywhere. To guard against this, opening any window, running `planner status`, or the 2 AM cleanup firing all first check that all three agents (`com.dailyplanner.morning/evening/cleanup`) are actually loaded in launchd, and silently reload any that are missing.

If a scheduled popup still isn't appearing despite this, run `planner install` to force a full plist rewrite + reload, or check manually with `launchctl list | grep dailyplanner`.

---

## Data & Privacy

- All data is local. No network calls are made from within the planner.
- `tasks.db` is a standard SQLite file — inspect or back up with any SQLite tool.
- `config.json` stores only scheduled times and theme — no task content.
- See [SECURITY.md](SECURITY.md) for the full security model.

---

## Future Features

### Always-On Background App *(partially built — focus/switch works, true residency doesn't)*

Current state: while the Morning/Evening window is open, a local Unix socket (`~/Library/Application Support/DailyPlanner/planner.sock`) lets a later `planner morning`/`planner evening` invocation — from `launchd` or the terminal — notify that running process instead of opening a second one. The running instance switches to the requested tab and brings itself to the front (via AppKit's `activateIgnoringOtherApps_`); the new invocation just sends the message and exits.

What's still missing: this only helps while a window is already open. There's no background process that stays resident once you close the window, so the header (clock, date, year countdown) and Runs state go idle when nothing's open, and a `launchd` fire with no window open still spawns a fresh one (correctly — there's nothing to notify). True permanence would need a background process — most naturally a macOS menu-bar icon via a library like `rumps`, or `pywebview` kept running in a hidden/background mode — that stays resident at all times. Logged here rather than built, since it pulls in stack beyond what the socket fix needed — build it if/when you want the app to never fully quit.

### Local Agent Coordination *(planned)*

The fundamental next step: local agents that connect this planner to your email, calendar, and other data sources to help keep your days coordinated automatically.

The idea: an agent reads your calendar, scans relevant emails, and surfaces suggested tasks into the morning window — which you accept, reject, or modify before they enter your list. The planner remains the single point of truth for what you've committed to. Agents are advisors, not writers.

Potential integrations:
- **Calendar** — surface meetings, deadlines, and blocked time as context when planning
- **Email** — flag action-item emails as candidate tasks
- **Cross-tool awareness** — agent knows what's already in your backlog so it doesn't suggest duplicates or overload a day

Local-first, no cloud dependency. Any external service access (Gmail, calendar APIs) will be gated behind explicit user authorization and shown transparently in the window.
