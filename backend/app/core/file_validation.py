"""Upload validation — size, type, extension, and the bytes themselves.

Spec §33 lists four things to validate (size, content type, file extension,
authorization) and then adds the sentence that decides how:

    Never trust filename or MIME type alone.

Two of those three inputs are the client's own claims. The filename is a string the
client chose and the `Content-Type` is a header the client set; either can say anything.
So neither is trusted on its own, and the **bytes** get the final word: a file is
accepted only when the extension, the declared type, and the leading bytes all agree,
and one of the three was produced by reading the file.

Signatures rather than libmagic
-------------------------------
`python-magic` would be more thorough, and it needs the libmagic native library present
— on Windows that means shipping DLLs or telling every contributor to install one by
hand, which breaks `pip install -r requirements` on a fresh clone. A signature table
covering five types is a few dozen lines, has no native dependency, and is exactly as
testable. The allowlist is small on purpose: a support desk attaches screenshots, PDFs,
and logs.

What is deliberately **not** here is path handling. Traversal is prevented by
`app/core/storage.py` generating the object key itself, so no filename from a request
ever becomes part of a path. Nothing in this module needs to sanitize `../../` — it is
already impossible — and `sanitize_filename` below exists only because the original name
has to be *displayed*, which is a different problem with a different answer.
"""

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Final
from urllib.parse import quote

# Longest signature in the table. Reading more than this proves nothing; reading less
# would truncate one.
HEAD_SIZE: Final = 512

# The displayed filename has to fit `attachments.filename`, which is String(255).
MAX_FILENAME_LENGTH: Final = 255


@dataclass(frozen=True)
class AllowedType:
    """One accepted media type, and the two ways a client can name it.

    `signatures` is empty for types that have no magic number. That is not a gap in the
    table — it is a fact about the format, and `validate` handles it by requiring the
    other two signals to agree *and* the bytes to match nothing else.
    """

    media_type: str
    signatures: tuple[bytes, ...]
    extensions: frozenset[str]


ALLOWED: Mapping[str, AllowedType] = {
    allowed.media_type: allowed
    for allowed in (
        AllowedType("image/png", (b"\x89PNG\r\n\x1a\n",), frozenset({".png"})),
        # JPEG has no single fixed header: it starts with SOI (FFD8) followed by a
        # marker that varies. The three-byte prefix is the shortest form every JFIF and
        # Exif file shares.
        AllowedType("image/jpeg", (b"\xff\xd8\xff",), frozenset({".jpg", ".jpeg"})),
        AllowedType("image/gif", (b"GIF87a", b"GIF89a"), frozenset({".gif"})),
        AllowedType("application/pdf", (b"%PDF-",), frozenset({".pdf"})),
        # Plain text has no signature at all. Accepted only when the extension and the
        # declared type both say text *and* the bytes match no other type's signature,
        # so renaming a PNG to .txt does not get it in as text.
        AllowedType("text/plain", (), frozenset({".txt", ".log", ".csv"})),
    )
}

# Extension -> the one media type that owns it. Built from ALLOWED so the two cannot
# drift; a type that claims an extension another type already claims is a bug at import
# time rather than a surprise at request time.
EXTENSIONS: Mapping[str, str] = {
    extension: allowed.media_type
    for allowed in ALLOWED.values()
    for extension in allowed.extensions
}


class UnsupportedUpload(Exception):
    """An internal signal, not an API error.

    Kept separate from `UnsupportedFileTypeError` so this module stays free of HTTP and
    of the app's error hierarchy — it is pure logic over three inputs, which is what
    makes it testable without a client. `app/services/attachment_service.py` catches
    this and raises the API error, and the reason string it carries is for the log.

    The reason is never returned to the client: which check failed is of no use to a
    legitimate caller and of considerable use to someone probing the validator.
    """


def normalize_content_type(declared: str | None) -> str:
    """Reduce a `Content-Type` header to its bare media type, lowercase.

    A multipart part may carry parameters — `text/plain; charset=utf-8` — and the
    casing of a media type is not significant. Both are stripped so the comparison in
    `validate` is against the same shape the table uses.
    """
    if not declared:
        return ""
    return declared.split(";", 1)[0].strip().lower()


def sniff(head: bytes) -> str | None:
    """The media type whose signature `head` begins with, or `None`.

    `None` means "the bytes did not announce themselves", which is true of plain text
    and of anything unrecognised — the two cases are told apart by the declared type and
    the extension, not here.
    """
    for allowed in ALLOWED.values():
        # `startswith` takes a tuple, so a type with several signatures needs no loop of
        # its own. An empty tuple is False for every input, which is the right answer
        # for a type that has no signature.
        if head.startswith(allowed.signatures):
            return allowed.media_type
    return None


def validate(*, filename: str, declared_content_type: str | None, head: bytes) -> str:
    """Return the detected media type, or refuse the upload.

    Three checks, and the third is the one the spec's warning is about:

    1. The **extension** must be in the allowlist.
    2. The **declared type** must be in the allowlist, and must be the type that owns
       that extension. A `.png` announced as `application/pdf` is refused here rather
       than being resolved in the client's favour.
    3. The **bytes** must announce the declared type — and for a type that has no
       signature, must announce nothing at all. This is the check that makes "never
       trust the filename" true: an executable renamed to `.png` and sent as
       `image/png` passes the first two and fails this one.

    The caller streams the file for its size separately, because size is a fact about
    the whole body and this function only sees its first `HEAD_SIZE` bytes.
    """
    extension = PurePosixPath(PureWindowsPath(filename).name).suffix.lower()

    expected = EXTENSIONS.get(extension)
    if expected is None:
        raise UnsupportedUpload(f"extension {extension!r} is not allowed")

    declared = normalize_content_type(declared_content_type)
    if declared not in ALLOWED:
        raise UnsupportedUpload(f"declared type {declared!r} is not allowed")
    if declared != expected:
        raise UnsupportedUpload(f"{extension!r} is {expected!r}, not {declared!r}")

    detected = sniff(head)
    if detected is None:
        if ALLOWED[declared].signatures:
            raise UnsupportedUpload(f"{declared!r} requires a signature the file lacks")
    elif detected != declared:
        raise UnsupportedUpload(f"bytes are {detected!r}, declared {declared!r}")

    return declared


def sanitize_filename(filename: str) -> str:
    """A safe *display* name for the original file.

    Not a security control — the stored key never contains this, so a hostile name
    cannot reach a path either way. It exists because the name is echoed back in
    `Content-Disposition` and in the UI, and that has three requirements:

    * **No directory parts.** A browser given `Content-Disposition: attachment;
      filename="../../.bashrc"` may act on the traversal when saving. Only the final
      component survives, and Windows separators are handled as well as POSIX ones —
      this runs on Windows and a name like `C:\\Users\\me\\shot.png` is what a client
      will actually send.
    * **No control characters.** CR and LF in a header value are header injection.
      Stripped via the Unicode category rather than a hand-written list, so the bidi
      overrides and zero-width characters go too.
    * **No quotes or backslashes.** The name is emitted inside a quoted
      `Content-Disposition` filename, where either character ends the string early and
      turns the rest of the name into header syntax.
    * **A bounded length**, because the column is `String(255)` and a name past it is an
      integrity error at insert time rather than a refusal here.

    An empty result is replaced rather than allowed through, so the column is never
    blank and a filename is always something a person can read.
    """
    # Windows first: on Windows, `PurePosixPath` would leave `C:\Users\me\shot.png`
    # intact as a single component. Taking the Windows basename first handles both
    # separator conventions regardless of which platform is running this.
    basename = PurePosixPath(PureWindowsPath(filename).name).name

    cleaned = "".join(
        character
        for character in basename
        if unicodedata.category(character) not in {"Cc", "Cf"} and character not in {'"', "\\"}
    ).strip()

    # Leading dots hide the file on a POSIX filesystem and make `.` and `..` reachable
    # as names. Neither is a directory part, so the basename step above did not catch
    # them.
    cleaned = cleaned.lstrip(".")

    if not cleaned:
        return "attachment"

    return cleaned[:MAX_FILENAME_LENGTH]


def content_disposition(filename: str) -> str:
    """The `Content-Disposition` header for a stored attachment.

    `attachment` rather than `inline`, always. An inline response asks the browser to
    render the file, and these files came from a customer — HTML rendered in the API's
    own origin is stored XSS against the same origin the refresh cookie is scoped to.
    Combined with the `X-Content-Type-Options: nosniff` the route sets, the browser
    downloads rather than interprets, whatever the file claims to be.

    Two filenames, per RFC 6266, because one cannot do the job:

    * `filename="..."` is the fallback every client understands, and it must be ASCII —
      HTTP headers are latin-1, so a name with an accent in it would raise on the way
      out. Non-ASCII characters are replaced with `_` here.
    * `filename*=UTF-8''...` carries the real name, percent-encoded, for every client
      written this century. A client that understands it prefers it; one that does not
      falls back to the ASCII version rather than failing.

    Both are safe to interpolate because `sanitize_filename` has already removed quotes
    and backslashes, which is what would otherwise let a name escape the quoted string.
    """
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"
