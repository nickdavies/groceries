import dataclasses

from groceries.anylist import Ingredient, IngredientText, Line, LineText, Snapshot
from groceries.kroger import Location, Product
from groceries.normalize import normalize


class FakeKroger:
    """Kroger API stand-in. Records which store each search was scoped to."""

    def __init__(self):
        self.searched_at: list[str] = []

    async def locations(self, zip_code: str) -> list[Location]:
        return [Location("62000001", "Test Marketplace", f"1 Main St, Arvada, CO {zip_code}")]

    async def search(self, term: str, location_id: str) -> list[Product]:
        self.searched_at.append(location_id)
        if term == "no such thing":
            return []
        url = f"https://www.kingsoopers.com/p/{term}/0001"
        return [Product("0001", term.title(), "Brand", "1 lb", 1.99, None, url, None)]


class FakeList:
    """In-memory stand-in for one AnyList list and the account's recipes. `add`,
    `add_recipe`, `set_by_hand`, `edit` and `check_off` are a person using AnyList;
    `set_stores`, `update_lines` and `update_ingredients` are our writes, which are
    recorded.

    Like AnyList, rewriting an ingredient moves an open line added from its recipe to a
    new line (new id, no stores) with the ingredient's new name."""

    def __init__(self, stores=("King Soopers", "Costco", "Natural Grocers", "Total Beverage")):
        self.stores: list[str] = list(stores)
        self.lines: dict[str, Line] = {}
        self.recipes: dict[str, tuple[str, dict[str, IngredientText]]] = {}
        self.writes: list[dict[str, frozenset[str]]] = []
        self.line_writes: list[dict] = []
        self.ingredient_writes: list[dict] = []
        self.fail_writes = False
        # Lines whose renames AnyList acknowledges but doesn't apply, like lines added
        # from a recipe.
        self.ignored_renames: set[str] = set()

    def _replace(self, line_id: str, **changes) -> None:
        line = self.lines[line_id]
        self.lines[line_id] = dataclasses.replace(line, **changes)

    def add(
        self,
        line_id: str,
        name: str,
        *stores: str,
        quantity: str = "",
        details: str = "",
        recipes: tuple[str, ...] = (),
        checked: bool = False,
    ) -> None:
        self.stores += [s for s in stores if s not in self.stores]
        self.lines[line_id] = Line(
            line_id, name, quantity, checked, frozenset(stores), details, frozenset(recipes)
        )

    def add_recipe(self, recipe_id: str, name: str, *ingredients: tuple) -> None:
        """Each ingredient is (id, quantity, name, note), shown as AnyList composes it."""
        self.recipes[recipe_id] = (
            name,
            {
                i: IngredientText(
                    q, n, note, " ".join(x for x in (q, n) if x) + (f", {note}" if note else "")
                )
                for i, q, n, note in ingredients
            },
        )

    def ingredient(self, recipe_id: str, ingredient_id: str) -> IngredientText:
        return self.recipes[recipe_id][1][ingredient_id]

    def edit(self, line_id: str, **changes) -> None:
        self._replace(line_id, **changes)

    def set_by_hand(self, line_id: str, *stores: str) -> None:
        self.stores += [s for s in stores if s not in self.stores]
        self._replace(line_id, stores=frozenset(stores))

    def check_off(self, line_id: str) -> None:
        self._replace(line_id, checked=True)

    def stores_of(self, line_id: str) -> set[str]:
        return set(self.lines[line_id].stores)

    async def snapshot(self) -> Snapshot:
        ingredients = [
            Ingredient(rid, name, iid, text)
            for rid, (name, texts) in self.recipes.items()
            for iid, text in texts.items()
        ]
        return Snapshot(list(self.lines.values()), list(self.stores), ingredients)

    async def update_lines(self, changes) -> set[str]:
        if self.fail_writes:
            raise ConnectionError("AnyList unreachable")
        self.line_writes.append(dict(changes))
        done = set()
        for line_id, (old, new) in changes.items():
            line = self.lines.get(line_id)
            if line is None or LineText(line.name, line.details, line.quantity) != old:
                continue
            if line_id not in self.ignored_renames:
                self._replace(line_id, name=new.name, details=new.details, quantity=new.quantity)
            done.add(line_id)
        return done

    async def update_ingredients(self, changes) -> set[tuple[str, str]]:
        if self.fail_writes:
            raise ConnectionError("AnyList unreachable")
        self.ingredient_writes.append(dict(changes))
        done = set()
        for (rid, iid), (old, new) in changes.items():
            texts = self.recipes[rid][1]
            if texts.get(iid) != old:
                continue
            texts[iid] = new
            done.add((rid, iid))
            for line in list(self.lines.values()):
                if (
                    rid in line.recipe_ids
                    and not line.checked
                    and normalize(line.name) == normalize(old.name)
                ):
                    del self.lines[line.id]
                    moved = f"{line.id}-moved"
                    self.lines[moved] = dataclasses.replace(
                        line, id=moved, name=new.name, stores=frozenset()
                    )
        return done

    async def create_store(self, name: str) -> None:
        if name not in self.stores:
            self.stores.append(name)

    async def set_stores(self, changes) -> None:
        if self.fail_writes:
            raise ConnectionError("AnyList unreachable")
        self.writes.append(dict(changes))
        for line_id, names in changes.items():
            self.stores += [s for s in names if s not in self.stores]
            self._replace(line_id, stores=frozenset(names))
