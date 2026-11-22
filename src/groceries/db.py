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
    """
    -- Stores mirror the AnyList list's stores. 'offline' ones (bought in person only,
    -- e.g. H-Mart) get no page, but keep an item's store set the same as AnyList's.
    CREATE TABLE store_v2 (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('kroger', 'manual', 'offline')),
        sort INTEGER NOT NULL
    );
    INSERT INTO store_v2 (id, name, kind, sort) SELECT id, name, kind, sort FROM store;
    DROP TABLE store;
    ALTER TABLE store_v2 RENAME TO store;

    -- The stores an item can come from, with at most one marked preferred (the code
    -- keeps exactly one while the set is non-empty). The same set is written to the
    -- item's AnyList lines, and a change on either side wins over the other.
    CREATE TABLE item_store (
        item_id INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
        store_id INTEGER NOT NULL REFERENCES store(id) ON DELETE CASCADE,
        preferred INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (item_id, store_id)
    );
    CREATE UNIQUE INDEX item_store_one_preferred ON item_store (item_id) WHERE preferred;
    INSERT INTO item_store (item_id, store_id, preferred)
        SELECT item_id, store_id,
               row_number() OVER (PARTITION BY item_id ORDER BY rank, store_id) = 1
        FROM store_rule;
    DROP TABLE store_rule;

    -- One row per unchecked line on the AnyList list.
    --   stores: JSON list of the line's AnyList store names as last seen or written.
    --     If AnyList differs at the next sync, a person changed them there.
    --   ordered_from: the store it was ordered from, NULL while still needed. The
    --     line drops out when someone checks it off in AnyList.
    CREATE TABLE list_line (
        anylist_item_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        quantity TEXT NOT NULL DEFAULT '',
        item_id INTEGER REFERENCES item(id) ON DELETE SET NULL,
        stores TEXT NOT NULL DEFAULT '[]',
        ordered_from INTEGER REFERENCES store(id) ON DELETE SET NULL
    );
    INSERT INTO list_line (anylist_item_id, name, quantity, item_id, ordered_from)
        SELECT anylist_item_id, name, quantity, item_id,
               CASE WHEN state = 'en_route' THEN store_id END
        FROM list_entry;
    DROP TABLE list_entry;
    """,
    """
    -- Products stand alone so one product can serve several items (whole lemons for
    -- both "lemon" and "lemon juice"). item_product carries each item's own choice
    -- order and on/off for a product. An item with several enabled products at a store
    -- is shown as alternatives: buy one of them.
    CREATE TABLE product_v2 (
        id INTEGER PRIMARY KEY,
        store_id INTEGER NOT NULL REFERENCES store(id) ON DELETE CASCADE,
        label TEXT NOT NULL,
        url TEXT NOT NULL,
        upc TEXT,
        UNIQUE (store_id, url)
    );
    INSERT INTO product_v2 (id, store_id, label, url, upc)
        SELECT min(id), store_id, label, url, upc FROM product GROUP BY store_id, url;
    CREATE TABLE item_product (
        item_id INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
        product_id INTEGER NOT NULL REFERENCES product(id) ON DELETE CASCADE,
        priority INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (item_id, product_id)
    );
    INSERT INTO item_product (item_id, product_id, priority, enabled)
        SELECT p.item_id, v.id, p.priority, p.enabled
        FROM product p JOIN product_v2 v ON v.store_id = p.store_id AND v.url = p.url;
    DROP TABLE product;
    ALTER TABLE product_v2 RENAME TO product;

    -- Link to a store's own site search, with {} where the search term goes. Lines
    -- without a product get it, so no line is a dead end. Editable under Settings.
    ALTER TABLE store ADD COLUMN search_url TEXT;
    UPDATE store SET search_url = 'https://www.kingsoopers.com/search?query={}'
        WHERE name = 'King Soopers';
    UPDATE store SET search_url = 'https://sameday.costco.com/store/costco/s?k={}'
        WHERE name = 'Costco';
    UPDATE store SET search_url = 'https://totalbev.com/shop/?ch-query={}'
        WHERE name = 'Total Beverage';
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
    if version == len(MIGRATIONS):
        return
    # Rebuilding a table means dropping it, which with foreign keys on would cascade
    # into the tables that reference it. They can only be switched outside a
    # transaction, and are checked once all migrations have run.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            # The script carries its own transaction so a failed migration leaves no trace.
            try:
                conn.executescript(f"BEGIN;\n{sql};\nPRAGMA user_version = {i};\nCOMMIT;")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"foreign key violations after migrating: {broken}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
