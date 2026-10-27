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
# Wikidata descriptions of things that are names rather than what an image can show.
_PROPER = re.compile(
    r"family name|given name|surname|\bfilm\b|album|\bsong\b|\bsingle\b|\bband\b|musician|singer|actor|"
    r"actress|politician|footballer|company|character|wikimedia|disambiguation|scientific article|"
    r"\b(?:by|from) [A-Z]",  # "painting by Armando Spadini", "single by Zucchero": a work, not a thing
)


def _singular(word: str) -> list[str]:
    """Italian singulars ``word`` may be the plural of, most likely first (no dictionary needed)."""
    for plural, singulars in (("chi", ("co", "chio")), ("ghi", ("go", "ghio")), ("che", ("ca",)), ("ghe", ("ga",)),
                              ("ci", ("cio", "co")), ("gi", ("gio", "go")), ("ce", ("cia", "ca")), ("ge", ("gia", "ga")),
                              ("i", ("o", "e", "a")), ("e", ("a", "o")), ("a", ("o",))):
        if word.endswith(plural) and len(word) - len(plural) >= 2:
            return [word[: -len(plural)] + s for s in singulars]
    return []


def _singulars(text: str) -> list[str]:
    """The text itself, then versions with its words put in the singular ("foglie rosse" -> "foglia rossa")."""
    words = text.split()
    if len(words) == 1 and text.endswith("a"):
        return [text]  # "foglia" is already singular; "foglio" would be another word
    options = [[w] + _singular(w) for w in words]
    forms = [text]
    for depth in range(3):  # first, second and third guess for every word that has one
        forms.append(" ".join(o[min(depth, len(o) - 1)] for o in options))
    if len(words) > 1:  # also only the head noun in the singular ("scale a chiocciola")
        forms += [" ".join([s] + words[1:]) for s in options[0][1:]]
    return list(dict.fromkeys(forms))[:5]


def _text(html: Optional[str]) -> str:
    return unescape(_TAG.sub("", html or "")).strip()


class Commons:
    def __init__(self):
        self.http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30
        )
        # Per image host: Wikimedia answers 429 above a few parallel fetches; other hosts get their own.
        self.image_sems: dict = {}
        self._cache: dict = {}  # metadata responses only, kept CACHE_TTL_S; never images

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, url: str, _headers: Optional[dict] = None, _budget=None, _background: bool = False,
                   **params) -> dict:
        """Cached GET of a JSON API (MediaWiki ones get format=json; also used by ``sources``).

        ``_budget`` (a ``sources.Budget``) is only charged on a cache miss; it raises
        ``sources.RateLimited`` instead of sending a request the API would refuse.
        """
        if url.endswith("api.php"):
            params.setdefault("format", "json")
            params.setdefault("formatversion", 2)
        key = (url, tuple(sorted(params.items())))
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
        if _budget is not None:
            _budget.take(_background)
        resp = await self.http.get(url, params=params, headers=_headers)
        if resp.status_code == 429:  # back off once, politely
            await asyncio.sleep(min(float(resp.headers.get("retry-after") or 2), 5))
            resp = await self.http.get(url, params=params, headers=_headers)
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
            "source": "Wikimedia Commons",
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

    async def concept(self, query: str) -> tuple[Optional[str], Optional[str]]:
        """English label and id of the Wikidata concept whose Italian name is exactly ``query``.

        Used for fixed terms a word-by-word translator gets wrong ("scale a chiocciola" is
        "helical stairs", not "snail stairs"). The words are also tried in the singular. Proper nouns
        (a capitalised match for a lower-case query), names and works are ignored; among the rest
        the concept with most Wikipedia articles wins, preferring labels over aliases and the form
        as typed. Returns (None, None) when nothing fits.
        """
        q = query.strip()
        hits: dict[str, dict] = {}
        for rank, form in enumerate(_singulars(q.lower())):
            try:
                data = await self._get(
                    WIKIDATA_API, action="wbsearchentities", search=form, language="it",
                    uselang="en", type="item", limit=10,
                )
            except httpx.HTTPError as exc:
                log.warning("wikidata lookup failed: %s", exc)
                break
            for hit in data.get("search", []):
                match = hit.get("match", {})
                text = match.get("text", "")
                if match.get("type") not in ("label", "alias") or match.get("language") != "it":
                    continue
                if text.lower() != form or not hit.get("label") or _PROPER.search(hit.get("description") or ""):
                    continue
                if q == q.lower() and text != text.lower():
                    continue
                weight = (1.0 if match["type"] == "label" else 0.3) * 0.6 ** rank
                hits.setdefault(hit["id"], {**hit, "weight": weight})
        if not hits:
            return None, None
        if len(hits) > 1:
            try:
                ent = await self._get(WIKIDATA_API, action="wbgetentities", ids="|".join(list(hits)[:50]), props="sitelinks")
                for qid, h in hits.items():
                    h["weight"] *= 1 + len(((ent.get("entities") or {}).get(qid) or {}).get("sitelinks") or {})
            except httpx.HTTPError:
                pass
        best = max(hits.values(), key=lambda h: h["weight"])
        return best["label"], best["id"]

    async def search(self, query: str, offset: int = 0, limit: int = 40,
                     curated: bool = False) -> tuple[list[dict], Optional[int]]:
        """Full-text file search. ``curated`` keeps only community-reviewed "Quality images"."""
        extra = " incategory:Quality_images" if curated else ""
        data = await self._get(
            COMMONS_API, action="query", generator="search", gsrnamespace=6,
            gsrsearch=f"{query} filetype:bitmap{extra}", gsrlimit=limit, gsroffset=offset, **self._IMAGEINFO,
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
        host = httpx.URL(url).host
        sem = self.image_sems.setdefault(host, asyncio.Semaphore(4 if "wikimedia" in host else 6))
        async with sem:
            for attempt in range(4):
                resp = await self.http.get(url)
                if resp.status_code == 429:
                    wait = float(resp.headers.get("retry-after") or 2 ** attempt)
                    await asyncio.sleep(min(wait, 8))
                    continue
                resp.raise_for_status()
                return Image.open(io.BytesIO(resp.content)).convert("RGB")
        raise RuntimeError(f"rate limited: {url}")
