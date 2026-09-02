#!/usr/bin/env python3
"""A minimal Crawl4AI HTTP server, for people who do not want Docker.

`docker compose up` is the easy path and gives you this for free. But the
Crawl4AI *pip package* ships the crawler and its browser without the HTTP
server that the Docker image runs -- so if you have installed it yourself, this
is the missing forty lines.

It speaks the subset of upstream's `/crawl` API that dethrottled uses, so
pointing at it is the same as pointing at the real thing:

    # in a SEPARATE virtualenv from dethrottled -- this pulls in a browser
    pip install crawl4ai fastapi uvicorn
    crawl4ai-setup
    python examples/crawl4ai_server.py --port 11235

    # then
    DETHROTTLED_CRAWL4AI_URL=http://127.0.0.1:11235 dethrottled

Keep it in its own environment. dethrottled talks to the renderer over HTTP and
never imports it, which is deliberate: a browser is a heavy, stateful thing with
a large dependency tree, and it has no business sharing a process with a web
service.
"""
import argparse
import asyncio

from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="crawl4ai-mini")

# One browser for the life of the process. Launching Chromium per request costs
# roughly a second and a half of pure startup, which on a tier that exists to
# be the slow-but-capable option is a cost you notice.
_crawler = None
_lock = asyncio.Lock()


class CrawlBody(BaseModel):
    urls: list[str] = []
    # Accepted and ignored: upstream takes full config objects here, and
    # accepting the fields means a client written against the real server does
    # not need a special case for this one.
    browser_config: dict | None = None
    crawler_config: dict | None = None


async def crawler() -> AsyncWebCrawler:
    global _crawler
    async with _lock:
        if _crawler is None:
            instance = AsyncWebCrawler(config=BrowserConfig(headless=True))
            await instance.start()
            _crawler = instance
    return _crawler


@app.get("/health")
async def health():
    return {"status": "ok", "service": "crawl4ai-mini"}


@app.post("/crawl")
async def crawl(body: CrawlBody):
    """Render each URL and return upstream's response shape."""
    page_timeout = ((body.crawler_config or {}).get("params", {})
                    .get("page_timeout") or 20000)
    engine = await crawler()
    results = []
    for url in body.urls:
        try:
            out = await engine.arun(url, config=CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS, page_timeout=page_timeout))
            # `markdown` is an object in recent versions and a string in older
            # ones. Callers should not have to know which they are talking to.
            markdown = getattr(out, "markdown", None)
            if markdown is not None and not isinstance(markdown, str):
                markdown = (getattr(markdown, "fit_markdown", None)
                            or getattr(markdown, "raw_markdown", None) or "")
            results.append({
                "url": getattr(out, "url", url),
                "success": bool(getattr(out, "success", False)),
                "markdown": markdown or "",
                "cleaned_html": getattr(out, "cleaned_html", "") or "",
                "html": getattr(out, "html", "") or "",
                "error_message": getattr(out, "error_message", "") or "",
            })
        except Exception as exc:
            # A failed render is a result with a reason, not a 500. One bad URL
            # in a batch must not lose the whole response.
            results.append({"url": url, "success": False, "markdown": "",
                            "error_message": "%s: %s" % (type(exc).__name__,
                                                         str(exc)[:200])})
    return {"success": True, "results": results}


def main():
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11235)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
