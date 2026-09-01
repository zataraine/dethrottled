"""OpenDocument, EPUB and RTF, against files this test builds.

These three fill a real gap. OpenDocument is what most European public bodies
publish in, an EPUB is a zip of XHTML, and RTF still turns up from older
systems. All three were previously unreadable and reported as an unsupported
document.

The detection tests matter as much as the parsing ones: ODF, EPUB and OOXML are
all zips sharing the same four magic bytes, so telling them apart means reading
what is inside rather than trusting the extension or the server's header.
"""
import io
import zipfile

import pytest


def missing(module):
    import importlib.util
    return importlib.util.find_spec(module) is None


from dethrottled import documents as docs  # noqa: E402

needs_odf = pytest.mark.skipif(missing("odf"), reason="odfpy not installed")
needs_epub = pytest.mark.skipif(missing("ebooklib"), reason="ebooklib not installed")
needs_rtf = pytest.mark.skipif(missing("striprtf"), reason="striprtf not installed")


# ── telling zips apart ───────────────────────────────────────────────────────

def zip_with(mimetype):
    """A minimal zip declaring itself through its `mimetype` member, the way
    the ODF and EPUB specifications require."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", mimetype)
        z.writestr("content.xml", "<root/>")
    return buf.getvalue()


@pytest.mark.parametrize("mimetype,expected", [
    ("application/vnd.oasis.opendocument.text", "odt"),
    ("application/vnd.oasis.opendocument.spreadsheet", "ods"),
    ("application/vnd.oasis.opendocument.presentation", "odp"),
    ("application/epub+zip", "epub"),
])
def test_zip_formats_identified_from_their_mimetype_member(mimetype, expected):
    assert docs.kind_of(zip_with(mimetype)) == expected


def test_ooxml_still_wins_over_the_mimetype_member():
    """An OOXML file is identified by a member path. Adding the ODF branch must
    not have changed that."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<root/>")
    assert docs.kind_of(buf.getvalue()) == "xlsx"


def test_an_unknown_zip_is_not_a_document():
    """A plain archive is not something we read, and guessing would be worse
    than saying so."""
    assert docs.kind_of(zip_with("application/x-something-else")) == ""


def test_a_bare_zip_with_no_mimetype_is_not_a_document():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("notes.txt", "hello")
    assert docs.kind_of(buf.getvalue()) == ""


def test_rtf_is_identified_by_its_opening_token():
    assert docs.kind_of(b"{\\rtf1\\ansi hello\\par}") == "rtf"


def test_something_merely_mentioning_rtf_is_not_rtf():
    assert docs.kind_of(b"<html><body>about rtf files</body></html>") != "rtf"


# ── parsing ──────────────────────────────────────────────────────────────────

@needs_odf
def test_odt_text_is_extracted():
    from odf.opendocument import OpenDocumentText
    from odf.text import H, P
    doc = OpenDocumentText()
    doc.text.addElement(H(outlinelevel=1, text="Tender notice"))
    doc.text.addElement(P(text="Sealed bids are invited for photovoltaic modules."))
    buf = io.BytesIO()
    doc.write(buf)

    kind = docs.kind_of(buf.getvalue())
    text, reason = docs.to_text(buf.getvalue(), kind, 4000)
    assert kind == "odt", reason
    assert "Tender notice" in text
    assert "photovoltaic" in text


@needs_odf
def test_ods_rows_keep_their_boundaries():
    """Run two rows together and the table stops saying which number belongs to
    which year -- the same reason XLSX rows keep their line breaks."""
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableCell, TableRow
    from odf.text import P
    sheet = OpenDocumentSpreadsheet()
    table = Table(name="Capacity")
    for row in (["country", "year", "megawatts"],
                ["Denmark", "2024", "5120"],
                ["Denmark", "2023", "4560"]):
        tr = TableRow()
        for value in row:
            cell = TableCell(valuetype="string")
            cell.addElement(P(text=value))
            tr.addElement(cell)
        table.addElement(tr)
    sheet.spreadsheet.addElement(table)
    buf = io.BytesIO()
    sheet.write(buf)

    text, reason = docs.to_text(buf.getvalue(), "ods", 4000)
    assert "5120" in text, reason
    assert text.count("\n") >= 2, "each row needs its own line"
    assert "2024" in text.splitlines()[1]


@needs_epub
def test_epub_chapters_are_extracted(tmp_path):
    from ebooklib import epub
    book = epub.EpubBook()
    book.set_identifier("id")
    book.set_title("A Book")
    book.set_language("en")
    chapter = epub.EpubHtml(title="Ch1", file_name="c1.xhtml")
    chapter.content = ("<html><body><h1>Chapter One</h1><p>"
                       + "Sparse retrieval scores documents by term frequency. " * 10
                       + "</p></body></html>")
    book.add_item(chapter)
    book.spine = ["nav", chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    path = tmp_path / "b.epub"
    epub.write_epub(str(path), book)
    data = path.read_bytes()

    assert docs.kind_of(data) == "epub"
    text, reason = docs.to_text(data, "epub", 4000)
    assert "Sparse retrieval" in text, reason


@needs_rtf
def test_rtf_text_is_extracted():
    data = br"{\rtf1\ansi Sealed bids are invited for photovoltaic modules.\par}"
    text, reason = docs.to_text(data, "rtf", 4000)
    assert "photovoltaic" in text, reason
    assert "\\rtf" not in text, "control words must not survive"


# ── failure behaviour ────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["odt", "ods", "odp", "epub", "rtf"])
def test_corrupt_input_gives_a_reason_not_an_exception(kind):
    text, reason = docs.to_text(b"PK\x03\x04 truncated nonsense", kind, 1000)
    assert text == ""
    assert reason


@needs_odf
def test_the_character_limit_is_respected():
    from odf.opendocument import OpenDocumentText
    from odf.text import P
    doc = OpenDocumentText()
    for _ in range(200):
        doc.text.addElement(P(text="a reasonably long paragraph of text here"))
    buf = io.BytesIO()
    doc.write(buf)
    text, _ = docs.to_text(buf.getvalue(), "odt", 200)
    assert len(text) <= 200


def test_every_new_format_has_a_reader():
    """A format that kind_of() names but READERS cannot open would be detected
    and then reported as unsupported, which is worse than not detecting it."""
    for kind in ("odt", "ods", "odp", "epub", "rtf"):
        assert kind in docs.READERS, kind
