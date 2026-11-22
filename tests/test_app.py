import re
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


def item_of(conn, line_id):
    return conn.execute(
        "SELECT item_id FROM list_line WHERE anylist_item_id = ?", (line_id,)
    ).fetchone()[0]


def section(page: str, heading: str) -> str:
    """The HTML from an <h2> starting with `heading` up to the next <h2>."""
    match = re.search(rf"<h2>{re.escape(heading)}.*?(?=<h2>|\Z)", page, re.S)
    assert match, f"no section {heading!r}"
    return match.group(0)


def test_list_groups_every_line_once(env):
    client, conn, fake = env
    fake.add("a", "Prosciutto", "Costco")
    fake.add("b", "Kewpie mayo", "H-Mart")
    fake.add("c", "Shower rod")
    fake.add("d", "Templeton Rye", "Total Beverage", quantity="2")
    client.post("/sync")
    page = client.get("/").text
    assert "Shower rod" in section(page, "No store yet (1)")
    assert "Prosciutto" in section(page, "Costco (1)")
    assert "× 2" in section(page, "Total Beverage (1)")
    assert "Kewpie mayo" in section(page, "In person (1)") and "H-Mart" in page
    assert page.index("No store yet") < page.index("King Soopers (0)")


def test_setting_a_store_from_the_burndown_writes_anylist(env):
    client, conn, fake = env
    fake.add("a", "Shower rod")
    client.post("/sync")
    client.post("/lines/a/store", data={"store": "Costco"})
    assert fake.stores_of("a") == {"Costco"}
    assert "No store yet" not in client.get("/").text


def test_product_links_and_pick_one(env):
    client, conn, fake = env
    fake.add("a", "lemon juice", "King Soopers")
    client.post("/sync")
    item = f"/items/{item_of(conn, 'a')}"
    ks = sid(conn, "King Soopers")

    page = section(client.get("/").text, "King Soopers")
    assert "kingsoopers.com/search?query=lemon+juice" in page  # no product yet

    for label, url in [("Bottled juice", "https://x/juice"), ("Whole lemons", "https://x/lemon")]:
        client.post(f"{item}/products", data={"store_id": ks, "label": label, "url": url})
    page = section(client.get("/").text, "King Soopers")
    assert "pick one:" in page and "https://x/juice" in page and "https://x/lemon" in page


def test_product_is_shared_between_items(env):
    client, conn, fake = env
    fake.add("a", "lemon juice", "King Soopers")
    fake.add("b", "lemon", "King Soopers")
    client.post("/sync")
    ks = sid(conn, "King Soopers")
    juice, lemon = f"/items/{item_of(conn, 'a')}", f"/items/{item_of(conn, 'b')}"
    client.post(f"{lemon}/products", data={"store_id": ks, "label": "Lemons", "url": "https://x/l"})
    (pid,) = conn.execute("SELECT id FROM product").fetchone()

    assert "used by lemon" in client.get(juice).text  # offered for reuse
    client.post(f"{juice}/link-product", data={"product_id": pid})
    assert "also used by lemon" in client.get(juice).text
    assert conn.execute("SELECT count(*) FROM product").fetchone()[0] == 1

    client.post(f"{juice}/products/{pid}/unlink")
    assert conn.execute("SELECT count(*) FROM product").fetchone()[0] == 1  # lemon keeps it
    client.post(f"{lemon}/products/{pid}/unlink")
    assert conn.execute("SELECT count(*) FROM product").fetchone()[0] == 0


def test_also_available_and_ordering_from_there(env):
    client, conn, fake = env
    fake.add("a", "milk")
    client.post("/sync")
    item = f"/items/{item_of(conn, 'a')}"
    client.post(f"{item}/stores", data={"preferred": "Natural Grocers", "stores": ["King Soopers"]})
    assert fake.stores_of("a") == {"Natural Grocers", "King Soopers"}
    page = client.get("/").text
    assert "also at King Soopers" in section(page, "Natural Grocers (1)")
    assert "King Soopers (0)" in page

    client.post(f"/stores/{sid(conn, 'King Soopers')}/ordered", data={"ids": ["a"]})
    page = client.get("/").text
    assert "Natural Grocers (0)" in page and "On the way (1)" in page
    client.post("/lines/not-received", data={"ids": ["a"]})
    assert "Natural Grocers (1)" in client.get("/").text


def test_ui_store_change_applies_on_top_of_anylist_edits(env):
    client, conn, fake = env
    fake.add("a", "milk", "King Soopers")
    client.post("/sync")
    fake.set_by_hand("a", "Costco")  # person edits AnyList, then someone edits the UI
    item = f"/items/{item_of(conn, 'a')}"
    client.post(f"{item}/stores", data={"preferred": "Natural Grocers", "stores": ["Costco"]})
    assert fake.stores_of("a") == {"Natural Grocers", "Costco"}


def test_hand_pick_goes_in_person(env):
    client, conn, fake = env
    fake.add("a", "ribeye", "King Soopers", "Costco")
    client.post("/sync")
    item = f"/items/{item_of(conn, 'a')}"
    client.post(item, data={"name": "Ribeye", "hand_pick": "true"})
    page = client.get("/").text
    assert "ribeye" in section(page, "In person (1)")
    assert "King Soopers (0)" in page and "Costco (0)" in page


def test_merge_from_the_item_page(env):
    client, conn, fake = env
    fake.add("a", "Avocados", "King Soopers")
    fake.add("b", "Avocado")
    client.post("/sync")
    keep, typo = item_of(conn, "a"), item_of(conn, "b")
    client.post(f"/items/{keep}/merge", data={"source": typo})
    assert item_of(conn, "b") == keep
    assert fake.stores_of("b") == {"King Soopers"}
    assert "No store yet" not in client.get("/").text


def test_search_links_are_editable(env):
    client, conn, fake = env
    fake.add("a", "flax", "Natural Grocers")
    client.post("/sync")
    ng = sid(conn, "Natural Grocers")
    assert "Search Natural Grocers" not in client.get("/").text
    client.post("/settings/search-links", data={f"search_url_{ng}": "https://ng.example/s/{}"})
    assert "https://ng.example/s/flax" in client.get("/").text


def test_every_page_renders(env):
    client, conn, fake = env
    fake.add("a", "milk", "King Soopers", "H-Mart")
    client.post("/sync")
    for path in ["/", "/items", f"/items/{item_of(conn, 'a')}", "/settings", "/healthz"]:
        assert client.get(path).status_code == 200, path


def test_kroger_search_runs_on_open_once_a_store_is_picked(env):
    client, conn, fake = env
    fake.add("a", "butter", "King Soopers")
    client.post("/sync")
    item = f"/items/{item_of(conn, 'a')}"
    assert "Pick your store" in client.get(item).text
    assert client.app.state.svc.kroger.searched_at == []

    assert "Test Marketplace" in client.get("/settings?zip=80007").text
    client.post(
        "/settings/kroger-location",
        data={"id": "62000001", "name": "Test Marketplace", "address": "1 Main St"},
    )
    page = client.get(item).text  # no product yet, so it searches straight away
    assert "kingsoopers.com/p/butter/0001" in page
    assert client.app.state.svc.kroger.searched_at == ["62000001"]
