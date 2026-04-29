# claude-bar

A macOS menu bar app that shows your real-time [Claude.ai](https://claude.ai) usage — no login required beyond your existing browser session.

> **Fork notice.** This repository is a fork of [BOUSHABAMohammed/claude-bar](https://github.com/BOUSHABAMohammed/claude-bar). The original concept, scraping approach, and the bulk of the code are his work — please ⭐ the upstream repo if you find this useful. Upstream commit history is preserved in `git log`. See the [Credits](#credits) section for what this fork adds.

<table><tr>
  <td><img src="menubar.png" alt="menu bar icon showing 3% · 19% usage"></td>
  <td><img src="menubar_expanded.png" alt="expanded menu" width="320"></td>
</tr></table>

## Table of Contents

- [What it shows](#what-it-shows)
- [Why this exists](#why-this-exists)
- [Privacy and Security](#privacy-and-security)
- [Requirements](#requirements)
- [Install](#install)
- [Manual installation](#manual-installation)
- [Starting and stopping](#starting-and-stopping)
- [Refresh behavior](#refresh-behavior)
- [Updating](#updating)
- [Uninstall](#uninstall)
- [Troubleshooting](#troubleshooting)
- [Advanced](#advanced)
- [How it works](#how-it-works)
- [Credits](#credits)
- [License](#license)

---

## What it shows

- **5-hour window** — your current utilization with a progress bar and time until reset
- **7-day window** — your rolling weekly utilization and next reset date
- **Extra credits** — dollar amount used and remaining (shown only if you have a credit balance)
- **Percentage summary** in the menu bar title (optional, togglable from the menu)
- **Manual refresh** on demand via the menu
- **Optional auto-refresh** — opt-in via the menu, with intervals of 5 / 10 / 30 / 60 minutes

By default, this fork does **not** auto-poll. The app fetches usage once on launch, then stays idle until you click *Refresh Now* or toggle on auto-refresh. See [Refresh behavior](#refresh-behavior) for why.

> **Heads-up:** [claude.ai/settings/usage](https://claude.ai/settings/usage) currently exposes additional buckets (per-model weekly limits, daily routine runs, etc.) that this app does not yet render. A schema-driven renderer that absorbs new buckets without code changes is being worked on — see the v1.2 milestone. Today, the menu shows the three buckets listed above.

---

## Why this exists

I'm on Claude Pro and use it heavily while working on side projects. Keeping track of the 5-hour and 7-day usage limits meant constantly switching to [claude.ai/settings/usage](https://claude.ai/settings/usage), breaking my flow every time I wanted to know how much headroom I had left.

I tried [CodexBar](https://codexbar.app/) but it was reporting my limits as fully consumed when they weren't, which made it useless for my workflow.

So I built claude-bar: a small menu bar icon that shows the real numbers straight from the Claude API, always visible, always accurate, zero clicks needed.

---

## Privacy and Security

This section is here first because it matters most.

### What claude-bar accesses

claude-bar reads **one session cookie** (`sessionKey` or `__Secure-next-auth.session-token`) from your browser's cookie store for `claude.ai`. That cookie is the same credential your browser already holds after you log in — claude-bar does not store it anywhere new; it reads it fresh from your browser on each launch.

It makes exactly **three API calls**:

1. `GET https://claude.ai/api/organizations` — to get your organization ID
2. `GET https://claude.ai/api/organizations/{id}/usage` — to fetch utilization numbers
3. `GET https://api.github.com/repos/mntrspace/claude-bar/releases/latest` — once at startup, to check for updates (no credentials sent)

You can verify all three calls yourself in [`claude_bar.py`](claude_bar.py).

### The macOS Keychain prompt

When claude-bar first runs, macOS shows a Keychain access dialog from your browser (Chrome or Safari). This is because the browser encrypts its cookie store with a key stored in your Keychain, and claude-bar — via the [`rookiepy`](https://github.com/thewh1teagle/rookiepy) library — needs to decrypt it.

Click **"Always Allow"** to avoid being asked again. The prompt is from your browser and macOS, not from claude-bar.

### What claude-bar does NOT do

- Does **not** read passwords, payment details, or any cookies other than the session token
- Does **not** send data to any third party — the only external calls are to `claude.ai` and the public GitHub API (no credentials)
- Does **not** store your session cookie outside your browser's cookie store
- Does **not** have network access beyond the three endpoints listed above

---

## Requirements

- macOS 12 Monterey or later
- A paid Claude subscription (usage data is only available for Pro/Team/Enterprise plans)
- Logged in to [claude.ai](https://claude.ai) in Chrome, Safari, Firefox, Brave, or Edge

---

## Install

**One-liner:**

```bash
curl -fsSL https://raw.githubusercontent.com/mntrspace/claude-bar/main/install.sh | bash
```

**What the script does (no surprises):**

1. Fetches the latest release tag from the GitHub API
2. Installs [`uv`](https://docs.astral.sh/uv/) (a fast Python package manager) if you don't have it — `uv` itself downloads a self-contained Python 3.12 if needed, so nothing system-wide is modified
3. Downloads the release source archive from GitHub into `~/.local/share/claude-bar/`
4. Strips the macOS Gatekeeper quarantine flag (prevents "unidentified developer" errors)
5. Runs `uv sync` to install Python dependencies into an isolated virtual environment
6. Creates a `run.sh` launcher in the install directory
7. Optionally writes and loads a LaunchAgent so claude-bar starts at login

Everything is self-contained in `~/.local/share/claude-bar/`. Nothing is written to system directories or `/usr/local`.

---

## Manual installation

```bash
git clone https://github.com/mntrspace/claude-bar.git
cd claude-bar
uv sync --frozen --no-dev
.venv/bin/python claude_bar.py
```

> Don't have `uv`? Install it with `brew install uv`

---

## Starting and stopping

Quit claude-bar at any time from its menu. Because the LaunchAgent is configured to restart only on crash (not on a normal quit), it stays quit until you start it again.

**Restart from the terminal:**

```bash
~/.local/share/claude-bar/run.sh
```

**Restart via launchd (if you installed the LaunchAgent):**

```bash
launchctl start com.user.claude-bar
```

**Prevent it from starting at login (without uninstalling):**

```bash
launchctl unload ~/Library/LaunchAgents/com.user.claude-bar.plist
```

**Re-enable start at login:**

```bash
launchctl load ~/Library/LaunchAgents/com.user.claude-bar.plist
```

---

## Refresh behavior

This fork ships with **auto-polling off** by default. The app does one fetch on launch (so the menu has a fresh number when you first open it) and then stays idle until you ask for more data. From the menu you can:

- **⟳ Refresh Now** — fetch once.
- **▶ Start auto-refresh (10 min)** — toggle. Once on, the label changes to `✓ Auto-refresh: 10 min` and the app polls in the background until you toggle it off again.
- **⏱ Refresh interval ▶** — submenu with `5 minutes`, `10 minutes`, `30 minutes`, `60 minutes`. The current selection is marked with `✓`. Changing the interval restarts the timer with the new cadence (if auto-refresh is on) or just stashes the value (if it's off).

**Defaults reset on every launch.** Auto-refresh always starts off; interval always starts at 10 minutes. There is no settings file. (If you want this to persist, open an issue — it's a small change.)

**Why opt-in?** Auto-polling every 5 minutes means ~288 calls per day to Anthropic's private `/usage` endpoint, which is a clear bot pattern. Opt-in significantly reduces that for users who only check occasionally. See [How it works](#how-it-works) for the underlying mechanism.

---

## Updating

claude-bar checks for updates automatically at startup. When a new version is available, a notification appears at the bottom of the menu:

> `🆕 Update available: v1.x.x — click to open`

Clicking it opens the GitHub release page in your browser. Then re-run the install command to apply the update — the script overwrites the install directory in place:

```bash
curl -fsSL https://raw.githubusercontent.com/mntrspace/claude-bar/main/install.sh | bash
```

---

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.user.claude-bar.plist 2>/dev/null || true
rm -rf ~/.local/share/claude-bar ~/Library/LaunchAgents/com.user.claude-bar.plist
```

---

## Troubleshooting

### "claude-bar cannot be opened because the developer cannot be verified"

The installer strips the quarantine attribute automatically. If you see this after a manual install, run:

```bash
xattr -r -d com.apple.quarantine ~/.local/share/claude-bar
```

### Keychain prompt keeps appearing

This means your browser session cookie has expired or been cleared. Log back in to [claude.ai](https://claude.ai) in your browser, then restart claude-bar.

### No data / shows "?" in menu bar

- Make sure you are logged in to `claude.ai` in Chrome or Safari
- Try specifying a browser explicitly (see **Advanced** below)
- If you use a less common browser, try using Chrome or Safari for the Claude session

### Checking logs

When run as a LaunchAgent, output is written to:

```
~/.local/share/claude-bar/claude-bar.log
```

When run from the terminal, output goes to stdout/stderr directly.

### Wondering what the Claude API is returning?

On every successful refresh, claude-bar writes the raw `/usage` response to:

```
~/Library/Caches/claude-bar/last-response.json
```

Mode 0600 (only readable by you). Useful when a bucket you expect to see isn't rendered, or when contributing the renderer refactor (v1.2). Inspect with `jq`:

```bash
jq keys ~/Library/Caches/claude-bar/last-response.json
```

---

## Advanced

### `--browser` flag

By default claude-bar tries Chrome, Safari, Firefox, Brave, and Edge in order. To force a specific browser:

```bash
~/.local/share/claude-bar/run.sh --browser safari
~/.local/share/claude-bar/run.sh --browser chrome
~/.local/share/claude-bar/run.sh --browser firefox
~/.local/share/claude-bar/run.sh --browser brave
~/.local/share/claude-bar/run.sh --browser edge
```

To persist the choice, edit `~/.local/share/claude-bar/run.sh` and append `--browser <name>` to the last line.

---

## How it works

claude-bar is a native macOS menu bar app built with:

- [`rumps`](https://github.com/jaredks/rumps) — Python wrapper around AppKit's `NSStatusItem`
- [`rookiepy`](https://github.com/thewh1teagle/rookiepy) — reads and decrypts browser cookie stores (handles macOS Keychain decryption)
- [`curl-cffi`](https://github.com/yifeikong/curl-cffi) — HTTP client that impersonates Chrome's TLS fingerprint for `claude.ai` requests

On startup it reads your session cookie, authenticates against `claude.ai`, and does a single fetch so the menu has data when you first open it. After that, the app stays idle unless you click *Refresh Now* or toggle on auto-refresh from the menu (5 / 10 / 30 / 60 minute presets). Each API call runs on a background thread so the UI stays responsive. Session expiry (HTTP 401) clears the cached session and shows a key icon; the next refresh re-reads the cookie automatically.

A separate background thread checks the GitHub releases API 5 seconds after launch and compares the remote tag against the hardcoded `VERSION` constant. If a newer release exists, a menu item appears at the bottom of the menu linking directly to the release page.

The menu items use `NSAttributedString` (via `pyobjc`) to render a colour-coded progress bar and dim secondary text directly inside the native menu.

For a deeper architectural walkthrough — threading model, key invariants, security notes, and how to extend the app — see [`CLAUDE.md`](CLAUDE.md).

---

## Credits

Forked from [BOUSHABAMohammed/claude-bar](https://github.com/BOUSHABAMohammed/claude-bar). The original concept, scraping approach, and the bulk of the code are his work — this fork stands on his shoulders. If you find claude-bar useful, please ⭐ the upstream repo.

What this fork (`mntrspace/claude-bar`) adds on top:

- **Opt-in auto-poll** with an interval picker (5 / 10 / 30 / 60 minutes). Upstream auto-polls every 5 minutes by default; this fork ships with auto-poll **off** so the app makes ~zero background calls unless you explicitly turn it on. Reduces detection surface against Anthropic's bot filtering for users who only check occasionally.
- **Debug response dump** at `~/Library/Caches/claude-bar/last-response.json`, mode 0600 — captures the raw `/usage` response on every refresh so the renderer can be made schema-driven (v1.2 work).
- **`CLAUDE.md`** — codebase guide for AI agents and humans picking up the project.
- **Documentation updates** to the README — credits, refresh behavior, troubleshooting entry for the dump, and a clearer "what it does and doesn't render" note.

Upstream commit history is preserved in `git log`.

---

## License

MIT
