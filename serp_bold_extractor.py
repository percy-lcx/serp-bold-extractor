#!/usr/bin/env python3
"""SERP Bold Text Extractor - Extract bolded terms from Google search results."""

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlencode, quote_plus
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth


def _get_user_agent():
    """Return a Chrome user-agent string matching the current platform."""
    if sys.platform == "darwin":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
    elif sys.platform.startswith("linux"):
        return (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )


USER_AGENT = _get_user_agent()
DEFAULT_PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".serp-bold-extractor", "profile")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract bold (<em>/<b>) terms from Google search result pages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python3 serp_bold_extractor.py "what is bitcoin" --pages 1
  python3 serp_bold_extractor.py "best crypto wallets" --pages 3 --delay 1.5
  python3 serp_bold_extractor.py "ethereum staking" --pages 2 > results.txt
  python3 serp_bold_extractor.py "defi explained" --output json > results.json
  python3 serp_bold_extractor.py "web3" --pages 2 --verbose
  python3 serp_bold_extractor.py "nft meaning" --pages 1 --debug

notes:
  On first run Chrome opens visibly. If Google shows a CAPTCHA, solve it once —
  the session is saved to --profile-dir and reused on all future runs (~6 months).
  To reset the session: rm -rf ~/.serp-bold-extractor/profile
"""
    )
    parser.add_argument("query", nargs="?", help="The Google search query string")
    parser.add_argument(
        "--pages",
        type=int,
        default=2,
        choices=range(1, 6),
        metavar="N",
        help="Number of SERP pages to extract (1-5, default: 2)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Base delay in seconds between page navigations (default: 3.0)",
    )
    parser.add_argument(
        "--hl",
        default="en",
        help="Google hl parameter — interface language (default: en)",
    )
    parser.add_argument(
        "--gl",
        default="us",
        help="Google gl parameter — geolocation (default: us)",
    )
    parser.add_argument(
        "--output",
        default="text",
        choices=["text", "json"],
        help="Output format: text (one per line) or json (default: text)",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Use plain HTTP requests instead of a browser (faster, no Playwright needed)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Dump raw HTML to debug_page_N.html files for inspection",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print timestamped progress messages to stderr",
    )
    parser.add_argument(
        "--profile-dir",
        default=DEFAULT_PROFILE_DIR,
        help="Persistent browser profile directory (default: ~/.serp-bold-extractor/profile, or profile-firefox for Firefox)",
    )
    parser.add_argument(
        "--browser",
        default="auto",
        choices=["auto", "chrome", "brave", "firefox", "chromium"],
        help="Browser to use: auto (default), chrome, brave, firefox, or chromium (Playwright bundled)",
    )
    parser.add_argument(
        "--file", "-f",
        metavar="FILE",
        help="Path to a TXT file with one query per line",
    )
    parser.add_argument(
        "--save-html",
        metavar="DIR",
        help="Save raw SERP HTML for each page to DIR/{query}_page{N}.html",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        metavar="N",
        help="Number of parallel browser tabs for batch queries (default: 3, range: 1-5)",
    )
    parser.add_argument(
        "--no-headless-switch",
        action="store_true",
        help="Stay in headed mode for the entire session (don't switch to headless after CAPTCHA)",
    )
    return parser.parse_args()


def load_queries_from_file(path):
    """Read queries from a text file, one per line. Empty lines are skipped."""
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


async def detect_blockers(page):
    """Check for CAPTCHAs and consent walls. Returns 'captcha', 'js_challenge', or None."""
    if "/sorry/" in page.url:
        return "captcha"

    if await page.locator("#captcha-form").count() > 0:
        return "captcha"

    # "Unusual traffic" only appears on /sorry/ pages which Google always redirects to —
    # it is never shown inline on a search results page, so skip the expensive full-body
    # text scan when we're already on a normal search URL.
    if "google.com/search" not in page.url:
        try:
            body_text = await page.locator("body").inner_text(timeout=3000)
            lower_text = body_text.lower()
            if "unusual traffic" in lower_text:
                page_html = await page.content()
                html_lower = page_html.lower()
                if "knitsail" in html_lower or "/httpservice/retry/enablejs" in html_lower:
                    return "js_challenge"
                return "captcha"
        except PlaywrightTimeout:
            pass

    # Consent wall: try to dismiss
    for btn_name in ("Reject all", "Accept all"):
        try:
            btn = page.get_by_role("button", name=btn_name)
            if await btn.count() > 0:
                await btn.click()
                await asyncio.sleep(2)
                return None
        except Exception:
            pass

    return None


def detect_blockers_html(html, url=""):
    """Check raw HTML for CAPTCHA or JS challenge indicators. Returns error string or None."""
    if "/sorry/" in url:
        return "captcha"
    lower = html.lower()
    if 'id="captcha-form"' in lower:
        return "captcha"
    if "unusual traffic" in lower:
        return "captcha"
    # Google JS challenge page — requires browser execution, plain HTTP won't work
    if "/httpservice/retry/enablejs" in lower or "knitsail" in lower:
        return "js_challenge"
    return None


def extract_from_page(html):
    """Extract bold term strings from SERP HTML. Returns list of strings."""
    soup = BeautifulSoup(html, "html.parser")
    # Try #search container first, fall back to entire page.
    container = soup.find(id="search") or soup
    # Google uses <em> in JS-rendered HTML and <b> in raw HTTP responses.
    # Search for both to handle either case.
    terms = []
    for tag in container.find_all("em"):
        text = tag.get_text(strip=True)
        if text:
            terms.append(text)
    return terms


# ---------------------------------------------------------------------------
# HTTP-only extraction (no browser)
# ---------------------------------------------------------------------------


def _save_page_html(html, save_html_dir, query, page_num):
    safe = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")[:60]
    os.makedirs(save_html_dir, exist_ok=True)
    path = os.path.join(save_html_dir, f"{safe}_page{page_num}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def _http_fetch(url):
    """Fetch a URL with realistic headers. Returns (html, final_url)."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    req = Request(url, headers=headers)
    resp = urlopen(req, timeout=15)
    return resp.read().decode("utf-8", errors="replace"), resp.url


def extract_bold_terms_http(query, pages, delay, hl, gl, debug=False, save_html_dir=None):
    """Extract bold terms using plain HTTP requests (no browser)."""
    all_terms = []
    pages_scraped = 0
    seen = set()

    for page_num in range(1, pages + 1):
        params = {"q": query, "hl": hl, "gl": gl}
        if page_num > 1:
            params["start"] = (page_num - 1) * 10
        url = f"https://www.google.com/search?{urlencode(params)}"

        if page_num > 1:
            sleep_time = max(0.5, delay + random.uniform(-1.0, 1.0))
            time.sleep(sleep_time)

        try:
            html, final_url = _http_fetch(url)
        except Exception as e:
            if pages_scraped == 0:
                return {
                    "query": query,
                    "total_terms": 0,
                    "pages_scraped": 0,
                    "terms": [],
                    "error": f"Failed to fetch search results: {e}",
                }
            print(f"Failed to fetch page {page_num}: {e}", file=sys.stderr)
            break

        if debug:
            filename = f"debug_page_{page_num}.html"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"[debug] Saved {len(html)} bytes to {filename}", file=sys.stderr)
            print(f"[debug] Final URL: {final_url}", file=sys.stderr)

        if save_html_dir:
            _save_page_html(html, save_html_dir, query, page_num)

        blocker = detect_blockers_html(html, final_url)
        if blocker == "js_challenge":
            return {
                "query": query,
                "total_terms": 0,
                "pages_scraped": 0,
                "terms": [],
                "error": (
                    "Google returned a JavaScript challenge page. "
                    "Plain HTTP mode cannot bypass this — run without "
                    "--http to use a browser instead."
                ),
            }
        if blocker == "captcha":
            if pages_scraped == 0:
                return {
                    "query": query,
                    "total_terms": len(all_terms),
                    "pages_scraped": pages_scraped,
                    "terms": all_terms,
                    "error": "CAPTCHA detected. Try again later or reduce request frequency.",
                }
            print("CAPTCHA on subsequent page, stopping.", file=sys.stderr)
            break

        terms = extract_from_page(html)
        for i, term in enumerate(terms):
            if term.lower() in seen:
                continue
            seen.add(term.lower())
            all_terms.append({"term": term, "page": page_num, "position": i + 1})
        pages_scraped += 1

    return {
        "query": query,
        "total_terms": len(all_terms),
        "pages_scraped": pages_scraped,
        "terms": all_terms,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Browser-based extraction (Playwright)
# ---------------------------------------------------------------------------


def _start_xvfb():
    """Start Xvfb on a free display and return (process, display_string)."""
    for display_num in range(99, 120):
        display = f":{display_num}"
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=0.5)
            continue
        except subprocess.TimeoutExpired:
            return proc, display
    return None, None


class _XvfbManager:
    """Manage Xvfb lifecycle — can be started/stopped multiple times for headless switching."""

    def __init__(self):
        self.proc = None
        self._original_display = os.environ.get("DISPLAY")
        self.available = sys.platform not in ("darwin", "win32") and shutil.which("Xvfb") is not None

    def start(self):
        """Start Xvfb if available. Returns True if a virtual display is running."""
        if not self.available or self.proc is not None:
            return self.proc is not None
        self.proc, display = _start_xvfb()
        if self.proc:
            os.environ["DISPLAY"] = display
            return True
        return False

    def stop(self):
        """Terminate Xvfb and restore the original DISPLAY."""
        if self.proc is None:
            return
        self.proc.terminate()
        self.proc.wait()
        self.proc = None
        if self._original_display is not None:
            os.environ["DISPLAY"] = self._original_display
        elif "DISPLAY" in os.environ:
            del os.environ["DISPLAY"]

    @property
    def running(self):
        return self.proc is not None


def _detect_browser(pref):
    """Detect a browser based on preference string.
    Returns dict: {type, channel, executable, app_name}
    """
    def _try_chrome():
        for name in ("google-chrome", "google-chrome-stable"):
            if shutil.which(name):
                return {"type": "chromium", "channel": "chrome", "executable": None, "app_name": "Google Chrome"}
        if sys.platform == "win32":
            if shutil.which("chrome"):
                return {"type": "chromium", "channel": "chrome", "executable": None, "app_name": "Google Chrome"}
            for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                base = os.environ.get(env_var)
                if not base:
                    continue
                candidate = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
                if os.path.isfile(candidate):
                    return {"type": "chromium", "channel": None, "executable": candidate, "app_name": "Google Chrome"}
        if os.path.exists("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
            return {"type": "chromium", "channel": "chrome", "executable": None, "app_name": "Google Chrome"}
        return None

    def _try_brave():
        for name in ("brave-browser", "brave", "brave-browser-stable"):
            path = shutil.which(name)
            if path:
                return {"type": "chromium", "channel": None, "executable": path, "app_name": "Brave Browser"}
        if sys.platform == "win32":
            for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                base = os.environ.get(env_var)
                if not base:
                    continue
                candidate = os.path.join(base, "BraveSoftware", "Brave-Browser", "Application", "brave.exe")
                if os.path.isfile(candidate):
                    return {"type": "chromium", "channel": None, "executable": candidate, "app_name": "Brave Browser"}
        brave_mac = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
        if os.path.exists(brave_mac):
            return {"type": "chromium", "channel": None, "executable": brave_mac, "app_name": "Brave Browser"}
        return None

    def _try_firefox():
        for name in ("firefox", "firefox-esr"):
            path = shutil.which(name)
            if path:
                return {"type": "firefox", "channel": None, "executable": path, "app_name": "Firefox"}
        if sys.platform == "win32":
            for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
                base = os.environ.get(env_var)
                if not base:
                    continue
                candidate = os.path.join(base, "Mozilla Firefox", "firefox.exe")
                if os.path.isfile(candidate):
                    return {"type": "firefox", "channel": None, "executable": candidate, "app_name": "Firefox"}
        ff_mac = "/Applications/Firefox.app/Contents/MacOS/firefox"
        if os.path.exists(ff_mac):
            return {"type": "firefox", "channel": None, "executable": ff_mac, "app_name": "Firefox"}
        return None

    def _bundled():
        return {"type": "chromium", "channel": None, "executable": None, "app_name": "Chromium"}

    if pref == "chrome":   return _try_chrome()   or _bundled()
    if pref == "brave":    return _try_brave()    or _bundled()
    if pref == "chromium": return _bundled()
    if pref == "firefox":
        found = _try_firefox()
        if not found:
            print("error: Firefox not found", file=sys.stderr)
            sys.exit(1)
        return found
    # auto: Chrome > Brave > Firefox > bundled Chromium
    return _try_chrome() or _try_brave() or _try_firefox() or _bundled()


async def extract_bold_terms_batch(queries, pages, delay, hl, gl, debug=False, on_query_start=None, on_term=None, verbose=False, profile_dir=None, browser_info=None, save_html_dir=None, concurrency=3, headless_switch=True):
    """Launch browser once, scrape all queries, return list of result dicts."""
    xvfb_mgr = _XvfbManager()

    # On macOS/Windows a real display is always available — run headed without Xvfb.
    # On Linux: start Xvfb virtual display so the browser can run headed.
    headed = True
    if sys.platform not in ("darwin", "win32"):
        if xvfb_mgr.available:
            xvfb_mgr.start()
            headed = xvfb_mgr.running
        else:
            headed = False

    try:
        return await _run_extraction_batch(queries, pages, delay, hl, gl, headed, debug, on_query_start=on_query_start, on_term=on_term, verbose=verbose, profile_dir=profile_dir, browser_info=browser_info, save_html_dir=save_html_dir, concurrency=concurrency, headless_switch=headless_switch, xvfb_mgr=xvfb_mgr)
    finally:
        xvfb_mgr.stop()


def _log(msg, t0, verbose):
    if verbose:
        print(f"[+{time.perf_counter() - t0:.2f}s] {msg}", file=sys.stderr, flush=True)


def _macos_get_frontmost():
    """Return the name of the currently focused macOS app."""
    try:
        r = subprocess.run(
            ["osascript", "-e",
             "tell application \"System Events\" to get name of first process whose frontmost is true"],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip() or None
    except Exception:
        return None


def _macos_activate(app_name):
    """Bring a macOS app to the foreground by name."""
    if not app_name:
        return
    try:
        subprocess.run(
            ["osascript", "-e", f"tell application \"{app_name}\" to activate"],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass


async def _launch_browser(profile_dir, browser_info, headed, verbose, t0):
    """Launch a persistent browser context.

    Returns (pw_context_manager, context) — caller must close both.
    """
    is_chromium = browser_info["type"] == "chromium"
    _log(f'launching {browser_info["app_name"]} ({"headed" if headed else "headless"})', t0, verbose)

    if is_chromium:
        if sys.platform == "win32":
            _nav_platform = "Win32"
        elif sys.platform == "darwin":
            _nav_platform = "MacIntel"
        else:
            _nav_platform = "Linux x86_64"
        _stealth = Stealth(navigator_platform_override=_nav_platform)
        pw_cm = _stealth.use_async(async_playwright())
    else:
        _stealth = None
        pw_cm = async_playwright()
    p = await pw_cm.__aenter__()

    os.makedirs(profile_dir, exist_ok=True)

    if is_chromium:
        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--window-size=1920,1080",
        ]
        persistent_kwargs = {
            "headless": not headed,
            "args": browser_args,
            "user_agent": USER_AGENT,
            "viewport": {"width": 1920, "height": 1080},
            "screen": {"width": 1920, "height": 1080},
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "color_scheme": "light",
            "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
        }
        if browser_info["channel"]:
            persistent_kwargs["channel"] = browser_info["channel"]
        elif browser_info["executable"]:
            persistent_kwargs["executable_path"] = browser_info["executable"]
        context = await p.chromium.launch_persistent_context(profile_dir, **persistent_kwargs)
        await _stealth.apply_stealth_async(context)
    else:  # Firefox
        persistent_kwargs = {
            "headless": not headed,
            "viewport": {"width": 1920, "height": 1080},
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "color_scheme": "light",
            "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
        }
        if browser_info["executable"]:
            persistent_kwargs["executable_path"] = browser_info["executable"]
        context = await p.firefox.launch_persistent_context(profile_dir, **persistent_kwargs)

    _log("browser launched", t0, verbose)
    return pw_cm, context


async def _close_browser(context, pw_cm, verbose, t0):
    """Close browser context and stop Playwright."""
    _log("closing browser", t0, verbose)
    try:
        await asyncio.wait_for(context.close(), timeout=2.0)
    except (asyncio.TimeoutError, Exception):
        pass
    _log("browser closed — stopping Playwright", t0, verbose)
    try:
        await asyncio.wait_for(pw_cm.__aexit__(None, None, None), timeout=2.0)
    except (asyncio.TimeoutError, Exception):
        pass


async def _run_extraction_batch(queries, pages, delay, hl, gl, headed, debug=False, on_query_start=None, on_term=None, verbose=False, profile_dir=None, browser_info=None, save_html_dir=None, concurrency=3, headless_switch=True, xvfb_mgr=None):
    """Launch browser once, process all queries, return list of result dicts."""
    t0 = time.perf_counter()

    _pw_cm, context = await _launch_browser(profile_dir, browser_info, headed, verbose, t0)

    # Return focus to whatever was frontmost before the browser launched
    prior_app = _macos_get_frontmost() if sys.platform == "darwin" else None
    if prior_app:
        await asyncio.sleep(0.5)
        _macos_activate(prior_app)

    # Start with a single tab only — opening multiple tabs upfront triggers
    # aggressive CAPTCHA detection on unestablished sessions.
    first_page = context.pages[0] if context.pages else await context.new_page()

    # Warmup: navigate to google.com on first run to establish session.
    # Skip if profile already exists (session is still valid).
    session_exists = os.path.isdir(profile_dir) and bool(os.listdir(profile_dir))
    if not session_exists:
        await asyncio.sleep(random.uniform(0.5, 1.5))
        _log("navigating to google.com (first-run warmup)", t0, verbose)
        try:
            await first_page.goto("https://www.google.com", wait_until="domcontentloaded", timeout=30000)
        except (PlaywrightTimeout, Exception) as e:
            err = f"Failed to reach Google: {e}"
            await _close_browser(context, _pw_cm, verbose, t0)
            return [{"query": q, "total_terms": 0, "pages_scraped": 0, "terms": [], "error": err} for q in queries]
        _log("google.com loaded", t0, verbose)

    for _ in range(15):
        blocker = await detect_blockers(first_page)
        if blocker == "js_challenge":
            await asyncio.sleep(1)
            continue
        break

    app_name = browser_info["app_name"]
    multi = len(queries) > 1
    results = []

    # --- helper: switch from headed to headless (or back) ---
    async def _switch_browser(to_headed):
        nonlocal _pw_cm, context, headed
        if to_headed:
            print("[info] Switching to headed mode for CAPTCHA solving...", file=sys.stderr, flush=True)
        else:
            print("[info] Session established — switching to headless mode.", file=sys.stderr, flush=True)
        await _close_browser(context, _pw_cm, verbose, t0)
        if to_headed:
            if xvfb_mgr and not xvfb_mgr.running:
                xvfb_mgr.start()
        else:
            if xvfb_mgr and xvfb_mgr.running:
                xvfb_mgr.stop()
        await asyncio.sleep(0.5)  # allow profile lock release
        _pw_cm, context = await _launch_browser(profile_dir, browser_info, to_headed, verbose, t0)
        headed = to_headed
        return context.pages[0] if context.pages else await context.new_page()

    # --- helper: handle captcha_headless by falling back to headed ---
    async def _retry_with_headed(query):
        """Re-run a single query in headed mode after a headless CAPTCHA, then switch back."""
        nonlocal _pw_cm, context, headed
        _log("CAPTCHA in headless mode — falling back to headed", t0, verbose)
        page = await _switch_browser(to_headed=True)
        result = await _extract_single_query(page, query, pages, delay, hl, gl, app_name, True, debug, None, verbose, t0, save_html_dir=save_html_dir)
        # Switch back to headless for remaining queries
        await _switch_browser(to_headed=False)
        if result.get("error") == "captcha_headless":
            # Headed retry should never return this, but guard against it
            result["error"] = "CAPTCHA detected. Try again later or reduce request frequency."
        return result

    total_q = len(queries)

    def _print_query_result(idx, total, query, result):
        """Print completion status for a query."""
        n_terms = result.get("total_terms", 0)
        err = result.get("error")
        prefix = f'[{idx}/{total}] ' if total > 1 else ''
        if err and err != "captcha_headless":
            print(f'{prefix}"{query}" -> error: {err}', file=sys.stderr, flush=True)
        else:
            print(f'{prefix}"{query}" -> {n_terms} term{"s" if n_terms != 1 else ""} found', file=sys.stderr, flush=True)

    try:
        if concurrency == 1:
            # Sequential mode: preserve streaming output
            for idx, query in enumerate(queries):
                if on_query_start:
                    on_query_start(query)
                result = await _extract_single_query(first_page, query, pages, delay, hl, gl, app_name, headed, debug, on_term, verbose, t0, save_html_dir=save_html_dir)
                # Handle captcha_headless fallback
                if result.get("error") == "captcha_headless":
                    result = await _retry_with_headed(query)
                    first_page = context.pages[0] if context.pages else await context.new_page()
                    if on_term:
                        for entry in result.get("terms", []):
                            on_term(entry)
                _print_query_result(idx + 1, total_q, query, result)
                results.append(result)
                # After the first query, switch to headless if enabled
                if idx == 0 and headless_switch and len(queries) > 1 and headed:
                    _log("switching to headless mode", t0, verbose)
                    first_page = await _switch_browser(to_headed=False)
                    _log("headless browser ready", t0, verbose)
        else:
            # Parallel mode: query 1 runs alone on the single tab (handles any
            # CAPTCHA), then extra tabs are opened and queries 2-N run in parallel.
            first_result = await _extract_single_query(first_page, queries[0], pages, delay, hl, gl, app_name, headed, debug, None, verbose, t0, save_html_dir=save_html_dir)
            _print_query_result(1, total_q, queries[0], first_result)
            results = [first_result]

            if len(queries) > 1:
                # Switch to headless before opening extra tabs
                if headless_switch and headed:
                    _log("switching to headless mode", t0, verbose)
                    first_page = await _switch_browser(to_headed=False)
                    _log("headless browser ready", t0, verbose)

                remaining = queries[1:]
                done_count = 1  # first query already done

                n_tabs = min(concurrency, len(remaining))
                extra_tabs = [await context.new_page() for _ in range(n_tabs)]
                tab_pool = [first_page] + extra_tabs

                tab_q = asyncio.Queue()
                for tab in tab_pool:
                    await tab_q.put(tab)

                async def run_one(query):
                    nonlocal done_count
                    tab = await tab_q.get()
                    try:
                        r = await _extract_single_query(tab, query, pages, delay, hl, gl, app_name, headed, debug, None, verbose, t0, save_html_dir=save_html_dir)
                        done_count += 1
                        _print_query_result(done_count, total_q, query, r)
                        return r
                    except BaseException as exc:
                        done_count += 1
                        print(f'[{done_count}/{total_q}] "{query}" -> error: {exc}', file=sys.stderr, flush=True)
                        raise
                    finally:
                        await tab_q.put(tab)

                raw = await asyncio.gather(*[run_one(q) for q in remaining], return_exceptions=True)
                # Collect results, noting any captcha_headless failures for retry
                captcha_retries = []
                for q, r in zip(remaining, raw):
                    if isinstance(r, BaseException):
                        results.append({"query": q, "total_terms": 0, "pages_scraped": 0, "terms": [], "error": str(r)})
                    elif isinstance(r, dict) and r.get("error") == "captcha_headless":
                        captcha_retries.append((len(results), q))
                        results.append(r)  # placeholder
                    else:
                        results.append(r)

                # Retry captcha_headless queries one by one in headed mode
                if captcha_retries:
                    for result_idx, query in captcha_retries:
                        retry_result = await _retry_with_headed(query)
                        results[result_idx] = retry_result

            # Emit all buffered output in original query order
            for result in results:
                if multi and on_query_start:
                    on_query_start(result["query"])
                if on_term:
                    for entry in result.get("terms", []):
                        on_term(entry)
    finally:
        await _close_browser(context, _pw_cm, verbose, t0)
        _log("done", t0, verbose)

    # Sanitise internal sentinel — should never leak but guard against it
    for r in results:
        if isinstance(r, dict) and r.get("error") == "captcha_headless":
            r["error"] = "CAPTCHA detected. Try again later or reduce request frequency."

    return results


async def _extract_single_query(page, query, pages, delay, hl, gl, app_name, headed, debug, on_term, verbose, t0, save_html_dir=None):
    """Navigate to a search URL and extract <em> terms for one query. Returns result dict."""
    params = urlencode({"q": query, "hl": hl, "gl": gl})
    url = f"https://www.google.com/search?{params}"
    all_terms = []
    pages_scraped = 0
    seen = set()

    _log(f'navigating to search: "{query}"', t0, verbose)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except (PlaywrightTimeout, Exception) as e:
        return {"query": query, "total_terms": 0, "pages_scraped": 0, "terms": [], "error": f"Failed to load search results: {e}"}
    _log(f'search loaded: "{query}"', t0, verbose)

    for page_num in range(1, pages + 1):
        # Save debug HTML before blocker check so it's always captured
        if debug:
            html = await page.content()
            filename = f"debug_page_{page_num}.html"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"[debug] Saved {len(html)} bytes to {filename}", file=sys.stderr)
            print(f"[debug] Page URL: {page.url}", file=sys.stderr)
        else:
            html = None

        # Check for blockers, waiting through JS challenges
        blocker = await detect_blockers(page)
        if blocker == "js_challenge":
            for _ in range(10):
                await asyncio.sleep(1)
                blocker = await detect_blockers(page)
                if blocker != "js_challenge":
                    break
        if blocker == "captcha":
            if headed:
                _macos_activate(app_name)
                print(
                    "\nCAPTCHA detected — please solve it in the browser window. "
                    "Your session will be saved so future runs won't need this. "
                    "Continuing automatically once solved (timeout: 2 min).",
                    file=sys.stderr,
                )
                for _ in range(60):
                    await asyncio.sleep(2)
                    try:
                        if "/sorry/" not in page.url:
                            print("[info] CAPTCHA solved, resuming...", file=sys.stderr, flush=True)
                            _log("CAPTCHA solved", t0, verbose)
                            break
                    except Exception:
                        break
                else:
                    return {
                        "query": query,
                        "total_terms": len(all_terms),
                        "pages_scraped": pages_scraped,
                        "terms": all_terms,
                        "error": "CAPTCHA not solved within 2 minutes.",
                    }
                # Re-save debug HTML after CAPTCHA resolution
                if debug:
                    html = await page.content()
                    filename = f"debug_page_{page_num}.html"
                    with open(filename, "w", encoding="utf-8") as f:
                        f.write(html)
                    print(f"[debug] Post-CAPTCHA: saved {len(html)} bytes to {filename}", file=sys.stderr)
            else:
                return {
                    "query": query,
                    "total_terms": len(all_terms),
                    "pages_scraped": pages_scraped,
                    "terms": all_terms,
                    "error": "captcha_headless",
                }

        # Wait for rendered result snippets (<em> tags appear after JS renders)
        _log(f"page {page_num}: waiting for result snippets", t0, verbose)
        try:
            await page.wait_for_selector("#search em", timeout=30000)
        except PlaywrightTimeout:
            print(f"Timeout waiting for results on page {page_num}.", file=sys.stderr)
            break
        _log(f"page {page_num}: snippets ready", t0, verbose)

        if save_html_dir:
            _save_page_html(await page.content(), save_html_dir, query, page_num)

        if html is not None:
            # Debug mode already fetched full HTML — parse it directly.
            terms = extract_from_page(html)
        else:
            # Use a JS DOM query instead of fetching the full page HTML.
            # This avoids serialising ~1.7 MB of HTML over the CDP channel
            # and skips BeautifulSoup parsing entirely.
            _log(f"page {page_num}: running JS term extraction", t0, verbose)
            terms = await page.evaluate(
                "() => Array.from("
                "  document.querySelectorAll('#search em')"
                ").map(el => el.textContent.replace(/\\s+/g, ' ').trim()).filter(t => t.length > 0)"
            )
        _log(f"page {page_num}: {len(terms)} terms extracted", t0, verbose)
        for i, term in enumerate(terms):
            if term.lower() in seen:
                continue
            seen.add(term.lower())
            entry = {"term": term, "page": page_num, "position": i + 1}
            all_terms.append(entry)
            if on_term:
                on_term(entry)
        pages_scraped += 1

        if page_num < pages:
            # Try both the classic selector and the newer aria-label variant.
            next_link = page.locator("a#pnnext, a[aria-label='Next page'], a[aria-label='Next']")
            if await next_link.count() == 0:
                print("No more result pages available.", file=sys.stderr)
                break
            sleep_time = max(0.5, delay + random.uniform(-1.0, 1.0))
            _log(f"inter-page delay {sleep_time:.1f}s", t0, verbose)
            await asyncio.sleep(sleep_time)
            _log(f"navigating to page {page_num + 1}", t0, verbose)
            await next_link.first.click()
            await page.wait_for_load_state("domcontentloaded")
            _log(f"page {page_num + 1} loaded", t0, verbose)

    return {
        "query": query,
        "total_terms": len(all_terms),
        "pages_scraped": pages_scraped,
        "terms": all_terms,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    args = parse_args()

    if not args.query and not args.file:
        print("error: provide a query or --file FILE", file=sys.stderr)
        sys.exit(2)
    if args.query and args.file:
        print("error: provide either a query or --file FILE, not both", file=sys.stderr)
        sys.exit(2)

    if args.file:
        try:
            queries = load_queries_from_file(args.file)
        except OSError as e:
            print(f"error: cannot read file: {e}", file=sys.stderr)
            sys.exit(1)
        if not queries:
            print("error: file contains no queries", file=sys.stderr)
            sys.exit(1)
    else:
        queries = [args.query]

    multi = len(queries) > 1
    # In text mode, stream each term to stdout as it's scraped
    on_term = (lambda e: print(e["term"], flush=True)) if args.output == "text" else None
    on_query_start = None
    if multi and args.output == "text":
        on_query_start = lambda q: print(f'\n=== Query: "{q}" ===', flush=True)

    if args.http:
        all_results = []
        for query in queries:
            if on_query_start:
                on_query_start(query)
            all_results.append(
                extract_bold_terms_http(query, args.pages, args.delay, args.hl, args.gl, debug=args.debug, save_html_dir=args.save_html)
            )
    else:
        browser_info = _detect_browser(args.browser)
        # Use a browser-specific profile dir unless the user explicitly overrode it
        if args.profile_dir == DEFAULT_PROFILE_DIR and browser_info["type"] == "firefox":
            profile_dir = os.path.join(os.path.expanduser("~"), ".serp-bold-extractor", "profile-firefox")
        else:
            profile_dir = args.profile_dir
        all_results = asyncio.run(
            extract_bold_terms_batch(
                queries, args.pages, args.delay, args.hl, args.gl,
                debug=args.debug, on_query_start=on_query_start, on_term=on_term,
                verbose=args.verbose, profile_dir=profile_dir, browser_info=browser_info,
                save_html_dir=args.save_html, concurrency=args.concurrency,
                headless_switch=not args.no_headless_switch,
            )
        )

    json_results = [] if args.output == "json" else None
    exit_code = 0

    for result in all_results:
        if result.get("error"):
            print(result["error"], file=sys.stderr)
            exit_code = 1
            if json_results is not None:
                json_results.append(result)
            continue

        if not result["terms"]:
            print(f'No bold terms found for "{result["query"]}".', file=sys.stderr)
            if json_results is not None:
                json_results.append(result)
            continue

        if json_results is not None:
            json_results.append({
                "query": result["query"],
                "total_terms": result["total_terms"],
                "pages_scraped": result["pages_scraped"],
                "terms": result["terms"],
            })

    if json_results is not None:
        output = json_results if multi else (json_results[0] if json_results else {})
        print(json.dumps(output, indent=2, ensure_ascii=False))

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
