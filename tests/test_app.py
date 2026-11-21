from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from groceries import db
from groceries.app import Services, create_app

from .fakes import FakeKroger, FakeList


@pytest.fixture
def env():
    conn = db.connect(":memory:")
    fake = FakeList()
    svc = Services(conn, fake, kroger=FakeKroger())

    @asynccontextmanager
    async def lifespan(app):
        app.state.svc = svc
        yield

    with TestClient(create_app(lifespan)) as client:
        yield client, conn, fake
    conn.close()


def sid(conn, name):
    return conn.execute("SELECT id FROM store WHERE name = ?", (name,)).fetchone()[0]


def test_line_to_product_to_ordered(env):
    client, conn, fake = env
    fake.add("a", "Bananas", "King Soopers", quantity="6")
    client.post("/sync")
    ks = sid(conn, "King Soopers")

    page = client.get(f"/stores/{ks}").text
    assert "Bananas" in page and "/match?name=Bananas" in page

    item = client.post("/items", data={"name": "Bananas"}).url.path
    assert "King Soopers" in client.get(item).text  # adopted from AnyList
    client.post(
        f"{item}/products",
        data={"store_id": ks, "label": "Bananas (1 lb)", "url": "https://example.com/b"},
    )
    page = client.get(f"/stores/{ks}").text
    assert "https://example.com/b" in page and "× 6" in page

    client.post(f"/stores/{ks}/ordered", data={"ids": ["a"]})
    assert "Bananas" not in client.get(f"/stores/{ks}").text
    assert "Bananas" in client.get("/on-the-way").text
    client.post("/lines/not-received", data={"ids": ["a"]})
    assert "Bananas" in client.get(f"/stores/{ks}").text


def test_preferred_store_and_also_available(env):
    client, conn, fake = env
    fake.add("a", "milk")
    item = client.post("/items", data={"name": "Milk"}).url.path
    client.post(f"{item}/stores", data={"preferred": "Natural Grocers", "stores": ["King Soopers"]})
    assert fake.stores_of("a") == {"Natural Grocers", "King Soopers"}

    ng = client.get(f"/stores/{sid(conn, 'Natural Grocers')}").text
    ks = client.get(f"/stores/{sid(conn, 'King Soopers')}").text
    assert ng.index("milk") < ng.index("Mark ticked")
    assert "Also available here" in ks and "milk" in ks

    client.post(f"/stores/{sid(conn, 'King Soopers')}/ordered", data={"ids": ["a"]})
    assert "milk" not in client.get(f"/stores/{sid(conn, 'Natural Grocers')}").text


def test_ui_store_change_applies_on_top_of_anylist_edits(env):
    client, conn, fake = env
    fake.add("a", "milk")
    item = client.post("/items", data={"name": "Milk"}).url.path
    client.post(f"{item}/stores", data={"preferred": "King Soopers"})
    fake.set_by_hand("a", "Costco")  # person edits AnyList, then someone edits the UI
    client.post(f"{item}/stores", data={"preferred": "Natural Grocers", "stores": ["Costco"]})
    assert fake.stores_of("a") == {"Natural Grocers", "Costco"}
    assert "Only at" not in client.get("/items").text


def test_hand_pick_is_never_in_the_order_list(env):
    client, conn, fake = env
    fake.add("a", "ribeye", "King Soopers", "Costco")
    item = client.post("/items", data={"name": "Ribeye"}).url.path
    client.post(item, data={"name": "Ribeye", "hand_pick": "true"})
    ks = client.get(f"/stores/{sid(conn, 'King Soopers')}").text
    assert "Pick in person" in ks
    assert ks.index("ribeye") > ks.index("Pick in person")
    assert "ribeye" not in client.get(f"/stores/{sid(conn, 'Costco')}").text


def test_every_page_renders(env):
    client, conn, fake = env
    fake.add("a", "milk", "King Soopers", "H-Mart")
    client.post("/sync")
    item = client.post("/items", data={"name": "Milk"}).url.path
    pages = ["/", "/items", item, "/on-the-way", "/settings", "/match?name=milk", "/healthz"]
    pages += [
        f"/stores/{s}" for (s,) in conn.execute("SELECT id FROM store WHERE kind != 'offline'")
    ]
    for path in pages:
        assert client.get(path).status_code == 200, path
    assert client.get(f"/stores/{sid(conn, 'H-Mart')}").status_code == 404


def test_kroger_search_needs_a_picked_store(env):
    client, conn, fake = env
    fake.add("a", "butter", "King Soopers")
    item = client.post("/items", data={"name": "Butter"}).url.path
    page = client.get(f"{item}?q=butter").text
    assert "Pick your store" in page
    assert client.app.state.svc.kroger.searched_at == []

    assert "Test Marketplace" in client.get("/settings?zip=80007").text
    client.post(
        "/settings/kroger-location",
        data={"id": "62000001", "name": "Test Marketplace", "address": "1 Main St"},
    )
    page = client.get(f"{item}?q=butter").text
    assert "kingsoopers.com/p/butter/0001" in page
    assert client.app.state.svc.kroger.searched_at == ["62000001"]
