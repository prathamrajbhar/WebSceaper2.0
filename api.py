import uuid
import os
import sys
import asyncio
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel
import uvicorn

# Import the standalone scraper module using the correct path if necessary
from web_scraper import WebScraper, OrganicResult, RelatedQuestion, KnowledgeGraph, ScrapedContent

# ---------------------------------------------------------------------------
# Global State for Persistent Browsers
# ---------------------------------------------------------------------------
class ScraperPool:
    google_scraper: Optional[WebScraper] = None
    bing_scraper: Optional[WebScraper] = None
    ddg_scraper: Optional[WebScraper] = None

    # Locks to prevent concurrent API requests from typing in the same browser tab
    google_lock: asyncio.Lock = asyncio.Lock()
    bing_lock: asyncio.Lock = asyncio.Lock()
    ddg_lock: asyncio.Lock = asyncio.Lock()

    # Generic lock for URL scraping to not conflict with searching
    scrape_lock: asyncio.Lock = asyncio.Lock()
    scrape_engine: Optional[WebScraper] = None

pool = ScraperPool()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize all scrapers sequentially on server startup
    print("Initializing Google Scraper...")
    pool.google_scraper = WebScraper()
    await pool.google_scraper.initialize()

    print("Initializing Bing Scraper...")
    pool.bing_scraper = WebScraper()
    await pool.bing_scraper.initialize()
    
    print("Initializing DuckDuckGo Scraper...")
    pool.ddg_scraper = WebScraper()
    await pool.ddg_scraper.initialize()

    # Can just use duckduckgo for generic scraping URL jobs too
    pool.scrape_engine = pool.ddg_scraper

    print("All Scrapers are warmed up and ready!")
    yield

    # Cleanup on shutdown
    print("Shutting down Scrapers...")
    await pool.google_scraper.cleanup()
    await pool.bing_scraper.cleanup()
    await pool.ddg_scraper.cleanup()

app = FastAPI(
    title="Web Scraper API",
    description="API for scraping search engines and URLs using undetected_chromedriver",
    version="1.0.0",
    lifespan=lifespan
)

# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------
class SearchRequest(BaseModel):
    query: str
    engine: str = "all"
    num: int = 10

class SearchResponse(BaseModel):
    engine: str
    organic_results: List[OrganicResult]
    related_questions: List[RelatedQuestion]
    knowledge_graph: Optional[KnowledgeGraph] = None

class ScrapeRequest(BaseModel):
    url: str

class ScrapeResponse(BaseModel):
    content: ScrapedContent

# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/search", response_model=SearchResponse)
async def api_search(request: SearchRequest):
    engine_requested = request.engine.lower().strip()
    
    try:
        if engine_requested == "all":
            # For "all", we dispatch to all 3 but inside their respective locks
            async def run_google():
                async with pool.google_lock:
                    res = await pool.google_scraper.search(request.query, engine="google", num=request.num)
                    return ("google",) + res
            
            async def run_bing():
                async with pool.bing_lock:
                    res = await pool.bing_scraper.search(request.query, engine="bing", num=request.num)
                    return ("bing",) + res
            
            async def run_ddg():
                async with pool.ddg_lock:
                    res = await pool.ddg_scraper.search(request.query, engine="duckduckgo", num=request.num)
                    return ("duckduckgo",) + res

            tasks = [asyncio.create_task(run_google()), asyncio.create_task(run_bing()), asyncio.create_task(run_ddg())]
            
            winner = None
            for fut in asyncio.as_completed(tasks):
                try:
                    res = await fut
                    eng, organic, questions, kg = res
                    if organic:
                        winner = res
                        # Cancel remaining tasks
                        for t in tasks:
                            if not t.done():
                                t.cancel()
                        break
                except Exception:
                    pass
            
            if not winner:
                raise HTTPException(status_code=500, detail="All search engines failed.")

            winning_engine, organic, questions, kg = winner
            return SearchResponse(
                engine=winning_engine,
                organic_results=organic,
                related_questions=questions,
                knowledge_graph=kg
            )

        else:
            if engine_requested == "google":
                async with pool.google_lock:
                    organic, questions, kg = await pool.google_scraper.search(request.query, engine=engine_requested, num=request.num)
            elif engine_requested == "bing":
                async with pool.bing_lock:
                    organic, questions, kg = await pool.bing_scraper.search(request.query, engine=engine_requested, num=request.num)
            elif engine_requested in ("duckduckgo", "ddg"):
                async with pool.ddg_lock:
                    organic, questions, kg = await pool.ddg_scraper.search(request.query, engine=engine_requested, num=request.num)
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported engine: {engine_requested}")

            return SearchResponse(
                engine=engine_requested,
                organic_results=organic,
                related_questions=questions,
                knowledge_graph=kg
            )
            
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

@app.post("/api/scrape", response_model=ScrapeResponse)
async def api_scrape(request: ScrapeRequest):
    try:
        async with pool.scrape_lock:
            content = await pool.scrape_engine.scrape_url(request.url)
            if not content:
                raise HTTPException(status_code=404, detail="Failed to extract content from URL")
            return ScrapeResponse(content=content)
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Scrape failed: {str(e)}")

# ---------------------------------------------------------------------------
# App startup setup
# ---------------------------------------------------------------------------
@app.get("/health")
def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    # Heroku provides the port via the PORT environment variable
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8002))
    uvicorn.run("api:app", host=host, port=port, reload=False)
