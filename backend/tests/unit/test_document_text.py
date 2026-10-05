"""§22's three pure steps, tested without a database, a provider, or an event loop.

`app/services/document_text.py` is the module this phase's retrieval quality rests on, and it
is deliberately the only one in the knowledge pipeline with **no dependency on anything** — so
this file can assert what a chunk boundary looks like without a fixture, and the assertions are
about the text rather than about a row.

The claim most of these tests exist for is the chunker's invariant: **a chunk never exceeds
`CHUNK_TARGET_TOKENS`, and the number stored beside it is the number its own string has.** The
second half is the one that is easy to lose — `_as_chunk` recomputes the size from the joined
text rather than summing its parts, and this file is what makes that recomputation a checked
fact instead of a comment.

The PDF cases are load-bearing for §22's first document kind rather than for coverage: a
password-protected PDF and a truncated one both have to arrive as `failed` documents with a
reason an admin can read, not as a `completed` document with no passages. The fixture below
writes a real, minimal PDF by hand, which is what lets the extraction path be tested at all
without a binary file in the repository.
"""

import io
from itertools import pairwise

import pytest

from app.services import document_text
from app.services.document_text import (
    CHUNK_OVERLAP_TOKENS,
    CHUNK_TARGET_TOKENS,
    DocumentTextError,
    chunk,
    clean,
    estimated_tokens,
    extract,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _pdf(text: str) -> bytes:
    """A minimal, valid one-page PDF carrying `text` in Helvetica.

    Written by hand rather than committed as a binary, for the reason
    `app/core/file_validation.py` gives about signature tables: what is needed here is one page
    of extractable text, and a checked-in PDF is a file nobody can review, diff, or explain.
    The cross-reference offsets are computed rather than faked, so `pypdf` reads this the way it
    reads any other file and the test is of the real path.

    `text` must be ASCII and free of parentheses and backslashes — the content stream is a
    literal PDF string, and escaping it is not what these tests are about.
    """
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    start_xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start_xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def _encrypted_pdf(text: str) -> bytes:
    """`_pdf(text)`, encrypted with a password this pipeline has nowhere to ask for."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_pdf(text))))
    writer.encrypt("hunter2")

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _paragraph(number: int) -> str:
    """A distinct paragraph of about fifty estimated tokens, sized to share a chunk.

    Fifty tokens matters: `CHUNK_OVERLAP_TOKENS` is a hundred, so a paragraph this size is one
    the overlap can carry forward — which is what makes the overlap test below assert a repeated
    passage rather than an empty tail. Sixteen of them fill a chunk exactly.
    """
    return f"p{number} " + "word " * 35 + "end"


def _document(paragraphs: int) -> str:
    """`paragraphs` distinct paragraphs, separated the way `clean` leaves a document's breaks."""
    return "\n\n".join(_paragraph(number) for number in range(paragraphs))


# ---------------------------------------------------------------------------
# The estimate
# ---------------------------------------------------------------------------


def test_the_token_estimate_rounds_up_so_nothing_is_size_zero() -> None:
    """Four characters to a token, and the rounding direction is a decision.

    Rounded down, a one-character segment would count as zero tokens and a chunk of them could
    grow without the target noticing anything. The column's `token_count > 0` check is the same
    decision made in the schema, and this is where it is kept true.
    """
    assert estimated_tokens("") == 0
    assert estimated_tokens("a") == 1
    assert estimated_tokens("abcd") == 1
    assert estimated_tokens("abcde") == 2
    assert estimated_tokens("a" * 400) == 100


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------


def test_cleaning_normalises_the_line_endings_a_real_file_arrives_with() -> None:
    """A PDF and a form both produce `\\r\\n`, and every later pass is written for `\\n`."""
    assert clean("a\r\nb\rc") == "a\nb\nc"


def test_cleaning_drops_control_characters_but_keeps_the_newline() -> None:
    """By category rather than by a list, so the C1 range a Latin-1 decode produces goes too.

    The newline is the exception because it is structure: a document's paragraphs are what the
    chunker cuts on, and a pass that removed whitespace wholesale would leave one paragraph.
    """
    assert clean("a\x00b\x1bc\nd") == "abc\nd"
    assert clean("a\x9db") == "ab", "a C1 control, which only a Latin-1 decode produces"


def test_cleaning_turns_non_breaking_spaces_and_runs_of_whitespace_into_one_space() -> None:
    """Extraction pads and indents; the model does not need the layout.

    The tab is the case worth naming: it is category `Cc` \u2014 a control character \u2014 *and*
    the separator in every table a PDF extracts, so a pass that dropped control characters
    wholesale would join `col1\\tcol2` into one word one pass before the whitespace collapse
    meant to turn it into a space. The non-breaking space is here for the other half of the same
    decision.
    """
    assert clean("a\u00a0b   c\t\td") == "a b c d"


def test_cleaning_strips_each_line_and_collapses_blank_runs() -> None:
    """Leading indentation goes, and a page break does not become a page of newlines.

    Two newlines survive because that is a paragraph break — the unit the chunker assembles
    from — while three or more collapse to two, so a document cannot use blank space to win
    chunk boundaries it did not earn.
    """
    assert clean("   a  \n\n\n\n   b   ") == "a\n\nb"


def test_cleaning_an_empty_document_returns_empty_rather_than_raising() -> None:
    """`clean` is a pure transform; the refusal belongs to `extract`, one function up.

    Asserted because the two would otherwise be easy to merge, and the refusal's message names
    the cause — a scanned PDF — which is a fact about a whole document rather than about a
    string.
    """
    assert clean("   \n\n\t  ") == ""


# ---------------------------------------------------------------------------
# Extraction, by media type
# ---------------------------------------------------------------------------


def test_plain_text_is_decoded_and_cleaned() -> None:
    """The simplest reader: UTF-8 where it can be, Latin-1 where it cannot."""
    assert extract(b"Refunds take 5 days.\n", "text/plain") == "Refunds take 5 days."


def test_a_content_type_with_parameters_is_normalised_before_the_reader_is_chosen() -> None:
    """A fetched page arrives as `text/html; charset=utf-8`, and that is still HTML.

    Without the normalisation the type matches nothing in `READABLE_MEDIA_TYPES` and a perfectly
    ordinary web page is refused as unreadable — a failure that would look like a bug in the
    fetcher rather than in this comparison.
    """
    assert extract(b"<p>Five days.</p>", "text/html; charset=utf-8") == "Five days."


def test_markdown_is_read_as_its_own_text() -> None:
    """§22's refund policies are written in markdown, and markdown *is* text — no reader needed.

    The `text/markdown` entry is what admits it, and it is the entry Phase X added to
    `file_validation.ALLOWED`; this asserts the other half, that the pipeline can read what the
    validator admits.
    """
    assert extract(b"# Refunds\n\nFive working days.", "text/markdown") == (
        "# Refunds\n\nFive working days."
    )


def test_text_that_is_not_utf8_is_decoded_rather_than_refused() -> None:
    """A Windows-1252 file from a desktop word processor is far likelier than a corrupt one.

    Latin-1 maps every byte, so every document gets read; the cost is that the smart quotes in
    such a file arrive as control characters, which `clean` drops. A lost apostrophe is a
    cheaper failure than a lost document, and this asserts the direction of the trade.
    """
    assert extract("caf\xe9 policy".encode("latin-1"), "text/plain") == "café policy"


def test_a_type_the_pipeline_cannot_read_is_refused_by_name() -> None:
    """An image is refused rather than run through Latin-1 into mojibake.

    The shared upload validator admits `image/png` because a ticket attachment may be a
    screenshot, so this check is what stops one from being *also* a knowledge document full of
    nonsense — and the message names the type, because the person reading it is an admin looking
    at why their upload failed.
    """
    with pytest.raises(DocumentTextError, match="image/png"):
        extract(b"\x89PNG\r\n\x1a\n", "image/png")


def test_a_document_with_no_text_is_an_error_rather_than_an_empty_document() -> None:
    """§22's scanned PDF, and the whole reason this refusal exists.

    An empty string would let the document finish `completed` with no passages: it would appear
    in the list as ingested, and it could never answer anything. The message names the likely
    cause, because "why did this one fail" is the question an admin is actually asking.
    """
    with pytest.raises(DocumentTextError, match="OCR is not part of this pipeline"):
        extract(b"   \n\n  ", "text/plain")


# ---------------------------------------------------------------------------
# Extraction, HTML
# ---------------------------------------------------------------------------


def test_html_yields_its_visible_text_and_not_its_script() -> None:
    """A page's JavaScript is not its content, and a template's markup is not text anyone reads.

    All four skipped elements are asserted at once, because they are one decision — the set is a
    set — and a reader that dropped `script` and kept `style` would index a stylesheet as
    documentation without failing anything.
    """
    page = (
        b"<html><head><title>Refund policy</title>"
        b"<style>p { color: red; }</style></head><body>"
        b"<script>var refund = 5;</script>"
        b"<noscript>Enable JS</noscript>"
        b"<template><p>Draft</p></template>"
        b"<h1>Refunds</h1><p>Five working days.</p></body></html>"
    )

    text = extract(page, "text/html")

    assert "Refunds" in text
    assert "Five working days." in text
    assert "color: red" not in text
    assert "var refund" not in text
    assert "Enable JS" not in text
    assert "Draft" not in text


def test_html_keeps_the_title_because_it_is_usually_the_most_informative_line() -> None:
    """The one element inside `head` that is text, and dropping `head` wholesale would lose it.

    A fetched policy page's `<title>` is often the only place its subject is stated — the visible
    heading may be the company's name — so a reader that skipped `head` as markup would discard
    the sentence that makes the page retrievable at all.
    """
    page = b"<head><title>Refund policy</title></head><body><p>x</p></body>"

    assert extract(page, "text/html") == "Refund policy\nx"


def test_html_block_elements_end_a_line_and_a_self_closing_break_ends_one() -> None:
    """Paragraphs and list items are separate thoughts, and a run-on chunks badly.

    `<br/>` is asserted separately because it is the case the hand-written reader had to
    override a base-class method for: the default `handle_startendtag` calls the start *and* end
    handlers, which would write two breaks where the page meant one.
    """
    text = extract(b"<p>one</p><p>two</p><ul><li>a</li><li>b</li></ul>", "text/html")

    assert text == "one\n\ntwo\n\na\n\nb"
    assert extract(b"<p>one<br/>two</p>", "text/html") == "one\ntwo"


def test_html_character_references_arrive_as_the_characters_a_reader_sees() -> None:
    """`&amp;` is `&` on the page, and the model should read what the reader reads."""
    assert extract(b"<p>Terms &amp; conditions</p>", "text/html") == "Terms & conditions"


# ---------------------------------------------------------------------------
# Extraction, PDF
# ---------------------------------------------------------------------------


def test_a_pdf_yields_the_text_of_its_pages() -> None:
    """§22's product documentation, through `pypdf`, end to end on a real file."""
    assert extract(_pdf("Refunds take 5 working days"), "application/pdf") == (
        "Refunds take 5 working days"
    )


def test_a_password_protected_pdf_is_refused_with_its_own_reason() -> None:
    """A reader cannot extract from one without the password, and nowhere here can ask for it.

    A file that arrived password-protected is a fact to report rather than a puzzle to solve,
    and the reason is its own rather than the generic one because the admin's next action
    differs: this document needs its protection removed rather than its text checked.
    """
    with pytest.raises(DocumentTextError, match="password-protected"):
        extract(_encrypted_pdf("Refunds take 5 working days"), "application/pdf")


def test_a_truncated_pdf_is_refused_rather_than_stored_as_nothing() -> None:
    """The `PyPdfError` path, and the shape of every other way a PDF can be unusable.

    A file whose transfer was cut short parses to nothing useful, and the alternative to
    refusing it is a document with no passages that reads as a successful ingestion. No message
    is asserted here, because whether a truncated stream raises or simply yields no text is
    `pypdf`'s business — what this file owns is that both arrive as the same refusal.
    """
    truncated = _pdf("Refunds take 5 working days")[:120]

    with pytest.raises(DocumentTextError):
        extract(truncated, "application/pdf")


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def test_a_short_document_is_one_chunk_numbered_from_zero() -> None:
    """`chunk_index`'s own check is `>= 0`, so the numbering starts where the column does."""
    chunks = chunk("Refunds take five working days.")

    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert chunks[0].content == "Refunds take five working days."
    assert chunks[0].token_count == estimated_tokens(chunks[0].content)


def test_text_with_no_content_produces_no_chunks() -> None:
    """`extract` has already refused an empty document; this step returns rather than raises.

    The two are different questions — "is there a document here" and "how is it divided" — and
    the second has an answer for the empty case, which is none.
    """
    assert chunk("") == []
    assert chunk("   \n\n  ") == []


def test_every_chunk_is_within_the_target_and_sized_by_its_own_string() -> None:
    """The invariant, asserted on the value the column stores rather than on the sum of parts.

    This is the test the `_joined_tokens` measure exists for. Summing segment sizes would leave
    a chunk of many short segments above the target while the recomputed `token_count` reported
    the larger number — the two disagreeing is exactly the drift a reader would never see.
    """
    chunks = chunk(_document(40))

    assert len(chunks) > 1
    for chunk_ in chunks:
        assert chunk_.token_count == estimated_tokens(chunk_.content)
        assert chunk_.token_count <= CHUNK_TARGET_TOKENS
        assert chunk_.token_count > 0


def test_chunks_are_numbered_in_order_without_gaps() -> None:
    """`(document_id, chunk_index)` is the key a re-ingestion replaces rows by, so the sequence
    has to be dense and start at zero — a gap would be a passage nothing ever writes again."""
    chunks = chunk(_document(40))

    assert [chunk_.index for chunk_ in chunks] == list(range(len(chunks)))


def test_a_boundary_repeats_the_end_of_the_previous_chunk() -> None:
    """The overlap, asserted as a repeated paragraph rather than as a token count.

    A boundary is drawn by arithmetic and a relevant sentence can sit exactly across it; the
    overlap is what keeps such a sentence retrievable from the following passage. Paragraphs
    here are fifty estimated tokens against an overlap budget of a hundred, so at least one
    always fits — and the assertion is that the passage a reader sees at the end of one chunk is
    also one of the passages at the start of the next.
    """
    chunks = chunk(_document(40))
    # How many of these paragraphs the overlap can carry, derived rather than assumed: the
    # trailing paragraph of a chunk is the last entry of the tail `_overlap_tail` built.
    carried = CHUNK_OVERLAP_TOKENS // estimated_tokens(_paragraph(0))

    assert carried >= 2
    for previous, following in pairwise(chunks):
        trailing = previous.content.split("\n\n")[-1]
        assert trailing in following.content.split("\n\n")[:carried]
        assert trailing != previous.content, "the whole chunk was repeated"


def test_a_paragraph_too_large_for_a_chunk_is_split_at_its_sentences() -> None:
    """The first fallback, and the reason a boundary is not always a paragraph break.

    A policy written as one enormous paragraph is common enough, and cutting it in the middle of
    a sentence where a full stop was available is the difference between a passage that reads
    like a fragment and one that does not. The sentences are distinct strings, so this asserts
    they survived whole.
    """
    sentences = " ".join(f"Sentence number {number} about refunds." for number in range(120))
    chunks = chunk(sentences)

    assert len(chunks) > 1
    assert all(chunk_.token_count <= CHUNK_TARGET_TOKENS for chunk_ in chunks)
    # Every sentence is present somewhere, and none was cut mid-word.
    joined = " ".join(chunk_.content for chunk_ in chunks)
    assert joined.count("about refunds.") >= 120


def test_a_document_over_the_chunk_cap_is_refused_rather_than_truncated() -> None:
    """The cap exists because an unbounded document is an unbounded embedding bill.

    Reaching it fails the document instead of indexing the first `MAX_CHUNKS` passages: a
    document that was silently cut answers nothing past the cut, and nothing in the product
    would say so. The cap is patched down rather than built up — a real 501-chunk document is
    half a million tokens of test data to prove a branch that is one comparison.
    """
    monkeypatch_max = 2
    original = document_text.MAX_CHUNKS
    document_text.MAX_CHUNKS = monkeypatch_max
    try:
        with pytest.raises(DocumentTextError, match=f"more than the {monkeypatch_max}"):
            chunk(_document(40))
    finally:
        document_text.MAX_CHUNKS = original


def test_a_single_word_longer_than_a_segment_is_cut_rather_than_dropped() -> None:
    """The last resort, and the only cut that lands inside a word.

    A base64 blob or a very long URL has no space to cut at, and there is no better place to
    cut either — so the assertion is that the text survives, not that it survives unbroken.
    """
    blob = "x" * (3 * document_text._MAX_SEGMENT_TOKENS * 4)
    chunks = chunk(blob)

    assert len(chunks) > 1
    assert sum(len(chunk_.content) for chunk_ in chunks) >= len(blob)
    assert all(chunk_.token_count <= CHUNK_TARGET_TOKENS for chunk_ in chunks)
