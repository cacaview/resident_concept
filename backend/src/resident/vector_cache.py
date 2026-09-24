"""The versioned vector cache (v0.2 Step 2, ADR-0010).

Real embeddings are expensive and (per provider) not reproducible run-to-run;
the shadow/treatment experiments need determinism. Rule: **a cached vector is
valid only for an exact identity match** — provider, model, version, dimension
and normalization must all agree, AND the *exact text* (content hash) must
match. A provider/model change or a text change therefore can never reuse an
incompatible vector (the brief's hard rule), and once a text is cached the
simulation is deterministic without touching the network again.

Storage: one sqlite file per cache namespace (a sim dir, or the resident home)
via stdlib sqlite3 — the same append-friendly store discipline as the event
log. Vectors are stored L2-normalized as the providers emit them (each
provider declares its ``normalization``; nothing here re-normalizes).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    cache_key     TEXT PRIMARY KEY,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    version       TEXT NOT NULL,
    dim           INTEGER NOT NULL,
    normalization TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    vector        BLOB NOT NULL
);
"""


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key(*, provider: str, model: str, version: str, dim: int,
              normalization: str, chash: str) -> str:
    basis = json.dumps(
        {"provider": provider, "model": model, "version": version,
         "dim": dim, "normalization": normalization, "content_hash": chash},
        sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class VectorCache:
    """Persistent, identity-checked vector cache. ``get`` returns a vector only
    when the stored metadata exactly matches the requested identity."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as c:
            c.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, *, provider: str, model: str, version: str, dim: int,
            normalization: str, text: str) -> list[float] | None:
        chash = content_hash(text)
        key = cache_key(provider=provider, model=model, version=version,
                        dim=dim, normalization=normalization, chash=chash)
        with self._connect() as c:
            row = c.execute("SELECT * FROM vectors WHERE cache_key = ?", (key,)).fetchone()
        if row is None:
            return None
        # identity re-verification (the key already encodes it; check anyway so
        # a corrupted/mismatched row can never be served)
        if (row["provider"] != provider or row["model"] != model
                or row["version"] != version or row["dim"] != dim
                or row["normalization"] != normalization
                or row["content_hash"] != chash):
            return None
        return list(struct.unpack(f"<{row['dim']}d", row["vector"]))

    def put(self, *, provider: str, model: str, version: str, dim: int,
            normalization: str, text: str, vector: list[float]) -> None:
        if len(vector) != dim:
            raise ValueError(f"vector dim {len(vector)} != declared dim {dim}")
        chash = content_hash(text)
        key = cache_key(provider=provider, model=model, version=version,
                        dim=dim, normalization=normalization, chash=chash)
        blob = struct.pack(f"<{dim}d", *vector)  # float64: EXACT roundtrip — a
        # 32-bit cache would make re-runs differ from first runs in the low
        # digits, silently breaking experiment determinism
        with self._connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO vectors "
                "(cache_key, provider, model, version, dim, normalization, "
                " content_hash, created_at, vector) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, provider, model, version, dim, normalization, chash,
                 datetime.now(timezone.utc).isoformat(), blob))

    def stats(self) -> dict:
        with self._connect() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM vectors").fetchone()["n"]
            by_provider = {r["provider"]: r["n"] for r in c.execute(
                "SELECT provider, COUNT(*) AS n FROM vectors GROUP BY provider")}
        return {"entries": n, "by_provider": by_provider}
