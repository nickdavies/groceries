"""Canonical names: rewrite a curated item's AnyList names to the item's own name.

A name maps to an item through external_ref, by normalized name. For each item marked
canonical, every recipe ingredient and list line (crossed off too) whose name maps to it
but isn't exactly item.name becomes a planned Change:
  - the name becomes item.name;
  - the alias's unit joins a bare-number quantity: "3" + "cloves" gives "3 cloves"
    (ingredients only; on a line it goes into the details with the style);
  - the alias's style goes in front of the note or details, unless already in them;
  - an ingredient's display text is rebuilt as "{quantity} {name}, {note}", the form
    AnyList's own editor writes. Scaling uses `quantity`, never this text.

Lines added from a recipe are left alone: AnyList ignores renames of those lines (it
acknowledges the write but keeps the name), and rewriting the recipe already moves or
relinks them.

A change is written only if its Form was approved on /names. A Form is the exact source
(old name, note or details, whether the quantity was a bare number) plus the exact
mapping (new name, unit, style), so a different note, a different spelling or an edited
mapping all wait for review again. Ingredient changes are held back while an ordered
line comes from their recipe: AnyList moves that line to a new id, which would drop its
"On the way" state. Any change already written once whose target still has the old text
is held back too, so a write AnyList doesn't keep is never retried in a loop.

Ingredients are written first, because saving a recipe makes AnyList move its open lines
to the line for the new name, which merges same-unit lines. Lines are planned again
afterwards from a fresh snapshot.
"""

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, astuple, dataclass
from datetime import UTC, datetime

from aioanylist.derived import full_ingredient_string
from aioanylist.parsing.quantity import (
    amount_as_float,
    parse_quantity_and_package_size,
    unit_for_amount,
)
from aioanylist.proto import PB

from .anylist import IngredientText, LineText, ListBackend, Snapshot
from .normalize import normalize

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Form:
    """What an approval covers. Mirrors the rename_approval table."""

    kind: str  # 'ingredient' | 'line'
    name: str
    extra: str  # the ingredient's note, or the line's details
    bare_quantity: bool
    new_name: str
    unit: str
    style: str

    def to_json(self) -> str:
        return json.dumps(astuple(self))

    @classmethod
    def from_json(cls, value: str) -> "Form":
        return cls(*json.loads(value))


@dataclass(frozen=True)
class Change:
    form: Form
    target: str  # a line id, or "<recipe id>/<ingredient id>"
    place: str  # the recipe's name, or where the line is
    item_id: int
    old: LineText | IngredientText
    new: LineText | IngredientText
    held: str = ""  # why it isn't written even once approved


NOT_KEPT = "AnyList didn't keep this rewrite when it was written before"


def combine(style: str, text: str) -> str:
    """`style` in front of a note or details, unless it's already there."""
    if not style or style.casefold() in text.casefold():
        return text
    return f"{style}, {text}" if text else style


def _bare(quantity: str) -> bool:
    """A number with no unit ("3", "1 ½"), so a unit can be added to it."""
    parsed = parse_quantity_and_package_size(quantity)
    return bool(parsed and parsed.quantityPb.amount and not parsed.quantityPb.unit)


def _with_unit(quantity: str, unit: str) -> str:
    """ "3" + "cloves" gives "3 cloves", and "1" + "cloves" gives "1 clove"."""
    amount = parse_quantity_and_package_size(quantity).quantityPb.amount
    return f"{quantity} {unit_for_amount(unit, amount_as_float(amount))}"


def compose(quantity: str, name: str, note: str) -> str:
    """The text a recipe shows for these fields, as AnyList's apps build it."""
    text = full_ingredient_string(PB.PBIngredient(quantity=quantity, name=name, note=note))
    return text.strip()


def _held_recipes(conn: sqlite3.Connection, snap: Snapshot) -> set[str]:
    ordered = {
        r[0]
        for r in conn.execute(
            "SELECT anylist_item_id FROM list_line WHERE ordered_from IS NOT NULL"
        )
    }
    return {rid for line in snap.lines if line.id in ordered for rid in line.recipe_ids}


def plan(
    conn: sqlite3.Connection, snap: Snapshot, overrides: Mapping[str, dict] | None = None
) -> list[Change]:
    """Every rewrite the canonical items call for, approved or not. `overrides` maps name
    keys to mappings ({item_id, item_name, unit, style}) that aren't saved yet, to preview a
    merge before making it."""
    refs = {
        r["external_id"]: dict(r)
        for r in conn.execute(
            """
            SELECT r.external_id, r.unit, r.style, i.id AS item_id, i.name AS item_name
            FROM external_ref r JOIN item i ON i.id = r.item_id
            WHERE r.source = 'anylist' AND i.canonical
            """
        )
    }
    refs.update(overrides or {})
    held = _held_recipes(conn, snap)
    written = {
        (r["target"], r["old"], r["new"])
        for r in conn.execute("SELECT target, old, new FROM rename_log")
    }

    def not_kept(target: str, old, new) -> bool:
        return (target, json.dumps(asdict(old)), json.dumps(asdict(new))) in written

    changes = []
    for ing in snap.ingredients:
        old = ing.text
        ref = refs.get(normalize(old.name))
        if ref is None or old.name == ref["item_name"]:
            continue
        bare = _bare(old.quantity)
        quantity = _with_unit(old.quantity, ref["unit"]) if ref["unit"] and bare else old.quantity
        note = combine(ref["style"], old.note)
        name = ref["item_name"]
        new = IngredientText(quantity, name, note, compose(quantity, name, note))
        form = Form("ingredient", old.name, old.note, bare, name, ref["unit"], ref["style"])
        target = f"{ing.recipe_id}/{ing.id}"
        reason = ""
        if not_kept(target, old, new):
            reason = NOT_KEPT
        elif ing.recipe_id in held:
            reason = "an ordered line comes from this recipe"
        changes.append(Change(form, target, ing.recipe_name, ref["item_id"], old, new, reason))
    for line in snap.lines:
        ref = refs.get(normalize(line.name))
        if ref is None or line.name == ref["item_name"] or line.recipe_ids:
            continue
        style = ", ".join(p for p in (ref["unit"], ref["style"]) if p)
        old = LineText(line.name, line.details)
        new = LineText(ref["item_name"], combine(style, line.details))
        form = Form("line", old.name, old.details, False, new.name, ref["unit"], ref["style"])
        place = "Shared List, crossed off" if line.checked else "Shared List"
        reason = NOT_KEPT if not_kept(line.id, old, new) else ""
        changes.append(Change(form, line.id, place, ref["item_id"], old, new, reason))
    return changes


def preview(
    conn: sqlite3.Connection,
    snap: Snapshot,
    item_id: int | None,
    item_name: str,
    names: list[tuple[str, str, str]],
) -> list[Change]:
    """The rewrites merging `names`, each (key, unit, style), into the item would make:
    theirs, plus respellings of the item's own name, since merging makes it canonical.
    `item_id` is None for an item that doesn't exist yet."""

    def ref(unit: str, style: str) -> dict:
        return {"item_id": item_id, "item_name": item_name, "unit": unit, "style": style}

    overrides = {normalize(item_name): ref("", "")}
    overrides |= {key: ref(unit.strip(), style.strip()) for key, unit, style in names}
    return [c for c in plan(conn, snap, overrides) if normalize(c.form.name) in overrides]


def approvals(conn: sqlite3.Connection) -> set[Form]:
    return {
        Form(r[0], r[1], r[2], bool(r[3]), r[4], r[5], r[6])
        for r in conn.execute(
            "SELECT kind, name, extra, bare_quantity, new_name, unit, style FROM rename_approval"
        )
    }


def approve(conn: sqlite3.Connection, forms: list[Form]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO rename_approval "
        "(kind, name, extra, bare_quantity, new_name, unit, style) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (f.kind, f.name, f.extra, int(f.bare_quantity), f.new_name, f.unit, f.style)
            for f in forms
        ],
    )


def _log(conn: sqlite3.Connection, changes: list[Change]) -> None:
    at = datetime.now(UTC).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO rename_log (at, kind, target, place, old, new) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                at,
                c.form.kind,
                c.target,
                c.place,
                json.dumps(asdict(c.old)),
                json.dumps(asdict(c.new)),
            )
            for c in changes
        ],
    )


async def apply(
    conn: sqlite3.Connection, backend: ListBackend, snap: Snapshot
) -> tuple[int, int, Snapshot]:
    """Write every approved, unheld change. Returns the ingredient and line counts written,
    and a snapshot taken after the writes (the one given if nothing was written)."""
    ok = approvals(conn)
    if not ok:
        return 0, 0, snap

    todo = [
        c for c in plan(conn, snap) if c.form.kind == "ingredient" and c.form in ok and not c.held
    ]
    ingredients = 0
    if todo:
        keys = {tuple(c.target.split("/", 1)): c for c in todo}
        done = await backend.update_ingredients({k: (c.old, c.new) for k, c in keys.items()})
        _log(conn, [keys[k] for k in done])
        ingredients = len(done)
        if done:
            snap = await backend.snapshot()

    todo = [c for c in plan(conn, snap) if c.form.kind == "line" and c.form in ok and not c.held]
    lines = 0
    if todo:
        by_id = {c.target: c for c in todo}
        done = await backend.update_lines({k: (c.old, c.new) for k, c in by_id.items()})
        _log(conn, [by_id[k] for k in done])
        lines = len(done)
        if done:
            snap = await backend.snapshot()
    if ingredients or lines:
        log.info("rewrote %d recipe ingredients and %d list lines", ingredients, lines)
    return ingredients, lines, snap


def record_names(conn: sqlite3.Connection, snap: Snapshot) -> None:
    """Count every name in AnyList, lines (crossed off too) and recipe ingredients."""
    spellings: dict[str, Counter] = defaultdict(Counter)
    lines: Counter = Counter()
    recipes: Counter = Counter()
    for line in snap.lines:
        if line.name.strip():
            spellings[normalize(line.name)][line.name.strip()] += 1
            lines[normalize(line.name)] += 1
    for ing in snap.ingredients:
        if ing.text.name.strip():
            spellings[normalize(ing.text.name)][ing.text.name.strip()] += 1
            recipes[normalize(ing.text.name)] += 1
    conn.execute("UPDATE anylist_name SET line_count = 0, recipe_count = 0")
    conn.executemany(
        """
        INSERT INTO anylist_name (key, label, spellings, line_count, recipe_count)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (key) DO UPDATE SET label = excluded.label, spellings = excluded.spellings,
          line_count = excluded.line_count, recipe_count = excluded.recipe_count
        """,
        [
            (
                key,
                counts.most_common(1)[0][0],
                json.dumps(sorted(counts)),
                lines[key],
                recipes[key],
            )
            for key, counts in spellings.items()
        ],
    )
