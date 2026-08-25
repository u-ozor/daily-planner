# Security Considerations

This document tracks the security model for Daily Planner. Update it when the attack surface changes — new inputs, new file paths, new integrations, new dependencies.

---

## Threat Model

Daily Planner is a local-only tool. It has no server and makes no outbound network connections. Primary risks:

1. Malicious task input crafted to escape the DB layer or execute code
2. JS→Python bridge abuse if the webview can call arbitrary Python
3. Unsafe file paths if user-controlled strings reach `open()` or `subprocess`
4. launchd privilege escalation if plists are written insecurely
5. Future agent integrations introducing external data into the app

---

## Input Handling

All task text is treated as untrusted user input regardless of source.

- Task names are plain strings — stored and retrieved, never evaluated or interpolated into shell commands.
- Priority values are validated against an explicit allowlist (`1`, `2`, `3`) before use.
- Task name length is capped at 280 characters.
- Control characters (0x00–0x1F, 0x7F) are stripped before storage.
- Scope values are validated against `('today', 'weekly')` — anything else is rejected.
- Time values (morning/evening schedule) are validated against `HH:MM` format and numeric range before any plist write.

---

## Task Name Rendering (Inline Formatting)

Task names support a small set of Obsidian-style inline markers (`**bold**`, `*italic*`, `~~strikethrough~~`), rendered as HTML in every window that displays task names.

Rendering order is fixed and matters for safety: the raw task name is passed through the existing HTML-escaping function first (`&`, `<`, `>`, `"` become entities), and only then are the markdown regexes applied on top of the already-escaped string. A task name cannot inject real HTML tags — any `<`, `>`, or `"` a user types is neutralized before the formatting regexes ever run, so the regexes only ever wrap escaped, inert text in `<strong>`, `<em>`, or `<del>`.

---

## Database

- All queries use parameterized statements (SQLite `?` placeholders). No string-formatted SQL anywhere.
- `sqlite3.connect()` receives a path derived from `pathlib.Path` — never a user-supplied string.
- `check_same_thread=False` is set on the connection. pywebview 6 calls JS API methods from a GUI thread separate from the main thread; this setting allows that safely. WAL journal mode (`PRAGMA journal_mode = WAL`) handles concurrent read/write access correctly.
- The DB file lives in `~/Library/Application Support/DailyPlanner/tasks.db`. No network socket, no remote connection.

---

## Content Security Policy

The HTML windows do not use a Content-Security-Policy meta tag. pywebview 6.x uses a custom URL scheme for its JS→Python RPC channel; `default-src 'self'` blocks that channel when the page loads via the `html=` string parameter (null origin). Since this is a local-only tool with no external assets, the CSP adds no practical protection — the real enforcement is on the Python side. This is a known pywebview 6 constraint and is documented here as a deliberate trade-off, not an oversight.

---

## pywebview JS→Python Bridge

JS running in the webview can only call Python functions explicitly exposed on the `PlannerAPI` class. pywebview enforces this — the webview has no access to any other Python object.

**Exposed methods:**
- `get_today_tasks` — read today's tasks
- `get_weekly_backlog` — read weekly backlog
- `get_week_overview` — read week grid data
- `add_task(name, priority, scope)` — validated before DB write
- `update_task_name(task_id, name)` — task_id validated as integer; name re-run through the same sanitization as `add_task` (control chars stripped, capped at 280 chars)
- `mark_done(task_id)` — integer ID only
- `unmark_done(task_id)` — integer ID only
- `promote_task(task_id)` — weekly → today
- `demote_task(task_id)` — today → weekly (clears today_date; treats as planning mistake)
- `delete_task(task_id)` — integer ID only
- `get_stats` — aggregate counts only
- `get_categories` — read all categories (id, name, color)
- `add_category(name, color)` — name sanitized (max 50 chars, control chars stripped); color validated as `#RRGGBB` hex, falls back to default if invalid; names are unique case-insensitively
- `update_category_color(category_id, color)` — category_id validated as integer; color validated as `#RRGGBB` hex, falls back to default if invalid
- `set_task_category(task_id, category_id)` — both validated as integers; category_id may be null to clear
- `get_config` — scheduled times and theme
- `save_config(morning_time, evening_time)` — HH:MM validated before plist write
- `save_theme(theme)` — validated against `('dark', 'warm', 'red')` allowlist
- `open_pin_window` — spawns a subprocess with fixed arguments (no user input in the command)

**Not exposed:** `os`, `subprocess` (except via `open_pin_window`'s fixed call), `eval`, `exec`, arbitrary file I/O, any Python import, shell passthrough.

---

## File Paths

- No hardcoded absolute paths in source. All paths are derived at runtime from `pathlib.Path.home()` or `Path(__file__).resolve()`.
- `PYTHON_PATH` is set as `Path(sys.executable)` without calling `.resolve()`. The venv Python binary is a symlink to the base Homebrew Python; resolving it would produce the base Python path (which doesn't have pywebview installed). Keeping the symlink path is correct and intentional.
- All paths the app writes to:
  - `~/Library/Application Support/DailyPlanner/` — data, config, logs, venv
  - `~/Library/LaunchAgents/com.dailyplanner.*.plist` — scheduling
- The app never writes outside these two directories.

---

## launchd Reload Safety

When scheduled times are changed from the settings panel:

1. Times are validated (HH:MM format, valid hour/minute ranges).
2. Config is written to `config.json`.
3. Only the three planner plist files are regenerated using `plistlib` (Python stdlib — no shell templates).
4. `launchctl unload <plist>` then `launchctl load <plist>` is called on each of the three files.

`launchctl unload/load` on a specific plist only stops and restarts that one job. It does not restart `launchd` itself, does not affect any other LaunchAgent, and does not interrupt any other running process.

Plist files are written with `ProgramArguments` containing the Python path and script path — both resolved at install time from trusted sources. No user input reaches the plist content.

---

## Pin Window Subprocess

`open_pin_window` spawns `planner pin` as a detached subprocess using `subprocess.Popen` with a fixed argument list. The arguments are `[PYTHON_PATH, SCRIPT_PATH, "pin"]` — no user-supplied strings are passed. Stderr is captured to `~/Library/Application Support/DailyPlanner/pin.err` for diagnostics.

---

## Config File

- `config.json` is read with `json.loads()` and validated against an expected schema before any value is used.
- Time values are re-validated as `HH:MM` strings on every read — config file tampering produces rejected values, not arbitrary input to the scheduler.
- The config is never evaluated.

---

## Future Agent Integrations

When external data sources (email, calendar) are added:

- External data is advisory only — it surfaces suggested tasks but never writes to the DB without user confirmation.
- All external content is treated as untrusted. Task names suggested by an agent pass through the same input validation as manually entered tasks.
- API credentials (OAuth tokens) are stored in macOS Keychain, never in `config.json` or `tasks.db`.
- Network calls are made by the agent process, not by the planner window. The window only receives already-sanitized task suggestions.
- This section must be updated with specifics when the first agent integration is designed.

---

## What This Tool Does Not Do

- No network requests from within the planner window
- No subprocess calls with user-supplied strings
- No `eval()` or `exec()` anywhere in the codebase
- No writing outside `~/Library/Application Support/DailyPlanner/` and `~/Library/LaunchAgents/`
- No reading of sensitive paths (SSH keys, shell history, credentials, `.env` files)
- No logging of task content anywhere outside `tasks.db`

---

## Tracking Changes to This File

Update when:
- A new input vector is added (new field, new command)
- A new file path is written to
- A new subprocess call is introduced
- An agent integration is designed
- A dependency is added or a major version is bumped (`pywebview` version changes count)
