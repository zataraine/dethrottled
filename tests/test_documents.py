"""Format detection from the file SIGNATURE, never the header.

Servers mislabel these constantly. An HTML error page served as
application/vnd.ms-excel is routine, and handing that to a spreadsheet parser
produces either a crash or -- much worse -- nonsense that looks like data.
So the bytes decide, and the header is not consulted at all when they disagree.
"""
import io
import zipfile

import pytest

from dethrottled import documents as docs

OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32
HTML = b"<!doctype html><html><body>Not a spreadsheet</body></html>"
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"


def ooxml(kind):
    """A minimal but structurally real OOXML file of the given kind."""
    member = {"xlsx": "xl/workbook.xml",
              "docx": "word/document.xml",
              "pptx": "ppt/presentation.xml"}[kind]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(member, "<root/>")
    return buf.getvalue()


@pytest.mark.parametrize("kind", ["xlsx", "docx", "pptx"])
def test_ooxml_detected_from_the_zip_member(kind):
    """All three are zips with the same magic bytes; only the member path
    inside distinguishes them."""
    assert docs.kind_of(ooxml(kind)) == kind


def test_a_mislabelled_html_page_is_not_a_spreadsheet():
    """The failure this whole approach exists to prevent."""
    assert docs.kind_of(
        HTML, url="https://example.com/data.xlsx",
        content_type="application/vnd.ms-excel") != "xlsx"


def test_the_extension_alone_does_not_decide():
    assert docs.kind_of(HTML, url="https://example.com/report.docx") != "docx"


def test_signature_beats_a_wrong_content_type():
    data = ooxml("xlsx")
    assert docs.kind_of(data, content_type="text/html") == "xlsx"


def test_pdf_is_not_this_modules_job():
    """PDFs are handled in fetch.py by pymupdf, with their own size budget and
    an OCR fallback. This module covers the Office formats only, and must not
    quietly claim a PDF it cannot parse."""
    assert docs.kind_of(PDF) == ""


def test_legacy_formats_are_refused_by_name_not_silently():
    """Legacy .doc/.ppt need a ~500MB converter to read. Refusing them by name
    is honest; parsing them as something else and returning noise is not."""
    kind = docs.kind_of(OLE2, url="https://example.com/old.doc")
    assert kind in ("doc", "ppt", "xls", "")


def test_unknown_bytes_are_empty_not_a_guess():
    assert docs.kind_of(b"\x00\x01\x02\x03 random") == ""


def test_csv_round_trips_to_text():
    data = b"country,year,capacity\nDenmark,2024,5120\nDenmark,2023,4560\n"
    text, why = docs.to_text(data, "csv", 4000)
    assert "Denmark" in text and "5120" in text


def test_tidy_structured_keeps_row_boundaries():
    """The reason structured text does not go through the prose tidier: run two
    rows together and the table no longer says which number belongs to which
    year."""
    value = "Denmark | 2024 | 5120\n\n\nDenmark | 2023 | 4560"
    out = docs.tidy_structured(value)
    assert out.count("\n") >= 1
    assert "5120" in out and "4560" in out


def test_tidy_structured_still_collapses_intra_line_whitespace():
    assert docs.tidy_structured("a     b\tc") == "a b c"


def test_tidy_structured_respects_a_limit():
    assert len(docs.tidy_structured("x" * 500, limit=50)) <= 50
