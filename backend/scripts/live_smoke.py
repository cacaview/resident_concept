#!/usr/bin/env python3
"""v0.2 Step 1 live smoke (ADR-0009): the visual organ against the real web.

Engineering-only: proves "given a URL, Resident can safely/truly/replayably
experience it". No cognitive claims. Includes two deliberate probes: a cloud
metadata endpoint (must be BLOCKED by the SSRF guard) and a URL that will 404.

Also proves the capture→replay round trip on REAL data: after capture, a
ReplayWorldSource rebuilt from the snapshot store re-serves every observation
byte-identically (title/summary/snapshot_id), fully offline.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resident.web_fetch import FetchError, SafeFetcher  # noqa: E402
from resident.world_source import (  # noqa: E402
    LiveWebWorldSource,
    ReplayWorldSource,
    SeedEntry,
    WorldSnapshotStore,
)

SNAP_DIR = Path(__file__).resolve().parents[1] / ".." / "resident_home" / "world_snapshots"
OUT_DIR = Path(__file__).resolve().parents[1] / "out" / "live_smoke"

SEEDS = [
    # (url, source label, declared topic) — a bounded frontier, ~15 lookouts
    SeedEntry("https://example.com/", "example", "reference",
              title="Example domain", summary="一个保留给文档示例的域名页面。"),
    SeedEntry("https://en.wikipedia.org/wiki/Umami", "wikipedia", "food",
              title="Umami - Wikipedia", summary="关于鲜味（umami）味道的百科条目。"),
    SeedEntry("https://zh.wikipedia.org/wiki/记忆", "wikipedia", "memory",
              title="记忆 - 维基百科", summary="关于记忆的百科条目。"),
    SeedEntry("https://en.wikipedia.org/wiki/Circadian_rhythm", "wikipedia", "biology",
              title="Circadian rhythm - Wikipedia", summary="关于昼夜节律的百科条目。"),
    SeedEntry("https://news.ycombinator.com/", "hacker_news", "tech",
              title="Hacker News", summary="技术社区的头版链接列表。"),
    SeedEntry("https://docs.python.org/3/library/ipaddress.html", "python_docs", "tech",
              title="ipaddress - Python docs", summary="Python 标准库 ipaddress 文档。"),
    SeedEntry("https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS", "mdn", "tech",
              title="CORS - MDN", summary="MDN 关于 HTTP CORS 的文档。"),
    SeedEntry("https://arxiv.org/abs/2404.07143", "arxiv", "science",
              title="arXiv abstract", summary="arXiv 上的一篇论文摘要页。"),
    SeedEntry("https://www.bbc.com/news", "bbc", "news",
              title="BBC News", summary="BBC 新闻首页。"),
    SeedEntry("https://www.gutenberg.org/files/11/11-h/11-h.htm", "gutenberg", "literature",
              title="Alice's Adventures in Wonderland", summary="古腾堡计划的一本书的 HTML 版。"),
    SeedEntry("https://httpbin.org/html", "httpbin", "reference",
              title="httpbin html", summary="httpbin 的测试 HTML 页面。"),
    SeedEntry("https://www.rfc-editor.org/rfc/rfc1918.txt", "rfc", "reference",
              title="RFC 1918", summary="RFC 1918 文本（私有地址分配）。"),
    SeedEntry("https://planetpython.org/", "planet_python", "tech",
              title="Planet Python", summary="Python 博客聚合页。"),
    SeedEntry("https://lobste.rs/", "lobsters", "tech",
              title="Lobsters", summary="技术社区首页。"),
    SeedEntry("https://www.gutenberg.org/ebooks/missing-book-xyz", "gutenberg", "literature",
              title="Missing book (deliberate 404 probe)", summary="一个故意不存在的页面，用于真实 404。"),
]

PROBES = [
    SeedEntry("http://169.254.169.254/latest/meta-data/", "metadata_probe", "probe",
              title="Cloud metadata endpoint (must be BLOCKED)",
              summary="link-local 元数据端点——SSRF 防护必须拒绝。"),
]


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snapshots = WorldSnapshotStore(SNAP_DIR)
    # the smoke run is the explicit operator action: this host's DNS answers
    # from the benchmark range (transparent proxy), so the fetcher runs with
    # the documented allow_proxy_dns opt-in (ADR-0009). TLS stays host-bound.
    fetcher = SafeFetcher(total_timeout=25.0, allow_proxy_dns=True)
    live = LiveWebWorldSource(SEEDS + PROBES, snapshots, fetcher=fetcher)

    rows = []
    for seed in SEEDS + PROBES:
        item = seed.item()
        t0 = time.monotonic()
        row = {"url": seed.url, "declared_topic": seed.topic, "probe": seed in PROBES}
        try:
            mat = live.observe(item, now_iso=datetime.now(timezone.utc).isoformat())
            f = mat.fetch
            row.update({
                "ok": True, "status": f["status"], "content_type": f["content_type"],
                "source_type": f["source_type"], "title": mat.item.title,
                "text_chars": len(mat.item.summary),
                "byte_size": f["byte_size"], "redirects": f["redirect_count"],
                "duration_ms": f["duration_ms"], "snapshot_id": f["snapshot_id"],
                "links_found": len(src_links(live, mat.item.id)),
                "final_url": f["final_url"],
            })
        except FetchError as exc:
            row.update({"ok": False, "reason": exc.reason, "detail": exc.detail})
        row["wall_ms"] = int((time.monotonic() - t0) * 1000)
        rows.append(row)
        tag = "OK " if row.get("ok") else ("BLK" if row.get("reason") == "fetch_blocked" else "ERR")
        print(f"[{tag}] {seed.url} -> {row.get('title') or row.get('reason')}", flush=True)

    # ---- capture -> replay round trip on REAL captures (offline) ----
    replay = ReplayWorldSource(SEEDS + PROBES, WorldSnapshotStore(SNAP_DIR))
    replay_rows = []
    for row in rows:
        if not row.get("ok"):
            continue
        item = next(s.item() for s in SEEDS + PROBES if s.url == row["url"])
        try:
            mat = replay.observe(item, now_iso=item.id)
            same = (mat.item.title == row["title"] and
                    mat.fetch["snapshot_id"] == row["snapshot_id"])
            replay_rows.append({"url": row["url"], "replay_identical": same})
        except FetchError as exc:
            replay_rows.append({"url": row["url"], "replay_identical": False,
                                "reason": exc.reason})

    summary = {
        "n_seeds": len(SEEDS), "n_probes": len(PROBES),
        "ok": sum(1 for r in rows if r.get("ok")),
        "blocked": sum(1 for r in rows if r.get("reason") == "fetch_blocked"),
        "failed": sum(1 for r in rows if r.get("ok") is False and r.get("reason") != "fetch_blocked"),
        "total_bytes": sum(r.get("byte_size", 0) for r in rows),
        "snapshots": snapshots.count(),
        "replay_identical": sum(1 for r in replay_rows if r.get("replay_identical")),
        "replay_checked": len(replay_rows),
        "rows": rows, "replay": replay_rows,
    }
    (OUT_DIR / "report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("rows", "replay")},
                     ensure_ascii=False, indent=2))


def src_links(live, item_id):
    item = live.get(item_id)
    return live.outgoing_links(item) if item else []


if __name__ == "__main__":
    main()
