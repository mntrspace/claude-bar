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

VERSION = "1.1.1"  # bump this with each release
GITHUB_REPO = "mntrspace/claude-bar"

INTERVAL_OPTIONS = (300, 600, 1800, 3600)  # seconds: 5, 10, 30, 60 min
DEFAULT_INTERVAL = 600  # 10 min — auto-poll is opt-in (off by default)
ICON_SETUP_DELAY_SECS = 0.5  # give the run loop time to start before loading the icon
UPDATE_CHECK_DELAY_SECS = 5.0  # wait for run loop to settle before hitting GitHub
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


def get_org_id(session: requests.Session, org_hint: str | None = None) -> tuple[str, str]:
    """Return (org_id, org_name) for the org we'll query.

    Selection rules:
      1. If `org_hint` is given, prefer the org whose name contains it (case-insensitive).
      2. Otherwise prefer the org with the highest priority — paid subscription first,
         then more capabilities (heuristic for "the active one").
    """
    resp = session.get("https://claude.ai/api/organizations", timeout=10)
    resp.raise_for_status()
    orgs = resp.json()
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
        set_menu_title(self.five_h_hdr, make_section_header("5-Hour Window"))
        set_menu_title(self.seven_d_hdr, make_section_header("7-Day Window"))
        set_menu_title(self.credits_hdr, make_section_header("Extra Credits"))
        self._set_credits_visible(False)  # hidden until first refresh confirms is_enabled
        self.update_item._menuitem.setHidden_(True)  # shown only when update is available
        self._refresh(None)
        rumps.Timer(self._setup_icon, ICON_SETUP_DELAY_SECS).start()
        rumps.Timer(self._start_update_check, UPDATE_CHECK_DELAY_SECS).start()

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------

    def _build_menu_items(self):
        self.five_h_hdr = rumps.MenuItem("◆ 5-Hour Window")
        self.five_h_row = rumps.MenuItem("  …")
        self.seven_d_hdr = rumps.MenuItem("◆ 7-Day Window")
        self.seven_d_row = rumps.MenuItem("  …")
        self.credits_hdr = rumps.MenuItem("◆ Extra Credits")
        self.credits_row = rumps.MenuItem("  …")
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
        self.last_item = rumps.MenuItem("  Last updated: —")
        self.update_item = rumps.MenuItem("  🆕 Update available", callback=self.on_open_update)
        self.version_item = rumps.MenuItem(f"  v{VERSION}")

        self.menu = [
            self.five_h_hdr, self.five_h_row, None,
            self.seven_d_hdr, self.seven_d_row, None,
            self.credits_hdr, self.credits_row, None,
            self.summary_toggle,
            self.auto_poll_toggle,
            self.interval_header,
            self.refresh_btn, self.last_item, None,
            self.update_item,
            self.version_item,
        ]

    def _init_state(self):
        self._session: requests.Session | None = None
        self._org_id: str | None = None
        self._refreshing: bool = False
        self._refresh_lock = threading.Lock()
        self._show_summary: bool = SHOW_TITLE_SUMMARY
        self._icon_loaded: bool = False
        self._last_fh_pct: float | None = None
        self._last_sd_pct: float | None = None
        self._credits_shown: bool = False
        self._update_url: str = f"https://github.com/{GITHUB_REPO}/releases"
        self._auto_poll_enabled: bool = False
        self._poll_interval_secs: int = DEFAULT_INTERVAL
        self._poll_timer: rumps.Timer | None = None

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
    # Callbacks
    # ------------------------------------------------------------------

    def on_refresh(self, _):
        self._refresh(None)

    def on_open_update(self, _):
        subprocess.Popen(["open", self._update_url])

    def on_toggle_summary(self, _):
        self._show_summary = not self._show_summary
        self._update_toggle_label()
        self._apply_title()

    def on_toggle_auto_poll(self, _):
        self._auto_poll_enabled = not self._auto_poll_enabled
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
            self._session = build_session(self._browser)
        if self._org_id is None:
            self._org_id, _ = get_org_id(self._session, self._org_hint)

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
    # Menu update
    # ------------------------------------------------------------------

    def _set_credits_visible(self, visible: bool):
        self.credits_hdr._menuitem.setHidden_(not visible)
        self.credits_row._menuitem.setHidden_(not visible)
        self._credits_shown = visible

    def _render_window(self, row_item, pct: float, suffix: str):
        set_menu_title(row_item, make_progress_row(pct, suffix))

    def _render_credits(self, extra_usage: dict):
        if extra_usage.get("is_enabled"):
            used = (extra_usage.get("used_credits") or 0) / 100
            limit = (extra_usage.get("monthly_limit") or 0) / 100
            util = extra_usage.get("utilization") or 0
            set_menu_title(self.credits_row,
                make_plain(f"  ${used:.2f} used of ${limit:,.0f}  ({util:.2f}%)", ColorKey.CREDITS))
            if not self._credits_shown:
                self._set_credits_visible(True)
        elif self._credits_shown:
            self._set_credits_visible(False)

    def _update_menu(self, data: dict):
        try:
            five_hour = data.get("five_hour")
            seven_day = data.get("seven_day")

            if five_hour is None or seven_day is None:
                print(f"[claude_bar] unexpected API response keys: {list(data.keys())}")
                set_menu_title(self.last_item,
                    make_plain("  Error: unexpected API response shape", ColorKey.ERROR))
                return

            fh_util = five_hour.get("utilization", 0.0)
            fh_resets = five_hour.get("resets_at")
            sd_util = seven_day.get("utilization", 0.0)
            sd_resets = seven_day.get("resets_at")

            self._render_window(self.five_h_row, fh_util,
                                "   Starts when a message is sent" if fmt_reset(fh_resets) == "unknown" else f"  resets in {fmt_reset(fh_resets)}")
            self._render_window(self.seven_d_row, sd_util,
                                f"  resets {fmt_date(sd_resets)}")

            # Update title and timestamp FIRST — before credits which may fail
            self._last_fh_pct = fh_util
            self._last_sd_pct = sd_util
            self._apply_title()
            set_menu_title(self.last_item,
                make_plain(f"  Last updated {datetime.datetime.now():%H:%M:%S}", ColorKey.LAST_UPDATED))

            # Credits section isolated — crash here won't affect title/timestamp
            try:
                self._render_credits(data.get("extra_usage") or {})
            except Exception as exc:
                print(f"[claude_bar] credits render error: {type(exc).__name__}: {exc}")

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
