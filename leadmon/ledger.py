"""统计与对账。

币安给的是"持仓周期"聚合（一笔 = 开仓到某次平仓），不是逐笔成交，所以这里做两件事：

1. 把已平仓周期滚成统计（胜率/盈亏/持仓时长），这是"他到底怎么打"的主线；
2. 把"当前持仓"和"历史里还没走完的周期"对账 —— 对不上就说明有刚开的新仓
   或者接口还没同步，这恰恰是盯盘最想看的东西。
"""

from __future__ import annotations

WINDOWS = (("7D", 7), ("30D", 30), ("90D", 90), ("ALL", None))


def _aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"trades": 0, "pnl": 0.0, "win_rate": None, "avg_roi": None,
                "avg_hold_min": None, "best": None, "worst": None,
                "long": 0, "short": 0}
    pnls = [r.get("pnl") or 0.0 for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    rois = [r["roi"] for r in rows if r.get("roi") is not None]
    holds = [r["duration_min"] for r in rows if r.get("duration_min")]
    best = max(rows, key=lambda r: r.get("pnl") or 0.0)
    worst = min(rows, key=lambda r: r.get("pnl") or 0.0)
    return {
        "trades": len(rows),
        "pnl": round(sum(pnls), 2),
        "win_rate": round(100.0 * wins / len(rows), 1),
        "avg_roi": round(100.0 * sum(rois) / len(rois), 2) if rois else None,
        "avg_hold_min": round(sum(holds) / len(holds), 0) if holds else None,
        "best": {"symbol": best.get("symbol"), "pnl": round(best.get("pnl") or 0.0, 2)},
        "worst": {"symbol": worst.get("symbol"), "pnl": round(worst.get("pnl") or 0.0, 2)},
        "long": sum(1 for r in rows if r.get("side") == "LONG"),
        "short": sum(1 for r in rows if r.get("side") == "SHORT"),
    }


def window_stats(closed: list[dict], now_sec: int) -> dict:
    """closed 已按 opened 倒序。返回 {7D, 30D, 90D, ALL} 四档。"""
    out = {}
    for label, days in WINDOWS:
        if days is None:
            rows = closed
        else:
            cutoff = (now_sec - days * 86400) * 1000  # 记录里是毫秒
            rows = [r for r in closed if (r.get("closed") or r.get("opened") or 0) >= cutoff]
        out[label] = _aggregate(rows)
    return out


def by_symbol(closed: list[dict], limit: int = 12) -> list[dict]:
    """按币种聚合：这个人在哪几个标的上反复做、赚不赚钱。"""
    book: dict[str, dict] = {}
    for r in closed:
        state = book.setdefault(
            r["symbol"],
            {"symbol": r["symbol"], "trades": 0, "pnl": 0.0, "wins": 0,
             "volume": 0.0, "last_closed": 0},
        )
        state["trades"] += 1
        state["pnl"] += r.get("pnl") or 0.0
        if (r.get("pnl") or 0.0) > 0:
            state["wins"] += 1
        state["volume"] += r.get("volume") or 0.0
        state["last_closed"] = max(state["last_closed"], r.get("closed") or 0)

    rows = []
    for state in book.values():
        rows.append({
            "symbol": state["symbol"],
            "trades": state["trades"],
            "pnl": round(state["pnl"], 2),
            "win_rate": round(100.0 * state["wins"] / state["trades"], 1),
            "volume": round(state["volume"], 4),
            "last_closed": state["last_closed"],
        })
    rows.sort(key=lambda r: -r["pnl"])
    return rows[:limit]


def reconcile(live: list[dict], closed: list[dict]) -> list[dict]:
    """当前持仓 vs 历史里未走完的周期。

    - live_only: 接口有持仓、历史里没有未平记录 → 刚开的仓，或历史还没同步
    - history_only: 历史说没走完、接口却没持仓 → 已平仓但接口滞后
    - matched: 两边对得上
    """
    open_hist = {(r["symbol"], r["side"]): r for r in closed if r.get("is_open")}
    live_map = {(p["symbol"], p["side"]): p for p in live}

    notes = []
    for key, pos in live_map.items():
        hist = open_hist.get(key)
        notes.append({
            "kind": "matched" if hist else "live_only",
            "symbol": key[0],
            "side": key[1],
            "live_qty": pos.get("qty"),
            "hist_qty": hist.get("qty") if hist else None,
            "hist_open_qty": hist.get("open_qty") if hist else None,
            "opened": hist.get("opened") if hist else None,
        })
    for key, hist in open_hist.items():
        if key not in live_map:
            notes.append({
                "kind": "history_only",
                "symbol": key[0],
                "side": key[1],
                "live_qty": None,
                "hist_qty": hist.get("qty"),
                "hist_open_qty": hist.get("open_qty"),
                "opened": hist.get("opened"),
            })
    notes.sort(key=lambda n: (n["kind"] != "live_only", n["symbol"]))
    return notes


def unclosed_cycles(cycles: list[dict]) -> list[dict]:
    return [c for c in cycles if c.get("is_open")]


# ---------------------------------------------------------------- 持仓周期状态机

ACTION_LABEL = {
    "OPEN": "开仓",
    "ADD": "加仓",
    "REDUCE": "部分平仓",
    "CLOSE": "全平",
    "FLIP": "反手",
}


def build_events(fills: list[dict], bucket_ms: int = 2000) -> list[dict]:
    """把同一时刻的批量成交聚合成一次"操作"。

    他会在一瞬间砸几十上百笔（同一毫秒/相邻毫秒），不聚合的话事件流没法看。
    """
    grouped: dict[tuple, dict] = {}
    order: list[tuple] = []
    for f in fills:
        key = (f["symbol"], f["position_side"], (f["time"] // bucket_ms) * bucket_ms)
        st = grouped.get(key)
        if st is None:
            st = grouped[key] = {
                "symbol": f["symbol"], "side": f["position_side"], "ts": key[2],
                "buy": 0.0, "sell": 0.0, "buy_notional": 0.0, "sell_notional": 0.0,
                "realized_api": 0.0, "fee": 0.0, "fills": 0,
            }
            order.append(key)
        q, p = f.get("qty") or 0.0, f.get("price") or 0.0
        if f["side"] == "BUY":
            st["buy"] += q
            st["buy_notional"] += q * p
        else:
            st["sell"] += q
            st["sell_notional"] += q * p
        st["realized_api"] += f.get("realized") or 0.0
        st["fee"] += f.get("fee") or 0.0
        st["fills"] += 1
    return [grouped[k] for k in order]


def _new_book(ev: dict) -> dict:
    return {
        "symbol": ev["symbol"], "side": ev["side"],
        "net": 0.0, "cost": 0.0, "peak": 0.0,
        "bought": 0.0, "sold": 0.0,
        "realized_model": 0.0, "realized_api": 0.0, "fees": 0.0,
        "first_ts": ev["ts"], "last_ts": ev["ts"], "fills": 0,
        "events": [],
    }


def _record(book: dict, buy: float, sell: float, notional: float,
            realized_api: float, fee: float, fills: int, ts: int) -> None:
    """把一次单向操作记进账本。buy/sell 只有一个非零。"""
    prev = book["net"]
    delta = buy - sell
    total = buy + sell
    price = notional / total if total else 0.0

    if prev == 0:
        action = "OPEN"
        book["cost"] = abs(delta) * price
    elif (prev > 0) == (delta > 0):
        action = "ADD"
        book["cost"] += abs(delta) * price
    else:
        closing = min(abs(delta), abs(prev))
        avg = book["cost"] / abs(prev) if prev else 0.0
        book["realized_model"] += closing * (price - avg) * (1 if prev > 0 else -1)
        book["cost"] -= closing * avg
        new_net = prev + delta
        if abs(new_net) <= 1e-12:
            action = "CLOSE"
            book["cost"] = 0.0
        elif abs(delta) < abs(prev) - 1e-12:
            action = "REDUCE"
        else:
            action = "FLIP"
            # 反手后新方向的成本只按**剩余净额**算，不能按整笔成交量算
            book["cost"] = abs(new_net) * price

    book["net"] = prev + delta
    book["peak"] = max(book["peak"], abs(book["net"]))
    book["bought"] += buy
    book["sold"] += sell
    book["realized_api"] += realized_api
    book["fees"] += fee
    book["fills"] += fills
    book["last_ts"] = ts
    book["events"].append({
        "ts": ts, "action": action, "label": ACTION_LABEL[action],
        "buy": round(buy, 6), "sell": round(sell, 6), "price": round(price, 6),
        "remaining": round(book["net"], 6), "fills": fills,
        "realized_api": round(realized_api, 4),
        "realized_model": round(book["realized_model"], 4),
    })


def run_state_machine(events: list[dict]) -> dict[tuple[str, str], dict]:
    """每个 (币种, 方向) 跑一遍开/加/减/平/反手，算出剩余持仓与已实现盈亏。

    成本用加权平均法；减仓按比例释放成本，已实现 = (成交价 - 均价) × 平掉的数量。
    接口自报的 realizedProfit 单独累计，用于交叉校验。

    两种边界都处理了：
    - 同一个 2 秒桶里既有买又有卖：拆成"先平后开"两个动作，别把减仓吞掉
    - 反手：新方向成本只按剩余净额计算
    """
    books: dict[tuple[str, str], dict] = {}
    for ev in events:
        key = (ev["symbol"], ev["side"])
        book = books.setdefault(key, _new_book(ev))
        buy, sell = ev["buy"], ev["sell"]
        notional = ev["buy_notional"] + ev["sell_notional"]
        realized = ev["realized_api"]
        fee = ev["fee"]
        fills = ev["fills"]

        if buy > 0 and sell > 0:
            # 同桶既有买又有卖：拆成两个单向动作，顺序按"谁在平仓"决定
            total_notional = ev["buy_notional"] + ev["sell_notional"]
            if book["net"] > 0:
                order = [("SELL", sell, ev["sell_notional"]), ("BUY", buy, ev["buy_notional"])]
            elif book["net"] < 0:
                order = [("BUY", buy, ev["buy_notional"]), ("SELL", sell, ev["sell_notional"])]
            elif ev["side"] == "SHORT":
                order = [("SELL", sell, ev["sell_notional"]), ("BUY", buy, ev["buy_notional"])]
            else:
                order = [("BUY", buy, ev["buy_notional"]), ("SELL", sell, ev["sell_notional"])]
            for idx, (direction, qty, side_notional) in enumerate(order):
                share = side_notional / total_notional if total_notional else 0.0
                if direction == "BUY":
                    _record(book, qty, 0.0, side_notional, realized * share, fee * share,
                            fills if idx == 0 else 0, ev["ts"])
                else:
                    _record(book, 0.0, qty, side_notional, realized * share, fee * share,
                            fills if idx == 0 else 0, ev["ts"])
            continue

        _record(book, buy, sell, notional, realized, fee, fills, ev["ts"])
    return books


def cycle_state(book: dict) -> dict:
    """一笔仓位当前处于什么状态：全平 / 部分平仓 / 持有中。"""
    net = abs(book["net"])
    peak = book["peak"] or 0.0
    closed_qty = max(peak - net, 0.0)
    ratio = (100.0 * closed_qty / peak) if peak else 0.0
    has_reduce = any(e["action"] in ("REDUCE", "CLOSE", "FLIP") for e in book["events"])

    if net <= 1e-9:
        status = "已全平"
    elif book["net"] < 0 and book["peak"] and any(e["action"] == "FLIP" for e in book["events"]):
        status = "反手持有"
    elif has_reduce:
        status = "部分平仓后持有"
    else:
        status = "持有中（未减仓）"

    return {
        "status": status,
        "peak_qty": round(peak, 6),
        "closed_qty": round(closed_qty, 6),
        "closed_ratio": round(ratio, 2),
        "remaining": round(net, 6),
        "bought": round(book["bought"], 6),
        "sold": round(book["sold"], 6),
        "realized_model": round(book["realized_model"], 2),
        "realized_api": round(book["realized_api"], 2),
        "pnl_diff": round(book["realized_model"] - book["realized_api"], 2),
    }


def analyze_positions(fills: list[dict], cycles: list[dict],
                      grace_ms: int = 60_000, event_bucket_ms: int = 2_000) -> list[dict]:
    """当前持仓的完整分析：量、均价、已平多少、部分平仓还是全平、逐次操作事件。

    只取"上一轮周期平仓之后"的成交再跑状态机 —— 否则历史成交被 6000 条上限截断，
    从零累加会留下一个虚假的反向残余仓。
    """
    # 每个 (币种, 方向) 取最后一轮周期的平仓点
    last_cycle: dict[tuple[str, str], dict] = {}
    for c in cycles:
        if c.get("is_open") or not c.get("closed"):
            continue
        key = (c["symbol"], c["side"])
        if key not in last_cycle or int(c["closed"]) > int(last_cycle[key]["closed"]):
            last_cycle[key] = c

    def _keep(f: dict) -> bool:
        cyc = last_cycle.get((f["symbol"], f["position_side"]))
        if not cyc:
            return True
        closed = int(cyc["closed"])
        if f["time"] <= closed:
            return False
        # 平仓那一瞬间的尾巴（分批平仓，时间戳比 closed 晚约 1 秒）要剔除；
        # 但只剔"平仓方向"的成交，这样他 60 秒内反手重开的新仓不会被误删
        opposing = "SELL" if cyc["side"] == "LONG" else "BUY"
        if f["side"] == opposing and f["time"] <= closed + grace_ms:
            return False
        return True

    scoped = [f for f in fills if _keep(f)]
    books = run_state_machine(build_events(scoped, event_bucket_ms))

    out = []
    for (symbol, side), book in books.items():
        state = cycle_state(book)
        if abs(book["net"]) <= 1e-9:
            continue  # 已经平干净
        qty = abs(book["net"])
        avg = book["cost"] / qty if qty else None
        out.append({
            "symbol": symbol,
            # 反手后净额符号才是真实方向（币安的 positionSide 不会跟着翻）
            "side": "LONG" if book["net"] > 0 else "SHORT",
            "qty": round(qty, 8),
            "avg_cost": round(avg, 8) if avg else None,
            # 已实现以接口自报为准（每笔成交都带 realizedProfit），
            # 模型值只做交叉校验：两者差得多说明成交数据被截断了
            "realized": (state["realized_api"]
                         if abs(state["realized_api"]) > 1e-9
                         else state["realized_model"]),
            "realized_api": state["realized_api"],
            "pnl_diff": state["pnl_diff"],
            "fees": round(book["fees"], 4),
            "fills": book["fills"],
            "first_fill": book["first_ts"],
            "last_fill": book["last_ts"],
            "peak_qty": state["peak_qty"],
            "closed_qty": state["closed_qty"],
            "closed_ratio": state["closed_ratio"],
            "status": state["status"],
            "events": book["events"][-12:],
            "derived_from_fills": True,
        })
    out.sort(key=lambda r: -(r["qty"] * (r["avg_cost"] or 0)))
    return out
