from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from groceries import db
from groceries.app import Services, create_app
from groceries.sync import ON_THE_WAY

from .fakes import FakeKroger, FakeList


@pytest.fixture
def env():
    conn = db.connect(":memory:")
    fake = FakeList(["King Soopers", "Costco"])
    svc = Services(conn, fake, kroger=FakeKroger())

    @asynccontextmanager
    async def lifespan(app):
        app.state.svc = svc
        yield

    with TestClient(create_app(lifespan)) as client:
        yield client, conn, fake
    conn.close()


def test_new_name_to_delivered(env):
    client, conn, fake = env
    fake.add("a", "Bananas", quantity="6")
    client.post("/sync")

    assert "Bananas" in client.get("/review").text

    r = client.post("/items", data={"name": "Bananas"})
    item_url = r.url.path
    assert item_url.startswith("/items/")
    ks = conn.execute("SELECT id FROM store WHERE name = 'King Soopers'").fetchone()[0]
    client.post(item_url, data={"name": "Bananas", f"rank_{ks}": "1"})
    assert fake.store_names("a") == {"King Soopers"}

    client.post(
        f"{item_url}/products",
        data={"store_id": ks, "label": "Bananas (1 lb)", "url": "https://example.com/b"},
    )
    page = client.get(f"/stores/{ks}").text
    assert "https://example.com/b" in page and "× 6" in page

    client.post("/entries/en-route", data={"ids": ["a"]})
    assert fake.store_names("a") == {ON_THE_WAY}
    assert "Bananas" in client.get("/on-the-way").text

    client.post("/entries/delivered", data={"ids": ["a"]})
    assert fake.lines["a"].checked


def test_every_page_renders(env):
    client, conn, fake = env
    fake.add("a", "milk")
    client.post("/sync")
    r = client.post("/items", data={"name": "Milk"})
    for path in [
        "/",
        "/review",
        "/items",
        r.url.path,
        "/on-the-way",
        "/stores/1",
        "/settings",
        "/healthz",
    ]:
        assert client.get(path).status_code == 200, path


def test_skipping_a_store_reroutes(env):
    client, conn, fake = env
    fake.add("a", "towels")
    r = client.post("/items", data={"name": "Towels"})
    costco, ks = (
        conn.execute("SELECT id FROM store WHERE name = ?", (n,)).fetchone()[0]
        for n in ("Costco", "King Soopers")
    )
    client.post(r.url.path, data={"name": "Towels", f"rank_{costco}": "1", f"rank_{ks}": "2"})
    assert fake.store_names("a") == {"Costco"}
    client.post(f"/stores/{costco}/active", data={"active": "false"})
    assert fake.store_names("a") == {"King Soopers"}


def test_kroger_search_needs_a_picked_store(env):
    client, conn, fake = env
    item = client.post("/items", data={"name": "Butter"}).url.path
    page = client.get(f"{item}?q=butter").text
    assert "Pick your store" in page
    assert client.app.state.svc.kroger.searched_at == []

    assert "Test Marketplace" in client.get("/settings?zip=80007").text
    client.post(
        "/settings/kroger-location",
        data={"id": "62000001", "name": "Test Marketplace", "address": "1 Main St"},
    )
    assert "Test Marketplace" in client.get("/settings").text

    page = client.get(f"{item}?q=butter").text
    assert "kingsoopers.com/p/butter/0001" in page
    assert client.app.state.svc.kroger.searched_at == ["62000001"]
