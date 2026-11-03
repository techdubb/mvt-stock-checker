import copy
import json
from pathlib import Path

import pytest

from config import DEFAULT_EXCLUDE, DEFAULT_WATCH_TERMS
from detect import MalformedResponse, StockState, classify_product, match_term, parse_search_page

FIXTURES = Path(__file__).parent / "fixtures"
STORE = "https://www.mainevintagetoys.com"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def product(fixture, name_prefix):
    return next(p for p in load(fixture)["data"] if p["name"].startswith(name_prefix))


def matches(name):
    return match_term(name, DEFAULT_WATCH_TERMS, DEFAULT_EXCLUDE)


# --- matching ---------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Pokemon 30th Celebration Elite Trainer Box (*In-Store Pick-up Only!*)",
    "Pokemon 30th Celebration Tech Sticker Collection (*In-Store Pick-up Only!*)",
    "Pokémon 30th Anniversary Celebration Booster Bundle",
    "Pokemon Mega Evolution Delta Reign Booster Box",
    "Pokemon Mega Evolution—Delta Reign Elite Trainer Box",
])
def test_english_sealed_product_matches(name):
    assert matches(name)


@pytest.mark.parametrize("name", [
    "Pokemon Japanese 30th Celebration Booster Box (*In-Store Pick-up Only!*)",
    "Pokemon CHINESE 30th Celebration Booster Pack (*In-Store Pick-up Only!*)",
    "Pokemon Chinese 30th Anniversary Celebration Sylveon Card Display Set (Simplified Chinese)",
    "Ultra Pro Pokemon Mega Evolution Delta Reign 9-Pocket Portfolio",
    "Pokemon Delta Reign Prerelease PLAY Event (Saturday, 10/31 - 10:00AM)",
    "Pokemon Sword & Shield Chilling Reign Booster Pack",  # "reign" alone isn't enough
    "Ultra Pro 30th Anniversary Zippered 12-Pocket PRO-Binder for Magic: The Gathering",
])
def test_other_products_do_not_match(name):
    assert matches(name) is None


def test_exclude_is_whole_word():
    # "event" must not reject a name that merely contains those letters.
    assert match_term("Pokemon Delta Reign Eventide Box", ("delta reign",), ("event",))


def test_live_fixture_matches_only_english_30th_celebration():
    names = [p["name"] for p in load("search_30th_celebration.json")["data"] if matches(p["name"])]
    assert names, "fixture should contain English 30th Celebration product"
    assert all("30th Celebration" in n for n in names)
    assert not any("Japanese" in n or "CHINESE" in n.upper() for n in names)


# --- stock classification (real records) -------------------------------------

def test_sold_out_record():
    det = classify_product(product("search_30th_celebration.json", "Pokemon 30th Celebration Elite Trainer Box"), STORE)
    assert det.state is StockState.OUT_OF_STOCK
    assert det.price == "$99.99"
    assert det.url.startswith(f"{STORE}/product/pokemon-30th-celebration-elite-trainer-box")


def test_in_stock_record():
    det = classify_product(product("search_30th_celebration.json", "Pokemon 30th Celebration Battle Deck"), STORE)
    assert det.state is StockState.IN_STOCK
    assert det.quantity and det.quantity > 0


def flip_to_in_stock(raw, total=5):
    raw = copy.deepcopy(raw)
    raw["badges"]["out_of_stock"] = False
    raw["inventory"]["all_variations_sold_out"] = False
    raw["inventory"]["total"] = total
    return raw


@pytest.fixture
def sold_out_raw():
    return product("search_30th_celebration.json", "Pokemon 30th Celebration Elite Trainer Box")


def test_simulated_restock(sold_out_raw):
    assert classify_product(flip_to_in_stock(sold_out_raw), STORE).state is StockState.IN_STOCK


@pytest.mark.parametrize("mutate, reason", [
    (lambda r: r["badges"].update(out_of_stock=False), "disagree"),
    (lambda r: r["inventory"].update(all_variations_sold_out=False), "disagree"),
    (lambda r: r["inventory"].update(total=3), "inventory total is 3"),
    (lambda r: r.update(visibility="hidden"), "not visible"),
    (lambda r: r.pop("badges"), "badges missing"),
])
def test_single_signal_disagreement_is_unknown(sold_out_raw, mutate, reason):
    raw = copy.deepcopy(sold_out_raw)
    mutate(raw)
    det = classify_product(raw, STORE)
    assert det.state is StockState.UNKNOWN
    assert reason in det.reason


def test_not_sold_out_with_zero_count_is_unknown(sold_out_raw):
    raw = flip_to_in_stock(sold_out_raw, total=0)
    assert classify_product(raw, STORE).state is StockState.UNKNOWN


def test_untracked_inventory_counts_as_in_stock(sold_out_raw):
    raw = flip_to_in_stock(sold_out_raw, total=0)
    raw["inventory"]["has_location_not_tracking"] = True
    assert classify_product(raw, STORE).state is StockState.IN_STOCK


def test_preorder_counts_as_in_stock(sold_out_raw):
    raw = flip_to_in_stock(sold_out_raw, total=0)
    raw["preordering"]["pickup"] = True
    det = classify_product(raw, STORE)
    assert det.state is StockState.IN_STOCK
    assert det.preorder


def test_garbage_record_never_raises():
    det = classify_product({"id": "X", "name": "Delta Reign", "badges": "nope", "visibility": "visible"}, STORE)
    assert det.state is StockState.UNKNOWN


# --- search page parsing ----------------------------------------------------

def test_parse_search_page():
    records, pages = parse_search_page(load("search_delta_reign.json"))
    assert len(records) == 18
    assert pages == 1


@pytest.mark.parametrize("payload", [None, [], {"error": "x"}, {"data": "x"}])
def test_parse_search_page_rejects_bad_shape(payload):
    with pytest.raises(MalformedResponse):
        parse_search_page(payload)
