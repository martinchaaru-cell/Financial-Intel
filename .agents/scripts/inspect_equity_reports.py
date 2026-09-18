from pathlib import Path
import json
import re

import fitz


REPORTS = [
    Path("attached_assets/Equity-Group-Holdings-PLC-2022-Integrated-Report-and-Financial_1789705029137.pdf"),
    Path("attached_assets/EGH-PLC-2023-Integrated-Report-and-Financial-Statements_1789705029140.pdf"),
]

OUTPUT = Path(".agents/outputs/pdf-inspection")
KEYWORDS = {
    "financial_statements": [
        "consolidated statement of profit or loss",
        "statement of financial position",
        "statement of cash flows",
        "statement of changes in equity",
    ],
    "market_data": [
        "share price",
        "market capitalisation",
        "market capitalization",
        "shareholder",
        "investor information",
    ],
    "guidance": ["outlook", "guidance", "forward-looking"],
    "risks": ["principal risks", "principal risk", "risk management"],
    "remuneration": ["directors' remuneration", "directors remuneration", "remuneration"],
    "ratios_kpis": [
        "return on average equity",
        "return on equity",
        "cost to income",
        "earnings per share",
        "key performance indicators",
        "five year",
        "five-year",
    ],
    "segments": ["segment information", "operating segments"],
}


def compact(text: str, limit: int = 420) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


def render_page(doc, page_no: int, output_path: Path) -> None:
    page = doc[page_no]
    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
    pix.save(output_path)


def inspect(path: Path) -> None:
    doc = fitz.open(path)
    pages = []
    hits = {name: [] for name in KEYWORDS}
    for page_no, page in enumerate(doc):
        text = page.get_text("text") or ""
        lower = text.lower()
        pages.append({"page": page_no + 1, "text": text})
        for group, words in KEYWORDS.items():
            if any(word in lower for word in words):
                hits[group].append(page_no + 1)

    stem = path.stem.replace(" ", "_")
    text_path = OUTPUT / f"{stem}_pages.json"
    text_path.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")

    summary = {
        "file": str(path),
        "pages": len(doc),
        "metadata": doc.metadata,
        "keyword_pages": hits,
        "page_previews": [
            {"page": item["page"], "preview": compact(item["text"])}
            for item in pages
            if item["text"].strip()
        ],
    }
    (OUTPUT / f"{stem}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    selected = {1}
    for page_numbers in hits.values():
        selected.update(page_numbers[:4])
    for page_no in sorted(selected):
        if 1 <= page_no <= len(doc):
            render_page(doc, page_no - 1, OUTPUT / f"{stem}_page_{page_no:03d}.png")

    print(f"\n{path.name}: {len(doc)} pages")
    for group, page_numbers in hits.items():
        print(f"  {group}: {page_numbers[:30]}{' ...' if len(page_numbers) > 30 else ''}")
    print(f"  text: {text_path}")


for report in REPORTS:
    inspect(report)