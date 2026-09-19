# Financial Intelligence — production container
#
# Build:  docker build -t financial-intelligence .
# Run:    docker run -p 5000:5000 --env-file .env financial-intelligence
#
# Uses gunicorn instead of `python app.py` (Flask's dev server) for
# production: multi-worker, no debug reloader, handles concurrent
# requests properly (this app already relies on that for its SSE
# upload-progress stream — see the `threaded=True` note at the bottom
# of app.py).

FROM python:3.11-slim

# System libs needed by pdfplumber/pymupdf (PDF parsing) and psycopg2
# (Postgres driver) — mirrors the [nix] packages block in .replit.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo \
    libopenjp2-7 \
    zlib1g \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so this layer caches independently of app code
# changes — rebuilds are fast unless requirements.txt itself changed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .

ENV PORT=5000
EXPOSE 5000

# Matches the /api/system/status route already in app.py — no new
# endpoint needed. Fails the container health check if the process is
# up but can't reach its database.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5000/api/system/status', timeout=4).status==200 else 1)"

# 2 workers is a reasonable default for a small deployment; raise with
# --workers as load grows. --threads keeps the SSE upload-progress
# endpoint responsive within a worker, same reasoning as threaded=True
# in app.py's own dev-server invocation. --timeout 600: a multi-year
# PDF import (parse + match + extract + score) genuinely took 97s on
# Replit's dev container alone - confirmed on a real Equity Group
# filing - and a slower/shared free-tier CPU plus cold-start overhead
# pushed the same request past 300s on Render, so the previous 120s
# was killing the worker mid-request before it could send its response
# (surfaces to the client as a broken/non-JSON response, not a normal
# error). 600s gives real headroom without masking a genuinely stuck
# request forever.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "600", "app:app"]