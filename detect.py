"""Pure product matching and stock detection for the Square Online store API.

The store's search box calls
    <API_BASE>/products?q=<term>&per_page=<n>&page=<p>
which returns JSON (`data` = products, `meta.pagination` = paging). This
module turns that JSON into ProductDetections. It does no I/O.

Signals observed on the live store (2026-11-02):

  * badges.out_of_stock:               true when sold out.
  * inventory.all_variations_sold_out: true when sold out.
  * inventory.total:                   units on hand (0 when sold out).
                                       `inventory.enabled` is false even for
                                       tracked items, so it is ignored.
  * preordering.*:                     all false so far; any true means the
                                       item can be ordered ahead of stock.

IN_STOCK requires every signal to agree. Anything ambiguous is UNKNOWN.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StockState(str, Enum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProductDetection:
    id: str
    name: str
    url: str
    state: StockState
    reason: str
    signals: dict[str, Any] = field(default_factory=dict)
    price: str | None = None
    quantity: int | None = None
    preorder: bool = False


class MalformedResponse(ValueError):
    pass


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Lowercase, strip accents ("Pokémon" -> "pokemon"), collapse punctuation to spaces."""
    # Drop only the accent marks; other non-ASCII (e.g. an em dash) becomes a space.
    stripped = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return _NON_ALNUM_RE.sub(" ", stripped.lower()).strip()


def match_term(name: str, terms: tuple[str, ...], exclude: tuple[str, ...]) -> str | None:
    """Return the first watch term the product name matches, or None.

    A term matches when every one of its words is a word in the name. Any
    exclude phrase appearing as whole words in the name rejects the product.
    """
    norm = normalize(name)
    padded = f" {norm} "
    if any(f" {normalize(phrase)} " in padded for phrase in exclude):
        return None
    words = set(norm.split())
    for term in terms:
        if set(normalize(term).split()) <= words:
            return term
    return None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _price(raw: dict[str, Any]) -> str | None:
    price = raw.get("price")
    if not isinstance(price, dict):
        return None
    low, high = price.get("low_formatted"), price.get("high_formatted")
    if low and high and low != high:
        return f"{low}-{high}"
    return low or high


def _url(raw: dict[str, Any], store_url: str) -> str:
    absolute = raw.get("absolute_site_link")
    if isinstance(absolute, str) and absolute.startswith("http"):
        return absolute
    link = raw.get("site_link")
    if isinstance(link, str) and link:
        return f"{store_url}/{link.lstrip('/')}"
    return store_url


def classify_product(raw: dict[str, Any], store_url: str) -> ProductDetection:
    """Classify one product record. Never raises; never returns IN_STOCK on ambiguity."""
    pid = str(raw.get("id") or raw.get("square_id") or "")
    name = str(raw.get("name") or "Unnamed product")
    try:
        return _classify(raw, pid, name, store_url)
    except Exception as exc:  # parser bugs must never become false alerts
        return ProductDetection(
            pid, name, store_url, StockState.UNKNOWN, f"parse error: {type(exc).__name__}: {exc}"
        )


def _classify(raw: dict[str, Any], pid: str, name: str, store_url: str) -> ProductDetection:
    badges = raw.get("badges") if isinstance(raw.get("badges"), dict) else {}
    inventory = raw.get("inventory") if isinstance(raw.get("inventory"), dict) else {}
    preordering = raw.get("preordering") if isinstance(raw.get("preordering"), dict) else {}

    signals: dict[str, Any] = {
        "badge_out_of_stock": _bool(badges.get("out_of_stock")),
        "all_variations_sold_out": _bool(inventory.get("all_variations_sold_out")),
        "inventory_total": _int(inventory.get("total")),
        "untracked_location": _bool(inventory.get("has_location_not_tracking")),
        "visible": raw.get("visibility") == "visible",
    }
    preorder = any(v is True for v in preordering.values())
    if preorder:
        signals["preorder"] = True

    def result(state: StockState, reason: str) -> ProductDetection:
        return ProductDetection(
            pid, name, _url(raw, store_url), state, reason, signals,
            price=_price(raw), quantity=signals["inventory_total"], preorder=preorder,
        )

    if not pid:
        return result(StockState.UNKNOWN, "product has no id")
    if not signals["visible"]:
        return result(StockState.UNKNOWN, f"not visible ({raw.get('visibility')!r})")

    badge_oos = signals["badge_out_of_stock"]
    sold_out = signals["all_variations_sold_out"]
    total = signals["inventory_total"]
    if badge_oos is None or sold_out is None:
        return result(StockState.UNKNOWN, "stock badges missing")
    if badge_oos != sold_out:
        return result(StockState.UNKNOWN, "out_of_stock badge and sold-out flag disagree")

    if badge_oos:
        if total:
            return result(StockState.UNKNOWN, f"marked sold out but inventory total is {total}")
        return result(StockState.OUT_OF_STOCK, "all signals out of stock")

    # Not marked sold out. Require a positive count unless it is a pre-order or
    # a location doesn't track inventory (then the count isn't meaningful).
    if preorder:
        return result(StockState.IN_STOCK, "available for pre-order")
    if total is None:
        return result(StockState.UNKNOWN, "inventory total missing")
    if total > 0:
        return result(StockState.IN_STOCK, "all signals in stock")
    if signals["untracked_location"]:
        return result(StockState.IN_STOCK, "not sold out; inventory not tracked")
    return result(StockState.UNKNOWN, "not marked sold out but inventory total is 0")


def parse_search_page(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """Return (product records, total_pages). Raises MalformedResponse on bad shape."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise MalformedResponse("response has no `data` list")
    products = [p for p in payload["data"] if isinstance(p, dict)]
    try:
        total_pages = int(payload["meta"]["pagination"]["total_pages"])
    except (KeyError, TypeError, ValueError):
        total_pages = 1
    return products, max(total_pages, 1)


@dataclass(frozen=True)
class Scan:
    """One poll cycle's result: every matching product, or a failure reason.

    `ok` is False when any search request failed, so a partial result never
    makes a product look delisted or re-arms its alert.
    """

    ok: bool
    reason: str
    products: tuple[ProductDetection, ...] = ()
    signals: dict[str, Any] = field(default_factory=dict)
