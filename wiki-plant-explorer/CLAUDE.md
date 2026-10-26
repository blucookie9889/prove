# wiki-plant-explorer

Dual-vector visual/semantic search over ~300 plant species pulled from Wikidata / Wikimedia Commons.

## Architecture

```
backend/ingest.py  Wikidata SPARQL -> Commons thumbnails (500px) -> DINOv2 + OpenCLIP -> Qdrant (embedded)
backend/main.py    FastAPI: /api/feed (scroll), /api/similar (hybrid re-ranking), /api/health
frontend/          React 19 + Vite 6 + Tailwind 4 dashboard; Vite proxies /api -> http://localhost:8000
qdrant_db/         Embedded Qdrant storage (git-ignored, created by ingest.py)
```

- Collection `wiki_plants`, named vectors:
  - `visual`   768-d cosine, `facebook/dinov2-base` pooler output (shape / texture / style)
  - `semantic` 512-d cosine, OpenCLIP `ViT-B-32` / `laion2b_s34b_b79k` image embedding (concept / taxonomy)
- Payload: `wiki_id, title, taxon, family, image_url, wiki_url`.
- Point ids are contiguous ints from 0 (assigned only on successful embed), so `/api/feed?offset=N`
  is both a position and the Qdrant scroll start id. Keep this invariant if you change ingestion.

## Hybrid scoring (`/api/similar`)

1. Retrieve the origin point's two vectors.
2. ANN search each space for `max(limit*4, 50)` candidates; union them, drop the origin.
3. Re-score every candidate exactly in both spaces (so a candidate found by only one search
   still gets a real score in the other), min-max normalise each space over the candidate set.
4. `final_score = visual_norm * weight + semantic_norm * (1 - weight)`; return top `limit`.
   Raw cosine scores are also returned as `visual_score` / `semantic_score`.

`weight = 1.0` is pure DINOv2 (shape), `0.0` pure OpenCLIP (concept).

## Data source notes

- A whole-tree walk (`wdt:P171+ wd:Q756` over all species) and the literal `wdt:P31/wdt:P279* wd:Q756`
  pattern both fail on the public endpoint: the first times out, the second matches almost nothing
  because species are `instance of taxon`, not subclasses of Plantae. `ingest.py` instead anchors on
  a list of 15 plant families (verified to descend from Q756) and takes an equal share per family.
  Edit `FAMILIES` to change the mix.
- Thumbnails come from the Commons `imageinfo` API with `iiurlwidth=500`.
- Wikimedia requires a descriptive User-Agent (`WikiPlantExplorer/1.0 (...)`). Some cloud/CI egress
  IPs get `403 Please respect our robot policy` for Python HTTP clients; run ingestion from a normal
  network if that happens.

## Commands (run from this directory)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
python backend/ingest.py                               # ~2-10 min; downloads models on first run
uvicorn backend.main:app --host 0.0.0.0 --port 8000

cd frontend && npm install && npm run dev               # http://localhost:5173
```

## Gotchas

- Embedded Qdrant takes an exclusive file lock on `qdrant_db/`: stop uvicorn before re-running
  `ingest.py`, and run uvicorn with a single worker.
- `ingest.py` drops and recreates the collection on every run.
- `qdrant-client>=1.12` is required for `query_points` (the old `search` API was removed).
- The frontend has no API base URL setting; it relies on the Vite dev proxy (`vite.config.js`).
  For a production build, serve `frontend/dist` behind the same origin as the API.
