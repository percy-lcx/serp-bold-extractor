#!/usr/bin/env python3
"""SERP Bold Text Extractor - Extract bolded terms from Google search results."""

import argparse
import asyncio
import json
import random
import sys
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout


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
    return parser.parse_args()


async def detect_blockers(page):
    """Check for CAPTCHAs and consent walls. Returns 'captcha' or None."""
    # CAPTCHA: URL contains /sorry/
    if "/sorry/" in page.url:
        return "captcha"

    # CAPTCHA: #captcha-form exists
    if await page.locator("#captcha-form").count() > 0:
        return "captcha"

    # CAPTCHA: page mentions unusual traffic
    try:
        body_text = await page.locator("body").inner_text(timeout=3000)
        if "unusual traffic" in body_text.lower():
            return "captcha"
    except PlaywrightTimeout:
        pass

    # Consent wall: try to dismiss
    try:
        reject_btn = page.get_by_role("button", name="Reject all")
        if await reject_btn.count() > 0:
            await reject_btn.click()
            await asyncio.sleep(2)
            return None
    except Exception:
        pass

    try:
        accept_btn = page.get_by_role("button", name="Accept all")
        if await accept_btn.count() > 0:
            await accept_btn.click()
            await asyncio.sleep(2)
            return None
    except Exception:
        pass

    return None


def extract_from_page(html):
    """Extract bold term strings from SERP HTML. Returns list of strings."""
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(id="search")
    if not container:
        return []
    terms = []
    for em in container.find_all("em"):
        text = em.get_text(strip=True)
        if text:
            terms.append(text)
    return terms


async def extract_bold_terms(query, pages, delay, hl, gl):
    """Launch browser, scrape SERP pages, return structured results dict."""
    params = urlencode({"q": query, "hl": hl, "gl": gl})
    url = f"https://www.google.com/search?{params}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()
        all_terms = []
        pages_scraped = 0

        for page_num in range(1, pages + 1):
            # Navigate: first page via URL, subsequent pages via #pnnext click
            if page_num == 1:
                await page.goto(url, wait_until="domcontentloaded")

            # Check for blockers
            blocker = await detect_blockers(page)
            if blocker == "captcha":
                await browser.close()
                return {
                    "query": query,
                    "total_terms": len(all_terms),
                    "pages_scraped": pages_scraped,
                    "terms": all_terms,
                    "error": "CAPTCHA detected. Try again later or reduce request frequency.",
                }

            # Wait for search results
            try:
                await page.wait_for_selector("#search", timeout=15000)
            except PlaywrightTimeout:
                print(
                    f"Timeout waiting for results on page {page_num}.",
                    file=sys.stderr,
                )
                break

            # Extract terms
            html = await page.content()
            terms = extract_from_page(html)
            for i, term in enumerate(terms):
                all_terms.append({"term": term, "page": page_num, "position": i + 1})
            pages_scraped += 1

            # Navigate to next page if needed
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


def main():
    args = parse_args()
    result = asyncio.run(
        extract_bold_terms(args.query, args.pages, args.delay, args.hl, args.gl)
    )

    if result.get("error"):
        print(result["error"], file=sys.stderr)
        sys.exit(1)

    if not result["terms"]:
        print("No bold terms found for this query.", file=sys.stderr)
        sys.exit(0)

    if args.output == "json":
        output = {
            "query": result["query"],
            "total_terms": result["total_terms"],
            "pages_scraped": result["pages_scraped"],
            "terms": result["terms"],
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        for entry in result["terms"]:
            print(entry["term"])


if __name__ == "__main__":
    main()
