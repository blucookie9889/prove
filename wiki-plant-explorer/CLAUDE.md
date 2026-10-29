# wiki-plant-explorer (Wiki Explorer)

A personal, visual "Wikipedia rabbit hole" over **all of Wikimedia Commons** plus other open collections
(Cleveland Museum of Art CC0 artworks, iNaturalist CC0/CC BY/CC BY-SA nature photos, and Unsplash /
Pexels photography when their free API keys are in `.env`), browsed live
through their public APIs. Click an image to get visually similar ones; "più così / meno così" feedback
refines the ranking, opened and saved images feed a personal "Per te" home, and a background crawler
keeps growing a local vector index. No image is ever stored on disk: only vectors.

The core is the flow (`?flow=<title>`): a continuous stream of stimuli. A curator (Claude, when
`ANTHROPIC_API_KEY` is set) looks at each picture and opens doors from details in it to concepts
worth knowing (the mechanism, the cause, the same principle far away, the human story, another
scale); each door is grounded in its Wikipedia article and shown with the best picture of it the
person has not seen yet. Scrolling takes the door farthest from where they have been; clicking one
takes that one. No trail: only what comes next. Quality of knowledge and pictures comes first.

## Architecture

```
backend/commons.py   async client for Commons/Wikidata APIs (search, feed, categories, depicts, thumbs in memory)
backend/sources.py   other collections (Cleveland, iNaturalist, Unsplash, Pexels): search, get by id, items in
                     the Commons shape; `Budget` caps requests per hour for the keyed APIs
backend/translator.py  local MarianMT Italian -> English
.env                 UNSPLASH_ACCESS_KEY / PEXELS_API_KEY (git-ignored; template in .env.example)
backend/curator.py   Claude (claude-opus-5-5) sees a picture, returns doors as structured JSON
backend/embedder.py  DINOv2 (visual, 768-d) + OpenCLIP ViT-B-32 (semantic, 512-d), batched, L2-normalised;
                     `regions`: an image's parts from DINOv2 patch tokens (k-means + connected blobs)
backend/main.py      FastAPI: /api/flow, /api/foryou, /api/feed, /api/search, /api/file, /api/page, /api/concepts,
                     /api/concept, /api/similar, /api/vote,
                     /api/view, /api/saved, /api/health; background crawler
frontend/            React 19 + Vite 6 + Tailwind 4, white image-first UI; Vite proxies /api to the backend
qdrant_db/           embedded Qdrant: collection `commons_vectors` = vector cache (git-ignored). A `dino_dense`
                     collection left by a removed experiment may be in there; it is unused
state/feedback.json  votes, saved images and opened images ("views") (git-ignored)
state/crawler.json   crawler cursors, done categories, interest categories and queries (git-ignored)
```

- UI language is Italian; look and motion follow the portfolio art direction (warm paper, Geist 15px,
  two tones, hairlines, slow eased motion). Routes are URL params: `?q=`, `?flow=<title>` (`?file=`
  opens the flow too), `?view=saved`, `?view=featured`.
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

## Image page (`/api/page`): parts of an image as links

1. The origin is fetched at 1280 px (`large_url`, into memory) and `Embedder.regions` reads it at
   448 px in its own aspect ratio (up to 32x32 DINOv2 patch tokens). k-means (10) on the last-layer
   tokens gives kinds of things; the connected blobs of each kind are the things.
2. `link_regions` keeps blobs between 1.2% and 50% of the image that don't wrap around it (background),
   and gives each a crop box (padded, at least 18% of each side) and a hotspot on the blob itself.
   Crops that are flat or black (grey std < 14, mean < 10) are dropped.
3. Each crop is embedded (DINOv2 + CLIP) like a whole image. Crops that look like the whole image add
   nothing; two crops alike (DINOv2 > 0.8 or CLIP > 0.92: two eyes, two strips of sky) are one link.
4. `rank_links`: for each crop, its 200 nearest index images by look and by subject; score =
   ½ look + ½ subject + beauty − 0.5 · CLIP similarity to the whole origin (`LINK_ESCAPE`: a link
   leads elsewhere, the door in a painting to doors, not to the same painting). No documents, game
   textures / swatches (CLIP near "blank" prompts > 0.27), strips (aspect > 3.5), near-duplicates.
   Links take their 10 targets in order of strength and never share one, so each opens another door.
5. Response: `linkmap` (link index per patch, 255 = none, base64 bytes) for hover highlighting, and
   per link its box, hotspot and targets. Pages are cached in memory (32). ~2-4 s the first time.

Frontend (`PageHero`, `Threads`, `Trail` in App.jsx): dots on the links; hover darkens the rest and
pops a preview of where it leads; click → the crop, drawn over itself, is named `hero` and the View
Transition grows it into the next image ("dive"). On touch, a first tap previews and a second follows.
"In questa immagine" lists every link: the crop, then its targets. The trail at the top is the way
here, with the crop that led to each step (stored in sessionStorage with the step).

## Flow (`POST /api/flow {title, via}`)

1. Curator (`curator.py`): the picture (1024 px JPEG, in memory) + the concept that led here + the
   concepts already met (`flow_memory()["seen"]`, never lead back). Claude Opus 5.5, effort medium,
   structured output (`SCHEMA`): `seeing` + 5 doors {anchor, box, leap ∈ dentro/perché/altrove/
   storia/scala, title, wikipedia_it/en, hook, image_query}. `fallbacks: "default"` (beta
   `server-side-fallback-2026-07-01`) re-runs a declined request on Anthropic's fallback model.
2. `ground`: a door is kept only if its Wikipedia article exists (Italian first, not a
   disambiguation); the extract shown is Wikipedia's, the hook is the curator's one line.
3. `door_pictures`: the article's lead image + Commons "Quality images" search + all Commons (+
   Unsplash/Pexels with keys) for image_query, then the English title, then a shorter query, until 8
   candidates. Embedded, kept if CLIP relevance ≥ best − 0.05 and beauty ≥ PLAIN, not like a picture
   already shown on the journey (DINOv2 > 0.85, last 200) nor taken by another door; ranked by
   relevance + beauty + 0.02 for curated.
4. `farthest`: the door scrolling takes = lowest CLIP text similarity to the concepts met, +0.05 for
   altrove/scala. The frontend prefetches it.
Without a key the doors are `/api/concepts`' Wikimedia concepts of the image's DINOv2 parts.
Cost with a key: one Opus call per picture (~3k tokens in, ~3k out with thinking).

## Concepts: what a detail is (`/api/concepts`, `/api/concept`)

`/api/concepts?title=` names each link of a page (same order as its links), from free Wikimedia data
only, in a few batched requests (Commons answers 429 to bursts; `_get` allows 4 at a time per host):
1. For every link image: what its structured data says it depicts (P180, `depicts_of`, weight 1.5)
   and the Wikidata topics of its Commons categories (`categories_of` + `articles`, weight 1), minus
   archive facets (`ARCHIVE_NAME`: "photographs of", "IUCN", "by color", months…). Images count
   0.85^rank; what the origin itself depicts / is filed under adds 0.3 to every link.
2. Kept: items with an Italian or English Wikipedia article that are kinds of things — not category
   items, not places / people / works (`PLACE_OR_WORK` on descriptions, then one SPARQL query,
   `individuals`: P31 human, coordinates P625, creator P170, author P50). A town that looks like this
   one is never this one.
3. Score = CLIP cosine of the crop with "a photo of <English label>" + 0.015 · votes (≤ 6); each
   link gets the best concept not already used. Labels fall back to Wikidata's "mul" (taxa).
4. `summarize`: the article's REST summary (extract, image, url), cached by Wikidata id.
The frontend shows the dots as soon as `/api/page` answers, then the names (`/api/concepts`, ~5 s).

`/api/concept?qid=&offset=` is a voce: the summary, the Commons files that depict it
(`haswbstatement:P180=Q…`, 40 per page) plus its Commons category (P373) when few, and "voci
collegate" = the explainable concepts most often depicted together with it in those files.

Quality depends on how people labelled Commons: good on common things (leaf, Ferris wheel, horizon,
cliff, armchair), weaker on fine details. A vision-language model could name and explain details
far better, but would need an API key.

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
