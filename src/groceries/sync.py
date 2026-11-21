"""Reconcile the AnyList list with the database and route lines to stores.

AnyList only has checked/unchecked, so the three-way state is split:
  needed    unchecked, sitting at its routed store (or unrouted, awaiting review)
  en route  unchecked, moved to the "On the way" AnyList store
  done      checked in AnyList, at which point the line is dropped from the database

Manual overrides: we remember which AnyList stores we expect each line to have. If AnyList
disagrees, a human moved it, and the line becomes `manual` (routing leaves it alone) until
someone clears its stores again.
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

from .anylist import Line, ListBackend
from .normalize import normalize

ON_THE_WAY = "On the way"


def in_person_name(store_name: str) -> str:
    return f"{store_name} (in person)"


@dataclass(frozen=True)
class Placement:
    store_id: int
    in_person: bool


@dataclass
class SyncReport:
    lines: int = 0
    writes: dict[str, list[str]] = field(default_factory=dict)  # line name -> store names
    unrouted: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    dropped: int = 0


@dataclass
class _Stores:
    """Our stores plus the names AnyList uses for each placement."""

    rows: dict[int, sqlite3.Row]

    def anylist_name(self, p: Placement) -> str:
        name = self.rows[p.store_id]["name"]
        return in_person_name(name) if p.in_person else name

    def placement_for(self, anylist_name: str) -> Placement | None:
        for sid, row in self.rows.items():
            if anylist_name == row["name"]:
                return Placement(sid, False)
            if anylist_name == in_person_name(row["name"]):
                return Placement(sid, True)
        return None


def route(conn: sqlite3.Connection, item_id: int | None) -> Placement | None:
    """Highest-ranked active store for an item, or None if it can't be routed."""
    if item_id is None:
        return None
    row = conn.execute(
        """
        SELECT r.store_id, i.hand_pick
        FROM store_rule r
        JOIN store s ON s.id = r.store_id
        JOIN item i ON i.id = r.item_id
        WHERE r.item_id = ? AND s.active
        ORDER BY r.rank, s.sort
        LIMIT 1
        """,
        (item_id,),
    ).fetchone()
    return Placement(row["store_id"], bool(row["hand_pick"])) if row else None


def _match(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute(
        "SELECT item_id FROM external_ref WHERE source = 'anylist' AND external_id = ?",
        (normalize(name),),
    ).fetchone()
    return row["item_id"] if row else None


async def sync(conn: sqlite3.Connection, backend: ListBackend, *, dry_run: bool = False):
    snap = await backend.snapshot()
    stores = _Stores({r["id"]: r for r in conn.execute("SELECT * FROM store")})
    id_to_name = dict(snap.stores)
    report = SyncReport()

    conn.execute("BEGIN")
    try:
        live = [line for line in snap.lines if not line.checked]
        report.lines = len(live)
        report.dropped = _drop_missing(conn, {line.id for line in live})
        wanted: dict[str, frozenset[str]] = {}  # line id -> AnyList store names
        for line in live:
            names = _reconcile(conn, stores, line, id_to_name, report)
            if names is not None:
                wanted[line.id] = names
        if dry_run:
            conn.execute("ROLLBACK")
            return report
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    if wanted:
        name_to_id = await backend.ensure_stores(set().union(*wanted.values()))
        await backend.set_store_ids(
            {lid: frozenset(name_to_id[n] for n in names) for lid, names in wanted.items()}
        )
    return report


def _drop_missing(conn: sqlite3.Connection, live_ids: set[str]) -> int:
    ids = [r[0] for r in conn.execute("SELECT anylist_item_id FROM list_entry")]
    gone = [i for i in ids if i not in live_ids]
    conn.executemany("DELETE FROM list_entry WHERE anylist_item_id = ?", [(i,) for i in gone])
    return len(gone)


def _reconcile(
    conn: sqlite3.Connection,
    stores: _Stores,
    line: Line,
    id_to_name: dict[str, str],
    report: SyncReport,
) -> frozenset[str] | None:
    """Update one line's entry. Returns the AnyList store names to write, if any change."""
    current = frozenset(id_to_name.get(sid, sid) for sid in line.store_ids)
    item_id = _match(conn, line.name)
    entry = conn.execute(
        "SELECT * FROM list_entry WHERE anylist_item_id = ?", (line.id,)
    ).fetchone()

    if entry is None:
        state = "needed"
        # A line re-added from Recents can carry last cycle's "On the way". Anything
        # else already on the line at first sight was put there by a person.
        manual = bool(current - {ON_THE_WAY})
    else:
        state, manual = entry["state"], bool(entry["manual"])
        expected = frozenset(json.loads(entry["last_written"]))
        if current != expected:
            manual = bool(current)
            if state == "en_route":
                state = "needed"

    placement: Placement | None
    write: frozenset[str] | None = None
    if state == "en_route":
        placement = (
            Placement(entry["store_id"], bool(entry["in_person"])) if entry["store_id"] else None
        )
        target = frozenset({ON_THE_WAY})
    elif manual:
        placement = next(filter(None, map(stores.placement_for, sorted(current))), None)
        target = current
        report.manual.append(line.name)
    else:
        placement = route(conn, item_id)
        target = frozenset({stores.anylist_name(placement)}) if placement else current
        if placement is None:
            report.unrouted.append(line.name)
    if target != current:
        write = target
        report.writes[line.name] = sorted(target)

    conn.execute(
        """
        INSERT INTO list_entry (anylist_item_id, name, quantity, item_id, state, store_id,
                                in_person, manual, last_written)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (anylist_item_id) DO UPDATE SET
            name = excluded.name, quantity = excluded.quantity, item_id = excluded.item_id,
            state = excluded.state, store_id = excluded.store_id,
            in_person = excluded.in_person, manual = excluded.manual,
            last_written = excluded.last_written
        """,
        (
            line.id,
            line.name,
            line.quantity,
            item_id,
            state,
            placement.store_id if placement else None,
            int(placement.in_person) if placement else 0,
            int(manual),
            json.dumps(sorted(target)),
        ),
    )
    return write


async def mark_en_route(conn: sqlite3.Connection, backend: ListBackend, ids: Iterable[str]):
    """The household added these lines to a store's cart."""
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT anylist_item_id FROM list_entry WHERE state = 'needed' "
            "AND anylist_item_id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(ids)),),
        )
    ]
    if not ids:
        return
    conn.executemany(
        "UPDATE list_entry SET state = 'en_route', last_written = ? WHERE anylist_item_id = ?",
        [(json.dumps([ON_THE_WAY]), i) for i in ids],
    )
    name_to_id = await backend.ensure_stores([ON_THE_WAY])
    await backend.set_store_ids({i: frozenset({name_to_id[ON_THE_WAY]}) for i in ids})


async def mark_delivered(conn: sqlite3.Connection, backend: ListBackend, ids: Iterable[str]):
    ids = list(ids)
    await backend.check_off(ids)
    conn.executemany("DELETE FROM list_entry WHERE anylist_item_id = ?", [(i,) for i in ids])


async def mark_not_received(conn: sqlite3.Connection, backend: ListBackend, ids: Iterable[str]):
    """Back to needed. Routing puts the line back at its store on the next sync."""
    conn.executemany(
        "UPDATE list_entry SET state = 'needed', manual = 0 WHERE anylist_item_id = ?",
        [(i,) for i in ids],
    )
    await sync(conn, backend)
