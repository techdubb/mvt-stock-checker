"""Persisted watcher state and the pure transition function `step()`.

`step()` takes the previous state, one Scan, and the current time, and
returns the new state plus any notifications to send. It does no I/O, which
keeps alert/reminder/heartbeat/failure logic unit-testable with a fake clock.

Each matching product gets its own little state machine (confirmations,
alert, reminders, re-arm on sell-out). Health and heartbeat are store-wide.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path

from config import Config
from detect import ProductDetection, Scan, StockState

log = logging.getLogger(__name__)

PRIORITY_URGENT = "urgent"
PRIORITY_HIGH = "high"
PRIORITY_DEFAULT = "default"
PRIORITY_LOW = "low"

STORE_NAME = "Maine Vintage Toys"


@dataclass(frozen=True)
class Notification:
    kind: str  # restock | reminder | new_listing | heartbeat | failure | recovered
    title: str
    message: str
    priority: str
    click_url: str | None = None
    tags: tuple[str, ...] = ()
    product_id: str | None = None  # set on restock/reminder notes


@dataclass(frozen=True)
class ProductState:
    name: str
    url: str
    current: str = StockState.UNKNOWN.value
    last_known: str | None = None  # last non-UNKNOWN state
    consecutive_in_stock: int = 0
    alerted: bool = False  # restock alert sent for the current in-stock episode
    reminders_sent: int = 0
    last_alert_at: str | None = None
    first_seen: str | None = None


@dataclass(frozen=True)
class WatchState:
    products: dict[str, ProductState] = field(default_factory=dict)
    seeded: bool = False  # True after the first good scan; new-listing alerts start then
    unhealthy_since: str | None = None
    failure_alert_sent: bool = False
    last_heartbeat_date: str | None = None


def load_state(path: Path) -> WatchState:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return WatchState()
    except (OSError, ValueError) as exc:
        log.error("state file unreadable, starting fresh", extra={"fields": {"error": str(exc)}})
        return WatchState()
    known = {f.name for f in fields(WatchState)} - {"products"}
    product_fields = {f.name for f in fields(ProductState)}
    products = {
        pid: ProductState(**{k: v for k, v in p.items() if k in product_fields})
        for pid, p in (raw.get("products") or {}).items()
        if isinstance(p, dict) and "name" in p and "url" in p
    }
    return WatchState(products=products, **{k: v for k, v in raw.items() if k in known})


def save_state(path: Path, state: WatchState) -> None:
    """Atomic write: a crash mid-write never leaves a corrupt state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(state), f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def step(
    state: WatchState, scan: Scan, now: datetime, cfg: Config
) -> tuple[WatchState, list[Notification]]:
    """Advance the state machine by one scan. `now` must be timezone-aware."""
    notes: list[Notification] = []
    s = state

    # --- per-product stock transitions -----------------------------------
    # Only a complete scan touches products. Products missing from a complete
    # scan (delisted, hidden, renamed) keep their last state untouched.
    if scan.ok:
        products = dict(s.products)
        for det in scan.products:
            prev = products.get(det.id)
            ps, product_notes = _step_product(prev, det, now, cfg, seeded=s.seeded)
            products[det.id] = ps
            notes.extend(product_notes)
        s = replace(s, products=products, seeded=True)

    # --- health / failure alert ------------------------------------------
    if not scan.ok:
        since = _parse(s.unhealthy_since)
        if since is None:
            s = replace(s, unhealthy_since=now.isoformat())
        elif (
            not s.failure_alert_sent
            and (now - since).total_seconds() >= cfg.failure_alert_after_seconds
        ):
            minutes = int((now - since).total_seconds() // 60)
            notes.append(Notification(
                kind="failure",
                title="MVT stock watcher may be blocked or broken",
                message=f"No reliable reading of the store for {minutes} min. Last reason: {scan.reason}",
                priority=PRIORITY_HIGH,
                click_url=cfg.store_url,
                tags=("warning",),
            ))
            s = replace(s, failure_alert_sent=True)
    else:
        if s.failure_alert_sent:
            notes.append(Notification(
                kind="recovered",
                title="MVT stock watcher recovered",
                message=f"Reading the store again. {_summary(s)}",
                priority=PRIORITY_LOW,
                click_url=cfg.store_url,
                tags=("white_check_mark",),
            ))
        s = replace(s, unhealthy_since=None, failure_alert_sent=False)

    # --- daily heartbeat -------------------------------------------------
    local = now.astimezone(cfg.timezone)
    today = local.date().isoformat()
    if local.time() >= cfg.heartbeat_time and s.last_heartbeat_date != today:
        health = "OK" if scan.ok else f"last check failed ({scan.reason})"
        notes.append(Notification(
            kind="heartbeat",
            title="MVT stock watcher is alive",
            message=f"Watching: {', '.join(cfg.watch_terms)}. {_summary(s)} Store: {health}",
            priority=PRIORITY_LOW,
            click_url=cfg.store_url,
            tags=("heartbeat",),
        ))
        s = replace(s, last_heartbeat_date=today)

    return s, notes


def _step_product(
    prev: ProductState | None, det: ProductDetection, now: datetime, cfg: Config, seeded: bool
) -> tuple[ProductState, list[Notification]]:
    notes: list[Notification] = []
    is_new = prev is None
    ps = prev or ProductState(name=det.name, url=det.url, first_seen=now.isoformat())
    ps = replace(ps, name=det.name, url=det.url, current=det.state.value)

    if det.state is StockState.IN_STOCK:
        ps = replace(ps, consecutive_in_stock=ps.consecutive_in_stock + 1)
        if not ps.alerted:
            # A product we have never seen that is already in stock has no
            # flapping history to confirm against, and is the most
            # time-sensitive case: alert right away.
            if is_new or ps.consecutive_in_stock >= cfg.confirmations:
                notes.append(_restock_note(det, cfg, new=is_new and seeded))
                ps = replace(
                    ps, alerted=True, reminders_sent=0, last_alert_at=now.isoformat(),
                    last_known=StockState.IN_STOCK.value,
                )
        else:
            ps = replace(ps, last_known=StockState.IN_STOCK.value)
            last = _parse(ps.last_alert_at)
            due = last is None or (now - last).total_seconds() >= cfg.reminder_interval_seconds
            if ps.reminders_sent < cfg.max_reminders and due:
                n = ps.reminders_sent + 1
                notes.append(_restock_note(det, cfg, reminder=n))
                ps = replace(ps, reminders_sent=n, last_alert_at=now.isoformat())
    elif det.state is StockState.OUT_OF_STOCK:
        # Re-arm: a later restock will alert again.
        ps = replace(
            ps, consecutive_in_stock=0, alerted=False, reminders_sent=0,
            last_known=StockState.OUT_OF_STOCK.value,
        )
    else:
        # UNKNOWN breaks the "consecutive" run but doesn't re-arm an alert,
        # so IN -> UNKNOWN -> IN does not double-alert.
        ps = replace(ps, consecutive_in_stock=0)

    # A new listing that isn't in stock yet (e.g. a set that hasn't released)
    # is worth a heads-up, but not on the very first scan, when everything is "new".
    if is_new and seeded and cfg.alert_new_listings and det.state is not StockState.IN_STOCK:
        status = "sold out" if det.state is StockState.OUT_OF_STOCK else "stock unclear"
        price = f" - {det.price}" if det.price else ""
        notes.append(Notification(
            kind="new_listing",
            title=f"New listing at MVT ({status}): {det.name}",
            message=f"{det.name}{price}\nNot buyable yet; you'll get an alert when it is.\n{det.url}",
            priority=PRIORITY_DEFAULT,
            click_url=det.url,
            tags=("new",),
        ))

    return ps, notes


def _summary(s: WatchState) -> str:
    in_stock = [p.name for p in s.products.values() if p.current == StockState.IN_STOCK.value]
    text = f"{len(s.products)} matching products, {len(in_stock)} in stock."
    if in_stock:
        text += " In stock: " + "; ".join(sorted(in_stock))
    return text


def _restock_note(
    det: ProductDetection, cfg: Config, reminder: int = 0, new: bool = False
) -> Notification:
    price = f" - {det.price}" if det.price else ""
    qty = f" ({det.quantity} available)" if det.quantity else ""
    status = "PRE-ORDER OPEN" if det.preorder else "IN STOCK"
    title = f"{'NEW + ' if new else ''}{status} at MVT: {det.name}"
    if reminder:
        title = f"Still in stock ({reminder}/{cfg.max_reminders}): {det.name}"
    return Notification(
        kind="reminder" if reminder else "restock",
        title=title,
        message=f"{det.name}{price}{qty}\nTap to open: {det.url}",
        priority=PRIORITY_HIGH if reminder else PRIORITY_URGENT,
        click_url=det.url,
        tags=("rotating_light",) if not reminder else ("bell",),
        product_id=det.id,
    )
