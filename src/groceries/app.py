"""Web UI: per-store shopping pages, the review queue and item/rule editing.

Server-rendered forms with POST-redirect-GET, no client-side JS. Every change that can
affect routing runs a sync so AnyList reflects it immediately. A background loop also
syncs periodically to pick up edits made in the AnyList app.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
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
    """Process-wide dependencies. One lock serializes every AnyList read-modify-write."""

    def __init__(self, conn, backend: anylist.ListBackend, kroger: Kroger | None):
        self.conn = conn
        self.backend = backend
        self.kroger = kroger
        self.lock = asyncio.Lock()
        self.last_sync: sync.SyncReport | None = None
        self.last_error: str | None = None

    async def sync(self) -> None:
        async with self.lock:
            try:
                self.last_sync = await sync.sync(self.conn, self.backend)
                self.last_error = None
            except Exception as e:
                log.exception("sync failed")
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


def _svc(request: Request) -> Services:
    return request.app.state.svc


def _back(request: Request, fallback: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def _render(request: Request, name: str, **ctx):
    svc = _svc(request)
    stores = svc.conn.execute("SELECT * FROM store ORDER BY sort").fetchall()
    return templates.TemplateResponse(request, name, {"nav_stores": stores, "svc": svc, **ctx})


def _routes(app: FastAPI) -> None:
    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/")
    async def overview(request: Request):
        conn = _svc(request).conn
        stores = conn.execute(
            """
            SELECT s.*,
              (SELECT count(*) FROM list_entry e WHERE e.store_id = s.id
                 AND e.state = 'needed' AND NOT e.in_person) AS deliver,
              (SELECT count(*) FROM list_entry e WHERE e.store_id = s.id
                 AND e.state = 'needed' AND e.in_person) AS in_person,
              (SELECT count(*) FROM list_entry e WHERE e.store_id = s.id
                 AND e.state = 'en_route') AS en_route
            FROM store s ORDER BY s.sort
            """
        ).fetchall()
        review = conn.execute(
            "SELECT count(*) FROM list_entry WHERE state = 'needed' AND store_id IS NULL "
            "AND NOT manual"
        ).fetchone()[0]
        return _render(request, "overview.html", stores=stores, review=review)

    @app.post("/sync")
    async def sync_now(request: Request):
        await _svc(request).sync()
        return _back(request)

    @app.post("/stores/{store_id}/active")
    async def set_store_active(request: Request, store_id: int, active: Annotated[bool, Form()]):
        svc = _svc(request)
        svc.conn.execute("UPDATE store SET active = ? WHERE id = ?", (int(active), store_id))
        await svc.sync()
        return _back(request)

    @app.get("/stores/{store_id}")
    async def store_page(request: Request, store_id: int):
        conn = _svc(request).conn
        store = conn.execute("SELECT * FROM store WHERE id = ?", (store_id,)).fetchone()
        if store is None:
            raise HTTPException(404)
        entries = conn.execute(
            "SELECT * FROM list_entry WHERE store_id = ? AND state = 'needed' "
            "ORDER BY in_person, name COLLATE NOCASE",
            (store_id,),
        ).fetchall()
        products = {}
        for e in entries:
            if e["item_id"] is not None:
                products[e["anylist_item_id"]] = conn.execute(
                    "SELECT * FROM product WHERE item_id = ? AND store_id = ? AND enabled "
                    "ORDER BY priority, id",
                    (e["item_id"], store_id),
                ).fetchall()
        return _render(
            request,
            "store.html",
            store=store,
            deliver=[e for e in entries if not e["in_person"]],
            in_person=[e for e in entries if e["in_person"]],
            products=products,
            search_url=_search_url(store),
        )

    @app.post("/entries/en-route")
    async def entries_en_route(request: Request, ids: Annotated[list[str], Form()]):
        svc = _svc(request)
        async with svc.lock:
            await sync.mark_en_route(svc.conn, svc.backend, ids)
        return _back(request)

    @app.get("/on-the-way")
    async def on_the_way(request: Request):
        conn = _svc(request).conn
        rows = conn.execute(
            """
            SELECT e.*, s.name AS store_name FROM list_entry e
            LEFT JOIN store s ON s.id = e.store_id
            WHERE e.state = 'en_route'
            ORDER BY s.sort, e.name COLLATE NOCASE
            """
        ).fetchall()
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r["store_name"] or "Unknown store", []).append(r)
        return _render(request, "on_the_way.html", groups=groups)

    @app.post("/entries/delivered")
    async def entries_delivered(request: Request, ids: Annotated[list[str], Form()]):
        svc = _svc(request)
        async with svc.lock:
            await sync.mark_delivered(svc.conn, svc.backend, ids)
        return _back(request)

    @app.post("/entries/not-received")
    async def entries_not_received(request: Request, ids: Annotated[list[str], Form()]):
        svc = _svc(request)
        async with svc.lock:
            await sync.mark_not_received(svc.conn, svc.backend, ids)
        return _back(request)

    @app.get("/review")
    async def review(request: Request):
        conn = _svc(request).conn
        rows = conn.execute(
            """
            SELECT e.*, i.name AS item_name FROM list_entry e
            LEFT JOIN item i ON i.id = e.item_id
            WHERE e.state = 'needed' AND e.store_id IS NULL AND NOT e.manual
            ORDER BY e.name COLLATE NOCASE
            """
        ).fetchall()
        items = conn.execute("SELECT id, name FROM item ORDER BY name COLLATE NOCASE").fetchall()
        return _render(
            request,
            "review.html",
            unmatched=[r for r in rows if r["item_id"] is None],
            unrouted=[r for r in rows if r["item_id"] is not None],
            items=items,
        )

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
        return _back(request)

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
              (SELECT group_concat(s.name, ' → ') FROM
                 (SELECT s.name FROM store_rule r JOIN store s ON s.id = r.store_id
                  WHERE r.item_id = i.id ORDER BY r.rank, s.sort) s) AS stores,
              (SELECT count(*) FROM product p WHERE p.item_id = i.id) AS products
            FROM item i ORDER BY i.name COLLATE NOCASE
            """
        ).fetchall()
        return _render(request, "items.html", items=rows)

    @app.post("/items")
    async def create_item(
        request: Request,
        name: Annotated[str, Form()],
        alias: Annotated[str | None, Form()] = None,
    ):
        svc = _svc(request)
        name = name.strip()
        existing = svc.conn.execute("SELECT id FROM item WHERE name = ?", (name,)).fetchone()
        item_id = (
            existing["id"]
            if existing
            else svc.conn.execute("INSERT INTO item (name) VALUES (?)", (name,)).lastrowid
        )
        svc.conn.execute(
            "INSERT OR IGNORE INTO external_ref (source, external_id, item_id) "
            "VALUES ('anylist', ?, ?)",
            (normalize(alias or name), item_id),
        )
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
            SELECT s.*, r.rank FROM store s
            LEFT JOIN store_rule r ON r.store_id = s.id AND r.item_id = ?
            ORDER BY s.sort
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

    @app.post("/items/{item_id}")
    async def update_item(request: Request, item_id: int):
        svc = _svc(request)
        form = await request.form()
        conn = svc.conn
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE item SET name = ?, hand_pick = ? WHERE id = ?",
            (str(form["name"]).strip(), int("hand_pick" in form), item_id),
        )
        conn.execute("DELETE FROM store_rule WHERE item_id = ?", (item_id,))
        for key, value in form.items():
            if key.startswith("rank_") and value:
                conn.execute(
                    "INSERT INTO store_rule (item_id, store_id, rank) VALUES (?, ?, ?)",
                    (item_id, int(key.removeprefix("rank_")), int(value)),
                )
        conn.execute("COMMIT")
        await svc.sync()
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/items/{item_id}/delete")
    async def delete_item(request: Request, item_id: int):
        svc = _svc(request)
        svc.conn.execute("DELETE FROM item WHERE id = ?", (item_id,))
        await svc.sync()
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
        conn = _svc(request).conn
        conn.execute(
            "UPDATE product SET priority = ?, enabled = ? WHERE id = ?",
            (priority, int(enabled), product_id),
        )
        return _back(request)

    @app.post("/products/{product_id}/delete")
    async def delete_product(request: Request, product_id: int):
        _svc(request).conn.execute("DELETE FROM product WHERE id = ?", (product_id,))
        return _back(request)


def _search_url(store) -> str | None:
    """Template for a store-site search link, for lines with no saved product."""
    if store["kind"] == "kroger":
        return "https://www.kingsoopers.com/search?query={}"
    return None


def search_link(template: str | None, term: str) -> str | None:
    return template.format(quote_plus(term)) if template else None


templates.env.globals["search_link"] = search_link
