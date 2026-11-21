import asyncio
import json
import sqlite3

import pytest

from groceries import db
from groceries.sync import (
    item_stores,
    mark_not_received,
    mark_ordered,
    push_item_stores,
    set_item_stores,
    sync,
)

from .fakes import FakeList


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def store_id(conn, name):
    return conn.execute("SELECT id FROM store WHERE name = ?", (name,)).fetchone()[0]


def make_item(conn, name, *stores, aliases=()):
    item_id = conn.execute("INSERT INTO item (name) VALUES (?)", (name,)).lastrowid
    for alias in (name, *aliases):
        conn.execute(
            "INSERT INTO external_ref VALUES ('anylist', ?, ?)", (alias.casefold(), item_id)
        )
    if stores:
        set_item_stores(conn, item_id, stores, preferred=stores[0])
    return item_id


def preferred(conn, item_id):
    row = conn.execute(
        "SELECT s.name FROM item_store x JOIN store s ON s.id = x.store_id "
        "WHERE x.item_id = ? AND x.preferred",
        (item_id,),
    ).fetchone()
    return row[0] if row else None


def line(conn, line_id):
    return conn.execute("SELECT * FROM list_line WHERE anylist_item_id = ?", (line_id,)).fetchone()


def test_unmatched_lines_are_mirrored_untouched(conn):
    fake = FakeList()
    fake.add("a", "kimchi", "H-Mart", quantity="2")
    fake.add("b", "mystery")
    run(sync(conn, fake))
    assert fake.writes == []
    assert json.loads(line(conn, "a")["stores"]) == ["H-Mart"]
    assert line(conn, "a")["quantity"] == "2"
    kind = conn.execute("SELECT kind FROM store WHERE name = 'H-Mart'").fetchone()[0]
    assert kind == "offline"


def test_new_line_takes_the_item_stores_over_stale_recents(conn):
    make_item(conn, "Milk", "King Soopers", "Natural Grocers")
    fake = FakeList()
    fake.add("a", "milk", "Costco")  # re-added from a Recent made before the change
    run(sync(conn, fake))
    assert fake.stores_of("a") == {"King Soopers", "Natural Grocers"}


def test_new_line_fills_in_an_unassigned_line(conn):
    make_item(conn, "Milk", "King Soopers")
    fake = FakeList()
    fake.add("a", "milk")
    run(sync(conn, fake))
    assert fake.stores_of("a") == {"King Soopers"}
    run(sync(conn, fake))
    assert len(fake.writes) == 1


def test_item_without_stores_adopts_the_line(conn):
    item_id = make_item(conn, "Halloumi")
    fake = FakeList()
    fake.add("a", "halloumi", "Natural Grocers", "King Soopers")
    run(sync(conn, fake))
    assert fake.writes == []
    assert item_stores(conn, item_id) == {"Natural Grocers", "King Soopers"}
    assert preferred(conn, item_id) == "King Soopers"  # first in store order


def test_change_in_anylist_wins_and_updates_the_item(conn):
    item_id = make_item(conn, "Milk", "King Soopers", "Natural Grocers")
    fake = FakeList()
    fake.add("a", "milk")
    run(sync(conn, fake))
    fake.set_by_hand("a", "Natural Grocers")
    report = run(sync(conn, fake))
    assert report.adopted == 1
    assert item_stores(conn, item_id) == {"Natural Grocers"}
    assert preferred(conn, item_id) == "Natural Grocers"
    assert fake.stores_of("a") == {"Natural Grocers"}


def test_preferred_survives_when_anylist_adds_a_store(conn):
    item_id = make_item(conn, "Milk", "Natural Grocers")
    fake = FakeList()
    fake.add("a", "milk")
    run(sync(conn, fake))
    fake.set_by_hand("a", "Natural Grocers", "King Soopers")
    run(sync(conn, fake))
    assert preferred(conn, item_id) == "Natural Grocers"


def test_clearing_stores_in_anylist_clears_the_item(conn):
    item_id = make_item(conn, "Milk", "King Soopers")
    fake = FakeList()
    fake.add("a", "milk")
    run(sync(conn, fake))
    fake.set_by_hand("a")
    run(sync(conn, fake))
    assert item_stores(conn, item_id) == set()
    assert preferred(conn, item_id) is None


def test_ui_change_is_pushed_to_open_lines(conn):
    item_id = make_item(conn, "Milk", "King Soopers")
    fake = FakeList()
    fake.add("a", "milk")
    fake.add("b", "Milk")
    run(sync(conn, fake))
    set_item_stores(conn, item_id, {"Costco"}, preferred="Costco")
    run(push_item_stores(conn, fake, item_id))
    assert fake.stores_of("a") == fake.stores_of("b") == {"Costco"}
    run(sync(conn, fake))  # our own write isn't mistaken for a person's edit
    assert item_stores(conn, item_id) == {"Costco"}


def test_failed_write_is_retried_not_adopted(conn):
    item_id = make_item(conn, "Milk", "King Soopers")
    fake = FakeList()
    fake.add("a", "milk")
    fake.fail_writes = True
    with pytest.raises(ConnectionError):
        run(sync(conn, fake))
    fake.fail_writes = False
    run(sync(conn, fake))
    assert fake.stores_of("a") == {"King Soopers"}
    assert item_stores(conn, item_id) == {"King Soopers"}


def test_newly_linked_line_is_treated_as_new(conn):
    fake = FakeList()
    fake.add("a", "yellow onions")
    run(sync(conn, fake))
    make_item(conn, "Onion", "King Soopers", aliases=["yellow onions"])
    run(sync(conn, fake))
    assert fake.stores_of("a") == {"King Soopers"}


def test_ordered_lines_are_left_alone(conn):
    item_id = make_item(conn, "Milk", "King Soopers")
    fake = FakeList()
    fake.add("a", "milk")
    run(sync(conn, fake))
    mark_ordered(conn, store_id(conn, "King Soopers"), ["a"])
    set_item_stores(conn, item_id, {"Costco"}, preferred="Costco")
    run(push_item_stores(conn, fake, item_id))
    assert fake.stores_of("a") == {"King Soopers"}
    run(sync(conn, fake))
    assert fake.stores_of("a") == {"King Soopers"}
    assert line(conn, "a")["ordered_from"] == store_id(conn, "King Soopers")

    mark_not_received(conn, ["a"])  # back to needed: the item's current stores apply
    run(sync(conn, fake))
    assert fake.stores_of("a") == {"Costco"}


def test_checked_off_line_is_dropped(conn):
    fake = FakeList()
    fake.add("a", "milk", "King Soopers")
    run(sync(conn, fake))
    fake.check_off("a")
    report = run(sync(conn, fake))
    assert report.dropped == 1 and line(conn, "a") is None


def test_ordering_twice_keeps_the_first_store(conn):
    fake = FakeList()
    fake.add("a", "milk", "King Soopers", "Natural Grocers")
    run(sync(conn, fake))
    mark_ordered(conn, store_id(conn, "Natural Grocers"), ["a"])
    mark_ordered(conn, store_id(conn, "King Soopers"), ["a"])
    assert line(conn, "a")["ordered_from"] == store_id(conn, "Natural Grocers")


def test_migration_from_v1_keeps_settings_rules_and_ordered_lines(tmp_path):
    path = tmp_path / "g.db"
    old = sqlite3.connect(path, isolation_level=None)
    old.executescript(f"BEGIN;\n{db.MIGRATIONS[0]};\nPRAGMA user_version = 1;\nCOMMIT;")
    old.execute("INSERT INTO kv VALUES ('kroger_location', '{}')")
    old.execute("INSERT INTO item (name, hand_pick) VALUES ('Milk', 1)")
    old.execute("INSERT INTO product (item_id, store_id, label, url) VALUES (1, 1, 'M', 'u')")
    old.execute("INSERT INTO store_rule VALUES (1, 3, 1), (1, 1, 2)")
    old.execute(
        "INSERT INTO list_entry (anylist_item_id, name, item_id, state, store_id) "
        "VALUES ('a', 'milk', 1, 'en_route', 1), ('b', 'eggs', NULL, 'needed', 1)"
    )
    old.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("SELECT value FROM kv").fetchone()[0] == "{}"
    assert conn.execute("SELECT count(*) FROM product").fetchone()[0] == 1
    assert conn.execute("SELECT hand_pick FROM item").fetchone()[0] == 1
    assert item_stores(conn, 1) == {"King Soopers", "Natural Grocers"}
    assert preferred(conn, 1) == "Natural Grocers"
    rows = {r["anylist_item_id"]: r for r in conn.execute("SELECT * FROM list_line")}
    assert rows["a"]["ordered_from"] == 1 and rows["b"]["ordered_from"] is None
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO store (name, kind, sort) VALUES ('X', 'bogus', 9)")
