# replit.md

## Overview

This is **Finance Intel**, a financial and governance intelligence platform for NSE-listed (Nairobi Securities
Exchange) companies. It has two linked layers: a **financial statement layer** (import, store, and analyze company
financials, ratios, risks, and management guidance) and a **survey layer** (board composition, director
remuneration, governance/committee structure, and policy disclosures, benchmarked across companies for a fiscal
year). Data is populated either from hand-authored plain-text filings, or extracted automatically from uploaded
annual-report PDFs via an evidence-scored extraction pipeline.

The app is a single Flask application with a server-rendered, single-page HTML/JS front end — not a
client/server monorepo. There is no separate build step for the frontend.

> Note: this repository originally held a different, unrelated project (a React/Express sports-prediction
> engine). That codebase was fully replaced by the current Finance Intel application; nothing from the old
> project remains in use.

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Layout

```
app.py                    → Flask app: routes, auth, scoring, import orchestration
templates/index.html      → Single-page frontend (dashboard, company views, survey pages) — vanilla JS, no bundler
models.py                 → Core financial-statement schema (Company, FinancialPeriod/Statement/LineItem, etc.)
models_survey.py          → Survey-layer schema (SurveyCompanyData, policies, directors, evidence conflicts, activity log)
pdf_parse.py               → PDF text/table extraction + condensed-format parsing for financial statements
document_chunk.py          → Heading/column-aware PDF chunking shared by the extraction pipeline
intelligence_extractor.py  → Evidence-scored field extraction from annual-report chunks (survey + policy fields)
remuneration_ontology.py   → Canonical field ontology / synonym mapping used to normalize disclosures
survey_data_parse.py       → Parser for the hand-authored Survey Data condensed format
survey_aggregate.py        → Benchmarking, percentiles, compa-ratios, historical trends, data-quality rollups
company_directory.py       → Static NSE company roster + fuzzy name matching for uploaded PDFs
ratios.py                  → Financial ratio calculations from stored statement data
report_context.py          → Single read-path for reporting data (feeds every export format consistently)
report_docx.py / survey_report_pdf.py / report_narrative.py → Word/PDF export + narrative commentary generation
migrate_to_v2.py, migrate_v3_evidence_review.py,
migrate_v4_extraction_score.py                              → Additive Postgres migrations (see schema-drift note below)
CONDENSED_FORMAT_SPEC.md / SURVEY_FORMAT_SPEC.md            → Plain-text filing format specs (financials vs. survey data)
attached_assets/           → Reference files, including sample annual-report PDFs used for extraction testing
```

### Frontend (templates/index.html)

- **Rendering**: Server-rendered by Flask (`render_template`), single HTML file containing all page views and
  vanilla JS/CSS — no React, no Vite, no separate client build.
- **Key views**: Survey Administration Dashboard (the default/landing view), company detail pages, financial
  statement/period views, comparison and portfolio views, and the Survey (board/remuneration benchmarking) pages.
- **Access model**: pages themselves are open to guests (no login wall on `GET /`); actions are gated
  server-side (see Auth below), with the UI hiding controls a guest/user role can't use.

### Backend (app.py)

- **Runtime**: Flask, run directly with `python app.py` (see `.replit`); no separate dev/prod server split.
- **API pattern**: 60+ routes under `/api/...` for companies, financial periods/statements, survey data, PDF
  import (financial and survey/policy), field-level review, overview/analytics, alerts, and multi-format exports
  (DOCX, PDF, CSV).
- **Import pipeline**: three ingestion paths — hand-authored condensed-text files, financial-statement PDFs, and
  survey/remuneration-policy PDFs — all converging on the same Company/FinancialPeriod or SurveyCompanyData
  records.
- **Auth**: session-based, three-tier role model — `admin` (upload/edit/delete/view/download), `user`
  (view/download only), and an implicit `guest` (view only, no account, no session) for anyone not logged in.
  `require_role()` enforces this server-side on every gated route; hiding a button in the UI is not treated as
  sufficient enforcement on its own.
- **Extraction quality tracking**: every imported source document gets an `extraction_score` compared against a
  low-score threshold, and individual extracted fields carry a confidence value plus a review status
  (`pending` / `approved` / `rejected`, with corrections recorded) via `POST /api/survey-data/<company_id>/
  <fiscal_year>/review`. Conflicting candidate values from the same document are recorded in an
  `EvidenceConflict` table with its own open/resolved lifecycle, rather than silently discarded. All review and
  import activity is written to an immutable `ActivityLogEntry` audit log, kept separate from the survey data so
  history survives later re-imports.

### Database

- **Database**: PostgreSQL, required in deployment (`DATABASE_URL` environment variable). A SQLite fallback
  (`sqlite:///test.db`) exists only for local/dev convenience when `DATABASE_URL` is unset — migration scripts
  explicitly assume Postgres and do not support SQLite.
- **ORM**: Flask-SQLAlchemy, models split across `models.py` (financial statements) and `models_survey.py`
  (survey/governance layer).
- **Schema evolution**: the app relies on `db.create_all()` at startup, which only creates missing tables — it
  never alters existing ones. Adding a column to a table that already exists in a live database requires an
  explicit, idempotent migration script (see `migrate_v3_evidence_review.py`, `migrate_v4_extraction_score.py`),
  run once from the Flask app context. This constraint is documented in `.agents/memory/
  sqlalchemy-schema-drift.md` and should be followed for any future model field additions.

### Key Design Decisions

1. **One canonical read-path for reporting**: `report_context.py` is the only place that reads financial data
   out of the database for report generation; every export format (DOCX, PDF, and any future format) reads the
   same plain dict it returns, so figures stay consistent across formats.
2. **Ontology-normalized disclosures**: `remuneration_ontology.py` maps company-specific wording variants onto a
   shared set of canonical fields, so extraction and benchmarking work across annual reports that describe the
   same disclosure differently.
3. **Extraction is confidence-scored, not silently trusted**: PDF-derived fields carry a confidence score and
   review status; low-confidence fields and multi-candidate conflicts are surfaced for review rather than
   written straight into the live record.
4. **Guest-open, action-gated**: the app is browsable with zero login; every state-changing action is
   role-checked server-side regardless of what the frontend shows.

## External Dependencies

### Required Services

- **PostgreSQL Database**: connected via `DATABASE_URL`. Required for deployment; the app also provisions
  `postgresql-16` as a Replit module.

### Key Python Dependencies

Flask, Flask-SQLAlchemy, psycopg2-binary, pdfplumber, pypdf, pymupdf, python-docx, reportlab, pandas, openpyxl,
werkzeug.

### Environment Variables

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | Yes (in deployment) | PostgreSQL connection string; falls back to local SQLite if unset |
| `SESSION_SECRET` | Recommended | Flask session signing key (defaults to a dev-only placeholder if unset) |

### Not Yet Present

No Dockerfile, container config, or CI/CD workflow exists in the repository, and there is no automated test
suite. These remain open work rather than implemented infrastructure.
