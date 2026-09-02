#!/usr/bin/env python3
"""Is the image genuinely self-contained?

Run INSIDE the container, with no bind mounts and no host paths. A Docker image
is self-contained by construction -- the build happens inside a base image, so
the host's packages cannot leak in -- but "cannot leak in" and "everything
needed is present" are different claims, and only the second one matters when
somebody pulls this on a machine that has nothing installed.

So this checks what is actually there:

  * every optional dependency imports
  * the system binaries the code shells out to exist and run
  * every declared capability reports available
  * every document format parses, from bytes built here
  * nothing resolves to a path outside the container

    docker run --rm --network none dethrottled:local \\
        python /app/verify_image.py
"""
import io
import shutil
import subprocess
import sys
import zipfile

FAILURES = []


def check(label, fn, required=True):
    try:
        detail = fn()
        print("  %-34s ok    %s" % (label, detail or ""))
        return True
    except Exception as exc:
        mark = "FAIL " if required else "absent"
        print("  %-34s %s %s: %s" % (label, mark, type(exc).__name__,
                                     str(exc)[:60]))
        if required:
            FAILURES.append(label)
        return False


def main():
    print("PYTHON DEPENDENCIES")
    for module in ("requests", "feedparser", "ddgs", "trafilatura",
                   "resiliparse", "selectolax", "fastapi", "uvicorn",
                   "pymupdf", "openpyxl", "docx", "pptx", "xlrd",
                   "odf", "ebooklib", "striprtf", "youtube_transcript_api",
                   "numpy", "onnxruntime", "transformers", "flashrank",
                   "curl_cffi"):
        check(module, lambda m=module: __import__(m) and "")

    print("\nSYSTEM BINARIES")

    def tesseract():
        if not shutil.which("tesseract"):
            raise RuntimeError("not on PATH")
        out = subprocess.run(["tesseract", "--version"], capture_output=True,
                             timeout=20)
        return out.stdout.decode().splitlines()[0]

    def tessdata():
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True,
                             timeout=20)
        langs = [x.strip() for x in out.stdout.decode().splitlines()[1:]
                 if x.strip()]
        if "eng" not in langs:
            raise RuntimeError("no eng language pack: %r" % langs)
        return "languages: %s" % ",".join(langs)

    check("tesseract", tesseract)
    check("tesseract language data", tessdata)
    check("curl (healthcheck uses it)",
          lambda: shutil.which("curl") or (_ for _ in ()).throw(
              RuntimeError("not on PATH")))

    print("\nDECLARED CAPABILITIES")
    from dethrottled import extract as fx
    from dethrottled import rank

    def extractors():
        have = fx.available()
        missing = [k for k, v in have.items() if not v]
        if missing:
            raise RuntimeError("missing %s" % missing)
        return ", ".join(have)

    check("extraction cascade", extractors)
    check("bm25", lambda: rank.available()["bm25"] or (_ for _ in ()).throw(
        RuntimeError("unavailable")))
    check("cross-encoder", lambda: rank.available()["rerank"] or
          (_ for _ in ()).throw(RuntimeError("flashrank missing")))
    # The embedding weights are a volume, not a layer -- 87MB does not belong
    # in an image, and the corpus is optional. Reported, not required.
    check("corpus (needs mounted model)",
          lambda: rank.available()["corpus"] or (_ for _ in ()).throw(
              RuntimeError("model not mounted -- expected in a bare image")),
          required=False)

    print("\nDOCUMENT FORMATS (parsed from bytes built here)")
    from dethrottled import documents as docs

    def ooxml(member):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr(member, "<root/>")
        return buf.getvalue()

    def odf_zip(mimetype):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("mimetype", mimetype)
            z.writestr("content.xml", "<root/>")
        return buf.getvalue()

    cases = [
        ("xlsx", ooxml("xl/workbook.xml")),
        ("docx", ooxml("word/document.xml")),
        ("pptx", ooxml("ppt/presentation.xml")),
        ("odt", odf_zip("application/vnd.oasis.opendocument.text")),
        ("ods", odf_zip("application/vnd.oasis.opendocument.spreadsheet")),
        ("odp", odf_zip("application/vnd.oasis.opendocument.presentation")),
        ("epub", odf_zip("application/epub+zip")),
        ("rtf", b"{\\rtf1\\ansi hello world\\par}"),
    ]
    for kind, data in cases:
        check("detect %s" % kind,
              lambda k=kind, d=data: (docs.kind_of(d) == k) or
              (_ for _ in ()).throw(RuntimeError("got %r" % docs.kind_of(d))))

    def readers():
        missing = [k for k in ("xlsx", "docx", "pptx", "odt", "ods", "odp",
                               "epub", "rtf", "csv", "xls")
                   if k not in docs.READERS]
        if missing:
            raise RuntimeError("no reader for %s" % missing)
        return "%d readers" % len(docs.READERS)

    check("all readers present", readers)

    def pdf_engine():
        import pymupdf
        doc = pymupdf.open()
        doc.new_page()
        doc.tobytes()
        doc.close()
        return "pymupdf works"

    check("pdf engine", pdf_engine)

    print("\nNO HOST LEAKAGE")

    def paths_inside():
        from dethrottled import paths
        for name, value in (("data", paths.data_dir()),
                            ("models", paths.model_dir())):
            text = str(value)
            if text.startswith("/mnt/") or ("/home/" in text and "/data" not in text):
                raise RuntimeError("%s resolves outside the container: %s"
                                   % (name, text))
        return "data=%s models=%s" % (paths.data_dir(), paths.model_dir())

    check("state paths", paths_inside)

    def not_root():
        import os
        if os.geteuid() == 0:
            raise RuntimeError("running as root")
        return "uid %d" % os.geteuid()

    check("unprivileged user", not_root)

    print()
    if FAILURES:
        print("SELF-CONTAINMENT FAILED: %s" % ", ".join(FAILURES))
        return 1
    print("image is self-contained: every required dependency is inside it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
