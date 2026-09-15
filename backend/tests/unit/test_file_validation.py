"""Upload validation, tested as pure logic over three inputs.

Spec §33's warning — *"never trust filename or MIME type alone"* — is a claim about
which of three client-controlled signals decides an upload. That makes `validate` a
function of exactly three values, and this file exercises it as one: no client, no
database, no storage. Everything the attachment route can be made to accept is
reachable from here.

The tests are grouped by the signal that is lying, because that is the axis the design
turns on. A file is accepted only when the extension, the declared type, and the leading
bytes agree — and one of the three was produced by reading the file itself.
"""

import pytest

from app.core.file_validation import (
    ALLOWED,
    EXTENSIONS,
    HEAD_SIZE,
    MAX_FILENAME_LENGTH,
    UnsupportedUpload,
    content_disposition,
    normalize_content_type,
    sanitize_filename,
    sniff,
    validate,
)

pytestmark = pytest.mark.unit

# The real leading bytes of each accepted type, shorter than `HEAD_SIZE` on purpose:
# nothing past the signature is read.
PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
GIF87A = b"GIF87a"
GIF89A = b"GIF89a"
PDF = b"%PDF-"

# What a Windows executable starts with. The canonical "this is not what it says it is".
MZ = b"MZ\x90\x00"


def _validate(filename: str, declared: str | None, head: bytes) -> str:
    return validate(filename=filename, declared_content_type=declared, head=head)


# ---------------------------------------------------------------------------
# Every accepted type, by its bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "declared", "head"),
    [
        ("screenshot.png", "image/png", PNG),
        ("photo.jpg", "image/jpeg", JPEG),
        ("photo.jpeg", "image/jpeg", JPEG),
        ("loop.gif", "image/gif", GIF87A),
        ("loop.gif", "image/gif", GIF89A),
        ("report.pdf", "application/pdf", PDF),
        # Text has no signature, so it is accepted on the strength of the other two
        # signals — and only because the bytes match no other type. See the test below.
        ("notes.txt", "text/plain", b"hello, this is a log"),
        ("server.log", "text/plain", b"2026-09-15 ERROR something"),
        ("export.csv", "text/plain", b"a,b,c\n1,2,3\n"),
    ],
)
def test_an_allowed_type_is_identified_from_its_bytes(
    filename: str, declared: str, head: bytes
) -> None:
    """The detected type is returned, and it is what the download will be served as."""
    assert _validate(filename, declared, head) == declared


def test_the_signature_is_read_from_the_start_of_the_file() -> None:
    """A signature that appears later in the file is not a signature.

    `sniff` compares a prefix, and this is the assertion that it is a prefix rather than
    a substring search — the difference between a PNG and a text file that mentions one.
    """
    assert _validate("notes.txt", "text/plain", b"see the bytes \x89PNG\r\n\x1a\n later")


def test_every_allowed_extension_is_owned_by_exactly_one_type() -> None:
    """`EXTENSIONS` is built from `ALLOWED`, so a collision would be silent.

    A second type claiming `.txt` would overwrite the first in the dict, and the
    validator would then accept one of two media types depending on declaration order.
    """
    claimed = [extension for allowed in ALLOWED.values() for extension in allowed.extensions]

    assert len(claimed) == len(set(claimed))
    assert set(EXTENSIONS) == set(claimed)


@pytest.mark.parametrize("media_type", sorted(ALLOWED))
def test_an_uppercase_or_parameterised_type_is_normalised_not_refused(
    media_type: str,
) -> None:
    """A `Content-Type` header's casing and parameters are not significant.

    `text/plain; charset=utf-8` is what a browser actually sends for a text file, and
    refusing it would refuse every legitimate upload from a form.
    """
    extension = sorted(ALLOWED[media_type].extensions)[0]
    head = ALLOWED[media_type].signatures[0] if ALLOWED[media_type].signatures else b"plain"

    assert _validate(f"file{extension}", f"{media_type.upper()}; charset=utf-8", head)


# ---------------------------------------------------------------------------
# The extension is lying
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename", ["payload.exe", "script.sh", "archive.zip", "page.html", "clip.mov", "noextension"]
)
def test_an_extension_outside_the_allowlist_is_refused(filename: str) -> None:
    """The allowlist is the first gate, and it is closed by default.

    `page.html` is in the list deliberately. It is the extension whose acceptance would
    turn this endpoint into stored XSS: served from the API's own origin, in the same
    origin the refresh cookie is scoped to.
    """
    with pytest.raises(UnsupportedUpload, match="extension"):
        _validate(filename, "text/plain", b"whatever")


def test_a_traversal_shaped_name_is_refused_when_its_extension_is_not_allowed() -> None:
    """`../../evil.exe` fails on the extension.

    Worth stating plainly, because the intuitive reading is wrong: `../../evil.png` is
    **accepted**, and correctly so. Its extension is allowed, its declared type agrees,
    and its bytes are a real PNG — every signal a validator can check is consistent. The
    traversal is prevented where it would matter, by `build_key` never putting a
    client-supplied string into a path, and the display name is reduced to `evil.png` by
    `sanitize_filename`. See `test_a_directory_part_never_survives_sanitizing`.
    """
    with pytest.raises(UnsupportedUpload, match="extension"):
        _validate("../../evil.exe", "application/octet-stream", MZ)


# ---------------------------------------------------------------------------
# The declared type is lying
# ---------------------------------------------------------------------------


def test_a_declared_type_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(UnsupportedUpload, match="declared type"):
        _validate("notes.txt", "application/x-msdownload", b"hello")


def test_a_declared_type_that_disagrees_with_the_extension_is_refused() -> None:
    """A `.png` announced as a PDF is refused rather than resolved in the client's favour.

    Both values are in the allowlist and both are the client's own claims, so there is
    no basis on which to prefer one — and choosing would be choosing arbitrarily on
    behalf of an uploader who has already been caught contradicting themselves.
    """
    with pytest.raises(UnsupportedUpload, match="not"):
        _validate("screenshot.png", "application/pdf", PDF)


# ---------------------------------------------------------------------------
# The bytes are lying — the check that makes the warning true
# ---------------------------------------------------------------------------


def test_an_executable_renamed_to_png_is_refused() -> None:
    """The whole point of sniffing.

    A name of `screenshot.png` and a declared type of `image/png` pass the first two
    checks. Only the third — reading the file — tells the truth.
    """
    with pytest.raises(UnsupportedUpload, match="signature"):
        _validate("screenshot.png", "image/png", MZ)


def test_a_png_renamed_to_txt_is_refused() -> None:
    """The mirror image, and the reason `text/plain` requires the bytes to match nothing.

    Text has no signature, so "the bytes announced nothing" is only meaningful if the
    bytes are also checked against every *other* type's signature. Otherwise the
    signatureless type is a hole through the allowlist.
    """
    with pytest.raises(UnsupportedUpload, match="bytes are"):
        _validate("notes.txt", "text/plain", PNG)


def test_a_pdf_whose_signature_is_missing_is_refused() -> None:
    """A type that *has* a signature must present it.

    Otherwise `report.pdf` containing arbitrary bytes would be accepted, which is the
    same hole approached from the other side.
    """
    with pytest.raises(UnsupportedUpload, match="signature"):
        _validate("report.pdf", "application/pdf", b"this is not a pdf at all")


def test_a_zero_byte_file_has_no_signature_and_is_refused() -> None:
    """No rule of its own is needed, and that is worth knowing.

    An empty body fails sniffing for every type — including the signatureless one, where
    it fails the "must announce nothing *and* be text" test by having nothing to read.
    The service additionally refuses a zero-length upload for the signatureless path,
    because `attachments.size_bytes` is constrained to be positive.
    """
    with pytest.raises(UnsupportedUpload):
        _validate("screenshot.png", "image/png", b"")


def test_sniff_reports_nothing_for_bytes_it_does_not_recognise() -> None:
    """`None` is a fact about the bytes, not a failure — the caller decides what it means."""
    assert sniff(b"just some text") is None
    assert sniff(b"") is None
    assert sniff(PNG) == "image/png"


def test_the_signature_window_is_the_declared_constant() -> None:
    """The service reads exactly `HEAD_SIZE` bytes, so the table must fit inside it."""
    longest = max(
        len(signature) for allowed in ALLOWED.values() for signature in allowed.signatures
    )

    assert longest <= HEAD_SIZE


# ---------------------------------------------------------------------------
# The display name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        # A directory part never survives, in either separator convention. This is what
        # makes a traversal-shaped *allowed* name harmless.
        ("../../evil.png", "evil.png"),
        ("/etc/passwd", "passwd"),
        # Windows, because this is what a browser on Windows actually sends.
        ("C:\\Users\\me\\shot.png", "shot.png"),
        ("..\\..\\evil.png", "evil.png"),
        # Leading dots: hidden on POSIX, and `.` and `..` are reachable as names. The
        # basename step does not catch these, which is why the strip is separate.
        ("...hidden.png", "hidden.png"),
        (".bashrc", "bashrc"),
        # CR and LF are header injection in a `Content-Disposition` value.
        ("injected\r\nX-Evil: 1.png", "injectedX-Evil: 1.png"),
    ],
)
def test_a_directory_part_never_survives_sanitizing(supplied: str, expected: str) -> None:
    assert sanitize_filename(supplied) == expected


def test_bidi_and_zero_width_characters_are_removed() -> None:
    """A right-to-left override in a name is a filename that displays as something else.

    `gpj.exe` reversed by U+202E renders as `gpj.exe` with the extension on the left —
    the classic disguise. The Unicode category strip removes it without a hand-written
    list of characters to remember.
    """
    cleaned = sanitize_filename("photo\u202egnp.exe.png")

    assert "\u202e" not in cleaned
    assert cleaned.endswith(".png")


def test_quotes_and_backslashes_are_removed() -> None:
    """Either character ends the quoted string in `Content-Disposition` early.

    What follows would then be read as header syntax, which is how a filename becomes a
    response-splitting primitive.
    """
    cleaned = sanitize_filename('say "hi" \\ there.png')

    assert '"' not in cleaned
    assert "\\" not in cleaned


def test_an_empty_name_is_replaced_rather_than_allowed_through() -> None:
    """`attachments.filename` is non-null and the UI renders it, so it is never blank."""
    assert sanitize_filename("") == "attachment"
    assert sanitize_filename("...") == "attachment"
    assert sanitize_filename("\u200b") == "attachment"


def test_a_name_is_bounded_to_what_the_column_holds() -> None:
    """255 characters, past which the insert would be an integrity error."""
    cleaned = sanitize_filename("a" * 400 + ".png")

    assert len(cleaned) == MAX_FILENAME_LENGTH


def test_a_name_at_the_limit_is_untouched() -> None:
    name = "a" * (MAX_FILENAME_LENGTH - 4) + ".png"

    assert sanitize_filename(name) == name


def test_sanitizing_does_not_change_a_name_that_is_already_clean() -> None:
    """A control against an over-eager cleaner: ordinary names must survive intact."""
    assert sanitize_filename("Screenshot 2026-09-15 at 14.02.11.png") == (
        "Screenshot 2026-09-15 at 14.02.11.png"
    )


# ---------------------------------------------------------------------------
# The response header
# ---------------------------------------------------------------------------


def test_the_disposition_is_always_an_attachment() -> None:
    """`inline` would ask the browser to render a file a customer supplied.

    Rendered in the API's own origin, that is stored XSS against the same origin the
    refresh cookie is scoped to.
    """
    assert content_disposition("holiday.png").startswith("attachment;")


def test_a_non_ascii_name_is_carried_in_both_forms() -> None:
    """HTTP headers are latin-1, so one form cannot do the job.

    The plain `filename` must be ASCII or the response raises on the way out; the
    `filename*` form carries the real name for any client written this century.
    """
    header = content_disposition("réunion.pdf")

    assert "filename*=UTF-8''r%C3%A9union.pdf" in header
    assert "r_union.pdf" in header, "the ASCII fallback replaces the accented character"


def test_the_disposition_value_is_ascii_encodable() -> None:
    """The property that matters, asserted directly against the encoder."""
    content_disposition("réunion-日本語.pdf").encode("latin-1")


def test_a_quoted_name_cannot_escape_the_header() -> None:
    """Defence in depth: `sanitize_filename` strips quotes, and this shows why it must.

    The header is built by interpolation, so a quote reaching it would end the string
    early — the same failure as CRLF injection, reached by a different character.
    """
    header = content_disposition(sanitize_filename('a"; X-Evil: 1; ".png'))

    assert header.count('"') == 2, "exactly the two quotes this format requires"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("text/plain", "text/plain"),
        ("TEXT/PLAIN", "text/plain"),
        ("text/plain; charset=utf-8", "text/plain"),
        ("  text/plain  ", "text/plain"),
        (None, ""),
        ("", ""),
    ],
)
def test_a_content_type_is_reduced_to_its_bare_media_type(
    declared: str | None, expected: str
) -> None:
    assert normalize_content_type(declared) == expected
