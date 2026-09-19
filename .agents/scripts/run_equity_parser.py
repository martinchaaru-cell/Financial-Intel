from pathlib import Path
import json
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pdf_parse import (
    extract_director_remuneration,
    extract_director_remuneration_detail,
    extract_market_data,
    extract_management_guidance,
    extract_pdf_document,
    extract_principal_risks,
)


REPORTS = [
    Path("attached_assets/Equity-Group-Holdings-PLC-2022-Integrated-Report-and-Financial_1789705029137.pdf"),
    Path("attached_assets/EGH-PLC-2023-Integrated-Report-and-Financial-Statements_1789705029140.pdf"),
]
OUTPUT = Path(".agents/outputs/pdf-inspection")


def run(path: Path) -> None:
    pdf_bytes = path.read_bytes()
    print(f"\n=== {path.name} ({len(pdf_bytes):,} bytes) ===", flush=True)
    started = time.perf_counter()
    extracted = extract_pdf_document(pdf_bytes)
    print(
        f"main statements: {time.perf_counter() - started:.1f}s; "
        f"pages_text={len(extracted['pages_text'])}; "
        f"statements={list(extracted['statements'])}",
        flush=True,
    )
    for name, statement in extracted["statements"].items():
        items = statement["line_items"]
        print(
            f"  {name}: {len(items)} rows; "
            f"normalized={[x['normalized_name'] for x in items if x.get('normalized_name')]}",
            flush=True,
        )

    supplemental = {}
    calls = [
        ("market_data", lambda: extract_market_data(pdf_bytes)),
        ("management_guidance", lambda: extract_management_guidance(pdf_bytes)),
        ("principal_risks", lambda: extract_principal_risks(pdf_bytes)),
        ("director_remuneration", lambda: extract_director_remuneration(pdf_bytes)),
        ("director_remuneration_detail", lambda: extract_director_remuneration_detail(pdf_bytes)),
    ]
    for name, call in calls:
        started = time.perf_counter()
        try:
            value = call()
            supplemental[name] = value
            if isinstance(value, dict):
                summary = list(value)
            elif isinstance(value, list):
                summary = f"{len(value)} rows"
            else:
                summary = repr(value)
            print(f"{name}: {time.perf_counter() - started:.1f}s; {summary}", flush=True)
        except Exception as exc:
            supplemental[name] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"{name}: ERROR {type(exc).__name__}: {exc}", flush=True)

    result = {
        "file": str(path),
        "main_statements": extracted["statements"],
        "supplemental": supplemental,
        "timing_note": "Times are direct parser timings outside the Flask request.",
    }
    out = OUTPUT / f"{path.stem}_parser_output.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {out}", flush=True)


for report in REPORTS:
    run(report)