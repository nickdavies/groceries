"""Kroger Public API: store lookup and product search, using client credentials.

Cart writes need a shopper OAuth token and come later. Search and locations only need the
app's own token (scope product.compact), which lasts 30 minutes and is cached here.
"""

import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

API = "https://api.kroger.com/v1"
# Kroger banners share product paths, so the API's relative URI works on the banner site.
SITE = "https://www.kingsoopers.com"


@dataclass(frozen=True)
class Product:
    upc: str
    description: str
    brand: str
    size: str
    price: float | None
    promo: float | None
    url: str
    image: str | None

    @property
    def label(self) -> str:
        return f"{self.description} ({self.size})" if self.size else self.description


@dataclass(frozen=True)
class Location:
    id: str
    name: str
    address: str


class Kroger:
    def __init__(self, client_id: str, client_secret: str):
        self._auth = (client_id, client_secret)
        self._http = httpx.AsyncClient(base_url=API, timeout=30)
        self._token: str | None = None
        self._token_expires = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    async def _headers(self) -> dict[str, str]:
        if self._token is None or time.monotonic() > self._token_expires:
            r = await self._http.post(
                "/connect/oauth2/token",
                auth=self._auth,
                data={"grant_type": "client_credentials", "scope": "product.compact"},
            )
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            self._token_expires = time.monotonic() + body["expires_in"] - 60
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    async def search(self, term: str, location_id: str, limit: int = 20) -> list[Product]:
        """Products matching `term`, with prices and availability at `location_id`."""
        r = await self._http.get(
            "/products",
            headers=await self._headers(),
            params={"filter.term": term, "filter.locationId": location_id, "filter.limit": limit},
        )
        r.raise_for_status()
        return [_product(p) for p in r.json()["data"]]

    async def locations(self, zip_code: str, limit: int = 10) -> list[Location]:
        """King Soopers stores near a ZIP code, nearest first."""
        r = await self._http.get(
            "/locations",
            headers=await self._headers(),
            params={
                "filter.zipCode.near": zip_code,
                "filter.chain": "KINGSOOPERS",
                "filter.limit": limit,
            },
        )
        r.raise_for_status()
        return [_location(x) for x in r.json()["data"]]


def _location(x: dict) -> Location:
    a = x.get("address") or {}
    address = ", ".join(
        p for p in (a.get("addressLine1"), a.get("city"), a.get("state"), a.get("zipCode")) if p
    )
    return Location(id=x["locationId"], name=x.get("name") or x["locationId"], address=address)


def _product(p: dict) -> Product:
    item = (p.get("items") or [{}])[0]
    price = item.get("price") or {}
    # Drop the query string: it is a tracking tag that names our API app.
    path = urlsplit(p.get("productPageURI") or f"/p/item/{p['upc']}").path
    return Product(
        upc=p["upc"],
        description=p.get("description") or p["upc"],
        brand=p.get("brand") or "",
        size=item.get("size") or "",
        price=price.get("regular") or None,
        promo=price.get("promo") or None,
        url=SITE + path,
        image=_thumbnail(p),
    )


def _thumbnail(p: dict) -> str | None:
    for image in p.get("images") or []:
        if image.get("featured"):
            for size in image.get("sizes") or []:
                if size.get("size") == "thumbnail":
                    return size.get("url")
    return None
