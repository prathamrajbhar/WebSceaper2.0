#!/bin/bash

# Web Scraper API - Automation Setup Script
# This script handles cleanup, environment setup, and Docker deployment.

set -e # Exit on error

echo "🚀 Starting Web Scraper API Setup..."

# 1. Cleanup unnecessary files
echo "🧹 Cleaning up unnecessary files..."
FILES_TO_REMOVE=(
    "test_uc.py"
    "logs.txt"
    "yagooglesearch.py.log"
    "results.json"
)

for file in "${FILES_TO_REMOVE[@]}"; do
    if [ -f "$file" ]; then
        rm "$file"
        echo "   ✅ Removed $file"
    fi
done

if [ -d "__pycache__" ]; then
    rm -rf "__pycache__"
    echo "   ✅ Removed __pycache__"
fi

# 2. Environment Configuration
echo "⚙️ Setting up environment variables..."
if [ ! -f ".env" ]; then
    if [ -f ".env.production" ]; then
        cp .env.production .env
        echo "   ✅ Created .env from .env.production"
    elif [ -f ".env.example" ]; then
        cp .env.example .env
        echo "   ✅ Created .env from .env.example"
    else
        echo "   ⚠️ Warning: No .env template found. Creating a default one..."
        cat <<EOF > .env
HEADLESS=true
LOG_LEVEL=INFO
HOST=0.0.0.0
PORT=8002
EOF
    fi
else
    echo "   ℹ️ .env file already exists, skipping."
fi

# 3. Docker Deployment
echo "🐳 Deploying with Docker Compose..."
if command -v docker-compose &> /dev/null; then
    DOCKER_CMD="docker-compose"
elif docker compose version &> /dev/null; then
    DOCKER_CMD="docker compose"
else
    echo "❌ Error: Docker Compose not found. Please install it first."
    exit 1
fi

echo "   🏗️ Building and starting containers..."
$DOCKER_CMD up -d --build

echo ""
echo "✨ Setup complete!"
echo "🌐 API Health Check: http://localhost:8002/health"
echo "📜 View Logs: $DOCKER_CMD logs -f"
