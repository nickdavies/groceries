import asyncio
import json
import sqlite3

import pytest

from groceries import db, rename
from groceries.anylist import IngredientText, LineText
from groceries.sync import (
    NameTaken,
    item_for_name,
    make_canonical,
    map_name,
    mark_ordered,
    split_off,
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


def item(conn, name):
    return item_for_name(conn, name)


def planned(conn, fake):
    return rename.plan(conn, run(fake.snapshot()))


def approve_all(conn, fake):
    rename.approve(conn, [c.form for c in planned(conn, fake)])


def text(quantity, name, note, raw):
    return IngredientText(quantity, name, note, raw)


@pytest.mark.parametrize(
    "quantity, alias, note, item_name, unit, style, expected",
    [
        ("1 tablespoon", "whole black peppercorns", "", "Black Peppercorns", "", "",
         text("1 tablespoon", "Black Peppercorns", "", "1 tablespoon Black Peppercorns")),
        ("2 tablespoons", "minced shallot", "", "Shallot", "", "minced",
         text("2 tablespoons", "Shallot", "minced", "2 tablespoons Shallot, minced")),
        ("3", "garlic cloves, finely chopped", "", "Garlic", "cloves", "finely chopped",
         text("3 cloves", "Garlic", "finely chopped", "3 cloves Garlic, finely chopped")),
        ("5", "egg whites", "", "Eggs", "", "whites",
         text("5", "Eggs", "whites", "5 Eggs, whites")),
        # An existing note is kept, with the style in front.
        ("1", "large egg", "lightly beaten", "Eggs", "", "large",
         text("1", "Eggs", "large, lightly beaten", "1 Eggs, large, lightly beaten")),
        # A style already in the note isn't added twice.
        ("6", "egg whites", "Whites", "Eggs", "", "whites",
         text("6", "Eggs", "Whites", "6 Eggs, Whites")),
        # The unit only joins a bare number.
        ("2 tablespoons", "garlic cloves", "", "Garlic", "cloves", "",
         text("2 tablespoons", "Garlic", "", "2 tablespoons Garlic")),
    ],
)  # fmt: skip
def test_ingredient_rewrites(conn, quantity, alias, note, item_name, unit, style, expected):
    target = item(conn, item_name)
    map_name(conn, alias.casefold(), target, unit, style)
    fake = FakeList()
    fake.add_recipe("r", "Recipe", ("i", quantity, alias, note))
    [change] = planned(conn, fake)
    assert change.new == expected
    assert change.old == fake.ingredient("r", "i")
    assert change.form.bare_quantity == (quantity.strip().isdigit())


def test_items_not_marked_canonical_are_never_planned(conn):
    garlic = item(conn, "Garlic")
    conn.execute(
        "INSERT INTO external_ref (source, external_id, item_id) VALUES ('anylist', ?, ?)",
        ("garlic cloves", garlic),
    )
    fake = FakeList()
    fake.add_recipe("r", "Recipe", ("i", "3", "garlic cloves", ""))
    fake.add("a", "garlic cloves", checked=True)
    assert planned(conn, fake) == []


def test_unapproved_rewrites_wait_and_approved_ones_are_written(conn):
    map_name(conn, "minced shallot", item(conn, "Shallot"), style="minced")
    fake = FakeList()
    fake.add_recipe("r", "Chicken au Poivre", ("i", "2 tablespoons", "minced shallot", ""))
    fake.add("old", "minced shallot", checked=True)

    run(sync(conn, fake))
    assert fake.ingredient_writes == [] and fake.line_writes == []
    assert len(planned(conn, fake)) == 2

    approve_all(conn, fake)
    report = run(sync(conn, fake))
    assert (report.renamed_ingredients, report.renamed_lines) == (1, 1)
    assert fake.ingredient("r", "i").raw == "2 tablespoons Shallot, minced"
    assert fake.lines["old"].name == "Shallot" and fake.lines["old"].details == "minced"
    assert planned(conn, fake) == []

    logged = conn.execute("SELECT kind, place, old, new FROM rename_log ORDER BY id").fetchall()
    assert [(r["kind"], r["place"]) for r in logged] == [
        ("ingredient", "Chicken au Poivre"),
        ("line", "Shared List, crossed off"),
    ]
    assert json.loads(logged[0]["old"])["raw"] == "2 tablespoons minced shallot"


@pytest.mark.parametrize(
    "later",
    [
        ("j", "2 tablespoons", "minced shallot", "divided"),  # different note
        ("j", "2 tablespoons", "Minced Shallot", ""),  # different spelling
    ],
)
def test_only_exact_matches_of_an_approved_form_are_written(conn, later):
    map_name(conn, "minced shallot", item(conn, "Shallot"), style="minced")
    fake = FakeList()
    fake.add_recipe("r", "One", ("i", "1 tablespoon", "minced shallot", ""))
    approve_all(conn, fake)
    fake.add_recipe("s", "Two", later, ("k", "3 tablespoons", "minced shallot", ""))

    run(sync(conn, fake))
    assert fake.ingredient("s", "k").name == "Shallot"  # same form, other quantity
    assert fake.ingredient("s", "j").name == later[2]  # waits for review
    [waiting] = planned(conn, fake)
    assert waiting.target == "s/j"


def test_editing_a_mapping_needs_approval_again(conn):
    shallot = item(conn, "Shallot")
    map_name(conn, "minced shallot", shallot, style="minced")
    fake = FakeList()
    fake.add_recipe("r", "One", ("i", "1 tablespoon", "minced shallot", ""))
    approve_all(conn, fake)
    conn.execute("UPDATE external_ref SET style = 'finely minced' WHERE external_id = ?",
                 ("minced shallot",))  # fmt: skip
    run(sync(conn, fake))
    assert fake.ingredient("r", "i").name == "minced shallot"


def test_renaming_the_item_needs_approval_again(conn):
    shallot = item(conn, "Shallot")
    map_name(conn, "minced shallot", shallot, style="minced")
    fake = FakeList()
    fake.add_recipe("r", "One", ("i", "1 tablespoon", "minced shallot", ""))
    approve_all(conn, fake)
    conn.execute("UPDATE item SET name = 'Shallots' WHERE id = ?", (shallot,))
    make_canonical(conn, shallot)
    run(sync(conn, fake))
    assert fake.ingredient("r", "i").name == "minced shallot"


def test_an_ordered_line_holds_back_its_recipe(conn):
    garlic = item(conn, "Garlic")
    map_name(conn, "garlic cloves", garlic, unit="cloves")
    fake = FakeList()
    fake.add_recipe("r", "Soup", ("i", "3", "garlic cloves", ""))
    fake.add("a", "garlic cloves", "King Soopers", recipes=("r",))
    run(sync(conn, fake))
    mark_ordered(conn, 1, ["a"])
    approve_all(conn, fake)

    run(sync(conn, fake))
    assert fake.ingredient_writes == []
    held = [c for c in planned(conn, fake) if c.form.kind == "ingredient"]
    assert held[0].held
    # The line itself keeps its id when renamed, so it is still written.
    assert fake.lines["a"].name == "Garlic"


def test_ingredients_are_written_first_and_moved_lines_keep_the_items_stores(conn):
    map_name(conn, "garlic cloves", item(conn, "Garlic"), unit="cloves")
    fake = FakeList()
    fake.add_recipe("r", "Soup", ("i", "3", "garlic cloves", ""))
    fake.add("a", "garlic cloves", "King Soopers", recipes=("r",))
    run(sync(conn, fake))
    approve_all(conn, fake)

    run(sync(conn, fake))
    assert "a" not in fake.lines and fake.lines["a-moved"].name == "Garlic"
    assert fake.line_writes == []  # nothing left to rename after the move
    run(sync(conn, fake))
    assert fake.lines["a-moved"].stores == {"King Soopers"}  # the item's stores
    rows = conn.execute("SELECT item_id FROM list_line").fetchall()
    assert [r[0] for r in rows] == [item(conn, "Garlic")]


def test_lines_get_the_unit_and_style_in_their_details(conn):
    map_name(conn, "garlic cloves, minced", item(conn, "Garlic"), unit="cloves", style="minced")
    fake = FakeList()
    fake.add("a", "garlic cloves, minced", quantity="3", details="for the soup", checked=True)
    [change] = planned(conn, fake)
    assert change.new == LineText("Garlic", "cloves, minced, for the soup")


def test_split_off_stops_rewrites(conn):
    garlic = item(conn, "Garlic")
    map_name(conn, "garlic cloves", garlic)
    fake = FakeList()
    fake.add("a", "garlic cloves", checked=True)
    assert planned(conn, fake)
    split_off(conn, garlic, "garlic cloves")
    assert planned(conn, fake) == []
    split_off(conn, garlic, "garlic")  # its own name stays
    assert item(conn, "garlic") == garlic


def test_mapping_an_items_name_merges_that_item(conn):
    eggs, whites = item(conn, "Eggs"), item(conn, "egg whites")
    map_name(conn, "egg whites", eggs, style="whites")
    assert conn.execute("SELECT count(*) FROM item WHERE id = ?", (whites,)).fetchone()[0] == 0
    assert item(conn, "Egg Whites") == eggs


def test_make_canonical_refuses_a_name_another_item_owns(conn):
    onion, onions = item(conn, "Onion"), item(conn, "Onions")
    map_name(conn, "yellow onion", onions)
    conn.execute("UPDATE item SET name = 'Yellow Onion' WHERE id = ?", (onion,))
    with pytest.raises(NameTaken):
        make_canonical(conn, onion)


def test_record_names_counts_crossed_off_lines_and_recipes(conn):
    fake = FakeList()
    fake.add("a", "Black Pepper", checked=True)
    fake.add("b", "black pepper")
    fake.add("c", "Black Pepper", checked=True)
    fake.add_recipe("r", "Pie", ("i", "1/8 tsp", "Black Pepper", ""))
    run(sync(conn, fake))
    row = conn.execute("SELECT * FROM anylist_name WHERE key = 'black pepper'").fetchone()
    assert (row["label"], row["line_count"], row["recipe_count"]) == ("Black Pepper", 3, 1)
    assert json.loads(row["spellings"]) == ["Black Pepper", "black pepper"]

    del fake.lines["a"], fake.lines["b"], fake.lines["c"]
    run(sync(conn, fake))
    row = conn.execute("SELECT * FROM anylist_name WHERE key = 'black pepper'").fetchone()
    assert (row["line_count"], row["recipe_count"]) == (0, 1)


def test_skipped_writes_are_not_logged(conn):
    map_name(conn, "garlic cloves", item(conn, "Garlic"))
    fake = FakeList()
    fake.add("a", "garlic cloves", checked=True)
    approve_all(conn, fake)
    snap = run(fake.snapshot())
    fake.edit("a", name="garlic cloves", details="someone typed this")  # edited since
    ingredients, lines, _ = run(rename.apply(conn, fake, snap))
    assert lines == 0
    assert conn.execute("SELECT count(*) FROM rename_log").fetchone()[0] == 0


def test_migration_from_v4_keeps_items_and_aliases(tmp_path):
    path = tmp_path / "g.db"
    old = sqlite3.connect(path, isolation_level=None)
    for i, sql in enumerate(db.MIGRATIONS[:4], start=1):
        old.executescript(f"BEGIN;\n{sql};\nPRAGMA user_version = {i};\nCOMMIT;")
    old.execute("INSERT INTO item (name, note) VALUES ('Garlic', 'the big heads')")
    old.execute("INSERT INTO external_ref VALUES ('anylist', 'garlic', 1)")
    old.execute("INSERT INTO external_ref VALUES ('anylist', 'garlic cloves', 1)")
    old.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    row = conn.execute("SELECT name, note, canonical FROM item").fetchone()
    assert tuple(row) == ("Garlic", "the big heads", 0)
    refs = conn.execute("SELECT external_id, unit, style FROM external_ref ORDER BY 1").fetchall()
    assert [tuple(r) for r in refs] == [("garlic", "", ""), ("garlic cloves", "", "")]
    for table in ("anylist_name", "name_distinct", "rename_approval", "rename_log"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_a_unit_after_one_is_singular_and_raw_text_is_trimmed(conn):
    map_name(conn, "garlic cloves", item(conn, "Garlic"), unit="cloves")
    map_name(conn, "egg", item(conn, "Eggs"))
    fake = FakeList()
    fake.add_recipe("r", "R", ("i", "1", "garlic cloves", ""), ("j", "3", "Egg", "Yolks only "))
    new = {c.target: c.new for c in planned(conn, fake)}
    assert new["r/i"].raw == "1 clove Garlic"
    assert new["r/j"].raw == "3 Eggs, Yolks only"
