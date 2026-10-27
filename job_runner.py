"""Report reading runs in a separate worker process, so a large annual report never slows the app for other users.

Reading a 300-page report keeps a processor busy for minutes. Python runs one thread of Python code at a time per
process, so when that work ran on a thread inside the web server, every other request queued behind it. Here it runs
in its own process (EXTRACT_PROCESSES, default 1): the web server stays free to answer everyone else.

The worker sends its progress lines and final result back through a queue; a listener thread in the web process
passes them to the same live progress stream and result store as before, so the upload screens work unchanged.
If the worker process cannot start or dies (for example, out of memory on a huge PDF), the batch is reported as
failed instead of leaving the screen waiting, and the next batch gets a fresh worker.

EXTRACT_PROCESSES=0 runs everything on threads in the web process, as before. Tests and in-memory databases always
use threads, because a separate process cannot see an in-memory database.

Wired from app.py:  job_runner.init(app, progress_emit=..., store_result=..., event_emit=...)
"""
import logging
import multiprocessing as mp
import os
import sys
import threading
from concurrent.futures import ProcessPoolExecutor

log = logging.getLogger(__name__)

_state = {'app': None, 'progress': None, 'store': None, 'event': None, 'pool': None, 'queue': None,
          'listener': None, 'lock': threading.Lock()}
_child_queue = None


# ---------------------------------------------------------------- web-process side
def init(app, progress_emit, store_result, event_emit=None):
    _state.update(app=app, progress=progress_emit, store=store_result, event=event_emit)


def _processes():
    try:
        return max(0, int(os.environ.get('EXTRACT_PROCESSES', '1')))
    except ValueError:
        return 1


def use_processes():
    app = _state['app']
    if app is None or _processes() == 0 or app.config.get('TESTING'):
        return False
    uri = str(app.config.get('SQLALCHEMY_DATABASE_URI') or os.environ.get('DATABASE_URL') or '')
    return ':memory:' not in uri and bool(uri)


def _listen(q):
    while True:
        try:
            item = q.get()
        except (EOFError, OSError):
            return
        try:
            kind = item[0]
            if kind == 'progress':
                _state['progress'](item[1], item[2])
            elif kind == 'result':
                _state['store'](item[1], item[2])
            elif kind == 'event' and _state['event']:
                _state['event'](item[1])
        except Exception:
            log.exception('job_runner: could not pass on a worker message')


def _pool():
    with _state['lock']:
        if _state['pool'] is None:
            ctx = mp.get_context('spawn')        # a clean process: never copies the web server's threads or connections
            q = ctx.Queue()
            _state['queue'] = q
            _state['pool'] = ProcessPoolExecutor(max_workers=_processes(), mp_context=ctx,
                                                 initializer=_child_init, initargs=(q,))
            t = threading.Thread(target=_listen, args=(q,), daemon=True, name='job-runner-listener')
            t.start()
            _state['listener'] = t
        return _state['pool']


def _reset_pool():
    with _state['lock']:
        pool = _state['pool']
        _state['pool'] = None
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=False)
        except Exception:
            pass


def _failed(batch_id, n_files, error):
    result = {'results': [], 'saved': 0, 'failed': n_files,
              'error': 'Reading the report stopped unexpectedly (it may be too large). Please try again, or upload it on its own.'}
    try:
        _state['store'](batch_id, result)
        _state['progress'](batch_id, {'type': 'batch_failed', 'error': result['error'], 'saved': 0, 'failed': n_files})
    except Exception:
        log.exception('job_runner: could not report a failed batch')
    log.error('job_runner: batch %s failed in the worker process: %s', batch_id, error)


def _thread_fallback(run_in_thread, args, wait):
    if wait:
        run_in_thread(_state['app'], *args)
    else:
        threading.Thread(target=run_in_thread, args=(_state['app'],) + args, daemon=True).start()


def submit_batch(run_in_thread, file_payloads, batch_id, forced_company_id, batch_started, pre_results=None, draft_ids=None,
                 wait=False):
    """Read and import an upload batch. run_in_thread is app._run_upload_batch_job (used when processes are off)."""
    args = (file_payloads, batch_id, forced_company_id, batch_started, pre_results, draft_ids)
    if not use_processes():
        return _thread_fallback(run_in_thread, args, wait)
    try:
        fut = _pool().submit(_child_batch, args)
    except Exception as e:                     # the pool is broken: start a fresh one next time, run this one on a thread
        log.warning('job_runner: worker process unavailable (%s); reading on a thread instead', e)
        _reset_pool()
        return _thread_fallback(run_in_thread, args, wait)

    def _done(f):
        try:
            res = f.result()
            if res is not None:
                _state['store'](batch_id, res)      # the queue may lag behind; the result must be there when asked for
        except Exception as e:
            _reset_pool()
            _failed(batch_id, len(file_payloads), e)
    if wait:
        _done(fut)
    else:
        fut.add_done_callback(_done)


def submit_preview(run_preview, draft_id):
    """Build an upload's review preview (also reads the whole PDF) away from the web process."""
    if not use_processes():
        threading.Thread(target=run_preview, args=(_state['app'], draft_id), daemon=True).start()
        return
    try:
        fut = _pool().submit(_child_preview, draft_id)
        fut.add_done_callback(lambda f: f.exception() and _reset_pool())
    except Exception:
        _reset_pool()
        threading.Thread(target=run_preview, args=(_state['app'], draft_id), daemon=True).start()


# ---------------------------------------------------------------- worker-process side
def _child_init(q):
    global _child_queue
    _child_queue = q
    os.environ['FI_JOB_WORKER'] = '1'


def _app_module():
    """The application module inside the worker. Under `python app.py` the worker has already loaded it as
    __mp_main__; under gunicorn it is imported as `app`."""
    m = sys.modules.get('__mp_main__')
    if m is not None and hasattr(m, '_run_upload_batch_job'):
        return m
    import app as m  # noqa: E402
    return m


def _child_batch(args):
    A = _app_module()
    holder = {}

    def progress(bid, ev):
        _child_queue.put(('progress', bid, ev))

    def store(bid, res):
        holder['result'] = res
        _child_queue.put(('result', bid, res))

    A._progress_emit = progress
    A._store_batch_result = store
    A._event_emit = lambda ev: _child_queue.put(('event', ev))
    A._run_upload_batch_job(A.app, *args)
    return holder.get('result')


def _child_preview(draft_id):
    A = _app_module()
    import review_api
    review_api._run_preview(A.app, draft_id)
    return True
