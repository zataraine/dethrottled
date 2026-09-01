#!/usr/bin/env python3
"""Does reranking actually improve the order, or does it just run?

A stage that executes is not a stage that helps. The cross-encoder costs a
model load and roughly six milliseconds per document, and until this script
existed nothing here had ever checked that it bought anything.

The test is a pool with a known right answer: a handful of results per query
where exactly one genuinely answers it and the rest are plausible neighbours --
same vocabulary, same subject area, wrong document. That is the case where
lexical scoring is weakest and a cross-encoder should earn its keep, because
BM25 cannot tell "mentions the words" from "answers the question".

    python scripts/probe_rerank.py

Reported as mean reciprocal rank: 1.0 means the right answer was always first,
0.5 means it averaged second.
"""
import sys
import time

from dethrottled import rank

# (query, [(id, title, text)], correct_id)
CASES = [
    ("how does BM25 handle document length", [
        ("right", "Okapi BM25 length normalisation",
         "BM25 divides term frequency by a factor derived from document length "
         "relative to the average, so a long document does not score highly "
         "merely by containing more words."),
        ("near1", "BM25 term frequency saturation",
         "The k1 parameter controls how quickly term frequency saturates in "
         "BM25 scoring of documents."),
        ("near2", "TF-IDF explained",
         "TF-IDF multiplies term frequency by inverse document frequency "
         "across a document collection."),
        ("near3", "BM25 implementations in search engines",
         "Many search engines implement BM25 as their default ranking "
         "function for document retrieval."),
    ], "right"),
    ("why does a headless browser get detected", [
        ("right", "Automation fingerprints in headless Chrome",
         "Headless browsers expose navigator.webdriver and differ in their "
         "rendering and timing signatures, which detection scripts read to "
         "identify automation."),
        ("near1", "Installing headless Chrome on Linux",
         "Headless Chrome can be installed from the distribution package "
         "manager and run without a display server."),
        ("near2", "Browser rendering pipeline",
         "A browser parses HTML into a DOM, applies CSS, and paints the "
         "result to a compositor layer."),
        ("near3", "Scraping with a browser",
         "Driving a real browser lets a scraper read pages whose content is "
         "produced by JavaScript after load."),
    ], "right"),
    ("what makes a TLS fingerprint identifiable", [
        ("right", "JA3 and the TLS ClientHello",
         "A JA3 hash is computed from the ordered cipher suites, extensions "
         "and curves in the ClientHello, and differs between HTTP libraries "
         "and real browsers."),
        ("near1", "TLS handshake overview",
         "The TLS handshake exchanges keys and negotiates a cipher suite "
         "before application data is encrypted."),
        ("near2", "Certificate validation",
         "A client validates the server certificate chain against its trust "
         "store during connection setup."),
        ("near3", "HTTPS performance tuning",
         "Session resumption and OCSP stapling reduce the cost of "
         "establishing TLS connections."),
    ], "right"),
    ("how do I stop one slow page costing a whole run", [
        ("right", "Per-page time budgets between retry tiers",
         "Checking a whole-page budget before each escalation stops a single "
         "pathological URL from spending the time allotted to every other "
         "page in the batch."),
        ("near1", "HTTP timeouts",
         "Setting a socket timeout bounds how long a single request waits "
         "for a response from the server."),
        ("near2", "Retrying failed requests",
         "Bounded retries with backoff recover from transient network "
         "failures without hammering the origin."),
        ("near3", "Concurrency limits",
         "Limiting simultaneous requests protects both the client and the "
         "server from overload."),
    ], "right"),
]


def reciprocal_rank(rows, correct):
    for position, row in enumerate(rows, 1):
        if row["id"] == correct:
            return 1.0 / position
    return 0.0


def main():
    if not rank.available()["rerank"]:
        print("cross-encoder not installed; nothing to measure")
        return 1

    print("%-46s %10s %10s" % ("query", "bm25", "+rerank"))
    print("-" * 68)
    bm25_rr, rerank_rr = [], []
    bm25_ms, rerank_ms = [], []

    for query, docs, correct in CASES:
        pool = [{"id": i, "title": t, "text": x} for i, t, x in docs]

        started = time.perf_counter()
        lexical = rank.rank_rows(list(pool), query)
        bm25_ms.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        reranked = rank.cross_encode(list(lexical), query)
        rerank_ms.append((time.perf_counter() - started) * 1000)

        a, b = reciprocal_rank(lexical, correct), reciprocal_rank(reranked, correct)
        bm25_rr.append(a)
        rerank_rr.append(b)
        flag = "" if b == a else ("  better" if b > a else "  WORSE")
        print("%-46s %10.2f %10.2f%s" % (query[:46], a, b, flag))

    print("-" * 68)
    print("%-46s %10.3f %10.3f" % ("MEAN RECIPROCAL RANK",
                                   sum(bm25_rr) / len(bm25_rr),
                                   sum(rerank_rr) / len(rerank_rr)))
    print("%-46s %9.2fms %9.2fms" % ("median cost per query",
                                     sorted(bm25_ms)[len(bm25_ms) // 2],
                                     sorted(rerank_ms)[len(rerank_ms) // 2]))
    gain = (sum(rerank_rr) - sum(bm25_rr)) / len(bm25_rr)
    print("\nchange from reranking: %+.3f MRR" % gain)
    if gain <= 0:
        print("The cross-encoder is not earning its cost on this set. That is a")
        print("result worth having: it is a stage that can be left off.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
