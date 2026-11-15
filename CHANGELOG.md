# Changelog

All notable changes to Portfolio Manager are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Data safety

- Upgrades are now atomic: each schema migration commits together with its version
  record or not at all, so an interrupted or failed upgrade leaves your data at its
  previous version and the next launch retries. Databases left half-upgraded by older
  builds repair themselves.
- Before an upgrade, a consistent, verified snapshot is written to
  `portfolio.<from>-to-<to>.<time>.bak` (the newest 5 are kept, none is overwritten).
  This replaces the single `portfolio.db.bak`, which could be empty or stale. If the
  snapshot cannot be written, the upgrade does not run.
- Added automatic daily backups (newest 7 kept) in `~/.portfolio_manager/backups`, and
  a **Back up now** button with a list of backups in Settings. You can restore a backup
  from the list (applied safely at the next launch, with the replaced data kept aside),
  export a verified copy to a folder you choose, and, if the database is too damaged to
  open, restore the newest backup from the error banner.
- The app checks the database for damage at launch and reports it plainly without
  changing the file.
- An older app now refuses a database created by a newer version, and leaves it
  untouched.
- A damaged or unreadable `config.toml` is set aside as `config.toml.corrupt-<time>`
  and defaults are used, instead of the app failing to start. Settings are written
  atomically and escaped correctly.
- A full disk, a read-only or damaged database, and disk I/O errors now show a specific
  message ("Your disk is full, so this change was not saved") instead of "An unexpected
  error occurred". A full disk at launch shows its own message.
- SQLite `fullfsync` is enabled so committed data survives a power cut on macOS.
- The CI workflow now also tests the backend, frontend and Tauri shell from the
  hash-pinned lockfiles (it previously covered only the legacy Tkinter code).
- Only one copy of the app runs at a time; opening it again brings the first window
  forward.
- The backend now exits when the app is killed or crashes, and is asked to stop with
  `SIGTERM` (then killed after 2 seconds) so the database is closed cleanly.
- The "changes are disabled" banner now really disables the controls, and a failed
  dashboard load shows a readable message instead of "[object Object]".

### Added

- Added the Tauri/React desktop application as the current app target, replacing
  the legacy Tkinter UI path for daily use.
- Added a modern React interface with dashboard, sessions, projects,
  milestones, weekly review, settings, tab navigation, and week navigation.
- Added a README dashboard screenshot using `_design/Portfolio-Manager-Dashboard.png`.
- Added README badges for the current pre-release status and app stack: macOS,
  local-first data, time-boxed workflow, Tauri, React, FastAPI, SQLite, Rust,
  Python, CI, and license.
- Added a concise explanation of the core loop: choose active projects, set a
  weekly time budget, define milestones, plan sessions, execute, review, and
  adjust the portfolio.

### Changed

- Refactored the app from the original Python/Tkinter desktop architecture into
  a Tauri shell with a React frontend and bundled FastAPI/Python sidecar.
- Updated the product design from the legacy Tkinter feel to a cleaner,
  denser, modern desktop UI suited to technical solo-project management.
- Reframed the README around the GitHub pitch: a single-user desktop app for
  managing, scheduling, and executing creative and technical work through
  time-boxed sessions.
- Clarified that Portfolio Manager is pre-release software and does not yet
  have a public packaged download.
- Repositioned the project for systems thinkers, programmers, math-minded
  planners, and solo builders rather than broad productivity-tool adoption.
- Documented the repository as both a usable personal app and a macOS
  release-stack rehearsal for Tauri, React, FastAPI, Python, Rust, SQLite,
  GitHub Actions, and GitHub Releases.

### Removed

- Removed release/download badge emphasis from the README until a public GitHub
  release exists.

---

## [2.1.0] — 2026-04-09

### Added

- **Weekly budget bar** — Sessions tab now shows a summary bar with planned minutes, done minutes, budget (from settings), and remaining time for the displayed week
- **Dashboard "This Week" section** — new focus panel below Portfolio Summary shows this week's session totals and the next upcoming (non-done) milestone per active project, sorted by target date
- **Past reviews list** — Weekly Review tab now shows a left-panel listbox of all saved reviews; click any entry to load it directly, removing the need to type week keys manually
- **"Today" button** on all date entry fields — session date, milestone target date, and project start/end dates now have a one-click "Today" shortcut next to the entry

### Changed

- **Consistent data-view pattern** — Sessions, Projects, and Milestones tabs now all use a full-width table + toolbar + popup dialog layout with double-click to edit
- **Session popup** — fields: Project, Milestone, Session name, Date, Status, Min, Description (notes); notes are now correctly persisted on create (previously silently dropped)
- **Project popup** — non-modal 900×680 window with metadata (name, description, status, priority, start, end) and embedded Plan document editor with preview-first toggle
- **Milestone popup** — fields: Milestone name, Target date (with live Week derivation), Status, Total Min (read-only), Description (notes)
- **Week navigator** — selecting a week in the left panel now syncs Sessions and Review silently without forcing a tab switch
- **Project priority** — dropdown now offers 1–5 (matching the data model and SRS), not the previous 1–3
- **Milestone status** — replaced binary `is_complete` toggle with a full five-state status (backlog / planned / doing / done / cancelled)
- **Session notes** — `create_session()` now accepts and persists a `notes` parameter through the full service/controller/view stack

### Removed

- `score_bar.py` widget — was never imported or used anywhere; dead code removed
- **Dashboard Status Notes panel** — removed because `status_note` was always an empty string (no entry point existed); replaced by the new "This Week" focus section
- **Left panel project list** — replaced by a week navigator (12 past + current + 4 future weeks, current week highlighted)

### Database

- Migration v4: `ALTER TABLE milestone ADD COLUMN notes TEXT NOT NULL DEFAULT ''`

---

## [2.0.0] — 2026-04-08

### Added

- Full MVC architecture with layered Service and Repository pattern
- SQLite database with versioned migration system (`db/migrations.py`)
- Five domain models: `Project`, `Session`, `Milestone`, `ProjectScore`, `WeeklyReview`
- Five repositories with full CRUD and typed domain-object mapping
- `ProjectService` — project lifecycle (create, update, archive, delete)
- `SessionService` — session lifecycle (create, complete, reschedule, cancel, reopen)
- `ScoringService` — pluggable Strategy pattern; default algorithm: 60% sessions + 40% milestones
- `PlanService` — Markdown → HTML rendering with Mermaid.js diagram support
- `WeekService` — ISO 8601 week-key computation and date-range helpers
- `EventBus` — in-process Observer pattern; 11 named domain events
- Six Tkinter views: Dashboard, Sessions, Projects, Milestones, Weekly Review, Settings
- Two custom widgets: `StatusBadge` (traffic-light + text for accessibility), `PlanEditor` (split-pane Markdown editor with debounced live Mermaid preview)
- Settings panel persisting to `~/.portfolio_manager/config.toml`
- `launch.sh` — Tk-capable Python detection and venv auto-creation
- `create_shortcut.sh` — macOS `.app` bundle for Dock placement
- MkDocs/Material documentation site (`site/` source → `docs/` HTML for GitHub Pages)
- 140 unit and integration tests at 94.8% coverage

### Architecture

- MVC separation: views contain no business logic
- Repository layer: no SQL outside `repositories/`
- Strategy pattern: scoring algorithm injectable and replaceable
- Observer pattern: `EventBus` decouples services from views
- Singleton: `DatabaseConnection` shared across all repositories
- Transaction context manager: `with db.transaction():` on all multi-step writes

---

## [1.0.0] — (V1 — not implemented in this repository)

V1 SRS defined behavioural requirements only. V2 supersedes it with a full implementation specification.
