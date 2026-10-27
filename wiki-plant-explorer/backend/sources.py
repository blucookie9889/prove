"""Open image collections besides Wikimedia Commons, returning items in the same shape.

Collections with a public API and freely usable images:
  - Cleveland Museum of Art open access: CC0 artworks (no key).
  - iNaturalist: nature photos, only those licensed CC0, CC BY or CC BY-SA (no key).
  - Unsplash and Pexels: curated, good-looking photography under their own free licences. They
    need a free API key (``UNSPLASH_ACCESS_KEY``, ``PEXELS_API_KEY``) and are skipped without one.
    Their hourly limits are low (Unsplash demo apps: 50 requests/hour), so every request is counted
    by a ``Budget`` and background work (crawler, similar) may only use part of it.

Item titles are "<key>:<id>" (Commons items keep their "File:..." title), so every part of the app
can tell where an item comes from and how to look it up again (``Source.get``).
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Awaitable, Callable, Optional

Getter = Callable[..., Awaitable[dict]]


class RateLimited(Exception):
    """The source's hourly request budget is used up; try again later."""


class Budget:
    """Requests allowed per rolling hour. Background work may use only ``background_share`` of it,
    so the user's own searches always have requests left."""

    def __init__(self, per_hour: int, background_share: float = 0.4):
        self.per_hour = per_hour
        self.background_share = background_share
        self.sent: deque = deque()

    def take(self, background: bool = False) -> None:
        now = time.time()
        while self.sent and now - self.sent[0] > 3600:
            self.sent.popleft()
        limit = self.per_hour * (self.background_share if background else 1.0)
        if len(self.sent) >= limit:
            raise RateLimited(f"{len(self.sent)} requests in the last hour")
        self.sent.append(now)


class Cleveland:
    key = "cma"
    label = "Cleveland Museum of Art"
    API = "https://openaccess-api.clevelandart.org/api/artworks/"

    def __init__(self, get: Getter):
        self._get = get

    def _item(self, a: dict) -> Optional[dict]:
        images = a.get("images") or {}
        web = images.get("web") or {}
        if not web.get("url"):
            return None
        full = images.get("print") or web
        creators = "; ".join(c.get("description") or "" for c in a.get("creators") or [])
        return {
            "title": f"{self.key}:{a['id']}",
            "name": a.get("title") or "Senza titolo",
            "thumb": web["url"],
            "width": int(web.get("width") or 0) or None,
            "height": int(web.get("height") or 0) or None,
            "full": full["url"],
            "full_width": int(full.get("width") or 0) or None,
            "page_url": a.get("url"),
            "author": creators[:120],
            "license": "CC0",
            "source": self.label,
        }

    async def search(self, query: str, offset: int = 0, limit: int = 12,
                     background: bool = False) -> tuple[list[dict], Optional[int]]:
        data = await self._get(self.API, q=query, has_image=1, cc0=1, limit=limit, skip=offset)
        items = [i for i in map(self._item, data.get("data") or []) if i]
        total = (data.get("info") or {}).get("total") or 0
        return items, (offset + limit if offset + limit < total else None)

    async def get(self, ident: str) -> Optional[dict]:
        data = await self._get(self.API + ident)
        return self._item(data.get("data") or {})

    def related_query(self, item: dict) -> str:
        """Other leaves of the same album, other prints of the same series..."""
        return re.split(r"[,(:]", item["name"])[0].strip()


class INaturalist:
    key = "inat"
    label = "iNaturalist"
    API = "https://api.inaturalist.org/v1/observations"
    LICENSES = ("cc0", "cc-by", "cc-by-sa")

    def __init__(self, get: Getter):
        self._get = get

    def _item(self, o: dict) -> Optional[dict]:
        photos = [p for p in o.get("photos") or [] if p.get("license_code") in self.LICENSES and p.get("url")]
        if not photos:
            return None
        photo = photos[0]
        dims = photo.get("original_dimensions") or {}
        taxon = o.get("taxon") or {}
        common, scientific = taxon.get("preferred_common_name"), taxon.get("name")
        name = f"{common} ({scientific})" if common and scientific else common or scientific or "Osservazione"
        return {
            "title": f"{self.key}:{o['id']}",
            "name": name,
            "thumb": photo["url"].replace("/square.", "/medium."),  # 500 px
            "width": dims.get("width"),
            "height": dims.get("height"),
            "full": photo["url"].replace("/square.", "/large."),  # 1024 px
            "full_width": min(dims.get("width") or 1024, 1024),
            "page_url": o.get("uri"),
            "author": (photo.get("attribution") or "")[:120],
            "license": photo["license_code"].upper().replace("-", " "),
            "source": self.label,
        }

    async def search(self, query: str, offset: int = 0, limit: int = 12,
                     background: bool = False) -> tuple[list[dict], Optional[int]]:
        data = await self._get(
            self.API, q=query, photos="true", quality_grade="research", photo_license=",".join(self.LICENSES),
            per_page=limit, page=offset // limit + 1, order_by="votes",
        )
        items = [i for i in map(self._item, data.get("results") or []) if i]
        total = data.get("total_results") or 0
        return items, (offset + limit if offset + limit < min(total, 10000) else None)

    async def get(self, ident: str) -> Optional[dict]:
        data = await self._get(f"{self.API}/{ident}")
        results = data.get("results") or []
        return self._item(results[0]) if results else None

    def related_query(self, item: dict) -> str:
        """The species: other observations of the same organism."""
        m = re.search(r"\(([^)]+)\)$", item["name"])
        return m.group(1) if m else item["name"]


REFERRAL = "utm_source=wiki_explorer&utm_medium=referral"  # Unsplash asks for this on its links


def _sentence(text: Optional[str]) -> str:
    text = (text or "").strip()
    return text[:1].upper() + text[1:] if text else ""


class Unsplash:
    key = "unsplash"
    label = "Unsplash"
    API = "https://api.unsplash.com"

    def __init__(self, get: Getter, access_key: str, per_hour: int = 50):
        self._get = get
        self._headers = {"Authorization": f"Client-ID {access_key}", "Accept-Version": "v1"}
        self.budget = Budget(per_hour - 5)  # a few spare for opening single photos

    def _item(self, ph: dict) -> Optional[dict]:
        urls = ph.get("urls") or {}
        if not urls.get("raw"):
            return None
        user = (ph.get("user") or {}).get("name") or ""
        page = (ph.get("links") or {}).get("html") or ""
        return {
            "title": f"{self.key}:{ph['id']}",
            "name": _sentence(ph.get("alt_description") or ph.get("description")) or f"Foto di {user}",
            "thumb": f"{urls['raw']}&w=600&q=80&fm=jpg&fit=max",
            "width": ph.get("width"),
            "height": ph.get("height"),
            "full": f"{urls['raw']}&w=1600&q=85&fm=jpg&fit=max",
            "full_width": 1600,
            "page_url": f"{page}?{REFERRAL}" if page else None,
            "author": f"{user} su Unsplash" if user else "Unsplash",
            "license": "Unsplash License",
            "source": self.label,
        }

    async def search(self, query: str, offset: int = 0, limit: int = 12,
                     background: bool = False) -> tuple[list[dict], Optional[int]]:
        data = await self._get(
            f"{self.API}/search/photos", _headers=self._headers, _budget=self.budget, _background=background,
            query=query, per_page=limit, page=offset // limit + 1, content_filter="high",
        )
        items = [i for i in map(self._item, data.get("results") or []) if i]
        total = data.get("total") or 0
        return items, (offset + limit if offset + limit < total else None)

    async def get(self, ident: str) -> Optional[dict]:
        return self._item(await self._get(f"{self.API}/photos/{ident}", _headers=self._headers, _budget=self.budget))

    def related_query(self, item: dict) -> str:
        return " ".join(item["name"].split()[:4])


class Pexels:
    key = "pexels"
    label = "Pexels"
    API = "https://api.pexels.com/v1"

    def __init__(self, get: Getter, api_key: str, per_hour: int = 200):
        self._get = get
        self._headers = {"Authorization": api_key}
        self.budget = Budget(per_hour - 10)

    def _item(self, ph: dict) -> Optional[dict]:
        src = ph.get("src") or {}
        if not src.get("large"):
            return None
        user = ph.get("photographer") or ""
        return {
            "title": f"{self.key}:{ph['id']}",
            "name": _sentence(ph.get("alt")) or f"Foto di {user}",
            "thumb": src["large"],  # 940 px wide
            "width": ph.get("width"),
            "height": ph.get("height"),
            "full": src.get("large2x") or src["large"],  # 1880 px wide
            "full_width": 1880,
            "page_url": ph.get("url"),
            "author": f"{user} su Pexels" if user else "Pexels",
            "license": "Pexels License",
            "source": self.label,
        }

    async def search(self, query: str, offset: int = 0, limit: int = 12,
                     background: bool = False) -> tuple[list[dict], Optional[int]]:
        data = await self._get(
            f"{self.API}/search", _headers=self._headers, _budget=self.budget, _background=background,
            query=query, per_page=limit, page=offset // limit + 1,
        )
        items = [i for i in map(self._item, data.get("photos") or []) if i]
        total = data.get("total_results") or 0
        return items, (offset + limit if offset + limit < total else None)

    async def get(self, ident: str) -> Optional[dict]:
        return self._item(await self._get(f"{self.API}/photos/{ident}", _headers=self._headers, _budget=self.budget))

    def related_query(self, item: dict) -> str:
        return " ".join(item["name"].split()[:4])
