import html
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


def sections(page: str) -> dict[str, str]:
    """Each <h2>'s text, as "Name (count)", mapped to its HTML up to the next <h2>."""
    found = {}
    for match in re.finditer(r"<h2[^>]*>(.*?)</h2>.*?(?=<h2|\Z)", page, re.S):
        text = " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split())
        found[re.sub(r" (\d+)$", r" (\1)", text)] = match.group(0)
    return found


def section(page: str, heading: str) -> str:
    """The section whose heading starts with `heading`."""
    html = next((h for text, h in sections(page).items() if text.startswith(heading)), None)
    assert html, f"no section {heading!r} in {list(sections(page))}"
    return html


def test_list_groups_every_line_once(env):
    client, conn, fake = env
    fake.add("a", "Prosciutto", "Costco")
    fake.add("b", "Kewpie mayo", "H-Mart")
    fake.add("c", "Shower rod")
    fake.add("d", "Templeton Rye", "Total Beverage", quantity="2")
    client.post("/sync")
    page = client.get("/").text
    burndown = section(page, "No store yet (1)")
    assert "Shower rod" in burndown
    assert 'name="store" value="Costco"' in burndown  # online stores are one click
    assert '<wa-dropdown-item value="H-Mart">' in burndown  # in-person ones are under "More"
    assert "Prosciutto" in section(page, "Costco (1)")
    assert "× 2" in section(page, "Total Beverage (1)")
    assert "Kewpie mayo" in section(page, "In person (1)") and "H-Mart" in page
    headings = list(sections(page))
    assert headings.index("No store yet (1)") < headings.index("King Soopers (0)")


def test_setting_a_store_from_the_burndown_writes_anylist(env):
    client, conn, fake = env
    fake.add("a", "Shower rod")
    client.post("/sync")
    client.post("/lines/a/store", data={"store": "Costco"})
    assert fake.stores_of("a") == {"Costco"}
    assert "No store yet" not in client.get("/").text


def test_first_product_link_with_alternatives(env):
    client, conn, fake = env
    fake.add("a", "lemon juice", "King Soopers")
    client.post("/sync")
    item = f"/items/{item_of(conn, 'a')}"
    ks = sid(conn, "King Soopers")

    page = section(client.get("/").text, "King Soopers")
    assert "kingsoopers.com/search?query=lemon+juice" in page  # no product yet
    assert f'href="{item}#products"' in page  # "Add product" lands on the Products section

    for label, url in [("Bottled juice", "https://x/juice"), ("Whole lemons", "https://x/lemon")]:
        client.post(f"{item}/products", data={"store_id": ks, "label": label, "url": url})
    page = section(client.get("/").text, "King Soopers")
    assert 'href="https://x/juice"' in page.split("+1")[0]  # the first is the Product button
    assert "https://x/lemon" in page.split("+1")[1]  # the other is in the menu


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
    assert "milk" in section(page, "Natural Grocers (1)")
    also = section(page, "King Soopers (0)")  # listed greyed, and not counted
    assert "milk" in also and "Preferred at Natural Grocers" in also
    ks = sid(conn, "King Soopers")
    assert f'value="a" form="ordered-{ks}"' in also  # it can be ticked to order from here

    client.post(f"/stores/{ks}/ordered", data={"ids": ["a"]})
    page = client.get("/").text
    assert {"Natural Grocers (0)", "On the way (1)"} <= sections(page).keys()
    assert "milk" not in section(page, "King Soopers (0)")
    client.post("/lines/not-received", data={"ids": ["a"]})
    assert "Natural Grocers (1)" in sections(client.get("/").text)


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
    assert {"King Soopers (0)", "Costco (0)"} <= sections(page).keys()


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
    client.post("/settings/stores", data={f"search_url_{ng}": "https://ng.example/s/{}"})
    assert "https://ng.example/s/flax" in client.get("/").text


def test_anywhere_is_explicit_and_leaves_anylist_unset(env):
    client, conn, fake = env
    fake.add("a", "Shower rod")
    client.post("/sync")
    client.post("/lines/a/store", data={"store": ""})  # no choice does nothing
    assert "Shower rod" in section(client.get("/").text, "No store yet")

    client.post("/lines/a/store", data={"store": ":anywhere"})
    page = client.get("/").text
    assert "No store yet" not in page
    assert "Anywhere" in section(page, "In person (1)")
    assert fake.stores_of("a") == set() and fake.writes == []

    fake.set_by_hand("a", "Costco")  # a store set later in AnyList wins
    client.post("/sync")
    fake.set_by_hand("a")  # and clearing it there goes back to the burndown
    client.post("/sync")
    assert "Shower rod" in section(client.get("/").text, "No store yet")


def test_anywhere_on_the_item_page(env):
    client, conn, fake = env
    fake.add("a", "coffee", "Costco")
    client.post("/sync")
    item = f"/items/{item_of(conn, 'a')}"
    client.post(f"{item}/stores", data={})  # nothing chosen: no change
    assert fake.stores_of("a") == {"Costco"}
    client.post(f"{item}/stores", data={"preferred": ":anywhere"})
    assert fake.stores_of("a") == set()
    assert "Anywhere" in client.get("/items").text


def test_new_store_from_the_burndown_and_notes(env):
    client, conn, fake = env
    fake.add("a", "Rhum agricole")
    client.post("/sync")
    client.post("/lines/a/store", data={"new_store": "Argonaut"})
    assert "Argonaut" in fake.stores
    assert fake.stores_of("a") == {"Argonaut"}
    kind = conn.execute("SELECT kind FROM store WHERE name = 'Argonaut'").fetchone()[0]
    assert kind == "offline"

    item = f"/items/{item_of(conn, 'a')}"
    client.post(item, data={"name": "Rhum agricole", "note": "Ask for the Clément"})
    page = section(client.get("/").text, "In person (1)")
    assert "Argonaut" in page and "Ask for the Clément" in page


def test_store_type_can_change(env):
    client, conn, fake = env
    fake.add("a", "kimchi", "H-Mart")
    client.post("/sync")
    hmart = sid(conn, "H-Mart")
    assert "kimchi" in section(client.get("/").text, "In person")
    client.post("/settings/stores", data={f"kind_{hmart}": "manual"})
    assert "kimchi" in section(client.get("/").text, "H-Mart (1)")
    ks = sid(conn, "King Soopers")
    client.post("/settings/stores", data={f"kind_{ks}": "offline"})  # Kroger's type is fixed
    assert conn.execute("SELECT kind FROM store WHERE id = ?", (ks,)).fetchone()[0] == "kroger"


def test_add_store_from_settings(env):
    client, conn, fake = env
    client.post("/stores", data={"name": "Target", "kind": "manual"})
    assert "Target" in fake.stores
    assert "Target (0)" in sections(client.get("/").text)


def test_every_page_renders(env):
    client, conn, fake = env
    fake.add("a", "milk", "King Soopers", "H-Mart")
    client.post("/sync")
    for path in ["/", "/items", f"/items/{item_of(conn, 'a')}", "/settings", "/healthz"]:
        response = client.get(path)
        assert response.status_code == 200, path
        # Absolute URLs take the scheme the app sees, http behind the gateway: mixed content.
        assert "http://testserver" not in response.text, path


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


def test_search_with_no_results_says_so(env):
    client, conn, fake = env
    fake.add("a", "butter", "King Soopers")
    client.post("/sync")
    item = f"/items/{item_of(conn, 'a')}"
    client.post(
        "/settings/kroger-location",
        data={"id": "62000001", "name": "Test Marketplace", "address": "1 Main St"},
    )
    assert "No results" not in client.get(item).text
    assert 'No results for "no such thing"' in client.get(f"{item}?q=no such thing").text


def test_names_flow_from_suggestion_to_written(env):
    client, conn, fake = env
    fake.add("a", "Shallot", "King Soopers")
    fake.add("b", "minced shallot", checked=True)
    fake.add_recipe("r", "Chicken au Poivre", ("i", "2 tablespoons", "minced shallot", ""))
    fake.add_recipe("s", "Soup", ("j", "", "salt and pepper", "to taste"))
    client.post("/sync")

    page = client.get("/names").text
    suggestions = section(page, "Likely the same")
    assert "minced shallot" in suggestions and "same words" in suggestions
    assert "salt and pepper" in section(page, "Compounds and choices")
    assert "Nothing to review" in section(page, "Needs review")

    shallot = conn.execute("SELECT id FROM item WHERE name = 'Shallot'").fetchone()[0]
    client.post(
        "/names/map",
        data={"key": "minced shallot", "target_id": shallot, "unit": "", "style": "minced"},
    )
    review = section(client.get("/names").text, "Needs review")
    assert "2 tablespoons minced shallot" in review and "2 tablespoons Shallot, minced" in review
    assert "Shared List, crossed off" in review
    assert fake.ingredient_writes == [] and fake.line_writes == []

    forms = [html.unescape(v) for v in re.findall(r'name="forms" value="([^"]+)"', review)]
    assert len(forms) == 2
    client.post("/names/approve", data={"forms": forms[:1]})
    assert len(fake.ingredient_writes) + len(fake.line_writes) == 1
    client.post("/names/approve", data={"forms": forms[1:]})
    assert fake.ingredient("r", "i").raw == "2 tablespoons Shallot, minced"
    assert fake.lines["b"].name == "Shallot"

    page = client.get("/names").text
    assert "Nothing to review" in section(page, "Needs review")
    assert "Chicken au Poivre" in section(page, "Recent rewrites")


def test_not_the_same_and_ignore_hide_suggestions(env):
    client, conn, fake = env
    fake.add("a", "Lemon", "King Soopers")
    fake.add("b", "Lemon juice", checked=True)
    fake.add("c", "Lemon curd", checked=True)
    client.post("/sync")
    assert "Lemon juice" in section(client.get("/names").text, "Contains another name")
    client.post("/names/distinct", data={"a": "lemon juice", "b": "lemon"})
    client.post("/names/ignore", data={"key": "lemon curd"})
    suggestions = section(client.get("/names").text, "Contains another name")
    assert "Lemon juice" not in suggestions and "Lemon curd" not in suggestions


def test_map_by_name_creates_the_item(env):
    client, conn, fake = env
    fake.add_recipe("r", "Pie", ("i", "3", "Eggs, whites", ""))
    client.post("/sync")
    client.post(
        "/names/map", data={"key": "eggs, whites", "target_name": "Eggs", "style": "whites"}
    )
    row = conn.execute("SELECT id, canonical FROM item WHERE name = 'Eggs'").fetchone()
    assert row["canonical"] == 1
    assert "3 Eggs, whites" in section(client.get("/names").text, "Needs review")


def test_item_aliases_edit_and_split_off(env):
    client, conn, fake = env
    fake.add("a", "Garlic", "King Soopers")
    fake.add("b", "garlic cloves", checked=True)
    client.post("/sync")
    garlic = conn.execute("SELECT id FROM item WHERE name = 'Garlic'").fetchone()[0]
    client.post("/names/map", data={"key": "garlic cloves", "target_id": garlic})
    client.post(f"/items/{garlic}/aliases", data={"key": "garlic cloves", "unit": "cloves"})
    ref = conn.execute("SELECT unit FROM external_ref WHERE external_id = 'garlic cloves'")
    assert ref.fetchone()[0] == "cloves"
    assert "Split off" in client.get(f"/items/{garlic}").text
    client.post(f"/items/{garlic}/aliases/split", data={"key": "garlic cloves"})
    assert "Nothing to review" in section(client.get("/names").text, "Needs review")


def test_renaming_an_item_to_another_items_name_is_refused(env):
    client, conn, fake = env
    fake.add("a", "Onion", "King Soopers")
    fake.add("b", "Onions", "King Soopers")
    client.post("/sync")
    onion = conn.execute("SELECT id FROM item WHERE name = 'Onion'").fetchone()[0]
    response = client.post(f"/items/{onion}", data={"name": "Onions"})
    assert response.status_code == 409
    response = client.post(f"/items/{onion}", data={"name": "Yellow Onion", "canonical": "true"})
    assert response.status_code == 200
    row = conn.execute("SELECT name, canonical FROM item WHERE id = ?", (onion,)).fetchone()
    assert tuple(row) == ("Yellow Onion", 1)
