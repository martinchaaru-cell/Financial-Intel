"""Production web server settings (Gunicorn). Used by the deployment run command:
    gunicorn -c gunicorn.conf.py app:app

One web process with many threads answers everyone at once; report reading runs in its own worker process
(job_runner.py), so a large upload never slows other users down. The web process stays single because the live
upload-progress stream and its results are kept in that process's memory. Tune with secrets if needed:
    WEB_THREADS (default 16)   requests answered at the same time
    EXTRACT_PROCESSES (default 1)   report-reading worker processes
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
worker_class = 'gthread'
workers = 1
threads = int(os.environ.get('WEB_THREADS', '16'))
timeout = 300            # a worker that stops answering for 5 minutes is restarted; uploads return straight away
graceful_timeout = 30
keepalive = 5
accesslog = '-'
errorlog = '-'
loglevel = 'info'
