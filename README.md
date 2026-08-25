# Daily Planner

A local, windowed daily planning tool for macOS. Two scheduled popup windows — morning for planning, evening for review — backed by a SQLite task database and driven by `launchd` (macOS's native scheduler).

No cloud, no subscriptions, no telemetry. Everything runs locally.

---

## How It Works

Two `launchd` agents fire at times you set. Each launches a native-looking window via `pywebview`, which embeds macOS's WKWebView (the same rendering engine as Safari, without any browser chrome). If your Mac was asleep at the scheduled time, the window appears when it wakes up. A third silent agent runs at 2:00 AM to move uncompleted today-tasks into the weekly backlog.

Your data lives in `~/Library/Application Support/DailyPlanner/`:
- `tasks.db` — SQLite database (WAL mode, safe for concurrent access)
- `config.json` — scheduled times and theme preference
- `venv/` — isolated Python environment with pywebview

LaunchAgent plists live in `~/Library/LaunchAgents/`.

---

## The Three Windows

### 1. Morning Window (`planner morning`)

**Week at a Glance** — top section, 7-column grid showing the current Mon–Sun week. Each column shows tasks that were in "today" scope on that day, as dots:
- Colored dot = planned, not completed (category color if assigned, otherwise priority color)
- Grey dot = completed

Each day column has a fixed height, capped at 4 visible rows. A day with more than 4 tasks shows its first 3 dots plus a `+N more` line instead of stretching the grid.

Tasks moved to weekly by the 2AM agent (planned but not done) stay visible as uncompleted dots — they're part of the historical record. Tasks demoted back to weekly by you (a mistake, not a failure) disappear from the grid.

**Today panel** — left side. Each task has:
- A checkbox to mark done/undone
- Priority badge (H / M / L)
- Double-click the task name to edit it in place — Enter or clicking away saves, Esc cancels
- `→w` button — moves the task back to weekly and removes it from the week grid (treats it as a planning mistake, not a failure)
- `✕` button — deletes the task entirely

**Weekly Backlog panel** — right side. Tasks accumulate here. Each has:
- Double-click the task name to edit it in place, same as the Today panel
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

Categories are shared across all tasks and persist in the database. Two tasks can share the same category; category names are unique — creating one with the same name updates its color. To recolor an existing category, click its color dot inside the category picker — this opens the native color picker and applies the new color to every task using that category, immediately.

**Text formatting** — task names support a small set of Obsidian-style inline markers, rendered wherever a task name is displayed (Today, Weekly Backlog, Week at a Glance, Evening Review, and the pinned task tracker):

| Syntax | Result |
|---|---|
| `**bold**` | **bold** |
| `*italic*` | *italic* |
| `~~strikethrough~~` | ~~strikethrough~~ |

**Footer**
- Theme switcher: Dark / Warm / Red
- Settings: adjust morning/evening popup times (rewrites and reloads launchd plists — only this app's agents are affected)
- Pin button: opens the pinned task tracker
- `planner morning` — terminal fallback command shown for reference

---

### 2. Pinned Task Tracker (`planner pin`)

A small always-on-top window that stays visible throughout the day. Shows today's tasks with live checkboxes. Can be opened from the morning window's Pin button or directly from the terminal. Refreshes automatically every 60 seconds.

---

### 3. Evening Window (`planner evening`)

- Stats bar: tasks done / total for the day, progress bar, completion percentage
- Dopamine summary: a completion message scaled to how much you got done
- Today tasks: check off anything you finished late, double-click a name to edit it in place
- Weekly backlog: promote tasks to today if needed, double-click a name to edit it in place
- Week progress bar at the bottom

---

## 2 AM Auto-Move

A silent launchd agent runs at 2:00 AM. Any task still in today's scope and not marked complete moves to the weekly backlog. Its `today_date` is preserved — it stays visible as an uncompleted dot in the week grid, showing you it was planned that day. No window, no interruption.

---

## Terminal Commands

If launchd has a hiccup or you want to trigger manually:

```
planner morning    # open the morning planning window
planner evening    # open the evening review window
planner pin        # open the pinned task tracker
planner status     # print today's tasks and backlog count (no window)
planner install    # rewrite launchd plists and reload them
planner cleanup    # run the 2AM auto-move manually
```

The `planner` alias is installed to `~/.zshrc` by `install.sh`.

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

Morning window → Settings (bottom) → change the time fields → Apply. The app validates the times, rewrites its launchd plists, and reloads them. Only this app's three agents are touched.

---

## Data & Privacy

- All data is local. No network calls are made from within the planner.
- `tasks.db` is a standard SQLite file — inspect or back up with any SQLite tool.
- `config.json` stores only scheduled times and theme — no task content.
- See [SECURITY.md](SECURITY.md) for the full security model.

---

## Future Features

### Local Agent Coordination *(planned)*

The fundamental next step: local agents that connect this planner to your email, calendar, and other data sources to help keep your days coordinated automatically.

The idea: an agent reads your calendar, scans relevant emails, and surfaces suggested tasks into the morning window — which you accept, reject, or modify before they enter your list. The planner remains the single point of truth for what you've committed to. Agents are advisors, not writers.

Potential integrations:
- **Calendar** — surface meetings, deadlines, and blocked time as context when planning
- **Email** — flag action-item emails as candidate tasks
- **Cross-tool awareness** — agent knows what's already in your backlog so it doesn't suggest duplicates or overload a day

Local-first, no cloud dependency. Any external service access (Gmail, calendar APIs) will be gated behind explicit user authorization and shown transparently in the window.
