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
import os
import random
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
from .sources import Cleveland, INaturalist
from .embedder import SEMANTIC_DIM, VISUAL_DIM, Embedder

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "qdrant_db"
FEEDBACK_PATH = ROOT / "state" / "feedback.json"
CRAWL_PATH = ROOT / "state" / "crawler.json"
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
SEARCH_PAGE = 36  # Commons results per page...
SOURCE_PAGE = 12  # ...plus this many from each other collection
CLIP_UNDERSTOOD = 0.22  # best image-text cosine below this: CLIP doesn't know the query, don't filter
CLIP_FLOOR = 0.19
CLIP_SPREAD = 0.13
# Index neighbours: images already in the local vector cache that are closest to the origin. They
# cost nothing to rank, so the more the app is used (and the crawler runs) the better "similar" gets.
INDEX_NEIGHBOURS = 160
# Background crawler: while nobody is waiting, embed CRAWL_BATCH more Commons images every
# CRAWL_PAUSE_S seconds (vectors only), mostly from the categories of what the user looks at.
CRAWL = os.environ.get("CRAWL", "1") != "0"
CRAWL_BATCH = 12
CRAWL_PAUSE_S = 6
USER_QUIET_S = 10
BROAD_POOLS = [
    "Category:Quality images",
    "Category:Featured pictures on Wikimedia Commons",
    "Category:Valued images sorted by promotion date",
]
# "Per te" home feed: neighbours of what the user saved, liked and opened, most recent first.
FOR_YOU_SEEDS = 30
FOR_YOU_PAGE = 40

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("api")

S: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    S["embedder"] = Embedder()
    S["commons"] = Commons()
    S["sources"] = {src.key: src for src in (Cleveland(S["commons"]._get), INaturalist(S["commons"]._get))}
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
    S["user_active"] = 0.0
    S["crawl"] = load_crawl_state()
    crawler_task = asyncio.create_task(crawler()) if CRAWL else None
    yield
    if crawler_task:
        crawler_task.cancel()
    await S["commons"].close()
    client.close()


app = FastAPI(title="wiki-explorer", lifespan=lifespan)


# --------------------------------------------------------------------------- feedback store


def load_feedback() -> dict:
    data = json.loads(FEEDBACK_PATH.read_text()) if FEEDBACK_PATH.exists() else {}
    data.setdefault("events", [])  # votes and saves
    data.setdefault("views", [])  # images opened: a weak, implicit "I'm interested"
    return data


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


async def ensure_vectors(items: list[dict], new_budget: int = NEW_PER_CALL, background: bool = False) -> tuple[dict, int]:
    """Return {title: {space: np.ndarray}} for ``items``, embedding at most ``new_budget`` unseen ones.

    The second value is how many items are still waiting to be embedded. ``background`` calls
    (the crawler) don't count as user activity.
    """
    if not background:
        S["user_active"] = time.time()
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
                points.append(models.PointStruct(id=point_id(it["title"]), vector={"visual": v, "semantic": s}, payload=clean(it)))
        client.upsert(COLLECTION, points=points)
    log.info("embedded %d new images (%d failed, %d pending)", len(ok), len(todo) - len(ok) - len(late), pending)
    return vecs, pending


def minmax(x: np.ndarray) -> np.ndarray:
    lo, hi = float(x.min()), float(x.max())
    return np.ones_like(x) if hi - lo < 1e-9 else (x - lo) / (hi - lo)


def unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) or 1.0)


ITEM_KEYS = ("title", "name", "thumb", "width", "height", "full", "full_width", "page_url", "author", "license", "source")


def clean(item: dict) -> dict:
    """Only the Commons fields of an item (cards sent back by the UI also carry score, vote...)."""
    return {k: item.get(k) for k in ITEM_KEYS}


async def resolve(title: str) -> Optional[dict]:
    """Look an item up again by its title, whichever collection it comes from."""
    if title.startswith("File:"):
        found = await S["commons"].files([title])
        return found[0] if found else None
    key, _, ident = title.partition(":")
    src = S["sources"].get(key)
    return await src.get(ident) if src else None


def index_neighbours(vec: dict, limit: int = INDEX_NEIGHBOURS, weight: float = 0.75) -> list[dict]:
    """Nearest images in the local vector cache, by look (DINOv2) and by subject (CLIP)."""
    client: QdrantClient = S["qdrant"]
    out: dict[str, dict] = {}
    n_visual = int(limit * (0.3 + 0.5 * weight))
    for space, n in (("visual", n_visual), ("semantic", limit - n_visual)):
        res = client.query_points(COLLECTION, query=vec[space].tolist(), using=space, limit=n, with_payload=True)
        for p in res.points:
            out.setdefault(p.payload["title"], clean(p.payload))
    return list(out.values())


# --------------------------------------------------------------------------- background crawler


def load_crawl_state() -> dict:
    data = json.loads(CRAWL_PATH.read_text()) if CRAWL_PATH.exists() else {}
    data.setdefault("cont", {})  # category -> Commons continue token
    data.setdefault("done", [])  # categories crawled to the end
    data.setdefault("interests", [])  # categories of what the user looked at, most recent first
    data.setdefault("queries", [])  # searches and species/series of opened items: for other collections
    return data


def save_crawl_state() -> None:
    CRAWL_PATH.parent.mkdir(exist_ok=True)
    CRAWL_PATH.write_text(json.dumps(S["crawl"], ensure_ascii=False))


def note_query(query: str) -> None:
    """Queue a search term for crawling the other collections."""
    st = S["crawl"]
    st["queries"] = list(dict.fromkeys([query] + st["queries"]))[:40]
    save_crawl_state()


async def note_interest(title: str, item: Optional[dict] = None) -> None:
    """Queue the most specific categories of an image the user opened, saved or liked for crawling."""
    if not title.startswith("File:"):
        src = S["sources"].get(title.partition(":")[0])
        if src and item:
            note_query(src.related_query(item))
        return
    try:
        cats = await S["commons"].categories(title)
    except Exception as exc:  # noqa: BLE001 - an interest that can't be read is just skipped
        log.warning("interest %s: %s", title, exc)
        return
    st = S["crawl"]
    st["interests"] = list(dict.fromkeys([c for c, _ in cats[:3]] + st["interests"]))[:80]
    save_crawl_state()


def pick_recent(xs: list):
    """Mostly the first (most recent) elements, sometimes older ones."""
    return xs[min(int(random.expovariate(0.25)), len(xs) - 1)] if xs else None


def next_crawl_job(turn: int) -> Optional[tuple[str, str]]:
    """("commons", category) or (source key, query). Turns rotate: Commons interests, other
    collections on the user's searches, Commons interests, broad pools of quality images."""
    st = S["crawl"]
    kind = turn % 4
    if kind == 1:
        queries = [(k, q) for q in st["queries"] for k in S["sources"] if f"{k}:{q}" not in st["done"]]
        if queries:
            return pick_recent(queries)
    if kind in (0, 1, 2):
        interests = [c for c in st["interests"] if c not in st["done"]]
        if interests:
            return "commons", pick_recent(interests)
    pools = [c for c in BROAD_POOLS if c not in st["done"]]
    return ("commons", random.choice(pools)) if pools else None


async def crawler() -> None:
    """Grow the vector index while the user isn't waiting for anything (vectors only, no images kept).

    This is what makes "similar" and "Per te" work like Pinterest: they can pick from everything
    already in the index instantly, instead of only from what can be fetched during one request.
    """
    turn = 0
    while True:
        await asyncio.sleep(CRAWL_PAUSE_S)
        if time.time() - S["user_active"] < USER_QUIET_S:
            continue
        job = next_crawl_job(turn)
        turn += 1
        if not job:
            continue
        where, what = job
        st = S["crawl"]
        key = what if where == "commons" else f"{where}:{what}"
        try:
            if where == "commons":
                items, cont = await S["commons"].category_files(what, limit=CRAWL_BATCH, cont=st["cont"].get(key))
            else:
                items, cont = await S["sources"][where].search(what, offset=st["cont"].get(key) or 0, limit=CRAWL_BATCH)
            if cont:
                st["cont"][key] = cont
            else:
                st["cont"].pop(key, None)
                st["done"].append(key)
            await ensure_vectors(items, new_budget=CRAWL_BATCH, background=True)
            save_crawl_state()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep crawling whatever one category does
            log.warning("crawler %s: %s", key, exc)


# --------------------------------------------------------------------------- routes


@app.get("/api/health")
async def health():
    return {"status": "ok", "indexed": S["qdrant"].count(COLLECTION).count, "saved": len(saved_items()), "crawling": CRAWL}


def with_saved_flag(items: list[dict]) -> list[dict]:
    saved_titles = {it["title"] for it in saved_items()}
    return [{**it, "saved": it["title"] in saved_titles} for it in items]


@app.get("/api/feed")
async def feed(cont: Optional[str] = None):
    items, nxt = await S["commons"].feed(cont)
    return {"items": with_saved_flag(items), "next": nxt}


async def federated_search(query: str, page: int) -> tuple[list[dict], Optional[int]]:
    """One page from Commons and from each other collection, interleaved (3 Commons : 1 other)."""
    jobs = [S["commons"].search(query, offset=page * SEARCH_PAGE, limit=SEARCH_PAGE)]
    jobs += [src.search(query, offset=page * SOURCE_PAGE, limit=SOURCE_PAGE) for src in S["sources"].values()]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    lists, more = [], False
    for r in results:
        if isinstance(r, Exception):
            log.warning("search source failed: %s", r)
            continue
        lists.append(r[0])
        more = more or r[1] is not None
    commons_items, others = (lists[0], lists[1:]) if lists else ([], [])
    out: list[dict] = []
    for i in range(max([len(commons_items) // 3 + 1] + [len(l) for l in others])):
        out += commons_items[i * 3 : i * 3 + 3]
        out += [l[i] for l in others if i < len(l)]
    return out, (page + 1 if more else None)


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
    budget: int = Query(SEARCH_PAGE, ge=0, le=96),
):
    """Page ``offset`` of the results from all collections, checked by CLIP and sorted by how well
    they show the query.

    ``budget`` = how many not-yet-seen images may be embedded now. With 0 the answer is instant:
    already-known images are checked, the others come back unchecked (``pending``) at the end.
    """
    commons: Commons = S["commons"]
    english, qid = await commons.translate(q)
    if offset == 0:
        note_query(english)
    items, nxt = await federated_search(english, offset)
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
    item = await resolve(title)
    if not item:
        raise HTTPException(404, f"{title} not found")
    return with_saved_flag([item])[0]


async def candidates_for(origin: dict, query: Optional[str], extra_origins: list[str]) -> list[dict]:
    """Images likely to look alike, gathered from Commons' human-curated structure.

    Sources, round-robin merged so a limited embedding budget samples all of them:
      - the origin's most specific content categories (and their parents when that is too few),
      - files sharing its structured-data "depicts" (P180),
      - the search the user came from,
      - the categories of ``extra_origins``: results marked "più così" plus the best visual matches
        of the previous round. This second hop walks to the neighbours of the neighbours, which is
        what makes each refinement round find images the first round could not reach,
      - the other collections, searched for the search query and for what the origin is (its
        most specific category; the species or series for an item from another collection).
    The local index adds its nearest neighbours separately (see ``_similar``).
    """
    commons: Commons = S["commons"]
    title = origin["title"]

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

    async def from_sources(text: str, per_source: int) -> list[list[dict]]:
        res = await asyncio.gather(*(src.search(text, limit=per_source) for src in S["sources"].values()), return_exceptions=True)
        return [r[0] for r in res if not isinstance(r, Exception)]

    jobs = []
    if title.startswith("File:"):
        jobs += [from_categories(title, 4, 40), from_depicts(title)]
        cats = await commons.categories(title)
        if cats:  # "Category:Colocasia esculenta leaves" -> "Colocasia esculenta leaves"
            jobs.append(from_sources(cats[0][0].removeprefix("Category:"), 12))
    else:
        src = S["sources"][title.partition(":")[0]]
        related = src.related_query(origin)
        jobs += [src.search(related, limit=40), commons.search(related, limit=30)]
    if query:
        english, _ = await commons.translate(query)
        jobs += [commons.search(english, limit=40), from_sources(english, 12)]
    jobs += [from_categories(t, 2, 25) for t in extra_origins[:5] if t.startswith("File:")]
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
    limit: int = Query(60, ge=1, le=600),
    budget: int = Query(NEW_PER_CALL, ge=0, le=128),
):
    """``weight`` = 1.0 ranks purely by DINOv2 (look: shape, texture, colour), 0.0 purely by CLIP (subject)."""
    async with origin_lock(title):
        return await _similar(title, weight, q, limit, budget)


async def _similar(title: str, weight: float, q: Optional[str], limit: int, budget: int) -> dict:
    origin = await resolve(title)
    if not origin:
        raise HTTPException(404, "Immagine non trovata")
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

    cands = await candidates_for(origin, q, more + hops)
    seen = {c["title"] for c in cands}
    cands += [c for c in index_neighbours(vecs[title], weight=weight) if c["title"] not in seen and c["title"] != title]
    cands = [c for c in cands if c["title"] not in less]
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


def interest_seeds() -> list[str]:
    """Titles the user showed interest in, most relevant first: saves and likes, then opened images.

    A save or "più così" counts as if it happened three days later than it did, so it outranks a
    merely opened image for a while.
    """
    fb = S["feedback"]
    score: dict[str, float] = {}
    latest_vote: dict[str, int] = {}
    for ev in fb["events"]:
        latest_vote[ev["title"]] = ev["value"]
        if ev["value"] == 1:
            score[ev["title"]] = max(score.get(ev["title"], 0.0), ev["ts"] + 3 * 86400)
    for ev in fb["views"]:
        score[ev["title"]] = max(score.get(ev["title"], 0.0), ev["ts"])
    titles = sorted(score, key=lambda t: -score[t])
    return [t for t in titles if latest_vote.get(t) != -1][:FOR_YOU_SEEDS]


def build_for_you(seeds: list[str]) -> list[dict]:
    client: QdrantClient = S["qdrant"]
    pts = client.retrieve(COLLECTION, ids=[point_id(t) for t in seeds], with_vectors=True, with_payload=True)
    vec = {p.payload["title"]: {s: np.asarray(p.vector[s], dtype=np.float32) for s in SPACES} for p in pts}
    seeds = [t for t in seeds if t in vec]
    if not seeds:
        return []
    per_seed = max(24, 800 // len(seeds))
    lists = [index_neighbours(vec[t], limit=per_seed, weight=0.6) for t in seeds]
    # Weighted round-robin: the newest interests give 3 images per round, older ones 2, then 1.
    shares = [3 if i < 4 else 2 if i < 12 else 1 for i in range(len(seeds))]
    blocked = set(seeds) | {t for t, v in votes().items() if v == -1}
    order: list[dict] = []
    pos = [0] * len(lists)
    while any(pos[i] < len(l) for i, l in enumerate(lists)):
        for i, l in enumerate(lists):
            for _ in range(shares[i]):
                while pos[i] < len(l) and l[pos[i]]["title"] in blocked:
                    pos[i] += 1
                if pos[i] < len(l):
                    blocked.add(l[pos[i]]["title"])
                    order.append(l[pos[i]])
                    pos[i] += 1
    # Drop near-duplicates (burst shots, crops) so the feed doesn't repeat itself.
    out_vecs = {p.payload["title"]: np.asarray(p.vector["visual"], dtype=np.float32) for p in client.retrieve(
        COLLECTION, ids=[point_id(it["title"]) for it in order], with_vectors=["visual"], with_payload=["title"])}
    kept: list[dict] = []
    kept_v: list[np.ndarray] = []
    for it in order:
        v = out_vecs.get(it["title"])
        if v is None:
            continue
        if kept_v and float(np.max(np.stack(kept_v[-200:]) @ v)) > NEAR_DUP:
            continue
        kept.append(it)
        kept_v.append(v)
    return kept


@app.get("/api/foryou")
async def for_you(page: int = Query(0, ge=0)):
    """Pinterest-style home: neighbours (in the local index) of everything the user is interested in."""
    seeds = interest_seeds()
    if not seeds:
        return {"items": [], "next": None, "personal": False}
    indexed = S["qdrant"].count(COLLECTION).count
    key = (tuple(seeds), indexed // 500)  # rebuilt when interests change or the index grows a lot
    cached = S.get("for_you")
    if not cached or cached[0] != key or page == 0:
        S["for_you"] = (key, await asyncio.to_thread(build_for_you, seeds))
    feed = S["for_you"][1]
    chunk = feed[page * FOR_YOU_PAGE : (page + 1) * FOR_YOU_PAGE]
    nxt = page + 1 if (page + 1) * FOR_YOU_PAGE < len(feed) else None
    return {"items": with_saved_flag(chunk), "next": nxt, "personal": True, "seeds": len(seeds)}


class View(BaseModel):
    title: str
    item: Optional[dict] = None


@app.post("/api/view")
async def view(v: View):
    """The user opened an image: a weak signal for "Per te" and a hint for the crawler."""
    views = S["feedback"]["views"]
    views.append({"title": v.title, "item": clean(v.item) if v.item else None, "ts": time.time()})
    del views[:-500]
    save_feedback()
    asyncio.create_task(note_interest(v.title, v.item))
    return {"ok": True}


class Vote(BaseModel):
    title: str
    value: int  # 1 = più così / salva, -1 = meno così, 0 = annulla
    origin: Optional[str] = None  # set when voting on a "simili" result; None = save to "Salvati"
    item: Optional[dict] = None


@app.post("/api/vote")
async def vote(v: Vote):
    if v.value not in (-1, 0, 1):
        raise HTTPException(422, "value must be -1, 0 or 1")
    item = clean(v.item) if v.item else None
    if v.value == 1 and not item:
        item = await resolve(v.title)
    S["feedback"]["events"].append({"title": v.title, "value": v.value, "origin": v.origin, "item": item, "ts": time.time()})
    save_feedback()
    if v.value == 1 and item:  # make sure liked images have vectors so they can shape the ranking
        await ensure_vectors([item], new_budget=1)
        asyncio.create_task(note_interest(v.title, item))
    return {"ok": True}
