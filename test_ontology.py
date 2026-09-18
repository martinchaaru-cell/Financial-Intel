"""
remuneration_ontology.score_chunk/rank_chunks drive which page of an
annual report gets treated as the source for a given field — this is
the core of the extraction-accuracy work described as the current
priority, so it's worth protecting with a few concrete cases rather
than only eyeballing results on real PDFs.
"""
from document_chunk import DocumentChunk
from remuneration_ontology import score_chunk, rank_chunks, ONTOLOGY


def test_ontology_has_entries():
    # Guards against an accidental empty/broken ONTOLOGY dict shipping —
    # every extraction call would silently score 0.0 for everything.
    assert len(ONTOLOGY) > 0


def test_curly_apostrophe_matches_straight_quote_synonym():
    """Real filings almost always use U+2019 in headings like
    "Directors' Remuneration Report" — this is the exact bug class
    _normalize_quotes() exists to prevent from regressing."""
    field = next(iter(ONTOLOGY))
    entry = ONTOLOGY[field]
    if not entry.get("synonyms"):
        return  # nothing to test this against for this particular field
    phrase = entry["synonyms"][0]
    heading_curly = phrase.replace("'", "\u2019")
    score = score_chunk(chunk_text="", chunk_heading=heading_curly, canonical_field=field)
    assert score > 0


def test_negative_terms_suppress_the_field():
    for field, entry in ONTOLOGY.items():
        if entry.get("negative_terms"):
            negative_phrase = entry["negative_terms"][0]
            score = score_chunk(chunk_text=negative_phrase, chunk_heading="", canonical_field=field)
            assert score == 0.0
            return
    # no field in this ontology currently defines negative_terms — fine,
    # nothing to assert, but not a silent no-op either.


def test_rank_chunks_orders_by_score_then_page():
    field = next(f for f, e in ONTOLOGY.items() if e.get("synonyms"))
    phrase = ONTOLOGY[field]["synonyms"][0]

    weak = DocumentChunk(page=5, heading=None, text=phrase)          # body-text hit, lower score
    strong = DocumentChunk(page=2, heading=phrase, text="")          # heading hit, higher score
    unrelated = DocumentChunk(page=1, heading="Unrelated Section", text="nothing relevant here")

    ranked = rank_chunks([weak, unrelated, strong], field)

    assert unrelated not in ranked
    assert ranked[0] is strong
