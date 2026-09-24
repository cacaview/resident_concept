#!/usr/bin/env python3
"""Probe world-seed reachability from this host's egress (domestic profile).

Runs the production SafeFetcher against every current seed + candidate
replacement seeds and reports, per URL: ok (status/title/bytes/text-len) or the
exact FetchError reason. Output: one line per URL, machine-parseable.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from resident.web_fetch import SafeFetcher  # noqa: E402

CANDIDATES = [
    # current 18 (from backend/seeds/world_seeds.json)
    ("wikipedia", "https://en.wikipedia.org/wiki/Umami"),
    ("wikipedia", "https://zh.wikipedia.org/wiki/%E8%AE%B0%E5%BF%86"),
    ("wikipedia", "https://en.wikipedia.org/wiki/Circadian_rhythm"),
    ("wikipedia", "https://en.wikipedia.org/wiki/Music_theory"),
    ("wikipedia", "https://en.wikipedia.org/wiki/Pasta"),
    ("wikipedia", "https://en.wikipedia.org/wiki/Mycelium"),
    ("wikipedia", "https://en.wikipedia.org/wiki/Procrastination"),
    ("wikipedia", "https://en.wikipedia.org/wiki/Flow_(psychology)"),
    ("wikipedia", "https://en.wikipedia.org/wiki/History_of_writing"),
    ("wikipedia", "https://en.wikipedia.org/wiki/Astrophysics"),
    ("hacker_news", "https://news.ycombinator.com/"),
    ("lobsters", "https://lobste.rs/"),
    ("mdn", "https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS"),
    ("python_docs", "https://docs.python.org/3/library/ipaddress.html"),
    ("arxiv", "https://arxiv.org/abs/2404.07143"),
    ("bbc", "https://www.bbc.com/news"),
    ("gutenberg", "https://www.gutenberg.org/files/11/11-h/11-h.htm"),
    ("rfc", "https://www.rfc-editor.org/rfc/rfc1918.txt"),
    # candidates (diverse sources, domestic-egress friendly, HTML/text)
    ("ruanyifeng", "https://www.ruanyifeng.com/blog/2015/07/readable-code.html"),
    ("sqlite", "https://sqlite.org/lang_select.html"),
    ("kernel", "https://www.kernel.org/"),
    ("unicode", "https://www.unicode.org/standard/"),
    ("peps", "https://peps.python.org/pep-0020/"),
    ("w3c", "https://www.w3.org/TR/WCAG21/"),
    ("gnu", "https://www.gnu.org/philosophy/free-sw.html"),
    ("nginx", "https://nginx.org/en/docs/http/"),
    ("postgresql", "https://www.postgresql.org/docs/current/intro.html"),
    ("python_org", "https://www.python.org/about/gettingstarted/"),
    ("fowler", "https://martinfowler.com/bliki/PrincipledCodebase.html"),
    ("wikibooks", "https://en.wikibooks.org/wiki/Music_Theory/Introduction"),
]


def main() -> None:
    fetcher = SafeFetcher(connect_timeout=8.0, total_timeout=20.0,
                          allow_proxy_dns=True)
    print("probe-start", flush=True)
    for source, url in CANDIDATES:
        t0 = time.monotonic()
        try:
            res = fetcher.fetch(url)
            dt = time.monotonic() - t0
            print(f"OK\t{source}\t{url}\t{res.status}\t{res.title[:60]!r}\t"
                  f"text_len={len(res.text)}\t{dt:.1f}s", flush=True)
        except Exception as exc:  # noqa: BLE001 - report every failure kind
            dt = time.monotonic() - t0
            reason = getattr(exc, "reason", "unknown")
            print(f"FAIL\t{source}\t{url}\t{reason}\t{str(exc)[:120]}\t{dt:.1f}s", flush=True)
    print("probe-done", flush=True)


if __name__ == "__main__":
    main()
