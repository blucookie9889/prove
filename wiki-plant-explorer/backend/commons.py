"""Thin async client for the public Wikimedia Commons / Wikidata APIs.

Nothing here stores images: thumbnails are only fetched into memory by ``fetch_image`` so they
can be embedded, then dropped.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from html import unescape
from typing import Optional

import httpx
from PIL import Image

log = logging.getLogger("commons")

USER_AGENT = "WikiPlantExplorer/1.0 (https://github.com/blucookie9889/prove; educational demo)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
THUMB_WIDTH = 500
FEED_CATEGORY = "Category:Featured pictures on Wikimedia Commons"
CACHE_TTL_S = 15 * 60

# Categories that group files by uploader, camera, licence or review status say nothing about
# what the image looks like, so they are useless as a source of similar candidates.
_NOISE_CATEGORY = re.compile(
    r"featured pictures|quality images|valued images|pictures of the day|photographs by|"
    r"files by|taken with|uploaded|self-published|cc-by|cc-zero|gfdl|pd-|media needing|"
    r"images from|photos from|scans|wiki loves|supported by|license|location|"
    r"^category:\d{4}\b|\bin \d{4}\b|\d{4} in ",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")


def _text(html: Optional[str]) -> str:
    return unescape(_TAG.sub("", html or "")).strip()


class Commons:
    def __init__(self):
        self.http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30
        )
        self.image_sem = asyncio.Semaphore(4)  # Wikimedia answers 429 above a few parallel fetches
        self._cache: dict = {}  # metadata responses only, kept CACHE_TTL_S; never images

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, url: str, **params) -> dict:
        params.setdefault("format", "json")
        params.setdefault("formatversion", 2)
        key = (url, tuple(sorted(params.items())))
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
        resp = await self.http.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        if len(self._cache) > 5000:
            self._cache.clear()
        self._cache[key] = (time.time(), data)
        return data

    # ----------------------------------------------------------------- items

    @staticmethod
    def _item(page: dict) -> Optional[dict]:
        info = (page.get("imageinfo") or [None])[0]
        if not info or not info.get("thumburl"):
            return None
        meta = info.get("extmetadata") or {}
        return {
            "title": page["title"],
            "name": re.sub(r"\.[a-z0-9]+$", "", page["title"].removeprefix("File:"), flags=re.I),
            "thumb": info["thumburl"],
            "width": info.get("thumbwidth") or info.get("width"),
            "height": info.get("thumbheight") or info.get("height"),
            "full": info["url"],
            "full_width": info.get("width"),
            "page_url": info.get("descriptionurl"),
            "author": _text(meta.get("Artist", {}).get("value"))[:120],
            "license": _text(meta.get("LicenseShortName", {}).get("value")),
        }

    _IMAGEINFO = {
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": THUMB_WIDTH,
        "iiextmetadatafilter": "Artist|LicenseShortName",
    }

    def _items(self, data: dict) -> list[dict]:
        pages = (data.get("query") or {}).get("pages") or []
        pages.sort(key=lambda p: p.get("index", 0))
        out = []
        for page in pages:
            mime = ((page.get("imageinfo") or [{}])[0]).get("mime", "")
            if mime not in ("image/jpeg", "image/png", "image/webp"):
                continue
            item = self._item(page)
            if item:
                out.append(item)
        return out

    async def files(self, titles: list[str]) -> list[dict]:
        out: list[dict] = []
        for start in range(0, len(titles), 50):
            data = await self._get(COMMONS_API, action="query", titles="|".join(titles[start : start + 50]), **self._IMAGEINFO)
            out.extend(self._items(data))
        return out

    # ----------------------------------------------------------------- search & feed

    async def translate(self, query: str) -> str:
        """Map a query in any language to the English label of the matching Wikidata concept.

        Commons file titles are mostly English, so "foglia" alone finds surnames and villages;
        Wikidata knows it means Q33971 "leaf". Only exact label/alias matches are used.
        """
        try:
            data = await self._get(
                WIKIDATA_API, action="wbsearchentities", search=query, language="it",
                uselang="en", type="item", limit=5,
            )
        except httpx.HTTPError:
            return query
        q = query.strip().lower()
        for hit in data.get("search", []):
            match = hit.get("match", {})
            if match.get("type") in ("label", "alias") and match.get("text", "").lower() == q and hit.get("label"):
                return hit["label"]
        return query

    async def search(self, query: str, offset: int = 0, limit: int = 40) -> tuple[list[dict], Optional[int]]:
        data = await self._get(
            COMMONS_API, action="query", generator="search", gsrnamespace=6,
            gsrsearch=f"{query} filetype:bitmap", gsrlimit=limit, gsroffset=offset, **self._IMAGEINFO,
        )
        nxt = (data.get("continue") or {}).get("gsroffset")
        return self._items(data), nxt

    async def category_files(self, category: str, limit: int = 40, cont: Optional[str] = None,
                             newest_first: bool = False) -> tuple[list[dict], Optional[str]]:
        params = dict(
            action="query", generator="categorymembers", gcmtitle=category, gcmtype="file",
            gcmlimit=limit, **self._IMAGEINFO,
        )
        if newest_first:
            params.update(gcmsort="timestamp", gcmdir="desc")
        if cont:
            params["gcmcontinue"] = cont
        data = await self._get(COMMONS_API, **params)
        return self._items(data), (data.get("continue") or {}).get("gcmcontinue")

    async def feed(self, cont: Optional[str] = None, limit: int = 40) -> tuple[list[dict], Optional[str]]:
        return await self.category_files(FEED_CATEGORY, limit=limit, cont=cont, newest_first=True)

    # ----------------------------------------------------------------- candidate sources

    async def categories(self, title: str) -> list[tuple[str, int]]:
        """Visible, content-describing categories of a file with their file counts, smallest first."""
        data = await self._get(COMMONS_API, action="query", titles=title, prop="categories",
                               clshow="!hidden", cllimit=50)
        pages = data["query"]["pages"]
        cats = [c["title"] for c in pages[0].get("categories", [])] if pages else []
        cats = [c for c in cats if not _NOISE_CATEGORY.search(c)]
        if not cats:
            return []
        info = await self._get(COMMONS_API, action="query", titles="|".join(cats[:50]), prop="categoryinfo")
        sized = [(p["title"], (p.get("categoryinfo") or {}).get("files", 0)) for p in info["query"]["pages"]]
        # Tiny categories are often one photographer's series; huge ones are too generic.
        sized = [c for c in sized if c[1] >= 3]
        return sorted(sized, key=lambda c: c[1])

    async def parent_categories(self, category: str) -> list[str]:
        """Visible, content-describing parents of a category (one step up the tree)."""
        data = await self._get(COMMONS_API, action="query", titles=category, prop="categories",
                               clshow="!hidden", cllimit=20)
        pages = data["query"]["pages"]
        cats = [c["title"] for c in pages[0].get("categories", [])] if pages else []
        return [c for c in cats if not _NOISE_CATEGORY.search(c)]

    async def depicts(self, title: str) -> list[str]:
        """Wikidata ids from the file's structured data "depicts" (P180) statements."""
        data = await self._get(COMMONS_API, action="query", titles=title, prop="info")
        pages = data["query"]["pages"]
        if not pages or "pageid" not in pages[0]:
            return []
        mid = f"M{pages[0]['pageid']}"
        ent = await self._get(COMMONS_API, action="wbgetentities", ids=mid, props="claims")
        claims = (ent.get("entities", {}).get(mid, {}) or {}).get("statements") or \
                 (ent.get("entities", {}).get(mid, {}) or {}).get("claims") or {}
        if not isinstance(claims, dict):
            return []
        out = []
        for st in claims.get("P180", []):
            val = (st.get("mainsnak", {}).get("datavalue") or {}).get("value") or {}
            if val.get("id"):
                out.append(val["id"])
        return out

    # ----------------------------------------------------------------- images (memory only)

    async def fetch_image(self, url: str) -> Image.Image:
        async with self.image_sem:
            for attempt in range(4):
                resp = await self.http.get(url)
                if resp.status_code == 429:
                    wait = float(resp.headers.get("retry-after") or 2 ** attempt)
                    await asyncio.sleep(min(wait, 8))
                    continue
                resp.raise_for_status()
                return Image.open(io.BytesIO(resp.content)).convert("RGB")
        raise RuntimeError(f"rate limited: {url}")
