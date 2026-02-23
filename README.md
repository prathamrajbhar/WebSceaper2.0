# Web Scraper

A **single-file** Python web scraper — no FastAPI, no Docker.
Searches Google/Bing and scrapes URL content with advanced anti-detection via `undetected-chromedriver`.

## Files

```
Web-Scraping/
├── web_scraper.py   ← everything: models, scraper, CLI
├── requirements.txt
├── README.md
└── .env             (optional — create this yourself)
```

## Setup

### 1. Install Chrome
```bash
# Ubuntu / Debian
sudo apt install google-chrome-stable
```

### 2. Create & activate a virtual environment
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Python dependencies
```bash
pip install -r requirements.txt
```

> **Python 3.12 note**: `undetected-chromedriver` needs `distutils`.
> The patch is built into `web_scraper.py` automatically, but you still
> need `setuptools` installed (`pip install setuptools`).

### 4. (Optional) Create a `.env` file
```env
HEADLESS=true          # false = show Chrome window while scraping
PROXY_LIST=http://user:pass@host:port,http://host2:port2
LOG_LEVEL=INFO
```

---

## Usage

Always run with the venv active:
```bash
source .venv/bin/activate
```

### Search Google
```bash
python web_scraper.py search "python tutorials"
python web_scraper.py search "python tutorials" --num 5
```

### Search Bing
```bash
python web_scraper.py search "openai" --engine bing
python web_scraper.py search "openai" --engine bing --num 5
```

### Scrape a URL
```bash
python web_scraper.py scrape "https://example.com"
```

### Output as JSON (save to file)
```bash
python web_scraper.py search "machine learning" --json > results.json
python web_scraper.py scrape "https://realpython.com" --json > page.json
```

---

## Library usage

```python
import asyncio
from web_scraper import WebScraper

async def main():
    s = WebScraper()
    await s.initialize()

    # Search
    organic, questions, kg = await s.search("openai", engine="google", num=10)
    for r in organic:
        print(r.position, r.title, r.link)
    if kg:
        print("KG:", kg.title, kg.description)

    # Scrape a URL
    content = await s.scrape_url("https://realpython.com")
    if content:
        print(content.title, content.word_count, "words")

    await s.cleanup()

asyncio.run(main())
```

---

## Return types

| Object | Fields |
|---|---|
| `OrganicResult` | `position`, `title`, `link`, `snippet`, `displayed_link` |
| `RelatedQuestion` | `question`, `snippet`, `link` |
| `KnowledgeGraph` | `title`, `type`, `description` |
| `ScrapedContent` | `url`, `title`, `content` (list of paragraphs), `meta_description`, `word_count`, `extracted_at` |

---

## Tips

- Google may show a CAPTCHA on first run — the scraper automatically falls back to the search-box approach, then Bing if needed.
- Use `HEADLESS=false` in `.env` to watch the browser and debug CAPTCHA issues.
- Add proxies in `PROXY_LIST` for high-volume scraping.
- Bing uses the real browser (not httpx) — this fixes the "0 results" issue that occurs with plain HTTP requests.
