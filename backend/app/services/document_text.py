"""§22's middle three steps — extract, clean, chunk. No network, no database, no vendor.

§22's pipeline is *"Document upload -> text extraction -> cleaning -> chunking -> embedding
generation -> store chunks + vectors -> document becomes searchable"*, and this module is the
three steps in the middle. Everything here is a pure function of its arguments, which is what
lets `tests/unit/test_document_text.py` assert on a chunk boundary without a database, a
fixture, or an event loop — and what makes re-chunking a document a function call rather than a
re-ingestion.

**Three formats, three readers, and the third is hand-written.** A PDF goes through `pypdf`; an
HTML page through the small `HTMLParser` subclass below; everything else in
`READABLE_MEDIA_TYPES` — `text/plain`, `text/markdown`, and the XML spellings a server may name
a page with — is decoded. A type outside that set is refused rather than decoded into mojibake,
which is the answer an image deserves. The HTML reader is hand-written for the reason that
module gives about signature tables: this needs to walk a tag stream and drop `script` and
`style`, BeautifulSoup does far more, and one native-free dependency that does exactly the job
is cheaper than a library nobody can be sure is not rendering something.

**The chunker is where the retrieval quality of this feature is decided**, so its two numbers
are the two worth arguing about. `CHUNK_TARGET_TOKENS` is the size a passage should be: too
small and a passage cannot contain an answer, too large and one vector has to stand for several
subjects at once. `CHUNK_OVERLAP_TOKENS` is how much of one passage is repeated at the start of
the next, and it exists because a boundary is drawn by arithmetic rather than by meaning — a
sentence stating a condition can sit exactly across one, and neither half alone would retrieve.
The overlap is a real cost: those tokens are embedded twice and billed twice. It is kept small
on purpose.

**`token_count` is an estimate, and it is stated as one everywhere it is used.** It comes from
OpenAI's published ratio for English prose, four characters to a token, and it exists to size a
chunk rather than to predict a bill — the billed figure is the vendor's own count, and the
ledger records that one. **`tiktoken` was considered and rejected**: it downloads its BPE file
on first use, which would make the first ingestion of a fresh deployment depend on a third
party's CDN being up, and a chunk boundary is not a place where exactness buys anything. The
target is conservative by roughly four times against `text-embedding-3-small`'s 8191-token
input limit, so an estimate that is wrong about a pathological document still cannot exceed it.

**A document that yields no text is an error, and that is the point.** A scanned PDF extracts to
nothing, and the honest outcome is a *failed* document whose reason says so — not an empty
document that indexed cleanly and answers nothing. §22's readers of that field are admins, so
the reason is written for one.
"""

import io
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final

from app.core.file_validation import normalize_content_type

#: The media types this module can turn into text, and therefore the set a knowledge document
#: or a fetched page has to be in. Three readers cover them: `pypdf` for the first, the tag
#: walker below for the HTML pair, and a decode for everything else — which is `text/plain`,
#: `text/markdown`, and the two XML spellings a server may name a page with.
#:
#: **Public, and `app/services/url_fetch.py` imports it rather than restating it.** That module
#: refuses a response whose media type this pipeline cannot read, and it had its own copy of
#: this set; two copies of "what can be read" is how a page comes to be fetched as a text
#: document and refused as an unreadable one a second later.
READABLE_MEDIA_TYPES: Final = frozenset(
    {
        "application/pdf",
        "application/xhtml+xml",
        "application/xml",
        "text/html",
        "text/markdown",
        "text/plain",
        "text/xml",
    }
)

#: OpenAI's published ratio for English prose — roughly four characters to a token. See the
#: module docstring: this is an estimate used to size a chunk, never a figure that reaches a
#: ledger or a bill.
_CHARS_PER_TOKEN: Final = 4

#: The size a passage should be, in estimated tokens. §22's six document kinds are policy
#: documents and guides rather than articles, and 800 tokens is a page or so of prose: large
#: enough to hold a condition and its exception together, small enough that one vector still
#: describes one subject.
CHUNK_TARGET_TOKENS: Final = 800

#: How much of the end of one chunk is repeated at the start of the next, in estimated tokens.
#: A boundary is drawn by arithmetic and a relevant sentence can straddle it; the overlap is
#: what keeps such a sentence retrievable from the following passage. Those tokens are embedded
#: twice and billed twice, which is why the number is small rather than absent.
CHUNK_OVERLAP_TOKENS: Final = 100

#: The largest a single segment may be, in estimated tokens. A segment is never allowed to fill
#: a whole chunk, so that the overlap prepended to the following chunk always leaves room for at
#: least one segment after it — which is what keeps the invariant in `chunk` provable rather
#: than merely likely, and what stops a run of empty overlap chunks.
_MAX_SEGMENT_TOKENS: Final = CHUNK_TARGET_TOKENS - CHUNK_OVERLAP_TOKENS

#: The most chunks one document may produce. Two hundred and fifty thousand tokens is a
#: five-hundred-page manual, well past anything §22 lists, and the cap exists because a
#: document that grew without one would spend an unbounded amount of the organization's money
#: on embeddings in a single task. Reaching it fails the document rather than truncating it:
#: a document that was silently indexed halfway answers nothing past the cut, and nothing would
#: say so.
MAX_CHUNKS: Final = 500

#: Elements whose contents are code or styling rather than text. `title` is deliberately not
#: here — it is inside `head`, and on a fetched page it is often the most informative sentence.
_SKIPPED_ELEMENTS: Final = frozenset({"script", "style", "noscript", "template"})

#: Elements that end a line. A paragraph, a list item, and a table row are separate thoughts; a
#: reader that joined them with a space would produce a page-long run-on that chunks badly and
#: reads worse.
_LINE_BREAK_ELEMENTS: Final = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

#: Sentence ends, for the case where a single paragraph is too long to be one segment. The
#: lookbehind keeps the punctuation with the sentence it ends.
_SENTENCE_END: Final = re.compile(r"(?<=[.!?])\s+")

#: Runs of blank lines, and the paragraph break a document's own structure is written with.
_PARAGRAPH_BREAK: Final = re.compile(r"\n\s*\n")

#: Horizontal whitespace — everything `\s` matches except a newline, which is structure.
_HORIZONTAL_SPACE: Final = re.compile(r"[^\S\n]+")

#: Three or more newlines, collapsed to two so a blank line stays a paragraph break and never
#: becomes a run of them.
_BLANK_RUN: Final = re.compile(r"\n{3,}")


class DocumentTextError(Exception):
    """This document cannot be turned into passages, and the message says why.

    Module-local and dependency-free, like `app/core/file_validation.py`'s `UnsupportedUpload`
    and for the same reason: the reason has to reach `knowledge_documents.error_message`, and a
    pure module should not be the one deciding how an error is reported. The message is written
    for the admin who sees it, so it names the document's condition rather than an internal
    step.
    """


@dataclass(frozen=True)
class TextChunk:
    """One retrievable passage: where it sits in the document, its text, and its estimated size.

    `index` is zero-based, matching `knowledge_chunks.chunk_index`'s own comment and its
    `>= 0` check. `token_count` is the estimate the module docstring describes, and it is
    positive by construction — the column's check constraint requires it, and a chunk with no
    text is not a chunk this module produces.
    """

    index: int
    content: str
    token_count: int


def estimated_tokens(text: str) -> int:
    """The estimated token count of `text` — four characters to a token, rounded up.

    Rounded up rather than down, so a one-character segment counts as one token rather than
    zero and every non-empty string has a positive size. That keeps `token_count > 0` true for
    every chunk without a special case at the column.
    """
    return -(-len(text) // _CHARS_PER_TOKEN)


def clean(text: str) -> str:
    """`text` with the noise extraction leaves behind removed, and paragraph structure kept.

    Five passes, each one undoing something a real document does:

    * **Line endings** are normalised to `\\n`. A PDF's text and a form's text can arrive with
      `\\r\\n`, and every later step — the blank-line collapse especially — is written for `\\n`.
    * **Control characters go, by Unicode category rather than by a list**, so the C1 range a
      Latin-1 decode produces from a Windows-1252 file goes too. **Whitespace is the exception,
      and it is tested by `str.isspace` rather than by naming `\\n`**: a tab is category `Cc` and
      is also the separator in every table a PDF extracts, so dropping it here — one pass before
      the one written to collapse it — joins `col1\\tcol2` into one word. The `Cc` members that
      are whitespace are layout and are collapsed below; the ones that are not are noise.
    * **Non-breaking spaces** become ordinary ones, and runs of horizontal whitespace collapse
      to a single space. Extraction pads and indents; the model does not need the layout.
    * **Each line is stripped**, which is where the leading indentation of a code block or a
      table column goes.
    * **Runs of blank lines** collapse to one blank line, so a paragraph break stays a paragraph
      break and a page break does not become a page of newlines.

    The result is stripped, and `""` is a legal return — a document whose extraction produced
    nothing gets there, and `extract` is what turns that into an error.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        character
        for character in text
        if character.isspace() or unicodedata.category(character) != "Cc"
    )
    text = text.replace("\u00a0", " ")
    text = _HORIZONTAL_SPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RUN.sub("\n\n", text).strip()


def extract(data: bytes, content_type: str) -> str:
    """The document's text, cleaned — or `DocumentTextError` if it has none.

    The one entry point the ingestion worker uses, because extraction and cleaning are one
    decision from a caller's point of view: what a document says. The three readers are chosen
    by media type, and the type is normalised first so a fetched page's
    `text/html; charset=utf-8` is not mistaken for an unknown format.

    **A type this cannot read is refused rather than decoded.** `READABLE_MEDIA_TYPES` is the
    set, and the refusal is what stops an image — which the shared upload validator admits,
    because a ticket attachment may be a screenshot — from being run through Latin-1 and
    stored as a document full of mojibake. `app/services/knowledge_service.py` refuses one at
    upload time with the same set; this is the second check, and it is here because the worker
    is the last place that can still tell what the bytes are.

    **An empty result raises rather than returning `""`**, and the distinction matters: this
    function returning an empty string would let a scanned PDF become a `completed` document
    with no chunks, which is a document that looks ingested and can never answer anything. The
    reason names the likely cause, because the reader of it is an admin looking at a list of
    documents and asking why one of them failed.
    """
    media_type = normalize_content_type(content_type)
    if media_type not in READABLE_MEDIA_TYPES:
        raise DocumentTextError(
            f"this pipeline cannot extract text from {media_type or 'an unknown type'}; it "
            "reads PDFs, HTML pages, and plain text"
        )

    if media_type == "application/pdf":
        raw = _from_pdf(data)
    elif media_type in {"text/html", "application/xhtml+xml"}:
        raw = _from_html(data)
    else:
        raw = _decode(data)

    text = clean(raw)
    if not text:
        raise DocumentTextError(
            "no text could be extracted from this document — a scanned or image-only PDF "
            "extracts to nothing, and OCR is not part of this pipeline"
        )
    return text


def chunk(text: str) -> list[TextChunk]:
    """Split `text` into passages of at most `CHUNK_TARGET_TOKENS`, each overlapping the last.

    **The unit of assembly is a segment**: a paragraph where the document has one, a sentence
    where a paragraph is too long to fit a chunk, and a character run where even one sentence
    is. A chunk is a greedy accumulation of segments up to the target, and the overflow rule is
    the whole of the algorithm:

    * when the next segment does not fit, the current chunk is closed;
    * the new chunk **opens with the trailing segments of the one just closed** — as many as fit
      `CHUNK_OVERLAP_TOKENS`, so a boundary that fell inside one thought leaves that thought
      readable in both passages;
    * the overlap counts against the target, so a chunk is bounded by `CHUNK_TARGET_TOKENS`
      including its repetition. That is why `_MAX_SEGMENT_TOKENS` is the target *minus* the
      overlap: every segment is guaranteed to fit after a full overlap, which makes "a chunk
      never exceeds the target" a property of the arithmetic rather than of the input.

    **The size measured is the joined text's, not the sum of its parts**, and the difference is
    the blank line `_as_chunk` writes between segments: summing parts would let a chunk of many
    short segments exceed the target by one separator per join, while `token_count` — recomputed
    from the joined string — reported the larger number. `_joined_tokens` is that measure, and
    the `_MAX_SEGMENT_TOKENS` bound below is what makes the invariant hold rather than nearly
    hold.

    **A segment is never split unless it has to be**, and that is the other half of retrieval
    quality: a boundary drawn mid-sentence is a passage that reads as a fragment, and one drawn
    mid-word is worse. Paragraphs are tried first, then sentences, then characters — so the
    only cuts a reader will notice are the ones the document itself did not make possible.

    Returns `[]` for text with no content in it, which `extract` has already refused; a caller
    that reaches this function directly gets the empty answer rather than an exception, and the
    cap below is the refusal that belongs to this step.
    """
    chunks: list[TextChunk] = []
    buffer: list[str] = []

    for segment in _segments(text):
        if buffer and _joined_tokens([*buffer, segment]) > CHUNK_TARGET_TOKENS:
            chunks.append(_as_chunk(len(chunks), buffer))
            buffer = _overlap_tail(buffer)
        buffer.append(segment)

    if buffer:
        chunks.append(_as_chunk(len(chunks), buffer))

    if len(chunks) > MAX_CHUNKS:
        raise DocumentTextError(
            f"the document produced {len(chunks)} passages, more than the {MAX_CHUNKS} this "
            "pipeline will embed; split it into smaller documents"
        )
    return chunks


def _joined_tokens(segments: Sequence[str]) -> int:
    """The estimated size of `segments` as one chunk — the measure `_as_chunk` stores.

    A chunk's segments are joined by a blank line, so the string that is embedded is two
    characters longer per join than the parts it was built from. Measuring the sum instead would
    let a chunk of many short segments sit above the target while the number the column holds
    said so — the two disagreeing is the drift `_as_chunk`'s own docstring is about.
    """
    return estimated_tokens("\n\n".join(segments))


def _decode(data: bytes) -> str:
    """The bytes as text: UTF-8 first, then Latin-1 — the encoding that cannot fail.

    A document that is not valid UTF-8 is far more likely to be a Windows-1252 file from a
    desktop word processor than to be corrupt, and Latin-1 maps every byte to a character, so
    every document gets read. The cost is that a curly quote from that file becomes a control
    character, which `clean` then drops — a lost apostrophe rather than a lost document, which
    is the direction this decision should point.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _from_pdf(data: bytes) -> str:
    """The text of every page, in order, joined by a blank line.

    `pypdf` is imported inside the function so that importing this module — which the test
    suite does, and which a deployment that never ingests a PDF also does — does not pay for
    a library it may not use.

    **An encrypted PDF is refused rather than attempted.** A reader cannot extract from one
    without the password, and this pipeline has nowhere to ask for it; a file that arrived
    password-protected is a fact to report rather than a puzzle to solve. Everything else that
    can go wrong in here — a truncated file, a page whose content stream is unreadable — is
    `PyPdfError`, and it becomes the same refusal with a different reason, because from a
    caller's position the document is equally unusable.
    """
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise DocumentTextError("the PDF is password-protected and cannot be read")
        pages = [page.extract_text() for page in reader.pages]
    except PyPdfError as exc:
        raise DocumentTextError(f"the PDF could not be read ({type(exc).__name__})") from exc

    return "\n\n".join(pages)


def _from_html(data: bytes) -> str:
    """The visible text of a page, with `script` and `style` contents left out."""
    parser = _PageText()
    parser.feed(_decode(data))
    parser.close()
    return parser.text


def _segments(text: str) -> list[str]:
    """`text` broken into the pieces a chunk is assembled from — see `chunk`.

    Paragraphs first, because a document's own breaks are the best boundaries it has. A
    paragraph that still would not fit a chunk is split at sentence ends, and a sentence that
    would not is split by characters. Each result is stripped and empty pieces are dropped, so
    the caller never accumulates a segment that contributes nothing.
    """
    segments: list[str] = []
    for paragraph in _PARAGRAPH_BREAK.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if estimated_tokens(paragraph) <= _MAX_SEGMENT_TOKENS:
            segments.append(paragraph)
        else:
            segments.extend(_split_sentences(paragraph))
    return segments


def _split_sentences(paragraph: str) -> list[str]:
    """One overlarge paragraph as its sentences, splitting any single sentence that is still
    too long.

    A sentence too long for a segment is not hypothetical — a table row extracted from a PDF
    can be a thousand words with no full stop in it — and `_hard_split` is what handles it.
    """
    parts: list[str] = []
    for sentence in _SENTENCE_END.split(paragraph):
        sentence = sentence.strip()
        if not sentence:
            continue
        if estimated_tokens(sentence) <= _MAX_SEGMENT_TOKENS:
            parts.append(sentence)
        else:
            parts.extend(_hard_split(sentence))
    return parts


def _hard_split(sentence: str) -> list[str]:
    """`sentence` cut into character runs that fit a segment, preferring a space to a letter.

    The last resort, and the only place a cut can land inside a sentence. It looks back for the
    final space within the limit, so a word is broken only when a single word is longer than a
    whole segment — which is a base64 blob or a URL, and there is no better place to cut either.
    """
    limit = _MAX_SEGMENT_TOKENS * _CHARS_PER_TOKEN
    parts: list[str] = []
    remaining = sentence
    while len(remaining) > limit:
        cut = remaining.rfind(" ", 0, limit + 1)
        parts.append(remaining[: cut if cut > 0 else limit].strip())
        remaining = remaining[cut if cut > 0 else limit :].strip()
    if remaining:
        parts.append(remaining)
    return [part for part in parts if part]


def _overlap_tail(segments: Sequence[str]) -> list[str]:
    """The trailing segments of a closed chunk that fit `CHUNK_OVERLAP_TOKENS`.

    Best-effort, and empty is a legal answer: a chunk whose every trailing segment is a long
    one carries no repetition forward. The alternative — taking a segment that overflows the
    overlap budget — would break the invariant `chunk` relies on, because a chunk would then be
    free to start with more than the overlap it reserved room for.
    """
    tail: list[str] = []
    total = 0
    for segment in reversed(segments):
        size = estimated_tokens(segment)
        if total + size > CHUNK_OVERLAP_TOKENS:
            break
        tail.insert(0, segment)
        total += size
    return tail


def _as_chunk(index: int, segments: Sequence[str]) -> TextChunk:
    """One buffer as a `TextChunk`, numbered and sized.

    Segments are joined by a blank line. Within a paragraph a segment is the paragraph itself
    and the join never happens; across a split paragraph the sentences were too long to share a
    passage anyway, and a blank line is the strongest boundary plain text has. The sizing is
    recomputed from the joined text rather than summed from the parts, so the number matches
    the string that is actually stored — those two disagreeing is exactly the kind of drift the
    column would never report.
    """
    content = "\n\n".join(segments)
    return TextChunk(index=index, content=content, token_count=estimated_tokens(content))


class _PageText(HTMLParser):
    """A page's text, collected from the tag stream.

    Four behaviours, and each one is a decision:

    * **`script`, `style`, `noscript` and `template` are skipped**, entire. A page's JavaScript
      is not its content, and a template's markup is not text anyone reads.
    * **Block-level elements end a line.** A paragraph and a list item are separate thoughts,
      and joining them with a space produces one run-on sentence that both chunks and reads
      badly. The set is a frozenset rather than a regex because it is matched per tag, thousands
      of times per page.
    * **`handle_startendtag` is overridden**, because the base implementation calls the start
      and end handlers and would therefore write `<br/>` as two line breaks — one paragraph
      break in the cleaned text, where the page meant a line.
    * **Everything else is kept, including `title`.** It lives in `head`, and dropping `head`
      wholesale — the obvious simplification — would throw away the one line that usually says
      what the page is about.

    `convert_charrefs` is left at its default of on, so `&amp;` arrives as `&` and the text is
    what a reader would see rather than what the source spells.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        #: Depth of nesting inside a skipped element, rather than a boolean: a `<script>` that
        #: contains the string `</script>` in a comment is malformed, and a counter that never
        #: went negative is one less way for the rest of a page to disappear.
        self._skipping = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_ELEMENTS:
            self._skipping += 1
        elif tag in _LINE_BREAK_ELEMENTS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # One line break rather than the base implementation's two — see the class docstring.
        if tag not in _SKIPPED_ELEMENTS and tag in _LINE_BREAK_ELEMENTS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_ELEMENTS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _LINE_BREAK_ELEMENTS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._parts.append(data)

    @property
    def text(self) -> str:
        """Everything collected, as one string. Un-cleaned: `extract` cleans it."""
        return "".join(self._parts)
