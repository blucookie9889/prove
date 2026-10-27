"""Open image collections besides Wikimedia Commons, returning items in the same shape.

Only collections with a public API (no key) and freely licensed images are used:
  - Cleveland Museum of Art open access: CC0 artworks.
  - iNaturalist: nature photos, only those licensed CC0, CC BY or CC BY-SA.

Item titles are "<key>:<id>" (Commons items keep their "File:..." title), so every part of the app
can tell where an item comes from and how to look it up again (``Source.get``).
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable, Optional

Getter = Callable[..., Awaitable[dict]]


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

    async def search(self, query: str, offset: int = 0, limit: int = 12) -> tuple[list[dict], Optional[int]]:
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

    async def search(self, query: str, offset: int = 0, limit: int = 12) -> tuple[list[dict], Optional[int]]:
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
