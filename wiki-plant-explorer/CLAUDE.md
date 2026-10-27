# wiki-plant-explorer (Wiki Explorer)

A personal, Pinterest-style explorer over **all of Wikimedia Commons**, browsed live through the public
APIs. Click an image to get visually similar ones; "più così / meno così" feedback refines the ranking
and the app slowly learns the user's visual taste. No image is ever stored on disk.

## Architecture

```
backend/commons.py   async client for Commons/Wikidata APIs (search, feed, categories, depicts, thumbs in memory)
backend/embedder.py  DINOv2 (visual, 768-d) + OpenCLIP ViT-B-32 (semantic, 512-d), batched, L2-normalised
backend/main.py      FastAPI: /api/feed, /api/search, /api/file, /api/similar, /api/vote, /api/saved, /api/health
frontend/            React 19 + Vite 6 + Tailwind 4, white image-first UI; Vite proxies /api to the backend
qdrant_db/           embedded Qdrant: collection `commons_vectors` = vector cache (git-ignored)
state/feedback.json  votes and saved images (git-ignored)
```

- UI language is Italian. Routes are URL params: `?q=`, `?file=File:...&q=`, `?view=saved`.
- Home feed = newest members of `Category:Featured pictures on Wikimedia Commons`.
- Search (`/api/search`):
  1. `Commons.translate` maps the query to the English label of a Wikidata item, because Commons
     titles are mostly English and Italian words hit surnames and places ("buchi" = a surname).
     - Only exact Italian label/alias matches count, and the words are also tried in the singular
       (`_singulars`: "buchi" -> "buco" -> "hole", "occhi" -> "occhio" -> "eye").
     - A capitalised match for a lower-case query is a proper noun and is used only as a last resort.
     - Names and works are dropped by their description (`_PROPER`).
     - The remaining matches are ranked by Wikipedia sitelinks, then labels over aliases, then the
       form as typed.
     - Phrases with no item are translated word by word ("foglie rosse" -> "leaf red").
  2. `generator=search` with `filetype:bitmap`, 48 per page.
  3. Every result is checked with CLIP: image vs. the text embedding of the English query (prompt
     ensemble). Results below `max(0.19, best - 0.13)` are hidden, unless CLIP doesn't understand the
     query (best < 0.22). The rest are sorted by score, with near-duplicates removed.
  4. The frontend loads the first page with `budget=0`, which is instant: it uses cached vectors and
     leaves the other results unchecked. It then calls `budget=48` and swaps the cleaned page in with a
     View Transition.

## Similar images (`/api/similar`)

Nobody publishes visual embeddings for Commons and its built-in `morelike:` is text-only (useless
visually), so similarity is two-stage, like Pinterest:

1. **Candidates** (up to 400): files in the origin's 4 most specific content categories (noise like
   "Featured pictures", "Photographs by", licences, years filtered out; categories with <3 files
   dropped; parent categories added when that yields <60 files), files sharing its structured-data
   "depicts" (P180), the current search query, and the categories of: results marked "più così" and
   the top 4 visual matches of the previous call (second hop, `S["best"]`). Round-robin merged.
2. **Visual re-rank**: thumbnails are fetched into memory, embedded, and only the vectors are cached
   in Qdrant (point id = uuid5 of the file title). The origin is always embedded first; then each call
   embeds at most `budget` unseen candidates (frontend: 24 first, then up to 3 rounds of 64), so
   answers get better as the page refines. Calls for the same origin are serialised (`origin_lock`).
3. Score per space = min-max(cosine to query); `final = w*visual + (1-w)*semantic` (default w=0.75).
   Query vector = origin + 0.7*mean(più così) - 0.35*mean(meno così) for votes given on this origin
   (Rocchio). With >=2 liked images overall, a small pull (0.12) towards their mean ("taste").
4. Greedy pick in score order, dropping near-duplicates (DINOv2 cosine > 0.94 to the origin or to an
   already picked image). `match` = raw blended cosine, shown as a % in the UI.

Commons metadata responses are cached in memory for 15 min (`Commons._cache`); images never are.
The frontend uses the View Transitions API (card -> detail morph via `view-transition-name: hero`,
re-rank glides via per-card names) and falls back to plain updates where unsupported.

## Commands (run from this directory)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn backend.main:app --host 127.0.0.1 --port 8001   # loads models (~5 s; downloads them on first run)

cd frontend && npm install && API_TARGET=http://127.0.0.1:8001 npm run dev   # http://localhost:5173
```

## Gotchas

- Wikimedia answers 429 above a few parallel thumbnail fetches: `Commons.image_sem` is 4, with
  backoff. Don't raise it. A descriptive User-Agent is required.
- Embedded Qdrant takes an exclusive lock on `qdrant_db/`: one uvicorn worker only. The old
  `wiki_plants` collection from the earlier plant-only version may still be in there; it is unused.
- Port 8000 is often taken (ComfyUI); the Vite proxy target is `API_TARGET`, default `http://localhost:8000`.
- The backend runs on the macOS system Python 3.9: no `match`, and pydantic models need `Optional[...]`, not `X | None`.
- For a production build, serve `frontend/dist` behind the same origin as the API.
