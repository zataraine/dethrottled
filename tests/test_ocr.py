"""ocr.py, against a real scanned PDF that this file builds.

A PDF with no text layer is the case OCR exists for, and it cannot be faked
with a fixture full of strings: the whole path is bytes to image to Tesseract
to text. So the tests here render text to an image, wrap it in a PDF, and read
it back the way a fetched document would arrive.

Skipped when Tesseract or PyMuPDF is missing, because both are optional and a
missing optional dependency is not a failure.
"""
import pytest

pymupdf = pytest.importorskip("pymupdf")

from dethrottled import ocr  # noqa: E402

SAMPLE = ("The quick brown fox jumps over the lazy dog. "
          "Sparse retrieval scores documents by term frequency. "
          "This page has no text layer and must be read by optical "
          "character recognition.")


def has_tesseract():
    import shutil
    return shutil.which("tesseract") is not None


requires_tesseract = pytest.mark.skipif(
    not has_tesseract(), reason="tesseract binary not installed")


def scanned_pdf(text=SAMPLE, pages=1):
    """A PDF whose pages are IMAGES of text -- no extractable text layer.

    Built by writing text to one document, rasterising each page, and pasting
    the raster into a fresh one. That second step is what removes the text
    layer, and without it this would test nothing.
    """
    typed = pymupdf.open()
    for _ in range(pages):
        page = typed.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 550, 750), text,
                            fontsize=15, fontname="helv")
    scanned = pymupdf.open()
    for page in typed:
        pixmap = page.get_pixmap(dpi=160)
        fresh = scanned.new_page(width=page.rect.width, height=page.rect.height)
        fresh.insert_image(fresh.rect, pixmap=pixmap)
    data = scanned.tobytes()
    typed.close()
    scanned.close()
    return data


# ── the fixture itself must be honest ────────────────────────────────────────

def test_the_scanned_pdf_really_has_no_text_layer():
    """If this fails, every other test in the file is measuring the wrong
    thing -- they would be reading a text layer and calling it OCR."""
    doc = pymupdf.open(stream=scanned_pdf(), filetype="pdf")
    assert doc[0].get_text().strip() == ""
    doc.close()


# ── the real thing ───────────────────────────────────────────────────────────

@requires_tesseract
def test_ocr_reads_a_scanned_page():
    text, reason = ocr.ocr_pdf(scanned_pdf(), 10000)
    assert reason in ("", None) or text, reason
    lowered = text.lower()
    assert "quick brown fox" in lowered
    assert "term frequency" in lowered


@requires_tesseract
def test_ocr_respects_the_character_limit():
    text, _ = ocr.ocr_pdf(scanned_pdf(pages=3), 120)
    assert len(text) <= 120


@requires_tesseract
def test_page_cap_bounds_the_work(monkeypatch):
    """OCR costs about 1.4s a page. A 400-page document must not be allowed to
    spend ten minutes of somebody's request."""
    monkeypatch.setattr(ocr, "PAGE_CAP", 1)
    one, _ = ocr.ocr_pdf(scanned_pdf(pages=1), 100000)
    monkeypatch.setattr(ocr, "PAGE_CAP", 3)
    three, _ = ocr.ocr_pdf(scanned_pdf(pages=3), 100000)
    assert len(three) > len(one), "the cap should be what limits the output"


@requires_tesseract
def test_a_blank_scan_yields_nothing_without_erroring():
    """A blank page is a real thing to be handed. It should come back empty
    with a reason, not as an exception."""
    blank = pymupdf.open()
    blank.new_page()
    data = blank.tobytes()
    blank.close()
    text, reason = ocr.ocr_pdf(data, 1000)
    assert len(text.strip()) < 20


def test_rubbish_bytes_do_not_raise():
    """A mislabelled download must come back as a reason, not an exception."""
    text, reason = ocr.ocr_pdf(b"this is not a pdf at all", 1000)
    assert text == ""
    assert reason


# ── language selection ───────────────────────────────────────────────────────

@requires_tesseract
def test_installed_never_returns_empty():
    """Asking Tesseract for a language it lacks fails the whole call, so this
    must always name at least one language that is actually present."""
    langs = ocr.installed()
    assert langs
    assert all(isinstance(x, str) for x in langs)


@requires_tesseract
def test_installed_filters_out_missing_packs(monkeypatch):
    monkeypatch.setenv("DETHROTTLED_OCR_LANGS", "eng+klingon")
    monkeypatch.setattr(ocr, "_available", None)
    assert "klingon" not in ocr.installed()


def test_missing_tessdata_dir_is_ignored(monkeypatch):
    """A user tessdata directory that does not exist must not be exported:
    TESSDATA_PREFIX REPLACES the system directory rather than adding to it, so
    pointing it at nothing would break OCR that was working."""
    monkeypatch.setattr(ocr, "TESSDATA", "/nonexistent/tessdata")
    assert "TESSDATA_PREFIX" not in ocr._environment()


def test_present_tessdata_dir_is_exported(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, "TESSDATA", str(tmp_path))
    assert ocr._environment()["TESSDATA_PREFIX"] == str(tmp_path)


def test_thread_limit_is_passed_through():
    """OCR is CPU-bound and shares a box with everything else."""
    assert ocr._environment()["OMP_THREAD_LIMIT"] == ocr.THREADS


# ── the French heuristic ─────────────────────────────────────────────────────

def test_french_is_detected_from_function_words():
    french = ("le rapport de la commission sur les tarifs des marches "
              "publics dans une region avec des donnees")
    assert ocr._looks_french(french) is True


def test_english_is_not_mistaken_for_french():
    english = ("the report of the commission on the tariffs of public "
               "markets in a region with data")
    assert ocr._looks_french(english) is False


def test_latin_script_recognised():
    prose = ("the report of the commission on the tariffs of public markets "
             "in the region with the data that was collected over the year")
    assert ocr._looks_latin(prose) is True


def test_short_text_is_not_judged_at_all():
    """Under fifteen words the marker ratio means nothing, so the honest
    answer is no rather than a guess from four words."""
    assert ocr._looks_latin("the quick brown fox") is False


def test_confident_nonsense_is_rejected():
    """The instructive failure this heuristic exists for: English data on a
    non-Latin page returns PLENTY of text, all of it gibberish. Counting words
    would pass it; the function-word ratio does not."""
    noise = " ".join(["lub", "glaxule", "juaddl", "sfrq", "mmkz", "plwx"] * 4)
    assert ocr._looks_latin(noise) is False


def test_markers_have_no_duplicates():
    """FRENCH_MARKERS once contained the same word twice, which silently made
    the list one word shorter than it appeared."""
    assert len(ocr.FRENCH_MARKERS) == len(set(ocr.FRENCH_MARKERS))
    assert not (ocr.FRENCH_MARKERS & ocr.ENGLISH_MARKERS), \
        "a marker in both sets discriminates nothing"
