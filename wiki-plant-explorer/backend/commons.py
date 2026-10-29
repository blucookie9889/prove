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
from urllib.parse import quote

import httpx
from PIL import Image

log = logging.getLogger("commons")

USER_AGENT = "WikiPlantExplorer/1.0 (https://github.com/blucookie9889/prove; educational demo)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
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
        self.api_sems: dict = {}  # per API host: Wikimedia answers 429 to bursts of requests

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
        sem = self.api_sems.setdefault(httpx.URL(url).host, asyncio.Semaphore(4))
        async with sem:
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

    async def categories_of(self, titles: list[str]) -> dict[str, list[str]]:
        """Visible, content-describing categories of many files at once (no sizes, unlike ``categories``)."""
        out: dict[str, list[str]] = {t: [] for t in titles}

        async def batch(chunk: list[str]) -> None:
            params = dict(action="query", titles="|".join(chunk), prop="categories", clshow="!hidden", cllimit="max")
            for _ in range(5):  # cllimit counts across all pages: follow the continuation
                data = await self._get(COMMONS_API, **params)
                for page in (data.get("query") or {}).get("pages") or []:
                    out.setdefault(page["title"], []).extend(
                        c["title"] for c in page.get("categories", []) if not _NOISE_CATEGORY.search(c["title"]))
                cont = data.get("continue")
                if not cont:
                    break
                params.update(cont)

        await asyncio.gather(*(batch(titles[i : i + 50]) for i in range(0, len(titles), 50)))
        return out

    async def subcategories(self, category: str, limit: int = 20) -> list[str]:
        data = await self._get(COMMONS_API, action="query", list="categorymembers", cmtitle=category,
                               cmtype="subcat", cmlimit=limit)
        cats = [c["title"] for c in (data.get("query") or {}).get("categorymembers", [])]
        return [c for c in cats if not _NOISE_CATEGORY.search(c)]

    async def article(self, category: str) -> dict:
        """The encyclopedia article about what a Commons category shows: {name, qid, lang, title}.

        A category is linked to a Wikidata item, either the topic itself or a "Wikimedia category"
        item pointing to it (P301, category's main topic). The topic's Italian Wikipedia article is
        preferred, then the English one. Only {name} when there is none.
        """
        return {"name": category.removeprefix("Category:"), **(await self.articles([category]))[category]}

    async def articles(self, categories: list[str]) -> dict[str, dict]:
        """``article`` for many categories in a few batched requests: {category: {name, qid, lang,
        title}}; categories without an article map to {}."""
        out: dict[str, dict] = {c: {} for c in categories}
        qid_of: dict[str, str] = {}
        pages = await asyncio.gather(*(
            self._get(COMMONS_API, action="query", titles="|".join(categories[i : i + 50]), prop="pageprops",
                      ppprop="wikibase_item") for i in range(0, len(categories), 50)))
        for data in pages:
            for page in (data.get("query") or {}).get("pages") or []:
                q = (page.get("pageprops") or {}).get("wikibase_item")
                if q:
                    qid_of[page["title"]] = q
        ents = await self._entities(list(set(qid_of.values())), "sitelinks|labels|descriptions")
        # Items that are themselves categories point to their topic (P301): one more batch.
        cat_items = [q for q, e in ents.items() if self._is_category_item(e)]
        topic: dict[str, str] = {}
        for q, e in (await self._entities(cat_items, "claims")).items():
            for c in (e.get("claims") or {}).get("P301", []):
                t = (((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("id")
                if t:
                    topic[q] = t
                    break
        topics = await self._entities(list(set(topic.values())), "sitelinks|labels|descriptions")
        for cat, q in qid_of.items():
            e = topics.get(topic[q]) if q in topic else ents.get(q)
            if not e or self._is_category_item(e):
                continue
            links = e.get("sitelinks") or {}
            for lang in ("it", "en"):
                if f"{lang}wiki" in links:
                    labels, descs = e.get("labels") or {}, e.get("descriptions") or {}
                    out[cat] = {"qid": e.get("id"), "lang": lang, "title": links[f"{lang}wiki"]["title"],
                                "name": (labels.get("it") or labels.get("en") or {}).get("value") or links[f"{lang}wiki"]["title"],
                                "description": " / ".join(d["value"] for d in (descs.get("en"), descs.get("it")) if d)}
                    break
        return out

    async def _entities(self, ids: list[str], props: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for data in await asyncio.gather(*(
                self._get(WIKIDATA_API, action="wbgetentities", ids="|".join(ids[i : i + 50]), props=props,
                          languages="it|en|mul")
                for i in range(0, len(ids), 50))):
            out.update({k: v for k, v in (data.get("entities") or {}).items() if "missing" not in v})
        return out

    @staticmethod
    def _is_category_item(e: dict) -> bool:
        """A "Wikimedia category" item: its Wikipedia links (if any) are category pages, not articles."""
        if "claims" in e:
            return any((((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("id") == "Q4167836"
                       for c in e["claims"].get("P31", []))
        labels = e.get("labels") or {}
        if re.match(r"^(Category|Categoria):", (labels.get("en") or labels.get("it") or {}).get("value", "")):
            return True
        links = e.get("sitelinks") or {}
        for site in ("itwiki", "enwiki"):
            if site in links:
                return bool(re.match(r"^(Category|Categoria):", links[site]["title"]))
        return True  # no Italian or English article: nothing to read, look for its topic instead

    async def depicts_of(self, titles: list[str]) -> dict[str, list[str]]:
        """Wikidata ids each file "depicts" (P180 in its structured data), for many files at once."""
        mid_of: dict[str, str] = {}
        for data in await asyncio.gather(*(
                self._get(COMMONS_API, action="query", titles="|".join(titles[i : i + 50]), prop="info")
                for i in range(0, len(titles), 50))):
            for page in (data.get("query") or {}).get("pages") or []:
                if "pageid" in page:
                    mid_of[f"M{page['pageid']}"] = page["title"]
        out: dict[str, list[str]] = {t: [] for t in titles}
        mids = list(mid_of)
        for data in await asyncio.gather(*(
                self._get(COMMONS_API, action="wbgetentities", ids="|".join(mids[i : i + 50]), props="claims")
                for i in range(0, len(mids), 50))):
            for mid, e in (data.get("entities") or {}).items():
                st = e.get("statements") or e.get("claims") or {}
                if isinstance(st, dict) and mid in mid_of:
                    out[mid_of[mid]] = [q for q in (
                        (((s.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}).get("id")
                        for s in st.get("P180", [])) if q]
        return out

    async def concepts(self, qids: list[str], claims: bool = False) -> dict[str, dict]:
        """{qid: {name (Italian, else English), en (English label), description, lang, title (the
        Wikipedia article, Italian first), category (its Commons category, with ``claims``)}}."""
        props = "sitelinks|labels|descriptions" + ("|claims" if claims else "")
        out = {}
        for q, e in (await self._entities(qids, props)).items():
            labels, descs, links = e.get("labels") or {}, e.get("descriptions") or {}, e.get("sitelinks") or {}
            label = labels.get("it") or labels.get("en") or labels.get("mul")  # "mul": scientific names
            c = {"qid": q, "name": (label or {}).get("value") or q,
                 "en": (labels.get("en") or labels.get("mul") or labels.get("it") or {}).get("value") or "",
                 "description": " / ".join(d["value"] for d in (descs.get("en"), descs.get("it")) if d),
                 "category_item": self._is_category_item({k: v for k, v in e.items() if k != "claims"})}
            for lang in ("it", "en"):
                if f"{lang}wiki" in links:
                    c["lang"], c["title"] = lang, links[f"{lang}wiki"]["title"]
                    break
            if claims:
                for s in (e.get("claims") or {}).get("P373", []):
                    v = ((s.get("mainsnak") or {}).get("datavalue") or {}).get("value")
                    if v:
                        c["category"] = f"Category:{v}"
                        break
            out[q] = c
        return out

    async def individuals(self, qids: list[str]) -> set[str]:
        """The ids that are one particular thing rather than a kind of thing: people (P31 human),
        anything with coordinates (places, buildings, monuments) and works with a creator or author."""
        if not qids:
            return set()
        values = " ".join(f"wd:{q}" for q in qids)
        query = (f"SELECT DISTINCT ?i WHERE {{ VALUES ?i {{ {values} }} "
                 "{ ?i wdt:P31 wd:Q5 } UNION { ?i wdt:P625 [] } UNION { ?i wdt:P170 [] } UNION { ?i wdt:P50 [] } }")
        data = await self._get(WIKIDATA_SPARQL, query=query, format="json")
        return {b["i"]["value"].rsplit("/", 1)[-1] for b in (data.get("results") or {}).get("bindings", [])}

    async def summary(self, lang: str, title: str) -> dict:
        """Wikipedia's page summary: {extract, thumbnail, url}."""
        path = quote(title.replace(" ", "_"), safe="")
        data = await self._get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{path}")
        return {
            "extract": data.get("extract") or "",
            "image": (data.get("originalimage") or data.get("thumbnail") or {}).get("source"),
            "url": ((data.get("content_urls") or {}).get("desktop") or {}).get("page"),
        }

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
