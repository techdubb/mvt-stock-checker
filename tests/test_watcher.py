"""Watcher loop tests with a fake HTTP session, fake clock, and recording notifier."""

import copy
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

from config import Config
from state import Notification, load_state
from watcher import Watcher

FIXTURES = Path(__file__).parent / "fixtures"
CELEBRATION = json.loads((FIXTURES / "search_30th_celebration.json").read_text())
DELTA = json.loads((FIXTURES / "search_delta_reign.json").read_text())
ETB = "Pokemon 30th Celebration Elite Trainer Box"


def restocked(payload, prefix=ETB):
    """Simulate a restock of one real record by flipping its stock signals."""
    payload = copy.deepcopy(payload)
    raw = next(p for p in payload["data"] if p["name"].startswith(prefix))
    raw["badges"]["out_of_stock"] = False
    raw["inventory"]["all_variations_sold_out"] = False
    raw["inventory"]["total"] = 4
    return payload


def with_new_listing(payload, name, sold_out=True):
    payload = copy.deepcopy(payload)
    raw = copy.deepcopy(payload["data"][0])
    raw.update(id="NEWLISTING", square_id="NEWLISTING", name=name, visibility="visible")
    raw["badges"]["out_of_stock"] = sold_out
    raw["inventory"].update(all_variations_sold_out=sold_out, total=0 if sold_out else 12)
    payload["data"].append(raw)
    return payload


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None, headers=None):
        self.status_code = status
        self.payload = payload
        self.text = text if text is not None else json.dumps(payload)
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """Returns queued responses; an Exception instance is raised instead."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs.get("params"))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def cycle(celebration=CELEBRATION, delta=DELTA):
    """One poll cycle = one search per default watch term."""
    return [FakeResponse(payload=celebration), FakeResponse(payload=delta)]


class RecordingNotifier:
    def __init__(self, ok=True):
        self.sent: list[Notification] = []
        self.ok = ok

    def send(self, note):
        self.sent.append(note)
        return self.ok(note) if callable(self.ok) else self.ok


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 2, 3, 0, tzinfo=ZoneInfo("America/New_York"))

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


@pytest.fixture
def cfg(tmp_path):
    return Config(ntfy_topic="test", state_file=tmp_path / "state.json")


def make(cfg, responses, notifier=None):
    clock = Clock()
    session = FakeSession(responses)
    notifier = notifier or RecordingNotifier()
    sleeps = []
    watcher = Watcher(
        cfg, notifier, session=session, now=clock, sleep=sleeps.append, rng=random.Random(0)
    )
    return watcher, session, notifier, clock, sleeps


def run(watcher, clock, n):
    delays = []
    for _ in range(n):
        delay = watcher.run_once()
        delays.append(delay)
        clock.advance(delay)
    return delays


def kinds(notifier):
    return [n.kind for n in notifier.sent]


def titles(notifier):
    return [n.title for n in notifier.sent]


def test_each_cycle_searches_each_term_with_polite_gap(cfg):
    watcher, session, notifier, clock, sleeps = make(cfg, cycle())
    delays = run(watcher, clock, 1)
    assert [c["q"] for c in session.calls] == ["30th celebration", "delta reign"]
    assert sleeps == [cfg.request_gap_seconds]  # gap between the two requests
    assert 144 <= delays[0] <= 216


def test_first_run_alerts_on_what_is_already_in_stock_only(cfg):
    watcher, _, notifier, clock, _ = make(cfg, cycle())
    run(watcher, clock, 1)
    # The live fixture has one English 30th Celebration product in stock.
    assert kinds(notifier) == ["restock"]
    assert "Battle Deck" in notifier.sent[0].title
    state = load_state(cfg.state_file)
    assert state.seeded
    assert all("Japanese" not in p.name and "CHINESE" not in p.name.upper() for p in state.products.values())


def test_restock_of_known_product_alerts_after_two_reads(cfg):
    responses = cycle() * 2 + cycle(restocked(CELEBRATION)) * 2
    watcher, _, notifier, clock, _ = make(cfg, responses)
    run(watcher, clock, 3)
    assert kinds(notifier) == ["restock"]  # just the Battle Deck from the first run
    run(watcher, clock, 1)
    assert kinds(notifier) == ["restock", "restock"]
    assert ETB in notifier.sent[-1].title
    assert notifier.sent[-1].click_url.startswith("https://www.mainevintagetoys.com/product/")


def test_new_delta_reign_listing(cfg):
    sold_out = with_new_listing(DELTA, "Pokemon Mega Evolution Delta Reign Elite Trainer Box")
    in_stock = with_new_listing(DELTA, "Pokemon Mega Evolution Delta Reign Elite Trainer Box", sold_out=False)
    watcher, _, notifier, clock, _ = make(cfg, cycle() + cycle(delta=sold_out) + cycle(delta=in_stock) * 2)
    run(watcher, clock, 2)
    assert kinds(notifier) == ["restock", "new_listing"]
    run(watcher, clock, 2)
    assert kinds(notifier) == ["restock", "new_listing", "restock"]
    assert "Delta Reign Elite Trainer Box" in notifier.sent[-1].title


def test_japanese_delta_reign_listing_is_ignored(cfg):
    jp = with_new_listing(DELTA, "Pokemon Japanese Delta Reign Booster Box", sold_out=False)
    watcher, _, notifier, clock, _ = make(cfg, cycle() + cycle(delta=jp))
    run(watcher, clock, 2)
    assert kinds(notifier) == ["restock"]


def test_partial_failure_does_not_touch_products(cfg):
    responses = cycle() + [FakeResponse(payload=CELEBRATION), FakeResponse(503, text="down")]
    watcher, _, notifier, clock, _ = make(cfg, responses)
    run(watcher, clock, 1)
    before = load_state(cfg.state_file).products
    run(watcher, clock, 1)
    assert load_state(cfg.state_file).products == before
    assert load_state(cfg.state_file).unhealthy_since is not None


@pytest.mark.parametrize("bad", [
    FakeResponse(429, text="slow down", headers={"Retry-After": "600"}),
    FakeResponse(503, text="down"),
    FakeResponse(200, text="<html><title>Just a moment...</title></html>"),
    requests.Timeout("timed out"),
])
def test_blocking_responses_back_off(cfg, bad):
    watcher, session, _, clock, _ = make(cfg, [bad, bad])
    delays = run(watcher, clock, 2)
    assert len(session.calls) == 2  # stops the cycle at the first failure
    assert delays[0] >= 360
    assert delays[1] > delays[0] or delays[1] >= 600


def test_backoff_resets_after_success(cfg):
    watcher, _, _, clock, _ = make(cfg, [FakeResponse(503, text="x")] + cycle())
    delays = run(watcher, clock, 2)
    assert delays[0] == 360
    assert delays[1] <= 216


def test_empty_search_is_a_failure_not_a_sellout(cfg):
    empty = {"data": [], "meta": {"pagination": {"total_pages": 1}}}
    watcher, _, notifier, clock, _ = make(cfg, cycle() + [FakeResponse(payload=empty)])
    run(watcher, clock, 2)
    state = load_state(cfg.state_file)
    assert state.unhealthy_since is not None
    assert kinds(notifier) == ["restock"]


def test_pagination_is_followed_and_capped(cfg):
    page = copy.deepcopy(DELTA)
    page["meta"]["pagination"]["total_pages"] = 10
    watcher, session, _, clock, _ = make(cfg, [FakeResponse(payload=CELEBRATION)] + [FakeResponse(payload=page)] * 3)
    run(watcher, clock, 1)
    assert [c["page"] for c in session.calls] == [1, 1, 2, 3]


def test_failed_restock_push_is_retried_next_cycle(cfg):
    attempts = []

    def flaky(note):
        attempts.append(note.kind)
        return len(attempts) > 1

    watcher, _, notifier, clock, _ = make(cfg, cycle() * 3, RecordingNotifier(ok=flaky))
    run(watcher, clock, 1)
    assert not load_state(cfg.state_file).products[notifier.sent[0].product_id].alerted
    run(watcher, clock, 2)
    assert kinds(notifier) == ["restock", "restock"]
    assert load_state(cfg.state_file).products[notifier.sent[0].product_id].alerted
