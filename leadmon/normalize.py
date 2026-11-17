"""bapi 原始返回 -> 内部结构。

币安跟单的字段名既没有文档、也随前端版本变，所以这里一律做"候选键"匹配。
匹配不上的行会原样落库，同时把真实键名打一条日志（只打一次），方便几分钟内修正。
"""

from __future__ import annotations

import hashlib
import logging

log = logging.getLogger(__name__)

SYM_KEYS = ("symbol", "pair", "contract", "contractName")
SIDE_KEYS = ("positionSide", "side", "direction", "positionDirection", "tradeSide")
QTY_KEYS = ("positionAmount", "positionAmt", "amount", "qty", "quantity", "volume")
ENTRY_KEYS = ("entryPrice", "avgPrice", "averagePrice", "openPrice", "costOpen")
MARK_KEYS = ("markPrice", "mark_price", "lastPrice", "currentPrice")
LEV_KEYS = ("leverage", "lever", "lev")
MARGIN_KEYS = ("isolatedMargin", "margin", "initialMargin", "positionMargin", "marginAmount")
UPNL_KEYS = ("unrealizedProfit", "unrealizedPnl", "unrealizedPNL", "upl", "profit", "pnl")
TS_KEYS = ("updateTime", "openTime", "createdTime", "time", "ctime", "createTime")
PRICE_KEYS = ("price", "filledPrice", "avgPrice", "executionPrice")
ROE_KEYS = ("roe", "returnOnEquity", "roi")

_seen_keys: set[str] = set()


def _pick(row: dict, keys) -> object | None:
    for k in keys:
        if k in row and row[k] not in (None, "", "0"):
            return row[k]
    for k in keys:
        if k in row:
            return row[k]
    return None


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_symbol(raw) -> str:
    sym = str(raw or "?").upper().replace("_PERP", "").replace("-", "")
    return sym or "?"


def _norm_side(raw, qty: float | None) -> str:
    text = str(raw or "").upper()
    if "LONG" in text or text in ("BUY", "B"):
        return "LONG"
    if "SHORT" in text or text in ("SELL", "S"):
        return "SHORT"
    if qty is not None:
        return "LONG" if qty > 0 else "SHORT"
    return "?"


def _warn_once(kind: str, keys) -> None:
    signature = kind + "|" + ",".join(sorted(map(str, keys)))
    if signature in _seen_keys:
        return
    _seen_keys.add(signature)
    log.warning("%s 行里没有识别到已知字段，真实键名: %s", kind, sorted(map(str, keys)))


def normalize_position(row: dict) -> dict:
    """单个持仓。qty 统一取正数，方向单独给 side。"""
    qty = _num(_pick(row, QTY_KEYS))
    if qty is None or qty == 0:
        _warn_once("position", row.keys())
    return {
        "symbol": _norm_symbol(_pick(row, SYM_KEYS)),
        "side": _norm_side(_pick(row, SIDE_KEYS), qty),
        "qty": abs(qty) if qty is not None else None,
        "entry_price": _num(_pick(row, ENTRY_KEYS)),
        "mark_price": _num(_pick(row, MARK_KEYS)),
        "leverage": _num(_pick(row, LEV_KEYS)),
        "margin": _num(_pick(row, MARGIN_KEYS)),
        "upnl": _num(_pick(row, UPNL_KEYS)),
        "roe": _num(_pick(row, ROE_KEYS)),
        "opened_ts": _num(_pick(row, TS_KEYS)),
        "raw": row,
    }


def normalize_fill(row: dict) -> dict:
    """单条开平仓记录。接口没有稳定主键，用内容哈希做幂等 uid。"""
    symbol = _norm_symbol(_pick(row, SYM_KEYS))
    price = _num(_pick(row, PRICE_KEYS))
    qty = _num(_pick(row, QTY_KEYS))
    if qty is None:
        _warn_once("fill", row.keys())
    ts = _num(_pick(row, TS_KEYS))
    raw_side = _pick(row, SIDE_KEYS)
    digest = hashlib.sha1(
        f"{symbol}|{raw_side}|{price}|{qty}|{ts}".encode()
    ).hexdigest()[:20]
    return {
        "uid": digest,
        "ts": int(ts) if ts else 0,
        "symbol": symbol,
        "side": str(raw_side or "?").upper(),
        "price": price,
        "qty": abs(qty) if qty is not None else None,
        "realized_pnl": _num(_pick(row, ("realizedProfit", "realizedPnl", "profit"))),
        "raw": row,
    }


# ---- 已平仓历史（friendly/lead-portfolio/position-history 真实字段）----
def normalize_closed_position(row: dict) -> dict:
    """一笔完整持仓周期：开仓→(可能分批)→全平。

    币安给的是"持仓周期"聚合，不是逐笔成交，所以这里保 maxOpenInterest（最大持仓量）
    和 closedVolume（已平量），两者不等就说明有分批：仓位还没走完。
    """
    symbol = _norm_symbol(row.get("symbol"))
    opened = int(row.get("opened") or 0)
    closed = int(row.get("closed") or 0)
    side = _norm_side(row.get("side"), None)
    qty = _num(row.get("maxOpenInterest"))
    volume = _num(row.get("closedVolume"))
    status = str(row.get("status") or "?")
    return {
        "position_id": str(row.get("positionId") or row.get("id") or f"{symbol}-{opened}-{closed}"),
        "symbol": symbol,
        "side": side,
        "status": status,
        "opened": opened,
        "closed": closed,
        "duration_min": round((closed - opened) / 60000, 1) if closed > opened > 0 else None,
        "avg_cost": _num(row.get("avgCost")),
        "avg_close": _num(row.get("avgClosePrice")),
        "qty": qty,
        "volume": volume,
        "open_qty": (
            round(max((qty or 0) - (volume or 0), 0.0), 8)
            if status != "All Closed" else 0.0
        ),
        "pnl": _num(row.get("closingPnl")),
        "roi": _num(row.get("roi")),
        "leverage": _num(row.get("leverage")),
        "isolated": row.get("isolated"),
        "is_open": status != "All Closed",
        "raw": row,
    }


# ---- 逐笔成交（friendly/lead-portfolio/trade-history 真实字段）----
def normalize_trade(row: dict) -> dict:
    """一笔成交。

    接口没有订单号，只能用「时间+币种+方向+价格+数量+手续费+已实现」做内容哈希。
    手续费随数量变化，所以正常情况下不会误合并；相邻页重叠的重复行会被这条哈希吃掉。
    """
    time_ms = int(row.get("time") or 0)
    symbol = _norm_symbol(row.get("symbol"))
    side = str(row.get("side") or "").upper()          # BUY / SELL
    pos_side = _norm_side(row.get("positionSide"), None)  # LONG / SHORT
    price = _num(row.get("price"))
    qty = _num(row.get("qty"))                          # 基础币数量
    quote = _num(row.get("quantity"))                   # 计价货币金额
    fee = _num(row.get("fee"))
    realized = _num(row.get("realizedProfit"))
    uid = hashlib.sha1(
        f"{time_ms}|{symbol}|{side}|{pos_side}|{price}|{qty}|{fee}|{realized}".encode()
    ).hexdigest()[:24]
    return {
        "uid": uid,
        "time": time_ms,
        "symbol": symbol,
        "side": side,
        "position_side": pos_side,
        "price": price,
        "qty": qty,
        "quote_qty": quote,
        "fee": fee,
        "realized": realized,
        "active_buy": 1 if row.get("activeBuy") else 0,
        "raw": row,
    }
