"""Web UI: the shopping list grouped by store, plus item and product editing.

Server-rendered forms with POST-redirect-GET, styled with Web Awesome components from
its CDN. The little client-side JS is in static/app.js. A background loop syncs AnyList
periodically. Store changes made here are written to AnyList straight away.
"""

import asyncio
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import anylist, db, sync
from .kroger import Kroger, Location

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
# Form value for "anywhere / no preference". Empty strings can't carry it: FastAPI turns
# an empty optional form field into its default.
ANYWHERE = ":anywhere"
templates = Jinja2Templates(directory=HERE / "templates")


class Services:
    """Process-wide dependencies. The lock serializes every AnyList read-modify-write."""

    def __init__(self, conn, backend: anylist.ListBackend, kroger: Kroger | None):
        self.conn = conn
        self.backend = backend
        self.kroger = kroger
        self.lock = asyncio.Lock()
        self.last_sync: sync.SyncReport | None = None
        self.last_sync_at: datetime | None = None
        self.last_error: str | None = None

    async def _sync_locked(self) -> bool:
        try:
            self.last_sync = await sync.sync(self.conn, self.backend)
            self.last_sync_at = datetime.now(UTC)
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
        this change is applied on top of them and is the latest. Every caller is a
        person choosing in the UI, so no stores means "anywhere / no preference".
        """
        async with self.lock:
            if not await self._sync_locked():
                return
            sync.set_item_stores(self.conn, item_id, names, preferred, anywhere=not names)
            try:
                await sync.push_item_stores(self.conn, self.backend, item_id)
            except Exception as e:
                log.exception("writing stores to AnyList failed")
                self.last_error = f"{type(e).__name__}: {e}"

    async def merge_items(self, source: int, target: int) -> None:
        async with self.lock:
            sync.merge_items(self.conn, source, target)
            await self._sync_locked()

    async def add_store(self, name: str, kind: str) -> None:
        """Create a store here and on the AnyList list, so it can be picked in both."""
        async with self.lock:
            self.conn.execute(
                "INSERT INTO store (name, kind, sort) "
                "SELECT ?, ?, coalesce(max(sort) + 1, 0) FROM store WHERE true "
                "ON CONFLICT (name) DO NOTHING",
                (name, kind),
            )
            await self.backend.create_store(name)


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


def item_products(conn, item_id: int | None, store_id: int, enabled_only: bool = True) -> list:
    """An item's products at a store, best first."""
    if item_id is None:
        return []
    return conn.execute(
        f"""
        SELECT p.*, ip.priority, ip.enabled,
          (SELECT group_concat(i.name, ', ') FROM item_product o JOIN item i ON i.id = o.item_id
             WHERE o.product_id = p.id AND o.item_id != ip.item_id) AS also_used_by
        FROM item_product ip JOIN product p ON p.id = ip.product_id
        WHERE ip.item_id = ? AND p.store_id = ? {"AND ip.enabled" if enabled_only else ""}
        ORDER BY ip.priority, p.id
        """,
        (item_id, store_id),
    ).fetchall()


@dataclass
class LineView:
    row: sqlite3.Row
    products: list = field(default_factory=list)  # at the store it's listed under
    also: list = field(default_factory=list)  # (store, products) for its other online stores


@dataclass
class StoreGroup:
    store: sqlite3.Row
    lines: list[LineView] = field(default_factory=list)


@dataclass
class ListView:
    unassigned: list[LineView] = field(default_factory=list)
    stores: list[StoreGroup] = field(default_factory=list)
    in_person: list[tuple[sqlite3.Row, str]] = field(default_factory=list)  # (line, store)
    on_the_way: dict[str, list] = field(default_factory=dict)  # store name -> lines


def build_list(conn) -> ListView:
    """Place every open line once.

    A line goes under its preferred store: the item's, if that is one of the line's
    AnyList stores, else the first of them in store order. Hand-pick items, lines whose
    preferred store isn't one we order from online, and items a person marked as
    "anywhere" go under "In person". Other lines with no store are the burndown list.
    """
    stores = conn.execute("SELECT * FROM store ORDER BY sort").fetchall()
    view = ListView(stores=[StoreGroup(s) for s in stores if s["kind"] != "offline"])
    groups = {g.store["id"]: g for g in view.stores}
    rows = conn.execute(
        """
        SELECT l.*, coalesce(i.hand_pick, 0) AS hand_pick, coalesce(i.anywhere, 0) AS anywhere,
          coalesce(i.note, '') AS note, s.name AS ordered_from_name,
          (SELECT x.store_id FROM item_store x WHERE x.item_id = l.item_id AND x.preferred)
            AS preferred_id
        FROM list_line l
        LEFT JOIN item i ON i.id = l.item_id
        LEFT JOIN store s ON s.id = l.ordered_from
        ORDER BY l.name COLLATE NOCASE
        """
    ).fetchall()
    for row in rows:
        if row["ordered_from"] is not None:
            view.on_the_way.setdefault(row["ordered_from_name"], []).append(row)
            continue
        names = set(json.loads(row["stores"]))
        at = [s for s in stores if s["name"] in names]
        if not at:
            if row["anywhere"]:
                view.in_person.append((row, "Anywhere"))
            else:
                view.unassigned.append(LineView(row))
            continue
        primary = next((s for s in at if s["id"] == row["preferred_id"]), at[0])
        if row["hand_pick"] or primary["id"] not in groups:
            view.in_person.append((row, primary["name"]))
            continue
        groups[primary["id"]].lines.append(
            LineView(
                row,
                products=item_products(conn, row["item_id"], primary["id"]),
                also=[
                    (s, item_products(conn, row["item_id"], s["id"]))
                    for s in at
                    if s["id"] in groups and s["id"] != primary["id"]
                ],
            )
        )
    view.in_person.sort(key=lambda pair: (pair[1], pair[0]["name"].casefold()))
    return view


def search_link(store, term: str) -> str | None:
    template = store["search_url"]
    return template.replace("{}", quote_plus(term)) if template else None


def _svc(request: Request) -> Services:
    return request.app.state.svc


def _back(request: Request, fallback: str = "/") -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or fallback, status_code=303)


def _render(request: Request, template: str, **ctx):
    return templates.TemplateResponse(request, template, {"svc": _svc(request), **ctx})


def _routes(app: FastAPI) -> None:
    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/")
    async def shopping_list(request: Request):
        conn = _svc(request).conn
        stores = conn.execute("SELECT name, kind FROM store ORDER BY sort").fetchall()
        return _render(request, "list.html", view=build_list(conn), all_stores=stores)

    @app.post("/sync")
    async def sync_now(request: Request):
        await _svc(request).sync()
        return _back(request)

    @app.post("/stores/{store_id}/ordered")
    async def lines_ordered(request: Request, store_id: int, ids: Annotated[list[str], Form()]):
        sync.mark_ordered(_svc(request).conn, store_id, ids)
        return _back(request)

    @app.post("/lines/not-received")
    async def lines_not_received(request: Request, ids: Annotated[list[str], Form()]):
        sync.mark_not_received(_svc(request).conn, ids)
        return _back(request)

    @app.post("/lines/{line_id}/store")
    async def set_line_store(
        request: Request,
        line_id: str,
        store: Annotated[str, Form()] = "",
        new_store: Annotated[str, Form()] = "",
    ):
        """Decide where an unassigned line's item comes from, written to AnyList too.

        `store` is an existing store's name, or ANYWHERE for "no preference". A
        `new_store` name creates that store (in person) and uses it.
        """
        svc = _svc(request)
        row = svc.conn.execute(
            "SELECT item_id FROM list_line WHERE anylist_item_id = ?", (line_id,)
        ).fetchone()
        if row is None or row["item_id"] is None:
            raise HTTPException(404)
        if new_store.strip():
            store = new_store.strip()
            await svc.add_store(store, "offline")
        if store == ANYWHERE:
            await svc.set_item_stores(row["item_id"], set(), None)
        elif store:
            await svc.set_item_stores(row["item_id"], {store}, store)
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
              (SELECT count(*) FROM item_product p WHERE p.item_id = i.id) AS products
            FROM item i ORDER BY i.name COLLATE NOCASE
            """
        ).fetchall()
        return _render(request, "items.html", items=rows)

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
        products = {s["id"]: item_products(conn, item_id, s["id"], False) for s in stores}
        reusable = {
            s["id"]: conn.execute(
                """
                SELECT p.*, (SELECT group_concat(i.name, ', ') FROM item_product o
                               JOIN item i ON i.id = o.item_id WHERE o.product_id = p.id)
                  AS used_by
                FROM product p WHERE p.store_id = ? AND p.id NOT IN
                  (SELECT product_id FROM item_product WHERE item_id = ?)
                ORDER BY p.label COLLATE NOCASE
                """,
                (s["id"], item_id),
            ).fetchall()
            for s in stores
        }
        kroger_store = next((s for s in stores if s["kind"] == "kroger" and s["allowed"]), None)
        location = kroger_location(conn)
        if q is None and kroger_store and not products[kroger_store["id"]]:
            q = item["name"]  # nothing picked yet, so show matches straight away
        results, search_error = [], None
        if q and kroger_store and svc.kroger and location:
            try:
                results = await svc.kroger.search(q, location.id)
            except Exception as e:
                log.exception("kroger search failed")
                search_error = f"{type(e).__name__}: {e}"
        used_by = {}
        if kroger_store:
            for r in results:
                row = conn.execute(
                    """
                    SELECT group_concat(i.name, ', ') FROM product p
                    JOIN item_product o ON o.product_id = p.id JOIN item i ON i.id = o.item_id
                    WHERE p.store_id = ? AND p.url = ?
                    """,
                    (kroger_store["id"], r.url),
                ).fetchone()
                used_by[r.url] = row[0]
        others = conn.execute(
            "SELECT id, name FROM item WHERE id != ? ORDER BY name COLLATE NOCASE", (item_id,)
        ).fetchall()
        return _render(
            request,
            "item.html",
            item=item,
            stores=stores,
            aliases=aliases,
            products=products,
            reusable=reusable,
            q=q or item["name"],
            results=results,
            used_by=used_by,
            search_error=search_error,
            kroger_enabled=svc.kroger is not None,
            kroger_location=location,
            others=others,
        )

    @app.post("/items/{item_id}")
    async def update_item(
        request: Request,
        item_id: int,
        name: Annotated[str, Form()],
        hand_pick: Annotated[bool, Form()] = False,
        note: Annotated[str, Form()] = "",
    ):
        _svc(request).conn.execute(
            "UPDATE item SET name = ?, hand_pick = ?, note = ? WHERE id = ?",
            (name.strip(), int(hand_pick), note.strip(), item_id),
        )
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/items/{item_id}/stores")
    async def update_item_stores(
        request: Request,
        item_id: int,
        stores: Annotated[list[str] | None, Form()] = None,
        preferred: Annotated[str | None, Form()] = None,
    ):
        """`preferred` is a store name, or ANYWHERE for an explicit "no preference"."""
        names = set(stores or [])
        if preferred == ANYWHERE:
            names, preferred = set(), None
        elif preferred:
            names.add(preferred)
        elif not names:
            return RedirectResponse(f"/items/{item_id}", status_code=303)  # nothing chosen
        await _svc(request).set_item_stores(item_id, names, preferred)
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/items/{item_id}/merge")
    async def merge_item(request: Request, item_id: int, source: Annotated[int, Form()]):
        await _svc(request).merge_items(source, item_id)
        return RedirectResponse(f"/items/{item_id}", status_code=303)

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
        conn.execute(
            "INSERT INTO product (store_id, label, url, upc) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (store_id, url) DO NOTHING",
            (store_id, label.strip(), url.strip(), upc or None),
        )
        (product_id,) = conn.execute(
            "SELECT id FROM product WHERE store_id = ? AND url = ?", (store_id, url.strip())
        ).fetchone()
        _link_product(conn, item_id, product_id)
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/items/{item_id}/link-product")
    async def link_product(request: Request, item_id: int, product_id: Annotated[int, Form()]):
        _link_product(_svc(request).conn, item_id, product_id)
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/items/{item_id}/products/{product_id}")
    async def update_item_product(
        request: Request,
        item_id: int,
        product_id: int,
        priority: Annotated[int, Form()],
        enabled: Annotated[bool, Form()] = False,
    ):
        _svc(request).conn.execute(
            "UPDATE item_product SET priority = ?, enabled = ? "
            "WHERE item_id = ? AND product_id = ?",
            (priority, int(enabled), item_id, product_id),
        )
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    @app.post("/items/{item_id}/products/{product_id}/unlink")
    async def unlink_product(request: Request, item_id: int, product_id: int):
        conn = _svc(request).conn
        conn.execute(
            "DELETE FROM item_product WHERE item_id = ? AND product_id = ?", (item_id, product_id)
        )
        conn.execute(
            "DELETE FROM product WHERE id = ? AND id NOT IN (SELECT product_id FROM item_product)",
            (product_id,),
        )
        return RedirectResponse(f"/items/{item_id}", status_code=303)

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
        stores = svc.conn.execute(
            """
            SELECT s.*, (SELECT count(*) FROM item_store x WHERE x.store_id = s.id) AS items
            FROM store s ORDER BY s.kind = 'offline', s.sort
            """
        ).fetchall()
        return _render(
            request,
            "settings.html",
            location=kroger_location(svc.conn),
            locations=locations,
            zip=zip or "",
            error=error,
            kroger_enabled=svc.kroger is not None,
            stores=stores,
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

    @app.post("/settings/stores")
    async def update_stores(request: Request):
        """Each store's type (order online vs in person) and search link.

        King Soopers' 'kroger' type is fixed: it is the one store with an API.
        """
        conn = _svc(request).conn
        form = await request.form()
        for key, value in form.items():
            if key.startswith("search_url_"):
                conn.execute(
                    "UPDATE store SET search_url = ? WHERE id = ?",
                    (str(value).strip() or None, int(key.removeprefix("search_url_"))),
                )
            elif key.startswith("kind_") and value in ("manual", "offline"):
                conn.execute(
                    "UPDATE store SET kind = ? WHERE id = ? AND kind != 'kroger'",
                    (value, int(key.removeprefix("kind_"))),
                )
        return RedirectResponse("/settings", status_code=303)

    @app.post("/stores")
    async def add_store(
        request: Request,
        name: Annotated[str, Form()],
        kind: Annotated[str, Form()] = "offline",
    ):
        if kind not in ("manual", "offline") or not name.strip():
            raise HTTPException(400)
        await _svc(request).add_store(name.strip(), kind)
        return RedirectResponse("/settings", status_code=303)


def _link_product(conn, item_id: int, product_id: int) -> None:
    """Add a product to an item, after the products it already has at that store."""
    conn.execute(
        """
        INSERT OR IGNORE INTO item_product (item_id, product_id, priority)
        SELECT ?, ?, coalesce(max(ip.priority) + 1, 0)
        FROM item_product ip JOIN product p ON p.id = ip.product_id
        WHERE ip.item_id = ? AND p.store_id = (SELECT store_id FROM product WHERE id = ?)
        """,
        (item_id, product_id, item_id, product_id),
    )


templates.env.globals["search_link"] = search_link
