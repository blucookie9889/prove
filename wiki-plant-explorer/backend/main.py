"""FastAPI service: browse all of Wikimedia Commons live, re-rank "similar" images visually.

Run from the project root:
    uvicorn backend.main:app --host 127.0.0.1 --port 8001

Images are never written to disk. Thumbnails are fetched into memory only when an image has to be
compared visually; what is kept is its two embedding vectors (~5 KB) in the local Qdrant cache, so
every image is looked at once and the app gets faster and better the more it is used.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from qdrant_client import QdrantClient, models

from .commons import Commons
from .sources import Cleveland, INaturalist, Pexels, RateLimited, Unsplash
from .translator import Translator
from .embedder import LAYOUT_DIM, SEMANTIC_DIM, TEXTURE_DIM, VISUAL_DIM, Embedder

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "qdrant_db"
FEEDBACK_PATH = ROOT / "state" / "feedback.json"
CRAWL_PATH = ROOT / "state" / "crawler.json"
ENV_PATH = ROOT / ".env"  # API keys (git-ignored); see .env.example
COLLECTION = "commons_vectors"
SPACES = ("visual", "semantic")
# DINOv2's patch-level descriptors (see embedder.py), same point ids: texture, layout, parts.
DENSE = "dino_dense"

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
SEARCH_PAGE = 24  # Commons results per page from "Quality images" and as many from everything...
SOURCE_PAGE = 12  # ...plus this many from each other collection
CLIP_UNDERSTOOD = 0.22  # best image-text cosine below this: CLIP doesn't know the query, don't filter
CLIP_FLOOR = 0.19
CLIP_SPREAD = 0.13
# Beauty: Commons is an archive (diagrams, maps, scans, flat snapshots), Pinterest is curated. CLIP
# zero-shot: p = softmax(100 * cos) of "beautiful photograph" prompts vs. AESTH_NEG, from the
# semantic vector alone (no extra download). Calibrated on contact sheets of ~7000 indexed images:
# p < JUNK is documents, PLAIN..JUNK flat snapshots and product shots, above ~0.02 good photos.
AESTH_POS = ["a beautiful photograph", "a stunning high quality photo", "an aesthetic professional photograph",
             "award-winning photography", "a beautiful artwork"]
AESTH_NEG = ["a diagram", "a chart or graph", "a scanned page of a document with text", "a screenshot", "a map",
             "a technical drawing", "a blurry low quality photo", "a boring amateur snapshot",
             "a dull grainy old photo", "a page of a book"]
JUNK = 0.001
PLAIN = 0.004
WANTS_DOCUMENTS = 0.71  # query text this close to an AESTH_NEG prompt ("old map", "manuscript"): no filter
AESTH_SEARCH = 0.04  # added to the image-text cosine (spread of a page is ~0.1)
AESTH_SIMILAR = 0.15  # added to the 0..1 similarity score
MIN_BEAUTIFUL = 12  # plain images are hidden only when at least this many better ones remain
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
# Patch-level DINOv2 descriptors for images indexed before they existed: re-fetched and computed
# while the user is idle, DENSE_BATCH every DENSE_PAUSE_S seconds.
DENSE_BATCH = 48
DENSE_PAUSE_S = 1
# "Similar": share of the DINOv2 score given to shared parts (chamfer similarity of the k-means
# regions: both images contain the same kinds of things, wherever they are).
PARTS_WEIGHT = 0.3
# Lenses: other ways to be alike, each searched in the whole index (see /api/lens).
LENS_POOL = 400
# Rime: among the RHYME_POOL nearest images by DINOv2, z(form) - RHYME_SEMANTIC * z(meaning). The pool
# must be wide: the closest few hundred are nearly always the same subject (all shells on black).
RHYME_POOL = 1500
RHYME_SEMANTIC = 1.5
RHYME_FLOOR = 0.25  # ...among the pool's images at least this far up by DINOv2 similarity (quantile)
# "Dettaglio": the patches picked around a click (same DINOv2 region, within this share of the grid).
DETAIL_RADIUS = 0.14
MAPS_CACHED = 8
# "Deriva": a walk through the index, one link at a time. Each link is one way to be alike; the
# next image is drawn among the DRIFT_TOP best (softmax at DRIFT_TEMPERATURE), so no two walks
# are the same, with momentum along the direction of the last step so it keeps going somewhere.
DRIFT_LINKS = {"forma": 1.0, "materia": 1.0, "sguardo": 0.8, "senso": 0.7, "rima": 1.2}
DRIFT_POOL = 80
DRIFT_TOP = 6
DRIFT_TEMPERATURE = 0.06
DRIFT_MOMENTUM = 0.12
DRIFT_RECENT = 12
# Novelty: minus this times the closest CLIP similarity to the last DRIFT_NOVELTY_STEPS images, so
# the walk leaves a subject instead of circling inside one big series (thousands of shells on black).
DRIFT_NOVELTY = 1.0
DRIFT_NOVELTY_STEPS = 5
# A rhyme in a walk looks for the current image's look minus what the last steps have in common
# (DINOv2 vector - DRIFT_RESIDUAL * their mean): the theme is taken out, the specific form stays.
DRIFT_RESIDUAL = 0.8

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("api")

S: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    S["embedder"] = Embedder()
    S["translator"] = Translator()
    aesthetic(np.zeros((1, SEMANTIC_DIM), dtype=np.float32))  # encode the beauty prompts once
    S["commons"] = Commons()
    S["sources"] = make_sources(S["commons"])
    log.info("sources: Wikimedia Commons, %s", ", ".join(src.label for src in S["sources"].values()))
    client = QdrantClient(path=str(DB_PATH))
    if not client.collection_exists(COLLECTION):
        client.create_collection(
            COLLECTION,
            vectors_config={
                "visual": models.VectorParams(size=VISUAL_DIM, distance=models.Distance.COSINE),
                "semantic": models.VectorParams(size=SEMANTIC_DIM, distance=models.Distance.COSINE),
            },
        )
    if not client.collection_exists(DENSE):
        client.create_collection(
            DENSE,
            vectors_config={
                "texture": models.VectorParams(size=TEXTURE_DIM, distance=models.Distance.COSINE),
                "layout": models.VectorParams(size=LAYOUT_DIM, distance=models.Distance.COSINE),
                "parts": models.VectorParams(
                    size=VISUAL_DIM, distance=models.Distance.COSINE,
                    multivector_config=models.MultiVectorConfig(comparator=models.MultiVectorComparator.MAX_SIM),
                ),
            },
        )
    S["qdrant"] = client
    S["embed_lock"] = asyncio.Lock()
    S["feedback"] = load_feedback()
    S["user_active"] = 0.0
    S["crawl"] = load_crawl_state()
    tasks = [asyncio.create_task(crawler()), asyncio.create_task(densifier())] if CRAWL else []
    yield
    for task in tasks:
        task.cancel()
    await S["commons"].close()
    client.close()


app = FastAPI(title="wiki-explorer", lifespan=lifespan)


def load_env() -> dict:
    """KEY=value lines from ``.env``, overridden by real environment variables."""
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                env[key.strip()] = value.strip().strip("'\"")
    env.update({k: v for k, v in os.environ.items() if k in ("UNSPLASH_ACCESS_KEY", "PEXELS_API_KEY")})
    return env


def make_sources(commons: Commons) -> dict:
    """The keyless collections, plus Unsplash / Pexels when their free API key is configured."""
    env = load_env()
    sources = [Cleveland(commons._get), INaturalist(commons._get)]
    if env.get("UNSPLASH_ACCESS_KEY"):
        sources.append(Unsplash(commons._get, env["UNSPLASH_ACCESS_KEY"]))
    if env.get("PEXELS_API_KEY"):
        sources.append(Pexels(commons._get, env["PEXELS_API_KEY"]))
    return {src.key: src for src in sources}


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

    new, failed, late = await embed_items(todo)
    vecs.update(new)
    pending += late
    if new:
        log.info("embedded %d new images (%d failed, %d pending)", len(new), failed, pending)
    return vecs, pending


async def embed_items(items: list[dict]) -> tuple[dict, int, int]:
    """Fetch the thumbnails of ``items`` into memory, embed them and store every vector (both
    collections). Returns ({title: {space: vector}}, failed, timed out)."""
    commons: Commons = S["commons"]
    tasks = {asyncio.create_task(commons.fetch_image(it["thumb"])): it for it in items}
    done, late = await asyncio.wait(tasks, timeout=FETCH_TIMEOUT_S)
    for t in late:
        t.cancel()
    ok = [(tasks[t], t.result()) for t in done if not t.exception()]
    vecs: dict[str, dict] = {}
    if not ok:
        return vecs, len(done), len(late)

    client: QdrantClient = S["qdrant"]
    async with S["embed_lock"]:
        points, dense_points = [], []
        for start in range(0, len(ok), 16):
            batch = ok[start : start + 16]
            visual, semantic, dense = await asyncio.to_thread(S["embedder"].embed_batch, [im for _, im in batch])
            for (it, _), v, s, d in zip(batch, visual, semantic, dense):
                vecs[it["title"]] = {"visual": np.asarray(v, dtype=np.float32), "semantic": np.asarray(s, dtype=np.float32)}
                pid = point_id(it["title"])
                points.append(models.PointStruct(id=pid, vector={"visual": v, "semantic": s}, payload=clean(it)))
                dense_points.append(models.PointStruct(
                    id=pid, vector={"texture": d["texture"], "layout": d["layout"], "parts": d["parts"]},
                    payload={"title": it["title"], "parts_xy": d["parts_xy"]},
                ))
        client.upsert(COLLECTION, points=points)
        client.upsert(DENSE, points=dense_points)
    return vecs, len(done) - len(ok), len(late)


def dense_vectors(titles: list[str], parts: bool = True) -> dict[str, dict]:
    """{title: {"texture", "layout"[, "parts", "parts_xy"]}} for the titles that have them."""
    names = ["texture", "layout"] + (["parts"] if parts else [])
    out = {}
    for start in range(0, len(titles), 256):
        ids = [point_id(t) for t in titles[start : start + 256]]
        for p in S["qdrant"].retrieve(DENSE, ids=ids, with_vectors=names, with_payload=True):
            d = {k: np.asarray(p.vector[k], dtype=np.float32) for k in names}
            d["parts_xy"] = p.payload.get("parts_xy") or []
            out[p.payload["title"]] = d
    return out


async def ensure_dense(item: dict) -> Optional[dict]:
    """Dense vectors of one image (the origin of a lens), computed now if it was indexed before them."""
    have = dense_vectors([item["title"]])
    if item["title"] not in have:
        await embed_items([item])
        have = dense_vectors([item["title"]])
    return have.get(item["title"])


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
    # Already in the index: no request (Unsplash/Pexels have small hourly limits).
    for p in S["qdrant"].retrieve(COLLECTION, ids=[point_id(title)], with_payload=True):
        if p.payload.get("thumb"):
            return clean(p.payload)
    key, _, ident = title.partition(":")
    src = S["sources"].get(key)
    if not src:
        return None
    try:
        return await src.get(ident)
    except RateLimited:
        return None


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
                items, cont = await S["sources"][where].search(
                    what, offset=st["cont"].get(key) or 0, limit=CRAWL_BATCH, background=True)
            if cont:
                st["cont"][key] = cont
            else:
                st["cont"].pop(key, None)
                st["done"].append(key)
            await ensure_vectors(items, new_budget=CRAWL_BATCH, background=True)
            save_crawl_state()
        except asyncio.CancelledError:
            raise
        except RateLimited:
            continue  # that source's hourly budget is spent; other turns go on
        except Exception as exc:  # noqa: BLE001 - keep crawling whatever one category does
            log.warning("crawler %s: %s", key, exc)


async def densifier() -> None:
    """Give the images indexed before the patch-level descriptors existed their texture, layout and
    parts: walk the index in id order, re-fetch the thumbnails that lack them (into memory only)."""
    client: QdrantClient = S["qdrant"]
    st = S["crawl"]
    queue: list[dict] = []
    after_queue, last_page = st.get("dense_offset"), False
    while not st.get("dense_done"):
        await asyncio.sleep(DENSE_PAUSE_S)
        if time.time() - S["user_active"] < USER_QUIET_S:
            continue
        try:
            if not queue:
                # The cursor moves past a page only once all of it is done, so a restart redoes at most one page.
                if last_page:
                    st["dense_done"] = True
                    save_crawl_state()
                    log.info("densifier: every indexed image has patch descriptors")
                    break
                st["dense_offset"] = after_queue
                pts, after_queue = client.scroll(COLLECTION, limit=256, offset=st["dense_offset"], with_payload=True)
                last_page = after_queue is None
                have = {p.id for p in client.retrieve(DENSE, ids=[p.id for p in pts])}
                queue = [clean(p.payload) for p in pts if p.id not in have]
                save_crawl_state()
                continue
            batch, queue = queue[:DENSE_BATCH], queue[DENSE_BATCH:]
            await embed_items(batch)
            if random.random() < 0.05:
                log.info("densifier: %d of %d images have patch descriptors", client.count(DENSE).count, client.count(COLLECTION).count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad batch must not stop the backfill
            log.warning("densifier: %s", exc)


# --------------------------------------------------------------------------- routes


@app.get("/api/health")
async def health():
    client: QdrantClient = S["qdrant"]
    return {"status": "ok", "indexed": client.count(COLLECTION).count, "dense": client.count(DENSE).count,
            "saved": len(saved_items()), "crawling": CRAWL}


def with_saved_flag(items: list[dict]) -> list[dict]:
    saved_titles = {it["title"] for it in saved_items()}
    return [{**it, "saved": it["title"] in saved_titles} for it in items]


@app.get("/api/feed")
async def feed(cont: Optional[str] = None):
    items, nxt = await S["commons"].feed(cont)
    return {"items": with_saved_flag(items), "next": nxt}


async def to_english(query: str) -> tuple[str, Optional[str]]:
    """English query for Commons and CLIP (plus the Wikidata id when it is a known concept).

    A phrase that is exactly the Italian name of a Wikidata concept uses its English label (fixed
    terms: "scale a chiocciola" -> "helical stairs"); everything else goes through the local
    translator, which handles whole phrases and plurals ("fori nel metallo" -> "holes in metal").
    Single words skip Wikidata: there a word's most famous meaning often isn't the one meant
    ("fori" -> the Roman forum instead of holes).
    """
    if " " in query.strip():
        label, qid = await S["commons"].concept(query)
        if label:
            return label, qid
    return await asyncio.to_thread(S["translator"].translate, query), None


async def federated_search(query: str, page: int) -> tuple[list[dict], Optional[int]]:
    """One page from Commons (curated "Quality images" first, then everything) and from each other
    collection, interleaved (3 Commons : 1 other)."""
    jobs = [
        S["commons"].search(query, offset=page * SEARCH_PAGE, limit=SEARCH_PAGE, curated=True),
        S["commons"].search(query, offset=page * SEARCH_PAGE, limit=SEARCH_PAGE),
    ]
    jobs += [src.search(query, offset=page * SOURCE_PAGE, limit=SOURCE_PAGE) for src in S["sources"].values()]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    lists, more = [], False
    for r in results:
        if isinstance(r, Exception):
            if not isinstance(r, RateLimited):
                log.warning("search source failed: %s", r)
            lists.append([])
            continue
        lists.append(r[0])
        more = more or r[1] is not None
    seen: set = set()
    commons_items = [it for it in lists[0] + lists[1] if not (it["title"] in seen or seen.add(it["title"]))]
    others = lists[2:]
    out: list[dict] = []
    for i in range(max([len(commons_items) // 3 + 1] + [len(l) for l in others])):
        out += commons_items[i * 3 : i * 3 + 3]
        out += [l[i] for l in others if i < len(l)]
    return out, (page + 1 if more else None)


def aesthetic(semantic: np.ndarray) -> np.ndarray:
    """Probability-like beauty score per row of CLIP image vectors (see AESTH_POS / AESTH_NEG)."""
    if "aesth" not in S:
        pos = np.asarray(S["embedder"].embed_text(AESTH_POS), dtype=np.float32)
        neg = np.stack([np.asarray(S["embedder"].embed_text([t]), dtype=np.float32) for t in AESTH_NEG])
        S["aesth"] = (pos, neg)
    pos, neg = S["aesth"]
    logits = 100 * np.concatenate([(semantic @ pos)[:, None], semantic @ neg.T], axis=1)
    logits -= logits.max(axis=1, keepdims=True)
    p = np.exp(logits)
    return p[:, 0] / p.sum(axis=1)


def beauty(p: np.ndarray) -> np.ndarray:
    """``aesthetic`` on a 0..1 log scale (1e-3 -> 0, 1 -> 1)."""
    return np.clip((np.log10(p + 1e-9) + 3) / 3, 0, 1)


def wants_documents(query: str) -> bool:
    """True for queries about maps, diagrams, manuscripts... where the beauty filter would hide everything."""
    cache = S.setdefault("wants_documents", {})
    if query not in cache:
        q = np.asarray(S["embedder"].embed_text([f"a photo of a {query}."]), dtype=np.float32)
        cache[query] = float(np.max(S["aesth"][1] @ q)) > WANTS_DOCUMENTS
    return cache[query]


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
    english, qid = await to_english(q)
    if offset == 0:
        note_query(english)
    items, nxt = await federated_search(english, offset)
    vecs, pending = await ensure_vectors(items, new_budget=budget)
    checked = [it for it in items if it["title"] in vecs]
    unchecked = [it for it in items if it["title"] not in vecs]

    hidden = 0
    if checked:
        semantic = np.stack([vecs[it["title"]]["semantic"] for it in checked])
        sims = semantic @ text_vector(english)
        best = float(sims.max())
        cutoff = max(CLIP_FLOOR, best - CLIP_SPREAD) if best >= CLIP_UNDERSTOOD else -1.0
        # Beauty: documents are hidden, the rest is ranked partly by how good the picture is,
        # and flat snapshots go too when there are enough better pictures.
        quality = aesthetic(semantic)
        documents = wants_documents(english)
        floor = 0.0 if documents else JUNK
        if not documents and int(np.sum((sims >= cutoff) & (quality >= PLAIN))) >= MIN_BEAUTIFUL:
            floor = PLAIN
        rank = sims + (0.0 if documents else AESTH_SEARCH) * beauty(quality)
        kept: list[int] = []
        for i in np.argsort(-rank):
            if sims[i] < cutoff or quality[i] < floor:
                continue
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
        res = await asyncio.gather(*(src.search(text, limit=per_source, background=True) for src in S["sources"].values()),
                                   return_exceptions=True)
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
        jobs += [src.search(related, limit=40, background=True), commons.search(related, limit=30)]
    if query:
        english, _ = await to_english(query)
        jobs += [commons.search(english, limit=40), from_sources(english, 12)]
    jobs += [from_categories(t, 2, 25) for t in extra_origins[:5] if t.startswith("File:")]
    results = await asyncio.gather(*jobs, return_exceptions=True)

    lists: list[list[dict]] = []
    for r in results:
        if isinstance(r, Exception):
            if not isinstance(r, RateLimited):
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
    await ensure_dense(origin)

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
    shared = await asyncio.to_thread(parts_similarity, title, [c["title"] for c in ranked])
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
        known = ~np.isnan(shared)
        if space == "visual" and known.sum() >= 2:
            # DINOv2 twice: the global look, and the parts both images are made of.
            parts = score.copy()
            parts[known] = minmax(shared[known])
            score = (1 - PARTS_WEIGHT) * score + PARTS_WEIGHT * parts
        if len(liked_all) >= 2:
            taste = unit(np.mean([vecs[t][space] for t in liked_all], axis=0))
            score = score + TASTE * minmax(matrix @ taste)
        final += w * score

    # Beauty, unless the origin itself is a document / plain picture (then that's what the user wants).
    semantic = np.stack([vecs[c["title"]]["semantic"] for c in ranked])
    quality = aesthetic(semantic)
    origin_quality = float(aesthetic(vecs[title]["semantic"][None, :])[0])
    if origin_quality >= PLAIN:
        final += AESTH_SIMILAR * beauty(quality)
        final[quality < JUNK] = -np.inf

    # Greedy pick in score order, skipping near-duplicates (crops, re-uploads, burst shots) of the
    # origin or of an image already picked, so the top of the list is similar but not repetitive.
    visual = np.stack([vecs[c["title"]]["visual"] for c in ranked])
    origin_v = vecs[title]["visual"]
    picked: list[int] = []
    for i in np.argsort(-final):
        if not np.isfinite(final[i]):
            break
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


def chamfer(a: np.ndarray, a_xy: list, b: np.ndarray, b_xy: list) -> float:
    """How much two images are made of the same things: each region's best match in the other image,
    averaged both ways and weighted by the regions' share of the image."""
    s = a @ b.T
    wa = np.asarray([r[2] for r in a_xy] or [1.0] * len(a), dtype=np.float32)
    wb = np.asarray([r[2] for r in b_xy] or [1.0] * len(b), dtype=np.float32)
    return 0.5 * (float(s.max(1) @ wa) / float(wa.sum()) + float(s.max(0) @ wb) / float(wb.sum()))


def parts_similarity(origin: str, titles: list[str]) -> np.ndarray:
    """Chamfer similarity of ``origin``'s parts to each title's (NaN where descriptors are missing)."""
    dense = dense_vectors([origin] + titles)
    out = np.full(len(titles), np.nan, dtype=np.float32)
    if origin not in dense:
        return out
    o = dense[origin]
    for i, t in enumerate(titles):
        if t in dense:
            out[i] = chamfer(o["parts"], o["parts_xy"], dense[t]["parts"], dense[t]["parts_xy"])
    return out


def zscore(x: np.ndarray) -> np.ndarray:
    return (x - x.mean()) / (x.std() + 1e-6)


def main_vectors(ids: list[str]) -> dict[str, tuple[dict, dict]]:
    """{point id: (item, {space: vector})} from the main collection."""
    out = {}
    for start in range(0, len(ids), 256):
        for p in S["qdrant"].retrieve(COLLECTION, ids=ids[start : start + 256], with_vectors=True, with_payload=True):
            out[p.id] = (clean(p.payload), {s: np.asarray(p.vector[s], dtype=np.float32) for s in SPACES})
    return out


def lens_pool(lens: str, vec: dict, dense: Optional[dict], limit: int) -> list[str]:
    """Point ids of the index images nearest to the origin in the spaces the lens looks at."""
    client: QdrantClient = S["qdrant"]
    # Global DINOv2 neighbours join every pool, so a lens works while the backfill is still running.
    queries = {
        "materia": [(DENSE, "texture", limit), (COLLECTION, "visual", limit // 2)],
        "forma": [(DENSE, "layout", limit), (COLLECTION, "visual", limit // 2)],
        "rime": [(COLLECTION, "visual", RHYME_POOL)],
    }[lens]
    ids: dict[str, None] = {}
    for coll, space, n in queries:
        if coll == DENSE and dense is None:
            continue
        q = (dense if coll == DENSE else vec)[space]
        for p in client.query_points(coll, query=q.tolist(), using=space, limit=n).points:
            ids[p.id] = None
    return list(ids)


def rank_lens(lens: str, title: str, vec: dict, dense: Optional[dict], limit: int) -> list[dict]:
    """Rank the lens pool. ``materia``: same surfaces and colour patterns (texture statistics);
    ``forma``: same composition (layout grid); ``rime``: same form but a different subject: high
    DINOv2 similarity (global, layout, texture) and low CLIP similarity, a visual rhyme."""
    pool = main_vectors(lens_pool(lens, vec, dense, LENS_POOL))
    pool.pop(point_id(title), None)
    if not pool:
        return []
    items = [it for it, _ in pool.values()]
    vis = np.stack([v["visual"] for _, v in pool.values()])
    sem = np.stack([v["semantic"] for _, v in pool.values()])
    pd = dense_vectors([it["title"] for it in items], parts=False) if dense is not None else {}
    vis_sim, sem_sim = vis @ vec["visual"], sem @ vec["semantic"]

    has = np.asarray([it["title"] in pd for it in items])

    def dense_sim(space: str) -> np.ndarray:
        """z-scored similarity in a dense space; images without descriptors yet get their z-scored
        global DINOv2 similarity instead (same scale, so they can be compared)."""
        out = zscore(vis_sim)
        if has.sum() >= 2:
            d = np.asarray([float(pd[it["title"]][space] @ dense[space]) for it in items if it["title"] in pd])
            out[has] = zscore(d)
        return out

    if lens == "materia" or lens == "forma":
        space = "texture" if lens == "materia" else "layout"
        z = dense_sim(space) if dense is not None else zscore(vis_sim)
        raw = np.asarray([float(pd[it["title"]][space] @ dense[space]) if it["title"] in pd else float(v)
                          for it, v in zip(items, vis_sim)])
        # Until the backfill is done, images that have the descriptor come first.
        score = minmax(z + (2.0 if dense is not None else 0.0) * has)
    else:
        form = 0.6 * zscore(vis_sim) + (0.25 * dense_sim("layout") + 0.15 * dense_sim("texture") if dense is not None else 0.4 * zscore(vis_sim))
        raw = vis_sim
        score = minmax(form - RHYME_SEMANTIC * zscore(sem_sim))
        # A rhyme must still look alike.
        score[vis_sim < np.quantile(vis_sim, RHYME_FLOOR)] = -np.inf

    quality = aesthetic(sem)
    if float(aesthetic(vec["semantic"][None, :])[0]) >= PLAIN:
        score = score + AESTH_SIMILAR * beauty(quality)
        score[quality < JUNK] = -np.inf
    picked: list[int] = []
    for i in np.argsort(-score):
        if not np.isfinite(score[i]) or float(vis[i] @ vec["visual"]) > NEAR_DUP:
            continue
        if picked and float(np.max(vis[picked] @ vis[i])) > NEAR_DUP:
            continue
        picked.append(int(i))
        if len(picked) >= limit:
            break
    out = with_saved_flag([items[i] for i in picked])
    for it, i in zip(out, picked):
        it.update(match=round(float(raw[i]), 3), meaning=round(float(sem_sim[i]), 3))
    return out


@app.get("/api/lens")
async def lens(title: str, kind: str = Query("rime", pattern="^(rime|materia|forma)$"), limit: int = Query(60, ge=1, le=600)):
    """Other ways for an image to be alike, searched in the whole local index (instant, no downloads
    except the origin's own descriptors)."""
    origin = await resolve(title)
    if not origin:
        raise HTTPException(404, "Immagine non trovata")
    vecs, _ = await ensure_vectors([origin], new_budget=1)
    if title not in vecs:
        raise HTTPException(502, "L'immagine non risponde, riprova tra poco")
    dense = await ensure_dense(origin)
    items = await asyncio.to_thread(rank_lens, kind, title, vecs[title], dense, limit)
    return {"origin": with_saved_flag([origin])[0], "items": items, "dense": S["qdrant"].count(DENSE).count}


async def patch_map(title: str) -> tuple[dict, dict]:
    """The origin seen patch by patch (``Embedder.patch_map``), kept for the last few images opened."""
    maps: OrderedDict = S.setdefault("maps", OrderedDict())
    if title in maps:
        maps.move_to_end(title)
        return maps[title]
    item = await resolve(title)
    if not item:
        raise HTTPException(404, "Immagine non trovata")
    try:
        image = await S["commons"].fetch_image(item["thumb"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, "L'immagine non risponde, riprova tra poco") from exc
    async with S["embed_lock"]:
        m = await asyncio.to_thread(S["embedder"].patch_map, image)
    maps[title] = (item, m)
    while len(maps) > MAPS_CACHED:
        maps.popitem(last=False)
    return item, m


def b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a, dtype=np.uint8).tobytes()).decode()


@app.get("/api/dino")
async def dino(title: str):
    """What DINOv2 sees: a colour per patch (similar parts, similar colours) and its regions."""
    _, m = await patch_map(title)
    return {"w": m["w"], "h": m["h"], "rgb": b64(m["rgb"]), "segments": b64(m["segments"])}


@app.get("/api/detail")
async def detail(title: str, x: float = Query(..., ge=0, le=1), y: float = Query(..., ge=0, le=1),
                 limit: int = Query(60, ge=1, le=300)):
    """Images that contain the detail at (x, y) of ``title``: the DINOv2 patches of that region
    around the point, matched against the regions (``parts``) of every indexed image."""
    origin, m = await patch_map(title)
    w, h = m["w"], m["h"]
    gx, gy = min(w - 1, int(x * w)), min(h - 1, int(y * h))
    ys, xs = np.mgrid[0:h, 0:w]
    dist = (np.hypot(xs - gx, ys - gy) / max(w, h)).flatten()
    sel = (m["segments"] == m["segments"][gy * w + gx]) & (dist <= DETAIL_RADIUS)
    if sel.sum() < 4:
        sel = dist <= DETAIL_RADIUS * 0.6
    query = unit(m["tokens"][sel].mean(0))

    def run() -> list[dict]:
        client: QdrantClient = S["qdrant"]
        res = client.query_points(DENSE, query=[query.tolist()], using="parts", limit=LENS_POOL, with_payload=True)
        found = {p.id: p.score for p in res.points}
        pool = main_vectors(list(found))
        own = point_id(title)
        ids = [i for i in found if i in pool and i != own]
        if not ids:
            return []
        sem = np.stack([pool[i][1]["semantic"] for i in ids])
        vis = np.stack([pool[i][1]["visual"] for i in ids])
        raw = np.asarray([found[i] for i in ids], dtype=np.float32)
        score = minmax(raw)
        quality = aesthetic(sem)
        score = score + 0.5 * AESTH_SIMILAR * beauty(quality)
        score[quality < JUNK] = -np.inf
        origin_v = S["qdrant"].retrieve(COLLECTION, ids=[own], with_vectors=["visual"])
        ov = np.asarray(origin_v[0].vector["visual"], dtype=np.float32) if origin_v else None
        picked: list[int] = []
        for i in np.argsort(-score):
            if not np.isfinite(score[i]) or (ov is not None and float(vis[i] @ ov) > NEAR_DUP):
                continue
            if picked and float(np.max(vis[picked] @ vis[i])) > NEAR_DUP:
                continue
            picked.append(int(i))
            if len(picked) >= limit:
                break
        # Where the detail is in each result: its best-matching region.
        dense = dense_vectors([pool[ids[i]][0]["title"] for i in picked])
        out = with_saved_flag([pool[ids[i]][0] for i in picked])
        for it, i in zip(out, picked):
            it["match"] = round(float(raw[i]), 3)
            d = dense.get(it["title"])
            if d is not None and d["parts_xy"]:
                j = int(np.argmax(d["parts"] @ query))
                fx, fy, share = d["parts_xy"][j]
                it["focus"] = {"x": fx, "y": fy, "r": round(float(np.sqrt(share)), 3)}
        return out

    items = await asyncio.to_thread(run)
    log.info("detail %s at (%.2f, %.2f): %d patches, %d results", title, x, y, int(sel.sum()), len(items))
    return {"origin": with_saved_flag([origin])[0], "items": items, "mask": b64(sel), "w": w, "h": h}


def drift_step(walk: dict) -> Optional[dict]:
    """One step of a walk: pick a kind of link, then the next image among the best few for it.
    A link with nothing left to offer gives way to the others."""
    cur = walk["path"][-1]
    pid = point_id(cur)
    got = main_vectors([pid])
    if pid not in got:
        return None
    vec = got[pid][1]
    dense = dense_vectors([cur], parts=False).get(cur)
    links = [k for k in DRIFT_LINKS if k != walk["last"] and (dense is not None or k in ("sguardo", "senso", "rima"))]
    while links:
        link = random.choices(links, weights=[DRIFT_LINKS[k] for k in links])[0]
        step = drift_link(walk, link, vec, dense)
        if step:
            return step
        links.remove(link)
    return None


def drift_link(walk: dict, link: str, vec: dict, dense: Optional[dict]) -> Optional[dict]:
    client: QdrantClient = S["qdrant"]
    coll, space, q = {
        "forma": (DENSE, "layout", dense and dense["layout"]),
        "materia": (DENSE, "texture", dense and dense["texture"]),
        "sguardo": (COLLECTION, "visual", vec["visual"]),
        "senso": (COLLECTION, "semantic", vec["semantic"]),
        "rima": (COLLECTION, "visual", vec["visual"]),
    }[link]
    if link == "rima" and len(walk["recent"]) >= 3:
        q = unit(vec["visual"] - DRIFT_RESIDUAL * np.mean(walk["recent"][-DRIFT_NOVELTY_STEPS - 1 : -1], axis=0))
    n = RHYME_POOL // 2 if link == "rima" else DRIFT_POOL
    res = client.query_points(coll, query=q.tolist(), using=space, limit=n + len(walk["path"]))
    found = {p.id: p.score for p in res.points}
    pool = main_vectors(list(found))
    ids = [i for i in found if i in pool and pool[i][0]["title"] not in walk["seen"]]
    if not ids:
        return None
    vis = np.stack([pool[i][1]["visual"] for i in ids])
    sem = np.stack([pool[i][1]["semantic"] for i in ids])
    raw = np.asarray([found[i] for i in ids], dtype=np.float32)
    if link == "rima":
        score = minmax(zscore(raw) - RHYME_SEMANTIC * zscore(sem @ vec["semantic"]))
        score[raw < np.quantile(raw, RHYME_FLOOR)] = -np.inf
    else:
        score = minmax(raw)
    # Momentum: keep moving the way the last step went instead of falling back.
    if walk["prev"] is not None:
        direction = unit(vec["visual"] - walk["prev"])
        steps = vis - vec["visual"]
        steps /= np.linalg.norm(steps, axis=1, keepdims=True) + 1e-6
        score = score + DRIFT_MOMENTUM * (steps @ direction)
    quality = aesthetic(sem)
    score = score + AESTH_SIMILAR * beauty(quality)
    # Stay among beautiful pictures, unless the walk is among documents / plain shots already.
    plain_here = float(aesthetic(vec["semantic"][None, :])[0]) < PLAIN
    score[quality < (JUNK if plain_here else PLAIN)] = -np.inf
    recent = np.stack(walk["recent"])
    score[np.max(vis @ recent.T, axis=1) > NEAR_DUP] = -np.inf
    lately = np.stack(walk["recent_sem"][-DRIFT_NOVELTY_STEPS:])
    score = score - DRIFT_NOVELTY * np.max(sem @ lately.T, axis=1)  # absolute: a pool of one species stays penalised
    top = [int(i) for i in np.argsort(-score)[:DRIFT_TOP] if np.isfinite(score[i])]
    if not top:
        return None
    p = np.exp((score[top] - score[top].max()) / DRIFT_TEMPERATURE)
    i = int(np.random.choice(top, p=p / p.sum()))
    item = pool[ids[i]][0]
    walk["prev"] = vec["visual"]
    walk["last"] = link
    walk["path"].append(item["title"])
    walk["seen"].add(item["title"])
    walk["recent"] = (walk["recent"] + [vis[i]])[-DRIFT_RECENT:]
    walk["recent_sem"] = (walk["recent_sem"] + [sem[i]])[-DRIFT_RECENT:]
    return {**item, "link": link, "match": round(float(raw[i]), 3)}


@app.get("/api/drift")
async def drift(title: str, walk: Optional[str] = None, steps: int = Query(8, ge=1, le=24)):
    """A walk ("deriva") through the index from ``title``; call again with ``walk`` to go on."""
    walks: OrderedDict = S.setdefault("walks", OrderedDict())
    w = walks.get(walk) if walk else None
    if w is None:
        origin = await resolve(title)
        if not origin:
            raise HTTPException(404, "Immagine non trovata")
        vecs, _ = await ensure_vectors([origin], new_budget=1)
        if title not in vecs:
            raise HTTPException(502, "L'immagine non risponde, riprova tra poco")
        await ensure_dense(origin)
        walk = uuid.uuid4().hex[:12]
        w = {"path": [title], "seen": {title}, "prev": None, "last": None, "recent": [vecs[title]["visual"]],
             "recent_sem": [vecs[title]["semantic"]]}
        walks[walk] = w
        while len(walks) > 50:
            walks.popitem(last=False)
    out = []
    for _ in range(steps):
        step = await asyncio.to_thread(drift_step, w)
        if step is None:
            break
        out.append(step)
    return {"walk": walk, "steps": with_saved_flag(out)}


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
    # ...and pictures that aren't nice (documents, flat snapshots): a home feed should be beautiful.
    out = client.retrieve(COLLECTION, ids=[point_id(it["title"]) for it in order], with_vectors=True, with_payload=["title"])
    out_vecs = {p.payload["title"]: np.asarray(p.vector["visual"], dtype=np.float32) for p in out}
    quality = dict(zip((p.payload["title"] for p in out),
                       aesthetic(np.stack([np.asarray(p.vector["semantic"], dtype=np.float32) for p in out])) if out else []))
    kept: list[dict] = []
    kept_v: list[np.ndarray] = []
    for it in order:
        v = out_vecs.get(it["title"])
        if v is None or quality[it["title"]] < PLAIN:
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
