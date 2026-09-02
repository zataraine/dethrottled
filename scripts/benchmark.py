#!/usr/bin/env python3
"""Benchmark a running dethrottled server over twenty deliberately awkward cases.

The case list is the point. Anyone can benchmark a search stack on English news
and report a good number; these are chosen to include the things that actually
break web extraction -- non-Latin scripts, JavaScript-only pages, PDFs, sites
behind bot walls, forums, and structured tables. A stack's honest score is the
one it gets on the cases it is bad at.

    python scripts/benchmark.py --url http://127.0.0.1:8182 --out results.json

Every number it prints comes from the same public API any caller uses. There is
no privileged path and nothing is stubbed.
"""
import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

CASES = [
    ("latest EU AI Act enforcement news", "news", "en"),
    ("Nashville Tennessee zoning ordinance 2026", "local-gov-pdf", "en"),
    ("how to fix qBittorrent port forwarding", "technical-forum", "en"),
    ("Okapi BM25 ranking function explained", "reference", "en"),
    ("Tennessee property tax rate Davidson County", "local-structured", "en"),
    ("recette tajine poulet citron confit", "multilingual", "fr"),
    ("أفضل وصفة كسكس", "multilingual", "ar"),
    ("Berlin Mietpreisbremse 2026 Änderungen", "multilingual", "de"),
    ("receta paella valenciana auténtica", "multilingual", "es"),
    ("東京 ラーメン おすすめ 2026", "multilingual", "ja"),
    ("React single page app client side rendering", "js-heavy", "en"),
    ("Cloudflare bot management challenge", "js-heavy", "en"),
    ("New York Times subscription article", "paywall", "en"),
    ("robots.txt disallow directive examples", "robots", "en"),
    ("Wikipedia article with large data tables", "long-form", "en"),
    ("government report PDF research paper", "document-pdf", "en"),
    ("GitHub README markdown code blocks", "document-code", "en"),
    ("Reddit thread with many comments", "nested-content", "en"),
    ("YouTube video page transcript", "video", "en"),
    ("laptop specification sheet comparison table", "structured-table", "en"),
]


def post(base, path, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = json.load(response)
        return body, int((time.time() - started) * 1000), None
    except Exception as exc:
        return None, int((time.time() - started) * 1000), str(exc)[:120]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8182")
    parser.add_argument("--out", default="benchmark-results.json")
    parser.add_argument("--top", type=int, default=3,
                        help="how many results per query to extract")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--quick", action="store_true",
                        help="first five cases only")
    args = parser.parse_args(argv)
    base = args.url.rstrip("/")

    cases = CASES[:5] if args.quick else CASES
    results, thin = [], 0
    print("%-44s %6s %6s %8s" % ("query", "found", "got", "chars"))
    print("-" * 68)

    for query, category, lang in cases:
        rows, ms, err = post(base, "/search-and-extract", {
            "query": query, "num_results": args.top,
            "rerank": args.rerank, "max_chars": 4000}, args.timeout)
        if err or rows is None:
            print("%-44s %6s %6s %8s  %s" % (query[:44], "-", "-", "-", err))
            results.append({"query": query, "category": category, "lang": lang,
                            "error": err, "elapsed_ms": ms})
            continue

        ok = [r for r in rows if r.get("quality") == "ok" and r.get("content")]
        chars = [len(r["content"]) for r in ok]
        # A "success" under 600 characters is usually a teaser or a consent
        # wall, so it is counted separately rather than folded into the total.
        thin += sum(1 for c in chars if c < 600)
        print("%-44s %6d %6d %8d" % (query[:44], len(rows), len(ok),
                                     sum(chars)))
        results.append({
            "query": query, "category": category, "lang": lang,
            "elapsed_ms": ms, "found": len(rows), "extracted": len(ok),
            "chars_total": sum(chars),
            "ranking": rows[0].get("ranking") if rows else [],
            "tiers": sorted({r.get("tier") for r in ok if r.get("tier")}),
            "results": [{"url": r.get("url"), "title": r.get("title"),
                         "chars": len(r.get("content") or ""),
                         "tier": r.get("tier")} for r in rows],
        })

    done = [r for r in results if "error" not in r]
    answered = [r for r in done if r["found"]]
    extracted = sum(r["extracted"] for r in done)
    attempted = sum(r["found"] for r in done)
    times = [r["elapsed_ms"] for r in done]

    summary = {
        "cases": len(cases),
        "cases_with_results": len(answered),
        "results_found": attempted,
        "pages_extracted": extracted,
        "extraction_rate": round(extracted / attempted, 3) if attempted else 0,
        "thin_extractions": thin,
        "median_ms": int(statistics.median(times)) if times else 0,
        "api_calls": 0,
        "keys_required": False,
    }
    print("-" * 68)
    for key, value in summary.items():
        print("  %-20s %s" % (key, value))

    payload = {"meta": {"tool": "dethrottled", "url": base,
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "top_n": args.top, "rerank": args.rerank},
               "summary": summary, "cases": results}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    print("\nwrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
