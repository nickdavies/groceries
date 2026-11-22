# groceries

Order-time companion to a shared AnyList list. AnyList stays where the household adds
things and sets stores. This app shows the whole list on one page, grouped by store,
with product links to add to each store's online cart, and remembers what has been
ordered.

- **The list.** Every open line appears once. "No store yet" comes first, as the list of
  lines still to sort out. Then a section per online store (its preferred store), then
  "In person", then "On the way". A line's other online stores list it greyed under
  "Also here", where it can be ticked to order it from that store instead.
- **Stores.** Each item has a preferred store and any number of "also available" stores,
  and AnyList holds the same set on the item's lines. Changing it in either place updates
  the other, and the newer change wins. A new line takes the item's stores, since a line
  re-added from Recents can carry stale ones. An item with no stores yet takes the line's.
- **Hand-pick.** Items flagged hand-pick (steaks, delicate produce) are never in an order
  list. They're listed under "In person" at their preferred store.
- **On the way.** "Mark ordered" is tracked only here. Check items off in AnyList when
  they arrive, and they drop off on the next sync. "Didn't arrive" puts a line back.
- **Items.** Every AnyList name becomes an item on first sight. AnyList item IDs change
  every shopping cycle, so lines match items by normalized name, and typos or variants
  are merged on the item page.
- **Products.** One product can serve several items (whole lemons for "lemon" and "lemon
  juice"). An item's first product at a store is its Product link on the list, and any
  others are in the menu beside it. King Soopers
  products come from the Kroger API search, and other stores take pasted links.
- **Other stores.** AnyList stores we don't order from online (H-Mart, Safeway) are kept in
  an item's store set and listed under "In person". A store's type and search link are
  set under Settings, and new stores can be added there or from the list (they're added
  to AnyList too).
- **Anywhere.** "Anywhere / no preference" is an explicit choice in the app for items
  bought wherever is convenient. AnyList leaves them unset, which the household already
  reads that way. Stores cleared in AnyList put the item back under "No store yet".
- **Notes.** An item can carry a short note shown next to its lines, e.g. which coffee
  shop.

## Develop

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Run locally against a real account (writes to that AnyList list once items have stores):

```sh
GROCERIES_DB=./dev.db op run --env-file dev.env -- uv run uvicorn groceries.app:create_app --factory
```

## Configuration

| Variable | |
|---|---|
| `ANYLIST_EMAIL`, `ANYLIST_PASSWORD` | AnyList login. Tokens are cached in the database after the first sign-in. |
| `ANYLIST_LIST` | List to manage (default `Shared List`) |
| `KROGER_CLIENT_ID`, `KROGER_CLIENT_SECRET` | Kroger developer app. Search is disabled if unset. The store whose prices it shows is picked under Settings. |
| `GROCERIES_DB` | SQLite path (default `/data/groceries.db`) |
| `SYNC_INTERVAL` | Seconds between background syncs (default 300) |

The app has no authentication of its own. It expects to sit behind a forward-auth proxy.
