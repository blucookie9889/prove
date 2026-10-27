# wiki-plant-explorer (Wiki Explorer)

A personal, Pinterest-style explorer over **all of Wikimedia Commons** plus other open collections
(Cleveland Museum of Art CC0 artworks, iNaturalist CC0/CC BY/CC BY-SA nature photos, and Unsplash /
Pexels photography when their free API keys are in `.env`), browsed live
through their public APIs. Click an image to get visually similar ones; "più così / meno così" feedback
refines the ranking, opened and saved images feed a personal "Per te" home, and a background crawler
keeps growing a local vector index. No image is ever stored on disk: only vectors.

## Architecture

```
backend/commons.py   async client for Commons/Wikidata APIs (search, feed, categories, depicts, thumbs in memory)
backend/sources.py   other collections (Cleveland, iNaturalist, Unsplash, Pexels): search, get by id, items in
                     the Commons shape; `Budget` caps requests per hour for the keyed APIs
backend/translator.py  local MarianMT Italian -> English
.env                 UNSPLASH_ACCESS_KEY / PEXELS_API_KEY (git-ignored; template in .env.example)
backend/embedder.py  DINOv2 (visual, 768-d) + OpenCLIP ViT-B-32 (semantic, 512-d), batched, L2-normalised
backend/main.py      FastAPI: /api/foryou, /api/feed, /api/search, /api/file, /api/similar, /api/vote,
                     /api/view, /api/saved, /api/health; background crawler
frontend/            React 19 + Vite 6 + Tailwind 4, white image-first UI; Vite proxies /api to the backend
qdrant_db/           embedded Qdrant: collection `commons_vectors` = vector cache (git-ignored)
state/feedback.json  votes, saved images and opened images ("views") (git-ignored)
state/crawler.json   crawler cursors, done categories, interest categories and queries (git-ignored)
```

- UI language is Italian. Routes are URL params: `?q=`, `?file=<title>&q=`, `?view=saved`, `?view=featured`.
- Item titles: Commons keeps `File:...`; other collections use `cma:<id>`, `inat:<observation id>`,
  `unsplash:<id>`, `pexels:<id>`. `resolve(title)` looks any of them up again (index payload first,
  so keyed APIs aren't charged); every item carries `source`.
- Keyed sources (Unsplash demo apps: 50 requests/hour; Pexels: 200/hour) are registered only when
  their key is set. Each has a `Budget`: background work (crawler, candidate lookups for similar,
  `background=True`) may use 40% of it, so user searches keep working; an exhausted budget raises
  `RateLimited`, which callers skip silently. Responses are cached 15 min, so the CLIP-check
  re-request of a search page costs nothing. Unsplash links carry the referral UTM params it requires.
- Home "Per te" (`/api/foryou`): seeds = saved / "più così" images (counted as 3 days newer) and
  opened images, most recent first (max 30). For each seed, its nearest neighbours in the local index;
  weighted round-robin (newest seeds 3 per round, then 2, then 1), near-duplicates removed. Without
  any seed it falls back to "In evidenza" = newest `Category:Featured pictures on Wikimedia Commons`.
- Search (`/api/search`):
  1. `to_english` translates the query, because Commons titles and CLIP are English and Italian
     words hit surnames and places ("buchi" is a surname, "Nel" a person):
     - Multi-word queries first try `Commons.concept`: an exact Italian label/alias of a Wikidata
       item (also in the singular), for fixed terms like "scale a chiocciola" -> "helical stairs".
       It ignores proper nouns, names and works (`_PROPER`), and ranks matches by sitelinks.
     - Otherwise the local MarianMT model (`backend/translator.py`, Helsinki-NLP/opus-mt-it-en,
       ~0.1 s) translates the whole phrase ("fori nel metallo" -> "holes in metal").
     - Single words skip Wikidata, whose most famous sense is often wrong ("fori" -> forum).
  2. Federated: Commons `generator=search` with `filetype:bitmap`, 24 per page restricted to
     `incategory:Quality_images` (community-reviewed) first, then 24 unrestricted; + 12 from each
     other collection, interleaved 3:1. `offset` is a page index.
  3. Every result is checked with CLIP: image vs. the text embedding of the English query (prompt
     ensemble). Results below `max(0.19, best - 0.13)` are hidden, unless CLIP doesn't understand the
     query (best < 0.22). The rest are sorted by score + beauty (see below), with near-duplicates removed.
  4. The frontend loads the first page with `budget=0`, which is instant: it uses cached vectors and
     leaves the other results unchecked. It then calls `budget=72` and swaps the cleaned page in with a
     View Transition.

## Beauty

Commons is an archive (diagrams, maps, scans, flat snapshots); Pinterest is curated. `aesthetic()`
is a CLIP zero-shot score computed from the stored semantic vector alone: softmax(100·cos) of
"beautiful photograph" prompts against document / low-quality prompts (`AESTH_POS`, `AESTH_NEG`).
Calibrated on contact sheets of ~7000 indexed images: < 0.001 = documents (`JUNK`), up to 0.004 =
flat snapshots and product shots (`PLAIN`), above ~0.02 = good photos.
- Search: `JUNK` always hidden; `PLAIN` hidden too when ≥ 12 better relevant images remain; rank =
  relevance + 0.04·beauty. Skipped when the query is itself about documents (`wants_documents`:
  query text within 0.71 cosine of a negative prompt, e.g. "ancient map", "manuscript").
- Similar: + 0.15·beauty and `JUNK` hidden, unless the origin itself is below `PLAIN`.
- Per te: images below `PLAIN` are dropped.

## Similar images (`/api/similar`)

Nobody publishes visual embeddings for Commons and its built-in `morelike:` is text-only (useless
visually), so similarity is two-stage, like Pinterest:

1. **Candidates** (up to 400): files in the origin's 4 most specific content categories (noise like
   "Featured pictures", "Photographs by", licences, years filtered out; categories with <3 files
   dropped; parent categories added when that yields <60 files), files sharing its structured-data
   "depicts" (P180), the current search query, and the categories of: results marked "più così" and
   the top 4 visual matches of the previous call (second hop, `S["best"]`), and the other collections
   searched for the query and for the origin's most specific category (or, for a non-Commons origin,
   its species / series via `related_query`). Round-robin merged. Then the **local index** adds the
   origin's nearest neighbours (160, split visual/semantic by the slider weight): free to rank,
   and the reason "similar" keeps improving as the index grows.
2. **Visual re-rank**: thumbnails are fetched into memory, embedded, and only the vectors are cached
   in Qdrant (point id = uuid5 of the file title). The origin is always embedded first; then each call
   embeds at most `budget` unseen candidates (frontend: 24 first, then up to 3 rounds of 64), so
   answers get better as the page refines. Calls for the same origin are serialised (`origin_lock`).
3. Score per space = min-max(cosine to query); `final = w*visual + (1-w)*semantic` (default w=0.75).
   Query vector = origin + 0.7*mean(più così) - 0.35*mean(meno così) for votes given on this origin
   (Rocchio). With >=2 liked images overall, a small pull (0.12) towards their mean ("taste").
4. Greedy pick in score order, dropping near-duplicates (DINOv2 cosine > 0.94 to the origin or to an
   already picked image). `match` = raw blended cosine, shown as a % in the UI.

Scrolling the similar list asks for 60 more each time (`limit` grows) and appends, keeping cards in place.

## Background crawler

While no user request touched `ensure_vectors` for 10 s, every 6 s it embeds 12 more images (vectors
only). Turns rotate: Commons categories of what the user opened/saved/liked (`interests`, most
recent most often), other collections searched with recent queries (searches, iNaturalist species,
Cleveland series), and broad pools (Quality images, Featured pictures, Valued images). Cursors are
persisted, so it resumes after a restart. `CRAWL=0` disables it.

API metadata responses are cached in memory for 15 min (`Commons._cache`); images never are.
The frontend uses the View Transitions API (card -> detail morph via `view-transition-name: hero`,
re-rank glides via per-card names) and falls back to plain updates where unsupported.

## Commands (run from this directory)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn backend.main:app --host 127.0.0.1 --port 8001   # loads DINOv2, CLIP, MarianMT (downloads them on first run)

cd frontend && npm install && API_TARGET=http://127.0.0.1:8001 npm run dev   # http://localhost:5173
```

## Gotchas

- Wikimedia answers 429 above a few parallel thumbnail fetches: `Commons.image_sems` gives 4 per
  Wikimedia host (6 for others), with backoff. Don't raise it. A descriptive User-Agent is required.
- Sources checked and left out: Openverse (anonymous API timed out), Art Institute of Chicago (IIIF
  images 403 to non-browser clients), The Met (search is IDs only, one request per object).
- Embedded Qdrant takes an exclusive lock on `qdrant_db/`: one uvicorn worker only. The old
  `wiki_plants` collection from the earlier plant-only version may still be in there; it is unused.
- Port 8000 is often taken (ComfyUI); the Vite proxy target is `API_TARGET`, default `http://localhost:8000`.
- The backend runs on the macOS system Python 3.9: no `match`, and pydantic models need `Optional[...]`, not `X | None`.
- For a production build, serve `frontend/dist` behind the same origin as the API.
