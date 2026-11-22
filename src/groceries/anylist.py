"""The slice of AnyList the app uses, behind a small interface so sync logic is testable.

The app reads the list and writes one thing back: which stores each line has.
`ListBackend` is what sync talks to, and `AnyListBackend` implements it with aioanylist
against one named shopping list. AnyList stores are per-list, so store ids are only
meaningful within that list. Everything above this module speaks in store names.
"""

import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from aioanylist import AnyListClient, AuthenticationError, AuthTokens
from aioanylist.identifiers import uuid4_hex
from aioanylist.item_semantics import quantity_not_empty, quantity_to_deprecated_string
from aioanylist.proto import PBStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Line:
    """One item on the AnyList list."""

    id: str
    name: str
    quantity: str
    checked: bool
    stores: frozenset[str]  # store names


@dataclass(frozen=True)
class Snapshot:
    lines: list[Line]
    stores: list[str]  # every store name on the list, in AnyList's order


class ListBackend(Protocol):
    async def snapshot(self) -> Snapshot: ...

    async def set_stores(self, changes: Mapping[str, frozenset[str]]) -> None:
        """Make each line's stores exactly the named set, creating missing stores."""
        ...

    async def create_store(self, name: str) -> None:
        """Add a store to the list if it doesn't have one by that name."""
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
            )
            for i in lst.items
        ]
        ordered = sorted(stores.values(), key=lambda s: s.sortIndex)
        return Snapshot(lines=lines, stores=[s.name for s in ordered])

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


async def connect(conn: sqlite3.Connection, email: str, password: str) -> AnyListClient:
    """Sign in, reusing tokens cached in the database so restarts don't re-authenticate."""

    def save_tokens(tokens: AuthTokens | None) -> None:
        if tokens is None:
            conn.execute("DELETE FROM kv WHERE key = 'anylist_tokens'")
            return
        value = json.dumps(
            {
                "user_id": tokens.user_id,
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "is_premium_user": tokens.is_premium_user,
                "user_locale": tokens.user_locale,
            }
        )
        conn.execute(
            "INSERT INTO kv (key, value) VALUES ('anylist_tokens', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (value,),
        )

    row = conn.execute("SELECT value FROM kv WHERE key = 'anylist_tokens'").fetchone()
    if row is not None:
        client = AnyListClient(
            tokens=AuthTokens(**json.loads(row["value"])),
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
