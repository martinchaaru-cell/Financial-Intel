"""The public website (guests) and the members' home summary.

Guests see the website: Home, About, What you get, Pricing, Contact, plus Sign in and
Create account. Reading actual survey content needs an account: every "view" link leads to /register (or
/login), then into the app at /app. The app itself moved from / to /app; a guest who opens /app is sent to
the sign-in page first.

Wired from app.py with one call:  site_pages.init(app, current_user)
"""
import os
import time
from urllib.parse import quote

from flask import jsonify, redirect, render_template, request, session

from models import db, Company, User
from models_survey import SurveyCompanyData, SurveyDirector

_STATS_CACHE = {'at': 0.0, 'data': None}


def _safe_next(target, default='/app'):
    """Only same-site paths: never redirect somewhere else after sign-in."""
    t = (target or '').strip()
    if not t.startswith('/') or t.startswith('//') or '\\' in t:
        return default
    return t


def site_stats():
    """Live coverage numbers for the website (cached for 5 minutes)."""
    if _STATS_CACHE['data'] is not None and time.time() - _STATS_CACHE['at'] < 300:
        return _STATS_CACHE['data']
    try:
        companies = db.session.query(SurveyCompanyData.company_id).distinct().count()
        years = sorted({fy for (fy,) in db.session.query(SurveyCompanyData.fiscal_year).distinct() if fy})
        directors = SurveyDirector.query.count()
        sector_rows = (db.session.query(Company.sector, db.func.count(db.distinct(Company.id)))
                       .join(SurveyCompanyData, SurveyCompanyData.company_id == Company.id)
                       .group_by(Company.sector).all())
        sectors = sorted([(s or 'Other', n) for s, n in sector_rows], key=lambda x: -x[1])
    except Exception:
        db.session.rollback()
        companies, years, directors, sectors = 0, [], 0, []
    data = {'companies': companies, 'years': len(years), 'first_year': years[0] if years else None,
            'last_year': years[-1] if years else None, 'directors': directors,
            'sectors': sectors, 'sector_count': len(sectors)}
    _STATS_CACHE.update(at=time.time(), data=data)
    return data


def _plans():
    try:
        from models_billing import Plan
        import billing
        plans = Plan.query.filter_by(active=True).order_by(Plan.sort_order, Plan.id).all()
        return [dict(p.to_dict(), tier=billing.plan_tier(p)) for p in plans], billing.trial_days(), billing.vat_rate()
    except Exception:
        db.session.rollback()
        return [], 0, 0


def _contact():
    import accounts_email
    return accounts_email.contact_details()


def init(app, current_user):
    from web_hardening import site_url

    @app.context_processor
    def _site_context():
        # Header, footer and coverage numbers for every website page (including the public company pages).
        return {'user': current_user(), 'stats': site_stats(), 'contact': _contact(),
                'self_registration': _self_registration_open()}

    def page(template, **ctx):
        ctx.setdefault('active', '')
        return render_template(template, site_url=site_url(), **ctx)

    @app.route('/')
    def site_home():
        plans, trial, _vat = _plans()
        return page('site/home.html', active='home', plans=plans, trial_days=trial)

    @app.route('/about')
    def site_about():
        return page('site/about.html', active='about')

    @app.route('/features')
    def site_features():
        return page('site/features.html', active='features')

    @app.route('/pricing')
    def site_pricing():
        plans, trial, vat = _plans()
        return page('site/pricing.html', active='pricing', plans=plans, trial_days=trial, vat_rate=vat)

    @app.route('/contact')
    def site_contact():
        return page('site/contact.html', active='contact')

    @app.route('/login')
    def site_login():
        nxt = _safe_next(request.args.get('next'))
        if current_user() is not None:
            return redirect(nxt)
        return page('site/auth.html', mode='login', next_url=nxt)

    @app.route('/register')
    def site_register():
        nxt = _safe_next(request.args.get('next'))
        if current_user() is not None:
            return redirect(nxt)
        return page('site/auth.html', mode='register', next_url=nxt, plan=request.args.get('plan', ''))

    # ---- the app itself ------------------------------------------------------------------------------------
    @app.route('/app')
    def app_page():
        if current_user() is None:
            from flask import g
            ended = getattr(g, 'session_ended', None)
            return redirect('/login?' + (f'ended={ended}&' if ended else '') + 'next=' + quote(request.full_path.rstrip('?') or '/app'))
        return render_template('index.html', site_url=site_url())

    # ---- members' home summary (any signed-in account, any plan) --------------------------------------------
    @app.route('/api/home/summary')
    def home_summary():
        if current_user() is None:
            return jsonify({'error': 'Please sign in.'}), 401
        from survey_aggregate import build_survey_overview
        try:
            ov = build_survey_overview(request.args.get('fiscal_year'))
        except Exception:
            db.session.rollback()
            ov = {'has_data': False}
        st = site_stats()
        out = {'companies': st['companies'], 'years': st['years'], 'first_year': st['first_year'],
               'last_year': st['last_year'], 'directors': st['directors'], 'sector_count': st['sector_count'],
               'has_data': bool(ov.get('has_data'))}
        if ov.get('has_data'):
            def pick(group, key):
                m = (ov.get(group) or {}).get(key) or {}
                return {'median': m.get('p50'), 'average': m.get('average'), 'companies': m.get('company_count')}
            eb, bo = ov.get('executive_summary') or {}, ov.get('board_overview') or {}
            f, m = bo.get('total_directors_female'), bo.get('total_directors_male')
            out.update({
                'fiscal_year': ov.get('fiscal_year'), 'currency': ov.get('currency') or 'KES',
                'companies_surveyed': ov.get('companies_surveyed'),
                'chair_retainer': pick('directors_remuneration', 'chairperson_annual_retainer'),
                'ned_retainer': pick('directors_remuneration', 'other_ned_annual_retainer'),
                'ned_sitting_fee': pick('directors_remuneration', 'other_ned_meeting_allowance'),
                'ceo_monthly_cost': pick('ceo_remuneration', 'ceo_monthly_cost_of_employment'),
                'avg_board_size': (eb.get('avg_board_size') or {}).get('average'),
                'women_on_boards_pct': round(f / (f + m) * 100, 1) if f is not None and m and (f + m) else None,
            })
        try:
            import dashboards
            out['headlines'] = dashboards.headlines()
            out['my_company_id'] = dashboards.my_company_id(current_user())
        except Exception:
            db.session.rollback()
            out['headlines'] = {'items': []}
        return jsonify(out)


def _self_registration_open():
    return os.environ.get('ALLOW_SELF_REGISTRATION', 'true').strip().lower() not in ('0', 'false', 'no', 'off')
