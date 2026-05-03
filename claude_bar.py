#!/usr/bin/env python3
"""Claude Usage macOS Menu Bar App

Displays real usage data from claude.ai. Auto-refresh is opt-in via the menu
(intervals: 5 / 10 / 30 / 60 minutes); manual refresh is always available.
Run: python claude_bar.py
"""

import argparse
import datetime
import hashlib
import json
import pathlib
import sqlite3
import subprocess
import threading
from datetime import timezone

import rumps
import rookiepy
from curl_cffi import requests
from PyObjCTools.AppHelper import callAfter
from color_utils import (
    ColorKey,
    make_plain, make_section_header, make_progress_row,
    set_menu_title,
)

VERSION = "1.2.1"  # bump this with each release
GITHUB_REPO = "mntrspace/claude-bar"

INTERVAL_OPTIONS = (300, 600, 1800, 3600)  # seconds: 5, 10, 30, 60 min
DEFAULT_INTERVAL = 600  # 10 min — auto-poll is opt-in (off by default)
ICON_SETUP_DELAY_SECS = 0.5  # give the run loop time to start before loading the icon
UPDATE_CHECK_DELAY_SECS = 5.0  # wait for run loop to settle before hitting GitHub
FIRST_RUN_WIZARD_DELAY_SECS = 0.3  # let the run loop start before any rumps.alert
COOKIE_NAMES = ("sessionKey", "__Secure-next-auth.session-token")
BROWSERS = ("chrome", "dia", "safari", "firefox", "brave", "edge", "arc")  # edge support on macOS is limited in rookiepy

# Chromium-based browsers that rookiepy doesn't natively support but follow the
# standard Chromium-on-macOS encryption scheme (PBKDF2-HMAC-SHA1, salt "saltysalt",
# 1003 iterations, AES-128-CBC, IV = 16 spaces). Each entry maps to:
#   (cookie_db_relative_path, keychain_service, keychain_account)
# The keychain account is the one shown by `security find-generic-password`.
CHROMIUM_LIKE_BROWSERS = {
    "dia": (
        "Library/Application Support/Dia/User Data/Default/Cookies",
        "Dia Safe Storage",
        "Dia",
    ),
}
ICON_PATH = pathlib.Path(__file__).parent / "icons8-claude-ai-96.png"
DEBUG_DUMP_PATH = pathlib.Path.home() / "Library" / "Caches" / "claude-bar" / "last-response.json"
SETTINGS_PATH = pathlib.Path.home() / "Library" / "Application Support" / "claude-bar" / "settings.json"

# Persisted user preferences. Loaded at startup, written on every state change.
DEFAULT_SETTINGS = {
    "version": 1,
    "browser": None,                   # None until the first-run wizard finishes
    "org_id": None,                    # UUID; None means "auto-pick paid org"
    "org_name": None,                  # display-only, kept in sync with org_id
    "auto_poll_enabled": False,
    "poll_interval_secs": DEFAULT_INTERVAL,
    "show_summary": True,
    "first_run_completed": False,
}

# ---------------------------------------------------------------------------
# Schema-driven renderer config
# ---------------------------------------------------------------------------

# Sections in render order. Each section gets a header + N pre-allocated row slots.
SECTIONS = ("session", "weekly", "additional", "extras")

SECTION_TITLES = {
    "session":    "Current session",
    "weekly":     "Weekly limits",
    "additional": "Additional features",
    "extras":     "Extra credits",
}

# Pre-allocated dynamic rows. Bump if Anthropic adds more buckets than fit.
WEEKLY_SLOTS = 8
ADDITIONAL_SLOTS = 4

# Curated (key → (section, label)) mapping for known API keys. Order is the
# render order WITHIN a section. Anything not here is classified by heuristic
# and rendered with prettify(key) as the label.
LABELS: dict[str, tuple[str, str]] = {
    "five_hour":            ("session",    "Current session"),
    "seven_day":            ("weekly",     "All models"),
    "seven_day_sonnet":     ("weekly",     "Sonnet only"),
    "seven_day_opus":       ("weekly",     "Opus only"),
    "seven_day_omelette":   ("weekly",     "Claude Design"),    # codename guess
    "seven_day_cowork":     ("weekly",     "Coworking"),         # guess
    "seven_day_oauth_apps": ("weekly",     "Connected apps"),
    "iguana_necktie":       ("additional", "Daily routines"),    # guess from screenshot
    "extra_usage":          ("extras",     "Extra credits"),
}

# Set to False to hide the percentage summary next to the menu bar icon.
# Can also be toggled at runtime via the menu.
SHOW_TITLE_SUMMARY = True


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------

def _parse_version(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag.lstrip("v").split("."))


def check_for_update() -> tuple[str, str] | None:
    """Return (tag_name, html_url) if a newer release exists on GitHub, else None.

    Uses curl_cffi rather than urllib because the bundled Python on macOS often
    lacks a working CA bundle, which made urllib raise CERTIFICATE_VERIFY_FAILED.
    """
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"User-Agent": "claude-bar"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        tag = data.get("tag_name", "")
        html_url = data.get("html_url", "")
        if tag and _parse_version(tag) > _parse_version(VERSION):
            return tag, html_url
    except Exception as exc:
        print(f"[claude_bar] update check error: {type(exc).__name__}: {exc}")
    return None


# ---------------------------------------------------------------------------
# Auth / cookie helpers
# ---------------------------------------------------------------------------

def _decrypt_chromium_cookie(encrypted: bytes, key: bytes) -> str | None:
    """Decrypt a Chromium-on-macOS encrypted_value blob.

    Format: prefix (b"v10" or b"v11") + AES-128-CBC ciphertext.
    IV is 16 ASCII spaces. Chrome 130+ binds the cookie to its host+path by
    prepending a 32-byte SHA-256 inside the encrypted plaintext; we strip that
    if it's present.
    """
    if not encrypted or encrypted[:3] not in (b"v10", b"v11"):
        return None
    body = encrypted[3:]
    iv_hex = "20" * 16
    try:
        result = subprocess.run(
            ["openssl", "enc", "-aes-128-cbc", "-d", "-K", key.hex(), "-iv", iv_hex],
            input=body, capture_output=True, check=True,
        )
    except subprocess.CalledProcessError:
        return None
    pt = result.stdout
    if not pt:
        return None
    # PKCS7 unpad
    pad = pt[-1]
    if 1 <= pad <= 16:
        pt = pt[:-pad]
    # Strip the 32-byte host+path SHA-256 prefix (Chrome 130+) if the head looks
    # non-printable. A real cookie value is always printable ASCII.
    if len(pt) > 32 and any(b < 0x20 or b > 0x7E for b in pt[:8]):
        pt = pt[32:]
    try:
        return pt.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _load_chromium_like_cookies(browser: str, domain: str):
    """Yield (name, value) tuples from a non-rookiepy-native Chromium browser.

    Uses the macOS Keychain to fetch the AES key, reads the SQLite cookie store
    in read-only mode, and decrypts each row via openssl. Stdlib + openssl only,
    no extra Python deps.
    """
    rel_db, service, account = CHROMIUM_LIKE_BROWSERS[browser]
    db_path = pathlib.Path.home() / rel_db
    if not db_path.exists():
        return
    try:
        pw = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        print(f"[claude_bar] {browser}: keychain lookup failed ({exc.returncode})")
        return
    key = hashlib.pbkdf2_hmac("sha1", pw.encode("utf-8"), b"saltysalt", 1003, dklen=16)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        rows = conn.execute(
            "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE ?",
            (f"%{domain}%",),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[claude_bar] {browser}: sqlite error: {exc}")
        return
    for name, encrypted in rows:
        if name not in COOKIE_NAMES:
            continue
        value = _decrypt_chromium_cookie(encrypted, key)
        if value:
            yield name, value


def get_session_cookie(browser: str):
    """Return (name, value) for the first matching claude.ai session cookie."""
    try:
        if browser in CHROMIUM_LIKE_BROWSERS:
            for name, value in _load_chromium_like_cookies(browser, "claude.ai"):
                return name, value
            return None, None
        loader = getattr(rookiepy, browser)
        for c in loader(["claude.ai"]):
            if c["name"] in COOKIE_NAMES:
                return c["name"], c["value"]
    except Exception as exc:
        print(f"[claude_bar] {browser}: {type(exc).__name__}")
    return None, None


def build_session(browser: str | None = None) -> requests.Session:
    """Build a requests.Session with the claude.ai session cookie attached."""
    browsers_to_try = (browser,) if browser else BROWSERS
    for b in browsers_to_try:
        name, val = get_session_cookie(b)
        if val:
            print(f"[claude_bar] Using cookie '{name}' from {b}")
            s = requests.Session(impersonate="chrome120")
            s.cookies.set(name, val, domain="claude.ai")
            return s
    if browser:
        raise RuntimeError(f"No session cookie found in '{browser}'.")
    raise RuntimeError(
        "No claude.ai session cookie found. "
        "Log in to Claude in Chrome or Safari first."
    )


def _validate_browser(browser: str) -> list | None:
    """Probe whether `browser` has a working claude.ai session.

    Returns the orgs list on success, None on any failure (no cookie, network
    error, non-200 response, etc.). This is used by the first-run wizard to
    figure out which browsers can actually log in to Claude. Side effect: each
    call may trigger a Keychain prompt the first time we touch a given
    Chromium-based browser.
    """
    try:
        s = build_session(browser)
        r = s.get("https://claude.ai/api/organizations", timeout=8)
        if r.status_code != 200:
            return None
        orgs = r.json()
        return orgs if orgs else None
    except Exception as exc:
        print(f"[claude_bar] validate {browser}: {type(exc).__name__}")
        return None


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

FREE_BILLING_TYPES = {"default_claude_ai", "free", None, ""}


def _org_priority(org: dict) -> tuple:
    """Sort key — higher is better. Prefers paid subscriptions over free orgs,
    then orgs with more capabilities. Stable on ties."""
    billing = (org.get("billing_type") or org.get("rate_limit_tier") or "").strip()
    is_paid = billing not in FREE_BILLING_TYPES
    cap_count = len(org.get("capabilities") or [])
    return (is_paid, cap_count)


def fetch_organizations(session: requests.Session) -> list[dict]:
    """Hit /api/organizations and return the raw list."""
    resp = session.get("https://claude.ai/api/organizations", timeout=10)
    resp.raise_for_status()
    orgs = resp.json()
    return orgs or []


def pick_org(orgs: list[dict], org_hint: str | None = None) -> tuple[str, str]:
    """Pick one org from the list. See get_org_id for selection rules."""
    if not orgs:
        raise RuntimeError("No organizations returned from API")

    chosen = None
    if org_hint:
        needle = org_hint.lower()
        # Score each org by match quality: exact > startswith > substring. Pick the
        # highest score; tie-break by paid-plan priority. Avoids "100ms" greedily
        # matching "mantra@100ms.live's Organization" before "100ms".
        def _score(org):
            name = (org.get("name") or "").lower()
            if name == needle:
                return 3
            if name.startswith(needle):
                return 2
            if needle in name:
                return 1
            return 0
        scored = [(o, _score(o)) for o in orgs]
        best = max(scored, key=lambda pair: (pair[1], _org_priority(pair[0])))
        if best[1] == 0:
            available = ", ".join(repr(o.get("name") or "?") for o in orgs)
            raise RuntimeError(
                f"No organization name matched --org {org_hint!r}. Available: {available}"
            )
        chosen = best[0]
    else:
        chosen = max(orgs, key=_org_priority)

    org_id = chosen.get("uuid") or chosen.get("id")
    if not org_id:
        raise RuntimeError("Organization has no 'uuid' or 'id' field")
    name = chosen.get("name") or "(unnamed)"
    if len(orgs) > 1:
        print(f"[claude_bar] {len(orgs)} orgs found; using '{name}'. Override with --org NAME.")
    return org_id, name


def get_org_id(session: requests.Session, org_hint: str | None = None) -> tuple[str, str]:
    """Compatibility shim: fetch + pick in one call. Returns (org_id, org_name).

    Prefer fetch_organizations + pick_org separately when you also need the
    full orgs list (e.g. to populate the Organization submenu).
    """
    return pick_org(fetch_organizations(session), org_hint)


def fetch_usage(session: requests.Session, org_id: str) -> dict:
    resp = session.get(
        f"https://claude.ai/api/organizations/{org_id}/usage",
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _dump_response(data: dict) -> None:
    """Write the raw API response to disk so the renderer can be iterated against
    real shapes. Mode 0600. Errors are non-fatal — we never want a debug aid to
    take down the main refresh path."""
    try:
        DEBUG_DUMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEBUG_DUMP_PATH.write_text(json.dumps(data, indent=2, default=str))
        DEBUG_DUMP_PATH.chmod(0o600)
    except Exception as exc:
        print(f"[claude_bar] dump error: {type(exc).__name__}: {exc}")


def prettify(key: str) -> str:
    """Convert an API codename to a human-readable label. Drops the
    'seven_day_' prefix because 'Weekly limits' section header already
    carries that context."""
    base = key.removeprefix("seven_day_") or key
    return base.replace("_", " ").title()


def classify(key: str, value) -> tuple[str, str]:
    """Return (section, label) for an API bucket, falling back to heuristics
    for unknown keys."""
    if key in LABELS:
        return LABELS[key]
    if key.startswith("seven_day_"):
        return ("weekly", prettify(key))
    if isinstance(value, dict) and "is_enabled" in value:
        return ("extras", prettify(key))
    return ("additional", prettify(key))


def bucket_shape(value) -> str:
    """Return 'utilization', 'count', or 'unknown' for an API bucket value.
    Unknown shapes (incl. null fields, disabled features) are skipped silently."""
    if not isinstance(value, dict) or not value:
        return "unknown"
    if value.get("is_enabled") is False:
        return "unknown"
    # Count buckets win over utilization for buckets that carry both fields
    # (e.g. extra_usage). Skip if the limit isn't actually set.
    if "used_credits" in value and value.get("monthly_limit") not in (None, 0):
        return "count"
    if "used" in value and value.get("limit") not in (None, 0):
        return "count"
    if value.get("utilization") is not None:
        return "utilization"
    return "unknown"


def load_settings() -> dict:
    """Return the persisted settings dict, falling back to defaults on missing
    or corrupt files. Always returns every key in DEFAULT_SETTINGS so callers
    can `settings[key]` without KeyError on schema additions."""
    try:
        loaded = json.loads(SETTINGS_PATH.read_text())
        if not isinstance(loaded, dict):
            raise ValueError("settings file is not a JSON object")
        return {**DEFAULT_SETTINGS, **loaded}
    except FileNotFoundError:
        return dict(DEFAULT_SETTINGS)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"[claude_bar] settings load error: {type(exc).__name__}: {exc}; using defaults")
        return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    """Persist settings to ~/Library/Application Support/claude-bar/settings.json
    with mode 0600. Non-fatal on failure — a save error should never crash the app."""
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
        SETTINGS_PATH.chmod(0o600)
    except OSError as exc:
        print(f"[claude_bar] settings save error: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _parse_iso(iso) -> datetime.datetime | None:
    """Parse an ISO 8601 string to a timezone-aware datetime, or None if invalid."""
    if not isinstance(iso, str):
        return None
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))


def fmt_reset(iso) -> str:
    """Format time remaining until reset as '2h 44m'."""
    dt = _parse_iso(iso)
    if dt is None:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    total_secs = max(0, int((dt - datetime.datetime.now(timezone.utc)).total_seconds()))
    h, rem = divmod(total_secs, 3600)
    return f"{h}h {rem // 60:02d}m"


def fmt_date(iso) -> str:
    """Format reset date as 'Fri Mar 06 06:00' (Local Time)."""
    dt = _parse_iso(iso)
    if dt is None:
        return "unknown"
    local_dt = dt.astimezone()
    return local_dt.strftime("%a %b %d %H:%M %p")


# ---------------------------------------------------------------------------
# Menu bar app
# ---------------------------------------------------------------------------

class ClaudeBar(rumps.App):
    def __init__(self, browser: str | None = None, org: str | None = None):
        super().__init__("Claude", "⚡ …")
        self._browser = browser
        self._org_hint = org
        self._build_menu_items()
        self._init_state()
        self._update_toggle_label()
        self._update_auto_poll_label()
        self._update_interval_marks()
        self._repaint_browser_marks()
        for section in SECTIONS:
            set_menu_title(self.section_hdrs[section], make_section_header(SECTION_TITLES[section]))
        self.update_item._menuitem.setHidden_(True)  # shown only when update is available

        # Decide whether we have enough info to authenticate, or need to ask
        # the user via the first-run wizard. CLI flags always win; a saved
        # browser in settings is enough; otherwise the wizard runs once and
        # picks for them.
        if self._browser or self._settings.get("browser") or self._settings.get("first_run_completed"):
            self._refresh(None)
        else:
            rumps.Timer(self._run_first_run_wizard, FIRST_RUN_WIZARD_DELAY_SECS).start()

        rumps.Timer(self._setup_icon, ICON_SETUP_DELAY_SECS).start()
        rumps.Timer(self._start_update_check, UPDATE_CHECK_DELAY_SECS).start()

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------

    def _build_menu_items(self):
        # Section headers + pre-allocated row slots. All start hidden; the
        # renderer shows whichever slots have data on each refresh.
        slot_counts = {
            "session":    1,
            "weekly":     WEEKLY_SLOTS,
            "additional": ADDITIONAL_SLOTS,
            "extras":     1,
        }
        self.section_hdrs: dict[str, rumps.MenuItem] = {}
        self.section_rows: dict[str, list[rumps.MenuItem]] = {}
        for section in SECTIONS:
            self.section_hdrs[section] = rumps.MenuItem("")
            self.section_rows[section] = [rumps.MenuItem("  …") for _ in range(slot_counts[section])]
        for hdr in self.section_hdrs.values():
            hdr._menuitem.setHidden_(True)
        for rows in self.section_rows.values():
            for row in rows:
                row._menuitem.setHidden_(True)

        # Persistent items (toggles, refresh, version, etc.)
        self.refresh_btn = rumps.MenuItem("  ⟳ Refresh Now", callback=self.on_refresh)
        self.summary_toggle = rumps.MenuItem("", callback=self.on_toggle_summary)
        self.auto_poll_toggle = rumps.MenuItem("", callback=self.on_toggle_auto_poll)
        self.interval_header = rumps.MenuItem("  ⏱ Refresh interval")
        self.interval_items = {
            secs: rumps.MenuItem("", callback=self._make_interval_callback(secs))
            for secs in INTERVAL_OPTIONS
        }
        for item in self.interval_items.values():
            self.interval_header.add(item)
        self.org_header = rumps.MenuItem("  🏢 Organization")
        # Children are added dynamically once we have the orgs list (in _rebuild_org_menu).
        # Until then the submenu shows a placeholder.
        self._org_placeholder = rumps.MenuItem("    (loading…)")
        self.org_header.add(self._org_placeholder)

        self.browser_header = rumps.MenuItem("  🌐 Browser")
        self.browser_items = {
            b: rumps.MenuItem("", callback=self._make_browser_callback(b))
            for b in BROWSERS
        }
        for item in self.browser_items.values():
            self.browser_header.add(item)
        self.last_item = rumps.MenuItem("  Last updated: —")
        self.update_item = rumps.MenuItem("  🆕 Update available", callback=self.on_open_update)
        self.version_item = rumps.MenuItem(f"  v{VERSION}")

        # Render order: section header → its rows → separator, then toggles/refresh/etc.
        menu_items: list = []
        for section in SECTIONS:
            menu_items.append(self.section_hdrs[section])
            menu_items.extend(self.section_rows[section])
            menu_items.append(None)
        menu_items.extend([
            self.summary_toggle,
            self.auto_poll_toggle,
            self.interval_header,
            self.org_header,
            self.browser_header,
            self.refresh_btn,
            self.last_item,
            None,
            self.update_item,
            self.version_item,
        ])
        self.menu = menu_items

    def _init_state(self):
        # settings is the source of truth for user prefs across launches.
        self._settings: dict = load_settings()
        self._session: requests.Session | None = None
        self._org_id: str | None = self._settings["org_id"]
        self._orgs: list[dict] | None = None
        self._refreshing: bool = False
        self._refresh_lock = threading.Lock()
        self._show_summary: bool = self._settings["show_summary"]
        self._icon_loaded: bool = False
        self._last_fh_pct: float | None = None
        self._last_sd_pct: float | None = None
        self._update_url: str = f"https://github.com/{GITHUB_REPO}/releases"
        self._auto_poll_enabled: bool = self._settings["auto_poll_enabled"]
        self._poll_interval_secs: int = self._settings["poll_interval_secs"]
        self._poll_timer: rumps.Timer | None = None

    def _save(self) -> None:
        """Persist current state to settings.json. Called from every callback that
        mutates a user-facing preference."""
        save_settings(self._settings)

    # ------------------------------------------------------------------
    # Icon setup
    # ------------------------------------------------------------------

    def _setup_icon(self, _):
        if not ICON_PATH.exists():
            print(f"[claude_bar] icon not found: {ICON_PATH}")
            return
        self.icon = str(ICON_PATH)
        self.template = True
        self._icon_loaded = True
        self._apply_title()

    # ------------------------------------------------------------------
    # First-run wizard
    # ------------------------------------------------------------------

    def _run_first_run_wizard(self, timer):
        """Probe every supported browser, ask the user to pick one if multiple
        have valid Claude sessions, save the choice, then trigger the first
        refresh. Designed to run from a one-shot Timer (~0.3s after launch)
        so the AppKit run loop is up before any rumps.alert."""
        timer.stop()
        if self._settings.get("first_run_completed"):
            return  # belt-and-suspenders; should never happen given the gate in __init__

        candidates: list[tuple[str, list]] = []
        for browser in BROWSERS:
            orgs = _validate_browser(browser)
            if orgs:
                candidates.append((browser, orgs))

        if not candidates:
            rumps.alert(
                title="Welcome to claude-bar",
                message=(
                    "Couldn't find a Claude session in any supported browser. "
                    "Log in to claude.ai in Chrome, Dia, Safari, Firefox, Brave, "
                    "Edge, or Arc — then click Refresh in the menu."
                ),
            )
            return  # don't mark complete; retry on next launch / refresh

        if len(candidates) == 1:
            browser, orgs = candidates[0]
        elif len(candidates) <= 3:
            names = [b for b, _ in candidates]
            kwargs = {"ok": names[0], "cancel": "Quit"}
            if len(names) >= 2:
                kwargs["other"] = names[1]
            if len(names) == 3:
                kwargs["cancel"] = names[2]
            choice = rumps.alert(
                title="Multiple Claude sessions found",
                message=(
                    f"Found Claude sessions in: {', '.join(names)}. "
                    "Which browser should claude-bar use?"
                ),
                **kwargs,
            )
            # rumps.alert returns: 1 = OK (first), 0 = Cancel (last), 2 = Other (middle)
            picked: str | None = None
            if choice == 1:
                picked = names[0]
            elif choice == 2 and len(names) >= 2:
                picked = names[1]
            elif choice == 0 and len(names) == 3:
                picked = names[2]
            if picked is None:
                # User dismissed the dialog without committing; bail out
                # and re-run the wizard on next launch.
                return
            browser = picked
            orgs = next(o for b, o in candidates if b == browser)
        else:
            # 4+: pick by BROWSERS-order priority (the order is hand-tuned)
            names = [b for b, _ in candidates]
            browser = names[0]
            orgs = candidates[0][1]
            rumps.alert(
                title="Multiple Claude sessions found",
                message=(
                    f"Found Claude sessions in: {', '.join(names)}. "
                    f"Using {browser}. Switch via menu → 🌐 Browser if needed."
                ),
            )

        # Pick org: 1 → silent; 2+ → auto-pick paid + inform
        chosen = max(orgs, key=_org_priority)
        if len(orgs) > 1:
            other_names = ", ".join(o.get("name") or "(unnamed)" for o in orgs if o is not chosen)
            rumps.alert(
                title="Multiple organizations found",
                message=(
                    f"Using '{chosen.get('name') or '(unnamed)'}' (paid plan preferred). "
                    f"Other orgs available: {other_names}. "
                    "Switch via menu → 🏢 Organization if needed."
                ),
            )

        org_id = chosen.get("uuid") or chosen.get("id")
        org_name = chosen.get("name") or "(unnamed)"

        # Persist
        self._settings["browser"] = browser
        self._settings["org_id"] = org_id
        self._settings["org_name"] = org_name
        self._settings["first_run_completed"] = True
        self._save()

        # In-memory state
        self._org_id = org_id
        self._orgs = orgs
        self._rebuild_org_menu(orgs)
        # Discard any stale session that might have been built during validation
        self._session = None

        self._refresh(None)

    # ------------------------------------------------------------------
    # Title helpers
    # ------------------------------------------------------------------

    def _apply_title(self):
        """Set self.title based on current show_summary flag and last known data."""
        if self._show_summary and self._last_fh_pct is not None:
            self.title = f"{self._last_fh_pct:.0f}% · {self._last_sd_pct:.0f}%"
        elif self._icon_loaded:
            self.title = None  # icon-only
        # else: no data yet and icon not loaded — leave "⚡ …" placeholder untouched

    def _update_toggle_label(self):
        mark = "✓" if self._show_summary else "  "
        self.summary_toggle.title = f"  {mark} Show % in status bar"

    def _update_auto_poll_label(self):
        mins = self._poll_interval_secs // 60
        self.auto_poll_toggle.title = (
            f"  ✓ Auto-refresh: {mins} min"
            if self._auto_poll_enabled
            else f"  ▶ Start auto-refresh ({mins} min)"
        )

    def _update_interval_marks(self):
        for secs, item in self.interval_items.items():
            mark = "✓" if secs == self._poll_interval_secs else "  "
            item.title = f"  {mark} {secs // 60} minutes"

    # ------------------------------------------------------------------
    # Auto-poll timer
    # ------------------------------------------------------------------

    def _make_interval_callback(self, secs: int):
        def _cb(_):
            self._set_interval(secs)
        return _cb

    def _set_interval(self, secs: int):
        if secs == self._poll_interval_secs:
            return
        self._poll_interval_secs = secs
        self._settings["poll_interval_secs"] = secs
        self._save()
        self._update_interval_marks()
        self._update_auto_poll_label()
        if self._auto_poll_enabled:
            self._stop_timer()
            self._start_timer()

    def _start_timer(self):
        # Re-create on each start so interval changes take effect cleanly,
        # without relying on rumps.Timer.interval being live-mutable.
        self._poll_timer = rumps.Timer(self._auto_refresh, self._poll_interval_secs)
        self._poll_timer.start()

    def _stop_timer(self):
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None

    # ------------------------------------------------------------------
    # Organization submenu
    # ------------------------------------------------------------------

    def _make_org_callback(self, uuid: str, name: str):
        def _cb(_):
            self._switch_org(uuid, name)
        return _cb

    def _rebuild_org_menu(self, orgs: list[dict]) -> None:
        """Rebuild the Organization submenu from the current orgs list. Idempotent;
        safe to call after any /api/organizations response. MUST run on the main
        thread (use callAfter from background threads)."""
        # rumps stores submenu children inside a private attribute; clear by
        # reassigning the underlying NSMenu.
        try:
            self.org_header._menuitem.submenu().removeAllItems()
        except Exception:
            pass  # best effort; on failure we'll just append next to existing items
        for org in orgs:
            uuid = org.get("uuid") or org.get("id") or ""
            name = org.get("name") or "(unnamed)"
            mark = "✓" if uuid == self._org_id else "  "
            label = f"  {mark} {name}"
            item = rumps.MenuItem(label, callback=self._make_org_callback(uuid, name))
            self.org_header.add(item)

    def _switch_org(self, uuid: str, name: str) -> None:
        """Handler for an Organization submenu click."""
        if uuid == self._org_id:
            return
        self._org_id = uuid
        self._settings["org_id"] = uuid
        self._settings["org_name"] = name
        self._save()
        # Repaint checkmarks against the cached orgs list, then trigger refresh.
        if self._orgs:
            self._rebuild_org_menu(self._orgs)
        self._refresh(None)

    # ------------------------------------------------------------------
    # Browser submenu
    # ------------------------------------------------------------------

    def _make_browser_callback(self, name: str):
        def _cb(_):
            self._switch_browser(name)
        return _cb

    def _active_browser(self) -> str | None:
        """Whichever browser the menu should mark as active. CLI flag wins
        until the user clicks a different one in the submenu."""
        return self._browser or self._settings.get("browser")

    def _repaint_browser_marks(self) -> None:
        active = self._active_browser()
        for name, item in self.browser_items.items():
            mark = "✓" if name == active else "  "
            item.title = f"  {mark} {name}"

    def _switch_browser(self, name: str) -> None:
        """Handler for a Browser submenu click. Drops any CLI override and
        forces a clean re-auth on the next refresh."""
        if name == self._active_browser():
            return
        self._browser = None  # any CLI override is superseded by an explicit user click
        self._settings["browser"] = name
        # Switching cookie source means the org list may differ — invalidate caches.
        self._settings["org_id"] = None
        self._settings["org_name"] = None
        self._save()
        self._session = None
        self._orgs = None
        self._org_id = None
        self._repaint_browser_marks()
        self._refresh(None)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def on_refresh(self, _):
        self._refresh(None)

    def on_open_update(self, _):
        subprocess.Popen(["open", self._update_url])

    def on_toggle_summary(self, _):
        self._show_summary = not self._show_summary
        self._settings["show_summary"] = self._show_summary
        self._save()
        self._update_toggle_label()
        self._apply_title()

    def on_toggle_auto_poll(self, _):
        self._auto_poll_enabled = not self._auto_poll_enabled
        self._settings["auto_poll_enabled"] = self._auto_poll_enabled
        self._save()
        if self._auto_poll_enabled:
            self._start_timer()
        else:
            self._stop_timer()
        self._update_auto_poll_label()

    def _auto_refresh(self, _):
        self._refresh(None)

    # ------------------------------------------------------------------
    # Update check
    # ------------------------------------------------------------------

    def _start_update_check(self, timer):
        timer.stop()
        threading.Thread(target=self._check_update_bg, daemon=True).start()

    def _check_update_bg(self):
        result = check_for_update()
        if result:
            tag, url = result
            self._update_url = url
            callAfter(self._show_update_banner, tag)

    def _show_update_banner(self, tag: str):
        self.update_item.title = f"  🆕 Update available: {tag} — click to open"
        self.update_item._menuitem.setHidden_(False)

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _ensure_session(self):
        if self._session is None:
            # Priority: CLI flag → saved settings → auto-detect across BROWSERS
            browser = self._browser or self._settings.get("browser")
            self._session = build_session(browser)
        if self._orgs is None:
            self._orgs = fetch_organizations(self._session)
            if not self._orgs:
                raise RuntimeError("No organizations returned from API")
            callAfter(self._rebuild_org_menu, self._orgs)
        # CLI --org always overrides saved org_id for this session, but doesn't
        # rewrite settings (so a one-off run doesn't clobber the user's saved choice).
        if self._org_hint:
            new_id, _ = pick_org(self._orgs, self._org_hint)
            self._org_id = new_id
            return
        # If a saved org_id is no longer in the org list, fall back to auto-pick.
        if self._org_id is not None:
            valid_ids = {o.get("uuid") or o.get("id") for o in self._orgs}
            if self._org_id not in valid_ids:
                print(f"[claude_bar] saved org_id no longer available; auto-picking")
                self._org_id = None
        if self._org_id is None:
            self._org_id, name = pick_org(self._orgs, None)
            self._settings["org_id"] = self._org_id
            self._settings["org_name"] = name
            self._save()
            callAfter(self._rebuild_org_menu, self._orgs)

    def _handle_error(self, exc: Exception):
        resp = getattr(exc, "response", None)
        if resp is not None:
            code = resp.status_code
            if code == 401:
                self._session = None
                self._org_id = None
                self.title = "⚡ 🔑"
                set_menu_title(self.last_item,
                    make_plain("  Error: session expired — refresh to retry", ColorKey.ERROR))
            else:
                self.title = f"⚡ err {code}"
                set_menu_title(self.last_item,
                    make_plain(f"  HTTP error {code}", ColorKey.ERROR))
        else:
            self.title = "⚡ ?"
            set_menu_title(self.last_item,
                make_plain(f"  Error: {type(exc).__name__}", ColorKey.ERROR))
            print(f"[claude_bar] refresh error: {type(exc).__name__}: {str(exc)[:200]}")

    def _refresh(self, _):
        with self._refresh_lock:
            if self._refreshing:
                return
            self._refreshing = True
        threading.Thread(target=self._refresh_bg, daemon=True).start()

    def _refresh_bg(self):
        try:
            self._ensure_session()
            data = fetch_usage(self._session, self._org_id)
            _dump_response(data)
            callAfter(self._update_menu, data)
        except Exception as exc:
            callAfter(self._handle_error, exc)
        finally:
            with self._refresh_lock:
                self._refreshing = False

    # ------------------------------------------------------------------
    # Menu update — schema-driven renderer
    # ------------------------------------------------------------------

    def _render_utilization_row(self, row, section: str, label: str, value: dict) -> None:
        pct = value.get("utilization") or 0.0
        resets = value.get("resets_at")
        # Suffix conventions chosen to match Anthropic's UI:
        #   session  → "resets in 4h 41m" or "starts when a message is sent"
        #   weekly   → "resets Tue 4:30 PM"
        if section == "session":
            if fmt_reset(resets) == "unknown":
                suffix = f"   {label} — starts when a message is sent"
            else:
                suffix = f"   {label} — resets in {fmt_reset(resets)}"
        elif section == "weekly":
            if resets:
                suffix = f"   {label} — resets {fmt_date(resets)}"
            else:
                suffix = f"   {label}"
        else:
            suffix = f"   {label}"
        set_menu_title(row, make_progress_row(pct, suffix))

    def _render_count_row(self, row, label: str, value: dict) -> None:
        if "used_credits" in value:
            # extra_usage shape: cents → dollars
            used = (value.get("used_credits") or 0) / 100
            limit = (value.get("monthly_limit") or 0) / 100
            util = value.get("utilization") or 0
            text = f"  {label}: ${used:.2f} used of ${limit:,.0f}  ({util:.2f}%)"
        else:
            used = value.get("used") or 0
            limit = value.get("limit") or 0
            text = f"  {label}: {used} / {limit}"
        set_menu_title(row, make_plain(text, ColorKey.CREDITS))

    def _ordered_keys(self, data: dict) -> list[str]:
        """Render order: known LABELS keys (in declaration order) first, then
        unknown keys alphabetically. Stable across refreshes regardless of
        Anthropic's API response order."""
        known = [k for k in LABELS.keys() if k in data]
        unknown = sorted(k for k in data.keys() if k not in LABELS)
        return known + unknown

    def _update_menu(self, data: dict):
        try:
            used_slots = {s: 0 for s in SECTIONS}
            slots_total = {s: len(self.section_rows[s]) for s in SECTIONS}

            for key in self._ordered_keys(data):
                value = data.get(key)
                shape = bucket_shape(value)
                if shape == "unknown":
                    continue
                section, label = classify(key, value)
                n = used_slots[section]
                if n >= slots_total[section]:
                    print(f"[claude_bar] no slot for '{key}' in section '{section}' "
                          f"({slots_total[section]} slots); bump constants and rebuild")
                    continue
                row = self.section_rows[section][n]
                if shape == "utilization":
                    self._render_utilization_row(row, section, label, value)
                elif shape == "count":
                    self._render_count_row(row, label, value)
                row._menuitem.setHidden_(False)
                used_slots[section] += 1

            # Hide unused slots and headers with no visible rows
            for section in SECTIONS:
                for i in range(used_slots[section], slots_total[section]):
                    self.section_rows[section][i]._menuitem.setHidden_(True)
                self.section_hdrs[section]._menuitem.setHidden_(used_slots[section] == 0)

            # Title-bar percentage: still driven by the 5h + 7d "all models" buckets
            fh = data.get("five_hour") or {}
            sd = data.get("seven_day") or {}
            if "utilization" in fh:
                self._last_fh_pct = fh["utilization"]
            if "utilization" in sd:
                self._last_sd_pct = sd["utilization"]
            self._apply_title()

            set_menu_title(self.last_item,
                make_plain(f"  Last updated {datetime.datetime.now():%H:%M:%S}", ColorKey.LAST_UPDATED))

        except Exception as exc:
            print(f"[claude_bar] _update_menu error: {type(exc).__name__}: {exc}")
            set_menu_title(self.last_item,
                make_plain(f"  Error: {type(exc).__name__}", ColorKey.ERROR))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Claude usage menu bar app")
    parser.add_argument(
        "--browser",
        choices=list(BROWSERS),
        metavar="BROWSER",
        help=f"Browser to read session cookie from. Choices: {', '.join(BROWSERS)}",
    )
    parser.add_argument(
        "--org",
        metavar="NAME",
        help=(
            "Organization name (case-insensitive substring match) to query usage for. "
            "Default: the org with a paid subscription, or the first one returned."
        ),
    )
    args = parser.parse_args()
    ClaudeBar(browser=args.browser, org=args.org).run()


if __name__ == "__main__":
    main()
