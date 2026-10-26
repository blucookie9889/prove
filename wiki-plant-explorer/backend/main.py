"""FastAPI service for wiki-plant-explorer.

Run from the project root:
    uvicorn backend.main:app --host 0.0.0.0 --port 8000

Note: embedded Qdrant holds an exclusive lock on ``qdrant_db/``, so stop this server
before running ``backend/ingest.py`` again.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from qdrant_client import QdrantClient

DB_PATH = Path(__file__).resolve().parent.parent / "qdrant_db"
COLLECTION = "wiki_plants"
# How many neighbours to pull from each vector space before blending. The union of both
# candidate pools is re-scored exactly in both spaces, so a wider pool only costs a few ms.
CANDIDATE_MULTIPLIER = 4
MIN_CANDIDATES = 50

state: dict[str, QdrantClient] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    client = QdrantClient(path=str(DB_PATH))
    if not client.collection_exists(COLLECTION):
        client.close()
        raise RuntimeError(f"Collection '{COLLECTION}' not found in {DB_PATH}. Run backend/ingest.py first.")
    state["qdrant"] = client
    yield
    client.close()


app = FastAPI(title="wiki-plant-explorer", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def qdrant() -> QdrantClient:
    return state["qdrant"]


def minmax(scores: np.ndarray) -> np.ndarray:
    lo, hi = float(scores.min()), float(scores.max())
    if hi - lo < 1e-9:
        return np.ones_like(scores)
    return (scores - lo) / (hi - lo)


@app.get("/api/health")
def health():
    return {"status": "ok", "points": qdrant().count(COLLECTION).count}


@app.get("/api/feed")
def feed(
    limit: int = Query(40, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Paginated scroll through the collection.

    Ingestion assigns contiguous integer ids starting at 0, so the numeric ``offset`` is
    both a position and the Qdrant scroll start id.
    """
    client = qdrant()
    points, next_id = client.scroll(
        COLLECTION,
        limit=limit,
        offset=offset,
        with_payload=True,
        with_vectors=False,
    )
    return {
        "items": [{"id": p.id, **p.payload} for p in points],
        "offset": offset,
        "limit": limit,
        "next_offset": next_id,
        "total": client.count(COLLECTION).count,
    }


@app.get("/api/similar")
def similar(
    point_id: int = Query(..., ge=0),
    weight: float = Query(0.5, ge=0.0, le=1.0),
    limit: int = Query(30, ge=1, le=200),
):
    """Hybrid similarity: ``weight`` = 1.0 is pure DINOv2 (visual), 0.0 is pure OpenCLIP (semantic)."""
    client = qdrant()
    origin = client.retrieve(COLLECTION, ids=[point_id], with_vectors=True, with_payload=True)
    if not origin:
        raise HTTPException(status_code=404, detail=f"Point {point_id} not found")
    origin = origin[0]

    pool = max(limit * CANDIDATE_MULTIPLIER, MIN_CANDIDATES)
    candidate_ids: set = set()
    for space in ("visual", "semantic"):
        hits = client.query_points(
            COLLECTION,
            query=origin.vector[space],
            using=space,
            limit=pool + 1,  # +1 because the origin itself is always the top hit
            with_payload=False,
        ).points
        candidate_ids.update(h.id for h in hits)
    candidate_ids.discard(origin.id)
    if not candidate_ids:
        return {"origin": {"id": origin.id, **origin.payload}, "weight": weight, "items": []}

    # Re-score every candidate in *both* spaces so a point that only appeared in one
    # ANN result list isn't penalised with a missing score.
    candidates = client.retrieve(COLLECTION, ids=list(candidate_ids), with_vectors=True, with_payload=True)
    scores = {}
    for space in ("visual", "semantic"):
        query = np.asarray(origin.vector[space], dtype=np.float32)
        matrix = np.asarray([c.vector[space] for c in candidates], dtype=np.float32)
        query /= np.linalg.norm(query) or 1.0
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True).clip(min=1e-12)
        scores[space] = matrix @ query

    visual_norm = minmax(scores["visual"])
    semantic_norm = minmax(scores["semantic"])
    final = visual_norm * weight + semantic_norm * (1.0 - weight)

    order = np.argsort(-final)[:limit]
    items = [
        {
            "id": candidates[i].id,
            **candidates[i].payload,
            "score": round(float(final[i]), 4),
            "visual_score": round(float(scores["visual"][i]), 4),
            "semantic_score": round(float(scores["semantic"][i]), 4),
        }
        for i in order
    ]
    return {"origin": {"id": origin.id, **origin.payload}, "weight": weight, "items": items}
