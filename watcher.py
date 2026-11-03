"""Maine Vintage Toys stock watcher: main loop, scheduling, HTTP, and CLI.

Usage:
    python watcher.py                 # run forever
    python watcher.py --once          # one check, then exit
    python watcher.py --dry-run       # log notifications instead of sending
    python watcher.py --test-notify   # send a test push and exit
    python watcher.py --list          # print what the watch terms match right now, then exit
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import signal
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import requests

from config import Config, ConfigError
from detect import (
    MalformedResponse, ProductDetection, Scan, StockState, classify_product, match_term,
    parse_search_page,
)
from notify import LogNotifier, Notifier, NtfyNotifier
from state import PRIORITY_HIGH, Notification, WatchState, load_state, save_state, step

log = logging.getLogger("watcher")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Responses that mean "slow down / we're being blocked" rather than "API changed".
_BACKOFF_STATUSES = {403, 429}

PER_PAGE = 100


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


@dataclass(frozen=True)
class FetchResult:
    status: int | None
    payload: object | None
    latency_ms: int
    error: str | None = None
    retry_after: int | None = None


def fetch_json(session: requests.Session, url: str, params: dict, timeout: float) -> FetchResult:
    start = time.monotonic()
    try:
        resp = session.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        latency = int((time.monotonic() - start) * 1000)
        return FetchResult(None, None, latency, error=f"{type(exc).__name__}: {exc}")
    latency = int((time.monotonic() - start) * 1000)
    retry_after = resp.headers.get("Retry-After", "")
    retry = int(retry_after) if retry_after.isdigit() else None
    if resp.status_code != 200:
        return FetchResult(resp.status_code, None, latency, error=f"HTTP {resp.status_code}", retry_after=retry)
    try:
        payload = resp.json()
    except ValueError:
        # An HTML page where JSON should be is most likely a bot challenge.
        return FetchResult(200, None, latency, error="response was not JSON")
    return FetchResult(200, payload, latency, retry_after=retry)


def should_back_off(result: FetchResult) -> bool:
    if result.error is None:
        return False
    if result.status is None or result.status == 200:  # network error or non-JSON body
        return True
    return result.status in _BACKOFF_STATUSES or result.status >= 500


@dataclass(frozen=True)
class ScanOutcome:
    scan: Scan
    back_off: bool
    retry_after: int | None
    requests_made: int
    # Products the search returned that contain a watch term's words but were
    # dropped by EXCLUDE. Only used by --list, to help tune the filters.
    excluded: tuple[str, ...] = ()


class Scanner:
    def __init__(self, cfg: Config, session: requests.Session, sleep=time.sleep) -> None:
        self.cfg = cfg
        self.session = session
        self.sleep = sleep

    def scan(self) -> ScanOutcome:
        """Run every watch-term search. Never raises."""
        url = f"{self.cfg.api_base}/products"
        seen: dict[str, ProductDetection] = {}
        excluded: set[str] = set()
        requests_made = 0
        latency_ms = 0
        truncated: list[str] = []

        def fail(reason: str, result: FetchResult | None = None) -> ScanOutcome:
            signals = {"requests": requests_made, "latency_ms": latency_ms}
            return ScanOutcome(
                Scan(False, reason, signals=signals),
                back_off=result is not None and should_back_off(result),
                retry_after=result.retry_after if result else None,
                requests_made=requests_made,
            )

        try:
            for term in self.cfg.watch_terms:
                page, total_pages, term_results = 1, 1, 0
                while page <= min(total_pages, self.cfg.max_pages_per_term):
                    if requests_made:
                        self.sleep(self.cfg.request_gap_seconds)
                    params = {"q": term, "per_page": PER_PAGE, "page": page}
                    result = fetch_json(self.session, url, params, self.cfg.request_timeout_seconds)
                    requests_made += 1
                    latency_ms += result.latency_ms
                    if result.error:
                        return fail(f"search {term!r} page {page}: {result.error}", result)
                    try:
                        records, total_pages = parse_search_page(result.payload)
                    except MalformedResponse as exc:
                        return fail(f"search {term!r} page {page}: {exc}")
                    term_results += len(records)
                    for raw in records:
                        name = str(raw.get("name") or "")
                        if match_term(name, self.cfg.watch_terms, self.cfg.exclude):
                            det = classify_product(raw, self.cfg.store_url)
                            seen.setdefault(det.id, det)
                        elif match_term(name, self.cfg.watch_terms, ()):
                            excluded.add(name)
                    page += 1
                if total_pages > self.cfg.max_pages_per_term:
                    truncated.append(term)
                # The store's search is fuzzy and always returns something; an
                # empty result means the API changed, not that nothing matched.
                if term_results == 0:
                    return fail(f"search {term!r} returned no products at all")
        except Exception as exc:  # belt and braces: the loop must survive anything
            log.exception("unexpected error during scan")
            return fail(f"internal error: {type(exc).__name__}: {exc}")

        signals = {"requests": requests_made, "latency_ms": latency_ms}
        if truncated:
            signals["truncated_terms"] = truncated
        return ScanOutcome(
            Scan(True, f"{len(seen)} matching products", tuple(seen.values()), signals),
            back_off=False,
            retry_after=None,
            requests_made=requests_made,
            excluded=tuple(sorted(excluded)),
        )


class Watcher:
    def __init__(
        self,
        cfg: Config,
        notifier: Notifier,
        session: requests.Session | None = None,
        now=lambda: datetime.now(timezone.utc),
        sleep=time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.cfg = cfg
        self.notifier = notifier
        self.scanner = Scanner(cfg, session or make_session(), sleep=sleep)
        self.now = now
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.state: WatchState = load_state(cfg.state_file)
        self.backoff_seconds: float | None = None

    def jittered_interval(self) -> float:
        j = self.cfg.jitter_percent / 100
        return self.cfg.poll_interval_seconds * (1 + self.rng.uniform(-j, j))

    def next_delay(self, outcome: ScanOutcome) -> float:
        if outcome.back_off:
            base = self.backoff_seconds or self.cfg.poll_interval_seconds
            self.backoff_seconds = min(base * 2, self.cfg.max_backoff_seconds)
            delay = self.backoff_seconds
            if outcome.retry_after:
                delay = max(delay, min(outcome.retry_after, 2 * self.cfg.max_backoff_seconds))
            return delay
        self.backoff_seconds = None
        return self.jittered_interval()

    def run_once(self) -> float:
        """One poll cycle. Never raises. Returns seconds to sleep before the next one."""
        outcome = self.scanner.scan()
        scan = outcome.scan
        delay = self.next_delay(outcome)

        try:
            new_state, notes = step(self.state, scan, self.now(), self.cfg)
            for note in notes:
                if not self.notifier.send(note) and note.kind == "restock" and note.product_id:
                    # Don't mark this episode as alerted; retry on the next in-stock read.
                    products = dict(new_state.products)
                    products[note.product_id] = replace(products[note.product_id], alerted=False)
                    new_state = replace(new_state, products=products)
            self.state = new_state
            save_state(self.cfg.state_file, self.state)
        except Exception:
            log.exception("failed to update state or notify")

        for det in scan.products:
            log.log(
                logging.WARNING if det.state is StockState.UNKNOWN else logging.DEBUG,
                "product",
                extra={"fields": {
                    "name": det.name, "state": det.state.value, "reason": det.reason,
                    "signals": det.signals, "price": det.price,
                }},
            )
        in_stock = [d.name for d in scan.products if d.state is StockState.IN_STOCK]
        log.log(
            logging.INFO if scan.ok else logging.WARNING,
            "check",
            extra={"fields": {
                "ok": scan.ok,
                "reason": scan.reason,
                "matched": len(scan.products),
                "in_stock": in_stock,
                **scan.signals,
                "next_check_s": round(delay),
                "backoff": self.backoff_seconds is not None,
            }},
        )
        return delay

    def run_forever(self) -> None:
        log.info("watcher started", extra={"fields": {
            "watch_terms": self.cfg.watch_terms,
            "interval_s": self.cfg.poll_interval_seconds,
            "confirmations": self.cfg.confirmations,
            "known_products": len(self.state.products),
        }})
        while True:
            self.sleep(self.run_once())


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    return session


def list_matches(cfg: Config, session: requests.Session) -> int:
    """Debug helper: show what the watch terms match right now. Doesn't touch state."""
    outcome = Scanner(cfg, session).scan()
    scan = outcome.scan
    if not scan.ok:
        print(f"scan failed: {scan.reason}", file=sys.stderr)
        return 1
    order = {StockState.IN_STOCK: 0, StockState.UNKNOWN: 1, StockState.OUT_OF_STOCK: 2}
    print(f"Watch terms: {', '.join(cfg.watch_terms)}  ({outcome.requests_made} requests)\n")
    print(f"Matching ({len(scan.products)}):")
    for det in sorted(scan.products, key=lambda d: (order[d.state], d.name)):
        price = f"  {det.price}" if det.price else ""
        qty = f"  qty {det.quantity}" if det.quantity else ""
        print(f"  [{det.state.value:<12}] {det.name}{price}{qty}")
        if det.state is StockState.UNKNOWN:
            print(f"                   reason: {det.reason}")
        print(f"                   {det.url}")
    if outcome.excluded:
        print(f"\nExcluded by EXCLUDE ({len(outcome.excluded)}):")
        for name in outcome.excluded:
            print(f"  {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch Maine Vintage Toys for Pokemon restocks.")
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    parser.add_argument("--dry-run", action="store_true", help="log notifications instead of sending")
    parser.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    parser.add_argument("--list", action="store_true", help="print current matches and exit")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg.log_level)

    notifier: Notifier = (
        LogNotifier() if args.dry_run
        else NtfyNotifier(cfg.ntfy_server, cfg.ntfy_topic, cfg.ntfy_token)
    )

    if args.test_notify:
        ok = notifier.send(Notification(
            kind="test",
            title="MVT stock watcher test",
            message="If you can read this, notifications work. Tap to open the store.",
            priority=PRIORITY_HIGH,
            click_url=cfg.store_url,
            tags=("test_tube",),
        ))
        return 0 if ok else 1

    session = make_session()
    if args.list:
        return list_matches(cfg, session)

    watcher = Watcher(cfg, notifier, session=session)
    if args.once:
        watcher.run_once()
        return 0

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        watcher.run_forever()
    except (KeyboardInterrupt, SystemExit):
        log.info("watcher stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
