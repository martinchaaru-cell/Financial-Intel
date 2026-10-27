"""The two dashboards: the super admin's business dashboard and the member's "my company vs the market" dashboard.

  /api/admin/dashboard        super admin: customers, revenue, renewals, client engagement (who is using the app,
                              who is at risk), plan mix and the companies clients look at most.
  /api/home/company           member on a paid plan: one company's pay and governance figures against every
                              other listed company and its sector (median, quartiles, percentile, rank, change).
  /api/home/my-company  POST  remembers the member's own company (for the whole organisation, else the account).
  headlines()                 market medians this year vs last year on the same companies - used by
                              /api/home/summary for everyone, free accounts included.

Every figure comes from the survey rows (survey_aggregate.build_company_rows: money in whole KES, foreign
currencies converted) or from the billing and activity tables. Nothing is estimated; a metric a company does not
disclose is left out rather than shown as zero.

Wired from app.py with one call:  dashboards.register(app, current_user, require_super_admin)
"""
from datetime import timedelta

from flask import jsonify, request

from models import db, Company, User
from models_survey import ActivityLogEntry

MIN_PEERS = 3          # a position is only shown when at least this many companies disclose the figure

# key, label, kind (money|pct|count), group, higher_is_better (None = neutral, e.g. pay)
METRICS = [
    ('chairperson_annual_retainer', 'Chair annual retainer', 'money', 'pay', None),
    ('other_ned_annual_retainer', 'Non-executive director retainer', 'money', 'pay', None),
    ('other_ned_meeting_allowance', 'NED sitting fee (per meeting)', 'money', 'pay', None),
    ('total_directors_fees', "Total directors' fees", 'money', 'pay', None),
    ('ceo_monthly_cost_of_employment', 'CEO cost of employment per month', 'money', 'pay', None),
    ('kmp_total', 'Key management pay (total)', 'money', 'pay', None),
    ('board_size', 'Board size', 'count', 'board', None),
    ('women_on_board_pct', 'Women on the board', 'pct', 'board', True),
    ('independent_pct', 'Independent directors', 'pct', 'board', True),
    ('board_meetings_per_year', 'Board meetings a year', 'count', 'board', None),
    ('board_attendance_pct', 'Board meeting attendance', 'pct', 'board', True),
    ('profit_margin', 'Net profit margin', 'pct', 'company', True),
    ('staff_cost_per_employee', 'Staff cost per employee', 'money', 'company', None),
    ('women_in_workforce_pct', 'Women in the workforce', 'pct', 'company', True),
    ('audit_fee_pct_of_turnover', 'Audit fee as % of turnover', 'pct', 'company', None),
    ('payout_ratio_pct', 'Dividend payout ratio', 'pct', 'company', None),
]
HEADLINE_KEYS = ['ceo_monthly_cost_of_employment', 'chairperson_annual_retainer', 'other_ned_annual_retainer',
                 'other_ned_meeting_allowance', 'women_on_board_pct', 'board_attendance_pct']


# ---------------------------------------------------------------- survey figures
def _value(row, key):
    if key == 'women_on_board_pct':
        f, m = row.get('directors_female'), row.get('directors_male')
        return f / (f + m) * 100 if (f is not None and m is not None and (f + m) > 0) else None
    if key == 'independent_pct':
        i, b = row.get('independent_neds_count'), row.get('board_size')
        return min(100.0, i / b * 100) if (i is not None and b) else None
    v = row.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def _median(vals):
    vals = sorted(vals)
    n = len(vals)
    if not n:
        return None
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _quantile(vals, p):
    vals = sorted(vals)
    if not vals:
        return None
    k = (len(vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    return vals[f] + (vals[c] - vals[f]) * (k - f)


_ROWS_CACHE = {}


def _rows(fy):
    """Per-company survey rows for one year, kept for 60 seconds (a dashboard asks for the same years several times)."""
    import time
    import survey_aggregate
    if not fy:
        return []
    hit = _ROWS_CACHE.get(fy)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    data = survey_aggregate.build_company_rows(fy)
    rows = data.get('rows', []) if data.get('has_data') else []
    _ROWS_CACHE[fy] = (time.time(), rows)
    return rows


def _years():
    import survey_aggregate
    return sorted(survey_aggregate.available_fiscal_years(), key=survey_aggregate._fy_sort_key)


def _r(v, kind):
    if v is None:
        return None
    if kind == 'money':
        return round(v)
    return round(v, 2) if abs(v) < 1 else round(v, 1)


def compare(company_id, fy):
    """One company's metrics against the market and its sector for one fiscal year."""
    rows = _rows(fy)
    me = next((r for r in rows if r['company_id'] == company_id), None)
    if me is None:
        return None
    years = _years()
    prev_fy = years[years.index(fy) - 1] if fy in years and years.index(fy) > 0 else None
    prev_me = next((r for r in _rows(prev_fy) if r['company_id'] == company_id), None) if prev_fy else None
    sector = me.get('sector')
    out = []
    for key, label, kind, group, good_high in METRICS:
        v = _value(me, key)
        market = [_value(r, key) for r in rows]
        market = [x for x in market if x is not None]
        sect = [x for x in (_value(r, key) for r in rows if r.get('sector') == sector) if x is not None]
        item = {'key': key, 'label': label, 'kind': kind, 'group': group, 'good_high': good_high,
                'value': _r(v, kind), 'companies': len(market),
                'median': _r(_median(market), kind) if len(market) >= MIN_PEERS else None,
                'p25': _r(_quantile(market, .25), kind) if len(market) >= MIN_PEERS else None,
                'p75': _r(_quantile(market, .75), kind) if len(market) >= MIN_PEERS else None,
                'min': _r(min(market), kind) if len(market) >= MIN_PEERS else None,
                'max': _r(max(market), kind) if len(market) >= MIN_PEERS else None,
                'sector_median': _r(_median(sect), kind) if len(sect) >= 2 else None, 'sector_companies': len(sect),
                'percentile': None, 'rank': None, 'prev': None, 'change_pct': None, 'change_pts': None}
        if v is not None and len(market) >= MIN_PEERS:
            others = list(market)
            if v in others:
                others.remove(v)
            below = sum(1 for x in others if x < v)
            equal = sum(1 for x in others if x == v)
            item['percentile'] = round((below + equal / 2) / len(others) * 100) if others else None
            item['rank'] = 1 + sum(1 for x in market if x > v)
        pv = _value(prev_me, key) if prev_me else None
        if v is not None and pv is not None:
            item['prev'] = _r(pv, kind)
            if kind == 'pct':
                item['change_pts'] = round(v - pv, 1)
            elif pv:
                item['change_pct'] = round((v - pv) / abs(pv) * 100, 1)
        out.append(item)
    company_years = [y for y in years if any(r['company_id'] == company_id for r in _rows(y))] if len(years) <= 8 else years
    return {'company': {'id': me['company_id'], 'name': me['name'], 'sector': sector, 'ticker': me.get('ticker')},
            'fiscal_year': fy, 'previous_year': prev_fy if prev_me else None, 'years': company_years,
            'currency': me.get('currency') or 'KES', 'companies': len(rows),
            'sector_companies': sum(1 for r in rows if r.get('sector') == sector), 'metrics': out}


def headlines():
    """Market medians for the latest year vs the year before, on the companies that disclose the figure in both
    years (like for like), so the change is not just a different mix of companies."""
    years = _years()
    if not years:
        return {'fiscal_year': None, 'items': []}
    fy = years[-1]
    prev = years[-2] if len(years) > 1 else None
    now_rows = {r['company_id']: r for r in _rows(fy)}
    prev_rows = {r['company_id']: r for r in _rows(prev)} if prev else {}
    kinds = {k: (lbl, kind) for k, lbl, kind, _g, _h in METRICS}
    items = []
    for key in HEADLINE_KEYS:
        label, kind = kinds[key]
        cur = [x for x in (_value(r, key) for r in now_rows.values()) if x is not None]
        both = [(cid, _value(now_rows[cid], key), _value(prev_rows[cid], key)) for cid in now_rows if cid in prev_rows]
        both = [(c, a, b) for c, a, b in both if a is not None and b is not None]
        item = {'key': key, 'label': label, 'kind': kind, 'median': _r(_median(cur), kind) if len(cur) >= MIN_PEERS else None,
                'companies': len(cur), 'like_for_like': len(both), 'change_pct': None, 'change_pts': None,
                'rose': None, 'fell': None}
        if len(both) >= MIN_PEERS:
            m_now, m_prev = _median([a for _c, a, _b in both]), _median([b for _c, _a, b in both])
            if kind == 'pct':
                item['change_pts'] = round(m_now - m_prev, 1)
            elif m_prev:
                item['change_pct'] = round((m_now - m_prev) / abs(m_prev) * 100, 1)
            item['rose'] = sum(1 for _c, a, b in both if a > b)
            item['fell'] = sum(1 for _c, a, b in both if a < b)
        items.append(item)
    return {'fiscal_year': fy, 'previous_year': prev, 'items': items}


def company_choices():
    """Every company with survey figures, for the "choose your company" list."""
    from models_survey import SurveyCompanyData
    ids = [i for (i,) in db.session.query(SurveyCompanyData.company_id).distinct().all()]
    if not ids:
        return []
    rows = (db.session.query(Company.id, Company.name, Company.sector)
            .filter(Company.id.in_(ids)).order_by(Company.name).all())
    return [{'id': i, 'name': n, 'sector': s} for i, n, s in rows]


def my_company_id(user):
    """The member's own company: the organisation's choice, else the account's own."""
    from models_billing import Organisation
    org = db.session.get(Organisation, user.organization_id) if user.organization_id else None
    if org is not None and getattr(org, 'company_id', None):
        return org.company_id
    return getattr(user, 'home_company_id', None)


# ---------------------------------------------------------------- business figures (super admin)
def business(at=None):
    import billing
    from models_billing import Organisation, Plan, Payment, Invoice
    at = at or billing.now()
    d30, d7 = at - timedelta(days=30), at - timedelta(days=7)
    month_start = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = month_start.replace(month=1)
    ly_start, ly_same = year_start.replace(year=year_start.year - 1), at.replace(year=at.year - 1)

    orgs = Organisation.query.order_by(Organisation.name).all()
    customers, mrr, plan_mix = [], {}, {}
    by_status = {}
    active_clients = trialing = paying = new_month = 0
    for o in orgs:
        sub, st = billing.best_subscription(o.id, at)
        key = st or 'no_plan'
        by_status[key] = by_status.get(key, 0) + 1
        plan = sub.plan if sub else None
        members = billing.org_members(o.id)
        ids = [m.id for m in members]
        last = sign30 = act_users = dl_month = 0
        last_at = None
        if ids:
            q = ActivityLogEntry.query.filter(ActivityLogEntry.user_id.in_(ids))
            e = q.order_by(ActivityLogEntry.created_at.desc()).first()
            last_at = e.created_at if e else None
            sign30 = q.filter(ActivityLogEntry.action == 'login', ActivityLogEntry.created_at >= d30).count()
            act_users = (db.session.query(db.func.count(db.distinct(ActivityLogEntry.user_id)))
                         .filter(ActivityLogEntry.user_id.in_(ids), ActivityLogEntry.created_at >= d30).scalar() or 0)
            dl_month = sum(billing.downloads_used(o.id, at).values())
        is_paying = st in ('active', 'past_due')
        paying += is_paying
        trialing += st == 'trialing'
        if o.created_at and o.created_at >= month_start:
            new_month += 1
        if is_paying and plan:
            per = plan.price / max(1, plan.interval_months)
            mrr[plan.currency] = mrr.get(plan.currency, 0) + per
            pm = plan_mix.setdefault(plan.name, {'plan': plan.name, 'customers': 0, 'mrr': 0, 'currency': plan.currency})
            pm['customers'] += 1
            pm['mrr'] += per
        if act_users and st in ('active', 'past_due', 'trialing'):
            active_clients += 1
        overdue = [i for i in Invoice.query.filter(Invoice.organisation_id == o.id, Invoice.status == 'open',
                                                   Invoice.due_at < at).all() if i.total - i.amount_paid() > 0]
        owed = sum(max(0, i.total - i.amount_paid())
                   for i in Invoice.query.filter_by(organisation_id=o.id, status='open').all())
        idle = (at - last_at).days if last_at else None
        if overdue:
            health, tone = 'Payment overdue', 'red'
        elif st in ('active', 'past_due') and sub and sub.cancel_at_period_end:
            health, tone = 'Cancelling', 'amber'
        elif st == 'trialing':
            health, tone = ('Trial · in use', 'blue') if act_users else ('Trial · not used yet', 'amber')
        elif st in ('active', 'past_due'):
            health, tone = ('At risk · inactive', 'red') if (idle is None or idle > 21) else ('Healthy', 'green')
        else:
            health, tone = 'No active plan', 'faint'
        end = (sub.trial_end if st == 'trialing' else sub.current_period_end) if sub else None
        customers.append({'id': o.id, 'name': o.name, 'plan': plan.name if plan else None, 'status': key,
                          'price': plan.price if plan else None, 'currency': plan.currency if plan else 'KES',
                          'interval_months': plan.interval_months if plan else None,
                          'seats_used': len(members), 'seats': sub.seats() if sub else 0,
                          'last_active': (last_at.isoformat() + 'Z') if last_at else None, 'idle_days': idle,
                          'sign_ins_30d': sign30, 'active_users_30d': act_users, 'downloads_month': dl_month,
                          'period_end': end.isoformat() if end else None, 'balance_due': owed,
                          'overdue': bool(overdue), 'health': health, 'tone': tone})
    order = {'red': 0, 'amber': 1, 'blue': 2, 'green': 3, 'faint': 4}
    customers.sort(key=lambda c: (order[c['tone']], c['period_end'] or '9999', c['name'].lower()))

    def money_sum(q):
        out = {}
        for p in q:
            out[p.currency] = out.get(p.currency, 0) + p.amount
        return out
    ytd = money_sum(Payment.query.filter(Payment.paid_at >= year_start, Payment.paid_at <= at).all())
    ly = money_sum(Payment.query.filter(Payment.paid_at >= ly_start, Payment.paid_at <= ly_same).all())
    c30 = money_sum(Payment.query.filter(Payment.paid_at >= d30).all())
    outstanding, overdue_total, overdue_list = {}, {}, []
    for i in Invoice.query.filter_by(status='open').all():
        owed = max(0, i.total - i.amount_paid())
        outstanding[i.currency] = outstanding.get(i.currency, 0) + owed
        if owed and i.due_at and i.due_at < at:
            overdue_total[i.currency] = overdue_total.get(i.currency, 0) + owed
            overdue_list.append({'id': i.id, 'number': i.number, 'organisation_id': i.organisation_id,
                                 'organisation': i.organisation.name if i.organisation else '', 'balance': owed,
                                 'currency': i.currency, 'days': (at - i.due_at).days})
    overdue_list.sort(key=lambda r: -r['days'])

    months = []
    for back in range(11, -1, -1):
        m = billing.add_months(month_start, -back)
        months.append({'month': m.strftime('%Y-%m'), 'label': m.strftime('%b'), 'year': m.year, 'amounts': {}})
    for p in Payment.query.filter(Payment.paid_at >= billing.add_months(month_start, -11)).all():
        k = p.paid_at.strftime('%Y-%m')
        for m in months:
            if m['month'] == k:
                m['amounts'][p.currency] = m['amounts'].get(p.currency, 0) + p.amount

    upcoming = []
    for c in customers:
        if c['period_end'] and c['status'] in ('trialing', 'active'):
            from datetime import datetime
            end = datetime.fromisoformat(c['period_end'])
            if at <= end <= at + timedelta(days=30):
                upcoming.append({'organisation_id': c['id'], 'organisation': c['name'], 'plan': c['plan'], 'status': c['status'],
                                 'date': c['period_end'], 'price': c['price'], 'currency': c['currency'],
                                 'health': c['health'], 'tone': c['tone']})
    upcoming.sort(key=lambda r: r['date'])

    trial_used = [o for o in orgs if o.trial_used]
    converted = sum(1 for o in trial_used if billing.best_subscription(o.id, at)[1] in ('active', 'past_due'))

    # What clients look at: company reports opened (once a day per person) and downloads by member accounts, last 30 days.
    viewed = (db.session.query(ActivityLogEntry.company_id, db.func.count(ActivityLogEntry.id),
                               db.func.count(db.distinct(ActivityLogEntry.user_id)))
              .filter(ActivityLogEntry.actor_role == 'user', ActivityLogEntry.company_id.isnot(None),
                      ActivityLogEntry.created_at >= d30)
              .group_by(ActivityLogEntry.company_id).order_by(db.func.count(ActivityLogEntry.id).desc()).limit(8).all())
    names = {c.id: c.name for c in Company.query.filter(Company.id.in_([v[0] for v in viewed])).all()} if viewed else {}
    member_q = ActivityLogEntry.query.filter(ActivityLogEntry.actor_role == 'user')
    week_users = (db.session.query(db.func.count(db.distinct(ActivityLogEntry.user_id)))
                  .filter(ActivityLogEntry.actor_role == 'user', ActivityLogEntry.created_at >= d7).scalar() or 0)
    downloads_month = member_q.filter(ActivityLogEntry.category == 'Exports & Downloads',
                                      ActivityLogEntry.created_at >= month_start).count()
    members_total = User.query.filter(User.role == 'user').count()

    return {
        'as_of': at.isoformat(), 'customers_total': len(orgs), 'paying': paying, 'trialing': trialing,
        'new_this_month': new_month, 'by_status': by_status,
        'mrr': {k: round(v) for k, v in mrr.items()}, 'arr': {k: round(v * 12) for k, v in mrr.items()},
        'collected_ytd': ytd, 'collected_last_year_same_period': ly, 'collected_30d': c30,
        'outstanding': outstanding, 'overdue_total': overdue_total, 'overdue_invoices': overdue_list[:6],
        'revenue_by_month': months, 'upcoming': upcoming[:8],
        'trial_conversion': {'trials': len(trial_used), 'converted': converted,
                             'pct': round(converted / len(trial_used) * 100) if trial_used else None},
        'active_clients_30d': active_clients, 'members_total': members_total, 'members_active_7d': week_users,
        'downloads_month': downloads_month,
        'plan_mix': sorted(({**p, 'mrr': round(p['mrr'])} for p in plan_mix.values()), key=lambda p: -p['mrr']),
        'most_viewed': [{'company_id': cid, 'name': names.get(cid, f'Company {cid}'), 'views': n, 'users': u}
                        for cid, n, u in viewed],
        'customers': customers,
        'at_risk': sum(1 for c in customers if c['tone'] == 'red'),
    }


# ---------------------------------------------------------------- routes
def register(app, current_user, require_super_admin):
    import billing

    @app.route('/api/admin/dashboard')
    @require_super_admin
    def admin_dashboard():
        return jsonify(business())

    def _paid(u):
        return 'reports' in billing.features_for(billing.member_tier(u))

    @app.route('/api/home/company')
    def home_company():
        u = current_user()
        if u is None:
            return jsonify({'error': 'Please sign in.'}), 401
        if not _paid(u):
            return jsonify({'error': 'Comparing your company with the market is part of the Professional plan.',
                            'billing': True, 'reason': 'upgrade', 'needs': 'Professional'}), 402
        cid = request.args.get('company_id', type=int) or my_company_id(u)
        if not cid:
            return jsonify({'company': None, 'choices': company_choices()})
        years = _years()
        fy = request.args.get('fiscal_year')
        if fy not in years:
            have = [y for y in years if any(r['company_id'] == cid for r in _rows(y))]
            # Open on the company's latest year that most companies have reported for: a new year with only a
            # handful of reports in makes a thin comparison. The other years stay one click away.
            biggest = max((len(_rows(y)) for y in have), default=0)
            solid = [y for y in have if len(_rows(y)) >= biggest / 2]
            fy = (solid or have)[-1] if have else None
        out = compare(cid, fy) if fy else None
        if out is None:
            return jsonify({'company': None, 'choices': company_choices(),
                            'error': 'That company has no survey figures yet. Choose another.'})
        out['choices'] = company_choices()
        out['saved_company_id'] = my_company_id(u)
        return jsonify(out)

    @app.route('/api/home/my-company', methods=['POST'])
    def home_set_company():
        u = current_user()
        if u is None:
            return jsonify({'error': 'Please sign in.'}), 401
        cid = (request.get_json(silent=True) or {}).get('company_id')
        if cid is not None:
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                return jsonify({'error': 'Choose a company from the list.'}), 400
            if db.session.get(Company, cid) is None:
                return jsonify({'error': 'That company is not on file.'}), 404
        from models_billing import Organisation
        org = db.session.get(Organisation, u.organization_id) if u.organization_id else None
        if org is not None:
            org.company_id = cid
        else:
            u.home_company_id = cid
        db.session.commit()
        return jsonify({'ok': True, 'company_id': cid, 'for': 'organisation' if org is not None else 'account'})