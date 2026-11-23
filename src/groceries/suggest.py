"""Suggested name merges for /names, and the unit and style a mapping starts with.

Names are compared by their main part: lowercased, with parentheticals and everything
after the first comma dropped, and prep words ("minced", "finely") removed. Words that
change what you buy are kept: ground, whole, cracked, salted, unsalted, sizes. What's left
is stemmed with AnyList's own stemmer, so "egg" and "Eggs" match.

Two names are suggested as one thing when:
  - their stemmed words are the same ("Yellow Onions", "yellow onion"),
  - one's words are a subset of the other's ("minced shallot" into "shallot"),
  - or a name is spelled almost like an item's name ("challot" into "Shallot").
Ignored names and pairs a person marked "not the same" are never suggested. A name with
"and", "&" or "+" is a compound ("salt and pepper") and one with "or" or "/" a choice
("walnuts or chocolate chips"). Those are listed on their own and never suggested.
"""

import difflib
import re
from collections import defaultdict
from dataclasses import dataclass

from aioanylist.stemming import stem_words


def _wordset(text: str) -> frozenset[str]:
    return frozenset(text.split())


PREP_WORDS = _wordset(
    "minced chopped finely diced sliced fresh freshly crushed grated roughly thinly "
    "coarsely lightly peeled softened melted beaten of"
)
# Words a default mapping moves into the quantity. A starting point only: the unit is
# editable before mapping.
UNITS = _wordset(
    "clove cloves head heads stalk stalks sprig sprigs bunch bunches can cans jar "
    "jars slice slices piece pieces pinch pinches stick sticks sheet sheets leaf "
    "leaves rib ribs fillet fillets package packages bag bags bottle bottles cup "
    "cups tbsp tablespoon tablespoons tsp teaspoon teaspoons g gram grams kg ml l oz "
    "ounce ounces lb lbs pound pounds dl krm"
)
SIMILARITY = 0.85

_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)?")
_PAREN = re.compile(r"\(([^)]*)\)?")


def _main(label: str) -> str:
    return _PAREN.sub(" ", label.casefold()).split(",", 1)[0]


def words(label: str) -> list[str]:
    """The main part's words, prep words included."""
    return _WORD.findall(_main(label))


def core(label: str) -> list[str]:
    return [w for w in words(label) if w not in PREP_WORDS]


def stems(label: str) -> frozenset[str]:
    return frozenset(stem_words(core(label)))


def title_case(name: str) -> str:
    """Each word's first letter capitalized, the rest as typed: "all-purpose flour" gives
    "All-purpose Flour", and "pip's" stays "Pip's"."""
    return " ".join(w[:1].upper() + w[1:] for w in name.split(" "))


def kind(label: str) -> str | None:
    """'compound' for "salt and pepper", 'choice' for "walnuts or chips", else None."""
    main = _main(label)
    if re.search(r"\band\b|&|\+", main):
        return "compound"
    if re.search(r"\bor\b|/", main):
        return "choice"
    return None


def defaults(alias: str, target: str) -> tuple[str, str]:
    """The (unit, style) mapping `alias` into the item `target` starts with.

    Alias words that aren't in the target (or a misspelling of one) become the style,
    except the first unit word, which becomes the unit. Parentheticals and the comma tail
    are kept in the style. "garlic cloves, finely chopped" into "Garlic" gives
    ("cloves", "finely chopped"); "egg whites" into "Eggs" gives ("", "whites").
    """
    target_stems, target_words = stems(target), core(target)
    unit, rest = "", []
    for word in words(alias):
        if stem_words([word])[0] in target_stems:
            continue
        if difflib.get_close_matches(word, target_words, n=1, cutoff=0.8):
            continue
        if not unit and word in UNITS:
            unit = word
            continue
        rest.append(word)
    tail = [p.strip() for p in _PAREN.findall(alias)]
    without_parens = _PAREN.sub(" ", alias)
    if "," in without_parens:
        tail.append(without_parens.split(",", 1)[1].strip())
    parts = [" ".join(rest), *tail]
    return unit, ", ".join(dict.fromkeys(p for p in parts if p))


@dataclass(frozen=True)
class Name:
    """A name seen in AnyList, or an item's own name."""

    key: str  # normalize(name)
    label: str
    count: int = 0  # lines plus recipe ingredients using it
    item_id: int | None = None  # the item it maps to, if any
    item_name: str | None = None
    is_item_name: bool = False  # it is that item's own name
    canonical: bool = False


@dataclass(frozen=True)
class Suggestion:
    alias: Name
    target: Name  # an item's name, or a name that would become a new item
    reason: str
    unit: str
    style: str


def _clean(label: str) -> bool:
    """A name fit to be an item's name: no parenthetical, comma tail or prep words."""
    return "," not in label and "(" not in label and words(label) == core(label)


def suggest(names: list[Name], distinct: set[tuple[str, str]]) -> list[Suggestion]:
    """At most one suggestion per name: the best item (or name) to merge it into.
    `distinct` holds sorted key pairs a person said are different things.

    The best target is a canonical item, then any item, then a clean name, then the
    closest one: for "ground black pepper", "black pepper" over "pepper". A canonical
    item's own name is never suggested into anything: a person already chose it.
    """
    plain = [n for n in names if kind(n.label) is None and stems(n.label)]
    by_key = {n.key: n for n in plain}
    stem_of = {n.key: stems(n.label) for n in plain}
    by_stem = defaultdict(set)
    for n in plain:
        for s in stem_of[n.key]:
            by_stem[s].add(n.key)
    item_of = {n.item_id: n for n in names if n.is_item_name}

    def rank(n: Name) -> tuple:
        clean = _clean(n.label)
        return (n.canonical, n.is_item_name, clean, len(stem_of[n.key]), n.count, n.key)

    candidates: dict[str, list[tuple[Name, str]]] = defaultdict(list)

    def add(alias: Name, target: Name, reason: str) -> None:
        if alias.canonical and alias.is_item_name:
            return
        if target.item_id is not None and not target.is_item_name:
            target = item_of.get(target.item_id, target)  # another item's alias: that item
        if alias.key == target.key or (alias.item_id and alias.item_id == target.item_id):
            return
        if tuple(sorted((alias.key, target.key))) in distinct:
            return
        candidates[alias.key].append((target, reason))

    for a in plain:
        sa = stem_of[a.key]
        for key in set().union(*(by_stem[s] for s in sa)) - {a.key}:
            b, sb = by_key[key], stem_of[key]
            if sa == sb and rank(a) > rank(b):
                add(b, a, "same words")
            elif sa < sb:
                add(b, a, f"contains “{a.label}”")

    item_names = [n for n in plain if n.is_item_name]
    for a in plain:
        ca = " ".join(core(a.label))
        if len(ca) < 4:
            continue
        for b in item_names:
            cb = " ".join(core(b.label))
            if b.key == a.key or abs(len(ca) - len(cb)) > 2:
                continue
            if stem_of[a.key] <= stem_of[b.key] or stem_of[b.key] <= stem_of[a.key]:
                continue  # compared above
            if a.is_item_name and rank(a) > rank(b):
                continue  # suggest the other way round
            matcher = difflib.SequenceMatcher(None, ca, cb)
            if matcher.real_quick_ratio() >= SIMILARITY and matcher.ratio() >= SIMILARITY:
                add(a, b, "spelled alike")

    found = []
    for key, options in candidates.items():
        alias = by_key[key]
        target, reason = max(options, key=lambda o: rank(o[0]) if o[0].key in by_key else ())
        unit, style = defaults(alias.label, target.label)
        found.append(Suggestion(alias, target, reason, unit, style))
    return sorted(found, key=lambda s: (s.target.label.casefold(), s.alias.label))
