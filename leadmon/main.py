"""轮询带单员成交（= 操作记录）+ 平仓周期，反推当前持仓，生成面板状态。

为什么不用 `lead-data/positions`：那个接口匿名访问恒返回空数组（有仓位时也是空），
只能用逐笔成交自己算。`position-history` 负责"已平掉的周期"，`trade-history` 负责"当前这笔"。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time

from . import client, ledger, notify, panel
from .normalize import normalize_closed_position, normalize_trade
from .store import Store

log = logging.getLogger("leadmon")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_SYNC_PAGES = 30
MAX_FILL_BACKFILL_PAGES = 30


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _fmt_day(ms) -> str | None:
    if not ms or ms < 1e11:
        return None
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000))


# ---------------------------------------------------------------- 成交同步

def sync_trades(cfg: dict, store: Store, portfolio_id: str, deep: bool = False) -> dict:
    """增量抓成交。

    每轮只抓第 1 页（最近 200 笔，约 9 小时），命中已存在的哈希就自然去重；
    deep=True 时（历史同步时）向前翻页，补齐 fill_backfill_days 天，防止进程停机后漏单。
    """
    size = int(cfg.get("fill_page_size", 200))
    ts = int(time.time())
    backfill_ms = int(cfg.get("fill_backfill_days", 7)) * 86400 * 1000
    floor = ts * 1000 - backfill_ms
    added = 0
    pages = 0

    budget = time.time() + float(cfg.get("fill_sync_budget_sec", 45))
    page = 1
    while page <= MAX_FILL_BACKFILL_PAGES:
        data = client.fetch_trades(portfolio_id, page=page, size=size)
        rows = data.get("list") or []
        pages += 1
        page_added = 0
        if rows:
            parsed = [normalize_trade(r) for r in rows]
            page_added = store.upsert_fills(portfolio_id, parsed, ts)
            added += page_added
        if not rows or len(rows) < size:
            break
        if not deep:
            break
        # 整页都是已见过的 -> 更早的页也不会新，停（省掉每 5 分钟重翻 20 页）
        if page > 1 and page_added == 0:
            break
        if time.time() > budget:
            log.warning("成交回填超时预算(%ds)，本轮先停在第 %d 页", int(budget - (budget - float(cfg.get("fill_sync_budget_sec", 45)))), page)
            break
        # 已经翻到 backfill 窗口之外，就不用再往前了
        oldest = min(r["time"] for r in rows)
        if oldest <= floor:
            break
        page += 1

    return {"added": added, "pages": pages, "ts": ts, "total_fills": store.fill_count(portfolio_id)}


# ---------------------------------------------------------------- 周期同步

def sync_cycles(cfg: dict, store: Store, portfolio_id: str, full: bool = False) -> dict:
    """同步 position-history（已平仓周期）。本地为空时全量翻页，之后只抓第 1 页增量。"""
    size = int(cfg.get("history_page_size", 100))
    ts = int(time.time())
    added_total = 0
    pages = 0
    new_records: list[dict] = []
    virgin = store.count_closed(portfolio_id) == 0

    if full or virgin:
        page, total = 1, None
        while page <= MAX_SYNC_PAGES:
            data = client.fetch_position_history(portfolio_id, page=page, size=size)
            rows = data.get("list") or []
            total = data.get("total")
            pages += 1
            if rows:
                parsed = [normalize_closed_position(r) for r in rows]
                store.snapshot("position_history", ts, rows)
                added_total += store.upsert_closed(portfolio_id, parsed, ts)
            if not rows or (total is not None and page * size >= total):
                break
            page += 1
    else:
        known = {r["position_id"] for r in store.closed_positions(portfolio_id)}
        page = 1
        while page <= 5:
            data = client.fetch_position_history(portfolio_id, page=page, size=size)
            rows = data.get("list") or []
            pages += 1
            if not rows:
                break
            parsed = [normalize_closed_position(r) for r in rows]
            fresh = [r for r in parsed if r["position_id"] not in known]
            store.snapshot("position_history", ts, rows)
            # 全部 upsert（已存在的周期可能从"部分平仓"更新成"全平"）
            added_total += store.upsert_closed(portfolio_id, parsed, ts)
            new_records.extend(fresh)
            if not fresh:
                break
            known |= {r["position_id"] for r in parsed}
            if data.get("total") is not None and page * size >= data["total"]:
                break
            page += 1

    log.info("周期同步完成: 新增 %d 条, 翻了 %d 页", added_total, pages)
    return {"added": added_total, "pages": pages, "ts": ts}


def evaluate_data_quality(store: Store, cycles: list[dict], positions: list[dict],
                          marks: dict[str, float], portfolio_id: str,
                          grace_ms: int = 60_000) -> dict:
    """自检：用已平仓周期反过来校验成交是否完整。

    一个周期结束时，窗口内的净成交必须是 0。不是 0 就说明成交被截断
    （接口最多给 6000 条）或者翻页漏了 —— 这会直接让仓位和盈亏算错。
    """
    oldest = store.oldest_fill_time(portfolio_id)
    fills = store.fills(portfolio_id)
    by_key: dict[tuple[str, str], list[dict]] = {}
    for f in fills:
        by_key.setdefault((f["symbol"], f["position_side"]), []).append(f)

    checked = 0
    worst = 0.0
    notes: list[str] = []
    for c in cycles[:30]:
        if not c.get("closed") or not c.get("opened") or c["closed"] < oldest:
            continue
        window = [f for f in by_key.get((c["symbol"], c["side"]), [])
                  if c["opened"] - grace_ms <= f["time"] <= c["closed"] + grace_ms]
        if not window:
            continue
        net = sum(f["qty"] * (1 if f["side"] == "BUY" else -1) for f in window)
        base = abs(c.get("qty") or 0) or 1.0
        gap_pct = abs(net) / base * 100
        checked += 1
        worst = max(worst, gap_pct)
        if gap_pct > 1.0:
            notes.append("%s %s 周期成交缺口 %+.4f（%.1f%%），大概率是历史成交被截断"
                         % (c["symbol"], c["side"], net, gap_pct))

    missing = [p["symbol"] for p in positions if not marks.get(p["symbol"])]
    if missing:
        notes.append("取不到标记价的持仓（浮盈亏按成本价显示）: " + ", ".join(sorted(set(missing))))

    return {"checked": checked, "worst_gap_pct": round(worst, 2), "notes": notes[-5:]}


def prune_old_fills(cfg: dict, store: Store, portfolio_id: str, positions: list[dict]) -> int:
    """清掉过老成交控制库体积；会砍到未平仓开仓记录时直接放弃。"""
    retain_days = int(cfg.get("fill_retain_days", 90))
    if retain_days <= 0:
        return 0
    cutoff = int(time.time() * 1000) - retain_days * 86400 * 1000
    earliest_needed = min((p.get("first_fill") or 0) for p in positions) if positions else 0
    if earliest_needed and cutoff > earliest_needed:
        log.info("跳过成交清理：未平仓最早成交在 %s，清理会丢数据",
                 time.strftime("%Y-%m-%d", time.localtime(earliest_needed / 1000)))
        return 0
    return store.prune_fills(portfolio_id, cutoff)


# ---------------------------------------------------------------- 推送

def collect_push_items(cfg: dict, store: Store, positions: list[dict],
                       cycles: list[dict]) -> list[dict]:
    """挑出还没推送过的新事件（开/加/部分平/反手）和新平仓周期。

    首次启动不回溯历史：只推 startup_grace_sec 之后发生的事，避免一上线刷屏。
    """
    boot = int(store.meta_get("push_boot_ts", "0") or 0)
    if not boot:
        boot = int(time.time()) - int((cfg.get("push") or {}).get("startup_grace_sec", 300))
        store.meta_set("push_boot_ts", str(boot))

    items: list[dict] = []
    for pos in positions:
        for ev in pos.get("events") or []:
            if (ev.get("ts") or 0) / 1000 < boot:
                continue
            uid = f"evt:{pos['symbol']}:{pos['side']}:{ev['ts']}:{ev['action']}"
            if store.push_seen(uid):
                continue
            items.append({"uid": uid, "uids": [uid], "kind": "event",
                          "data": notify.summarize_event(ev, pos)})

    for c in cycles:
        if c.get("is_open"):
            continue
        if (c.get("closed") or 0) / 1000 < boot:
            continue
        uid = f"cycle:{c['position_id']}"
        if store.push_seen(uid):
            continue
        items.append({"uid": uid, "uids": [uid], "kind": "cycle", "data": c})
    return items


def merge_events(items: list[dict]) -> list[dict]:
    """同一轮里同 (币种, 方向, 动作) 的拆单合并成一条，别一次下单推 80 条。"""
    merged: dict[tuple, dict] = {}
    out: list[dict] = []
    for item in items:
        if item["kind"] != "event":
            out.append(item)
            continue
        d = item["data"]
        key = (d["symbol"], d["side"], d["action"])
        cur = merged.get(key)
        if cur is None:
            merged[key] = item
            continue
        prev = cur["data"]
        total_qty = (prev.get("qty") or 0) + (d.get("qty") or 0)
        if total_qty > 0:
            prev["price"] = (((prev.get("qty") or 0) * (prev.get("price") or 0))
                             + ((d.get("qty") or 0) * (d.get("price") or 0))) / total_qty
        prev["qty"] = total_qty
        prev["realized"] = (prev.get("realized") or 0) + (d.get("realized") or 0)
        prev["notional"] = (prev.get("notional") or 0) + (d.get("notional") or 0)
        prev["remaining"] = d.get("remaining") or prev.get("remaining")
        prev["closed_ratio"] = d.get("closed_ratio") or prev.get("closed_ratio")
        prev["ts"] = d.get("ts") or prev.get("ts")
        # 合并后要记下所有成员 uid，否则没被记的那个下轮还会被当成新事件重推
        cur["uids"] = list(cur.get("uids") or [cur["uid"]]) + [item["uid"]]
    out.extend(merged.values())
    return out


def push_dispatch(cfg: dict, store: Store, pusher: "notify.Pusher",
                  positions: list[dict], cycles: list[dict]) -> dict:
    """落库 + 发送。发失败留在队列里下轮重试。"""
    stats = {"queued": 0, "sent": 0, "failed": 0}
    if not pusher.ready:
        return stats

    try:
        items = merge_events(collect_push_items(cfg, store, positions, cycles))
        messages = notify.build_messages(items)
        if messages:
            stats["queued"] = store.push_save(messages, int(time.time()))
    except Exception:  # noqa: BLE001
        log.exception("推送入队失败")

    try:
        for row in store.push_pending(limit=int((cfg.get("push") or {}).get("batch", 8)),
                                      max_attempts=pusher.max_attempts):
            ok, err = pusher.send(row["title"], row["body"],
                                  row.get("tags") or "", row.get("priority") or "")
            store.push_mark(row["uid"], ok, err, pusher.max_attempts)
            if ok:
                stats["sent"] += 1
                log.info("推送成功: %s", row["title"])
            else:
                stats["failed"] += 1
                log.warning("推送失败(将重试): %s -> %s", row["title"], err)
    except Exception:  # noqa: BLE001
        log.exception("推送发送异常")
    return stats


def fetch_official(cfg: dict, portfolio_id: str, previous: dict | None = None) -> dict:
    out = dict(previous or {})
    for time_range in cfg.get("official_ranges", ["7D", "30D", "90D"]):
        try:
            out[time_range] = client.fetch_performance(portfolio_id, time_range)
        except Exception as exc:  # noqa: BLE001
            log.warning("官方绩效 %s 获取失败: %s", time_range, exc)
    return out


# ---------------------------------------------------------------- 状态

def empty_state(cfg: dict, interval: int) -> dict:
    return {
        "ts": 0,
        "iso": "–",
        "title": cfg.get("panel_title", "带单持仓监控"),
        "portfolio_id": str(cfg.get("portfolio_id", "")),
        "trader_url": (
            "https://www.binance.com/zh-CN/copy-trading/lead-details/"
            f"{cfg.get('portfolio_id')}"
        ),
        "poll_interval": interval,
        "positions": [],
        "totals": {"count": 0, "upnl": 0.0, "long_notional": 0.0,
                   "short_notional": 0.0, "net_notional": 0.0},
        "stats": {},
        "by_symbol": [],
        "history": [],
        "recent_fills": [],
        "reconcile": [],
        "history_sync": {},
        "trade_sync": {},
        "official": {},
        "errors": [],
        "starting": True,
    }


def price_positions(open_positions: list[dict], marks: dict[str, float]) -> list[dict]:
    out = []
    for p in open_positions:
        mark = marks.get(p["symbol"]) or p.get("avg_cost")
        qty = p["qty"]
        avg = p["avg_cost"]
        sign = 1 if p["side"] == "LONG" else -1
        upnl = round(qty * ((mark or 0) - (avg or 0)) * sign, 4) if mark and avg else None
        out.append({
            **p,
            "mark_price": mark,
            "notional": round(qty * (mark or avg or 0), 2),
            "upnl": upnl,
            "upnl_pct": round(100.0 * upnl / (qty * avg), 2) if upnl is not None and avg else None,
            "opened_iso": _fmt_day(p.get("first_fill")),
        })
    return out


def build_state(cfg, store, positions, marks, errors, interval, official=None,
                trade_sync=None, history_sync=None) -> dict:
    portfolio_id = str(cfg["portfolio_id"])
    ts = int(time.time())
    cycles = store.closed_positions(portfolio_id)
    stats = ledger.window_stats(cycles, ts)

    recent = [
        {
            "symbol": r["symbol"], "side": r["side"], "status": r["status"],
            "opened_iso": _fmt_day(r["opened"]), "closed_iso": _fmt_day(r["closed"]),
            "duration_min": r["duration_min"], "avg_cost": r["avg_cost"],
            "avg_close": r["avg_close"], "qty": r["qty"], "pnl": r["pnl"],
            "roi": r["roi"], "leverage": r["leverage"], "is_open": bool(r["is_open"]),
        }
        for r in cycles[: int(cfg.get("history_limit", 20))]
    ]

    fills = [
        {
            "time": f["time"],
            "time_iso": _fmt_day(f["time"]),
            "symbol": f["symbol"], "side": f["side"], "position_side": f["position_side"],
            "price": f["price"], "qty": f["qty"], "quote_qty": f["quote_qty"],
            "fee": f["fee"], "realized": f["realized"],
        }
        for f in store.recent_fills(portfolio_id, int(cfg.get("fills_limit", 40)))
    ]

    total_upnl = sum(p.get("upnl") or 0.0 for p in positions)
    long_notional = sum(p["notional"] for p in positions if p["side"] == "LONG")
    short_notional = sum(p["notional"] for p in positions if p["side"] == "SHORT")

    state = empty_state(cfg, interval)
    state.update({
        "ts": ts,
        "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
        "positions": positions,
        "totals": {
            "count": len(positions),
            "upnl": round(total_upnl, 4),
            "long_notional": round(long_notional, 2),
            "short_notional": round(short_notional, 2),
            "net_notional": round(long_notional - short_notional, 2),
        },
        "stats": stats,
        "by_symbol": ledger.by_symbol(cycles, int(cfg.get("symbol_limit", 10))),
        "history": recent,
        "recent_fills": fills,
        "reconcile": ledger.unclosed_cycles(cycles),
        "closed_total": len(cycles),
        "fill_total": store.fill_count(portfolio_id),
        "official": official or {},
        "trade_sync": trade_sync or {},
        "history_sync": history_sync or {},
        "errors": errors,
        "starting": False,
    })
    return state


def main() -> int:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "config.json")
    cfg = load_config(cfg_path)
    logging.basicConfig(
        level=os.environ.get("LEADMON_LOG", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    base = os.path.dirname(os.path.abspath(cfg_path))

    def _path(key: str, default: str) -> str:
        value = cfg.get(key, default)
        return value if os.path.isabs(value) else os.path.join(base, value)

    state_path = _path("state_path", "data/state.json")
    store = Store(_path("db_path", "data/leadmon.db"))
    pusher = notify.Pusher(cfg, base)
    if pusher.ready:
        log.info("推送已启用: %s 主题 %s", pusher.url, pusher.topic)
    else:
        log.warning("推送未启用（config.push.enabled 或凭据缺失）")

    interval = max(10, int(cfg.get("poll_interval_sec", 60)))
    history_interval = max(interval, int(cfg.get("history_interval_sec", 300)))
    portfolio_id = str(cfg.get("portfolio_id", "")).strip()

    if cfg.get("serve_panel", True):
        httpd = panel.serve(
            state_path,
            os.path.join(ROOT, "web", "panel.html"),
            cfg.get("bind", "127.0.0.1"),
            int(cfg.get("port", 8791)),
            stale_after=interval * 4,
        )
        threading.Thread(target=httpd.serve_forever, name="panel", daemon=True).start()
        log.info("面板已启动 http://%s:%s", cfg.get("bind", "127.0.0.1"), cfg.get("port", 8791))

    running = True

    def _stop(signum, _frame):
        nonlocal running
        running = False
        log.info("收到信号 %s，准备退出", signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if not portfolio_id or portfolio_id == "PUT_PORTFOLIO_ID_HERE":
        log.error("config.json 里 portfolio_id 还是占位符，先填带单员 ID")

    _atomic_write_json(state_path, empty_state(cfg, interval))
    log.info("开始轮询 portfolio_id=%s 成交 %ds / 周期 %ds",
             portfolio_id, interval, history_interval)

    trade_sync: dict = {}
    history_sync: dict = {}
    official: dict = {}
    data_quality: dict = {}
    last_deep = 0.0
    marks: dict[str, float] = {}

    while running:
        started = time.time()
        errors: list[str] = []
        open_positions: list[dict] = []

        deep = (started - last_deep) >= history_interval or not history_sync
        try:
            trade_sync = sync_trades(cfg, store, portfolio_id, deep=deep)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"成交同步失败: {exc}")
            log.warning("成交同步失败: %s", exc)

        if deep:
            try:
                history_sync = sync_cycles(cfg, store, portfolio_id)
                last_deep = time.time()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"周期同步失败: {exc}")
                log.warning("周期同步失败: %s", exc)
            official = fetch_official(cfg, portfolio_id, official)

        try:
            marks = client.fetch_marks()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"标记价格获取失败: {exc}")

        try:
            cycles = store.closed_positions(portfolio_id)
            open_positions = ledger.analyze_positions(
                store.fills(portfolio_id), cycles,
                grace_ms=int(cfg.get("fill_boundary_grace_sec", 60)) * 1000,
                event_bucket_ms=int(cfg.get("event_bucket_sec", 2)) * 1000,
            )
            priced = price_positions(open_positions, marks)
            if deep:
                data_quality = evaluate_data_quality(
                    store, cycles, priced, marks, portfolio_id,
                    int(cfg.get("fill_boundary_grace_sec", 60)) * 1000)
                for note in data_quality["notes"]:
                    log.info("数据质量: %s", note)
                pruned = prune_old_fills(cfg, store, portfolio_id, priced)
                if pruned:
                    log.info("清理老成交 %d 条", pruned)
            state = build_state(cfg, store, priced, marks, errors, interval,
                                official, trade_sync, history_sync)
            state["data_quality"] = data_quality
            push_stats = push_dispatch(cfg, store, pusher, priced, cycles)
            state["push"] = push_stats
            _atomic_write_json(state_path, state)
            log.info(
                "轮询完成: 持仓 %d | 未实现 %s | 成交 %d 笔 | 周期 %d 条 | 错误 %d",
                len(priced), state["totals"]["upnl"], state.get("fill_total", 0),
                state.get("closed_total", 0), len(errors),
            )
        except Exception:  # noqa: BLE001
            log.exception("状态生成失败")

        deadline = started + interval
        while running and time.time() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.time())))

    log.info("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
