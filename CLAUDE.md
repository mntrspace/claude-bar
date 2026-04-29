# CLAUDE.md — codebase guide for AI agents (and humans)

This is an onboarding doc for someone (or something) picking up this codebase cold. Optimized for "what do I read first, what's load-bearing, what will silently break if I'm careless." Not a reference manual — read the code itself for that.

> **Fork lineage.** This is a fork of [BOUSHABAMohammed/claude-bar](https://github.com/BOUSHABAMohammed/claude-bar). Upstream's commit history is preserved in `git log`. The `upstream` git remote (if present) points at the original. Most of the architecture, dependency choices, and the scraping approach are upstream's — please read the [Credit](#credit) section and don't claim them as the fork's contribution.

## What this is

A macOS menu bar app that shows the user's [claude.ai](https://claude.ai) usage limits (5-hour session, 7-day weekly, optional credits). It works by reading the user's already-authenticated session cookie out of their browser's cookie store and calling claude.ai's private `/usage` endpoint — no Anthropic API key needed, no separate login.

The trust surface is real: the app has decryption-level access to the cookie store (via the macOS Keychain), and it spoofs Chrome's TLS fingerprint to talk to a private endpoint. Treat both as deliberate, load-bearing choices — see [Security notes](#security-notes).

## Where to start reading

Entry point: `claude_bar.py:415` (`main()`) → `ClaudeBar.__init__` at `claude_bar.py:166`. The constructor walks through the entire startup sequence in order:

1. `_build_menu_items()` — instantiate every `rumps.MenuItem` and assemble `self.menu`.
2. `_init_state()` — set up session/org-id placeholders, threading lock, UI state.
3. `_update_*_label()` calls — paint the toggle labels with their default values.
4. `set_menu_title(...)` for the three section headers — converts plain titles into styled `NSAttributedString` using `color_utils`.
5. `_set_credits_visible(False)` — credits section starts hidden until the API confirms it's enabled.
6. `_refresh(None)` — fire the first refresh synchronously so the menu has data on first open.
7. Two `rumps.Timer(...)` instances for deferred icon load (~0.5s) and update check (~5s). These run once and stop themselves.

After init, the app is event-driven — clicks on menu items invoke the `on_*` callbacks; the auto-poll timer (when enabled) invokes `_auto_refresh`.

## Architecture in one paragraph each

**Menu bar shell (`rumps` + AppKit).** `rumps.App` wraps `NSStatusItem`. We deliberately reach into `_menuitem` (rumps' private attribute) to call AppKit methods directly — `setAttributedTitle_` for colored rows, `setHidden_` for show/hide. This is why `rumps==0.4.0` is pinned in `requirements.txt`: future rumps versions could rename or remove that attribute. If you upgrade rumps, smoke-test colored rendering and the hide/show on Extra Credits.

**Cookie reading (`rookiepy==0.5.6`).** `rookiepy` decrypts the browser's encrypted cookie store. On macOS this triggers a Keychain access prompt the first time. We try a fixed list of browsers in order (`BROWSERS` constant in `claude_bar.py`) and use the first one that has a `claude.ai` session cookie under one of the names in `COOKIE_NAMES`. The cookie is held in memory in the `requests.Session` and never persisted to disk by this app.

For Chromium-based browsers that `rookiepy` doesn't natively support (currently Dia from The Browser Company), there's a small in-tree fallback in `claude_bar.py`: `_load_chromium_like_cookies` reads the SQLite cookie DB directly, fetches the `Safe Storage` key from macOS Keychain via the `security` CLI, derives the AES-128 key (PBKDF2-HMAC-SHA1, salt `saltysalt`, 1003 iterations), and decrypts each row via `openssl enc -aes-128-cbc`. Stdlib + `openssl` only — no extra Python deps. The browser → (db_path, keychain_service, keychain_account) mapping lives in `CHROMIUM_LIKE_BROWSERS`. To add another such browser, append a new entry there. Chrome 130+ binds cookies to host+path with a 32-byte SHA-256 prefix inside the encrypted plaintext; `_decrypt_chromium_cookie` strips it heuristically.

**HTTP (`curl-cffi==0.14.0` with `impersonate="chrome120"`).** `curl_cffi` wraps libcurl and can match the TLS fingerprint of real browsers. We do this because Anthropic's bot filter on `/api/organizations/{id}/usage` rejects requests that look like Python clients. This is intentional and load-bearing — removing the impersonation breaks the app. It's also a TOS gray area; see [Security notes](#security-notes).

**Styled rendering (`color_utils.py` + PyObjC AppKit).** Every progress row is an `NSMutableAttributedString` with a fixed palette (`_HEX` dict in `color_utils.py:29`) and three font slots (regular, bold, mono). The progress bar itself is unicode `▓` and `░` rendered in the mono font. Colors come from a small enum (`ColorKey`) so calls read like `make_plain(text, ColorKey.ERROR)`.

## Threading model

- **Main thread** runs the AppKit run loop. All menu mutation must happen here.
- **Background daemon threads** are spawned for two things only: refresh (`_refresh_bg`) and update check (`_check_update_bg`). They do network I/O and never touch the menu directly.
- **Marshalling back to main**: `PyObjCTools.AppHelper.callAfter(fn, *args)` schedules `fn(*args)` on the main thread. Use it for every menu update from a background thread. Look at `_refresh_bg` and `_update_menu` — that's the canonical pattern.
- **Refresh lock**: `self._refresh_lock` + `self._refreshing` flag prevent two refreshes from running at once. If a refresh is in flight when the user clicks Refresh Now (or the auto-poll timer fires), the click is silently dropped. Don't try to "fix" this without thinking through what the user expects.
- **Auto-poll timer**: a manually-managed `rumps.Timer` (not the `@rumps.timer` decorator). It's stored on `self._poll_timer` and **recreated**, not reused, on interval changes. This is deliberate — see [Key invariants](#key-invariants).

## Key invariants

These are subtle and will silently break things if forgotten.

- **`set_menu_title(item, attr_str)` is one-way.** Once a menu item has been painted via `setAttributedTitle_`, assigning to its `.title` attribute will not undo the styled rendering on macOS. If you need a row to switch between styled and plain, always go through `set_menu_title` with a fresh attributed string. The `_apply_title` method on `ClaudeBar` is a special case — it sets `self.title` (the menu bar title), which is plain text by design.
- **Auto-poll defaults to OFF on every launch.** No persistence. Default interval is 10 minutes. The user explicitly chose this UX in v1.1.0 (see [Fork-specific design choices](#fork-specific-design-choices)). Don't add persistence without explicit ask.
- **Initial fetch on launch is intentional** (`_refresh(None)` in `__init__`). The user gets fresh numbers without needing to opt into auto-poll. Don't gate this fetch behind the toggle.
- **`rumps.Timer` is recreated, not reused, on interval changes.** `_set_interval` calls `_stop_timer()` then `_start_timer()`. We don't trust `rumps.Timer.interval` to be live-mutable across a stop/restart cycle. Creating a fresh `NSTimer` is cheap.
- **Argv form for `subprocess.Popen` only.** We launch URLs via `["open", url]` (currently in `on_open_update`). Never `shell=True`, ever. There's a known issue here — see [Security notes](#security-notes).
- **The cookie value is never logged.** `get_session_cookie` logs only the cookie *name* (`sessionKey` etc.) and the browser. If you add logging in this area, preserve that — the log file may be world-readable.

## API contract

The app makes exactly three external calls.

**`GET https://claude.ai/api/organizations`**
Returns a list of organizations the user belongs to. `get_org_id` picks one — by default the org with the highest "priority" (paid `billing_type` like `stripe_subscription` first, then more capabilities); a `--org NAME` CLI flag overrides this with an exact-match-then-startswith-then-substring search. Response shape (the app cares about):
```json
[{"uuid": "...", "id": "...", "name": "...", "billing_type": "...", "capabilities": [...]}, ...]
```
The selected org's name is printed at startup whenever there are multiple orgs, so you know which account is being polled.

**`GET https://claude.ai/api/organizations/{org_id}/usage`**
The interesting one. Returns the usage dict that the menu renders. The exact shape is **captured to disk on every successful refresh** at `~/Library/Caches/claude-bar/last-response.json` (mode 0600). Inspect that file for ground truth before assuming anything about the schema.

As of v1.1.0 the renderer in `_update_menu` only knows about three top-level keys:
- `five_hour` → `{"utilization": float, "resets_at": str ISO-8601}`
- `seven_day` → `{"utilization": float, "resets_at": str ISO-8601}`
- `extra_usage` → `{"is_enabled": bool, "used_credits": int (cents), "monthly_limit": int (cents), "utilization": float}`

Anthropic's claude.ai settings page exposes additional buckets (per-model weekly limits like "Sonnet only" / "Claude Design", count-based ones like "Daily included routine runs"). These are **not yet rendered** — making the renderer schema-driven is the v1.2 work. If you're being asked to add a new bucket, first inspect the dump file and then refactor `_update_menu` to walk the response generically rather than hardcoding more keys.

**`GET https://api.github.com/repos/{GITHUB_REPO}/releases/latest`**
Once at startup, no credentials. Used for the "Update available" notification. The response's `html_url` is currently passed straight to `subprocess.Popen(["open", ...])` — see [Security notes](#security-notes).

## State and storage

What the app reads, writes, or holds in memory:

| Path | Purpose | Lifetime | Mode |
|---|---|---|---|
| Browser cookie store | Read on each refresh attempt to get the session cookie | Read-only | n/a |
| `~/Library/Caches/claude-bar/last-response.json` | Debug dump of the most recent `/usage` response | Overwritten on every refresh | 0600 |
| `~/.local/share/claude-bar/claude-bar.log` | LaunchAgent stdout/stderr (when installed via `install.sh`) | Append, no rotation | launchd default (typically 0644) |
| `~/.local/share/claude-bar/run.sh` | Wrapper that activates the venv and runs the app | Written by installer | 0755 |
| `~/Library/LaunchAgents/com.user.claude-bar.plist` | LaunchAgent definition (only if user opted in at install) | Written by installer | 0644 |

In-memory only:
- `self._session` — `curl_cffi.Session` with the claude.ai cookie attached. Cleared on HTTP 401.
- `self._org_id` — claude.ai organization UUID. Cleared on HTTP 401.
- `self._auto_poll_enabled`, `self._poll_interval_secs`, `self._poll_timer` — auto-poll state. Always reset to defaults on launch.
- `self._show_summary` — whether to render the percentage in the menu bar title. Always defaults to `SHOW_TITLE_SUMMARY` on launch.

## Security notes

These are real, not theoretical. Read them before changing anything in the auth/network/launch path.

- **Session cookie is the highest-value secret in this app.** It's read fresh from the browser per launch, held in memory in `_session.cookies`, and never written to disk by this code. Don't add `print(session)` or similar — it would dump cookies to the log file. The current logging convention is to print cookie *names* only.
- **Log file may be world-readable.** Under launchd, `~/.local/share/claude-bar/claude-bar.log` defaults to mode 0644 — readable by every local user. Currently no secrets land there. A defense-in-depth fix is to set `umask 077` in `run.sh`. Tracked for a separate security pass.
- **TLS impersonation (`impersonate="chrome120"`) is intentional.** It's how the app gets past Anthropic's bot filter on the private `/usage` endpoint. This is a TOS gray area; users should opt in with their eyes open. Don't remove it (the app stops working) and don't try to disguise it further (escalating cat-and-mouse).
- **GitHub `html_url` is not validated before `open`.** `on_open_update` passes the URL straight to `subprocess.Popen(["open", url])`. macOS `open` dispatches by URL scheme, including custom app schemes. If GitHub ever returns a non-`https://github.com/...` URL (e.g. via a maintainer-account compromise), one click could trigger an arbitrary URL handler. Argv form prevents shell injection but not scheme abuse. **The fix is: assert `self._update_url.startswith(f"https://github.com/{GITHUB_REPO}/")` before launching.** Tracked for a separate security pass.
- **Debug dump is mode 0600.** `_dump_response` explicitly chmods the file. Preserve this if you change the dump path.
- **Argv form everywhere for subprocess.** Never `shell=True`. Never `os.system(...)`.
- **Update check is unauthenticated.** No token sent to GitHub. Rate-limited to 60/hr per IP, but it only fires once per launch.

## Fork-specific design choices

These are decisions made in this fork (mntrspace/claude-bar v1.1.0) that diverge from upstream. If you ever PR back to upstream, these are the conversation points.

- **Auto-poll is opt-in.** Upstream auto-polled every 5 minutes by default. This fork ships with auto-poll OFF and an explicit toggle. Reason: every poll hits a private endpoint with a TLS-spoofed client, and 288 calls/day per user is a clear bot pattern. Opt-in significantly reduces detection surface for users who only look occasionally.
- **Interval picker.** Submenu with 5 / 10 / 30 / 60 minute presets. Default 10 min. Selected via `rumps.MenuItem.add` to nest a submenu.
- **Debug response dump.** Always-on, writes to `~/Library/Caches/claude-bar/last-response.json` on every refresh. Unblocks the upcoming schema-driven renderer (v1.2).
- **No persistence in v1.x.** Auto-poll state, interval, and "show %" toggle all reset on every launch. If this becomes annoying, persist via a small JSON in `~/Library/Application Support/claude-bar/`.

## How to extend it

**Adding a new bucket type to the menu (current renderer).** Until the schema-driven refactor lands:
1. Inspect `~/Library/Caches/claude-bar/last-response.json` to find the field.
2. Build new menu items in `_build_menu_items` — header + value row, mirroring the existing `five_h_hdr` / `five_h_row` pattern.
3. Read the field in `_update_menu` and call `set_menu_title(row, make_progress_row(pct, suffix))` (utilization) or build a `make_plain(...)` row (counts).
4. Insert the new items into `self.menu` in the order you want them rendered.

**Adding a new menu toggle.** Mirror `summary_toggle` (`claude_bar.py:195-197`): a `MenuItem` with a checkmark prefix in its title; an `_update_*_label` helper to repaint; an `on_toggle_*` callback that flips state and calls the helper.

**Changing the polling cadence options.** Edit `INTERVAL_OPTIONS` (currently `(300, 600, 1800, 3600)`) and `DEFAULT_INTERVAL`. The submenu renders from these constants — no other change needed.

**Adding persistence.** v1.1 deliberately doesn't persist. If you add it: write a tiny helper that loads/saves a JSON dict at `~/Library/Application Support/claude-bar/state.json` with mode 0600. Load in `_init_state`. Save in the toggle/interval callbacks. Don't put it in `~/.config` — that's not the macOS convention.

## Build, run, install

Detailed instructions live in the README. The short form:

```bash
git clone https://github.com/mntrspace/claude-bar.git
cd claude-bar
uv sync --frozen --no-dev
.venv/bin/python claude_bar.py
```

The first run will trigger a Keychain prompt from your browser (because `rookiepy` is decrypting the browser's cookie store). Click *Always Allow*.

For autostart: re-run `install.sh` and choose "yes" when it asks about the LaunchAgent. The installer copies the source into `~/.local/share/claude-bar/` and writes a plist at `~/Library/LaunchAgents/com.user.claude-bar.plist`.

## Files at a glance

| File | What's in it |
|---|---|
| `claude_bar.py` | The whole app: argparse entry, `ClaudeBar` rumps subclass, refresh logic, update check, debug dump, menu construction. |
| `color_utils.py` | NSAttributedString helpers — palette, fonts, progress-bar generation. |
| `install.sh` | macOS-only installer. Downloads the latest release tarball, runs `uv sync`, optionally installs a LaunchAgent. |
| `pyproject.toml` / `uv.lock` / `requirements.txt` | Dependency pinning. `uv.lock` has SHA256 hashes for every package. |
| `icons8-claude-ai-96.png` | Menu bar icon. Template image (macOS handles dark/light). |
| `README.md` | User-facing install + usage docs. |
| `CLAUDE.md` | This file. |

Notable absences: no test suite, no CI, no separate "config" or "settings" file.

## Credit

The original concept, all of the upstream architecture, and the scraping approach are by [BOUSHABAMohammed](https://github.com/BOUSHABAMohammed). The fork at `mntrspace/claude-bar` adds:
- Opt-in auto-poll with an interval picker (5 / 10 / 30 / 60 min)
- Debug response dump for schema-driven renderer work (v1.2 scope)
- Documentation and security notes

If you're modifying anything in the cookie/auth/HTTP path, check the upstream repo first — the maintainer may have already addressed it. The `upstream` remote (if you set it) tracks `BOUSHABAMohammed/claude-bar`.
