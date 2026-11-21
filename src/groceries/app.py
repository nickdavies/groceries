"""Web UI: per-store order pages, the on-the-way list and item/product editing.

Server-rendered forms with POST-redirect-GET, no client-side JS. A background loop syncs
AnyList periodically. Store changes made here are written to AnyList straight away.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import anylist, db, sync
from .kroger import Kroger, Location
from .normalize import normalize

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")


class Services:
    """Process-wide dependencies. The lock serializes every AnyList read-modify-write."""

    def __init__(self, conn, backend: anylist.ListBackend, kroger: Kroger | None):
        self.conn = conn
        self.backend = backend
        self.kroger = kroger
        self.lock = asyncio.Lock()
        self.last_sync: sync.SyncReport | None = None
        self.last_error: str | None = None

    async def _sync_locked(self) -> bool:
        try:
            self.last_sync = await sync.sync(self.conn, self.backend)
            self.last_error = None
            return True
        except Exception as e:
            log.exception("sync failed")
            self.last_error = f"{type(e).__name__}: {e}"
            return False

    async def sync(self) -> None:
        async with self.lock:
            await self._sync_locked()

    async def set_item_stores(self, item_id: int, names: set[str], preferred: str | None):
        """Change an item's stores from the UI and write them to its AnyList lines.

        Syncing first adopts any edits people made in AnyList since the last sync, so
        this change is applied on top of them and is the latest.
        """
        async with self.lock:
            if not await self._sync_locked():
                return
            sync.set_item_stores(self.conn, item_id, names, preferred)
            try:
                await sync.push_item_stores(self.conn, self.backend, item_id)
            except Exception as e:
                log.exception("writing stores to AnyList failed")
                self.last_error = f"{type(e).__name__}: {e}"


async def _sync_loop(svc: Services, interval: float) -> None:
    while True:
        await svc.sync()
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    env = os.environ
    conn = db.connect(env.get("GROCERIES_DB", "/data/groceries.db"))
    client = await anylist.connect(conn, env["ANYLIST_EMAIL"], env["ANYLIST_PASSWORD"])
    backend = anylist.AnyListBackend(client, env.get("ANYLIST_LIST", "Shared List"))
    kroger = None
    if env.get("KROGER_CLIENT_ID"):
        kroger = Kroger(env["KROGER_CLIENT_ID"], env["KROGER_CLIENT_SECRET"])
    svc = Services(conn, backend, kroger)
    app.state.svc = svc
    loop = asyncio.create_task(_sync_loop(svc, float(env.get("SYNC_INTERVAL", "300"))))
    try:
        yield
    finally:
        loop.cancel()
        await client.close()
        if kroger:
            await kroger.close()
        conn.close()


def create_app(lifespan=lifespan) -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    _routes(app)
    return app


def kroger_location(conn) -> Location | None:
    """The King Soopers store whose prices and stock product search uses. Set in the UI."""
    row = conn.execute("SELECT value FROM kv WHERE key = 'kroger_location'").fetchone()
    return Location(**json.loads(row["value"])) if row else None


@dataclass
class StoreLines:
    to_order: list = field(default_factory=list)  # this is the line's preferred store
    also: list = field(default_factory=list)  # preferred elsewhere, can come from here
    in_person: list = field(default_factory=list)  # hand-pick items preferred here


def lines_by_store(conn) -> tuple[dict[int, StoreLines], int]:
    """Place every open, unordered line on the store pages. Returns the placements and
    how many lines have no store at all.

    A line's preferred store is its item's, if that is one of the line's AnyList stores,
    else the first of them in store order.
    """
    stores = conn.execute("SELECT * FROM store ORDER BY sort").fetchall()
    by_store = {s["id"]: StoreLines() for s in stores if s["kind"] != "offline"}
    unassigned = 0
    rows = conn.execute(
        """
        SELECT l.*, coalesce(i.hand_pick, 0) AS hand_pick,
          (SELECT x.store_id FROM item_store x WHERE x.item_id = l.item_id AND x.preferred)
            AS preferred_id
        FROM list_line l LEFT JOIN item i ON i.id = l.item_id
        WHERE l.ordered_from IS NULL
        ORDER BY l.name COLLATE NOCASE
        """
    ).fetchall()
    for row in rows:
        names = set(json.loads(row["stores"]))
        at = [s for s in stores if s["name"] in names]
        if not at:
            unassigned += 1
            continue
        preferred = next((s["id"] for s in at if s["id"] == row["preferred_id"]), at[0]["id"])
        for s in at:
            bucket = by_store.get(s["id"])
            if bucket is None:
                continue
            if s["id"] == preferred:
                (bucket.in_person if row["hand_pick"] else bucket.to_order).append(row)
            elif not row["hand_pick"]:
                bucket.also.append(row)
    return by_store, unassigned


def _svc(request: Request) -> Services:
    return request.app.state.svc


def _back(request: Request, fallback: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def _render(request: Request, template: str, **ctx):
    svc = _svc(request)
    stores = svc.conn.execute(
        "SELECT * FROM store WHERE kind != 'offline' ORDER BY sort"
    ).fetchall()
    return templates.TemplateResponse(request, template, {"nav_stores": stores, "svc": svc, **ctx})


def _routes(app: FastAPI) -> None:
    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/")
    async def overview(request: Request):
        conn = _svc(request).conn
        by_store, unassigned = lines_by_store(conn)
        stores = conn.execute(
            """
            SELECT s.*, (SELECT count(*) FROM list_line l WHERE l.ordered_from = s.id)
              AS on_the_way
            FROM store s WHERE s.kind != 'offline' ORDER BY s.sort
            """
        ).fetchall()
        return _render(
            request, "overview.html", stores=stores, by_store=by_store, unassigned=unassigned
        )

    @app.post("/sync")
    async def sync_now(request: Request):
        await _svc(request).sync()
        return _back(request)

    @app.get("/stores/{store_id}")
    async def store_page(request: Request, store_id: int):
        conn = _svc(request).conn
        store = conn.execute(
            "SELECT * FROM store WHERE id = ? AND kind != 'offline'", (store_id,)
        ).fetchone()
        if store is None:
            raise HTTPException(404)
        lines = lines_by_store(conn)[0][store_id]
        products = {}
        for line in lines.to_order + lines.also:
            if line["item_id"] is not None:
                products[line["anylist_item_id"]] = conn.execute(
                    "SELECT * FROM product WHERE item_id = ? AND store_id = ? AND enabled "
                    "ORDER BY priority, id",
                    (line["item_id"], store_id),
                ).fetchall()
        return _render(
            request,
            "store.html",
            store=store,
            lines=lines,
            products=products,
            search_url=_search_url(store),
        )

    @app.post("/stores/{store_id}/ordered")
    async def lines_ordered(request: Request, store_id: int, ids: Annotated[list[str], Form()]):
        sync.mark_ordered(_svc(request).conn, store_id, ids)
        return _back(request)

    @app.get("/on-the-way")
    async def on_the_way(request: Request):
        conn = _svc(request).conn
        rows = conn.execute(
            """
            SELECT l.*, s.name AS store_name FROM list_line l
            JOIN store s ON s.id = l.ordered_from
            ORDER BY s.sort, l.name COLLATE NOCASE
            """
        ).fetchall()
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r["store_name"], []).append(r)
        return _render(request, "on_the_way.html", groups=groups)

    @app.post("/lines/not-received")
    async def lines_not_received(request: Request, ids: Annotated[list[str], Form()]):
        sync.mark_not_received(_svc(request).conn, ids)
        return _back(request)

    @app.get("/match")
    async def match(request: Request, name: str):
        conn = _svc(request).conn
        items = conn.execute("SELECT id, name FROM item ORDER BY name COLLATE NOCASE").fetchall()
        return _render(request, "match.html", name=name, items=items)

    @app.post("/aliases")
    async def add_alias(
        request: Request, item_id: Annotated[int, Form()], alias: Annotated[str, Form()]
    ):
        svc = _svc(request)
        svc.conn.execute(
            "INSERT INTO external_ref (source, external_id, item_id) VALUES ('anylist', ?, ?) "
            "ON CONFLICT DO UPDATE SET item_id = excluded.item_id",
            (normalize(alias), item_id),
        )
        await svc.sync()
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/aliases/delete")
    async def delete_alias(request: Request, alias: Annotated[str, Form()]):
        svc = _svc(request)
        svc.conn.execute(
            "DELETE FROM external_ref WHERE source = 'anylist' AND external_id = ?", (alias,)
        )
        await svc.sync()
        return _back(request)

    @app.get("/items")
    async def items(request: Request):
        conn = _svc(request).conn
        rows = conn.execute(
            """
            SELECT i.*,
              (SELECT group_concat(external_id, ', ') FROM external_ref r
                 WHERE r.item_id = i.id AND r.source = 'anylist') AS aliases,
              (SELECT s.name FROM item_store x JOIN store s ON s.id = x.store_id
                 WHERE x.item_id = i.id AND x.preferred) AS preferred,
              (SELECT count(*) - 1 FROM item_store x WHERE x.item_id = i.id) AS others,
              (SELECT count(*) FROM product p WHERE p.item_id = i.id) AS products
            FROM item i ORDER BY i.name COLLATE NOCASE
            """
        ).fetchall()
        return _render(request, "items.html", items=rows)

    @app.post("/items")
    async def create_item(request: Request, name: Annotated[str, Form()]):
        svc = _svc(request)
        conn = svc.conn
        name = name.strip()
        existing = conn.execute("SELECT id FROM item WHERE name = ?", (name,)).fetchone()
        item_id = (
            existing["id"]
            if existing
            else conn.execute("INSERT INTO item (name) VALUES (?)", (name,)).lastrowid
        )
        conn.execute(
            "INSERT OR IGNORE INTO external_ref (source, external_id, item_id) "
            "VALUES ('anylist', ?, ?)",
            (normalize(name), item_id),
        )
        await svc.sync()
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.get("/items/{item_id}")
    async def item_page(request: Request, item_id: int, q: str | None = None):
        svc = _svc(request)
        conn = svc.conn
        item = conn.execute("SELECT * FROM item WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            raise HTTPException(404)
        stores = conn.execute(
            """
            SELECT s.*, x.store_id IS NOT NULL AS allowed, coalesce(x.preferred, 0) AS preferred
            FROM store s LEFT JOIN item_store x ON x.store_id = s.id AND x.item_id = ?
            ORDER BY s.kind = 'offline', s.sort
            """,
            (item_id,),
        ).fetchall()
        aliases = conn.execute(
            "SELECT external_id FROM external_ref WHERE source = 'anylist' AND item_id = ? "
            "ORDER BY external_id",
            (item_id,),
        ).fetchall()
        products: dict[int, list] = {s["id"]: [] for s in stores}
        for p in conn.execute(
            "SELECT * FROM product WHERE item_id = ? ORDER BY priority, id", (item_id,)
        ):
            products[p["store_id"]].append(p)
        location = kroger_location(conn)
        results, search_error = [], None
        if q and svc.kroger and location:
            try:
                results = await svc.kroger.search(q, location.id)
            except Exception as e:
                log.exception("kroger search failed")
                search_error = f"{type(e).__name__}: {e}"
        return _render(
            request,
            "item.html",
            item=item,
            stores=stores,
            aliases=aliases,
            products=products,
            q=q if q is not None else item["name"],
            results=results,
            search_error=search_error,
            kroger_enabled=svc.kroger is not None,
            kroger_location=location,
        )

    @app.post("/items/{item_id}")
    async def update_item(
        request: Request,
        item_id: int,
        name: Annotated[str, Form()],
        hand_pick: Annotated[bool, Form()] = False,
    ):
        _svc(request).conn.execute(
            "UPDATE item SET name = ?, hand_pick = ? WHERE id = ?",
            (name.strip(), int(hand_pick), item_id),
        )
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/items/{item_id}/stores")
    async def update_item_stores(
        request: Request,
        item_id: int,
        stores: Annotated[list[str] | None, Form()] = None,
        preferred: Annotated[str | None, Form()] = None,
    ):
        names = set(stores or [])
        if preferred:
            names.add(preferred)
        await _svc(request).set_item_stores(item_id, names, preferred or None)
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/items/{item_id}/delete")
    async def delete_item(request: Request, item_id: int):
        _svc(request).conn.execute("DELETE FROM item WHERE id = ?", (item_id,))
        return RedirectResponse("/items", status_code=303)

    @app.post("/items/{item_id}/products")
    async def add_product(
        request: Request,
        item_id: int,
        store_id: Annotated[int, Form()],
        label: Annotated[str, Form()],
        url: Annotated[str, Form()],
        upc: Annotated[str | None, Form()] = None,
    ):
        conn = _svc(request).conn
        (priority,) = conn.execute(
            "SELECT coalesce(max(priority) + 1, 0) FROM product WHERE item_id = ? AND store_id = ?",
            (item_id, store_id),
        ).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO product (item_id, store_id, label, url, upc, priority) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, store_id, label.strip(), url.strip(), upc or None, priority),
        )
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/products/{product_id}")
    async def update_product(
        request: Request,
        product_id: int,
        priority: Annotated[int, Form()],
        enabled: Annotated[bool, Form()] = False,
    ):
        _svc(request).conn.execute(
            "UPDATE product SET priority = ?, enabled = ? WHERE id = ?",
            (priority, int(enabled), product_id),
        )
        return _back(request)

    @app.post("/products/{product_id}/delete")
    async def delete_product(request: Request, product_id: int):
        _svc(request).conn.execute("DELETE FROM product WHERE id = ?", (product_id,))
        return _back(request)

    @app.get("/settings")
    async def settings(request: Request, zip: str | None = None):
        svc = _svc(request)
        locations, error = [], None
        if zip and svc.kroger:
            try:
                locations = await svc.kroger.locations(zip)
            except Exception as e:
                log.exception("kroger location lookup failed")
                error = f"{type(e).__name__}: {e}"
        return _render(
            request,
            "settings.html",
            location=kroger_location(svc.conn),
            locations=locations,
            zip=zip or "",
            error=error,
            kroger_enabled=svc.kroger is not None,
        )

    @app.post("/settings/kroger-location")
    async def set_kroger_location(
        request: Request,
        id: Annotated[str, Form()],
        name: Annotated[str, Form()],
        address: Annotated[str, Form()],
    ):
        value = json.dumps({"id": id, "name": name, "address": address})
        _svc(request).conn.execute(
            "INSERT INTO kv (key, value) VALUES ('kroger_location', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (value,),
        )
        return RedirectResponse("/settings", status_code=303)


def _search_url(store) -> str | None:
    """Template for a store-site search link, for lines with no saved product."""
    if store["kind"] == "kroger":
        return "https://www.kingsoopers.com/search?query={}"
    return None


def search_link(template: str | None, term: str) -> str | None:
    return template.format(quote_plus(term)) if template else None


templates.env.globals["search_link"] = search_link
templates.env.filters["urlencode_plus"] = quote_plus
