"""The slice of AnyList the app uses, behind a small interface so sync logic is testable.

`ListBackend` is what sync talks to. `AnyListBackend` implements it with aioanylist
against one named shopping list. AnyList stores are per-list, so store ids are only
meaningful within that list.
"""

import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping
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
    store_ids: frozenset[str]


@dataclass(frozen=True)
class Snapshot:
    lines: list[Line]
    stores: dict[str, str]  # AnyList store id -> store name


class ListBackend(Protocol):
    async def snapshot(self) -> Snapshot: ...

    async def ensure_stores(self, names: Iterable[str]) -> dict[str, str]:
        """Create any missing stores. Returns name -> id for every store on the list."""
        ...

    async def set_store_ids(self, changes: Mapping[str, frozenset[str]]) -> None:
        """Make each line's store set exactly the given set."""
        ...

    async def check_off(self, line_ids: Iterable[str]) -> None: ...


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

    def _stores(self, list_id: str) -> dict[str, str]:
        return {sid: s.name for sid, s in self._client.state.list_stores.get(list_id, {}).items()}

    async def snapshot(self) -> Snapshot:
        await self._client.refresh()
        lst = self._list()
        lines = [
            Line(
                id=i.identifier,
                name=i.name,
                quantity=_quantity(i),
                checked=bool(i.checked),
                store_ids=frozenset(i.storeIds),
            )
            for i in lst.items
        ]
        return Snapshot(lines=lines, stores=self._stores(lst.identifier))

    async def ensure_stores(self, names: Iterable[str]) -> dict[str, str]:
        list_id = self._list().identifier
        by_name = {name: sid for sid, name in self._stores(list_id).items()}
        for name in sorted(set(names) - by_name.keys()):
            log.info("creating AnyList store %r", name)
            await self._client.lists.save_store(
                list_id, PBStore(identifier=uuid4_hex(), name=name), is_new=True, flush=False
            )
        await self._client.flush()
        return {name: sid for sid, name in self._stores(list_id).items()}

    async def set_store_ids(self, changes: Mapping[str, frozenset[str]]) -> None:
        list_id = self._list().identifier
        for line_id, wanted in changes.items():
            item = self._client.lists.item(list_id, line_id)
            if item is None:
                continue
            current = set(item.storeIds)
            for sid in current - wanted:
                await self._client.lists.remove_store(list_id, line_id, sid, flush=False)
            for sid in wanted - current:
                await self._client.lists.add_store(list_id, line_id, sid, flush=False)
        await self._client.flush()

    async def check_off(self, line_ids: Iterable[str]) -> None:
        ids = list(line_ids)
        if ids:
            await self._client.lists.bulk_set_checked(self._list().identifier, ids, True)


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
