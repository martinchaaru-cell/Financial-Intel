# Performance release

## Install
Use the complete archive over the existing source tree after backing up the project/database. No database schema or data is overwritten by the release itself. The optional index migration below uses the configured database; schedule it outside active uploads and back up first.

    pip install -r requirements.txt
    python -m unittest test_performance_cache -v
    flask --app app performance-indexes

The CLI creates two non-unique indexes if missing; PostgreSQL/SQLite may lock tables during creation. Run on the dedicated database, not a temporary local database. Do not increase Gunicorn workers yet; existing SSE/progress relies on process memory. WEB_THREADS can be tuned with load testing; keep PostgreSQL connections and available memory in mind.

## Changes
- Main JavaScript is now a separate versioned static asset with deferred execution; the chart library is also deferred. Preserve script order. Bump the JS query version and service worker cache name for subsequent releases.
- Admin navigation no longer calls /api/billing/me just to redraw the badge. Member billing status refreshes on demand, with a 30-second navigation debounce and identical in-flight requests coalesced. Authorization, session checks and live invoice routes remain server-side.
- Company report, directory and rank requests run in parallel once company selection is known. Duplicate concurrent admin analytics reads are coalesced. Stale company report responses are discarded.
- Admin-only, account-keyed, 20-second bounded response cache for eight analytics/list endpoints (128 entries/16 MiB); API responses are not stored in browser/service-worker caches. ORM mutations, successful write requests and local data-change events invalidate the cache. A separate extraction process or external database writes may be visible after TTL, not immediately. Set PERF_CACHE_ENABLED=0 to disable. Billing, account, activity and financial transactions are uncached.
- Historical trends fetches survey/company rows in a single batch instead of a query per year; ranking loads only the latest row per company; period picker eager loads used relationships. Distinct year query avoids hydrating every survey record. App shell bypasses unused public website statistics.
- Static files no longer perform account lookup solely for activity/session replacement checks; protected page/API checks remain. Service worker does not retain authenticated HTML; offline notice instead. Fixed JS/static assets cache per release.
- Server-Timing: app;dur=... and X-Performance-Cache: HIT/MISS on eligible routes; API requests >=750 ms logged using route patterns (not query strings). Timing excludes streaming transfer and browser/network overhead.

## Verify
In Chrome, uncheck Disable cache for warm-load tests. Reload, navigate each page twice, inspect Network > Timing and Server-Timing, and compare cold/warm durations. Verify admin/member permissions, session expiration, invoice actions, manual correction then refresh, PDF upload and progress, historical reports, and console errors. Compare p50/p95 across at least 20 requests per route. No measured performance claims are made by this offline release.

Future scaling: shared Redis cache/invalidation and rate limiter, separate durable queue and cross-process progress, load test with real data, and additional query plans/indexes after EXPLAIN. Existing single-worker constraint remains.
