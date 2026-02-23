# Heroku Deployment Guide

This guide outlines the steps to successfully deploy the Web Scraper project to Heroku. It includes specific optimizations for running Headless Chrome and `undetected-chromedriver` on Heroku's infrastructure.

## Prerequisites

1.  **Heroku CLI**: Installed and authenticated (`heroku login`).
2.  **Git**: Initialized and connected to your Heroku app (`heroku git:remote -a your-app-name`).

## Core Configuration Details

The following files have been optimized for Heroku:

*   **`.python-version`**: Pinned to `3.12` to ensure stability.
*   **`requirements.txt`**: Includes production-ready dependencies (`gunicorn`, `uvicorn`, `fastapi`) and pinned versions for `lxml` and `pydantic`.
*   **`Procfile`**: Configured with `-w 1` worker. **Do not increase workers** unless you upgrade to a higher-tier Heroku Dyno, as each worker launches a heavy Chrome process.

## 1. Set Up Buildpacks

Your app requires specific buildpacks to run Chrome. The traditional `google-chrome` buildpack is EOL; use the modern `chrome-for-testing` replacement.

Run these commands in order:

```bash
# Clear old buildpacks to avoid conflicts
heroku buildpacks:clear

# Add the modern Chrome buildpack (provides the browser and driver)
heroku buildpacks:add --index 1 https://github.com/heroku/heroku-buildpack-chrome-for-testing.git

# Add the standard Python buildpack
heroku buildpacks:add --index 2 heroku/python
```

## 2. Shared Browser Optimization

The app is configured in `api.py` to use a **single shared browser instance**. This is crucial for:
*   **Memory Efficiency**: Staying within Heroku's 512MB RAM limit.
*   **Speed**: Faster response times as the browser stays open between requests.
*   **Stability**: Prevents "Text file busy" errors by avoiding concurrent patching of the driver.

## 3. Deploy

Push your changes to Heroku:

```bash
git add .
git commit -m "Configure for Heroku deployment"
git push heroku main
```

## 4. Verification

After the build finishes, verify the app status:

### Check Logs
```bash
heroku logs --tail
```
Wait for the line: `Web Scraper is warmed up and ready!`

### Health Check
Visit: `https://your-app-name.herokuapp.com/health`
It should return `{"status": "ok"}`.

## Troubleshooting

### App Crashed (Error H10 / R14)
If you see "Memory limit exceeded", ensure you only have **one** Gunicorn worker in your `Procfile`:
`web: gunicorn -w 1 ...`

### Binary Location Error
If you see "Binary Location Must be a String", ensure the `chrome-for-testing` buildpack is installed (see step 1).

### Text File Busy
This usually happens if multiple processes try to start the browser at the same time. Ensure workers are set to 1.
