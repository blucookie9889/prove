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
import re
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
from .embedder import SEMANTIC_DIM, VISUAL_DIM, Embedder

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "qdrant_db"
FEEDBACK_PATH = ROOT / "state" / "feedback.json"
CRAWL_PATH = ROOT / "state" / "crawler.json"
ENV_PATH = ROOT / ".env"  # API keys (git-ignored); see .env.example
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
# Image page: like the words of a Wikipedia article, the things in an image are links. DINOv2 splits
# it into blobs (see Embedder.regions); each blob is cropped, embedded, and searched in the index.
LINK_MIN_AREA = 0.012  # smaller blobs are noise
LINK_MAX_AREA = 0.5  # larger ones are the background or the whole picture
LINK_PAD = 0.12  # crop = blob's box grown by this share of its size...
LINK_MIN_SIDE = 0.18  # ...and at least this share of the image's sides, for some context
LINK_SAME_THING = 0.8  # two blobs whose crops are this alike (DINOv2) are one link (two eyes)...
LINK_SAME_SUBJECT = 0.92  # ...or this alike for CLIP (two strips of the same sky)
LINK_MIN_CONTRAST = 14  # crops flatter than this (std of grey levels, 0-255) are shadow or blank wall
LINK_MIN_LIGHT = 10  # ...or darker than this mean grey level (night scenes with lights are fine)
LINK_WHOLE = 0.85  # a crop this alike to the whole image adds nothing: no link
MAX_LINKS = 7
LINK_POOL = 200  # index neighbours of each crop, by look and by subject
LINK_TARGETS = 10
# A link must lead somewhere else: minus this times the CLIP similarity to the whole origin, so the
# door in a painting leads to doors, not to more of the same painting.
LINK_ESCAPE = 0.5
PAGES_CACHED = 32
# Link targets must be pictures: no game textures or swatches (CLIP close to "blank" prompts; 99.9%
# of the index is below 0.26), no strips (scrolls 900 x 12 px are unreadable as a thumbnail).
BLANK_PROMPTS = ["a solid color image", "a blank empty image", "a plain black rectangle", "a uniform flat colour background"]
BLANK = 0.27
MAX_ASPECT = 3.5
# Concepts: what a link is about, as an encyclopedia article (a Wikidata item with a Wikipedia page).
# Candidates come from the link's images: what their structured data says they "depict" (P180:
# "water drop", "leaf", "Ferris wheel") and the topics of the Commons categories people filed them in.
# CLIP picks the one that best names the crop: cosine of the crop with "a photo of <English label>",
# plus CONCEPT_CONSENSUS per (rank-weighted) image that names it.
CONCEPT_CONSENSUS = 0.015
CONCEPT_DEPICTS = 1.5  # a "depicts" statement names the thing itself: it counts more than a category
CONCEPT_OWN = 0.3  # what the origin itself depicts or is filed under also counts, a little
CONCEPT_PAGE = 40
CONCEPTS_CACHED = 2000
# Categories about the archive rather than the world: how it was photographed, its status, its facets.
ARCHIVE_NAME = re.compile(
    r"photograph|photography|images\b|pictures\b|aspect\b|focus stack|iucn|least concern|species\b|"
    r"\bby (colou?r|taxon|country|year|month|period|location|name|shape|condition)\b|textures? from|"
    r"january|february|march|april|may\b|june|july|august|september|october|november|december|hemisphere|"
    r"national (birds|animals|flowers|symbols)|with (left|right) hand|unidentified|in culture\b|"
    r"\b(sitting|standing|facing|looking)\b|\bviews?\b|\bdetails?\b|collections? of|museum", re.IGNORECASE)
# A place, a person or a work found by look is nearly always another one: a town that looks like this
# one, not this one. Recognised by its Wikidata description, and left out.
PLACE_OR_WORK = re.compile(
    r"\b(comune|city|town|village|neighbo|district|quartiere|frazione|località|municipality|county|province|"
    r"provincia|region\b|regione|island (in|of)|isola (di|del)|mountain (in|range)|monte|peak in|river in|"
    r"fiume|lake in|lago|capital|country in|stato|building in|church in|chiesa|castle in|street in|piazza|"
    r"square in|national park|parco nazionale|painting by|dipinto|painter|pittore|artist|architect|"
    r"politician|actor|singer|surname|cognome|given name|human settlement|dam in|bridge in|stadium in|"
    r"video game|film\b|album|company|brand)", re.IGNORECASE)
CONCEPT_PAGE = 40
CONCEPTS_CACHED = 2000

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


def large_url(item: dict) -> str:
    """A bigger version of the image for cropping its parts (Commons serves 1280 px thumbnails)."""
    if "upload.wikimedia.org" in item["thumb"]:
        if item.get("full_width") and item["full_width"] <= 1280:
            return item["full"]
        return re.sub(r"/\d+px-", "/1280px-", item["thumb"])
    return item["thumb"]


def main_vectors(ids: list[str]) -> dict[str, tuple[dict, dict]]:
    """{point id: (item, {space: vector})} from the index."""
    out = {}
    for start in range(0, len(ids), 256):
        for p in S["qdrant"].retrieve(COLLECTION, ids=ids[start : start + 256], with_vectors=True, with_payload=True):
            out[p.id] = (clean(p.payload), {s: np.asarray(p.vector[s], dtype=np.float32) for s in SPACES})
    return out


def link_regions(reg: dict) -> list[dict]:
    """Blobs worth a link, with their crop box (fractions of the image), centre and mask."""
    gw, gh = reg["w"], reg["h"]
    out = []
    for mask in reg["masks"]:
        area = float(mask.mean())
        if not LINK_MIN_AREA <= area <= LINK_MAX_AREA:
            continue
        ys, xs = np.nonzero(mask)
        x0, x1, y0, y1 = xs.min() / gw, (xs.max() + 1) / gw, ys.min() / gh, (ys.max() + 1) / gh
        if (x0 == 0) + (y0 == 0) + (x1 == 1) + (y1 == 1) >= 3:
            continue  # wraps around the picture: background
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        half_w = max((x1 - x0) * (1 + 2 * LINK_PAD), LINK_MIN_SIDE) / 2
        half_h = max((y1 - y0) * (1 + 2 * LINK_PAD), LINK_MIN_SIDE) / 2
        box = [max(0.0, cx - half_w), max(0.0, cy - half_h), min(1.0, cx + half_w), min(1.0, cy + half_h)]
        # The hotspot sits on the blob itself (its centre of mass may fall outside a crescent shape).
        k = int(np.argmin((xs - xs.mean()) ** 2 + (ys - ys.mean()) ** 2))
        out.append({"mask": mask, "area": area, "box": box, "dot": [(xs[k] + 0.5) / gw, (ys[k] + 0.5) / gh]})
    return sorted(out, key=lambda r: -r["area"])[: MAX_LINKS * 2]


def rank_links(origin: dict, vec: dict, regions: list[dict], crop_vis: np.ndarray, crop_sem: np.ndarray) -> list[dict]:
    """Where each region leads: the index images most like its crop, by look and by subject, away
    from the origin's own subject. Each image is used by one link only, so every link opens a
    different door."""
    client: QdrantClient = S["qdrant"]
    own = point_id(origin["title"])
    links: list[dict] = []
    for r, cv, cs in zip(regions, crop_vis, crop_sem):
        if float(cv @ vec["visual"]) > LINK_WHOLE:
            continue
        same = next((l for l in links if float(l["vis"] @ cv) > LINK_SAME_THING or float(l["sem"] @ cs) > LINK_SAME_SUBJECT), None)
        if same:
            same["mask"] = same["mask"] | r["mask"]
            continue
        links.append({**r, "vis": cv, "sem": cs})

    ids: dict[str, None] = {}
    for l in links:
        for space, q in (("visual", l["vis"]), ("semantic", l["sem"])):
            for p in client.query_points(COLLECTION, query=q.tolist(), using=space, limit=LINK_POOL).points:
                ids[p.id] = None
    pool = main_vectors([i for i in ids if i != own])
    if not pool:
        return []
    keys = list(pool)
    items = [pool[k][0] for k in keys]
    vis = np.stack([pool[k][1]["visual"] for k in keys])
    sem = np.stack([pool[k][1]["semantic"] for k in keys])
    quality = aesthetic(sem)
    away = sem @ vec["semantic"]
    base = AESTH_SIMILAR * beauty(quality) - LINK_ESCAPE * away
    if "blank" not in S:
        S["blank"] = np.asarray(S["embedder"].embed_text(BLANK_PROMPTS), dtype=np.float32)
    aspect = np.asarray([max(it["width"] or 1, 1) / max(it["height"] or 1, 1) for it in items], dtype=np.float32)
    base[(quality < JUNK) | (vis @ vec["visual"] > NEAR_DUP) | (sem @ S["blank"] > BLANK)
         | (aspect > MAX_ASPECT) | (aspect < 1 / MAX_ASPECT)] = -np.inf

    for l in links:
        l["score"] = 0.5 * (vis @ l["vis"]) + 0.5 * (sem @ l["sem"]) + base
        l["best"] = float(np.max(l["score"]))
    links.sort(key=lambda l: -l["best"])
    used: set = set()
    out = []
    for l in links[:MAX_LINKS]:
        picked: list[int] = []
        for i in np.argsort(-l["score"]):
            if not np.isfinite(l["score"][i]) or items[i]["title"] in used:
                continue
            if picked and float(np.max(vis[picked] @ vis[i])) > NEAR_DUP:
                continue
            picked.append(int(i))
            if len(picked) >= LINK_TARGETS:
                break
        if not picked:
            continue
        used.update(items[i]["title"] for i in picked)
        targets = with_saved_flag([items[i] for i in picked])
        for t, i in zip(targets, picked):
            t["match"] = round(float(0.5 * vis[i] @ l["vis"] + 0.5 * sem[i] @ l["sem"]), 3)
        out.append({"box": [round(v, 4) for v in l["box"]], "dot": [round(float(v), 4) for v in l["dot"]],
                    "mask": l["mask"], "sem": l["sem"], "items": targets})
    return out


@app.get("/api/page")
async def page(title: str):
    """An image as a page whose links are the things in it.

    Returns the regions DINOv2 finds (``linkmap``: link index per patch, 255 = none, on a w x h grid)
    and, for each link, its crop box, a hotspot and the images it leads to.
    """
    pages: OrderedDict = S.setdefault("pages", OrderedDict())
    if title in pages:
        pages.move_to_end(title)
        return pages[title]
    origin = await resolve(title)
    if not origin:
        raise HTTPException(404, "Immagine non trovata")
    vecs, _ = await ensure_vectors([origin], new_budget=1)
    if title not in vecs:
        raise HTTPException(502, "L'immagine non risponde, riprova tra poco")
    t0 = time.time()
    commons: Commons = S["commons"]
    try:
        image = await commons.fetch_image(large_url(origin))
    except Exception:  # noqa: BLE001 - the grid thumbnail will do
        image = await commons.fetch_image(origin["thumb"])

    async with S["embed_lock"]:
        reg = await asyncio.to_thread(S["embedder"].regions, image)
        W, H = image.size
        regions, crops = [], []
        for r in link_regions(reg):
            b = r["box"]
            crop = image.crop((int(b[0] * W), int(b[1] * H), int(b[2] * W), int(b[3] * H)))
            grey = np.asarray(crop.convert("L").resize((64, 64)), dtype=np.float32)
            if grey.std() < LINK_MIN_CONTRAST or grey.mean() < LINK_MIN_LIGHT:
                continue  # nothing to see there: a shadow, a blank wall, an empty sky
            regions.append(r)
            crops.append(crop)
        crop_vis, crop_sem = await asyncio.to_thread(S["embedder"].embed_batch, crops) if crops else ([], [])
    links = await asyncio.to_thread(
        rank_links, origin, vecs[title], regions, np.asarray(crop_vis, dtype=np.float32), np.asarray(crop_sem, dtype=np.float32))

    linkmap = np.full((reg["h"], reg["w"]), 255, dtype=np.uint8)
    for i, l in enumerate(links):
        linkmap[l.pop("mask") & (linkmap == 255)] = i
    S.setdefault("crops", {})[title] = [l.pop("sem") for l in links]  # for naming the links (/api/concepts)
    result = {"origin": with_saved_flag([origin])[0], "w": reg["w"], "h": reg["h"],
              "linkmap": base64.b64encode(linkmap.tobytes()).decode(), "links": links}
    pages[title] = result
    while len(pages) > PAGES_CACHED:
        pages.popitem(last=False)
    log.info("page %s: %d regions, %d links, %.1fs", title, len(regions), len(links), time.time() - t0)
    return result


def label_vectors(labels: list[str]) -> np.ndarray:
    """CLIP text vectors of "a photo of <label>", cached."""
    cache = S.setdefault("label_vectors", {})
    todo = [l for l in dict.fromkeys(labels) if l not in cache]
    if todo:
        for l, v in zip(todo, S["embedder"].embed_texts([f"a photo of {l}." for l in todo])):
            cache[l] = v
    return np.stack([cache[l] for l in labels]) if labels else np.zeros((0, SEMANTIC_DIM), np.float32)


def explainable(c: Optional[dict]) -> bool:
    """A concept worth a link: it has a Wikipedia article and is a thing, not a place, person or work."""
    return bool(c and c.get("title") and not c.get("category_item") and not PLACE_OR_WORK.search(c.get("description", "")))


async def summarize(c: dict) -> dict:
    """A concept as the UI shows it: {"qid", "name", "description", "about", "lang", "extract",
    "image", "url"} (the article's summary), cached by Wikidata id."""
    cache: OrderedDict = S.setdefault("concepts", OrderedDict())
    if c["qid"] in cache:
        return cache[c["qid"]]
    out = {k: c.get(k) for k in ("qid", "name", "description", "lang")}
    out["about"] = c.get("title")
    if c.get("title"):
        try:
            out.update(await S["commons"].summary(c["lang"], c["title"]))
        except Exception as exc:  # noqa: BLE001 - the name alone still says what it is
            log.warning("summary %s: %s", c["title"], exc)
    cache[c["qid"]] = out
    while len(cache) > CONCEPTS_CACHED:
        cache.popitem(last=False)
    return out


@app.get("/api/concepts")
async def concepts(title: str):
    """What each link of ``title``'s page is about (same order as /api/page's links), so a detail
    leads to what explains it: a drop on a leaf to surface tension, a wheel to the Ferris wheel.
    A few batched requests per page: Wikimedia answers 429 to many small ones."""
    data = await page(title)
    crops = S.get("crops", {}).get(title)
    if not data["links"] or crops is None:
        return {"concepts": []}
    files, weights, owner = [], [], []
    for i, l in enumerate(data["links"]):
        for rank, it in enumerate(l["items"]):
            if it["title"].startswith("File:"):
                files.append(it["title"])
                weights.append(0.85 ** rank)
                owner.append(i)
    try:
        # One lookup for every file of every link, then votes split per link.
        commons: Commons = S["commons"]
        uniq = list(dict.fromkeys(files + ([title] if title.startswith("File:") else [])))
        filed, depicted = await asyncio.gather(commons.categories_of(uniq), commons.depicts_of(uniq))
        cats = list(dict.fromkeys(c for f in uniq for c in filed.get(f, [])
                                  if not ARCHIVE_NAME.search(c.removeprefix("Category:"))))
        arts = await commons.articles(cats)

        def named(f: str) -> set:
            return set(depicted.get(f, [])), {arts[c]["qid"] for c in filed.get(f, []) if arts.get(c, {}).get("qid")}

        per_link: list[dict[str, float]] = [{} for _ in data["links"]]
        for f, w, i in zip(files, weights, owner):
            dep, top = named(f)
            for q in dep:
                per_link[i][q] = per_link[i].get(q, 0.0) + CONCEPT_DEPICTS * w
            for q in top:
                per_link[i][q] = per_link[i].get(q, 0.0) + w
        if title.startswith("File:"):
            dep, top = named(title)
            for votes in per_link:
                for q in dep | top:
                    votes[q] = votes.get(q, 0.0) + CONCEPT_OWN
        info = await commons.concepts(list({q for votes in per_link for q in votes}))
        good = {q: c for q, c in info.items() if explainable(c)}
        try:
            for q in await commons.individuals(list(good)):
                good.pop(q, None)
        except Exception as exc:  # noqa: BLE001 - query service busy: the description filter has to do
            log.warning("individuals: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("concepts %s: %s", title, exc)
        return {"concepts": [None] * len(data["links"])}
    qids = list(good)
    vectors = dict(zip(qids, await asyncio.to_thread(label_vectors, [good[q]["en"] for q in qids]))) if qids else {}

    chosen: list[Optional[str]] = []
    for votes, crop in zip(per_link, crops):
        ranked = sorted((q for q in votes if q in good),
                        key=lambda q: -(float(vectors[q] @ crop) + CONCEPT_CONSENSUS * min(votes[q], 6)))
        chosen.append(next((q for q in ranked if q not in chosen), None))
    found = await asyncio.gather(*(summarize(good[q]) if q else asyncio.sleep(0) for q in chosen))
    log.info("concepts %s: %s", title, [f.get("name") if f else None for f in found])
    return {"concepts": [f or None for f in found]}


@app.get("/api/concept")
async def concept(qid: str = Query(..., pattern=r"^Q\d+$"), offset: int = Query(0, ge=0)):
    """A concept page: the article, the images that depict it (plus its Commons category's), and
    "voci collegate": the other concepts those images depict."""
    commons: Commons = S["commons"]
    info = (await commons.concepts([qid], claims=True)).get(qid)
    if not info:
        raise HTTPException(404, "Voce non trovata")
    head = await summarize(info)
    items, nxt = await commons.search(f"haswbstatement:P180={qid}", offset=offset, limit=CONCEPT_PAGE)
    related: list[dict] = []
    if offset == 0:
        if info.get("category") and len(items) < 24:
            more, _ = await commons.category_files(info["category"], limit=CONCEPT_PAGE)
            seen = {it["title"] for it in items}
            items += [it for it in more if it["title"] not in seen]
        try:
            depicted = await commons.depicts_of([it["title"] for it in items[:50]])
            counts: dict[str, int] = {}
            for qs in depicted.values():
                for q in set(qs) - {qid}:
                    counts[q] = counts.get(q, 0) + 1
            top = [q for q, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:24]]
            near = await commons.concepts(top)
            ones = await commons.individuals([q for q in top if explainable(near.get(q))])
            related = [{"qid": q, "name": near[q]["name"]} for q in top if explainable(near.get(q)) and q not in ones][:14]
        except Exception as exc:  # noqa: BLE001 - the page works without them
            log.warning("related %s: %s", qid, exc)
    return {**head, "items": with_saved_flag(items), "next": nxt, "related": related}


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
