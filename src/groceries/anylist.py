"""The slice of AnyList the app uses, behind a small interface so sync logic is testable.

The app reads the list and its recipes. It writes back each line's stores, and the
canonical-name rewrites a person approved (rename.py): line names and details, and recipe
ingredients' name, quantity and note. `ListBackend` is what sync talks to, and
`AnyListBackend` implements it with aioanylist against one named shopping list. AnyList
stores are per-list, so store ids are only meaningful within that list. Everything above
this module speaks in store names.
"""

import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from aioanylist import AnyListClient, AuthenticationError, AuthTokens
from aioanylist.identifiers import uuid4_hex
from aioanylist.item_semantics import quantity_not_empty, quantity_to_deprecated_string
from aioanylist.proto import PB, PBStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Line:
    """One item on the AnyList list."""

    id: str
    name: str
    quantity: str
    checked: bool
    stores: frozenset[str]  # store names
    details: str = ""
    recipe_ids: frozenset[str] = frozenset()  # recipes this line was added from


@dataclass(frozen=True)
class LineText:
    """The parts of a line a canonical-name rewrite shows and checks. Only the name and
    details are written; the quantity is shown so a preview can't hide a change to it."""

    name: str
    details: str
    quantity: str = ""


@dataclass(frozen=True)
class IngredientText:
    """The parts of a recipe ingredient a rewrite changes. `raw` is the text the recipe
    shows, which AnyList's editor writes as "{quantity} {name}, {note}"."""

    quantity: str
    name: str
    note: str
    raw: str


@dataclass(frozen=True)
class Ingredient:
    recipe_id: str
    recipe_name: str
    id: str
    text: IngredientText


@dataclass(frozen=True)
class Snapshot:
    lines: list[Line]
    stores: list[str]  # every store name on the list, in AnyList's order
    ingredients: list[Ingredient] = field(default_factory=list)  # headings left out


class ListBackend(Protocol):
    async def snapshot(self) -> Snapshot: ...

    async def set_stores(self, changes: Mapping[str, frozenset[str]]) -> None:
        """Make each line's stores exactly the named set, creating missing stores."""
        ...

    async def create_store(self, name: str) -> None:
        """Add a store to the list if it doesn't have one by that name."""
        ...

    async def update_lines(self, changes: Mapping[str, tuple[LineText, LineText]]) -> set[str]:
        """Rewrite lines from old to new text. A line whose text no longer matches the old
        one (someone edited it since) is skipped. Returns the line ids written."""
        ...

    async def update_ingredients(
        self, changes: Mapping[tuple[str, str], tuple[IngredientText, IngredientText]]
    ) -> set[tuple[str, str]]:
        """Rewrite (recipe id, ingredient id)s from old to new text, skipping any that no
        longer match the old text. Returns the ones written."""
        ...


def _quantity(item) -> str:
    if item.HasField("quantityPb") and quantity_not_empty(item.quantityPb):
        return quantity_to_deprecated_string(item.quantityPb)
    return str(getattr(item, "deprecatedQuantity", "") or "")


class AnyListBackend:
    def __init__(self, client: AnyListClient, list_name: str):
        self._client = client
        self._list_name = list_name

    def _list(self):
        matches = [x for x in self._client.lists.all() if x.name == self._list_name]
        if len(matches) != 1:
            raise RuntimeError(f"expected one AnyList list named {self._list_name!r}")
        return matches[0]

    def _store_map(self, list_id: str) -> dict[str, PBStore]:
        return self._client.state.list_stores.get(list_id, {})

    async def snapshot(self) -> Snapshot:
        await self._client.refresh()
        lst = self._list()
        stores = self._store_map(lst.identifier)
        lines = [
            Line(
                id=i.identifier,
                name=i.name,
                quantity=_quantity(i),
                checked=bool(i.checked),
                stores=frozenset(stores[s].name for s in i.storeIds if s in stores),
                details=i.details,
                recipe_ids=frozenset(x.recipeId for x in i.ingredients if x.recipeId),
            )
            for i in lst.items
        ]
        ingredients = [
            Ingredient(r.identifier, r.name, x.identifier, _ingredient_text(x))
            for r in self._client.state.recipes.values()
            for x in r.ingredients
            if not x.isHeading and x.identifier
        ]
        ordered = sorted(stores.values(), key=lambda s: s.sortIndex)
        return Snapshot(lines=lines, stores=[s.name for s in ordered], ingredients=ingredients)

    async def _ensure_stores(self, list_id: str, names: set[str]) -> dict[str, str]:
        """Store name -> id for the list, creating any of `names` it lacks (unflushed)."""
        by_name = {s.name: sid for sid, s in self._store_map(list_id).items()}
        for name in sorted(names - by_name.keys()):
            log.info("creating AnyList store %r", name)
            sid = uuid4_hex()
            await self._client.lists.save_store(
                list_id, PBStore(identifier=sid, name=name), is_new=True, flush=False
            )
            by_name[name] = sid
        return by_name

    async def create_store(self, name: str) -> None:
        await self._ensure_stores(self._list().identifier, {name})
        await self._client.flush()

    async def set_stores(self, changes: Mapping[str, frozenset[str]]) -> None:
        list_id = self._list().identifier
        by_name = await self._ensure_stores(list_id, set().union(*changes.values()))
        for line_id, names in changes.items():
            item = self._client.lists.item(list_id, line_id)
            if item is None:
                continue
            current = set(item.storeIds)
            wanted = {by_name[n] for n in names}
            for sid in current - wanted:
                await self._client.lists.remove_store(list_id, line_id, sid, flush=False)
            for sid in wanted - current:
                await self._client.lists.add_store(list_id, line_id, sid, flush=False)
        await self._client.flush()

    async def update_lines(self, changes: Mapping[str, tuple[LineText, LineText]]) -> set[str]:
        list_id = self._list().identifier
        written = set()
        for line_id, (old, new) in changes.items():
            item = self._client.lists.item(list_id, line_id)
            if item is None or LineText(item.name, item.details, _quantity(item)) != old:
                log.info("line %s changed since it was planned, skipping its rename", line_id)
                continue
            if new.name != old.name:
                await self._client.lists.rename_item(list_id, line_id, new.name, flush=False)
            if new.details != old.details:
                await self._client.lists.set_details(list_id, line_id, new.details, flush=False)
            written.add(line_id)
        await self._client.flush()
        return written

    async def update_ingredients(
        self, changes: Mapping[tuple[str, str], tuple[IngredientText, IngredientText]]
    ) -> set[tuple[str, str]]:
        by_recipe: dict[str, dict[str, tuple[IngredientText, IngredientText]]] = {}
        for (recipe_id, ingredient_id), change in changes.items():
            by_recipe.setdefault(recipe_id, {})[ingredient_id] = change
        written = set()
        for recipe_id, edits in by_recipe.items():
            current = self._client.state.recipes.get(recipe_id)
            if current is None:
                continue
            recipe = PB.PBRecipe()
            recipe.CopyFrom(current)
            changed = []
            for ingredient in recipe.ingredients:
                edit = edits.get(ingredient.identifier)
                if edit is None:
                    continue
                old, new = edit
                if _ingredient_text(ingredient) != old:
                    log.info(
                        "ingredient %s changed since it was planned, skipping",
                        ingredient.identifier,
                    )
                    continue
                ingredient.quantity, ingredient.name = new.quantity, new.name
                ingredient.note, ingredient.rawIngredient = new.note, new.raw
                changed.append((recipe_id, ingredient.identifier))
            if changed:
                # Saving runs aioanylist's recipe-edit hook, which moves open list lines
                # from these ingredients to the line for their new name and unit.
                await self._client.recipes.save(recipe, flush=False)
                written.update(changed)
        await self._client.flush()
        return written


class TokenStore(Protocol):
    """Where signed-in AnyList tokens are cached between runs, as JSON text."""

    def load(self) -> str | None: ...

    def save(self, value: str | None) -> None: ...


class DbTokens:
    """Tokens in the app database's kv table."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def load(self) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = 'anylist_tokens'").fetchone()
        return row["value"] if row else None

    def save(self, value: str | None) -> None:
        if value is None:
            self._conn.execute("DELETE FROM kv WHERE key = 'anylist_tokens'")
            return
        self._conn.execute(
            "INSERT INTO kv (key, value) VALUES ('anylist_tokens', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (value,),
        )


def _ingredient_text(ingredient) -> IngredientText:
    return IngredientText(
        ingredient.quantity, ingredient.name, ingredient.note, ingredient.rawIngredient
    )


async def connect(store: TokenStore, email: str, password: str) -> AnyListClient:
    """Sign in, reusing cached tokens so restarts don't re-authenticate."""

    def save_tokens(tokens: AuthTokens | None) -> None:
        if tokens is None:
            store.save(None)
            return
        value = {
            "user_id": tokens.user_id,
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "is_premium_user": tokens.is_premium_user,
            "user_locale": tokens.user_locale,
        }
        store.save(json.dumps(value))

    cached = store.load()
    if cached is not None:
        client = AnyListClient(
            tokens=AuthTokens(**json.loads(cached)),
            user_email=email,
            token_callback=save_tokens,
        )
        try:
            await client.load(load_tag_data=False)
            return client
        except AuthenticationError:
            log.info("cached AnyList tokens rejected, signing in again")
            await client.close()

    client = AnyListClient(token_callback=save_tokens)
    save_tokens(await client.sign_in(email, password))
    await client.load(load_tag_data=False)
    return client
