"""
Removes duplicated FinancialLineItem rows left behind by the old
_save_one_import(), which APPENDED a fresh copy of every line item each
time the same period was re-imported (4 imports = every line 4x).
Fixed going forward in app.py (re-imports now replace); this cleans up
what's already in the database.

Two items are treated as copies only when label, section, amount, page
AND order_index all match - a genuinely repeated label inside ONE
import has a different order_index, so it is kept. Keeps the oldest
copy, repoints any child rows at it, deletes the rest, then recomputes
the period's ratios.

Usage (dry run first, always):
    python dedupe_line_items.py --company "Equity Group Holdings"
    python dedupe_line_items.py --company "Equity Group Holdings" --apply
Omit --company to cover every company. Run against the SAME database the
published app uses.
"""
import argparse
from collections import defaultdict

from app import app, db
from models import Company, FinancialPeriod, FinancialStatement, FinancialLineItem
from ratios import calculate_ratios


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--company', default=None)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    with app.app_context():
        q = Company.query
        if args.company:
            q = q.filter(Company.name.ilike(f'%{args.company}%'))
        total_removed = 0
        for c in q.all():
            for period in FinancialPeriod.query.filter_by(company_id=c.id).all():
                touched = False
                for stmt in FinancialStatement.query.filter_by(period_id=period.id).all():
                    items = (FinancialLineItem.query.filter_by(statement_id=stmt.id)
                             .order_by(FinancialLineItem.id).all())
                    groups = defaultdict(list)
                    for i in items:
                        groups[(i.label, i.section, i.amount, i.page, i.order_index)].append(i)
                    dupes = {k: v for k, v in groups.items() if len(v) > 1}
                    extra = sum(len(v) - 1 for v in dupes.values())
                    if not extra:
                        continue
                    print(f'{c.name} / {period.period_label} / {stmt.statement_type}: '
                          f'{len(items)} items -> {len(items) - extra} '
                          f'({extra} duplicate rows {"removed" if args.apply else "would be removed"})')
                    total_removed += extra
                    if args.apply:
                        touched = True
                        for v in dupes.values():
                            keep, rest = v[0], v[1:]
                            ids = [r.id for r in rest]
                            FinancialLineItem.query.filter(
                                FinancialLineItem.parent_id.in_(ids)
                            ).update({FinancialLineItem.parent_id: keep.id}, synchronize_session=False)
                            FinancialLineItem.query.filter(
                                FinancialLineItem.id.in_(ids)
                            ).delete(synchronize_session=False)
                if touched:
                    db.session.commit()
                    calculate_ratios(period)
        print(f'\n{"Removed" if args.apply else "Dry run - would remove"} {total_removed} duplicate line items.')
        if not args.apply and total_removed:
            print('Re-run with --apply to make these changes.')


if __name__ == '__main__':
    main()