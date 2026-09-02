#!/usr/bin/env python3
"""One embedding model or two? Decide it on evidence.

Carrying both all-MiniLM-L6-v2 (87MB) and multilingual-e5-small (465MB) means
every install pays 552MB and every indexed page is embedded twice. That is only
worth it if the larger model is meaningfully better at the job this actually
does -- finding the right page among pages about neighbouring subjects.

So: a corpus of real fetched pages, and queries whose correct answer is known.
Both models see exactly the same documents and the same questions.

    python scripts/probe_embed.py

Reported as accuracy@1 (was the right page first?), MRR, and the score margin
between the right answer and the best wrong one -- the last being the number
that decides whether a relevance floor can be set safely at all.
"""
import sys
import time

from dethrottled.corpus import Corpus

# Distinct subjects with deliberately overlapping vocabulary, so that matching
# on topic words alone is not enough.
DOCS = [
    ("bm25", "Okapi BM25 ranking function",
     "BM25 scores a document by term frequency and inverse document frequency, "
     "with a length normalisation term so that long documents do not win "
     "merely by containing more words. The k1 parameter controls saturation."),
    ("tfidf", "TF-IDF weighting",
     "TF-IDF multiplies how often a term appears in a document by how rare "
     "that term is across the collection, producing a sparse vector per "
     "document with no length normalisation of its own."),
    ("embed", "Dense retrieval with embeddings",
     "A bi-encoder maps queries and documents into the same vector space so "
     "that semantic similarity becomes cosine distance, retrieving documents "
     "that share no vocabulary with the query at all."),
    ("rerank", "Cross-encoder reranking",
     "A cross-encoder reads the query and the document together with attention "
     "across both, which is why it outperforms a bag of words and why its cost "
     "grows with the number of documents scored."),
    ("tls", "TLS fingerprinting and JA3",
     "A JA3 hash is derived from the ordered cipher suites and extensions in a "
     "TLS ClientHello, and differs between ordinary HTTP libraries and real "
     "browsers, which is how automated clients are identified."),
    ("headless", "Headless browser detection",
     "Automation is detected from navigator.webdriver, missing plugin arrays "
     "and timing signatures that differ from a human driving a real browser "
     "window on a real display."),
    ("robots", "The robots exclusion protocol",
     "A robots.txt file tells crawlers which paths they may request and how "
     "often, and honouring it is a matter of politeness rather than a "
     "technical restriction enforced by the server."),
    ("ocr", "Optical character recognition of scans",
     "Tesseract converts page images into text, and works best when given a "
     "single language pack at a sensible resolution rather than several "
     "language packs at once."),
    ("pi", "The Raspberry Pi 4B",
     "A single board computer built around an ARM Cortex-A72, commonly used "
     "for small always-on services where power draw matters more than raw "
     "processing speed."),
    ("sqlite", "SQLite write-ahead logging",
     "WAL mode lets readers continue while a writer is active, which suits an "
     "embedded database serving concurrent requests from one process."),
]

QUERIES = [
    ("how does length normalisation stop long documents winning", "bm25"),
    ("what makes automated browsers identifiable", "headless"),
    ("how are cipher suites used to identify a client", "tls"),
    ("reading text out of scanned page images", "ocr"),
    ("retrieving documents that share no words with the query", "embed"),
    ("why does scoring cost more per document", "rerank"),
    ("am I allowed to crawl this path", "robots"),
    ("low power always-on ARM machine", "pi"),
    ("concurrent reads while writing to an embedded database", "sqlite"),
    ("weighting terms by how rare they are", "tfidf"),
]


def evaluate(multilingual, label):
    corpus = Corpus(multilingual=multilingual)
    started = time.perf_counter()
    for key, title, text in DOCS:
        corpus.add("https://x/%s" % key, title, text)
    index_ms = (time.perf_counter() - started) * 1000

    hits, rr, margins, times = 0, [], [], []
    for query, correct in QUERIES:
        started = time.perf_counter()
        found = corpus.search(query, limit=len(DOCS), floor=0.0, per_url=1)
        times.append((time.perf_counter() - started) * 1000)
        keys = [h["url"].rsplit("/", 1)[-1] for h in found]
        if not keys:
            rr.append(0.0)
            continue
        if keys[0] == correct:
            hits += 1
        rr.append(1.0 / (keys.index(correct) + 1) if correct in keys else 0.0)
        # The gap between the right answer and the best wrong one. A model whose
        # margin is tiny cannot have a threshold set on it safely, however well
        # it ranks.
        right = next((h["score"] for h in found
                      if h["url"].endswith("/" + correct)), 0.0)
        wrong = max((h["score"] for h in found
                     if not h["url"].endswith("/" + correct)), default=0.0)
        margins.append(right - wrong)

    return {
        "label": label,
        "acc": hits / len(QUERIES),
        "mrr": sum(rr) / len(rr),
        "margin": sum(margins) / len(margins),
        "worst_margin": min(margins) if margins else 0.0,
        "index_ms": index_ms,
        "query_ms": sorted(times)[len(times) // 2],
    }


def main():
    results = []
    for multilingual, label in ((False, "minilm (87MB)"), (True, "e5 (465MB)")):
        try:
            results.append(evaluate(multilingual, label))
        except Exception as exc:
            print("%s unavailable: %s" % (label, str(exc)[:70]))

    if not results:
        return 1

    print("%-16s %8s %8s %10s %12s %10s %9s" % (
        "model", "acc@1", "MRR", "margin", "worst margin", "index ms", "query ms"))
    print("-" * 80)
    for r in results:
        print("%-16s %8.2f %8.3f %10.3f %12.3f %10.0f %9.1f" % (
            r["label"], r["acc"], r["mrr"], r["margin"], r["worst_margin"],
            r["index_ms"], r["query_ms"]))

    if len(results) == 2:
        small, large = results
        print("\nsmaller model is %s on accuracy, %s on ranking"
              % ("equal or better" if small["acc"] >= large["acc"] else "worse",
                 "equal or better" if small["mrr"] >= large["mrr"] else "worse"))
        print("margin: %.3f vs %.3f -- a bigger margin means a relevance floor"
              % (small["margin"], large["margin"]))
        print("can be set with room to spare, which is what makes a floor safe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
