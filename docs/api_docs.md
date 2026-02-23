# Web Scraper API Documentation

This API provides tools for searching search engines (Google, Bing) and extracting clean content from web pages.

## Base URL
The API is deployed at: `https://webscrapeer-0dd75353a1e5.herokuapp.com/`

## Authentication
Currently, no authentication is required for local/internal use.

## Endpoints

### 1. Search Engine Results
Perform a high-fidelity search on Google or Bing. This endpoint uses a real browser instance to avoid bot detection.

- **URL**: `/api/search`
- **Method**: `POST`
- **Curl Example**:
  ```bash
  curl -X POST https://webscrapeer-0dd75353a1e5.herokuapp.com/api/search \
       -H "Content-Type: application/json" \
       -d '{"query": "best programming languages 2024", "engine": "google", "num": 10}'
  ```
- **Request Body**:
  ```json
  {
    "query": "best programming languages 2024",
    "engine": "google",
    "num": 10
  }
  ```
  - `query` (string, required): The search term.
  - `engine` (string, optional, default: "google"): "google", "bing", or "all".
  - `num` (int, optional, default: 10): Number of results to return.

- **Response Body**:
  ```json
  {
    "engine": "google",
    "organic_results": [...],
    "related_questions": [...],
    "knowledge_graph": {...}
  }
  ```

---

### 2. URL Scraper
Extract title, meta description, and clean text content from any public URL.

- **URL**: `/api/scrape`
- **Method**: `POST`
- **Curl Example**:
  ```bash
  curl -X POST https://webscrapeer-0dd75353a1e5.herokuapp.com/api/scrape \
       -H "Content-Type: application/json" \
       -d '{"url": "https://en.wikipedia.org/wiki/Web_scraping"}'
  ```
- **Request Body**:
  ```json
  {
    "url": "https://en.wikipedia.org/wiki/Web_scraping"
  }
  ```
  - `url` (string, required): The target website URL.

- **Response Body**:
  ```json
  {
    "content": {
      "url": "https://...",
      "title": "...",
      "content": ["paragraph 1", "paragraph 2", ...],
      "meta_description": "...",
      "word_count": 1250,
      "extracted_at": "2024-03-23T..."
    }
  }
  }
  ```

---

### 3. Health Check
Check the status of the API and the internal browser instance.

- **URL**: `/health`
- **Method**: `GET`
- **Curl Example**:
  ```bash
  curl http://localhost:8002/health
  ```
- **Response**:
  ```json
  {
    "status": "ok"
  }
  ```

## UI Interface
A built-in dashboard is available at the root (`/`) for interactive testing of these endpoints.
