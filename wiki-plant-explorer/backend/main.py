"""FastAPI service: browse all of Wikimedia Commons live, re-rank "similar" images visually.

Run from the project root:
    uvicorn backend.main:app --host 127.0.0.1 --port 8001

Images are never written to disk. Thumbnails are fetched into memory only when an image has to be
compared visually; what is kept is its two embedding vectors (~5 KB) in the local Qdrant cache, so
every image is looked at once and the app gets faster and better the more it is used.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from qdrant_client import QdrantClient, models

from .commons import Commons
from .embedder import SEMANTIC_DIM, VISUAL_DIM, Embedder

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "qdrant_db"
FEEDBACK_PATH = ROOT / "state" / "feedback.json"
COLLECTION = "commons_vectors"
SPACES = ("visual", "semantic")

# How many not-yet-seen images one /api/similar call may download and embed. The rest are
# embedded by later calls ("Altri simili"), so the first answer comes back in a few seconds.
NEW_PER_CALL = 64
FETCH_TIMEOUT_S = 20
# Relevance feedback (Rocchio): query = origin + MORE * mean(liked) - LESS * mean(disliked).
MORE = 0.7
LESS = 0.35
# Pull towards everything ever liked, so the ranking slowly learns the user's visual taste.
TASTE = 0.12
MAX_CANDIDATES = 400
# DINOv2 cosine above which two images are treated as the same picture (crop, re-upload, burst).
NEAR_DUP = 0.94
# How many top visual matches seed the next round's candidate search (second hop).
HOP_SEEDS = 4
# Search: Commons matches words in titles and descriptions, so "hole" also finds villages called
# Hole and portraits of people named Buchi. Every result is checked with CLIP against the query
# (image-text cosine): results far below the best of the page don't show the concept and are hidden.
SEARCH_PAGE = 48
CLIP_UNDERSTOOD = 0.22  # best image-text cosine below this: CLIP doesn't know the query, don't filter
CLIP_FLOOR = 0.19
CLIP_SPREAD = 0.13

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("api")

S: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    S["embedder"] = Embedder()
    S["commons"] = Commons()
    client = QdrantClient(path=str(DB_PATH))
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            COLLECTION,
            vectors_config={
                "visual": models.VectorParams(size=VISUAL_DIM, distance=models.Distance.COSINE),
                "semantic": models.VectorParams(size=SEMANTIC_DIM, distance=models.Distance.COSINE),
            },
        )
    S["qdrant"] = client
    S["embed_lock"] = asyncio.Lock()
    S["feedback"] = load_feedback()
    yield
    await S["commons"].close()
    client.close()


app = FastAPI(title="wiki-explorer", lifespan=lifespan)


# --------------------------------------------------------------------------- feedback store


def load_feedback() -> dict:
    if FEEDBACK_PATH.exists():
        return json.loads(FEEDBACK_PATH.read_text())
    return {"events": []}


def save_feedback() -> None:
    FEEDBACK_PATH.parent.mkdir(exist_ok=True)
    FEEDBACK_PATH.write_text(json.dumps(S["feedback"], ensure_ascii=False, indent=1))


def votes(origin: Optional[str] = None) -> dict[str, int]:
    """Latest vote per title, either globally (origin=None) or given while viewing ``origin``."""
    out: dict[str, int] = {}
    for ev in S["feedback"]["events"]:
        if origin is None or ev.get("origin") == origin:
            out[ev["title"]] = ev["value"]
    return {t: v for t, v in out.items() if v != 0}


def saved_items() -> list[dict]:
    latest: dict[str, dict] = {}
    for ev in S["feedback"]["events"]:
        if ev.get("origin") is None:
            latest[ev["title"]] = ev
    return [ev["item"] for ev in sorted(latest.values(), key=lambda e: -e["ts"]) if ev["value"] == 1 and ev.get("item")]


# --------------------------------------------------------------------------- vector cache


def point_id(title: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, title))


async def ensure_vectors(items: list[dict], new_budget: int = NEW_PER_CALL) -> tuple[dict, int]:
    """Return {title: {space: np.ndarray}} for ``items``, embedding at most ``new_budget`` unseen ones.

    The second value is how many items are still waiting to be embedded.
    """
    client: QdrantClient = S["qdrant"]
    vecs: dict[str, dict] = {}
    for start in range(0, len(items), 256):
        chunk = items[start : start + 256]
        for p in client.retrieve(COLLECTION, ids=[point_id(it["title"]) for it in chunk], with_vectors=True, with_payload=True):
            vecs[p.payload["title"]] = {s: np.asarray(p.vector[s], dtype=np.float32) for s in SPACES}

    missing = [it for it in items if it["title"] not in vecs]
    todo, pending = missing[:new_budget], len(missing) - min(len(missing), new_budget)
    if not todo:
        return vecs, pending

    commons: Commons = S["commons"]
    tasks = {asyncio.create_task(commons.fetch_image(it["thumb"])): it for it in todo}
    done, late = await asyncio.wait(tasks, timeout=FETCH_TIMEOUT_S)
    for t in late:
        t.cancel()
    pending += len(late)
    ok = [(tasks[t], t.result()) for t in done if not t.exception()]
    if not ok:
        return vecs, pending

    async with S["embed_lock"]:
        points = []
        for start in range(0, len(ok), 16):
            batch = ok[start : start + 16]
            visual, semantic = await asyncio.to_thread(S["embedder"].embed_batch, [im for _, im in batch])
            for (it, _), v, s in zip(batch, visual, semantic):
                vecs[it["title"]] = {"visual": np.asarray(v, dtype=np.float32), "semantic": np.asarray(s, dtype=np.float32)}
                points.append(models.PointStruct(id=point_id(it["title"]), vector={"visual": v, "semantic": s}, payload=it))
        client.upsert(COLLECTION, points=points)
    log.info("embedded %d new images (%d failed, %d pending)", len(ok), len(todo) - len(ok) - len(late), pending)
    return vecs, pending


def minmax(x: np.ndarray) -> np.ndarray:
    lo, hi = float(x.min()), float(x.max())
    return np.ones_like(x) if hi - lo < 1e-9 else (x - lo) / (hi - lo)


def unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) or 1.0)


# --------------------------------------------------------------------------- routes


@app.get("/api/health")
async def health():
    return {"status": "ok", "cached_vectors": S["qdrant"].count(COLLECTION).count, "saved": len(saved_items())}


def with_saved_flag(items: list[dict]) -> list[dict]:
    saved_titles = {it["title"] for it in saved_items()}
    return [{**it, "saved": it["title"] in saved_titles} for it in items]


@app.get("/api/feed")
async def feed(cont: Optional[str] = None):
    items, nxt = await S["commons"].feed(cont)
    return {"items": with_saved_flag(items), "next": nxt}


def text_vector(text: str) -> np.ndarray:
    cache = S.setdefault("text_vectors", {})
    if text not in cache:
        prompts = [f"a photo of a {text}.", f"a close-up photo of a {text}.", text]
        cache[text] = np.asarray(S["embedder"].embed_text(prompts), dtype=np.float32)
    return cache[text]


@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=1),
    offset: int = Query(0, ge=0),
    budget: int = Query(SEARCH_PAGE, ge=0, le=64),
):
    """One page of Commons results, checked by CLIP and sorted by how well they show the query.

    ``budget`` = how many not-yet-seen images may be embedded now. With 0 the answer is instant:
    already-known images are checked, the others come back unchecked (``pending``) at the end.
    """
    commons: Commons = S["commons"]
    english, qid = await commons.translate(q)
    items, nxt = await commons.search(english, offset=offset, limit=SEARCH_PAGE)
    vecs, pending = await ensure_vectors(items, new_budget=budget)
    checked = [it for it in items if it["title"] in vecs]
    unchecked = [it for it in items if it["title"] not in vecs]

    hidden = 0
    if checked:
        sims = np.stack([vecs[it["title"]]["semantic"] for it in checked]) @ text_vector(english)
        best = float(sims.max())
        cutoff = max(CLIP_FLOOR, best - CLIP_SPREAD) if best >= CLIP_UNDERSTOOD else -1.0
        kept: list[int] = []
        for i in np.argsort(-sims):
            if sims[i] < cutoff:
                break
            v = vecs[checked[i]["title"]]["visual"]
            if kept and max(float(vecs[checked[k]["title"]]["visual"] @ v) for k in kept) > NEAR_DUP:
                continue
            kept.append(int(i))
        hidden = len(checked) - len(kept)
        for i in kept:
            checked[i] = {**checked[i], "relevance": round(float(sims[i]), 3)}
        checked = [checked[i] for i in kept]
    log.info("search %r -> %r (%s): %d shown, %d hidden, %d unchecked", q, english, qid, len(checked), hidden, len(unchecked))
    return {
        "query": q, "english": english, "qid": qid, "items": with_saved_flag(checked + unchecked),
        "next": nxt, "pending": pending, "hidden": hidden,
    }


@app.get("/api/saved")
async def saved():
    return {"items": with_saved_flag(saved_items())}


@app.get("/api/file")
async def file(title: str):
    items = await S["commons"].files([title])
    if not items:
        raise HTTPException(404, f"{title} not found")
    return with_saved_flag(items)[0]


async def candidates_for(title: str, query: Optional[str], extra_origins: list[str]) -> list[dict]:
    """Images likely to look alike, gathered from Commons' human-curated structure.

    Sources, round-robin merged so a limited embedding budget samples all of them:
      - the origin's most specific content categories (and their parents when that is too few),
      - files sharing its structured-data "depicts" (P180),
      - the search the user came from,
      - the categories of ``extra_origins``: results marked "più così" plus the best visual matches
        of the previous round. This second hop walks to the neighbours of the neighbours, which is
        what makes each refinement round find images the first round could not reach.
    """
    commons: Commons = S["commons"]

    async def from_categories(t: str, n_cats: int, per_cat: int) -> list[list[dict]]:
        cats = await commons.categories(t)
        res = await asyncio.gather(*(commons.category_files(c, limit=per_cat) for c, _ in cats[:n_cats]), return_exceptions=True)
        lists = [r[0] for r in res if not isinstance(r, Exception)]
        if sum(len(l) for l in lists) < 60 and cats:
            # Too few files in the specific categories: go one level up the category tree.
            parents = await asyncio.gather(*(commons.parent_categories(c) for c, _ in cats[:2]), return_exceptions=True)
            names = list(dict.fromkeys(p for ps in parents if not isinstance(ps, Exception) for p in ps))
            res = await asyncio.gather(*(commons.category_files(c, limit=30) for c in names[:4]), return_exceptions=True)
            lists += [r[0] for r in res if not isinstance(r, Exception)]
        return lists

    async def from_depicts(t: str) -> list[list[dict]]:
        qids = await commons.depicts(t)
        res = await asyncio.gather(*(commons.search(f"haswbstatement:P180={q}", limit=30) for q in qids[:2]), return_exceptions=True)
        return [r[0] for r in res if not isinstance(r, Exception)]

    jobs = [from_categories(title, 4, 40), from_depicts(title)]
    if query:
        english, _ = await commons.translate(query)
        jobs.append(commons.search(english, limit=40))
    jobs += [from_categories(t, 2, 25) for t in extra_origins[:5]]
    results = await asyncio.gather(*jobs, return_exceptions=True)

    lists: list[list[dict]] = []
    for r in results:
        if isinstance(r, Exception):
            log.warning("candidate source failed: %s", r)
        elif isinstance(r, tuple):  # commons.search -> (items, next)
            lists.append(r[0])
        else:
            lists.extend(r)

    seen, out = {title}, []
    for i in range(max((len(l) for l in lists), default=0)):
        for l in lists:
            if i < len(l) and l[i]["title"] not in seen:
                seen.add(l[i]["title"])
                out.append(l[i])
    return out[:MAX_CANDIDATES]


def origin_lock(title: str) -> asyncio.Lock:
    """Serialise concurrent /api/similar calls for one image so they never embed the same files twice."""
    locks = S.setdefault("locks", {})
    if title not in locks:
        if len(locks) > 500:
            locks.clear()
        locks[title] = asyncio.Lock()
    return locks[title]


@app.get("/api/similar")
async def similar(
    title: str,
    weight: float = Query(0.75, ge=0.0, le=1.0),
    q: Optional[str] = None,
    limit: int = Query(60, ge=1, le=200),
    budget: int = Query(NEW_PER_CALL, ge=0, le=128),
):
    """``weight`` = 1.0 ranks purely by DINOv2 (look: shape, texture, colour), 0.0 purely by CLIP (subject)."""
    async with origin_lock(title):
        return await _similar(title, weight, q, limit, budget)


async def _similar(title: str, weight: float, q: Optional[str], limit: int, budget: int) -> dict:
    commons: Commons = S["commons"]
    found = await commons.files([title])
    if not found:
        raise HTTPException(404, "Immagine non trovata su Wikimedia Commons")
    origin = found[0]
    t0 = time.time()

    # The origin is always embedded first, whatever the budget.
    vecs, _ = await ensure_vectors([origin], new_budget=1)
    if title not in vecs:
        raise HTTPException(502, "Wikimedia non ha risposto per questa immagine, riprova tra poco")

    local = votes(origin=title)
    more = [t for t, v in local.items() if v == 1]
    less = [t for t, v in local.items() if v == -1]
    global_votes = votes()
    hops = [t for t in S.setdefault("best", {}).get(title, []) if t not in more]

    cands = [c for c in await candidates_for(title, q, more + hops) if c["title"] not in less]
    more_vecs, pending = await ensure_vectors(cands, new_budget=budget)
    vecs.update(more_vecs)

    # Vectors of feedback items (they were embedded when they were shown or saved).
    fb_titles = set(more) | set(less) | {t for t, v in global_votes.items() if v == 1}
    client: QdrantClient = S["qdrant"]
    for p in client.retrieve(COLLECTION, ids=[point_id(t) for t in fb_titles], with_vectors=True, with_payload=True):
        vecs.setdefault(p.payload["title"], {s: np.asarray(p.vector[s], dtype=np.float32) for s in SPACES})
    liked_all = [t for t, v in global_votes.items() if v == 1 and t in vecs]

    ranked = [c for c in cands if c["title"] in vecs]
    if not ranked:
        return {"origin": with_saved_flag([origin])[0], "items": [], "pending": pending, "candidates": len(cands)}

    final = np.zeros(len(ranked), dtype=np.float32)
    raw = {}
    for space, w in (("visual", weight), ("semantic", 1.0 - weight)):
        query = vecs[title][space].copy()
        if more_v := [vecs[t][space] for t in more if t in vecs]:
            query += MORE * np.mean(more_v, axis=0)
        if less_v := [vecs[t][space] for t in less if t in vecs]:
            query -= LESS * np.mean(less_v, axis=0)
        matrix = np.stack([vecs[c["title"]][space] for c in ranked])
        sims = matrix @ unit(query)
        raw[space] = sims
        score = minmax(sims)
        if len(liked_all) >= 2:
            taste = unit(np.mean([vecs[t][space] for t in liked_all], axis=0))
            score = score + TASTE * minmax(matrix @ taste)
        final += w * score

    # Greedy pick in score order, skipping near-duplicates (crops, re-uploads, burst shots) of the
    # origin or of an image already picked, so the top of the list is similar but not repetitive.
    visual = np.stack([vecs[c["title"]]["visual"] for c in ranked])
    origin_v = vecs[title]["visual"]
    picked: list[int] = []
    for i in np.argsort(-final):
        if float(visual[i] @ origin_v) > NEAR_DUP:
            continue
        if picked and float(np.max(visual[picked] @ visual[i])) > NEAR_DUP:
            continue
        picked.append(int(i))
        if len(picked) >= limit:
            break

    # Remember the best visual matches: next round explores their categories (second hop).
    S["best"][title] = [ranked[i]["title"] for i in picked[:HOP_SEEDS]]

    items = with_saved_flag([ranked[i] for i in picked])
    for item, i in zip(items, picked):
        match = weight * float(raw["visual"][i]) + (1.0 - weight) * float(raw["semantic"][i])
        item.update(score=round(float(final[i]), 4), match=round(match, 3), vote=local.get(item["title"], 0))
    log.info("similar %s: %d candidates, %d ranked, %d shown, %d pending, %.1fs",
             title, len(cands), len(ranked), len(items), pending, time.time() - t0)
    return {"origin": with_saved_flag([origin])[0], "items": items, "pending": pending, "candidates": len(cands)}


class Vote(BaseModel):
    title: str
    value: int  # 1 = più così / salva, -1 = meno così, 0 = annulla
    origin: Optional[str] = None  # set when voting on a "simili" result; None = save to "Salvati"
    item: Optional[dict] = None


@app.post("/api/vote")
async def vote(v: Vote):
    if v.value not in (-1, 0, 1):
        raise HTTPException(422, "value must be -1, 0 or 1")
    item = v.item
    if v.value == 1 and not item:
        found = await S["commons"].files([v.title])
        item = found[0] if found else None
    S["feedback"]["events"].append({"title": v.title, "value": v.value, "origin": v.origin, "item": item, "ts": time.time()})
    save_feedback()
    if v.value == 1 and item:  # make sure liked images have vectors so they can shape the ranking
        await ensure_vectors([item], new_budget=1)
    return {"ok": True}
