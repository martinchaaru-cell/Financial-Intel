"""Gzip for text responses.

The app page (templates/index.html) is ~780 KB of HTML + inline JavaScript. Flask does not compress by default, so every
visitor downloaded all of it uncompressed before the page could start (its boot script is at the very end). Gzip makes it
about 4x smaller. Streamed responses (the live-update SSE streams) and static files are left alone.
"""
import gzip

_TYPES = ('text/html', 'text/css', 'text/plain', 'text/xml', 'application/json', 'application/javascript',
          'text/javascript', 'application/xml', 'image/svg+xml')
_MIN_BYTES = 1024


def register(app):
    @app.after_request
    def _gzip(resp):
        try:
            if resp.status_code != 200 or resp.direct_passthrough or resp.is_streamed:
                return resp                                   # SSE / file streams / non-200: untouched
            from flask import request
            if 'gzip' not in request.headers.get('Accept-Encoding', '').lower():
                return resp
            if (resp.mimetype or '') not in _TYPES or resp.headers.get('Content-Encoding'):
                return resp
            data = resp.get_data()
            if len(data) < _MIN_BYTES:
                return resp
            resp.set_data(gzip.compress(data, 6))
            resp.headers['Content-Encoding'] = 'gzip'
            resp.headers['Content-Length'] = str(len(resp.get_data()))
            resp.headers.add('Vary', 'Accept-Encoding')
            if resp.headers.get('ETag'):
                resp.headers['ETag'] = resp.headers['ETag'].replace('"', '-gz"', 1) if resp.headers['ETag'].startswith('"') else resp.headers['ETag']
        except Exception:
            pass                                              # never break a page because compression failed
        return resp
