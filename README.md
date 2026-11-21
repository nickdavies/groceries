# groceries

Order-time companion to a shared AnyList list. AnyList stays where the household adds
things and sets stores. This app gives each store a page of product links to add to its
online cart, and remembers what has been ordered.

- **Stores.** Each item has a preferred store and any number of "also available" stores,
  and AnyList holds the same set on the item's lines. Changing it in either place updates
  the other, and the newer change wins. A new line takes the item's stores, since a line
  re-added from Recents can carry stale ones. An item with no stores yet takes the line's.
- **Store pages.** A line is listed under "To order" at its preferred store, and under
  "Also available here" at its other stores. Ordering it from any of them takes it off
  all of them.
- **Hand-pick.** Items flagged hand-pick (steaks, delicate produce) are never in an order
  list. They're listed under "Pick in person" at their preferred store.
- **On the way.** "Mark ordered" is tracked only here. Check items off in AnyList when
  they arrive, and they drop off on the next sync. "Didn't arrive" puts a line back.
- **Matching.** AnyList item IDs change every shopping cycle, so lines match our items by
  normalized name. Each item can have several AnyList names and several products per store.
  King Soopers products come from the Kroger API search. Other stores take pasted links.
- **Other stores.** AnyList stores we don't order from online (H-Mart, Safeway) are kept in
  an item's store set but get no page.

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
