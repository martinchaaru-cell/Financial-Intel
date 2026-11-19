# Remuneration Practice — production container
#
# Build:  docker build -t remuneration-practice .
# Run:    docker run -p 5000:5000 --env-file .env remuneration-practice
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
    tesseract-ocr \
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

# Matches the /healthz route already in app.py — no new
# endpoint needed. Fails the container health check if the process is
# up but can't reach its database.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5000/healthz', timeout=4).status==200 else 1)"

# One web worker with threads, as required by in-memory progress tracking.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
