# SERP Bold Text Extractor

Extract bolded/emphasized terms (`<em>` tags) from Google Search result pages for semantic SEO analysis.

## Prerequisites

- Python 3.10+

## Installation

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
python serp_bold_extractor.py "your search query here"
```

### Examples

**Text output (default):**

```bash
python serp_bold_extractor.py "best cfd trading platform"
```

**JSON output:**

```bash
python serp_bold_extractor.py "best cfd trading platform" --output json
```

**Single page only:**

```bash
python serp_bold_extractor.py "best cfd trading platform" --pages 1
```

**HTTP mode (no browser, faster, use if you're getting CAPTCHAs):**

```bash
python serp_bold_extractor.py "best cfd trading platform" --http
```

## CLI Flags

| Flag       | Default | Description                                      |
|------------|---------|--------------------------------------------------|
| `--pages`  | 2       | Number of SERP pages to extract (1–5)            |
| `--delay`  | 3.0     | Base delay in seconds between page navigations   |
| `--hl`     | en      | Google `hl` parameter (interface language)        |
| `--gl`     | us      | Google `gl` parameter (geolocation)               |
| `--output` | text    | Output format: `text` (one per line) or `json`   |
| `--http`   | off     | Use plain HTTP requests instead of a browser      |

## JSON Output Format

```json
{
  "query": "the original query",
  "total_terms": 42,
  "pages_scraped": 2,
  "terms": [
    {"term": "example term", "page": 1, "position": 1},
    {"term": "another term", "page": 1, "position": 2}
  ]
}
```

## Exit Codes

| Code | Meaning                          |
|------|----------------------------------|
| 0    | Success (or no terms found)      |
| 1    | CAPTCHA or block detected        |

## Disclaimer

This tool is intended for low-volume, personal SEO research. Automated scraping may violate Google's Terms of Service. Use responsibly.
