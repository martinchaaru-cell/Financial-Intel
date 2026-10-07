"""HTTPS enforcement, security headers, secure cookies, and the search-engine files (robots.txt, sitemap.xml).

    import web_hardening
    web_hardening.init(app)          # once, right after the Flask app and its config exist

What this can and cannot do: the TLS *certificate* is issued by wherever the site is hosted (Replit
Deployments, Render, or the domain's DNS/CDN such as Cloudflare) - no application code can create one. This module
makes the app behave correctly once a certificate is in place: it trusts the host's proxy headers so it knows a
request really was HTTPS, sends plain-HTTP visitors to HTTPS, tells browsers to keep using HTTPS (HSTS), and marks
the login cookie Secure so it never travels over plain HTTP.

Settings (all optional environment variables):
    FORCE_HTTPS   '0' turns the HTTP->HTTPS redirect off (default: on for any non-local host)
    SITE_URL      canonical public address, e.g. https://financeintel.example.com - used in robots.txt, sitemap
                  and the page's canonical/Open Graph tags. Falls back to the address the request arrived on.
    HSTS_MAX_AGE  seconds (default 31536000 = 1 year); set 0 to send no HSTS header while first testing HTTPS
"""
import logging
import os

from flask import Response, redirect, request
from flask.sessions import SecureCookieSessionInterface
from werkzeug.middleware.proxy_fix import ProxyFix

_LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1', '0.0.0.0'}
_EXEMPT_PATHS = {'/api/system/status'}          # health checks arrive over plain HTTP from inside the container


# Replit's workspace shows the running app inside a frame on replit.com. Blocking all outside frames made the
# preview pane fail (the app still worked in its own tab). Inside the Replit workspace only, those Replit
# addresses may frame the app; the published deployment and every other host stay "own pages only".
_REPLIT_WORKSPACE_ANCESTORS = "'self' https://replit.com https://*.replit.com https://*.replit.dev https://*.repl.co"


def _frame_ancestors():
    """Sites allowed to show the app inside a frame (the CSP frame-ancestors value).

    FRAME_ANCESTORS (optional env var) overrides everything, e.g. "'self' https://intranet.example.com".
    """
    configured = (os.environ.get('FRAME_ANCESTORS') or '').strip()
    if configured:
        return configured
    in_replit_workspace = bool(os.environ.get('REPL_ID')) and os.environ.get('REPLIT_DEPLOYMENT', '') != '1'
    return _REPLIT_WORKSPACE_ANCESTORS if in_replit_workspace else "'self'"


def _force_https_enabled():
    return os.environ.get('FORCE_HTTPS', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def site_url():
    """Public base URL with no trailing slash."""
    configured = (os.environ.get('SITE_URL') or '').strip().rstrip('/')
    if configured:
        return configured
    return request.url_root.rstrip('/')


class _SecureWhenHttps(SecureCookieSessionInterface):
    """The login cookie is Secure exactly when the request is HTTPS, so local http:// development still works."""

    def get_cookie_secure(self, app):
        return bool(request.is_secure)


def init(app):
    # Behind a proxy the app only sees plain HTTP from the proxy; X-Forwarded-Proto says what the visitor used.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.session_interface = _SecureWhenHttps()
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

    if app.config.get('SECRET_KEY') == 'dev-only-change-me':
        logging.getLogger(__name__).warning(
            'SESSION_SECRET is not set: the built-in development key is in use. Set a long random '
            'SESSION_SECRET in the deployment environment before real users log in.')

    @app.before_request
    def _redirect_to_https():
        if not _force_https_enabled() or request.is_secure:
            return None
        host = (request.host or '').split(':')[0].lower()
        if host in _LOCAL_HOSTS or request.path in _EXEMPT_PATHS or (app.testing and not app.config.get('FORCE_HTTPS_IN_TESTS')):
            return None
        return redirect(request.url.replace('http://', 'https://', 1), code=301)

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        ancestors = _frame_ancestors()
        if ancestors == "'self'":
            # X-Frame-Options cannot list other sites, so it is only sent when framing is limited to our own pages.
            resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        resp.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=()')
        # frame-ancestors only: a full script policy would need the app's many inline handlers rewritten first.
        resp.headers.setdefault('Content-Security-Policy', f'frame-ancestors {ancestors}')
        if request.is_secure:
            try:
                age = int(os.environ.get('HSTS_MAX_AGE', '31536000'))
            except ValueError:
                age = 31536000
            if age > 0:
                resp.headers.setdefault('Strict-Transport-Security', f'max-age={age}; includeSubDomains')
        if request.path.startswith('/api/'):
            resp.headers.setdefault('X-Robots-Tag', 'noindex, nofollow')     # data endpoints never belong in search
        return resp

    @app.route('/robots.txt')
    def robots_txt():
        body = ('User-agent: *\n'
                'Allow: /\n'
                'Disallow: /api/\n'
                f'Sitemap: {site_url()}/sitemap.xml\n')
        return Response(body, mimetype='text/plain')

    @app.route('/sitemap.xml')
    def sitemap_xml():
        # The homepage, plus the public directory and one page per company with published board data
        # (public_pages.py). Everything else is behind the login and stays out.
        extra = ''
        try:
            import public_pages
            extra = public_pages.sitemap_xml_fragment(site_url())
        except Exception:
            logging.getLogger(__name__).exception('Could not add public company pages to the sitemap')
        body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                f'  <url><loc>{site_url()}/</loc><changefreq>monthly</changefreq><priority>1.0</priority></url>\n'
                f'{extra}'
                '</urlset>\n')
        return Response(body, mimetype='application/xml')