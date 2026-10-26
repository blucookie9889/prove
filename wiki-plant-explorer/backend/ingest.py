"""Populate the local Qdrant database with plant species from Wikidata.

For every species we store two named vectors:
  - ``visual``   : DINOv2 (facebook/dinov2-base) image embedding, 768-d. Captures shape/texture/style.
  - ``semantic`` : OpenCLIP ViT-B-32 (laion2b_s34b_b79k) image embedding, 512-d. Captures concept/taxonomy.

Usage (from the project root):
    python backend/ingest.py
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from urllib.parse import unquote

import httpx
import open_clip
import torch
from PIL import Image
from qdrant_client import QdrantClient, models
from transformers import AutoImageProcessor, AutoModel

DB_PATH = Path(__file__).resolve().parent.parent / "qdrant_db"
COLLECTION = "wiki_plants"
TARGET_SPECIES = 300
THUMB_WIDTH = 500
DOWNLOAD_CONCURRENCY = 4
UPSERT_BATCH = 16

USER_AGENT = "WikiPlantExplorer/1.0 (https://github.com/blucookie9889/prove; educational demo)"
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# A transitive walk of the whole plant tree (wdt:P171+ wd:Q756) times out on the public
# endpoint, so we anchor on a diverse set of plant families (all descendants of Plantae, Q756)
# and take an even share of species from each. Families are matched by scientific name (P225),
# which is indexed and fast.
FAMILIES = [
    "Rosaceae", "Orchidaceae", "Asteraceae", "Fabaceae", "Lamiaceae",
    "Cactaceae", "Ranunculaceae", "Solanaceae", "Pinaceae", "Liliaceae",
    "Ericaceae", "Arecaceae", "Brassicaceae", "Apiaceae", "Iridaceae",
]
PER_FAMILY = -(-TARGET_SPECIES // len(FAMILIES))  # ceil division

SPARQL_TEMPLATE = """
SELECT ?item ?itemLabel ?taxon ?image ?familyLabel WHERE {{
  ?family wdt:P225 "{family}" ; wdt:P105 wd:Q35409 ; wdt:P171+ wd:Q756 .
  ?genus wdt:P171 ?family .
  ?item wdt:P171 ?genus ;
        wdt:P105 wd:Q7432 ;
        wdt:P225 ?taxon ;
        wdt:P18 ?image .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT {limit}
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ingest")


# --------------------------------------------------------------------------- device & models


def detect_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class Embedder:
    def __init__(self, device: str):
        self.device = device
        log.info("Loading DINOv2 (facebook/dinov2-base) on %s", device)
        self.dino_processor = AutoImageProcessor.from_pretrained("facebook/dinov2-base")
        self.dino = AutoModel.from_pretrained("facebook/dinov2-base").to(device).eval()

        log.info("Loading OpenCLIP ViT-B-32 (laion2b_s34b_b79k) on %s", device)
        self.clip, _, self.clip_preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k"
        )
        self.clip = self.clip.to(device).eval()

    @torch.inference_mode()
    def embed(self, image: Image.Image) -> tuple[list[float], list[float]]:
        dino_inputs = self.dino_processor(images=image, return_tensors="pt").to(self.device)
        visual = self.dino(**dino_inputs).pooler_output  # layer-normed CLS token, (1, 768)
        visual = torch.nn.functional.normalize(visual, dim=-1)

        clip_input = self.clip_preprocess(image).unsqueeze(0).to(self.device)
        semantic = self.clip.encode_image(clip_input)  # (1, 512)
        semantic = torch.nn.functional.normalize(semantic, dim=-1)

        return visual[0].float().cpu().tolist(), semantic[0].float().cpu().tolist()


# --------------------------------------------------------------------------- qdrant


def init_collection(client: QdrantClient) -> None:
    if client.collection_exists(COLLECTION):
        log.info("Dropping existing collection %s", COLLECTION)
        client.delete_collection(COLLECTION)
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={
            "visual": models.VectorParams(size=768, distance=models.Distance.COSINE),
            "semantic": models.VectorParams(size=512, distance=models.Distance.COSINE),
        },
    )
    log.info("Created collection %s at %s", COLLECTION, DB_PATH)


# --------------------------------------------------------------------------- wikidata / commons


async def fetch_species(http: httpx.AsyncClient) -> list[dict]:
    species: dict[str, dict] = {}
    for family in FAMILIES:
        query = SPARQL_TEMPLATE.format(family=family, limit=PER_FAMILY)
        try:
            resp = await http.get(
                SPARQL_ENDPOINT,
                params={"query": query, "format": "json"},
                headers={"Accept": "application/sparql-results+json"},
                timeout=90,
            )
            resp.raise_for_status()
            rows = resp.json()["results"]["bindings"]
        except Exception as exc:  # noqa: BLE001 - keep going with the other families
            log.warning("SPARQL query for %s failed: %s", family, exc)
            continue

        for row in rows:
            uri = row["item"]["value"]
            if uri in species:  # multiple P18 images -> keep the first
                continue
            wiki_id = uri.rsplit("/", 1)[-1]
            species[uri] = {
                "wiki_id": wiki_id,
                "title": row.get("itemLabel", {}).get("value", wiki_id),
                "taxon": row["taxon"]["value"],
                "family": family,
                "file_name": unquote(row["image"]["value"].rsplit("/", 1)[-1]),
                "wiki_url": f"https://www.wikidata.org/wiki/{wiki_id}",
            }
        log.info("%-14s -> %3d species (total %d)", family, len(rows), len(species))

    return list(species.values())[:TARGET_SPECIES]


async def resolve_thumbnails(http: httpx.AsyncClient, items: list[dict]) -> None:
    """Fill ``image_url`` with a 500px thumbnail URL using the Commons imageinfo API."""
    for start in range(0, len(items), 50):
        chunk = items[start : start + 50]
        titles = {f"File:{it['file_name']}": it for it in chunk}
        try:
            resp = await http.get(
                COMMONS_API,
                params={
                    "action": "query",
                    "titles": "|".join(titles),
                    "prop": "imageinfo",
                    "iiprop": "url",
                    "iiurlwidth": THUMB_WIDTH,
                    "format": "json",
                    "formatversion": 2,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()["query"]
        except Exception as exc:  # noqa: BLE001
            log.warning("imageinfo batch %d failed: %s", start // 50, exc)
            continue

        # The API normalizes titles (underscores, capitalisation); map them back.
        normalized = {n["to"]: n["from"] for n in data.get("normalized", [])}
        for page in data.get("pages", []):
            info = page.get("imageinfo")
            original = normalized.get(page["title"], page["title"])
            item = titles.get(original)
            if item and info:
                item["image_url"] = info[0].get("thumburl") or info[0]["url"]


async def download_image(http: httpx.AsyncClient, url: str, sem: asyncio.Semaphore) -> Image.Image:
    async with sem:
        for attempt in range(4):
            resp = await http.get(url, timeout=30)
            if resp.status_code == 429:
                await asyncio.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return Image.open(io.BytesIO(resp.content)).convert("RGB")
        raise RuntimeError(f"rate limited after retries: {url}")


# --------------------------------------------------------------------------- main


async def main() -> None:
    device = detect_device()
    log.info("Using device: %s", device)
    embedder = Embedder(device)

    client = QdrantClient(path=str(DB_PATH))
    init_collection(client)

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as http:
        items = await fetch_species(http)
        log.info("Fetched %d species from Wikidata", len(items))
        await resolve_thumbnails(http, items)
        items = [it for it in items if it.get("image_url")]
        log.info("%d species have a resolvable thumbnail", len(items))

        sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
        downloads = [asyncio.create_task(download_image(http, it["image_url"], sem)) for it in items]

        next_id = 0
        batch: list[models.PointStruct] = []
        failures = 0
        for item, task in zip(items, downloads):
            try:
                image = await task
                visual, semantic = await asyncio.to_thread(embedder.embed, image)
            except Exception as exc:  # noqa: BLE001 - one bad image must not stop the run
                failures += 1
                log.warning("Skipping %s (%s): %s", item["taxon"], item["wiki_id"], exc)
                continue

            payload = {k: item[k] for k in ("wiki_id", "title", "taxon", "family", "image_url", "wiki_url")}
            batch.append(
                models.PointStruct(
                    id=next_id,  # contiguous ids so /api/feed offsets map 1:1 to positions
                    vector={"visual": visual, "semantic": semantic},
                    payload=payload,
                )
            )
            next_id += 1

            if len(batch) >= UPSERT_BATCH:
                client.upsert(COLLECTION, points=batch)
                log.info("Upserted %d points", next_id)
                batch = []

        if batch:
            client.upsert(COLLECTION, points=batch)

    log.info("Done: %d points stored, %d failures", next_id, failures)
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
