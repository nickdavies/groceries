"""SQLite storage: schema, migrations and the connection used by the whole app.

The app is single-process and single-threaded (async on one event loop), so one shared
connection is enough. Migrations are an append-only list keyed by `PRAGMA user_version`.
"""

import sqlite3
from pathlib import Path

MIGRATIONS = [
    """
    -- Stores the household buys from. `name` is also the AnyList store name.
    -- `kind` decides how products are found: 'kroger' searches the Kroger API,
    -- 'manual' means someone pastes product links. `active` lets the household skip a
    -- store for a while; routing then falls through to the item's next-ranked store.
    CREATE TABLE store (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('kroger', 'manual')),
        active INTEGER NOT NULL DEFAULT 1,
        sort INTEGER NOT NULL
    );
    INSERT INTO store (name, kind, sort) VALUES
        ('King Soopers', 'kroger', 0),
        ('Costco', 'manual', 1),
        ('Natural Grocers', 'manual', 2),
        ('Total Beverage', 'manual', 3);

    -- Our canonical item. AnyList item IDs change every shopping cycle, so this is the
    -- stable identity everything else hangs off.
    CREATE TABLE item (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        hand_pick INTEGER NOT NULL DEFAULT 0
    );

    -- Translations from outside identifiers into an item. For AnyList the external id is
    -- the normalized item name; several rows give one item several aliases.
    CREATE TABLE external_ref (
        source TEXT NOT NULL,
        external_id TEXT NOT NULL,
        item_id INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
        PRIMARY KEY (source, external_id)
    );

    -- Concrete products that satisfy an item at a store, best first. `upc` is set for
    -- Kroger products and drives the cart API later.
    CREATE TABLE product (
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
        store_id INTEGER NOT NULL REFERENCES store(id) ON DELETE CASCADE,
        label TEXT NOT NULL,
        url TEXT NOT NULL,
        upc TEXT,
        priority INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        UNIQUE (item_id, store_id, url)
    );

    -- Which stores an item may come from. Lowest rank wins among active stores.
    CREATE TABLE store_rule (
        item_id INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
        store_id INTEGER NOT NULL REFERENCES store(id) ON DELETE CASCADE,
        rank INTEGER NOT NULL,
        PRIMARY KEY (item_id, store_id)
    );

    -- One row per unchecked line on the AnyList list. Checked or deleted lines are
    -- dropped: AnyList's checked state is the "done" leg of needed / en route / done.
    --   store_id, in_person: where the line is routed (or where it was ordered from)
    --   manual: a human set the store in AnyList; routing leaves the line alone
    --   last_written: JSON list of AnyList store ids we last wrote, NULL if never
    CREATE TABLE list_entry (
        anylist_item_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        quantity TEXT NOT NULL DEFAULT '',
        item_id INTEGER REFERENCES item(id) ON DELETE SET NULL,
        state TEXT NOT NULL CHECK (state IN ('needed', 'en_route')),
        store_id INTEGER REFERENCES store(id) ON DELETE SET NULL,
        in_person INTEGER NOT NULL DEFAULT 0,
        manual INTEGER NOT NULL DEFAULT 0,
        last_written TEXT
    );

    -- Small runtime state, e.g. cached AnyList auth tokens.
    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        # The script carries its own transaction so a failed migration leaves no trace.
        try:
            conn.executescript(f"BEGIN;\n{sql};\nPRAGMA user_version = {i};\nCOMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
