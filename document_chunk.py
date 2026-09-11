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
import pdfplumber


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


def _detect_column_split(page, chars) -> Optional[float]:
    """Returns an x-coordinate to split this page into left/right
    columns, or None if the page looks single-column. Detection: bin
    character x0 positions into a coarse histogram across the page
    width and look for a local dip in the middle third of the page -
    the visual gutter between two text columns has noticeably fewer
    characters starting there than its own immediate neighbors, even
    though real body text (indentation, justified-text overhang, a
    stray hyphen) means the gutter is rarely a literal zero-count bin.
    Compares each middle bin to the bins just outside the search
    window (its true left/right column neighbors) rather than a
    whole-page average, since the two columns can differ in width/
    density and a global average dilutes a real gutter's contrast."""
    if not chars:
        return None
    width = page.width
    bins = 60
    bin_width = width / bins
    counts = [0] * bins
    for c in chars:
        idx = min(bins - 1, max(0, int(c['x0'] / bin_width)))
        counts[idx] += 1

    middle_lo, middle_hi = int(bins * 0.35), int(bins * 0.65)
    # Neighbor density: the average of a few bins just left of the
    # search window and a few bins just right of it - a much more
    # local, robust baseline than averaging every non-middle bin
    # (which can include page-margin zeros or a much narrower second
    # column and skew the comparison).
    neighbor_span = 4
    left_neighbors = [n for n in counts[max(0, middle_lo - neighbor_span):middle_lo] if n > 0]
    right_neighbors = [n for n in counts[middle_hi + 1:middle_hi + 1 + neighbor_span] if n > 0]
    neighbors = left_neighbors + right_neighbors
    if not neighbors:
        return None
    neighbor_avg = sum(neighbors) / len(neighbors)
    if neighbor_avg <= 0:
        return None
    dip_threshold = neighbor_avg * 0.6   # a gutter bin has <60% of its immediate neighbors' density

    best_run = None
    run_start = None
    for i in range(middle_lo, middle_hi + 1):
        if counts[i] <= dip_threshold:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                best_run = (run_start, i - 1)
            run_start = None
    if run_start is not None:
        best_run = (run_start, middle_hi)
    if best_run is None:
        return None
    gap_center_bin = (best_run[0] + best_run[1]) / 2
    return gap_center_bin * bin_width


def _line_groups(page):
    """Groups this page's characters into visual lines (by top
    coordinate, +/- a few points of tolerance for sub-pixel jitter),
    each carrying its own average font size. Returns a list of
    (text, avg_size, top) tuples in proper reading order.

    Two-column layouts (common in annual reports/governance sections)
    are detected via _detect_column_split and read top-to-bottom
    through the LEFT column in full, then top-to-bottom through the
    RIGHT column - not interleaved by raw y-position across the whole
    page width, which is what naive top-coordinate grouping does and
    is exactly what garbles a two-column filing (a left-column
    sentence and an unrelated right-column sentence at the same
    vertical height get concatenated into one nonsense "line").

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

    # Column-split detection should only look at the two-column BODY
    # region, not a page-width running title/header banner - such a
    # banner is wide and its own visual middle is sparse (spaces
    # between words at a large font), which _detect_column_split's
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

    split_x = _detect_column_split(page, body_chars)
    if split_x is None:
        column_char_sets = [header_chars + body_chars] if header_chars else [body_chars]
    else:
        # The header band is kept as its own always-single-column
        # group rather than assigned to a side of the split: a
        # page-width header line's characters straddle whatever x0
        # the BODY region's split point lands on, so applying that
        # split_x to header_chars too would still slice a header
        # line in half mid-word even though header_bottom already
        # kept it out of split-point *detection*. Put it first so it
        # still reads before the two body columns.
        column_char_sets = [header_chars] if header_chars else []
        column_char_sets += [
            [c for c in body_chars if c['x0'] < split_x],
            [c for c in body_chars if c['x0'] >= split_x],
        ]

    all_lines = []
    for chars_in_column in column_char_sets:
        chars = sorted(chars_in_column, key=lambda c: (round(c['top'], 0), c['x0']))
        current_top = None
        current_chars = []
        for c in chars:
            top = round(c['top'], 0)
            if current_top is None or abs(top - current_top) > 3:
                if current_chars:
                    text = ''.join(ch['text'] for ch in current_chars).strip()
                    if text:
                        avg_size = sum(ch.get('size', 0) for ch in current_chars) / len(current_chars)
                        all_lines.append((text, avg_size, current_chars[0]['top']))
                current_chars = [c]
                current_top = top
            else:
                current_chars.append(c)
        if current_chars:
            text = ''.join(ch['text'] for ch in current_chars).strip()
            if text:
                avg_size = sum(ch.get('size', 0) for ch in current_chars) / len(current_chars)
                all_lines.append((text, avg_size, current_chars[0]['top']))
    return all_lines


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
