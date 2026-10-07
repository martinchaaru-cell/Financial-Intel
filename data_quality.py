"""Data-quality scoring for survey data - ONE definition shared by the Survey Analytics page, the Manual Entry PDF
forms and the forms zip, so a company never shows two different scores.

It mirrors the per-company "Data Quality" tab (templates/index.html, srDataQualityTab):
  field set   = every hand-enterable survey column (tab_modules.all_metric_labels(), == the page's MAPPING_FIELDS)
  Complete     = has a value AND (no confidence score OR confidence >= 0.75)
  Needs Review = has a value with confidence < 0.75
  Missing      = no value (a free-text field that is blank counts as missing)
  score        = Complete / total fields, as a whole percent
The portfolio "average" is the MEAN of the companies' scores (each company counts once, however many fields it has);
the pooled figure (all complete fields / all fields) is returned too.
Must be called inside a Flask app context (it queries the database).
"""
import tab_modules

LOW_CONF = 0.75


def grade(score):
    """Same bands as the Data Quality tab's Key Insights (75 / 50)."""
    return 'Good' if score >= 75 else 'Fair' if score >= 50 else 'Needs Attention'


def _has_value(v):
    return v is not None and str(v).strip() != ''


def company_quality(row):
    """SurveyCompanyData row (or None) -> counts, score, per-section scores and the labels of the missing fields."""
    conf = (row.field_confidence or {}) if row else {}
    complete = review = missing = 0
    sections, missing_labels = [], []
    for mod in tab_modules.TAB_MODULES:
        fields = list(mod['metrics']) + list(mod.get('text_metrics', []))
        if not fields:
            continue
        sc = sr = sm = 0
        for key, label in fields:
            v = getattr(row, key, None) if row else None
            if not _has_value(v):
                sm += 1
                missing_labels.append(label)
            elif conf.get(key) is not None and conf[key] < LOW_CONF:
                sr += 1
            else:
                sc += 1
        complete, review, missing = complete + sc, review + sr, missing + sm
        n = sc + sr + sm
        sections.append({'section': mod['label'], 'complete': sc, 'needs_review': sr, 'missing': sm, 'total': n,
                         'score': round(sc / n * 100) if n else 0})
    total = complete + review + missing
    score = round(complete / total * 100) if total else 0
    return {'score': score, 'grade': grade(score), 'complete': complete, 'needs_review': review, 'missing': missing,
            'total': total, 'sections': sections, 'missing_fields': missing_labels, 'has_data': row is not None}


def portfolio_quality(fiscal_year=None):
    """Average data quality across the companies that have survey data for `fiscal_year` (default: the newest year
    that has any). Companies with no survey row for that year are not counted - they are listed in `not_on_file`."""
    from models import Company
    from models_survey import SurveyCompanyData, is_full_year
    years = sorted({r.fiscal_year for r in SurveyCompanyData.query.all() if is_full_year(r.fiscal_year)}, reverse=True)
    fy = fiscal_year if fiscal_year in years else (years[0] if years else None)
    out = {'has_data': False, 'fiscal_year': fy, 'available_fiscal_years': years, 'companies_counted': 0,
           'average_score': 0, 'pooled_score': 0, 'grade': grade(0), 'best': None, 'lowest': None,
           'totals': {'complete': 0, 'needs_review': 0, 'missing': 0, 'total': 0}, 'sections': [],
           'companies': [], 'not_on_file': [], 'fields_per_company': len(tab_modules.all_metric_labels())}
    if fy is None:
        return out
    rows = {r.company_id: r for r in SurveyCompanyData.query.filter_by(fiscal_year=fy).all()}
    companies = {c.id: c for c in Company.query.all()}
    per, sec_acc = [], {}
    for cid, row in rows.items():
        c = companies.get(cid)
        if c is None:
            continue
        q = company_quality(row)
        per.append({'company_id': cid, 'name': c.name, 'ticker': c.ticker, 'sector': getattr(c, 'sector', None), **{
            k: q[k] for k in ('score', 'grade', 'complete', 'needs_review', 'missing', 'total')}})
        for s in q['sections']:
            a = sec_acc.setdefault(s['section'], {'scores': [], 'complete': 0, 'total': 0})
            a['scores'].append(s['score'])
            a['complete'] += s['complete']
            a['total'] += s['total']
    out['not_on_file'] = sorted(c.name for cid, c in companies.items() if cid not in rows)
    if not per:
        return out
    per.sort(key=lambda r: (-r['score'], r['name'] or ''))
    tot = {k: sum(p[k] for p in per) for k in ('complete', 'needs_review', 'missing', 'total')}
    avg = round(sum(p['score'] for p in per) / len(per))
    out.update({
        'has_data': True, 'companies_counted': len(per), 'average_score': avg, 'grade': grade(avg),
        'pooled_score': round(tot['complete'] / tot['total'] * 100) if tot['total'] else 0,
        'best': per[0], 'lowest': per[-1], 'totals': tot, 'companies': per,
        'sections': [{'section': k, 'average_score': round(sum(v['scores']) / len(v['scores'])),
                      'complete': v['complete'], 'total': v['total']} for k, v in sec_acc.items()],
    })
    return out


def missing_ranking():
    """Every company, fewest missing survey fields first (ties: higher score, then name). Each company is measured on its
    OWN latest survey year - the year the Survey Report page opens it on. A company with no survey data counts as
    missing every field and sorts last. Used for the Survey Report company drop-down."""
    from models import Company
    from models_survey import SurveyCompanyData
    latest = {}
    for r in SurveyCompanyData.query.order_by(SurveyCompanyData.fiscal_year.desc()).all():
        latest.setdefault(r.company_id, r)
    out = []
    for c in Company.query.all():
        row = latest.get(c.id)
        q = company_quality(row)
        out.append({'company_id': c.id, 'name': c.name, 'fiscal_year': row.fiscal_year if row else None,
                    'missing': q['missing'], 'score': q['score'], 'total': q['total'], 'has_data': q['has_data']})
    out.sort(key=lambda r: (r['missing'], -r['score'], (r['name'] or '').lower()))
    for i, r in enumerate(out, 1):
        r['rank'] = i
    return out