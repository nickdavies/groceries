from collections.abc import Iterable, Mapping

from groceries.anylist import Line, Snapshot
from groceries.kroger import Location, Product


class FakeKroger:
    """Kroger API stand-in. Records which store each search was scoped to."""

    def __init__(self):
        self.searched_at: list[str] = []

    async def locations(self, zip_code: str) -> list[Location]:
        return [Location("62000001", "Test Marketplace", f"1 Main St, Arvada, CO {zip_code}")]

    async def search(self, term: str, location_id: str) -> list[Product]:
        self.searched_at.append(location_id)
        url = f"https://www.kingsoopers.com/p/{term}/0001"
        return [Product("0001", term.title(), "Brand", "1 lb", 1.99, None, url, None)]


class FakeList:
    """In-memory stand-in for one AnyList list. Records every write."""

    def __init__(self, stores: Iterable[str] = ()):
        self.stores: dict[str, str] = {f"s-{n}": n for n in stores}
        self.lines: dict[str, Line] = {}
        self.writes: list[dict[str, frozenset[str]]] = []

    def add(self, line_id: str, name: str, *stores: str, quantity: str = "") -> None:
        ids = frozenset(self.id_of(s) for s in stores)
        self.lines[line_id] = Line(line_id, name, quantity, False, ids)

    def id_of(self, store_name: str) -> str:
        for sid, name in self.stores.items():
            if name == store_name:
                return sid
        sid = f"s-{store_name}"
        self.stores[sid] = store_name
        return sid

    def store_names(self, line_id: str) -> set[str]:
        return {self.stores[s] for s in self.lines[line_id].store_ids}

    def set_by_hand(self, line_id: str, *stores: str) -> None:
        line = self.lines[line_id]
        ids = frozenset(self.id_of(s) for s in stores)
        self.lines[line_id] = Line(line.id, line.name, line.quantity, line.checked, ids)

    async def snapshot(self) -> Snapshot:
        return Snapshot(list(self.lines.values()), dict(self.stores))

    async def ensure_stores(self, names: Iterable[str]) -> dict[str, str]:
        for n in names:
            self.id_of(n)
        return {name: sid for sid, name in self.stores.items()}

    async def set_store_ids(self, changes: Mapping[str, frozenset[str]]) -> None:
        self.writes.append(dict(changes))
        for line_id, ids in changes.items():
            line = self.lines[line_id]
            self.lines[line_id] = Line(line.id, line.name, line.quantity, line.checked, ids)

    async def check_off(self, line_ids: Iterable[str]) -> None:
        for i in line_ids:
            line = self.lines[i]
            self.lines[i] = Line(line.id, line.name, line.quantity, True, line.store_ids)
