"""持仓状态机回归测试：这些边界都是实际踩过的坑。

跑法：python3 -m pytest tests/ -q    或    python3 tests/test_ledger.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from leadmon import ledger  # noqa: E402


def fill(t, sym, pos_side, qty, price, realized=0.0):
    return {
        "time": t, "symbol": sym, "position_side": pos_side,
        "side": "BUY" if qty > 0 else "SELL", "qty": abs(qty), "price": price,
        "realized": realized, "fee": 0.0, "quote_qty": abs(qty) * price,
    }


def run(fills, key):
    return ledger.run_state_machine(ledger.build_events(fills))[key]


def test_open_close():
    b = run([fill(1_000, "A", "LONG", 10, 100), fill(9_000, "A", "LONG", -10, 110)], ("A", "LONG"))
    assert b["net"] == 0
    assert round(b["realized_model"], 2) == 100
    assert [e["action"] for e in b["events"]] == ["OPEN", "CLOSE"]


def test_partial_close():
    b = run([fill(1_000, "B", "LONG", 10, 100), fill(9_000, "B", "LONG", -7, 110)], ("B", "LONG"))
    st = ledger.cycle_state(b)
    assert round(b["net"], 6) == 3
    assert st["status"] == "部分平仓后持有"
    assert round(st["closed_ratio"], 1) == 70.0
    assert round(b["realized_model"], 2) == 70


def test_flip_cost_uses_remaining_only():
    """反手时新方向成本只算剩余净额（曾把 120 算成 360）。"""
    b = run([fill(1_000, "C", "LONG", 10, 100), fill(9_000, "C", "LONG", -15, 120)], ("C", "LONG"))
    assert round(b["net"], 6) == -5
    assert round(b["cost"] / abs(b["net"]), 2) == 120
    assert round(b["realized_model"], 2) == 200


def test_mixed_bucket_long():
    """同一 2 秒桶里买卖都有：要先平后开，不能吞掉减仓。"""
    b = run([fill(1_000, "D", "LONG", 10, 100), fill(1_500, "D", "LONG", -4, 110)], ("D", "LONG"))
    assert [e["action"] for e in b["events"]] == ["OPEN", "REDUCE"]
    assert round(b["net"], 6) == 6
    assert round(b["cost"] / b["net"], 2) == 100
    assert round(b["realized_model"], 2) == 40


def test_mixed_bucket_short():
    b = run([fill(1_000, "E", "SHORT", -10, 100), fill(1_500, "E", "SHORT", 4, 90)], ("E", "SHORT"))
    assert [e["action"] for e in b["events"]] == ["OPEN", "REDUCE"]
    assert round(b["net"], 6) == -6
    assert round(b["realized_model"], 2) == 40


def test_bucket_aggregates_burst():
    """同一毫秒几十笔拆单应聚合成一次操作。"""
    fills = [fill(1_000, "F", "LONG", 1, 100) for _ in range(50)]
    events = ledger.build_events(fills)
    assert len(events) == 1
    b = ledger.run_state_machine(events)[("F", "LONG")]
    assert len(b["events"]) == 1
    assert round(b["net"], 6) == 50


T0 = 1_792_000_000_000  # 真实量级的毫秒时间戳


def test_cutoff_respects_closed_cycle():
    """上一轮周期平仓之后的成交才算当前持仓；平仓尾巴要剔除。"""
    cycles = [{"symbol": "G", "side": "LONG", "closed": T0, "is_open": 0}]
    fills = [
        fill(T0 - 5_000, "G", "LONG", 10, 100),    # 上一轮持仓，剔除
        fill(T0 + 800, "G", "LONG", -10, 110),     # 平仓尾巴（晚于 closed 1 秒内），剔除
        fill(T0 + 3_600_000, "G", "LONG", 7, 200), # 新周期，保留
    ]
    pos = ledger.analyze_positions(fills, cycles, grace_ms=60_000)
    assert len(pos) == 1, pos
    assert round(pos[0]["qty"], 6) == 7
    assert round(pos[0]["avg_cost"], 2) == 200


def test_fast_reopen_not_dropped():
    """平仓后 60 秒内反手重开，新仓不能被宽限吃掉（只剔平仓方向的成交）。"""
    cycles = [{"symbol": "H", "side": "LONG", "closed": T0, "is_open": 0}]
    fills = [
        fill(T0 + 500, "H", "LONG", -10, 110),     # 平仓尾巴，剔除
        fill(T0 + 30_000, "H", "LONG", 5, 120),    # 30 秒后重开，必须保留
    ]
    pos = ledger.analyze_positions(fills, cycles, grace_ms=60_000)
    assert len(pos) == 1, pos
    assert round(pos[0]["qty"], 6) == 5


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✅ {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  ❌ {name}: {exc}")
    raise SystemExit(1 if failures else 0)
