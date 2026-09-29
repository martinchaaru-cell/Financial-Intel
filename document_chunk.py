"""
DocumentChunk engine — turns a raw PDF into a list of page-scoped
chunks, each tagged with its nearest heading and whether it contains a
table. This is the first stage of the intelligence-extraction pipeline
(see intelligence_extractor.py) and is deliberately dumb and cheap: no
embeddings, no vector store. For a single annual report (a few hundred
pages), ranking a curated keyword list against plain chunk text is
fast and auditable; a vector database solves a retrieval problem this
app doesn't have (millions of documents), and would make it harder,
not easier, to show a person exactly why a figure was picked.

Heading detection uses pdfplumber's own character-level font-size data:
a line whose characters are meaningfully larger than the page's most
common (body-text) font size is treated as a heading. This is the same
signal a person's eye uses when skimming a filing for section breaks,
and it is layout-format-agnostic — it doesn't care whether the heading
says "Directors' Remuneration" or something a company invents next
year, unlike pdf_parse.py's regex-on-known-wording approach.

A DocumentChunk is one page's content, split at heading boundaries -
so a single page with two headings on it becomes two chunks. Chunks
never span multiple pages, even if a heading's content runs onto the
next page as body text with no new heading — the next page becomes
its own chunk carrying forward the same `heading` value, so a
retrieval hit still tells you which page to open.
"""

from dataclasses import dataclass, field
from typing import Optional
import re
import pdfplumber

# Corrects two PDF-authoring/rendering artifacts confirmed on real filings
# this session (see pdf_parse.py's normalize_extracted_line docstring):
# doubled-bold-character headings and mirror-reversed table/heading text
# blocks. Applied to every line this module builds directly from raw
# pdfplumber character data below (this module has its own extraction
# path, separate from pdf_parse.py's extract_pdf_document(), so it needs
# this applied explicitly rather than inheriting it).
from pdf_parse import normalize_extracted_line


@dataclass
class DocumentChunk:
    page: int                          # 1-indexed, matches what a person would see printed/in a PDF viewer
    heading: Optional[str]             # nearest preceding heading-sized line, carried forward across pages until a new one appears
    text: str                          # this chunk's own body text (not including the heading line itself)
    is_table: bool = False             # True if pdfplumber's own table detector found a table anywhere in this chunk's page region
    table_rows: list = field(default_factory=list)  # raw extract_table() rows, only populated when is_table is True


def _body_font_size(page) -> float:
    """Most common character font size on the page - the working
    definition of "body text size" this page uses. Falls back to 10.0
    (a reasonable default) if the page has no extractable characters
    (e.g. a scanned image page with no text layer)."""
    sizes = [round(c.get('size', 0), 1) for c in page.chars if c.get('size')]
    if not sizes:
        return 10.0
    return max(set(sizes), key=sizes.count)


def _detect_column_splits(page, chars, max_columns: int = 4) -> list[float]:
    """Returns 0+ x-coordinates that split this page into columns (2+
    columns == 1+ splits), or [] if the page looks single-column.

    Generalizes the original single-gutter version, which only ever
    found ONE split and only searched the middle third of the page.
    Both limits were confirmed wrong on a real filing: a landscape
    page holding what a reader sees as two physical report pages side
    by side, each ITSELF two-column, so the true layout is 3 columns
    with gutters at roughly 27% and 50% of page width - not 1 gutter
    near center. The narrower gutter sat outside the old middle-third
    search window and would have been missed even alone. Restricting
    to the middle third was a reasonable guess for a plain magazine-
    style 2-column page, but it silently assumed every column split is
    both singular and centered - true often enough to look right in
    testing, but not a real constraint of the document format.

    Detection itself is unchanged: bin character x0 positions into a
    coarse histogram and look for a run of bins whose density is
    <60% of its own immediate left/right neighbors (a local, not
    whole-page, comparison - see the old docstring's reasoning, which
    still holds). What's new:
      - the search band is now most of the page width (8%-92%), not
        just 35%-65%, so off-center and/or multiple gutters are found;
      - a run must be at least min_gutter_bins wide to count - a real
        inter-column gutter persists across several consecutive bins
        at this resolution, whereas the whitespace between two
        adjacent headers in a wide numeric table (e.g. "Sitting
        allowance" | "Other allowances" in a fee table) is usually
        only a bin or two, which this width floor excludes so that
        table doesn't get misread as several one-column tables;
      - when more candidate gutters are found than max_columns - 1
        allows, only the deepest+widest ones are kept (depth * width
        score), so a handful of incidental wide-whitespace moments in
        body text can't fragment a page into dozens of slivers.
    """
    if not chars:
        return []
    width = page.width
    bins = 80
    bin_width = width / bins
    counts = [0] * bins
    for c in chars:
        idx = min(bins - 1, max(0, int(c['x0'] / bin_width)))
        counts[idx] += 1

    search_lo, search_hi = int(bins * 0.08), int(bins * 0.92)
    neighbor_span = 4
    min_gutter_bins = 2

    def neighbors_of(lo, hi):
        left = [n for n in counts[max(0, lo - neighbor_span):lo] if n > 0]
        right = [n for n in counts[hi + 1:hi + 1 + neighbor_span] if n > 0]
        return left + right

    runs = []
    run_start = None
    for i in range(search_lo, search_hi + 1):
        neighbors = neighbors_of(i, i)
        neighbor_avg = sum(neighbors) / len(neighbors) if neighbors else 0
        is_dip = neighbor_avg > 0 and counts[i] <= neighbor_avg * 0.6
        if is_dip:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and i - run_start >= min_gutter_bins:
                runs.append((run_start, i - 1))
            run_start = None
    if run_start is not None and search_hi + 1 - run_start >= min_gutter_bins:
        runs.append((run_start, search_hi))

    if not runs:
        return []

    def run_score(run):
        lo, hi = run
        neighbors = neighbors_of(lo, hi)
        neighbor_avg = sum(neighbors) / len(neighbors) if neighbors else 1
        run_avg = sum(counts[lo:hi + 1]) / (hi - lo + 1)
        return (neighbor_avg - run_avg) * (hi - lo + 1)

    runs.sort(key=run_score, reverse=True)
    runs = runs[:max(0, max_columns - 1)]
    runs.sort(key=lambda r: r[0])

    return [((lo + hi) / 2) * bin_width for lo, hi in runs]


def _line_groups(page):
    """Groups this page's characters into visual lines (by top
    coordinate, +/- a few points of tolerance for sub-pixel jitter),
    each carrying its own average font size. Returns a list of
    (text, avg_size, top) tuples in proper reading order.

    Multi-column layouts (common in annual reports/governance
    sections, and not always just 2 columns - see
    _detect_column_splits) are detected and read top-to-bottom through
    each column in full, left to right, one column at a time - not
    interleaved by raw y-position across the whole page width, which
    is what naive top-coordinate grouping does and is exactly what
    garbles a multi-column filing (a left-column sentence and an
    unrelated right-column sentence at the same vertical height get
    concatenated into one nonsense "line").

    Also drops characters that look like a small-font navigational
    banner (a breadcrumb/table-of-contents strip repeated near the top
    of every page, common in glossy annual reports) - specifically,
    text sitting in the top 6% of the page whose font size is smaller
    than body text. Such a banner is often too small to be caught by
    _looks_like_heading's LARGER-than-body check, and without this
    filter it gets read as ordinary body text and concatenated in
    front of whatever the page's real first paragraph is, corrupting
    the very first "line" of real content."""
    body_size = _body_font_size(page)
    top_band_cutoff = page.height * 0.06
    real_chars = [
        c for c in page.chars
        if not (c['top'] < top_band_cutoff and c.get('size', body_size) < body_size * 0.85)
    ]

    # Column-split detection should only look at the multi-column BODY
    # region, not a page-width running title/header banner - such a
    # banner is wide and its own visual middle is sparse (spaces
    # between words at a large font), which _detect_column_splits'
    # gutter-dip heuristic can mistake for a real column gutter and
    # then use to cut the banner's text in half mid-word.
    #
    # The fixed top_band_cutoff (6% of page height) above only drops a
    # SMALL-font banner and isn't tall enough for every document's
    # actual header block (seen on a real filing: a 2-line, 11-13pt
    # header still extends past that cutoff). Rather than tune a
    # second fixed cutoff - fragile across layouts - find the header
    # block's true extent directly: starting from the top of the page,
    # larger-than-body-size lines are header content; the header block
    # ends at the first top-to-bottom gap where body-sized (or
    # smaller) text appears. Only chars below that point feed column
    # detection. The banner's own characters still flow into
    # real_chars/lines below unchanged either way - only the
    # split-point calculation excludes them.
    def _header_block_bottom(chars, body_size) -> float:
        if not chars:
            return 0.0
        rows = sorted(set(round(c['top'], 1) for c in chars))
        by_row = {}
        for c in chars:
            by_row.setdefault(round(c['top'], 1), []).append(c.get('size', body_size))
        bottom = 0.0
        for top in rows:
            row_sizes = by_row[top]
            if max(row_sizes) <= body_size * 1.05:
                break
            bottom = top
        return bottom

    header_bottom = _header_block_bottom(
        [c for c in real_chars if c['top'] < page.height * 0.20], body_size
    )
    header_chars = [c for c in real_chars if c['top'] <= header_bottom]
    body_chars = [c for c in real_chars if c['top'] > header_bottom]

    split_xs = _detect_column_splits(page, body_chars)
    if not split_xs:
        column_char_sets = [header_chars + body_chars] if header_chars else [body_chars]
    else:
        # The header band is kept as its own always-single-column
        # group rather than assigned to a side of any split: a
        # page-width header line's characters straddle whatever x0
        # the BODY region's split points land on, so applying those
        # splits to header_chars too would still slice a header line
        # in half mid-word even though header_bottom already kept it
        # out of split-point *detection*. Put it first so it still
        # reads before the body columns.
        #
        # split_xs can now hold more than one boundary (see
        # _detect_column_splits) - e.g. a landscape page that's really
        # two physical report pages side by side, each itself
        # two-column, needs 2 splits to read as 3 separate columns
        # rather than blending two of them into one scrambled bucket.
        column_char_sets = [header_chars] if header_chars else []
        boundaries = [float('-inf')] + split_xs + [float('inf')]
        for lo, hi in zip(boundaries[:-1], boundaries[1:]):
            column_char_sets.append([c for c in body_chars if lo <= c['x0'] < hi])

    all_lines = []
    for chars_in_column in column_char_sets:
        chars = sorted(chars_in_column, key=lambda c: (round(c['top'], 0), c['x0']))
        current_top = None
        current_chars = []
        for c in chars:
            top = round(c['top'], 0)
            if current_top is None or abs(top - current_top) > 3:
                if current_chars:
                    text = normalize_extracted_line(''.join(ch['text'] for ch in current_chars).strip())
                    if text:
                        avg_size = sum(ch.get('size', 0) for ch in current_chars) / len(current_chars)
                        all_lines.append((text, avg_size, current_chars[0]['top']))
                current_chars = [c]
                current_top = top
            else:
                current_chars.append(c)
        if current_chars:
            text = normalize_extracted_line(''.join(ch['text'] for ch in current_chars).strip())
            if text:
                avg_size = sum(ch.get('size', 0) for ch in current_chars) / len(current_chars)
                all_lines.append((text, avg_size, current_chars[0]['top']))
    return all_lines


def _table_row_quality(table: list) -> int:
    """Counts rows that have BOTH a real number-looking cell and a
    real label-looking cell (3+ letters) - i.e. rows that actually
    read as "this row is about X and its value is Y", which is what
    intelligence_extractor.py's table-lookup strategy needs.

    Used to choose between pdfplumber's two table-extraction
    strategies (see below) rather than a plain filled-cell ratio: a
    text-position-based extraction can produce MORE non-empty cells
    overall (word-wrap artifacts split one header into many small
    fragment columns) while still being the BETTER result, because
    what matters isn't how full the grid looks but whether a number
    ever ends up in the same row as the label that names it.
    Confirmed on a real filing (KCB Group Plc): the default
    line-ruled-based strategy left every per-director row's name
    cell empty (0 qualifying rows here) while a text-based re-extract
    correctly paired every director's name with their fee amounts
    (13 qualifying rows) despite scoring lower on raw fill ratio."""
    numeric_re = re.compile(r'-?[0-9,]+(\.[0-9]+)?')
    label_re = re.compile(r'[A-Za-z]{3,}')
    count = 0
    for row in table:
        has_num = any(c and numeric_re.fullmatch(c.strip()) for c in row)
        has_label = any(c and label_re.search(c) for c in row)
        if has_num and has_label:
            count += 1
    return count


def chunk_pdf(path: str, start_page: int = 1, end_page: Optional[int] = None) -> list[DocumentChunk]:
    """Reads the PDF at `path` and returns one DocumentChunk per
    (page, heading-section) - i.e. a page with 3 headings on it
    produces 3 chunks, a page with none produces 1 chunk carrying
    forward whatever heading was last seen.

    start_page/end_page (1-indexed, inclusive) let a caller scope a
    huge filing to a page range already known to be relevant (e.g. a
    prior run's table-of-contents lookup) instead of always chunking
    every page - useful for a 50MB, 300-page report where the
    governance section is a fixed ~40-page range once you know where
    it starts. When end_page is None, chunks to the end of the
    document.
    """
    chunks: list[DocumentChunk] = []
    running_heading = None
    heading_seen_pages: dict[str, list[int]] = {}   # tracks WHICH pages each candidate appears on, to tell true page chrome apart from a genuine multi-page section (see running_headers below)

    # First pass: collect every candidate heading line's text so
    # running headers (identical text on many pages, e.g. "THE SOCIAL
    # VALUE WE CONTRIBUTE" printed at the top of a whole section) can
    # be told apart from a genuine one-off heading, before the second
    # pass commits to any chunk boundaries.
    with pdfplumber.open(path) as pdf:
        last_page = end_page or len(pdf.pages)
        page_range = range(start_page, min(last_page, len(pdf.pages)) + 1)

        candidate_lines_by_page = {}
        for page_num in page_range:
            page = pdf.pages[page_num - 1]
            body_size = _body_font_size(page)
            lines = _line_groups(page)
            candidates = [
                (text, size, top) for (text, size, top) in lines
                if _looks_like_heading(text, size, body_size)
            ]
            candidate_lines_by_page[page_num] = (lines, candidates, body_size)
            for text, _size, _top in candidates:
                heading_seen_pages.setdefault(text, []).append(page_num)

        # A candidate is a running header/footer - page chrome repeated
        # on essentially every page (e.g. the company name, or "FOR
        # THE YEAR ENDED ...") - rather than a genuine section heading
        # that happens to span several consecutive pages (e.g. "
        # DIRECTORS' REMUNERATION REPORT" heading 4 pages of one
        # section, or "STATEMENT OF CORPORATE GOVERNANCE (CONTINUED)"
        # heading 12 - not what a person would call a "repeated
        # banner", each is one topic that runs long).
        #
        # Raw repeat count alone can't tell these apart: a report with
        # a 12-page governance section and a 26-page range makes 12
        # look "frequent" by any small fixed threshold, even though
        # every one of those 12 occurrences is the SAME section
        # continuing, not chrome re-printed on unrelated pages.
        #
        # What actually distinguishes them: true page chrome covers
        # close to the FULL page range with no real gaps (it's printed
        # on every page regardless of what section that page belongs
        # to); a genuine multi-page section heading covers a bounded,
        # CONTIGUOUS sub-range and stops - the pages immediately
        # before and after its run carry other candidate text, not
        # this same one. So: only classify a candidate as a running
        # header if (a) it recurs enough to be worth checking at all,
        # and (b) its occurrences span at least running_header_coverage
        # of the full page range, which a real bounded section only
        # does on an already-short document (where the distinction
        # barely matters) but a true banner satisfies by definition.
        page_count = len(candidate_lines_by_page) or 1
        running_header_min_repeats = max(3, page_count // 3)
        running_header_coverage = 0.85  # fraction of the full range a true banner covers
        running_headers = set()
        for text, pages in heading_seen_pages.items():
            if len(pages) < running_header_min_repeats:
                continue
            coverage = len(pages) / page_count
            if coverage >= running_header_coverage:
                running_headers.add(text)

        for page_num in page_range:
            lines, candidates, _body_size = candidate_lines_by_page[page_num]
            candidate_texts = {t for t, _s, _top in candidates} - running_headers

            try:
                tables = pdf.pages[page_num - 1].extract_tables()
            except Exception:
                tables = []

            # The default strategy relies on pdfplumber finding ruled
            # gridlines between columns. Several real filings shade
            # alternating rows instead of ruling them, which leaves
            # that strategy's cells mostly empty except for whichever
            # column happens to sit against an actual rule (confirmed
            # on a real filing: every per-director row in KCB Group
            # Plc's Non-Executive Directors' fee table came back with
            # the name cell empty, only the rightmost "Total" column
            # populated - useless for pairing a number with who it
            # belongs to). When the default result looks that sparse,
            # retry with a text-position-based strategy (splits
            # columns by whitespace gaps between characters, not
            # gridlines) and keep whichever of the two actually pairs
            # numbers with labels better - see _table_row_quality.
            if tables and _table_row_quality(tables[0]) == 0:
                try:
                    text_tables = pdf.pages[page_num - 1].extract_tables(
                        {'vertical_strategy': 'text', 'horizontal_strategy': 'text'}
                    )
                except Exception:
                    text_tables = []
                if text_tables and _table_row_quality(text_tables[0]) > 0:
                    tables = text_tables

            current_heading = running_heading
            current_lines = []

            def flush():
                text = '\n'.join(current_lines).strip()
                if text or current_heading:
                    chunks.append(DocumentChunk(
                        page=page_num, heading=current_heading, text=text,
                        is_table=bool(tables), table_rows=tables[0] if tables else [],
                    ))

            for text, _size, _top in lines:
                if text in candidate_texts:
                    if current_lines or current_heading != running_heading:
                        flush()
                    current_heading = text
                    current_lines = []
                    candidate_texts.discard(text)  # only the first occurrence on this page counts as the break
                else:
                    current_lines.append(text)

            flush()
            running_heading = current_heading

    return chunks


def _looks_like_heading(text: str, size: float, body_size: float) -> bool:
    """A line qualifies as a heading candidate only if it's both
    meaningfully larger than body text AND shaped like a heading -
    filters out large-font pull-quote numbers ("5,000", "9%") and
    other big-but-not-a-heading marketing copy that would otherwise
    fragment a page into dozens of useless micro-chunks.

    "Meaningfully larger" is either a 1.25x ratio OR a flat +0.9pt
    over body size, whichever is met first. The ratio alone misses a
    common InDesign house-style pattern seen in real filings (this
    threshold was tuned against one): body text at 10pt with section
    headings styled at only 11pt - a deliberate, consistent, real
    heading, but only a 1.1x bump that the ratio test alone rejects.
    A flat point-gap catches that case without replacing the ratio
    test - for larger body sizes the ratio condition still dominates
    (e.g. 16pt body needs +4pt to clear 1.25x, well above the flat
    floor), and for small body text (e.g. 7pt captions) a genuine 1pt
    bump to 8pt is exactly the kind of subtle-but-real distinction
    this fix is meant to catch too, not a false positive to guard
    against - the existing word-shape and length checks below still
    filter out pull-quote numbers regardless of which bar they clear."""
    meaningfully_larger = (size > body_size * 1.25) or (size >= body_size + 0.9)
    if not meaningfully_larger:
        return False
    if len(text) > 120 or len(text) < 3:
        return False
    # Reject lines that are mostly digits/punctuation/symbols (stat
    # callouts like "5,000 9% Planet", page numbers, "50 million+") -
    # a heading is made of real words, not numbers-with-a-label. Split
    # into words and require most WORDS (not just characters) to be
    # alphabetic, which catches "5,000 9% Planet" (2 of 3 words are
    # non-alphabetic) while still allowing a heading like "Section 4.2
    # Overview" (2 of 4 words alphabetic-led is fine at word level).
    words = text.split()
    if not words:
        return False
    alpha_words = sum(1 for w in words if w[:1].isalpha())
    if alpha_words < max(1, len(words) * 0.6):
        return False
    return True




def chunks_to_text_index(chunks: list[DocumentChunk]) -> str:
    """Debug/inspection helper - a compact page-by-page heading outline,
    the kind of thing a person would want printed to quickly sanity-check
    that chunking found the right section breaks in a new filing before
    trusting extraction results against it."""
    lines = []
    last_heading = None
    for c in chunks:
        if c.heading != last_heading:
            lines.append(f"p.{c.page}: {c.heading or '(no heading yet)'}")
            last_heading = c.heading
    return '\n'.join(lines)