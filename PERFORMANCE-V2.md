Performance V2 (incremental release)
- Versioned runtime caching and prebuilt gzip. When editing the runtime, regenerate static/app-runtime.js.gz and bump the version in the template, worker and compression.py together. Never cache authenticated HTML or API data in the service worker.
- Review drafts UI requests 25 rows, excluding PDF and result blobs from SQL. Legacy unpaginated endpoint remains for approve-all. Review polling every 15 seconds while active, pauses when hidden or detail open.
- Server-Timing includes application and SQL execution duration/count (does not include all database fetch/connection costs). Use Chrome Network > request > Timing and Headers. Compare against Replit console slow request logs.
- Full module split, distributed cache, precomputed historical analytics, production load testing and live database validation are not included. No schema migration required for this patch.
