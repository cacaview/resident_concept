# py-claw — pinned submodule

The Resident Agency drives **py-claw** as its execution runtime (see ADR-0024).
In the development repository, `py-claw/` is a **git submodule** of the public
upstream [`github.com/cacaview/py-claw`](https://github.com/cacaview/py-claw):

```
[submodule "py-claw"]
    path = py-claw
    url  = https://github.com/cacaview/py-claw.git
```

## Why a submodule (not a vendored subtree)

ADR-0024 originally shipped py-claw as a **vendored subtree** (plain files copied
into the repo). That was revised on **2026-09-20 to a submodule** so an upstream
update is a plain re-pin (`cd py-claw && git pull && git add py-claw`) instead of
re-copying ~670 files by hand.

The **public mirror** (built by `backend/scripts/sync_public.sh`) does **not**
carry the gitlink. It ships the *pinned* py-claw source as **plain files**, so the
mirror stays self-contained: a Windows deploy or a fresh clone of the mirror needs
no network access to the submodule host. `sync_public.sh` extracts each file from
the submodule's object store at the pinned SHA below — the submodule's committed
state, never its working tree.

## Pinned upstream commit

- Repo: `https://github.com/cacaview/py-claw.git`
- Pinned SHA: `265eedaacfac1733d5cb17f2b29b83921a22f6da`
- Subject: `feat: can_use_tool 宿主授权通道 (ADR-0024)`
- The pin is authoritative in git as the **gitlink** (`git ls-tree HEAD py-claw`);
  this file is the human-readable record of *which upstream commit* that SHA is.

## Re-syncing after an upstream update

```
cd py-claw && git fetch origin && git checkout <new-sha>   # or git pull --ff-only
cd .. && git add py-claw && git commit -m "py-claw: re-pin to <new-sha> (<why>)"
make sync-public-dry    # re-extracts the new pin; verify before publishing
```

A fresh checkout of this repo needs `git submodule update --init py-claw` before
`py-claw/` is populated (and before `make sync-public`).
