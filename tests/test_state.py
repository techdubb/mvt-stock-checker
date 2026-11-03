from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from config import Config
from detect import ProductDetection, Scan, StockState
from state import WatchState, load_state, save_state, step

NY = ZoneInfo("America/New_York")
URL = "https://www.mainevintagetoys.com/product/etb/A"


def det(state, pid="A", name="Pokemon 30th Celebration Elite Trainer Box"):
    return ProductDetection(pid, name, URL, state, "test", price="$99.99", quantity=3)


IN = det(StockState.IN_STOCK)
OUT = det(StockState.OUT_OF_STOCK)
UNK = det(StockState.UNKNOWN)
FAILED = Scan(False, "HTTP 503")


def ok(*dets):
    return Scan(True, "ok", tuple(dets))


@pytest.fixture
def cfg(tmp_path):
    return Config(ntfy_topic="test", state_file=tmp_path / "state.json")


class Clock:
    """Starts at 03:00 New York time so the 09:00 heartbeat stays out of the way."""

    def __init__(self, start=datetime(2026, 11, 2, 3, 0, tzinfo=NY)):
        self.t = start

    def tick(self, seconds=180):
        self.t += timedelta(seconds=seconds)
        return self.t


def run(cfg, clock, scans, state=None):
    state = state or WatchState()
    sent = []
    for scan in scans:
        state, notes = step(state, scan, clock.tick(), cfg)
        sent.extend(notes)
    return state, sent


def kinds(notes):
    return [n.kind for n in notes]


def test_out_of_stock_sends_nothing(cfg):
    state, sent = run(cfg, Clock(), [ok(OUT)] * 10)
    assert sent == []
    assert state.products["A"].current == "OUT_OF_STOCK"
    assert state.seeded


def test_restock_alerts_once_after_confirmations(cfg):
    clock = Clock()
    state, sent = run(cfg, clock, [ok(OUT), ok(OUT), ok(IN)])
    assert sent == []  # first IN read isn't enough
    state, sent = run(cfg, clock, [ok(IN)], state)
    assert kinds(sent) == ["restock"]
    assert sent[0].priority == "urgent"
    assert sent[0].click_url == URL
    assert sent[0].product_id == "A"
    assert "$99.99" in sent[0].message
    state, sent = run(cfg, clock, [ok(IN)] * 3, state)  # within 15 min: no reminders yet
    assert sent == []


def test_product_already_in_stock_on_first_scan_alerts_immediately(cfg):
    _, sent = run(cfg, Clock(), [ok(IN)])
    assert kinds(sent) == ["restock"]
    assert not sent[0].title.startswith("NEW")  # first scan: everything is "new"


def test_new_listing_in_stock_alerts_immediately(cfg):
    clock = Clock()
    state, _ = run(cfg, clock, [ok(OUT)])
    new = det(StockState.IN_STOCK, pid="B", name="Pokemon Delta Reign Booster Box")
    _, sent = run(cfg, clock, [ok(OUT, new)], state)
    assert kinds(sent) == ["restock"]
    assert sent[0].title.startswith("NEW + IN STOCK")


def test_new_sold_out_listing_gets_heads_up(cfg):
    clock = Clock()
    state, _ = run(cfg, clock, [ok(OUT)])
    new = det(StockState.OUT_OF_STOCK, pid="B", name="Pokemon Delta Reign Elite Trainer Box")
    state, sent = run(cfg, clock, [ok(OUT, new)], state)
    assert kinds(sent) == ["new_listing"]
    assert sent[0].priority == "default"
    _, sent = run(cfg, clock, [ok(OUT, new)] * 3, state)
    assert sent == []  # only once


def test_new_listing_alerts_can_be_disabled(tmp_path):
    cfg = Config(ntfy_topic="t", state_file=tmp_path / "s.json", alert_new_listings=False)
    clock = Clock()
    state, _ = run(cfg, clock, [ok(OUT)])
    _, sent = run(cfg, clock, [ok(OUT, det(StockState.OUT_OF_STOCK, pid="B"))], state)
    assert sent == []


def test_first_scan_does_not_announce_sold_out_listings(cfg):
    _, sent = run(cfg, Clock(), [ok(OUT, det(StockState.OUT_OF_STOCK, pid="B"))])
    assert sent == []


def test_products_are_tracked_independently(cfg):
    clock = Clock()
    b_out = det(StockState.OUT_OF_STOCK, pid="B", name="Mini Tin")
    b_in = det(StockState.IN_STOCK, pid="B", name="Mini Tin")
    state, _ = run(cfg, clock, [ok(OUT, b_out)])
    state, sent = run(cfg, clock, [ok(IN, b_out), ok(IN, b_in)], state)
    assert kinds(sent) == ["restock"]
    assert sent[0].product_id == "A"
    _, sent = run(cfg, clock, [ok(IN, b_in)], state)
    assert kinds(sent) == ["restock"]
    assert sent[0].product_id == "B"


def test_unknown_breaks_confirmation_streak(cfg):
    clock = Clock()
    state, _ = run(cfg, clock, [ok(OUT)])
    _, sent = run(cfg, clock, [ok(IN), ok(UNK), ok(IN), ok(UNK), ok(IN)], state)
    assert sent == []


def test_failed_scan_leaves_products_untouched(cfg):
    clock = Clock()
    state, _ = run(cfg, clock, [ok(OUT), ok(IN)])
    state, _ = run(cfg, clock, [FAILED], state)
    assert state.products["A"].consecutive_in_stock == 1
    _, sent = run(cfg, clock, [ok(IN)], state)
    assert kinds(sent) == ["restock"]


def test_missing_product_keeps_state(cfg):
    clock = Clock()
    state, _ = run(cfg, clock, [ok(IN)])
    state, sent = run(cfg, clock, [ok()] * 3, state)
    assert sent == []
    assert state.products["A"].alerted  # not re-armed by disappearing
    _, sent = run(cfg, clock, [ok(IN)], state)
    assert sent == []  # comes back within the same episode: no duplicate


def test_unknown_mid_episode_does_not_double_alert(cfg):
    clock = Clock()
    state, sent = run(cfg, clock, [ok(OUT), ok(IN), ok(IN)])
    assert kinds(sent) == ["restock"]
    _, sent = run(cfg, clock, [ok(UNK), ok(IN), ok(IN)], state)
    assert "restock" not in kinds(sent)


def test_sold_out_rearms(cfg):
    clock = Clock()
    state, sent = run(cfg, clock, [ok(OUT), ok(IN), ok(IN), ok(OUT), ok(IN), ok(IN)])
    assert kinds(sent) == ["restock", "restock"]


def test_reminders_capped(cfg):
    clock = Clock()
    state, sent = run(cfg, clock, [ok(OUT), ok(IN), ok(IN)])
    assert kinds(sent) == ["restock"]
    for _ in range(20):
        state, notes = step(state, ok(IN), clock.tick(15 * 60), cfg)
        sent.extend(notes)
    assert kinds(sent) == ["restock", "reminder", "reminder", "reminder"]
    assert sent[-1].title.startswith("Still in stock (3/3)")


def test_failure_alert_after_30_min_then_recovery(cfg):
    clock = Clock()
    state, sent = run(cfg, clock, [ok(OUT)])
    state, sent = run(cfg, clock, [FAILED] * 11, state)  # 30 min of failures
    assert kinds(sent) == ["failure"]
    state, sent = run(cfg, clock, [FAILED] * 10, state)
    assert sent == []  # once per incident
    _, sent = run(cfg, clock, [ok(OUT)], state)
    assert kinds(sent) == ["recovered"]


def test_unknown_product_is_not_a_store_failure(cfg):
    _, sent = run(cfg, Clock(), [ok(UNK)] * 20)
    assert sent == []


def test_heartbeat_once_a_day(cfg):
    clock = Clock(datetime(2026, 11, 2, 8, 50, tzinfo=NY))
    _, sent = run(cfg, clock, [ok(OUT, det(StockState.IN_STOCK, pid="B", name="Battle Deck"))] * 10)
    beats = [n for n in sent if n.kind == "heartbeat"]
    assert len(beats) == 1
    assert "2 matching products, 1 in stock" in beats[0].message
    assert "Battle Deck" in beats[0].message


def test_state_roundtrip_prevents_duplicate_alert_after_restart(cfg):
    clock = Clock()
    state, sent = run(cfg, clock, [ok(OUT), ok(IN), ok(IN)])
    assert kinds(sent) == ["restock"]
    save_state(cfg.state_file, state)
    restored = load_state(cfg.state_file)
    assert restored == state
    _, sent = run(cfg, clock, [ok(IN)], restored)
    assert "restock" not in kinds(sent)


def test_corrupt_state_file_starts_fresh(cfg):
    cfg.state_file.write_text("{not json")
    assert load_state(cfg.state_file) == WatchState()
