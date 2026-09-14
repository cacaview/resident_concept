"""Shared route-meta prefix list (single source of truth).

Round 7 (self_thread_concentration): the prefix list previously lived ONLY in
``mind_loop.py`` (``_META_PREFIXES``) and was re-declared in the harness. The
creation side (``sleep.py::_best_cross_topic_pair``) needs the SAME definition
so self-thread titles are built from substantive text only — duplicated lists
drift. Everything that decides "is this text route-meta output?" imports from
here.
"""

META_PREFIXES: tuple[str, ...] = (
    "[continuity]", "[revisit]", "[distant]", "[serendipity]",
    "[self]", "[personal]", "[world]", "[dream]",
)
