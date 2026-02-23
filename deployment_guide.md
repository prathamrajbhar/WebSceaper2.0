# Deployment Guide: Web Scraper API

This guide provides instructions for deploying the Web Scraper API using Docker. This setup ensures all dependencies (including Google Chrome) are correctly configured for a production environment.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed.
- [Docker Compose](https://docs.docker.com/compose/install/) installed.

## Quick Start (Automated)

The easiest way to set up and deploy is using the provided `setup.sh` script. It handles cleanup, environment configuration, and Docker deployment in one go.

```bash
chmod +x setup.sh
./setup.sh
```

## 1. Environment Configuration

The application uses environment variables for configuration. A template is provided in `.env.production`.

1.  Copy `.env.production` to `.env`:
    ```bash
    cp .env.production .env
    ```
2.  Edit `.env` to set your desired port, log level, and proxies if needed.

## 2. Deploying with Docker Compose

Running the application is simple with Docker Compose:

```bash
# Build and start the container in detached mode
docker compose up -d --build
```

### Verification

Check if the service is running:
- **API Status**: [http://localhost:8002/health](http://localhost:8002/health)
- **Logs**: `docker compose logs -f`

## 3. API Usage

Once deployed, you can interact with the API:

### Search
```bash
curl -X POST http://localhost:8002/api/search \
     -H "Content-Type: application/json" \
     -d '{"query": "best pizza in NYC", "engine": "google", "num": 5}'
```

### Scrape URL
```bash
curl -X POST http://localhost:8002/api/scrape \
     -H "Content-Type: application/json" \
     -d '{"url": "https://example.com"}'
```

## 4. Maintenance and Monitoring

-   **Update Application**: Pull latest changes and run `docker compose up -d --build`.
-   **Resource Monitoring**: Use `docker stats` to monitor CPU and Memory usage of the Chrome instances.
-   **Clean Up**: To stop and remove the container: `docker compose down`.

## Production Tips

-   **Memory**: Chrome can be memory-intensive. Ensure your server has at least 2GB of RAM.
-   **Shm Size**: The `docker-compose.yml` includes `shm_size: '2gb'`. Do not reduce this significantly as Chrome needs it for stable operation.
-   **Proxies**: For high-volume scraping, always use the `PROXY_LIST` environment variable to avoid IP bans.
