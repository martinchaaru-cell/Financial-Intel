"""Gzip for text responses.

Compress text and the two fixed JavaScript assets, while leaving SSE and downloads untouched.
"""
import gzip
from pathlib import Path

_TYPES = ('text/html', 'text/css', 'text/plain', 'text/xml', 'application/json', 'application/javascript',
          'text/javascript', 'application/xml', 'image/svg+xml')
_MIN_BYTES = 1024


def register(app):
    @app.after_request
    def _gzip(resp):
        try:
            from flask import request
            versioned = request.path == '/static/app-runtime.js' and request.args.get('v') == 'perf2'
            if versioned and resp.status_code in (200, 304):
                resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            fixed_js = request.path in ('/static/app-runtime.js', '/static/vendors/chart.umd.min.js')
            if resp.status_code != 200 or request.method == 'HEAD' or ((resp.direct_passthrough or resp.is_streamed) and not fixed_js):
                return resp
            if (resp.mimetype or '') not in _TYPES or resp.headers.get('Content-Encoding'):
                return resp
            resp.vary.add('Accept-Encoding')
            if request.accept_encodings['gzip'] <= 0 or not request.headers.get('Accept-Encoding'):
                return resp
            if fixed_js: resp.direct_passthrough = False
            data = resp.get_data()
            if len(data) < _MIN_BYTES:
                return resp
            if versioned:
                from flask import current_app
                packed = Path(current_app.static_folder) / 'app-runtime.js.gz'
                resp.set_data(packed.read_bytes() if packed.exists() else gzip.compress(data, 3))
            else:
                resp.set_data(gzip.compress(data, 3))
            resp.headers['Content-Encoding'] = 'gzip'
            resp.headers['Content-Length'] = str(len(resp.get_data()))
            tag, weak = resp.get_etag()
            if tag: resp.set_etag(tag, weak=True)
        except Exception:
            pass                                              # never break a page because compression failed
        return resp
