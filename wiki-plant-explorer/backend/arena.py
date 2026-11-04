"""Taste from Are.na: the public images that a few people with an eye have collected.

Each person in ``ARENA_TASTE`` (profile slugs, comma separated) becomes a cloud of points: their
newest public Image blocks, at most ``PER_PERSON``, each embedded once with DINOv2 (how it looks)
and CLIP (what it shows). The clouds stay apart, so three people with different eyes don't blur
into one average: a picture is near the taste when it is near the closest few images of any one
person, averaged over the people.

The images themselves are never stored or shown: only their vectors, in ``state/taste.npz``, with
the block ids in ``state/taste.json`` so a refresh only fetches what was added since. Reading is
polite, as Are.na asks: a page at a time with a pause between pages, a named User-Agent, and only
the newest few pages of each person (no archive harvesting).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from pathlib import Path
from typing import Optional

import httpx
import numpy as np
from PIL import Image

log = logging.getLogger("arena")

API = "https://api.are.na/v3"
USER_AGENT = "WikiExplorer/0.1 (personal, local; reads a few public profiles for taste)"
PER_PAGE = 100
PER_PERSON = 300  # the newest public images of each person
PAGE_PAUSE_S = 0.6
REFRESH_S = 24 * 3600
NEAREST = 5  # a picture's closeness to a person: the mean of its closest few images of theirs
FULL_WEIGHT = 40  # people with fewer images than this count less


class Taste:
    def __init__(self, state_dir: Path, token: Optional[str], people: list[str]):
        self.npz = state_dir / "taste.npz"
        self.meta_path = state_dir / "taste.json"
        self.people = people
        self.http = httpx.AsyncClient(
            timeout=30, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, **({"Authorization": f"Bearer {token}"} if token else {})})
        self.meta: dict = json.loads(self.meta_path.read_text()) if self.meta_path.exists() else {}
        self.clouds: dict[str, dict[str, np.ndarray]] = {}
        if self.npz.exists():
            data = np.load(self.npz)
            for slug in people:
                if f"{slug}/visual" in data:
                    self.clouds[slug] = {s: data[f"{slug}/{s}"] for s in ("visual", "semantic")}

    def __bool__(self) -> bool:
        return any(len(c["visual"]) for c in self.clouds.values())

    def describe(self) -> dict:
        return {slug: len(self.clouds[slug]["visual"]) if slug in self.clouds else 0 for slug in self.people}

    def score(self, visual: np.ndarray, semantic: np.ndarray) -> np.ndarray:
        """Closeness of each row (unit vectors, (n, 768) and (n, 512)) to the taste; higher is nearer."""
        total = np.zeros(len(visual), dtype=np.float32)
        weights = 0.0
        for cloud in self.clouds.values():
            n = len(cloud["visual"])
            if not n:
                continue
            sim = 0.5 * (visual @ cloud["visual"].T) + 0.5 * (semantic @ cloud["semantic"].T)
            k = min(NEAREST, n)
            near = np.partition(sim, n - k, axis=1)[:, n - k:].mean(axis=1)
            w = min(1.0, n / FULL_WEIGHT)
            total += w * near
            weights += w
        return total / weights if weights else total

    async def blocks(self, slug: str, known: set) -> list[dict]:
        """The person's newest public Image blocks not seen yet: id and a small picture url."""
        out: list[dict] = []
        page = 1
        while page <= PER_PERSON // PER_PAGE:
            resp = await self.http.get(f"{API}/users/{slug}/contents",
                                       params={"type": "Image", "per": PER_PAGE, "page": page, "sort": "created_at_desc"})
            if resp.status_code == 429:  # wait for the minute's budget to come back, then the same page
                reset = float(resp.headers.get("x-ratelimit-reset") or time.time() + 60)
                await asyncio.sleep(min(max(reset - time.time(), 1), 70))
                continue
            resp.raise_for_status()
            page += 1
            body = resp.json()
            met_known = False
            for b in body.get("data", []):
                if b["id"] in known:
                    met_known = True
                    continue
                src = ((b.get("image") or {}).get("small") or {}).get("src")
                if b.get("visibility") == "public" and src:
                    out.append({"id": b["id"], "src": src})
            if met_known or not (body.get("meta") or {}).get("has_more_pages"):
                break
            await asyncio.sleep(PAGE_PAUSE_S)
        return out

    async def fetch(self, url: str, sem: asyncio.Semaphore) -> Optional[Image.Image]:
        async with sem:
            try:
                resp = await self.http.get(url)
                resp.raise_for_status()
                return Image.open(io.BytesIO(resp.content)).convert("RGB")
            except Exception:  # noqa: BLE001
                return None

    def stale(self) -> bool:
        return any(time.time() - (self.meta.get(slug) or {}).get("fetched", 0) > REFRESH_S for slug in self.people)

    async def refresh(self, embedder, lock: asyncio.Lock) -> None:
        """Adds each person's new images to their cloud, newest first, keeping at most PER_PERSON."""
        sem = asyncio.Semaphore(6)
        for slug in self.people:
            entry = self.meta.get(slug) or {"ids": [], "fetched": 0}
            if time.time() - entry["fetched"] < REFRESH_S:
                continue
            try:
                new = await self.blocks(slug, set(entry["ids"]))
            except httpx.HTTPError as exc:
                log.warning("taste: %s: %s", slug, exc)
                continue
            images = await asyncio.gather(*(self.fetch(b["src"], sem) for b in new))
            got = [(b, im) for b, im in zip(new, images) if im is not None]
            vis: list[list[float]] = []
            sem_: list[list[float]] = []
            for start in range(0, len(got), 16):
                async with lock:  # a batch at a time, so the person's own requests come between
                    v, s = await asyncio.to_thread(embedder.embed_batch, [im for _, im in got[start:start + 16]])
                vis += v
                sem_ += s
            old = self.clouds.get(slug) or {"visual": np.zeros((0, 768), np.float32), "semantic": np.zeros((0, 512), np.float32)}
            ids = [b["id"] for b, _ in got] + entry["ids"]
            cloud = {"visual": np.concatenate([np.asarray(vis, np.float32).reshape(-1, 768), old["visual"]])[:PER_PERSON],
                     "semantic": np.concatenate([np.asarray(sem_, np.float32).reshape(-1, 512), old["semantic"]])[:PER_PERSON]}
            self.clouds[slug] = cloud
            self.meta[slug] = {"ids": ids[:PER_PERSON], "fetched": time.time()}
            self.save()
            log.info("taste: %s: %d new images, %d in the cloud", slug, len(got), len(cloud["visual"]))

    def save(self) -> None:
        self.npz.parent.mkdir(parents=True, exist_ok=True)
        arrays = {f"{slug}/{s}": c[s] for slug, c in self.clouds.items() for s in ("visual", "semantic")}
        tmp = self.npz.with_suffix(".tmp.npz")
        np.savez(tmp, **arrays)
        tmp.replace(self.npz)
        self.meta_path.write_text(json.dumps(self.meta))

    async def close(self) -> None:
        await self.http.aclose()
