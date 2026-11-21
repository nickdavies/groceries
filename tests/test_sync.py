import asyncio

import pytest

from groceries import db
from groceries.sync import ON_THE_WAY, mark_delivered, mark_en_route, mark_not_received, sync

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


def make_item(conn, name, *stores, hand_pick=False, aliases=()):
    item_id = conn.execute(
        "INSERT INTO item (name, hand_pick) VALUES (?, ?)", (name, int(hand_pick))
    ).lastrowid
    for alias in (name, *aliases):
        conn.execute(
            "INSERT INTO external_ref (source, external_id, item_id) VALUES ('anylist', ?, ?)",
            (alias.casefold(), item_id),
        )
    for rank, store in enumerate(stores, start=1):
        conn.execute(
            "INSERT INTO store_rule (item_id, store_id, rank) VALUES (?, ?, ?)",
            (item_id, store_id(conn, store), rank),
        )
    return item_id


def entry(conn, line_id):
    return conn.execute("SELECT * FROM list_entry WHERE anylist_item_id = ?", (line_id,)).fetchone()


def test_unmatched_line_is_left_for_review(conn):
    fake = FakeList()
    fake.add("a", "Mystery thing")
    report = run(sync(conn, fake))
    assert report.unrouted == ["Mystery thing"]
    assert fake.writes == []
    assert entry(conn, "a")["store_id"] is None


def test_routes_to_first_choice_and_is_idempotent(conn):
    make_item(conn, "Milk", "King Soopers", "Costco")
    fake = FakeList(["King Soopers", "Costco"])
    fake.add("a", "milk")
    run(sync(conn, fake))
    assert fake.store_names("a") == {"King Soopers"}
    run(sync(conn, fake))
    assert len(fake.writes) == 1


def test_alias_and_whitespace_match(conn):
    make_item(conn, "Onion", "King Soopers", aliases=["yellow onions"])
    fake = FakeList()
    fake.add("a", "  Yellow   Onions ")
    run(sync(conn, fake))
    assert fake.store_names("a") == {"King Soopers"}


def test_skipped_store_falls_through_to_next_choice(conn):
    make_item(conn, "Paper towels", "Costco", "King Soopers")
    conn.execute("UPDATE store SET active = 0 WHERE name = 'Costco'")
    fake = FakeList()
    fake.add("a", "paper towels")
    run(sync(conn, fake))
    assert fake.store_names("a") == {"King Soopers"}


def test_hand_pick_goes_to_in_person_store(conn):
    make_item(conn, "Steak", "King Soopers", hand_pick=True)
    fake = FakeList()
    fake.add("a", "steak")
    run(sync(conn, fake))
    assert fake.store_names("a") == {"King Soopers (in person)"}
    assert entry(conn, "a")["in_person"] == 1


def test_human_reassignment_wins(conn):
    make_item(conn, "Milk", "King Soopers")
    fake = FakeList()
    fake.add("a", "milk")
    run(sync(conn, fake))
    fake.set_by_hand("a", "Costco")
    report = run(sync(conn, fake))
    assert fake.store_names("a") == {"Costco"}
    assert report.manual == ["milk"]
    e = entry(conn, "a")
    assert e["manual"] == 1 and e["store_id"] == store_id(conn, "Costco")
    # Clearing the store by hand hands the line back to routing.
    fake.set_by_hand("a")
    run(sync(conn, fake))
    assert fake.store_names("a") == {"King Soopers"}


def test_store_set_before_first_sync_is_manual(conn):
    make_item(conn, "Milk", "King Soopers")
    fake = FakeList()
    fake.add("a", "milk", "H-Mart")
    run(sync(conn, fake))
    assert fake.store_names("a") == {"H-Mart"}
    assert entry(conn, "a")["store_id"] is None  # not a store we shop online


def test_stale_on_the_way_from_recents_is_rerouted(conn):
    make_item(conn, "Milk", "King Soopers")
    fake = FakeList()
    fake.add("a", "milk", ON_THE_WAY)
    run(sync(conn, fake))
    assert fake.store_names("a") == {"King Soopers"}
    assert entry(conn, "a")["manual"] == 0


def test_cart_delivery_lifecycle(conn):
    make_item(conn, "Milk", "King Soopers")
    make_item(conn, "Eggs", "King Soopers")
    fake = FakeList()
    fake.add("a", "milk")
    fake.add("b", "eggs")
    run(sync(conn, fake))

    run(mark_en_route(conn, fake, ["a", "b"]))
    assert fake.store_names("a") == {ON_THE_WAY}
    run(sync(conn, fake))  # stays on the way, no rewrite
    assert fake.store_names("a") == {ON_THE_WAY}
    assert entry(conn, "a")["state"] == "en_route"
    assert entry(conn, "a")["store_id"] == store_id(conn, "King Soopers")

    run(mark_not_received(conn, fake, ["b"]))
    assert fake.store_names("b") == {"King Soopers"}
    assert entry(conn, "b")["state"] == "needed"

    run(mark_delivered(conn, fake, ["a"]))
    assert fake.lines["a"].checked
    assert entry(conn, "a") is None
    run(sync(conn, fake))
    assert entry(conn, "a") is None


def test_moving_an_en_route_line_by_hand_makes_it_needed(conn):
    make_item(conn, "Milk", "King Soopers")
    fake = FakeList()
    fake.add("a", "milk")
    run(sync(conn, fake))
    run(mark_en_route(conn, fake, ["a"]))
    fake.set_by_hand("a", "Costco")
    run(sync(conn, fake))
    e = entry(conn, "a")
    assert (e["state"], e["manual"]) == ("needed", 1)


def test_line_checked_in_anylist_is_dropped(conn):
    make_item(conn, "Milk", "King Soopers")
    fake = FakeList()
    fake.add("a", "milk")
    run(sync(conn, fake))
    run(fake.check_off(["a"]))
    report = run(sync(conn, fake))
    assert report.dropped == 1 and entry(conn, "a") is None


def test_dry_run_writes_nothing(conn):
    make_item(conn, "Milk", "King Soopers")
    fake = FakeList()
    fake.add("a", "milk")
    report = run(sync(conn, fake, dry_run=True))
    assert report.writes == {"milk": ["King Soopers"]}
    assert fake.writes == []
    assert entry(conn, "a") is None
