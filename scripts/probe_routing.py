#!/usr/bin/env python3
"""What does content routing actually cost?

The worry with adding formats is that every fetch starts paying for formats it
will never see. So this measures the two decisions on the hot path:

  1. is this URL a video?      decided BEFORE any network, from the URL
  2. what are these bytes?     decided from the file signature, not the header

Neither uses a model. MarkItDown does the second one with `magika`, a neural
network, which is 375MB of dependency and a forward pass per file; this is a
handful of byte comparisons and, for zips, one directory read.

    python scripts/probe_routing.py
"""
import io
import sys
import time
import zipfile

from dethrottled import documents as docs
from dethrottled import media

HTML = (b"<!doctype html><html><head><title>A page</title></head><body>"
        + b"<p>ordinary web page</p>" * 200 + b"</body></html>")
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"0" * 40000
RTF = b"{\\rtf1\\ansi Sealed bids are invited.\\par}"
CSV = b"country,year,value\nDenmark,2024,5120\n" * 400


def ooxml(member):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(member, "<root/>")
    return buf.getvalue()


def odf(mimetype):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", mimetype)
        z.writestr("content.xml", "<root/>")
    return buf.getvalue()


SAMPLES = [
    ("html page", HTML, ""),
    ("pdf", PDF, "pdf"),
    ("xlsx", ooxml("xl/workbook.xml"), "xlsx"),
    ("docx", ooxml("word/document.xml"), "docx"),
    ("pptx", ooxml("ppt/presentation.xml"), "pptx"),
    ("odt", odf("application/vnd.oasis.opendocument.text"), "odt"),
    ("ods", odf("application/vnd.oasis.opendocument.spreadsheet"), "ods"),
    ("odp", odf("application/vnd.oasis.opendocument.presentation"), "odp"),
    ("epub", odf("application/epub+zip"), "epub"),
    ("rtf", RTF, "rtf"),
    ("csv (hinted)", CSV, "csv"),
]

URLS = [
    ("youtube watch", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", True),
    ("youtu.be", "https://youtu.be/dQw4w9WgXcQ", True),
    ("youtube shorts", "https://www.youtube.com/shorts/dQw4w9WgXcQ", True),
    ("youtube embed", "https://www.youtube.com/embed/dQw4w9WgXcQ", True),
    ("youtube channel", "https://www.youtube.com/@someone", False),
    ("youtube search", "https://www.youtube.com/results?search_query=x", False),
    ("ordinary article", "https://en.wikipedia.org/wiki/Web_scraping", False),
    ("a pdf link", "https://example.gov/report.pdf", False),
]

ROUNDS = 2000


def main():
    print("1. VIDEO CHECK -- runs on every URL, before any network")
    print("   %-20s %-10s %-10s %s" % ("url", "detected", "expected", "µs/call"))
    print("   " + "-" * 60)
    ok = True
    for label, url, expected in URLS:
        started = time.perf_counter()
        for _ in range(ROUNDS):
            got = media.is_video(url)
        each = (time.perf_counter() - started) / ROUNDS * 1e6
        ok &= got == expected
        print("   %-20s %-10s %-10s %6.2f %s"
              % (label, got, expected, each, "" if got == expected else "WRONG"))

    print("\n2. FORMAT CHECK -- runs on the first bytes of every response")
    print("   %-14s %-9s %-9s %10s %s" % ("sample", "detected", "expected",
                                          "bytes", "µs/call"))
    print("   " + "-" * 62)
    for label, data, expected in SAMPLES:
        # CSV alone has no signature, so it is the one format that needs a
        # hint. Guessing would make every HTML page a one-column CSV.
        hint = "text/csv" if expected == "csv" else ""
        started = time.perf_counter()
        for _ in range(200):
            got = docs.kind_of(data, url="x", content_type=hint)
        each = (time.perf_counter() - started) / 200 * 1e6
        # HTML and PDF are not documents.kind_of's job; both are routed
        # elsewhere, so "" is the correct answer for them here.
        want = expected if expected not in ("pdf",) else ""
        ok &= got == want
        print("   %-14s %-9s %-9s %10d %7.1f %s"
              % (label, got or "-", want or "-", len(data), each,
                 "" if got == want else "WRONG"))

    print("\n   (pdf is routed by content-type before this, so \"\" is right)")
    print("\n%s" % ("routing is correct and costs microseconds" if ok
                    else "ROUTING ERRORS ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
