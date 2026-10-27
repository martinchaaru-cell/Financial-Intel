"""
Batch fetch + scan pacer for Match Oracle.
=============================================================================
Problem this solves: fetch_match_on_demand() fires 4-5 sequential API calls
per match, each blocking inside APISession's own reactive token bucket.
Run that for 20 matches back-to-back and you get bursts that hit the
Highlightly rate limit, trigger 429s, and eat retry/backoff time on top -
which is where the 30-60s-per-match number was coming from.

This module doesn't change fetch_match_on_demand() or APISession at all.
It just controls WHEN each match's fetch starts, so calls arrive at a
steady, predictable rate instead of in bursts - the rate limiter inside
APISession should then rarely (ideally never) have to make a call wait or
retry.

Tune RATE_LIMIT_PER_MINUTE to match your actual Highlightly tier. When
you upgrade tiers, changing this one number is the only change needed -
everything else here scales automatically.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, List, Optional

logger = logging.getLogger("match_oracle.batch_scan")

# ── Tune this when your Highlightly tier changes ───────────────────────
# Reads the SAME env var (and default) as APISession.RATE_LIMIT_PER_MINUTE
# in app.py, so the pacer and the real limiter can never drift apart.
# (Can't import from app.py - app.py imports this module, so it would be
# circular.) Pro tier = 720 req/min. Change the default here and in
# app.py together, or just set API_RATE_LIMIT_PER_MINUTE in the environment.
RATE_LIMIT_PER_MINUTE = int(os.getenv("API_RATE_LIMIT_PER_MINUTE", "720"))

# Calls per match with the current fetch_match_on_demand() shape:
# match detail (1) + statistics (0-1, often free/embedded) + h2h (1) +
# odds (1) + standings (0-1, cached after the first match per league).
# Conservative estimate - use the higher end so pacing stays safe even
# on a cold cache / first match of a league.
CALLS_PER_MATCH_ESTIMATE = 5

# Seconds to wait between STARTING each match's fetch. Spreads calls
# evenly across the full minute instead of bursting 5 calls then idling.
SECONDS_PER_CALL = 60.0 / RATE_LIMIT_PER_MINUTE
SECONDS_PER_MATCH = SECONDS_PER_CALL * CALLS_PER_MATCH_ESTIMATE

# Parallel fetch workers. Only the NETWORK fetch runs in parallel; every
# scan still runs one at a time, in input order, after all fetches finish
# (see run_batch_fetch_and_scan docstring for why). Set to 1 to get the
# original fully-sequential, paced behavior. APISession's token bucket
# still enforces RATE_LIMIT_PER_MINUTE across all workers combined.
BATCH_WORKERS = max(1, int(os.getenv("BATCH_SCAN_WORKERS", "4")))


@dataclass
class MatchRunResult:
    match_id: int
    fetch_ok: bool
    fetch_seconds: float
    scan_seconds: Optional[float] = None
    verdict: Optional[str] = None
    confidence: Optional[str] = None
    selection: Optional[str] = None
    error: Optional[str] = None
    # Data-integrity warnings for this scan (e.g. "season history incomplete:
    # Babrungas 6/21 games", "fetch parts failed: h2h") - from scan_fn's row.
    integrity: List[str] = field(default_factory=list)


@dataclass
class BatchRunReport:
    results: List[MatchRunResult] = field(default_factory=list)
    total_seconds: float = 0.0

    def summary(self) -> str:
        ok = [r for r in self.results if r.fetch_ok]
        failed = [r for r in self.results if not r.fetch_ok]
        approved = [r for r in ok if r.verdict == "APPROVED"]
        lines = [
            f"Batch: {len(self.results)} matches, {len(ok)} fetched OK, {len(failed)} failed",
            f"Verdicts: {len(approved)} APPROVED / {len(ok)} scanned",
            f"Total wall time: {self.total_seconds:.1f}s ({self.total_seconds/60:.1f} min)",
        ]

        # Fetch vs scan breakdown - the actual diagnostic signal: if fetch
        # times are wildly uneven (some matches near seconds_per_match,
        # others way over), that's retries/429s, not pacing. Scan should
        # always be near-zero (~ms) - if it isn't, that's a real pipeline
        # regression worth investigating separately from fetch.
        fetch_times = [r.fetch_seconds for r in self.results if r.fetch_seconds]
        scan_times = [r.scan_seconds for r in ok if r.scan_seconds is not None]
        if fetch_times:
            lines.append(
                f"Fetch time  — min {min(fetch_times):.1f}s / "
                f"avg {sum(fetch_times)/len(fetch_times):.1f}s / "
                f"max {max(fetch_times):.1f}s"
            )
        if scan_times:
            lines.append(
                f"Scan time   — min {min(scan_times)*1000:.1f}ms / "
                f"avg {sum(scan_times)/len(scan_times)*1000:.1f}ms / "
                f"max {max(scan_times)*1000:.1f}ms"
            )

        if failed:
            lines.append("Failed match_ids: " + ", ".join(str(r.match_id) for r in failed))

        # Scans that ran on incomplete data. The verdict is stored, but it was
        # computed on partial inputs - backfill / refetch, then rescan.
        flagged = [r for r in ok if r.integrity]
        if flagged:
            lines.append(f"Integrity: {len(flagged)} of {len(ok)} scans ran on incomplete data")
            for r in flagged[:15]:
                lines.append(f"  match {r.match_id}: " + "; ".join(r.integrity))
            if len(flagged) > 15:
                lines.append(f"  ... and {len(flagged) - 15} more")
        return "\n".join(lines)


def _fmt_mmss(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"


def _run_sequential(
    match_ids: List[int],
    fetch_fn: Callable[[int], bool],
    scan_fn: Callable[[int], Optional[dict]],
    seconds_per_match: float = SECONDS_PER_MATCH,
    progress_callback: Optional[Callable[[str], None]] = None,
    run_scan_fn: Optional[Callable[[int], object]] = None,
) -> BatchRunReport:
    """
    Fetch and scan each match in match_ids, one at a time, paced so calls
    stay under the tier's per-minute rate limit without ever needing to
    hit APISession's reactive wait/retry path.

    Args:
        match_ids: list of match IDs to process, in order.
        fetch_fn: should be app.py's fetch_match_on_demand - takes a
            match_id, returns True/False. This already runs
            run_oracle_scan_for_match() internally at the end, so
            scan_fn below is typically a thin DB read of the verdict
            that was just saved, not a second real scan.
        scan_fn: takes a match_id, returns the saved verdict dict (or
            None) - e.g. a small wrapper around
            "SELECT verdict, confidence FROM oracle_verdicts WHERE
            match_id = ?". Kept separate from fetch_fn so this module
            has no direct dependency on run_scan()'s exact signature.
        seconds_per_match: pacing interval between the START of each
            match's fetch. Defaults to a safe estimate for the current
            RATE_LIMIT_PER_MINUTE/CALLS_PER_MATCH_ESTIMATE above.
        progress_callback: optional function called with a one-line
            status string after each match (fetch done, scan done, and
            when pacing is waiting). Pass `print` for live console output,
            or wire it to a UI/websocket update. Always also goes through
            the logger regardless of whether this is set, so logs stay
            complete even with no callback.

    Returns:
        BatchRunReport with per-match timing and verdicts.
    """
    def _emit(msg: str) -> None:
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    report = BatchRunReport()
    batch_start = time.perf_counter()
    total = len(match_ids)

    for i, match_id in enumerate(match_ids):
        match_start = time.perf_counter()
        elapsed_so_far = match_start - batch_start
        _emit(f"[{i+1}/{total}] match {match_id}: fetching... (elapsed {_fmt_mmss(elapsed_so_far)})")

        fetch_error = None
        try:
            fetch_ok = bool(fetch_fn(match_id))
        except Exception as e:
            logger.error(f"Fetch raised for match {match_id}: {e}")
            fetch_error = str(e)
            fetch_ok = False
        fetch_elapsed_recorded = time.perf_counter() - match_start
        if fetch_error:
            # (This used to append a result here AND again below, so a failed
            # fetch was counted twice in the summary.)
            _emit(f"[{i+1}/{total}] match {match_id}: fetch FAILED ({fetch_elapsed_recorded:.1f}s) — {fetch_error}")

        if fetch_ok:
            _emit(f"[{i+1}/{total}] match {match_id}: fetch OK ({fetch_elapsed_recorded:.1f}s), scanning...")

        result = MatchRunResult(match_id=match_id, fetch_ok=fetch_ok, fetch_seconds=fetch_elapsed_recorded,
                                error=fetch_error)

        if fetch_ok:
            scan_start = time.perf_counter()
            try:
                # fetch_fn used to be the only scan trigger here, and
                # fetch_match_on_demand() returns early WITHOUT scanning when
                # the match's details were already cached - so in this
                # sequential mode a cached match kept whatever verdict it had
                # (or none). The scan is now run explicitly when supplied.
                if run_scan_fn is not None:
                    run_scan_fn(match_id)
                verdict_row = scan_fn(match_id)
                if verdict_row:
                    result.verdict = verdict_row.get("verdict")
                    result.confidence = verdict_row.get("confidence")
                    result.selection = verdict_row.get("selection")
                    result.integrity = list(verdict_row.get("integrity") or [])
            except Exception as e:
                logger.error(f"Scan lookup raised for match {match_id}: {e}")
                result.error = str(e)
            result.scan_seconds = time.perf_counter() - scan_start

            elapsed_total = time.perf_counter() - batch_start
            _emit(
                f"[{i+1}/{total}] match {match_id}: done — "
                f"fetch {fetch_elapsed_recorded:.1f}s, scan {result.scan_seconds*1000:.0f}ms, "
                f"verdict={result.verdict or 'N/A'} "
                f"| total elapsed {_fmt_mmss(elapsed_total)}"
            )

        report.results.append(result)

        # Pace the NEXT match's start, not a flat sleep after every match -
        # if this match's fetch already took longer than seconds_per_match
        # (e.g. it hit a retry), don't double up on waiting; just proceed.
        is_last = (i == total - 1)
        if not is_last:
            remaining = seconds_per_match - fetch_elapsed_recorded
            if remaining > 0:
                _emit(f"[{i+1}/{total}] pacing: waiting {remaining:.1f}s before next match...")
                time.sleep(remaining)

    report.total_seconds = time.perf_counter() - batch_start
    _emit(f"Batch complete: {total} matches in {_fmt_mmss(report.total_seconds)}")
    return report


def run_batch_fetch_and_scan(
    match_ids: List[int],
    fetch_fn: Callable[[int], bool],
    scan_fn: Callable[[int], Optional[dict]],
    seconds_per_match: float = SECONDS_PER_MATCH,
    progress_callback: Optional[Callable[[str], None]] = None,
    max_workers: int = BATCH_WORKERS,
    fetch_only_fn: Optional[Callable[[int], bool]] = None,
    run_scan_fn: Optional[Callable[[int], object]] = None,
) -> BatchRunReport:
    """
    Dispatcher. With max_workers <= 1 (or if fetch_only_fn / run_scan_fn
    aren't supplied) this is the original sequential, paced behavior using
    fetch_fn (fetch + scan together) - unchanged.

    With max_workers > 1 it runs in TWO phases so scan integrity is
    identical to a sequential run:

      Phase 1 - PARALLEL, network only: fetch_only_fn(match_id) on a thread
        pool. This is the slow part (4-5 API calls per match). It writes
        only to that match's own match_details row (plus the shared,
        idempotent standings cache). No scan runs here.

      Phase 2 - SEQUENTIAL, in input order: run_scan_fn(match_id) for each
        match whose fetch succeeded, then scan_fn(match_id) to read the
        verdict back. Scans stay single-threaded on purpose: each one calls
        recompute_missing_elo() (a read-modify-write on team_ratings that
        double-applies Elo if two threads run it at once) and
        recompute_league_stats(), and reads Elo/league_stats/standings
        that those functions write. Serializing them means no scan can see
        another scan's half-written state.

    Duplicate match_ids are dropped (keeping first occurrence) - the
    per-match fetch lock would otherwise make the second copy report a
    spurious failure.

    Scan integrity notes (Oct 2026 review):
      * app.py's run_oracle_scan_for_match() now holds a process-wide scan
        lock, so phase 2 can't interleave with scans started elsewhere
        (Fetch All, a user opening a match, Rescan All) - serialising only
        this module's own scans wasn't enough to protect the Elo update.
      * app.py passes run_scan_fn with force=True: a batch exists to give a
        fresh read, and the old "skip if a verdict exists" meant a match
        scanned days earlier (or before its season was backfilled) kept its
        stale verdict while this report showed it as if just scanned.
        Finished matches are still refused (lookahead-leakage guard).
      * Sequential mode (max_workers=1) now also runs run_scan_fn - before,
        it relied on fetch_fn scanning, which never happens for a match
        whose details were already cached.
    """
    # de-dupe, keep order
    seen = set()
    ids = [m for m in match_ids if not (m in seen or seen.add(m))]

    if max_workers <= 1 or fetch_only_fn is None or run_scan_fn is None:
        return _run_sequential(ids, fetch_only_fn or fetch_fn, scan_fn, seconds_per_match,
                               progress_callback, run_scan_fn=run_scan_fn)

    def _emit(msg: str) -> None:
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    total = len(ids)
    report = BatchRunReport()
    batch_start = time.perf_counter()
    _emit(f"Phase 1/2: fetching {total} matches with {max_workers} parallel workers...")

    fetched: dict = {}   # match_id -> (ok, seconds, error)

    def _fetch_one(mid: int):
        t0 = time.perf_counter()
        try:
            ok = bool(fetch_only_fn(mid))
            return mid, ok, time.perf_counter() - t0, None
        except Exception as e:
            logger.error(f"Fetch raised for match {mid}: {e}")
            return mid, False, time.perf_counter() - t0, str(e)

    done = 0
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="batch-fetch") as pool:
        futures = [pool.submit(_fetch_one, m) for m in ids]
        for fut in as_completed(futures):
            mid, ok, secs, err = fut.result()
            fetched[mid] = (ok, secs, err)
            done += 1
            status = "OK" if ok else f"FAILED{(' - ' + err) if err else ''}"
            _emit(f"[fetch {done}/{total}] match {mid}: {status} ({secs:.1f}s) "
                  f"| elapsed {_fmt_mmss(time.perf_counter() - batch_start)}")

    ok_count = sum(1 for v in fetched.values() if v[0])
    _emit(f"Phase 2/2: scanning {ok_count} fetched matches one at a time, in order...")

    for i, mid in enumerate(ids):
        ok, fsecs, ferr = fetched[mid]
        result = MatchRunResult(match_id=mid, fetch_ok=ok, fetch_seconds=fsecs, error=ferr)
        if ok:
            t0 = time.perf_counter()
            try:
                run_scan_fn(mid)   # app passes force=True; finished matches are still refused (it guards itself)
                row = scan_fn(mid)
                if row:
                    result.verdict = row.get("verdict")
                    result.confidence = row.get("confidence")
                    result.selection = row.get("selection")
                    result.integrity = list(row.get("integrity") or [])
            except Exception as e:
                logger.error(f"Scan raised for match {mid}: {e}")
                result.error = str(e)
            result.scan_seconds = time.perf_counter() - t0
            _emit(f"[scan {i+1}/{total}] match {mid}: verdict={result.verdict or 'N/A'}"
                  f"{(' ' + result.confidence) if result.confidence and result.confidence != '-' else ''}"
                  f"{(' on ' + result.selection) if result.selection else ''} "
                  f"({result.scan_seconds*1000:.0f}ms)"
                  f"{(' ⚠ ' + '; '.join(result.integrity)) if result.integrity else ''}")
        report.results.append(result)

    report.total_seconds = time.perf_counter() - batch_start
    _emit(f"Batch complete: {total} matches in {_fmt_mmss(report.total_seconds)}")
    return report