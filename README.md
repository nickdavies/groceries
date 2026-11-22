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
- **Names.** `/names` gives an item one name in AnyList. It lists every name in AnyList
  (crossed-off lines and recipe ingredients too), and suggests merges:
  - "likely the same": same words once prep words and plurals are set aside, or spelled
    almost like an item;
  - "contains another name": "garlic cloves" contains "garlic". Mark the wrong ones
    "Not the same", and they stay hidden.

  Compounds ("salt and pepper") and choices ("walnuts or chips") are listed separately.
  - **Mapping.** Mapping a name into an item makes the item *canonical*: its other names
    get rewritten to the item's name.
  - **Unit and style.** Each mapped name carries an optional unit and style.
    - The unit joins a bare-number quantity: "3 garlic cloves" becomes "3 cloves Garlic".
    - The style goes in front of the ingredient's note or the line's details: "minced
      shallot" becomes "Shallot, minced".
    - A recipe's display text is rebuilt as "{quantity} {name}, {note}", the form
      AnyList's editor writes. Scaling uses the quantity, not this text.
  - **Review.** Each rewrite is shown with its exact before and after, and nothing is
    written until it is approved.
    - An approval covers one exact source (name, note or details, and whether the quantity
      was a bare number) and one exact result. Later matches of that pair are written on
      sync.
    - Anything else waits for review: a different note, a different spelling, an edited
      mapping or a renamed item.
  - **Writing.** Recipes are written before list lines, so AnyList moves a recipe's open
    lines to the new name and merges lines with the same unit.
    - A recipe with an ordered line waits until that line is checked off, because moving
      the line would lose its "On the way" state.
    - Every rewrite is logged with its full old and new text.

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

## Recipe backup

`python -m groceries.backup` saves every AnyList recipe the account can see, with its
photos, to a volume of its own. It is meant to run every 30 minutes as a CronJob, with
VolSync backing up the volume. It only reads from AnyList.

```
recipes/<recipe id>/<sha256>.json   one file per distinct version of a recipe (full PBRecipe)
collections/<sha256>.json           recipe collections and their order
photos/<sha256>.jpg                 photos, from https://photos.anylist.com/<photo id>.jpg
photos/index.json                   photo id -> sha256
tokens.json                         cached AnyList sign-in (mode 0600)
```

- **Content-addressed.** A file is named by the sha256 of its bytes. A recipe file is
  canonical JSON (`MessageToDict` with field names kept, then sorted keys). An unchanged
  recipe writes nothing.
- **Verified.** Every run re-hashes the files it relies on, and rewrites (or downloads
  again) any that don't match. It then re-hashes every stored file. Mismatches are logged
  and counted in `hash_failures`.
- **Pruned.** A file's mtime is the last time it was current. Versions that haven't been
  current for `PRUNE_DAYS` are deleted, but never a recipe's newest file, so a recipe
  deleted in AnyList keeps its last version. Photos that no remaining recipe file uses are
  deleted too.
- **Reported.** After everything else succeeds, the run pushes
  `groceries_recipe_backup_*` gauges to the Pushgateway, job `groceries_recipe_backup`.
  These are `recipes`, `photos`, `files_written`, `pruned`, `hash_failures` and
  `last_success_timestamp_seconds`. A failed run pushes nothing and exits non-zero.

| Variable | |
|---|---|
| `ANYLIST_EMAIL`, `ANYLIST_PASSWORD` | AnyList login |
| `BACKUP_DIR` | Backup root (default `/backup`) |
| `PRUNE_DAYS` | Days an old version is kept (default 90) |
| `PUSHGATEWAY_URL` | Pushgateway to report to. Unset, success isn't reported. |

### Restoring (planned, not implemented)

There is no restore command yet. The intended procedure, if it's ever needed:

1. Get the files.
   - From the live volume: start a short-lived pod that mounts the backup PVC (it's RWO
     and only attached while a Job runs), then `kubectl cp` out.
   - From history: restore a VolSync snapshot.
2. For the recipe id, take the newest `recipes/<id>/*.json` by mtime. If you're working
   from a copy that lost mtimes, look at each version's `timestamp` field. Check that
   `sha256sum` matches the file name.
3. `ParseDict(json.load(f), PB.PBRecipe())`, then `await client.recipes.save(recipe)`. That
   saves it under the same id, overwriting the current recipe or recreating a deleted one.
   Diff the ingredients against the live recipe before saving.
4. Re-add photos by hand from `photos/<index[photo id]>.jpg`. `save` doesn't upload them.
