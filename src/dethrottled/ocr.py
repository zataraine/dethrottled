"""Text from scanned PDFs, via Tesseract.

A PDF with no text layer is otherwise a dead end -- fetch reports
pdf_no_text_layer and that is the end of the source. This matters more than it
sounds, because the documents most likely to be scans are also the ones least
likely to exist anywhere else: government tenders, municipal filings, older
statistical yearbooks. The web copy IS the scan.

Measured at 150dpi, ~2900 chars/page:

    150dpi   render 0.09s   ocr 1.44s
    220dpi   render 0.14s   ocr 1.75s

150 wins twice over -- faster, and CLEANER, because at 220 Tesseract began
reading a sideways watermark as text.

## One language, not several

The obvious thing is to pass every installed language and let Tesseract sort it
out. Measured against a French page with known ground truth, that is the worst
option available:

    -l fra           similarity 0.997   0.32s
    -l eng           similarity 0.981   0.33s
    -l eng+fra       similarity 0.986   0.46s
    -l eng+fra+ara   similarity 0.986   0.50s
    -l ara           similarity 0.092   0.31s

Stacking languages makes Tesseract hedge: it is slower AND less accurate than
the right single pack. And Arabic data on Latin text is catastrophic, so it can
never simply be included as insurance.

So the language is DETECTED once per document and one pack is used. Script
comes from Tesseract's own OSD, which is the split that matters most. Telling
French from English is done on the first page's English output -- eng reads
French at 0.981, more than good enough to recognise WHICH language it is even
though it transcribes it worse. Page one is not OCRed twice unless the answer
turns out not to be English.

Pages are piped as PNG on stdin, so nothing touches disk. Thread count follows
DETHROTTLED_EMBED_THREADS rather than introducing a second CPU-pinning
mechanism: the rest of the stack constrains CPU by thread count, and two ways
to say the same thing is one more thing to get wrong.
"""
import os
import re
import subprocess

DPI = int(os.environ.get("DETHROTTLED_OCR_DPI", "150"))
PAGE_CAP = int(os.environ.get("DETHROTTLED_OCR_PAGE_CAP", "8"))
THREADS = os.environ.get("DETHROTTLED_EMBED_THREADS", "4")
PAGE_TIMEOUT = int(os.environ.get("DETHROTTLED_OCR_PAGE_TIMEOUT", "30"))

# Language data installed without root. Tesseract reads TESSDATA_PREFIX, and a
# user directory keeps this off the system packages.
def _default_tessdata() -> str:
    # Beside the other model weights, because that is where fetch-models.sh
    # --all puts the language packs. These disagreed: the script installed to
    # the model directory and this looked in a hardcoded path from another
    # machine, so an installed French pack was never found.
    from . import paths as _paths
    return str(_paths.model_dir() / "tessdata")


TESSDATA = os.environ.get("TESSDATA_PREFIX") or _default_tessdata()

# Function words common in French and rare in English. Deliberately not
# accent-based: OCR is least reliable on exactly those characters, so keying
# the decision to them would fail when it matters most.
FRENCH_MARKERS = {
    "le", "la", "les", "des", "du", "pour", "dans", "une", "est", "sur",
    "aux", "par", "avec", "cette", "selon", "sont", "leur", "plus",
    "entre", "ainsi", "nous", "ont", "etre", "fait", "aussi", "donc",
}
# Function words common to the Latin languages we read. Garbage has almost
# none of these; real prose in either language is thick with them.
ENGLISH_MARKERS = {
    "the", "of", "and", "to", "in", "for", "is", "that", "with", "from",
    "this", "has", "was", "are", "by", "on", "at", "as", "it", "be", "which",
}
LATIN_MARKERS = FRENCH_MARKERS | ENGLISH_MARKERS

WORDS = re.compile(r"[a-zà-ÿ']{2,}")

_available = None


def _environment() -> dict:
    """The environment every Tesseract call runs in.

    One function, because the language probe and the OCR call MUST agree about
    which tessdata directory is in play. They did not: the probe unioned the
    user directory with the system one while the call used the user directory
    alone, so a user directory holding fra but not eng advertised English and
    then failed every page trying to load it.
    """
    environment = dict(os.environ)
    environment["OMP_THREAD_LIMIT"] = THREADS
    if os.path.isdir(TESSDATA):
        environment["TESSDATA_PREFIX"] = TESSDATA
    return environment


def _run(args, png: bytes) -> str:
    try:
        done = subprocess.run(["tesseract", "stdin", "stdout", *args],
                              input=png, capture_output=True,
                              timeout=PAGE_TIMEOUT, env=_environment())
    except Exception:
        return ""
    return done.stdout.decode("utf-8", "replace")


def installed() -> list:
    """Which of the wanted languages actually have data present.

    Asking Tesseract for a language it does not have fails the whole call, so a
    missing pack must degrade rather than lose the page. French and Arabic do
    not ship with the base package and live in TESSDATA.
    """
    global _available
    if _available is None:
        try:
            done = subprocess.run(["tesseract", "--list-langs"],
                                  capture_output=True, timeout=20,
                                  env=_environment())
            _available = {line.strip() for line
                          in done.stdout.decode("utf-8", "replace").splitlines()
                          if line.strip() and " " not in line.strip()}
        except Exception:
            _available = set()
    wanted = os.environ.get("DETHROTTLED_OCR_LANGS", "eng+fra+ara").split("+")
    return [lang for lang in wanted if lang in _available] or ["eng"]


def languages() -> str:
    """The candidate set, for reporting. Not what gets passed to Tesseract."""
    return "+".join(installed())


def _script(png: bytes) -> str:
    """Which script the page is written in, via Tesseract's own OSD."""
    for line in _run(["--psm", "0"], png).splitlines():
        if line.startswith("Script:"):
            return line.split(":", 1)[1].strip()
    return ""


def _looks_latin(text: str) -> bool:
    """Whether an English-data probe read real Latin prose, or just noise.

    The first version of this asked whether the probe found FEW Latin words,
    which was wrong in an instructive way. English data pointed at an Arabic
    page does not return little text -- it returns plenty of confident nonsense
    ("lub Yl glaxule juaddl"), so the word count stayed high and a genuinely
    Arabic document was read as English at a similarity of 0.007.

    Real prose is thick with function words and noise has almost none, so that
    ratio separates them cleanly where a raw count does not.
    """
    words = WORDS.findall(text.lower())
    if len(words) < 15:
        return False
    hits = sum(1 for word in words if word in LATIN_MARKERS)
    return hits / len(words) > 0.04


def _looks_french(text: str) -> bool:
    words = WORDS.findall(text.lower())
    if len(words) < 15:
        return False
    hits = sum(1 for word in words if word in FRENCH_MARKERS)
    return hits / len(words) > 0.06


def _choose(png: bytes, options: list) -> tuple:
    """(language, page_one_text). Detects once, and reuses the probe when it can.

    Ordered so the common case is free. Running OSD first cost a flat 2.5-3s a
    document -- 167% overhead on a one-page scan, for an accuracy gain of about
    0.012, which is not a trade worth making. But the English probe is work an
    English document needs ANYWAY, so probing first means a Latin document pays
    nothing: its probe becomes its page one.

    OSD is then only consulted when the probe comes back with almost no Latin
    words, which is what a non-Latin page looks like through English data.
    """
    if len(options) == 1:
        return options[0], None

    latin = [o for o in options if o != "ara"]
    if "eng" not in latin:
        # No English data to probe with; nothing cheap to reason from.
        if "ara" in options and _script(png) == "Arabic":
            return "ara", None
        return (latin or options)[0], None

    probe = _run(["-l", "eng", "--dpi", str(DPI)], png)

    # A probe that did not read real Latin prose means this is not a Latin
    # page. Only then is OSD worth its cost -- and this is a correctness
    # question rather than a refinement: Arabic data on Latin text scores
    # 0.092, and English data on an Arabic page scores 0.007.
    if "ara" in options and not _looks_latin(probe):
        if _script(png) == "Arabic":
            return "ara", None

    if "fra" in latin and _looks_french(probe):
        return "fra", None                            # worth re-reading page 1
    return "eng", probe                               # keep what we already have


def ocr_pdf(data: bytes, limit: int) -> tuple:
    """(text, reason) for a PDF with no text layer.

    Stops at the character limit or the page cap, whichever comes first. A
    scanned 200-page annual report is not worth ten minutes, and the first
    pages are what any cap keeps anyway.
    """
    try:
        import pymupdf
    except ImportError:                               # pragma: no cover
        return "", "ocr_unavailable:pymupdf"

    options = installed()
    lang, reuse = None, None
    parts, total = [], 0
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            for number, page in enumerate(doc):
                if number >= PAGE_CAP or total >= limit:
                    break
                png = page.get_pixmap(dpi=DPI).tobytes("png")
                if lang is None:
                    lang, reuse = _choose(png, options)
                text = reuse if reuse is not None else _run(
                    ["-l", lang, "--dpi", str(DPI)], png)
                reuse = None
                if text.strip():
                    parts.append(text)
                    total += len(text)
    except Exception as exc:
        return "", "ocr_failed:%s" % type(exc).__name__

    joined = "\n".join(parts).strip()
    if not joined:
        # Genuinely nothing on the page: a blank scan, or a picture with no
        # writing in it. Distinct from OCR being unavailable or erroring.
        return "", "ocr_found_nothing"
    return joined[:limit], ""
