#!/usr/bin/env python3
"""SERP Bold Text Extractor - Extract bolded terms from Google search results."""

import argparse
import asyncio
import json
import os
import random
import shutil
import subprocess
import sys
import time
from urllib.parse import urlencode, quote_plus
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout


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


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract bold (<em>) terms from Google search result pages."
    )
    parser.add_argument("query", help="The Google search query string")
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
    return parser.parse_args()


async def detect_blockers(page):
    """Check for CAPTCHAs and consent walls. Returns 'captcha', 'js_challenge', or None."""
    if "/sorry/" in page.url:
        return "captcha"

    if await page.locator("#captcha-form").count() > 0:
        return "captcha"

    try:
        body_text = await page.locator("body").inner_text(timeout=3000)
        lower_text = body_text.lower()
        if "unusual traffic" in lower_text:
            # Check if this is a JS challenge (not a real CAPTCHA) — the browser
            # can solve it if we give it time.
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
    for tag in container.find_all(["em", "b"]):
        text = tag.get_text(strip=True)
        if text:
            terms.append(text)
    return terms


# ---------------------------------------------------------------------------
# HTTP-only extraction (no browser)
# ---------------------------------------------------------------------------


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


def extract_bold_terms_http(query, pages, delay, hl, gl, debug=False):
    """Extract bold terms using plain HTTP requests (no browser)."""
    all_terms = []
    pages_scraped = 0

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


def _find_chrome_channel():
    """Return 'chrome' if system Google Chrome is installed, else None."""
    for name in ("google-chrome", "google-chrome-stable"):
        if shutil.which(name):
            return "chrome"
    # macOS
    if os.path.exists("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        return "chrome"
    return None


async def extract_bold_terms(query, pages, delay, hl, gl, debug=False):
    """Launch browser, scrape SERP pages, return structured results dict."""
    params = urlencode({"q": query, "hl": hl, "gl": gl})
    url = f"https://www.google.com/search?{params}"

    # Use headed mode with Xvfb virtual display to avoid headless detection
    use_xvfb = shutil.which("Xvfb") is not None
    xvfb_proc = None
    original_display = os.environ.get("DISPLAY")

    if use_xvfb:
        xvfb_proc, display = _start_xvfb()
        if xvfb_proc:
            os.environ["DISPLAY"] = display

    try:
        return await _run_extraction(query, pages, delay, hl, gl, url, use_xvfb and xvfb_proc is not None, debug)
    finally:
        if xvfb_proc:
            xvfb_proc.terminate()
            xvfb_proc.wait()
            if original_display is not None:
                os.environ["DISPLAY"] = original_display
            elif "DISPLAY" in os.environ:
                del os.environ["DISPLAY"]


async def _run_extraction(query, pages, delay, hl, gl, url, headed, debug=False):
    """Core extraction logic using Playwright."""
    async with async_playwright() as p:
        # Prefer system Chrome over Playwright's bundled Chromium — it has
        # fewer detectable automation artifacts.
        channel = _find_chrome_channel()

        launch_kwargs = {
            "headless": not headed,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--window-size=1920,1080",
            ],
        }
        if channel:
            launch_kwargs["channel"] = channel

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            screen={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="light",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await context.add_init_script("""
            // Hide webdriver property
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

            // Realistic plugins array (standard Chrome PDF plugins)
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const plugins = [
                        {name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1},
                        {name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1},
                        {name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1},
                    ];
                    plugins.length = 3;
                    return plugins;
                }
            });

            // Match languages to locale and Accept-Language header
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });

            // window.chrome must exist in real Chrome — but don't overwrite
            // the real object when running system Chrome via channel="chrome"
            if (!window.chrome) {
                window.chrome = {
                    runtime: {
                        connect: function() {},
                        sendMessage: function() {}
                    }
                };
            }

            // Notifications permission should return 'denied', not throw
            const originalQuery = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.query = (params) => {
                if (params.name === 'notifications') {
                    return Promise.resolve({state: 'denied', onchange: null});
                }
                return originalQuery(params);
            };

            // Realistic WebGL vendor/renderer
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 0x9245) return 'Google Inc. (NVIDIA)';
                if (parameter === 0x9246) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return getParameter.call(this, parameter);
            };
        """)

        page = await context.new_page()
        all_terms = []
        pages_scraped = 0

        for page_num in range(1, pages + 1):
            if page_num == 1:
                await asyncio.sleep(random.uniform(0.5, 1.5))
                try:
                    await page.goto("https://www.google.com", wait_until="networkidle", timeout=30000)
                except (PlaywrightTimeout, Exception) as e:
                    await browser.close()
                    return {
                        "query": query,
                        "total_terms": 0,
                        "pages_scraped": 0,
                        "terms": [],
                        "error": f"Failed to reach Google: {e}",
                    }

                # Wait for any JS challenge to resolve (up to 15s)
                for _ in range(15):
                    blocker = await detect_blockers(page)
                    if blocker == "js_challenge":
                        await asyncio.sleep(1)
                        continue
                    break

                await asyncio.sleep(random.uniform(1.0, 2.0))
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except (PlaywrightTimeout, Exception) as e:
                    await browser.close()
                    return {
                        "query": query,
                        "total_terms": 0,
                        "pages_scraped": 0,
                        "terms": [],
                        "error": f"Failed to load search results: {e}",
                    }

            # Check for blockers, waiting through JS challenges
            blocker = await detect_blockers(page)
            if blocker == "js_challenge":
                for _ in range(10):
                    await asyncio.sleep(1)
                    blocker = await detect_blockers(page)
                    if blocker != "js_challenge":
                        break
            if blocker == "captcha":
                await browser.close()
                return {
                    "query": query,
                    "total_terms": len(all_terms),
                    "pages_scraped": pages_scraped,
                    "terms": all_terms,
                    "error": "CAPTCHA detected. Try again later or reduce request frequency.",
                }

            try:
                await page.wait_for_selector("#search", timeout=15000)
            except PlaywrightTimeout:
                print(
                    f"Timeout waiting for results on page {page_num}.",
                    file=sys.stderr,
                )
                break

            html = await page.content()
            if debug:
                filename = f"debug_page_{page_num}.html"
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"[debug] Saved {len(html)} bytes to {filename}", file=sys.stderr)
                print(f"[debug] Page URL: {page.url}", file=sys.stderr)
            terms = extract_from_page(html)
            for i, term in enumerate(terms):
                all_terms.append({"term": term, "page": page_num, "position": i + 1})
            pages_scraped += 1

            if page_num < pages:
                next_link = page.locator("a#pnnext")
                if await next_link.count() == 0:
                    print("No more result pages available.", file=sys.stderr)
                    break
                sleep_time = max(0.5, delay + random.uniform(-1.0, 1.0))
                await asyncio.sleep(sleep_time)
                await next_link.click()
                await page.wait_for_load_state("domcontentloaded")

        await browser.close()

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

    if args.http:
        result = extract_bold_terms_http(
            args.query, args.pages, args.delay, args.hl, args.gl, debug=args.debug
        )
    else:
        result = asyncio.run(
            extract_bold_terms(args.query, args.pages, args.delay, args.hl, args.gl, debug=args.debug)
        )

    if result.get("error"):
        print(result["error"], file=sys.stderr)
        sys.exit(1)

    if not result["terms"]:
        print("No bold terms found for this query.", file=sys.stderr)

    if args.output == "json":
        output = {
            "query": result["query"],
            "total_terms": result["total_terms"],
            "pages_scraped": result["pages_scraped"],
            "terms": result["terms"],
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        if not result["terms"]:
            sys.exit(0)
        for entry in result["terms"]:
            print(entry["term"])


if __name__ == "__main__":
    main()
