import uuid
import os
import sys
import asyncio
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

# Import the standalone scraper module using the correct path if necessary
from web_scraper import WebScraper, OrganicResult, RelatedQuestion, KnowledgeGraph, ScrapedContent

# ---------------------------------------------------------------------------
# Global State for Persistent Browser
# ---------------------------------------------------------------------------
class ScraperPool:
    shared_scraper: Optional[WebScraper] = None
    # Single global lock to ensure sequential browser access
    lock: asyncio.Lock = asyncio.Lock()

pool = ScraperPool()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize a single shared browser sequentially on server startup
    print("Initializing Shared Web Scraper...")
    pool.shared_scraper = WebScraper()
    await pool.shared_scraper.initialize()
    
    print("Web Scraper is warmed up and ready!")
    yield

    # Cleanup on shutdown
    print("Shutting down Scraper...")
    if pool.shared_scraper:
        await pool.shared_scraper.cleanup()


app = FastAPI(
    title="Web Scraper API",
    description="API for scraping search engines and URLs using undetected_chromedriver",
    version="1.0.0",
    lifespan=lifespan
)

# Serve static files (CSS, JS)
app.mount("/static", StaticFiles(directory="static"), name="static")

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
        async with pool.lock:
            # Note: For 'all', we just use Google as default since we now have one browser
            target_engine = "google" if engine_requested == "all" else engine_requested
            
            organic, questions, kg = await pool.shared_scraper.search(
                request.query, 
                engine=target_engine, 
                num=request.num
            )
            
            return SearchResponse(
                engine=target_engine,
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
        async with pool.lock:
            content = await pool.shared_scraper.scrape_url(request.url)
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
@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("static/index.html", "r") as f:
        return f.read()

@app.get("/health")
def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    # Heroku provides the port via the PORT environment variable
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8002))
    uvicorn.run("api:app", host=host, port=port, reload=False)
