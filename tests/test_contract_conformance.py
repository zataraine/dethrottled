"""Conformance suite for the web engine contract.

Engine-agnostic on purpose. It describes what an engine implementing the
contract must do, and is run against any of them by pointing ENGINE_URL at one:

    ENGINE_URL=http://localhost:8787 python -m unittest tests.test_contract -v
    ENGINE_URL=https://example.invalid ENGINE_KEY=... python -m unittest ...

Written as unittest rather than pytest fixtures so the same file runs unchanged
under `python -m unittest` and under `pytest`, in whichever repo it sits.

WHY THIS EXISTS
---------------
Two engines implementing "the same" API drifted until the same JSON body meant
different things in each, and every one of the resulting failures was silent:

  render   typed bool in one and str in the other -- every fetch through a
           bridge written against the first returned 422 against the second.
  fresh    honoured by one, accepted-and-ignored by the other -- an external
           benchmark was built on it and published the wrong conclusion.
  robots   honoured by one, not checked at all by the other -- the same URL
           fetches from one engine and is refused by the other.

None of those produced an error anyone could see. A status code of 200 is not
evidence a field did anything, so the assertions below check OBSERVABLE
BEHAVIOUR wherever a field claims to change behaviour. That is the difference
between a suite that would have caught these and one that would not.

EXPECT FAILURES ON FIRST RUN. This encodes the target contract, not current
behaviour. A failure here is a gap to close or a deliberate exception to record,
not necessarily a bug to panic about -- run it as a report before you run it as
a gate.
"""
from __future__ import annotations

import json
import os
import unittest
import urllib.error
import urllib.request

BASE = os.environ.get("ENGINE_URL", "").rstrip("/")
KEY = os.environ.get("ENGINE_KEY", "")
TIMEOUT = float(os.environ.get("ENGINE_TIMEOUT", "60"))

# A URL whose robots.txt disallows crawling. Overridable because a site can
# change its policy and this suite should not start lying when it does.
ROBOTS_DISALLOWED_URL = os.environ.get(
    "ENGINE_ROBOTS_TEST_URL", "https://www.reuters.com/world/")

# Stable, cheap, unlikely to be behind a challenge.
PLAIN_URL = os.environ.get(
    "ENGINE_PLAIN_TEST_URL", "https://en.wikipedia.org/wiki/Okapi_BM25")


def _post(path: str, body: dict, timeout: float | None = None):
    """POST and return (status, parsed_json_or_text). Never raises on 4xx/5xx.

    HTTP errors are returned rather than raised because a 400 is a PASS for
    several tests here -- rejecting a field you cannot honour is the correct
    behaviour, and a suite that treats every non-200 as an error cannot express
    that.
    """
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + KEY} if KEY else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout or TIMEOUT) as response:
            raw = response.read().decode("utf-8", "replace")
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def _get(path: str):
    request = urllib.request.Request(
        BASE + path,
        headers={**({"Authorization": "Bearer " + KEY} if KEY else {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return 0, None


# Either is a valid "I will not do that". FastAPI answers schema violations
# with 422 and hand-rolled guards usually with 400; the contract cares that the
# caller was TOLD, not which of the two numbers carried the message.
REJECTED = (400, 422)


def _rows(payload):
    """Both list-shaped and {"results": [...]}-shaped answers, as rows."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("results") or payload.get("rows") or []
    return []


@unittest.skipIf(not BASE, "set ENGINE_URL to the engine under test")
class ContractTests(unittest.TestCase):

    # ---- the three verbs ------------------------------------------------

    def test_search_exists_and_returns_rows(self):
        status, payload = _post("/search", {"query": "okapi bm25 ranking", "limit": 3})
        self.assertEqual(status, 200, payload)
        rows = _rows(payload)
        self.assertTrue(rows, "search returned no rows")
        self.assertIn("url", rows[0])

    def test_fetch_exists_and_returns_rows(self):
        status, payload = _post("/fetch", {"urls": [PLAIN_URL], "max_chars": 300})
        self.assertEqual(status, 200, payload)
        rows = _rows(payload)
        self.assertTrue(rows, "fetch returned no rows")
        self.assertIn("url", rows[0])

    def test_search_and_fetch_exists(self):
        status, payload = _post(
            "/search-and-fetch", {"query": "okapi bm25", "limit": 1, "max_chars": 300})
        self.assertEqual(status, 200, payload)
        self.assertTrue(_rows(payload), "search-and-fetch returned no rows")

    # ---- field TYPES, identical across engines ---------------------------

    def test_render_accepts_the_contract_enum(self):
        """render is auto | always | never on every engine.

        Three states because a bool cannot express "never", and "never" is what
        a latency-sensitive caller wants when it knows the page is static.
        """
        for mode in ("auto", "always", "never"):
            with self.subTest(render=mode):
                status, payload = _post(
                    "/fetch",
                    {"urls": [PLAIN_URL], "max_chars": 200, "render": mode})
                self.assertEqual(status, 200, "render=%r rejected: %s" % (mode, payload))

    def test_render_rejects_the_wrong_type(self):
        """The exact drift that broke every fetch through the MCP bridge.

        One engine typed render as bool and the other as str. Whichever type an
        engine takes, the OTHER type must be a loud 400 -- never a 200 that
        quietly does something else, and never a 500.
        """
        status, payload = _post(
            "/fetch", {"urls": [PLAIN_URL], "max_chars": 200, "render": False})
        self.assertIn(
            status, REJECTED,
            "a boolean render must be rejected, got %s: %s" % (status, payload))

    def test_limit_is_the_result_count_field(self):
        """One name for result count. Not num_results, not max_items, not both."""
        status, payload = _post("/search", {"query": "okapi bm25", "limit": 2})
        self.assertEqual(status, 200, payload)
        self.assertLessEqual(len(_rows(payload)), 2, "limit did not bound the result count")

    def test_format_selects_the_output_shape(self):
        """format replaces the raw/links booleans and the extract-with-links route."""
        seen = {}
        for shape in ("text", "links", "html"):
            with self.subTest(format=shape):
                status, payload = _post(
                    "/fetch",
                    {"urls": [PLAIN_URL], "max_chars": 4000, "format": shape})
                self.assertEqual(status, 200, "format=%r rejected: %s" % (shape, payload))
                rows = _rows(payload)
                self.assertTrue(rows)
                seen[shape] = rows[0].get("content") or ""

        # The shapes must actually differ, or `format` is decorative.
        self.assertNotEqual(
            seen.get("text"), seen.get("html"),
            "format=text and format=html returned identical content")
        self.assertIn(
            "](http", seen.get("links", ""),
            "format=links returned no markdown links")

    # ---- honoured or rejected, never silently dropped --------------------

    def test_unknown_fields_are_rejected(self):
        """So that adding a field is a deliberate act on both engines.

        An engine that accepts anything cannot tell a caller they have made a
        typo, and a caller who mistypes `limit` as `limits` silently gets the
        default forever.
        """
        status, payload = _post(
            "/search", {"query": "okapi bm25", "limit": 2, "not_a_real_field": True})
        self.assertIn(
            status, REJECTED,
            "an unknown field must be rejected, got %s: %s" % (status, payload))

    def test_fresh_is_honoured_or_rejected_but_never_ignored(self):
        """The failure that invalidated an entire external benchmark.

        Asserted on a declared cache signal rather than on timing, because a
        timing assertion on a network call is a flake generator.

        This test SKIPS rather than passes when the engine gives no trustworthy
        signal, and that distinction was learned the hard way: an earlier
        version of it read the per-row `cached` flag and scored a PASS for an
        engine whose own source says it ignores `fresh`. That flag reports
        whether the PAGE was cached, not whether the search result set was
        replayed -- so the test was reading a real field that answered a
        different question. A false pass is worse than no test, because it
        retires the suspicion that would otherwise have found the bug.

        Hence the contract requirement: a response must say, at the envelope
        level, whether it came from cache. Without that no caller can verify
        `fresh` did anything -- which is precisely how a careful external agent
        came to benchmark a parameter that does nothing.
        """
        body = {"query": "conformance probe okapi bm25 ranking function", "limit": 3,
                "fresh": True}
        first_status, first = _post("/search", body)
        if first_status in REJECTED:
            return  # an honest refusal is a pass
        self.assertEqual(first_status, 200)

        second_status, second = _post("/search", body)
        self.assertEqual(second_status, 200)

        # Envelope-level cache signal, which is what the contract requires.
        envelope = second if isinstance(second, dict) else {}
        if "cached" in envelope:
            self.assertFalse(
                envelope["cached"],
                "fresh=true was accepted but the result set came from cache -- "
                "honour the field or reject it")
            return

        self.skipTest(
            "engine declares no envelope-level `cached` field, so `fresh` "
            "cannot be verified by a caller. Per-row `cached` is not a "
            "substitute: it reports page-cache state, not result-set replay.")

    # ---- policy is declared, not discovered ------------------------------

    def test_capabilities_declares_policy(self):
        """A caller must be able to read policy before calling, not infer it
        from a failed fetch."""
        status, payload = _get("/v2/capabilities")
        self.assertEqual(status, 200, "no /v2/capabilities")
        self.assertIsInstance(payload, dict)
        for field in ("respects_robots", "metered_tiers", "leaves_network",
                      "cache_ttl_seconds"):
            with self.subTest(field=field):
                self.assertIn(field, payload, "capabilities does not declare %r" % field)

    def test_robots_claim_matches_behaviour(self):
        """The test that turns a documentation lie into a build failure.

        Whichever way an engine answers is fine. Claiming one and doing the
        other is not -- and that is exactly the state two engines on the same
        hostname were found in, one honouring robots.txt and one with no robots
        handling at all.
        """
        status, capabilities = _get("/v2/capabilities")
        if status != 200 or not isinstance(capabilities, dict):
            self.skipTest("no /v2/capabilities to check the claim against")
        if "respects_robots" not in capabilities:
            self.skipTest("engine does not declare respects_robots")

        claims_respect = bool(capabilities["respects_robots"])
        _, payload = _post(
            "/fetch", {"urls": [ROBOTS_DISALLOWED_URL], "max_chars": 200})
        rows = _rows(payload)
        self.assertTrue(rows, "fetch returned nothing at all")
        row = rows[0]
        refused = (
            "robots" in str(row.get("failure_reason") or "").lower()
            or "robots" in str(row.get("tier") or "").lower())
        got_content = bool((row.get("content") or "").strip())

        if claims_respect:
            self.assertTrue(
                refused and not got_content,
                "declares respects_robots=true but fetched a disallowed URL")
        else:
            self.assertFalse(
                refused,
                "declares respects_robots=false but refused on robots grounds")


if __name__ == "__main__":
    unittest.main(verbosity=2)
