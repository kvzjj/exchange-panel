#!/usr/bin/env python3
"""侦察/验证工具：直接对币安接口跑一遍，打印原始数据与反推结果。

用途：
  1. 换带单员时先跑它，确认接口字段没变；
  2. 怀疑面板数字不对时跑它，和面板对照（独立路径，不读本地库）。

用法:
    python3 tools/probe.py 5075281354358777856
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from leadmon import client, ledger  # noqa: E402
from leadmon.normalize import (  # noqa: E402
    normalize_closed_position,
    normalize_trade,
)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pid = sys.argv[1]

    print("=" * 70)
    print("1) 逐笔成交 trade-history（真实数据源）")
    print("=" * 70)
    data = client.fetch_trades(pid, page=1, size=200)
    rows = data.get("list") or []
    # 往前多翻几页，直到覆盖最近一轮平仓时间（否则早开的仓位会漏）
    import time as _time
    need_floor = int(_time.time() * 1000) - 7 * 86400 * 1000  # 与服务端 backfill 窗口一致
    page = 1
    while rows and page < 15 and min(r["time"] for r in rows) > need_floor:
        page += 1
        more = client.fetch_trades(pid, page=page, size=200).get("list") or []
        if not more:
            break
        rows.extend(more)
    print(f"翻了 {page} 页成交（覆盖到 {__import__('time').strftime('%m-%d %H:%M', __import__('time').localtime(min(r['time'] for r in rows)/1000)) if rows else '-'}）")
    print(f"本页 {len(rows)} 笔, total={data.get('total')}（6000 是历史总量上限，不是分页总数）")
    if rows:
        print("字段:", sorted(rows[0].keys()))
        print("最新:", json.dumps(rows[0], ensure_ascii=False))
        trades = [normalize_trade(r) for r in rows]
        uniq = {}
        for t in trades:
            uniq.setdefault(t["uid"], t)
        print(f"本页去重后 {len(uniq)} 笔（接口会返回完全重复的行，必须按内容哈希去重）")

    print()
    print("=" * 70)
    print("2) 已平仓周期 position-history")
    print("=" * 70)
    hist = client.fetch_position_history(pid, page=1, size=100)
    cycles_raw = hist.get("list") or []
    print(f"total={hist.get('total')} 本页 {len(cycles_raw)} 条")
    if cycles_raw:
        print("字段:", sorted(cycles_raw[0].keys()))
    cycles = [normalize_closed_position(r) for r in cycles_raw]

    print()
    print("=" * 70)
    print("3) 官方持仓接口 lead-data/positions（匿名恒为空，仅作诊断）")
    print("=" * 70)
    pos = client.fetch_positions(pid)
    print(f"返回 {len(pos)} 条 -> {json.dumps(pos, ensure_ascii=False)[:200]}")

    print()
    print("=" * 70)
    print("4) 用成交反推的当前持仓")
    print("=" * 70)
    fills = [normalize_trade(r) for r in rows]
    seen, deduped = set(), []
    for f in fills:  # 保持时间顺序
        if f["uid"] in seen:
            continue
        seen.add(f["uid"])
        deduped.append(f)
    deduped.sort(key=lambda f: f["time"])

    open_pos = ledger.analyze_positions(deduped, cycles)
    if not open_pos:
        print("空仓（上一轮平仓之后没有净买入）")
    import time as _t
    for p in open_pos:
        print("  %-14s %-5s 数量=%.4f 均价=%.4f 成交=%d 笔 首笔=%s" % (
            p["symbol"], p["side"], p["qty"], p["avg_cost"] or 0, p["fills"],
            _t.strftime("%m-%d %H:%M", _t.localtime(p["first_fill"] / 1000))))
        print("      状态=%s 峰值=%.4f 已平=%.4f (%.1f%%) 已实现 接口=%.2f 模型=%.2f 差=%.2f" % (
            p["status"], p["peak_qty"], p["closed_qty"], p["closed_ratio"],
            p["realized_api"], p["realized"], p["pnl_diff"]))
        for e in p["events"]:
            print("        %s  %-6s 买%9.3f 卖%9.3f @%10.4f -> 剩余%10.3f  (%d 笔)" % (
                _t.strftime("%m-%d %H:%M:%S", _t.localtime(e["ts"] / 1000)),
                e["label"], e["buy"], e["sell"], e["price"], e["remaining"], e["fills"]))

    print()
    print("对照：打开带单主页的「当前持仓」，数量级应一致（允许分批/四舍五入差异）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
