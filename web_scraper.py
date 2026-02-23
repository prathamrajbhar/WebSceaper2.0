"""
Web Scraper — single-file, no FastAPI, no Docker.

Usage (CLI):
    python web_scraper.py search "python tutorials"                    # parallel (default)
    python web_scraper.py search "openai" --engine bing --num 5
    python web_scraper.py search "AI news" --engine duckduckgo --num 10
    python web_scraper.py search "AI news" --engine all --json > results.json
    python web_scraper.py scrape "https://example.com"

Supported engines:
    all         — DEFAULT: Searches Google + Bing + DuckDuckGo simultaneously;
                  returns the first engine that has results; others are cancelled.
    google      — Google Search  (CAPTCHA-fallback chain: direct → homepage → search-box → DDG)
    bing        — Bing Search    (direct hit, homepage-retry on failure)
    duckduckgo  — DuckDuckGo     (JS-rendered first, HTML-endpoint fallback)

Library usage:
    from web_scraper import WebScraper
    import asyncio

    async def main():
        s = WebScraper()

        # Parallel race — fastest/first engine wins:
        engine, organic, questions, kg = await s.search_parallel("openai", num=10)
        print(f"Won by: {engine}")

        # Single engine:
        await s.initialize()
        organic, questions, kg = await s.search("openai", engine="google", num=10)
        content = await s.scrape_url("https://example.com")
        await s.cleanup()

    asyncio.run(main())

.env / environment variables:
    HEADLESS=true              default true; false = show the browser window
    PROXY_LIST=http://...,...  comma-separated proxies (optional)
    LOG_LEVEL=INFO
"""

# ---------------------------------------------------------------------------
# Python 3.12 distutils compatibility patch (needed by undetected_chromedriver)
# ---------------------------------------------------------------------------
import sys
if sys.version_info >= (3, 12):
    try:
        import distutils  # noqa: F401
    except ImportError:
        try:
            import setuptools._distutils as _dist
            sys.modules.setdefault("distutils", _dist)
        except ImportError:
            pass  # setuptools not installed; user will see clear error from uc

# ---------------------------------------------------------------------------
# Standard library
# ---------------------------------------------------------------------------
import argparse
import asyncio
import base64
import json
import logging
import os
import random
import re
import threading
import time
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, urlparse

# ---------------------------------------------------------------------------
# Third-party
# ---------------------------------------------------------------------------
from dotenv import load_dotenv
load_dotenv()

from pydantic import BaseModel, Field
from bs4 import BeautifulSoup

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

try:
    from fake_useragent import UserAgent as _FakeUA
    _fake_ua_instance = _FakeUA()
except Exception:
    _fake_ua_instance = None

# ---------------------------------------------------------------------------
# Configuration (from .env or environment)
# ---------------------------------------------------------------------------
HEADLESS: bool = os.getenv("HEADLESS", "true").lower() == "true"
PROXY_LIST: List[str] = [p.strip() for p in os.getenv("PROXY_LIST", "").split(",") if p.strip()]
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# Serialises the undetected_chromedriver binary-patching step.
# When multiple uc.Chrome() calls race simultaneously the OS raises
# ETXTBSY (errno 26) because a second writer tries to patch the binary
# while a first Chrome process is already executing it.
_UC_PATCH_LOCK = threading.Lock()

_DELAY_RANGE   = (1.2, 2.5)   # used for URL scraping
_SEARCH_DELAY  = (1.0, 1.8)   # faster — used between search page loads
_TYPING_RANGE  = (0.05, 0.15)

_FALLBACK_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("scraper")
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("selenium").setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class OrganicResult(BaseModel):
    position: int
    title: str
    link: str
    snippet: str = ""
    displayed_link: Optional[str] = None


class RelatedQuestion(BaseModel):
    question: str
    snippet: Optional[str] = None
    link: Optional[str] = None


class KnowledgeGraph(BaseModel):
    title: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None


class ScrapedContent(BaseModel):
    url: str
    title: str
    content: List[str]
    meta_description: Optional[str] = None
    word_count: int = 0
    extracted_at: str = Field(default_factory=lambda: datetime.now().isoformat())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _random_ua() -> str:
    if _fake_ua_instance:
        try:
            return _fake_ua_instance.random
        except Exception:
            pass
    return random.choice(_FALLBACK_UA_POOL)


def _domain(url: str) -> str:
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return url


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    for junk in ("Skip to main content", "Accept cookies", "Cookie notice", "Privacy policy"):
        text = text.replace(junk, "")
    return text.strip()


def _decode_bing_redirect(url: str) -> Optional[str]:
    """Decode Bing click-tracking /ck/a URLs back to the actual target."""
    try:
        params = parse_qs(urlparse(url).query)
        if "u" in params:
            enc = params["u"][0]
            if enc.startswith("a1"):
                enc = enc[2:]
            dec = base64.urlsafe_b64decode(enc + "==").decode("utf-8", errors="ignore")
            if dec.startswith("http"):
                return dec
    except Exception:
        pass
    return None


def _sleep(search: bool = False):
    rng = _SEARCH_DELAY if search else _DELAY_RANGE
    time.sleep(random.uniform(*rng))


# ---------------------------------------------------------------------------
# WebScraper
# ---------------------------------------------------------------------------

class WebScraper:
    """
    Unified web scraper using undetected-chromedriver.
    Uses the **real browser** for both Google AND Bing searches,
    which avoids the bot-detection that breaks httpx-based Bing scraping.
    """

    def __init__(self):
        self.driver: Optional[uc.Chrome] = None
        self._temp_dir: Optional[str] = None
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._count = 0
        self._t0 = time.time()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def initialize(self):
        """Launch the Chrome browser in a background thread."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._pool, self._launch)
        log.info("Browser ready.")

    def _launch(self):
        opts = uc.ChromeOptions()

        if HEADLESS:
            opts.add_argument("--headless=new")

        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--disable-web-security")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_argument("--disable-default-apps")
        opts.add_argument("--disable-background-timer-throttling")
        opts.add_argument("--disable-backgrounding-occluded-windows")
        opts.add_argument("--disable-renderer-backgrounding")
        opts.add_argument("--disable-features=TranslateUI,BlinkGenPropertyTrees")
        opts.add_argument("--memory-pressure-off")
        opts.add_argument(f"--user-agent={_random_ua()}")

        if PROXY_LIST:
            proxy = random.choice(PROXY_LIST)
            if "@" in proxy:
                proto, rest = proxy.split("://", 1)
                _, server = rest.split("@", 1)
                opts.add_argument(f"--proxy-server={proto}://{server}")
            else:
                opts.add_argument(f"--proxy-server={proxy}")
            log.info(f"Using proxy: {proxy}")

        # Block images for speed
        opts.add_experimental_option("prefs", {
            "profile.default_content_setting_values": {
                "images": 2, "popups": 2, "geolocation": 2, "notifications": 2,
            }
        })

        # Eager strategy: don't wait for images/fonts — just DOM ready
        opts.page_load_strategy = "eager"

        # Acquire the global lock so only ONE uc.Chrome() patches the binary
        # at a time — prevents OSError ETXTBSY when launching in parallel.
        self._temp_dir = tempfile.mkdtemp(prefix="scraper_")
        
        # Heroku buildpack compatibility
        chrome_bin = os.getenv("GOOGLE_CHROME_BIN")
        
        with _UC_PATCH_LOCK:
            self.driver = uc.Chrome(
                options=opts,
                user_data_dir=self._temp_dir,
                version_main=None,
                use_subprocess=True,
                browser_executable_path=chrome_bin,
                headless=HEADLESS,
            )
        self.driver.implicitly_wait(8)
        self.driver.set_page_load_timeout(30)
        self._stealth()

    def _stealth(self):
        try:
            self.driver.execute_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
        except Exception:
            pass

    async def cleanup(self):
        """Quit the browser."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._pool, self._quit)

    def _quit(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        finally:
            if self._temp_dir:
                shutil.rmtree(self._temp_dir, ignore_errors=True)
                self._temp_dir = None
        self.driver = None

    async def is_ready(self) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._pool, self._health)

    def _health(self) -> bool:
        try:
            self.driver.execute_script("return 1;")
            return True
        except Exception:
            return False

    def _is_session_alive(self) -> bool:
        """
        Cheap synchronous ping to detect whether the Chrome process / WebDriver
        session is still running.  Returns False the moment Chrome has crashed
        or the WebDriver connection has been lost (ECONNREFUSED / RemoteDisconnected).
        """
        return self._health()

    # ── Public API ─────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        engine: str = "google",
        num: int = 10,
    ) -> Tuple[List[OrganicResult], List[RelatedQuestion], Optional[KnowledgeGraph]]:
        """
        Search a single engine and return results.
        Engine values: 'google', 'bing', 'duckduckgo'/'ddg', or 'all'.
        When engine='all' this delegates to search_parallel() and discards
        the winning-engine label, returning only (organic, questions, kg).
        """
        _e = engine.lower().strip()
        if _e == "all":
            _, organic, questions, kg = await self.search_parallel(query, num=num)
            return organic, questions, kg

        self._count += 1
        log.info(f"Search #{self._count}: '{query}'  engine={_e}  num={num}")
        loop = asyncio.get_event_loop()
        if _e == "bing":
            fn = self._bing_search
        elif _e in ("duckduckgo", "ddg"):
            fn = self._duckduckgo_search
        else:
            fn = self._google_search
        return await loop.run_in_executor(self._pool, fn, query, num)

    async def search_parallel(
        self,
        query: str,
        num: int = 10,
        engines: Optional[List[str]] = None,
        scrapers: Optional[List["WebScraper"]] = None,
    ) -> Tuple[str, List[OrganicResult], List[RelatedQuestion], Optional[KnowledgeGraph]]:
        """
        Race all engines simultaneously — each gets its own dedicated browser.
        Returns (winning_engine, organic_results, related_questions, knowledge_graph)
        as soon as the FIRST engine succeeds.  Remaining engines are cancelled and
        their browsers are cleaned up.

        Example::
            winning, organic, questions, kg = await scraper.search_parallel("openai")
            print(f"Won by: {winning}, {len(organic)} results")
        """
        if engines is None:
            engines = ["google", "bing", "duckduckgo"]

        log.info(
            f"Parallel search '{query}' — launching {len(engines)} browser(s): "
            + ", ".join(engines)
        )

        # Use provided scrapers or create new ones
        if scrapers is None:
            scrapers = [WebScraper() for _ in engines]
            # Pipeline: initialize each browser sequentially first to prevent
            # undetected_chromedriver from breaking other active drivers through
            # simultaneous patching or resource contention.
            for s in scrapers:
                await s.initialize()

        async def _one(
            scraper: "WebScraper", engine: str
        ) -> Tuple[str, List[OrganicResult], List[RelatedQuestion], Optional[KnowledgeGraph]]:
            organic, questions, kg = await scraper.search(query, engine=engine, num=num)
            if not organic:
                raise RuntimeError(f"'{engine}' returned 0 results")
            log.info(f"[{engine}] ✓ {len(organic)} results")
            return engine, organic, questions, kg

        tasks = [
            asyncio.create_task(_one(s, e))
            for s, e in zip(scrapers, engines)
        ]

        winner: Optional[
            Tuple[str, List[OrganicResult], List[RelatedQuestion], Optional[KnowledgeGraph]]
        ] = None
        failed: List[str] = []

        for fut in asyncio.as_completed(tasks):
            try:
                winner = await fut
                log.info(
                    f"First success: '{winner[0]}' — "
                    f"cancelling {len(tasks) - 1 - len(failed)} remaining task(s)"
                )
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                failed.append(str(exc))
                log.warning(f"Engine failed: {exc}")

        # Cancel every task that is still running
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        # Shut down every browser in parallel ONLY IF we created them here
        # If scrapers were passed in, we assume the caller manages their lifecycle.
        if scrapers is None:
            await asyncio.gather(*[s.cleanup() for s in scrapers], return_exceptions=True)

        if winner is not None:
            return winner

        log.error("All engines failed: " + " | ".join(failed))
        return "none", [], [], None

    async def scrape_url(self, url: str) -> Optional[ScrapedContent]:
        """Extract readable content from any URL."""
        self._count += 1
        log.info(f"Scrape #{self._count}: {url}")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._pool, self._scrape, url)

    # ── Google ─────────────────────────────────────────────────────────────

    def _google_search(
        self, query: str, num: int
    ) -> Tuple[List[OrganicResult], List[RelatedQuestion], Optional[KnowledgeGraph]]:
        enc = quote_plus(query)
        url = f"https://www.google.com/search?q={enc}&num={min(num, 20)}&hl=en&gl=us"

        # ── Attempt 1: Direct URL (fastest, no homepage visit) ──
        try:
            log.info(f"Google: direct search '{query}'")
            self.driver.get(url)
            _sleep(search=True)
            src = self.driver.page_source
            if "recaptcha" not in src.lower() and "unusual traffic" not in src.lower():
                soup = BeautifulSoup(src, "html.parser")
                organic = self._parse_google(soup, num)
                if organic:
                    log.info(f"Google: {len(organic)} results (direct)")
                    return organic, self._paa_google(soup), self._kg_google(soup)
        except Exception as e:
            log.warning(f"Google direct attempt failed: {e}")
            if not self._is_session_alive():
                raise RuntimeError(f"Google browser session died: {e}") from e

        # ── Attempt 2: Homepage visit first (handles consent banners) ──
        try:
            log.info("Google: retrying with homepage visit...")
            self.driver.get("https://www.google.com")
            _sleep(search=True)
            self._dismiss_consent()
            self.driver.get(url)
            _sleep(search=True)
            src = self.driver.page_source
            if "recaptcha" not in src.lower() and "unusual traffic" not in src.lower():
                soup = BeautifulSoup(src, "html.parser")
                organic = self._parse_google(soup, num)
                if organic:
                    log.info(f"Google: {len(organic)} results (homepage-first)")
                    return organic, self._paa_google(soup), self._kg_google(soup)
        except Exception as e:
            log.warning(f"Google homepage attempt failed: {e}")
            if not self._is_session_alive():
                raise RuntimeError(f"Google browser session died: {e}") from e

        # ── Attempt 3: Type into search box (bypasses some CAPTCHA checks) ──
        if self._is_session_alive():
            try:
                log.info("Google: trying search-box approach...")
                organic, questions, kg = self._google_via_box(query, num)
                if organic:
                    log.info(f"Google: {len(organic)} results (search-box)")
                    return organic, questions, kg
            except Exception as e:
                log.warning(f"Google search-box failed: {e}")
                if not self._is_session_alive():
                    raise RuntimeError(f"Google browser session died: {e}") from e

        # ── Attempt 4: DuckDuckGo HTML — no CAPTCHA, always works ──
        # Only attempt DDG fallback if the browser session is still alive;
        # a dead session would just produce another cascade of connection errors.
        if not self._is_session_alive():
            raise RuntimeError("Google browser session died before DDG fallback")
        log.warning("Google blocked — using DuckDuckGo fallback...")
        return self._duckduckgo_search(query, num)

    def _dismiss_consent(self):
        try:
            btn = WebDriverWait(self.driver, 4).until(
                EC.element_to_be_clickable(
                    (By.XPATH,
                     "//button[contains(.,'Accept') or contains(.,'I agree') or contains(.,'Agree')]")
                )
            )
            btn.click()
            time.sleep(1)
        except Exception:
            pass

    def _google_via_box(
        self, query: str, num: int
    ) -> Tuple[List[OrganicResult], List[RelatedQuestion], Optional[KnowledgeGraph]]:
        try:
            self.driver.get("https://www.google.com")
            _sleep(search=True)
            self._dismiss_consent()
            box = WebDriverWait(self.driver, 8).until(
                EC.presence_of_element_located((By.NAME, "q"))
            )
            box.clear()
            for ch in query:
                box.send_keys(ch)
                time.sleep(random.uniform(*_TYPING_RANGE))
            time.sleep(random.uniform(0.5, 1.0))
            box.submit()
            _sleep(search=True)
            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            return self._parse_google(soup, num), self._paa_google(soup), self._kg_google(soup)
        except Exception as e:
            log.error(f"Search-box fallback failed: {e}")
            return [], [], None

    def _duckduckgo_search(
        self, query: str, num: int
    ) -> Tuple[List[OrganicResult], List[RelatedQuestion], Optional[KnowledgeGraph]]:
        """
        Full DuckDuckGo search — tries the JS-rendered version first (richer
        results + People-Also-Ask), then falls back to the no-JS HTML endpoint
        which is immune to bot detection and always works.
        """
        # ── Attempt 1: JS-rendered DDG (better results + PAA) ──────────────
        try:
            organic, questions, kg = self._duckduckgo_js(query, num)
            if organic:
                log.info(f"DuckDuckGo JS: {len(organic)} results, {len(questions)} PAA")
                return organic, questions, kg
            log.warning("DuckDuckGo JS: 0 results — falling back to HTML endpoint...")
        except Exception as e:
            if not self._is_session_alive():
                raise RuntimeError(f"DuckDuckGo browser session died: {e}") from e
            log.warning(f"DuckDuckGo JS attempt failed: {e}")

        # ── Attempt 2: HTML endpoint (zero-JS, no CAPTCHA, ultra-reliable) ──
        try:
            enc = quote_plus(query)
            url = f"https://html.duckduckgo.com/html/?q={enc}&kl=us-en"
            log.info(f"DuckDuckGo HTML: searching '{query}'")
            self.driver.get(url)
            _sleep(search=True)
            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            results = self._parse_duckduckgo(soup, num)
            log.info(f"DuckDuckGo HTML: {len(results)} results")
            return results, [], None
        except Exception as e:
            if not self._is_session_alive():
                raise RuntimeError(f"DuckDuckGo browser session died: {e}") from e
            log.error(f"DuckDuckGo HTML fallback failed: {e}")
            return [], [], None

    def _duckduckgo_js(
        self, query: str, num: int
    ) -> Tuple[List[OrganicResult], List[RelatedQuestion], Optional[KnowledgeGraph]]:
        """JS-rendered DuckDuckGo — full SPA, returns richer results and PAA."""
        enc = quote_plus(query)
        url = f"https://duckduckgo.com/?q={enc}&kl=us-en&ia=web"
        log.info(f"DuckDuckGo JS: navigating to '{query}'")
        self.driver.get(url)
        _sleep(search=True)

        # Wait for any of the known result containers to appear
        try:
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR,
                     "[data-testid='result'], article[data-nrn='result'], .nrn-react-div")
                )
            )
        except TimeoutException:
            log.warning("DuckDuckGo JS: result containers slow — parsing anyway...")

        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        organic   = self._parse_duckduckgo_js(soup, num)
        questions = self._paa_duckduckgo(soup)
        return organic, questions, None

    def _parse_duckduckgo_js(self, soup: BeautifulSoup, limit: int) -> List[OrganicResult]:
        """Parse JS-rendered DuckDuckGo SPA results."""
        results: List[OrganicResult] = []
        seen_links: set = set()   # O(1) dedup
        pos = 1

        # DDG uses data-testid attributes in its React SPA
        selectors = [
            "[data-testid='result']",
            "article[data-nrn='result']",
            "li[data-layout='organic']",
            ".react-results--main li",
        ]

        containers: list = []
        for sel in selectors:
            containers = soup.select(sel)
            if containers:
                break

        for c in containers:
            if len(results) >= limit:
                break
            try:
                # Title + link
                a = (
                    c.select_one("[data-testid='result-title-a']")
                    or c.select_one("h2 a")
                    or c.select_one("a[href^='http']")
                )
                if not a:
                    continue
                title = a.get_text(strip=True)
                href  = a.get("href", "")
                if not href.startswith("http"):
                    continue
                if any(d in href.lower() for d in ("duckduckgo.com",)):
                    continue
                if href in seen_links:
                    continue

                # Snippet
                se = (
                    c.select_one("[data-result='snippet']")
                    or c.select_one("[data-testid='result-snippet']")
                    or c.select_one(".OgdwYG, .E2eLOJr")
                )
                snippet = re.sub(r"\s+", " ", se.get_text(strip=True)).strip() if se else ""

                seen_links.add(href)
                results.append(OrganicResult(
                    position=pos, title=title, link=href,
                    snippet=snippet[:350], displayed_link=_domain(href),
                ))
                pos += 1
            except Exception:
                continue

        return results

    def _paa_duckduckgo(self, soup: BeautifulSoup) -> List[RelatedQuestion]:
        """Extract People-Also-Ask / related questions from DuckDuckGo SPA."""
        out: List[RelatedQuestion] = []
        seen: set = set()
        selectors = [
            "[data-testid='related-searches'] a",
            ".related-searches__item a",
            ".module--related-searches a",
        ]
        for sel in selectors:
            for el in soup.select(sel):
                t = el.get_text(strip=True)
                # Only include items that look like proper questions
                if t and "?" in t and " " in t and len(t) > 10 and t not in seen:
                    out.append(RelatedQuestion(question=t))
                    seen.add(t)
        return out[:10]

    def _parse_duckduckgo(self, soup: BeautifulSoup, limit: int) -> List[OrganicResult]:
        """
        Parse DuckDuckGo *HTML* endpoint results (html.duckduckgo.com/html/).
        Handles both the classic CSS class names and any renamed variants.
        """
        from urllib.parse import unquote
        results: List[OrganicResult] = []
        seen_links: set = set()   # O(1) dedup
        pos = 1

        # Try multiple container selectors across DDG HTML versions
        containers = soup.select(".result.results_links, .result.results_links_deep, .web-result")
        if not containers:
            containers = soup.select(".result")
        if not containers:
            # Last-ditch: any <div> wrapping an <a class="result__a">
            containers = [a.find_parent("div") for a in soup.select("a.result__a") if a.find_parent("div")]

        for c in containers:
            if len(results) >= limit:
                break
            if c is None:
                continue
            try:
                # Title + link
                a = c.select_one("a.result__a, .result__a")
                if not a:
                    continue
                title = a.get_text(strip=True)
                if not title or len(title) < 4:
                    continue
                href = a.get("href", "")

                # DDG HTML wraps real URLs in /l/?uddg=<encoded> redirects
                if href.startswith("/l/") or "duckduckgo.com/l/" in href:
                    m = re.search(r"uddg=([^&]+)", href)
                    href = unquote(m.group(1)) if m else ""

                if not href.startswith("http"):
                    continue
                if href in seen_links:
                    continue

                # Snippet — try multiple selectors
                se = (
                    c.select_one(".result__snippet")
                    or c.select_one(".result__body")
                    or c.select_one(".result__intro")
                )
                snippet = re.sub(r"\s+", " ", se.get_text(strip=True)).strip() if se else ""

                # Displayed URL
                ue = c.select_one(".result__url")
                displayed = ue.get_text(strip=True) if ue else _domain(href)

                seen_links.add(href)
                results.append(OrganicResult(
                    position=pos, title=title, link=href,
                    snippet=snippet[:350], displayed_link=displayed,
                ))
                pos += 1
            except Exception:
                continue

        return results

    def _parse_google(self, soup: BeautifulSoup, limit: int) -> List[OrganicResult]:
        results: List[OrganicResult] = []
        seen_links: set = set()   # O(1) dedup
        pos = 1

        strategies = [
            (".MjjYud, .g, .hlcw0c", "h3, .LC20lb, .DKV0Md", ".VwiC3b, .s3v9rd, .aCOpRe"),
            (".g, .rc",               "h3",                   ".s, .st"),
            ("[data-ved]",            "h3",                   "span"),
        ]

        for containers_sel, title_sel, snippet_sel in strategies:
            for c in soup.select(containers_sel):
                if len(results) >= limit:
                    break
                try:
                    te = c.select_one(title_sel)
                    if not te:
                        continue
                    title = te.get_text(strip=True)
                    if not title or len(title) < 4:
                        continue

                    le = te.find_parent("a") or c.select_one("a[href^='http']")
                    if not le:
                        continue
                    href = le.get("href", "")
                    if not href.startswith("http"):
                        continue
                    if any(d in href.lower() for d in (
                        "google.com/search", "accounts.google", "google.com/url",
                    )):
                        continue
                    if href in seen_links:
                        continue

                    se = c.select_one(snippet_sel)
                    snippet = re.sub(r"\s+", " ", se.get_text(strip=True)).strip() if se else ""

                    seen_links.add(href)
                    results.append(OrganicResult(
                        position=pos, title=title, link=href,
                        snippet=snippet[:350], displayed_link=_domain(href),
                    ))
                    pos += 1
                except Exception:
                    continue
            if results:
                break

        # Aggressive fallback when no container matched
        if not results:
            results = self._google_fallback(soup, limit)

        return results

    def _google_fallback(self, soup: BeautifulSoup, limit: int) -> List[OrganicResult]:
        out: List[OrganicResult] = []
        seen_links: set = set()   # O(1) dedup
        pos = 1
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if not href.startswith("http"):
                continue
            if any(d in href.lower() for d in (
                "google.com", "accounts.google", "youtube.com/results",
            )):
                continue
            h3 = a.find("h3") or (a.find_parent() and a.find_parent().find("h3"))
            if not h3:
                continue
            title = h3.get_text(strip=True)
            if not title or len(title) < 8:
                continue
            if href in seen_links:
                continue
            seen_links.add(href)
            out.append(OrganicResult(position=pos, title=title, link=href,
                                     displayed_link=_domain(href)))
            pos += 1
            if pos > limit:
                break
        return out

    def _paa_google(self, soup: BeautifulSoup) -> List[RelatedQuestion]:
        out: List[RelatedQuestion] = []
        seen: set = set()
        selectors = [
            ".related-question-pair span",
            ".wQiwMc .iDjcJe",
            "[jsname='Cpkphb'] span",
            ".kno-ftr span",
        ]
        for sel in selectors:
            for el in soup.select(sel):
                t = el.get_text(strip=True)
                if t and t.endswith("?") and len(t) > 8 and t not in seen:
                    out.append(RelatedQuestion(question=t))
                    seen.add(t)
        return out[:10]

    def _kg_google(self, soup: BeautifulSoup) -> Optional[KnowledgeGraph]:
        for sel in (".kno-rdesc", ".I6TXqe", ".hgKElc"):
            box = soup.select_one(sel)
            if box:
                try:
                    te = soup.select_one(".qrShPb, .kno-ecr-pt, .SPZz6b")
                    de = box.select_one("span, div")
                    t  = te.get_text(strip=True) if te else None
                    d  = de.get_text(strip=True) if de else None
                    if t or d:
                        return KnowledgeGraph(title=t, description=d, type="knowledge_graph")
                except Exception:
                    pass
        return None

    # ── Bing ───────────────────────────────────────────────────────────────

    def _bing_search(
        self, query: str, num: int
    ) -> Tuple[List[OrganicResult], List[RelatedQuestion], Optional[KnowledgeGraph]]:
        """
        Uses the real browser for Bing (avoids httpx bot-detection).
        Goes directly to search URL — no homepage pre-visit for speed.
        """
        try:
            enc = quote_plus(query)
            # setlang + cc = enforce English results; avoid mixed-language pages
            url = f"https://www.bing.com/search?q={enc}&count={min(num, 20)}&mkt=en-US&setlang=en-US&cc=US&first=1"

            log.info(f"Bing: searching '{query}'")
            self.driver.get(url)
            _sleep(search=True)

            # Wait for organic results to render
            try:
                WebDriverWait(self.driver, 8).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".b_algo"))
                )
            except TimeoutException:
                log.warning("Bing .b_algo slow — parsing anyway...")

            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            organic   = self._parse_bing(soup, num)

            # If 0 results on direct hit, try homepage-first (cookie consent)
            if not organic:
                log.warning("Bing: 0 results on direct hit — retrying with homepage...")
                self.driver.get("https://www.bing.com")
                _sleep(search=True)
                self.driver.get(url)
                _sleep(search=True)
                try:
                    WebDriverWait(self.driver, 8).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".b_algo"))
                    )
                except TimeoutException:
                    pass
                soup    = BeautifulSoup(self.driver.page_source, "html.parser")
                organic = self._parse_bing(soup, num)

            questions = self._paa_bing(soup)
            kg        = self._kg_bing(soup)
            log.info(f"Bing: {len(organic)} results, {len(questions)} PAA")
            return organic, questions, kg

        except Exception as e:
            if not self._is_session_alive():
                raise RuntimeError(f"Bing browser session died: {e}") from e
            log.error(f"Bing search error: {e}")
            return [], [], None

    def _parse_bing(self, soup: BeautifulSoup, limit: int) -> List[OrganicResult]:
        results: List[OrganicResult] = []
        seen_links: set = set()   # O(1) dedup
        pos = 1

        containers = soup.select(".b_algo")
        if not containers:
            log.debug("No .b_algo containers — trying #b_results > li")
            containers = soup.select("#b_results > li")

        for c in containers:
            if len(results) >= limit:
                break
            try:
                a = c.select_one("h2 a") or c.select_one("a[href^='http']")
                if not a:
                    continue
                title = a.get_text(strip=True)
                href  = a.get("href", "")

                if not href.startswith("http"):
                    continue

                # Resolve Bing click-tracking redirects
                if "bing.com/ck/a" in href:
                    real = _decode_bing_redirect(href)
                    if real:
                        href = real

                if any(d in href.lower() for d in ("bing.com", "microsoft.com/en-us/bing")):
                    continue
                if href in seen_links:
                    continue

                # Snippet — try multiple selectors
                snippet = ""
                for ss in (".b_caption p", ".b_paractl", ".b_algoSlug", "p"):
                    se = c.select_one(ss)
                    if se:
                        snippet = se.get_text(strip=True)
                        break

                seen_links.add(href)
                results.append(OrganicResult(
                    position=pos, title=title, link=href,
                    snippet=snippet[:350], displayed_link=_domain(href),
                ))
                pos += 1
            except Exception:
                continue

        return results

    def _paa_bing(self, soup: BeautifulSoup) -> List[RelatedQuestion]:
        """
        Extract real People-Also-Ask questions from Bing.
        Deliberately excludes .b_rs (related searches) which produce
        space-stripped word-blobs, not real questions.
        """
        out: List[RelatedQuestion] = []
        seen: set = set()
        # Only selectors that contain real question sentences
        for sel in (".df_alsoasked a", ".b_ans .b_focusTextLarge",
                    "[data-tag='RelatedSearches.PeopleAlsoAsk'] a",
                    ".alsoAsk a", ".b_paa a", ".df_alsoask a"):
            for el in soup.select(sel):
                t = el.get_text(strip=True)
                # Must look like a real question (has words + spaces + ends with ?)
                if t and "?" in t and " " in t and len(t) > 10 and t not in seen:
                    out.append(RelatedQuestion(question=t))  # was missing — bug fix
                    seen.add(t)
        return out[:10]

    def _kg_bing(self, soup: BeautifulSoup) -> Optional[KnowledgeGraph]:
        # Titles that indicate a UI widget, not a real knowledge-graph entity
        _JUNK = {
            "searches you might like", "related searches",
            "people also search for", "explore further",
        }
        # Prefer entity panel first — it's the most specific
        for sel in (".b_entityTP", ".b_ans"):
            for box in soup.select(sel):
                try:
                    te = box.select_one(".b_entityTitle, h2")
                    de = box.select_one(".b_entitySubTypes, .b_snippet, p")
                    t  = te.get_text(strip=True) if te else None
                    d  = de.get_text(strip=True) if de else None
                    if t and t.lower().strip() in _JUNK:
                        continue  # skip widget panels
                    if t or d:
                        return KnowledgeGraph(title=t, description=d, type="answer_box")
                except Exception:
                    pass
        return None

    # ── URL scraping ───────────────────────────────────────────────────────

    def _scrape(self, url: str) -> Optional[ScrapedContent]:
        social = (
            "twitter.com", "x.com", "facebook.com", "instagram.com",
            "tiktok.com", "snapchat.com", "linkedin.com",
        )
        if any(s in url.lower() for s in social):
            log.warning(f"Skipping social media URL: {url}")
            return None
        try:
            _sleep()   # full delay for arbitrary URL scraping
            self.driver.get(url)
            _sleep()

            title = self.driver.title
            meta_desc = ""
            try:
                me = self.driver.find_element(By.CSS_SELECTOR, 'meta[name="description"]')
                meta_desc = me.get_attribute("content") or ""
            except Exception:
                pass

            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer",
                              "aside", "iframe", "noscript"]):
                tag.decompose()

            blocks: List[str] = []
            seen_blocks: set = set()  # O(1) dedup for scraped text blocks
            for sel in (
                "article", "main", "[role='main']", ".content",
                ".article-content", ".post-content", ".entry-content",
                ".article-body", "p",
            ):
                for el in soup.select(sel):
                    text = _clean_text(el.get_text(strip=True))
                    if text and len(text) > 50 and text not in seen_blocks:
                        seen_blocks.add(text)
                        blocks.append(text)

            if not blocks:
                for p in soup.find_all("p"):
                    text = _clean_text(p.get_text(strip=True))
                    if len(text) > 50 and text not in seen_blocks:
                        seen_blocks.add(text)
                        blocks.append(text)

            blocks = blocks[:25]
            if not blocks:
                log.warning(f"No content extracted from {url}")
                return None

            return ScrapedContent(
                url=url, title=title, content=blocks,
                meta_description=meta_desc,
                word_count=sum(len(b.split()) for b in blocks),
            )
        except Exception as e:
            log.error(f"scrape_url failed ({url}): {e}")
            return None

    # ── Misc ───────────────────────────────────────────────────────────────

    def uptime(self) -> float:
        return time.time() - self._t0


# ---------------------------------------------------------------------------
# CLI display helpers
# ---------------------------------------------------------------------------

def _show_search(organic, questions, kg, query: str, engine: str = ""):
    print(f"\n{'='*60}")
    print(f"Query  : {query}")
    if engine:
        print(f"Engine : {engine}")
    print(f"Results: {len(organic)}")
    print("=" * 60)

    if kg and (kg.title or kg.description):
        print("\n[Knowledge Graph]")
        if kg.title:       print(f"  Title : {kg.title}")
        if kg.description: print(f"  Desc  : {kg.description[:200]}")

    print("\n[Organic Results]")
    for r in organic:
        print(f"\n  [{r.position}] {r.title}")
        print(f"       {r.link}")
        if r.snippet:
            s = r.snippet[:160] + "..." if len(r.snippet) > 160 else r.snippet
            print(f"       {s}")

    if questions:
        print("\n[People Also Ask]")
        for q in questions[:5]:
            print(f"  · {q.question}")
    print()


def _show_scrape(c: ScrapedContent):
    print(f"\n{'='*60}")
    print(f"URL   : {c.url}")
    print(f"Title : {c.title}")
    print(f"Words : {c.word_count}")
    if c.meta_description:
        print(f"Meta  : {c.meta_description[:150]}")
    print("\n[Content — first 5 blocks]")
    for para in c.content[:5]:
        print(f"  {para[:200]}")
    print()


# ---------------------------------------------------------------------------
# Async runners
# ---------------------------------------------------------------------------

async def _search_cmd(query: str, engine: str, num: int, as_json: bool):
    s = WebScraper()

    if engine == "all":
        print(
            f"Launching 3 browsers in parallel (headless={HEADLESS}) — "
            "first engine to succeed wins..."
        )
        winning_engine, organic, questions, kg = await s.search_parallel(query, num=num)
        if as_json:
            print(json.dumps({
                "engine":            winning_engine,
                "organic_results":   [r.model_dump() for r in organic],
                "related_questions": [q.model_dump() for q in questions],
                "knowledge_graph":   kg.model_dump() if kg else None,
            }, indent=2))
        else:
            _show_search(organic, questions, kg, query, engine=winning_engine)
        return                          # browsers already cleaned up inside search_parallel

    print(f"Initializing browser (headless={HEADLESS})...")
    await s.initialize()
    try:
        organic, questions, kg = await s.search(query, engine=engine, num=num)
        if as_json:
            print(json.dumps({
                "engine":            engine,
                "organic_results":   [r.model_dump() for r in organic],
                "related_questions": [q.model_dump() for q in questions],
                "knowledge_graph":   kg.model_dump() if kg else None,
            }, indent=2))
        else:
            _show_search(organic, questions, kg, query, engine=engine)
    finally:
        await s.cleanup()


async def _scrape_cmd(url: str, as_json: bool):
    s = WebScraper()
    print(f"Initializing browser (headless={HEADLESS})...")
    await s.initialize()
    try:
        content = await s.scrape_url(url)
        if content:
            if as_json:
                print(json.dumps(content.model_dump(), indent=2))
            else:
                _show_scrape(content)
        else:
            print(f"ERROR: Failed to scrape {url}", file=sys.stderr)
            sys.exit(1)
    finally:
        await s.cleanup()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Web Scraper — Google/Bing search + URL content extraction",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python web_scraper.py search "python tutorials"                   # all engines (default)
  python web_scraper.py search "openai" --engine google --num 5
  python web_scraper.py search "openai" --engine bing --num 5
  python web_scraper.py search "AI news" --engine duckduckgo --num 10
  python web_scraper.py search "AI news" --engine all --json > results.json
  python web_scraper.py scrape "https://example.com"
  python web_scraper.py scrape "https://realpython.com" --json > page.json

Engines:
  all         DEFAULT — launches Google + Bing + DuckDuckGo simultaneously;
              returns whichever engine answers first; others are cancelled.
  google      Google Search (CAPTCHA-resilient fallback chain)
  bing        Bing Search
  duckduckgo  DuckDuckGo (JS-first, no CAPTCHA HTML fallback)

.env variables:
  HEADLESS=true            Run browser headlessly (default)
  PROXY_LIST=http://...    Comma-separated proxy list
  LOG_LEVEL=INFO
        """,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="Search Google, Bing, DuckDuckGo, or all in parallel")
    sp.add_argument("query",            help="Search query")
    sp.add_argument(
        "--engine",
        default="all",
        choices=["all", "google", "bing", "duckduckgo"],
        help=(
            "Search engine to use. "
            "'all' (default) launches all three simultaneously and returns "
            "whichever responds first."
        ),
    )
    sp.add_argument("--num",  type=int, default=10)
    sp.add_argument("--json", action="store_true", help="Output raw JSON")

    sc = sub.add_parser("scrape", help="Scrape content from a URL")
    sc.add_argument("url",              help="Target URL")
    sc.add_argument("--json", action="store_true", help="Output raw JSON")

    args = parser.parse_args()

    if args.cmd == "search":
        asyncio.run(_search_cmd(args.query, args.engine, args.num, args.json))
    elif args.cmd == "scrape":
        asyncio.run(_scrape_cmd(args.url, args.json))


if __name__ == "__main__":
    main()
