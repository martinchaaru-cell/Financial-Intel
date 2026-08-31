"""
AI narrative generator — Phase 6 step 3.

This is the ONLY module in the reporting pipeline that calls an LLM, and
it is deliberately the last, most replaceable layer. It never sees a raw
line item, a page number, or the statement tree - only the same trimmed
numbers report_docx.py already prints as templated sentences (revenue,
net income, margins, YoY deltas). That's the whole point: the model is
asked to phrase numbers Python already computed and verified, never to
compute or introduce one of its own.

Fully optional. If ANTHROPIC_API_KEY isn't set, the API call fails, or
the model's response doesn't parse as the expected JSON shape, this
returns None and report_docx.py silently falls back to the numbers-only
template it already uses. A report must never fail to generate because
this layer had a bad day.
"""

import os
import json
import re
from typing import Optional

MODEL = os.environ.get('REPORT_NARRATIVE_MODEL', 'claude-sonnet-4-6')
# Swap to 'claude-haiku-4-5-20251001' via REPORT_NARRATIVE_MODEL if you'd
# rather trade a bit of prose quality for lower cost/latency - this task
# (phrasing a handful of numbers) doesn't need the strongest model.


def _trimmed_payload(ctx: dict) -> dict:
    """Only computed/reported figures the templated version already
    uses - no line items, no pages, no statement tree. Keeps the model
    from ever having raw material to invent additional numbers from."""
    return {
        'company_name': ctx['company']['name'],
        'sector': ctx['company']['sector'],
        'period_label': ctx['period']['label'],
        'currency': ctx['period']['currency'],
        'calculated_metrics': {
            name: m['value'] for name, m in ctx['calculated_metrics'].items()
        },
        'year_over_year': {
            name: {'current': d['current'], 'prior': d['prior'],
                   'change_pct': d['change_pct'], 'prior_period_label': d['prior_period_label']}
            for name, d in ctx['yoy'].items()
        },
    }


_SYSTEM_PROMPT = """You draft short, factual sections of a financial report from pre-computed data. You will be given a JSON payload of numbers a Python program already calculated and verified - revenue, net income, ratios, and year-over-year changes.

Rules, no exceptions:
- Use ONLY the numbers in the payload. Never calculate a new figure, percentage, or comparison that isn't already there.
- Never introduce a number, date, or fact that isn't in the payload.
- If the payload is sparse, write a shorter section rather than filling gaps with generic filler or invented context.
- Neutral, factual tone - no promotional language ("impressive", "strong performance" as unearned praise), no speculation about causes unless obviously implied by the data itself (e.g. don't guess WHY revenue grew).
- Output ONLY valid JSON, no markdown fences, no commentary, matching exactly:
{"executive_summary": "2-4 sentences.", "key_findings": ["short finding 1", "short finding 2", ...]}
key_findings should have 3-5 items, each one sentence, each traceable to a specific number in the payload."""


def generate_narrative(ctx: dict) -> Optional[dict]:
    """Returns {'executive_summary': str, 'key_findings': [str, ...]} or
    None on any failure - missing key, network error, bad response shape."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        payload = _trimmed_payload(ctx)

        response = client.messages.create(
            model=MODEL,
            max_tokens=600,
            system=_SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': json.dumps(payload)}],
        )
        text = ''.join(block.text for block in response.content if hasattr(block, 'text'))
        text = re.sub(r'^```(json)?|```$', '', text.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(text)

        if not isinstance(parsed.get('executive_summary'), str) or not isinstance(parsed.get('key_findings'), list):
            return None
        return {
            'executive_summary': parsed['executive_summary'],
            'key_findings': [str(f) for f in parsed['key_findings']][:5],
        }
    except Exception as e:
        # Never let a narrative failure break report generation - log and
        # fall back to the numbers-only template.
        print(f"[report_narrative] AI narrative generation failed, falling back to template: {e}")
        return None
