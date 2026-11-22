from groceries.normalize import normalize
from groceries.suggest import Name, kind, suggest


def name(label, count=1, item_id=None, item_name=None, canonical=False):
    own = item_name is not None and normalize(item_name) == normalize(label)
    return Name(normalize(label), label, count, item_id, item_name, own, canonical)


def item(label, item_id, canonical=False):
    return name(label, item_id=item_id, item_name=label, canonical=canonical)


def pairs(names, distinct=frozenset()):
    return {(s.alias.label, s.target.label, s.reason) for s in suggest(names, set(distinct))}


def test_prep_words_are_ignored_when_comparing():
    found = pairs([item("Shallot", 1), name("minced shallot")])
    assert found == {("minced shallot", "Shallot", "same words")}


def test_subset_names_suggest_the_more_general_one():
    found = pairs([item("Shallot", 1), name("banana shallot")])
    assert found == {("banana shallot", "Shallot", "contains “Shallot”")}


def test_misspellings_of_an_item_are_suggested():
    found = pairs([item("Shallot", 1), name("challot")])
    assert found == {("challot", "Shallot", "spelled alike")}


def test_plurals_and_case_are_the_same_words():
    [s] = suggest([item("Yellow Onion", 1), name("yellow onions", count=5)], set())
    assert (s.alias.label, s.target.label, s.reason) == (
        "yellow onions",
        "Yellow Onion",
        "same words",
    )
    assert (s.unit, s.style) == ("", "")


def test_not_the_same_is_remembered():
    names = [item("Lemon", 1), name("lemon juice")]
    assert pairs(names) == {("lemon juice", "Lemon", "contains “Lemon”")}
    assert pairs(names, {("lemon", "lemon juice")}) == set()


def test_ground_pepper_and_peppercorns_are_not_suggested_together():
    names = [item("Black Peppercorns", 1), item("Ground Black Pepper", 2)]
    assert pairs(names) == set()
    found = pairs([*names, name("whole black peppercorns")])
    assert found == {
        ("whole black peppercorns", "Black Peppercorns", "contains “Black Peppercorns”")
    }


def test_compounds_and_choices_are_never_suggested():
    names = [item("Salt", 1), item("Walnuts", 2), name("salt and pepper, to taste"),
             name("chopped walnuts or mini chocolate chips")]  # fmt: skip
    assert pairs(names) == set()
    assert kind("salt and pepper, to taste") == "compound"
    assert kind("salt + pepper") == "compound"
    assert kind("chopped walnuts or mini chocolate chips") == "choice"
    assert kind("onion/shallot") == "choice"
    assert kind("Sandwich bread") is None


def test_names_already_in_the_same_item_are_not_suggested():
    assert pairs([item("Garlic", 1), name("garlic cloves", item_id=1, item_name="Garlic")]) == set()


def test_an_alias_target_becomes_its_item():
    names = [
        item("Garlic", 1),
        name("garlic cloves", item_id=1, item_name="Garlic"),
        name("garlic cloves, crushed peeled"),
    ]
    # "garlic cloves, crushed peeled" has the same words as the alias "garlic cloves".
    found = {(s.alias.label, s.target.label) for s in suggest(names, set())}
    assert ("garlic cloves, crushed peeled", "Garlic") in found
    assert all(target != "garlic cloves" for _, target in found)


def test_new_items_are_suggested_from_names_alone():
    [s] = suggest([name("minced shallot"), name("shallot", count=3)], set())
    assert (s.alias.label, s.target.label, s.target.item_id) == ("minced shallot", "shallot", None)
    assert s.style == "minced"


def test_the_canonical_item_wins_between_two_items():
    [s] = suggest([item("Onion", 1, canonical=True), item("Onions", 2)], set())
    assert (s.alias.label, s.target.label) == ("Onions", "Onion")


def test_each_name_gets_one_suggestion_into_the_best_target():
    names = [
        item("Garlic", 1),
        name("crushed garlic", count=4),
        name("garlic, cut the top off"),
        name("garlic cloves, finely chopped"),
    ]
    found = {(s.alias.label, s.target.label) for s in suggest(names, set())}
    assert found == {
        ("crushed garlic", "Garlic"),
        ("garlic, cut the top off", "Garlic"),
        ("garlic cloves, finely chopped", "Garlic"),
    }


def test_clean_names_are_preferred_as_new_items():
    found = {
        (s.alias.label, s.target.label)
        for s in suggest([name("shallots, chopped", count=9), name("Shallots")], set())
    }
    assert found == {("shallots, chopped", "Shallots")}


def test_a_canonical_items_name_is_not_suggested_into_anything():
    names = [item("Black Pepper", 1, canonical=True), name("pepper", count=20)]
    assert pairs(names) == set()


def test_equal_names_still_get_a_suggestion():
    assert len(suggest([name("Yellow onion"), name("yellow onions")], set())) == 1


def test_the_closest_contained_name_is_the_target():
    names = [name("Pepper", count=9), name("Black Pepper"), name("freshly ground black pepper")]
    found = {(s.alias.label, s.target.label) for s in suggest(names, set())}
    assert ("freshly ground black pepper", "Black Pepper") in found
    assert ("Black Pepper", "Pepper") in found
