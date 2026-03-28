# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python CLI tool that extracts bolded/emphasized terms (`<em>` and `<b>` tags) from Google SERPs for semantic SEO analysis. Supports two modes: browser-based (Playwright + stealth) and plain HTTP.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Running

```bash
# Single query (2 pages by default)
python serp_bold_extractor.py "search query"

# Batch mode from file
python serp_bold_extractor.py --file queries.txt

# Key flags
--pages N        # Pages to scrape (1–5, default 2)
--output json    # JSON output instead of plain text
--http           # Skip browser, use plain HTTP requests
--debug          # Save raw HTML to debug_page_N.html
--verbose        # Timestamped progress to stderr
--browser        # auto|chrome|brave|firefox|chromium
```

## Architecture

Single-file project: `serp_bold_extractor.py` (~776 lines). Two extraction paths:

### HTTP Mode (`--http`)
`extract_bold_terms_http()` → `_http_fetch()` → `detect_blockers_html()` → `extract_from_page()`

Faster but fails on CAPTCHA/JS challenges. Uses BeautifulSoup to parse `<em>` tags from raw HTML.

### Browser Mode (default)
`extract_bold_terms_batch()` → `_run_extraction_batch()` → `_extract_single_query()`

Launches a persistent Playwright browser context with stealth patches. Uses `querySelectorAll('#search em')` via JS (avoids serializing full HTML). Session persists across runs in `~/.serp-bold-extractor/profile`.

On Linux: starts Xvfb virtual display to avoid headless detection.
On macOS: manages window focus to restore foreground app after browser launch.

### Key functions
- `extract_from_page(html)` — BeautifulSoup HTML → `<em>` terms (HTTP mode)
- `detect_blockers(page)` — async Playwright CAPTCHA detection
- `detect_blockers_html(html, url)` — raw HTML CAPTCHA detection
- `_detect_browser(pref)` — find Chrome/Brave/Firefox or fall back to bundled Chromium
- `main()` — CLI entry point and orchestration

### Output
- **Text** (default): one term per line to stdout
- **JSON**: `{"query", "total_terms", "pages_scraped", "terms": [{"term", "page", "position"}]}`
- Batch mode: JSON array of result objects

### Exit codes: `0` success, `1` CAPTCHA/error, `2` bad arguments

## Session Management

Browser profile at `~/.serp-bold-extractor/profile` (~6 months validity). If CAPTCHA appears on first run, user solves it manually (2-minute timeout), then session is reused. Reset with `rm -rf ~/.serp-bold-extractor/profile`.
