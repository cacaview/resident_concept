#!/usr/bin/env python3
"""Enrich the harness replay world with multi-source captures (ADR-0015
follow-up): the 28-day confirmation showed the H1 chain blocked ONLY by
source diversity — every replay topic had exactly ONE source page, so
re-encounters were always same-source. This script probes candidate pages
from DIFFERENT domains per topic (real network, SSRF-guarded, explicit
allow_proxy_dns for this host) and captures the successes into a
HARNESS-ONLY snapshot store. The real resident_home is never written.

Usage: scripts/enrich_replay_world.py [--harness-snapshots DIR]
"""
import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resident.web_fetch import FetchError, SafeFetcher  # noqa: E402
from resident.world_source import (  # noqa: E402
    LiveWebWorldSource, SeedEntry, WorldSnapshotStore,
)

REPO = Path(__file__).resolve().parents[2]
ORIGINAL = REPO / "resident_home" / "world_snapshots"

# candidates: (url, source label, declared topic) — 2nd/3rd sources from
# DIFFERENT domains per topic, stable static-HTML pages preferred
CANDIDATES = [
    SeedEntry("https://www.britannica.com/topic/pasta", "britannica", "food",
              title="Pasta | Britannica", summary="Encyclopaedia Britannica on pasta."),
    SeedEntry("https://plato.stanford.edu/entries/memory/", "sep", "memory",
              title="Memory - Stanford Encyclopedia of Philosophy",
              summary="SEP entry on memory."),
    SeedEntry("https://www.britannica.com/science/circadian-rhythm", "britannica", "biology",
              title="Circadian rhythm | Britannica", summary="Britannica on circadian rhythms."),
    SeedEntry("https://www.britannica.com/art/harmony", "britannica", "music",
              title="Harmony | Britannica", summary="Britannica on musical harmony."),
    SeedEntry("https://plato.stanford.edu/entries/attention/", "sep", "cognition",
              title="Attention - Stanford Encyclopedia of Philosophy",
              summary="SEP entry on attention."),
    SeedEntry("https://www.britannica.com/science/mycorrhiza", "britannica", "mycology",
              title="Mycorrhiza | Britannica", summary="Britannica on mycorrhiza."),
    SeedEntry("https://science.nasa.gov/astrophysics/", "nasa", "astrophysics",
              title="Astrophysics - NASA Science", summary="NASA astrophysics overview."),
    SeedEntry("https://www.britannica.com/topic/writing", "britannica", "history",
              title="Writing | Britannica", summary="Britannica on the history of writing."),
    SeedEntry("https://plato.stanford.edu/entries/work/", "sep", "work",
              title="Work - Stanford Encyclopedia", summary="SEP entry related to work."),
    SeedEntry("https://en.wikisource.org/wiki/Alice%27s_Adventures_in_Wonderland_(1866)",
              "wikisource", "literature",
              title="Alice's Adventures in Wonderland (1866) - Wikisource",
              summary="Wikisource edition of the book."),
    SeedEntry("https://www.reuters.com/", "reuters", "news",
              title="Reuters", summary="Reuters news front page."),
    SeedEntry("https://www.britannica.com/science/entropy", "britannica", "science",
              title="Entropy | Britannica", summary="Britannica on entropy."),
]


CANDIDATES += [
    SeedEntry("https://en.wikipedia.org/wiki/Astrophysics", "wikipedia", "astrophysics",
              title="Astrophysics - Wikipedia", summary="Wikipedia on astrophysics."),
    SeedEntry("https://en.wikipedia.org/wiki/Flow_(psychology)", "wikipedia", "cognition",
              title="Flow (psychology) - Wikipedia", summary="Wikipedia on flow."),
    SeedEntry("https://en.wikipedia.org/wiki/Music_theory", "wikipedia", "music",
              title="Music theory - Wikipedia", summary="Wikipedia on music theory."),
    SeedEntry("https://en.wikipedia.org/wiki/Pasta", "wikipedia", "food",
              title="Pasta - Wikipedia", summary="Wikipedia on pasta."),
    SeedEntry("https://en.wikipedia.org/wiki/Procrastination", "wikipedia", "work",
              title="Procrastination - Wikipedia", summary="Wikipedia on procrastination."),
    SeedEntry("https://en.wikipedia.org/wiki/Mycelium", "wikipedia", "mycology",
              title="Mycelium - Wikipedia", summary="Wikipedia on mycelium."),
    SeedEntry("https://en.wikipedia.org/wiki/History_of_writing", "wikipedia", "history",
              title="History of writing - Wikipedia", summary="Wikipedia on the history of writing."),
]


CANDIDATES += [
    SeedEntry("https://zh.wikipedia.org/wiki/心流", "wikipedia", "cognition",
              title="心流 - 维基百科", summary="关于心流的百科条目。"),
    SeedEntry("https://zh.wikipedia.org/wiki/拖延", "wikipedia", "work",
              title="拖延 - 维基百科", summary="关于拖延的百科条目。"),
    SeedEntry("https://zh.wikipedia.org/wiki/音乐理论", "wikipedia", "music",
              title="音乐理论 - 维基百科", summary="关于音乐理论的百科条目。"),
    SeedEntry("https://zh.wikipedia.org/wiki/天体物理学", "wikipedia", "astrophysics",
              title="天体物理学 - 维基百科", summary="关于天体物理学的百科条目。"),
    SeedEntry("https://zh.wikipedia.org/wiki/意大利面", "wikipedia", "food",
              title="意大利面 - 维基百科", summary="关于意面的百科条目。"),
    SeedEntry("https://zh.wikipedia.org/wiki/菌絲體", "wikipedia", "mycology",
              title="菌絲體 - 维基百科", summary="关于菌丝体的百科条目。"),
    SeedEntry("https://zh.wikipedia.org/wiki/文字史", "wikipedia", "history",
              title="文字史 - 维基百科", summary="关于文字史的百科条目。"),
]


CANDIDATES += [
    SeedEntry("https://plato.stanford.edu/entries/consciousness/", "sep", "cognition",
              title="Consciousness - SEP", summary="SEP entry on consciousness."),
    SeedEntry("https://plato.stanford.edu/entries/time/", "sep", "memory",
              title="Time - SEP", summary="SEP entry on time."),
    SeedEntry("https://science.nasa.gov/solar-system/", "nasa", "astrophysics",
              title="Solar System - NASA", summary="NASA solar system overview."),
    SeedEntry("https://science.nasa.gov/universe/", "nasa", "astrophysics",
              title="Universe - NASA", summary="NASA universe overview."),
    SeedEntry("https://www.gutenberg.org/files/84/84-h/84-h.htm", "gutenberg", "literature",
              title="Frankenstein - Gutenberg", summary="Gutenberg edition of Frankenstein."),
    SeedEntry("https://www.gutenberg.org/files/2701/2701-h/2701-h.htm", "gutenberg", "literature",
              title="Moby Dick - Gutenberg", summary="Gutenberg edition of Moby Dick."),
    SeedEntry("https://arxiv.org/list/cs.AI/recent", "arxiv", "tech",
              title="cs.AI recent - arXiv", summary="arXiv AI listing."),
    SeedEntry("https://developer.mozilla.org/en-US/docs/Web/JavaScript", "mdn", "tech",
              title="JavaScript - MDN", summary="MDN JavaScript guide."),
    SeedEntry("https://plato.stanford.edu/entries/identity-personal/", "sep", "memory",
              title="Personal Identity - SEP", summary="SEP entry on personal identity."),
    SeedEntry("https://plato.stanford.edu/entries/sleep/", "sep", "biology",
              title="Sleep - SEP", summary="SEP entry on sleep."),
    SeedEntry("https://science.nasa.gov/sun/", "nasa", "biology",
              title="Sun - NASA", summary="NASA sun overview."),
    SeedEntry("https://www.gutenberg.org/files/64317/64317-h/64317-h.htm", "gutenberg", "mycology",
              title="Gutenberg mycology text", summary="Gutenberg natural-history text."),
]


# --- H1 multi-source enrichment round 2 (2026-09-13): give each existing
# topic a genuinely distinct second domain (britannica is dead 403; en.wikipedia
# rate-limited last run — zh.wikipedia and other domains preferred, retries kept
# honest). New topics: agriculture, ocean.
CANDIDATES += [
    # cognition: currently sep-only -> add IEP + wikipedia
    SeedEntry("https://iep.utm.edu/consciousness/", "iep", "cognition",
              title="Consciousness - Internet Encyclopedia of Philosophy",
              summary="IEP entry on consciousness."),
    SeedEntry("https://iep.utm.edu/attention/", "iep", "cognition",
              title="Attention - IEP", summary="IEP entry on attention."),
    SeedEntry("https://en.wikipedia.org/wiki/Attention", "wikipedia", "cognition",
              title="Attention - Wikipedia", summary="Wikipedia on attention."),
    # biology: 1 page per domain -> deepen wikipedia, retry SEP sleep
    SeedEntry("https://en.wikipedia.org/wiki/Sleep", "wikipedia", "biology",
              title="Sleep - Wikipedia", summary="Wikipedia on sleep."),
    SeedEntry("https://zh.wikipedia.org/wiki/昼夜节律", "wikipedia", "biology",
              title="昼夜节律 - 维基百科", summary="关于昼夜节律的百科条目。"),
    SeedEntry("https://plato.stanford.edu/entries/sleep/", "sep", "biology",
              title="Sleep - SEP", summary="SEP entry on sleep (retry)."),
    # astrophysics: nasa-only -> add ESA + arxiv
    SeedEntry("https://www.esa.int/Science_Exploration/Space_Science", "esa", "astrophysics",
              title="Space Science - ESA", summary="ESA space science overview."),
    SeedEntry("https://www.esa.int/Science_Exploration/Space_Science/Webb", "esa", "astrophysics",
              title="Webb - ESA", summary="ESA James Webb Space Telescope pages."),
    SeedEntry("https://arxiv.org/list/astro-ph/recent", "arxiv", "astrophysics",
              title="astro-ph recent - arXiv", summary="arXiv astrophysics listing."),
    SeedEntry("https://zh.wikipedia.org/wiki/天体物理学", "wikipedia", "astrophysics",
              title="天体物理学 - 维基百科", summary="关于天体物理学的百科条目。"),
    # food: wikipedia-only -> add wikibooks cookbook (distinct domain)
    SeedEntry("https://en.wikibooks.org/wiki/Cookbook:Pasta", "wikibooks", "food",
              title="Cookbook:Pasta - Wikibooks", summary="Wikibooks cookbook on pasta."),
    SeedEntry("https://en.wikibooks.org/wiki/Cookbook:Cuisine_of_Italy", "wikibooks", "food",
              title="Cookbook:Cuisine of Italy - Wikibooks", summary="Wikibooks on Italian cuisine."),
    # music: 0 captures so far (britannica 403, wikipedia missed) -> retry
    SeedEntry("https://en.wikipedia.org/wiki/Music_theory", "wikipedia", "music",
              title="Music theory - Wikipedia", summary="Wikipedia on music theory."),
    SeedEntry("https://zh.wikipedia.org/wiki/音乐理论", "wikipedia", "music",
              title="音乐理论 - 维基百科", summary="关于音乐理论的百科条目。"),
    SeedEntry("https://en.wikibooks.org/wiki/Music_Theory", "wikibooks", "music",
              title="Music Theory - Wikibooks", summary="Wikibooks music theory textbook."),
    # work: 0 captures so far -> retry
    SeedEntry("https://en.wikipedia.org/wiki/Procrastination", "wikipedia", "work",
              title="Procrastination - Wikipedia", summary="Wikipedia on procrastination."),
    # literature: gutenberg-only -> add wikisource domain
    SeedEntry("https://en.wikisource.org/wiki/Alice%27s_Adventures_in_Wonderland_(1866)",
              "wikisource", "literature",
              title="Alice's Adventures in Wonderland (1866) - Wikisource",
              summary="Wikisource edition (retry)."),
    SeedEntry("https://en.wikisource.org/wiki/Frankenstein,_or_the_Modern_Prometheus_(1831)",
              "wikisource", "literature",
              title="Frankenstein (1831) - Wikisource", summary="Wikisource edition of Frankenstein."),
    # news: bbc-only -> add AP News (reuters 401'd before)
    SeedEntry("https://apnews.com/", "apnews", "news",
              title="AP News", summary="Associated Press front page."),
    SeedEntry("https://www.reuters.com/", "reuters", "news",
              title="Reuters", summary="Reuters front page (retry)."),
    # science: arxiv-only -> add wikipedia entropy
    SeedEntry("https://en.wikipedia.org/wiki/Entropy", "wikipedia", "science",
              title="Entropy - Wikipedia", summary="Wikipedia on entropy."),
    # new topic: agriculture
    SeedEntry("https://www.fao.org/home/en/", "fao", "agriculture",
              title="FAO - Home", summary="FAO overview."),
    SeedEntry("https://en.wikipedia.org/wiki/Agriculture", "wikipedia", "agriculture",
              title="Agriculture - Wikipedia", summary="Wikipedia on agriculture."),
    SeedEntry("https://zh.wikipedia.org/wiki/农业", "wikipedia", "agriculture",
              title="农业 - 维基百科", summary="关于农业的百科条目。"),
    # new topic: ocean
    SeedEntry("https://www.noaa.gov/", "noaa", "ocean",
              title="NOAA", summary="NOAA overview."),
    SeedEntry("https://en.wikipedia.org/wiki/Ocean", "wikipedia", "ocean",
              title="Ocean - Wikipedia", summary="Wikipedia on the ocean."),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness-snapshots",
                    default=str(Path(__file__).resolve().parent / "out" / "harness_snapshots"))
    args = ap.parse_args()
    hs_dir = Path(args.harness_snapshots)

    # seed the harness store with the ORIGINAL captures (immutable copies)
    if hs_dir.exists() and not any(hs_dir.iterdir()):
        shutil.rmtree(hs_dir)
    if not hs_dir.exists():
        shutil.copytree(ORIGINAL, hs_dir)
        print(f"[enrich] copied {len(list(hs_dir.iterdir()))} original snapshots -> {hs_dir}")
    store = WorldSnapshotStore(hs_dir)
    before = len(store.all_ids())

    fetcher = SafeFetcher(allow_proxy_dns=True)  # this host's DNS quirk (ADR-0009)
    source = LiveWebWorldSource(CANDIDATES, store, fetcher=fetcher)
    ok, failed = [], []
    for seed in CANDIDATES:
        item = seed.item()
        try:
            mat = source.observe(item, now_iso=seed.url and __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(),
                context={"entry_mode": "enrichment"})
            ok.append((seed.source, seed.topic, mat.item.title,
                       mat.fetch.get("byte_size") if mat.fetch else None))
        except FetchError as exc:
            failed.append((seed.source, seed.topic, seed.url, exc.reason))
        __import__("time").sleep(4)
    after = len(store.all_ids())
    print(f"[enrich] captures: {before} -> {after} (+{after - before})")
    print("OK:")
    for s, t, title, size in ok:
        print(f"  {s}/{t}: {title} ({size}B)")
    print("FAILED:")
    for s, t, url, reason in failed:
        print(f"  {s}/{t}: {reason} ({url})")

    # multi-source coverage report
    by_topic: dict[str, set] = {}
    for sid in store.all_ids():
        rec = store.load(sid) or {}
        if rec.get("status") != 200:
            continue
        declared = rec.get("declared") or {}
        by_topic.setdefault(declared.get("topic") or rec.get("source_type"),
                            set()).add(declared.get("source") or rec.get("source_type"))
    multi = {t: sorted(s) for t, s in sorted(by_topic.items()) if len(s) >= 2}
    print(f"[enrich] topics with >=2 sources: {len(multi)}")
    for t, s in multi.items():
        print(f"  {t}: {s}")


if __name__ == "__main__":
    main()
