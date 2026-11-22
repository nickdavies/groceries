"""Mirror the AnyList list, keep item store sets in step with it, and track orders.

An item's store set and its AnyList lines' stores are synced both ways, and the latest
change wins. AnyList keeps no modification times, so "latest" comes from ordering:
  - Each line records the stores last seen or written. If AnyList differs at the next
    sync, a person changed them there, and the item takes the line's set.
  - The web UI syncs before applying a change (picking up any AnyList edits first),
    then writes the item's set to its open lines. That makes the UI change the latest.
  - A new line takes the item's set if it has one, because a line re-added from Recents
    carries whatever stores it had when that Recent was made. An item with no stores
    yet takes the line's.
Lines that have been ordered are left alone. Which store a line was ordered from lives
only here, and the line drops out when someone checks it off in AnyList.

Every name on an open line gets an item the first time it is seen. Names that mean the
same thing ("Avocado", "Avocados") are combined afterwards with `merge_items` or
`map_name`. Each sync also records every name in AnyList (crossed-off lines and recipes
too) for /names, then writes the approved canonical-name rewrites (see rename.py).
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import rename
from .anylist import ListBackend, Snapshot
from .normalize import normalize


@dataclass
class SyncReport:
    lines: int = 0
    dropped: int = 0
    adopted: int = 0  # items whose stores were taken from AnyList
    written: int = 0  # lines whose stores were written to AnyList
    renamed_ingredients: int = 0
    renamed_lines: int = 0
    snapshot: Snapshot | None = field(default=None, repr=False)  # AnyList after this sync


class NameTaken(Exception):
    """An item's name is already another item's AnyList name. Merge them instead."""


def item_for_name(conn: sqlite3.Connection, name: str) -> int:
    """The item an AnyList name maps to, creating it on first sight."""
    key = normalize(name)
    row = conn.execute(
        "SELECT item_id FROM external_ref WHERE source = 'anylist' AND external_id = ?", (key,)
    ).fetchone()
    if row:
        return row["item_id"]
    label = name.strip()
    existing = conn.execute("SELECT id FROM item WHERE name = ?", (label,)).fetchone()
    item_id = (
        existing["id"]
        if existing
        else conn.execute("INSERT INTO item (name) VALUES (?)", (label,)).lastrowid
    )
    conn.execute(
        "INSERT INTO external_ref (source, external_id, item_id) VALUES ('anylist', ?, ?)",
        (key, item_id),
    )
    return item_id


def make_canonical(conn: sqlite3.Connection, item_id: int) -> None:
    """Mark an item's names as curated, so its AnyList names are rewritten to its name.
    Its own name becomes one of its AnyList names, so renamed lines still match it."""
    (name,) = conn.execute("SELECT name FROM item WHERE id = ?", (item_id,)).fetchone()
    key = normalize(name)
    row = conn.execute(
        "SELECT item_id FROM external_ref WHERE source = 'anylist' AND external_id = ?", (key,)
    ).fetchone()
    if row is not None and row["item_id"] != item_id:
        raise NameTaken(name)
    conn.execute(
        "INSERT OR IGNORE INTO external_ref (source, external_id, item_id) "
        "VALUES ('anylist', ?, ?)",
        (key, item_id),
    )
    conn.execute("UPDATE item SET canonical = 1 WHERE id = ?", (item_id,))


def map_name(
    conn: sqlite3.Connection, key: str, target: int, unit: str = "", style: str = ""
) -> None:
    """Make the AnyList name `key` an alias of `target`, and `target` canonical. If the
    name already belongs to another item, that item is merged into `target`."""
    row = conn.execute(
        "SELECT item_id FROM external_ref WHERE source = 'anylist' AND external_id = ?", (key,)
    ).fetchone()
    if row is not None and row["item_id"] != target:
        merge_items(conn, row["item_id"], target)
    conn.execute(
        """
        INSERT INTO external_ref (source, external_id, item_id, unit, style)
        VALUES ('anylist', ?, ?, ?, ?)
        ON CONFLICT (source, external_id) DO UPDATE SET
          item_id = excluded.item_id, unit = excluded.unit, style = excluded.style
        """,
        (key, target, unit.strip(), style.strip()),
    )
    make_canonical(conn, target)


def split_off(conn: sqlite3.Connection, item_id: int, key: str) -> None:
    """Stop an alias mapping to its item. The item's own name can't be split off."""
    (name,) = conn.execute("SELECT name FROM item WHERE id = ?", (item_id,)).fetchone()
    if key != normalize(name):
        conn.execute(
            "DELETE FROM external_ref WHERE source = 'anylist' AND external_id = ? AND item_id = ?",
            (key, item_id),
        )


def merge_items(conn: sqlite3.Connection, source: int, target: int) -> None:
    """Fold `source` into `target`: its AnyList names, products and stores move over,
    and `target` keeps its own name, preferred store and hand-pick flag."""
    if source == target:
        return
    conn.execute("BEGIN")
    try:
        conn.execute("UPDATE external_ref SET item_id = ? WHERE item_id = ?", (target, source))
        conn.execute(
            "INSERT OR IGNORE INTO item_product (item_id, product_id, priority, enabled) "
            "SELECT ?, product_id, priority, enabled FROM item_product WHERE item_id = ?",
            (target, source),
        )
        has_preferred = conn.execute(
            "SELECT 1 FROM item_store WHERE item_id = ? AND preferred", (target,)
        ).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO item_store (item_id, store_id, preferred) "
            "SELECT ?, store_id, preferred AND ? FROM item_store WHERE item_id = ?",
            (target, has_preferred is None, source),
        )
        # The source's lines are re-linked by the next sync, which treats them as newly
        # matched, so they pick up the combined store set.
        conn.execute("DELETE FROM item WHERE id = ?", (source,))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def item_stores(conn: sqlite3.Connection, item_id: int) -> frozenset[str]:
    rows = conn.execute(
        "SELECT s.name FROM item_store i JOIN store s ON s.id = i.store_id WHERE i.item_id = ?",
        (item_id,),
    )
    return frozenset(r[0] for r in rows)


def set_item_stores(
    conn: sqlite3.Connection,
    item_id: int,
    names: Iterable[str],
    preferred: str | None = None,
    anywhere: bool = False,
) -> None:
    """Replace an item's store set. With no `preferred` given, the current preferred
    store is kept if it is still in the set, else the first store in store order.

    `anywhere` is only ever passed by an explicit choice in the app. Any other change,
    including stores cleared in AnyList, leaves the item unmarked, so an item with no
    stores goes back to the "No store yet" list.
    """
    names = set(names)
    if preferred is None:
        row = conn.execute(
            "SELECT s.name FROM item_store i JOIN store s ON s.id = i.store_id "
            "WHERE i.item_id = ? AND i.preferred",
            (item_id,),
        ).fetchone()
        preferred = row[0] if row and row[0] in names else None
    rows = conn.execute(
        "SELECT id, name FROM store WHERE name IN (SELECT value FROM json_each(?)) ORDER BY sort",
        (json.dumps(sorted(names)),),
    ).fetchall()
    if rows and preferred not in names:
        preferred = rows[0]["name"]
    conn.execute("DELETE FROM item_store WHERE item_id = ?", (item_id,))
    conn.executemany(
        "INSERT INTO item_store (item_id, store_id, preferred) VALUES (?, ?, ?)",
        [(item_id, r["id"], int(r["name"] == preferred)) for r in rows],
    )
    conn.execute("UPDATE item SET anywhere = ? WHERE id = ?", (int(anywhere and not rows), item_id))


def _mirror_stores(conn: sqlite3.Connection, names: list[str]) -> None:
    """Add AnyList stores we don't know yet. They're in-person only until said otherwise."""
    (next_sort,) = conn.execute("SELECT coalesce(max(sort) + 1, 0) FROM store").fetchone()
    for name in names:
        if conn.execute("SELECT 1 FROM store WHERE name = ?", (name,)).fetchone() is None:
            conn.execute(
                "INSERT INTO store (name, kind, sort) VALUES (?, 'offline', ?)", (name, next_sort)
            )
            next_sort += 1


async def sync(conn: sqlite3.Connection, backend: ListBackend) -> SyncReport:
    snap = await backend.snapshot()
    live = [line for line in snap.lines if not line.checked]
    live_ids = {line.id for line in live}
    report = SyncReport(lines=len(live))
    writes: dict[str, frozenset[str]] = {}

    conn.execute("BEGIN")
    try:
        _mirror_stores(conn, snap.stores)
        rename.record_names(conn, snap)
        known = [r[0] for r in conn.execute("SELECT anylist_item_id FROM list_line")]
        gone = [(i,) for i in known if i not in live_ids]
        conn.executemany("DELETE FROM list_line WHERE anylist_item_id = ?", gone)
        report.dropped = len(gone)

        for line in live:
            prev = conn.execute(
                "SELECT * FROM list_line WHERE anylist_item_id = ?", (line.id,)
            ).fetchone()
            item_id = item_for_name(conn, line.name)
            target = _reconcile(conn, prev, item_id, line.stores, report)
            if target != line.stores:
                writes[line.id] = target
            # Record what AnyList has now. The write below updates it once it lands, so a
            # failed write is retried next time rather than mistaken for a person's edit.
            conn.execute(
                """
                INSERT INTO list_line (anylist_item_id, name, quantity, item_id, stores)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (anylist_item_id) DO UPDATE SET
                    name = excluded.name, quantity = excluded.quantity,
                    item_id = excluded.item_id, stores = excluded.stores
                """,
                (line.id, line.name, line.quantity, item_id, json.dumps(sorted(line.stores))),
            )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    await _write(conn, backend, writes)
    report.written = len(writes)
    report.renamed_ingredients, report.renamed_lines, snap = await rename.apply(conn, backend, snap)
    report.snapshot = snap
    return report


def _reconcile(conn, prev, item_id: int, anylist: frozenset[str], report) -> frozenset[str]:
    """Decide a line's stores, updating the item's set if AnyList wins."""
    if prev is not None and prev["ordered_from"] is not None:
        return anylist
    mine = item_stores(conn, item_id)
    if prev is None or prev["item_id"] != item_id:  # a new line, or newly matched
        if mine:
            return mine
    elif anylist == frozenset(json.loads(prev["stores"])):  # unchanged in AnyList
        return mine
    # Otherwise a person changed it in AnyList, or the item has no stores yet.
    if anylist != mine:
        set_item_stores(conn, item_id, anylist)
        report.adopted += 1
    return anylist


async def _write(conn, backend: ListBackend, writes: dict[str, frozenset[str]]) -> None:
    if not writes:
        return
    await backend.set_stores(writes)
    conn.executemany(
        "UPDATE list_line SET stores = ? WHERE anylist_item_id = ?",
        [(json.dumps(sorted(names)), line_id) for line_id, names in writes.items()],
    )


async def push_item_stores(conn: sqlite3.Connection, backend: ListBackend, item_id: int) -> None:
    """Write an item's store set to its open, unordered lines."""
    mine = item_stores(conn, item_id)
    rows = conn.execute(
        "SELECT anylist_item_id, stores FROM list_line WHERE item_id = ? AND ordered_from IS NULL",
        (item_id,),
    ).fetchall()
    writes = {
        r["anylist_item_id"]: mine for r in rows if frozenset(json.loads(r["stores"])) != mine
    }
    await _write(conn, backend, writes)


def mark_ordered(conn: sqlite3.Connection, store_id: int, ids: Iterable[str]) -> None:
    conn.executemany(
        "UPDATE list_line SET ordered_from = ? WHERE anylist_item_id = ? AND ordered_from IS NULL",
        [(store_id, i) for i in ids],
    )


def mark_not_received(conn: sqlite3.Connection, ids: Iterable[str]) -> None:
    conn.executemany(
        "UPDATE list_line SET ordered_from = NULL WHERE anylist_item_id = ?", [(i,) for i in ids]
    )
