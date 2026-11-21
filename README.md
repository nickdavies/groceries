# groceries

Routes items on a shared AnyList list to the stores we buy them from, and gives each store
a page of product links to add to its online cart.

- **Routing.** Each item has a ranked list of stores it may come from. The first store that
  isn't skipped wins, and the line is assigned to that store in AnyList. Hand-pick items
  (steaks, delicate produce) go to "<store> (in person)" instead, so AnyList's own store
  filter is the trip list.
- **Manual override.** If someone changes a line's store in the AnyList app, routing leaves
  that line alone. Clearing its store hands it back.
- **Three states.** AnyList only has checked/unchecked. Lines added to a cart move to the
  "On the way" store. "Delivered" checks them off. "Didn't arrive" puts them back.
- **Matching.** AnyList item IDs change every shopping cycle, so lines match our items by
  normalized name. Each item can have several AnyList names and several products per store.
  King Soopers products come from the Kroger API search. Other stores take pasted links.

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
