"""Public, search-engine-visible company pages for Finance Intel.

    import public_pages
    public_pages.init(app)           # once, after web_hardening.init(app)

Why: the dashboard is one JavaScript page behind a login, so Google could only ever see the homepage.
This adds ordinary server-rendered pages:
    /companies             directory of every company that has published board data
    /company/<slug>        e.g. /company/kcb-group  (old /company/15 style links 301-redirect to the slug)
and feeds them into /sitemap.xml (see web_hardening.py).

What is public (a teaser, by design): company name, ticker, sector, and board COMPOSITION by fiscal year
(board size, executive / non-executive / independent split, women on the board, meetings).
What is NOT public: any pay figure, financial statement, extraction detail or source file. Those stay
behind the login. Edit `_board_rows()` below to change that line.

Env:  The pages are OFF by default (the public Coverage page was removed from the website on 27 Oct 2026):
      /companies and /company/<slug> redirect to the home page and are left out of the sitemap.
      PUBLIC_COMPANY_PAGES=1 switches them back on.
"""
import os
import re
from datetime import datetime
from xml.sax.saxutils import escape

from flask import abort, redirect, render_template, request, url_for

_TRAILING = re.compile(r'[\s,.-]+(plc|limited|ltd|company|co)\.?$', re.I)


def enabled():
    return os.environ.get('PUBLIC_COMPANY_PAGES', '0').strip().lower() in ('1', 'true', 'yes', 'on')


# ---------------------------------------------------------------- slugs
def make_slug(name):
    base = _TRAILING.sub('', (name or '').strip())
    slug = re.sub(r'[^a-z0-9]+', '-', base.lower().replace('&', ' and ')).strip('-')
    return slug or 'company'


def ensure_slugs():
    """Give every company without a slug a unique one. Cheap when nothing is missing."""
    from models import db, Company
    missing = Company.query.filter((Company.slug.is_(None)) | (Company.slug == '')).all()
    if not missing:
        return
    taken = {s for (s,) in db.session.query(Company.slug).filter(Company.slug.isnot(None)).all()}
    for c in missing:
        slug = make_slug(c.name)
        if slug in taken or slug.isdigit():
            slug = f'{slug}-{c.id}'
        taken.add(slug)
        c.slug = slug
    db.session.commit()


# ---------------------------------------------------------------- data
def _year_key(label):
    m = re.search(r'(\d{4})', str(label or ''))
    return int(m.group(1)) if m else 0


def _board_rows(company_id):
    """Public board-composition rows, newest year first. Full financial years with a stated board size only."""
    from models_survey import SurveyCompanyData, is_full_year
    rows = SurveyCompanyData.query.filter_by(company_id=company_id).all()
    rows = [r for r in rows if is_full_year(r.fiscal_year) and r.board_size]
    rows.sort(key=lambda r: _year_key(r.fiscal_year), reverse=True)
    out = []
    for r in rows:
        out.append({
            'year': r.fiscal_year, 'board_size': r.board_size,
            'executive': r.executive_directors_count, 'non_executive': r.non_executive_directors_count,
            'independent': r.independent_neds_count, 'women': r.directors_female, 'men': r.directors_male,
            'meetings': r.board_meetings_per_year, 'committees': r.committees_per_board,
            'updated': r.uploaded_at,
        })
    return out


def public_companies():
    """[(Company, rows)] for every company with at least one public board row, A-Z."""
    from models import Company
    ensure_slugs()
    result = []
    for c in Company.query.order_by(Company.name.asc()).all():
        rows = _board_rows(c.id)
        if rows:
            result.append((c, rows))
    return result


def sitemap_entries(base):
    """[(absolute_url, lastmod 'YYYY-MM-DD' or None, changefreq, priority)] for /sitemap.xml."""
    if not enabled():
        return []
    entries = [(f'{base}/companies', None, 'weekly', '0.8')]
    for c, rows in public_companies():
        stamps = [r['updated'] for r in rows if r['updated']]
        last = max(stamps).strftime('%Y-%m-%d') if stamps else None
        entries.append((f'{base}/company/{c.slug}', last, 'monthly', '0.7'))
    return entries


def sitemap_xml_fragment(base):
    parts = []
    for loc, last, freq, prio in sitemap_entries(base):
        lm = f'<lastmod>{last}</lastmod>' if last else ''
        parts.append(f'  <url><loc>{escape(loc)}</loc>{lm}<changefreq>{freq}</changefreq><priority>{prio}</priority></url>\n')
    return ''.join(parts)


# ---------------------------------------------------------------- text helpers
def _plural(n, word):
    return f'{n} {word}{"" if n == 1 else "s"}'


def describe(company, row):
    bits = [f'{company.name} board for {row["year"]}: {_plural(row["board_size"], "director")}']
    split = []
    if row['executive'] is not None:
        split.append(f'{row["executive"]} executive')
    if row['non_executive'] is not None:
        split.append(f'{row["non_executive"]} non-executive')
    if row['independent'] is not None:
        split.append(f'{row["independent"]} independent')
    if split:
        bits[0] += ' (' + ', '.join(split) + ')'
    if row['women'] is not None:
        bits.append(f'{row["women"]} women on the board')
    bits.append('Sign in to Finance Intel for director remuneration benchmarks and peer comparisons.')
    return '. '.join(bits)[:300]


# ---------------------------------------------------------------- routes
def init(app):
    from models import Company
    from web_hardening import site_url

    def _public(html):
        resp = app.make_response(html)
        resp.headers['Cache-Control'] = 'public, max-age=300'
        return resp

    @app.route('/companies')
    def public_directory():
        if not enabled():
            return redirect('/', code=301)      # the Coverage pages are switched off: send old links home
        base = site_url()
        items = [{'name': c.name, 'ticker': c.ticker, 'sector': c.sector, 'slug': c.slug,
                  'year': rows[0]['year'], 'board_size': rows[0]['board_size']}
                 for c, rows in public_companies()]
        return _public(render_template('public_directory.html', site_url=base, items=items))

    @app.route('/company/<slug>')
    def public_company(slug):
        if not enabled():
            return redirect('/', code=301)      # the Coverage pages are switched off: send old links home
        ensure_slugs()
        if slug.isdigit():                       # old id-style link -> permanent redirect to the readable URL
            c = Company.query.get(int(slug))
            if not c:
                abort(404)
            return redirect(url_for('public_company', slug=c.slug), code=301)
        c = Company.query.filter_by(slug=slug.lower()).first()
        if not c:
            abort(404)
        rows = _board_rows(c.id)
        base = site_url()
        latest = rows[0] if rows else None
        title_year = f' {latest["year"]}' if latest else ''
        ticker = f' ({c.ticker})' if c.ticker else ''
        ctx = {
            'site_url': base, 'company': c, 'rows': rows, 'latest': latest,
            'page_title': f'{c.name}{ticker} Board Composition & Governance{title_year} | Finance Intel',
            'page_description': describe(c, latest) if latest else
                                f'{c.name} is tracked by Finance Intel, which benchmarks NSE-listed companies on board composition and director remuneration.',
            'canonical': f'{base}/company/{c.slug}',
            'noindex': latest is None,           # never ask Google to index a page with no real content
        }
        return _public(render_template('public_company.html', **ctx))
