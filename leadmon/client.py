"""币安跟单(复制交易)公开数据抓取。纯标准库，无第三方依赖。

币安跟单没有官方 API，这里用的是网页端 bapi 接口，需要伪装浏览器头；
域名可能因地区被 WAF 拦（返回 202 空包），失败会重试并抛 FetchError。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

BAPI = "https://www.binance.com/bapi/futures/v1/public/future/copy-trade"
# 带单员的持仓/平仓历史只在这个 friendly 命名空间下能取到（public 下返回空或 404）
FRIENDLY = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade"
FAPI = "https://fapi.binance.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "clienttype": "web",
    "lang": "zh-CN",
    "Origin": "https://www.binance.com",
    "Referer": "https://www.binance.com/zh-CN/copy-trading",
}


class FetchError(RuntimeError):
    pass


def _get(url: str, timeout: int = 15, retries: int = 3):
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                if not body:
                    raise FetchError(f"空响应 HTTP {resp.status}（通常是 WAF 拦截）")
                return json.loads(body)
        except Exception as exc:  # noqa: BLE001 - 网络层什么都可能抛
            last = exc
            log.warning("GET %s 失败 (%d/%d): %s", url, attempt, retries, exc)
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise FetchError(f"GET {url} 重试全部失败: {last}")


def _post(url: str, body: dict, timeout: int = 15, retries: int = 3):
    """POST JSON。带单员的平仓历史只有 POST 才认参数，GET 一律 400。"""
    last = None
    data = json.dumps(body).encode()
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    raise FetchError(f"空响应 HTTP {resp.status}（通常是 WAF 拦截）")
                payload = json.loads(raw)
            if not payload.get("success", False):
                # 11012005 = 系统繁忙，重试通常就能过
                raise FetchError(
                    f"code={payload.get('code')} message={payload.get('message')}"
                )
            return payload
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("POST %s 失败 (%d/%d): %s", url, attempt, retries, exc)
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise FetchError(f"POST {url} 重试全部失败: {last}")


def fetch_positions(portfolio_id: str):
    """带单员当前持仓，返回 bapi 的 data 列表。空列表就是真空仓，不是错误。"""
    query = urllib.parse.urlencode({"portfolioId": portfolio_id})
    last = None
    for base in (FRIENDLY, BAPI):
        try:
            payload = _get(f"{base}/lead-data/positions?{query}")
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
        if isinstance(payload, dict) and payload.get("success", False):
            return payload.get("data") or []
        last = FetchError(f"code={payload.get('code')} message={payload.get('message')}")
    raise FetchError(f"持仓接口全部不可用: {last}")


def fetch_position_history(
    portfolio_id: str, page: int = 1, size: int = 100, time_range: str = "ALL"
) -> dict:
    """已平仓历史，返回 {total, list}。按开仓时间倒序。"""
    payload = _post(
        f"{FRIENDLY}/lead-portfolio/position-history",
        {
            "portfolioId": str(portfolio_id),
            "pageNumber": page,
            "pageSize": size,
            "timeRange": time_range,
        },
    )
    return payload.get("data") or {"total": 0, "list": []}


def fetch_marks(timeout: int = 20) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in _get(f"{FAPI}/fapi/v1/premiumIndex", timeout=timeout):
        try:
            out[row["symbol"]] = float(row["markPrice"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_performance(portfolio_id: str, time_range: str = "7D") -> dict:
    """官方口径的绩效：roi/pnl/胜率/订单数。和逐笔加总的口径不一致，两个都留着。"""
    query = urllib.parse.urlencode({"portfolioId": portfolio_id, "timeRange": time_range})
    payload = _get(f"{BAPI}/lead-portfolio/performance?{query}")
    if not payload.get("success", False):
        raise FetchError(f"code={payload.get('code')} message={payload.get('message')}")
    return payload.get("data") or {}


def fetch_trades(portfolio_id: str, page: int = 1, size: int = 200) -> dict:
    """逐笔成交明细（= 页面上的"操作记录"）。这才是真实数据源。

    注意：
    - pageSize 最大 200，pageNumber 分页有效（indexValue 是响应里的标记，不能当游标用）
    - timeRange / startTime / endTime 一律被忽略，total 恒为 6000（历史总量上限）
    - 相邻页会在同一毫秒处重叠，入库必须按内容哈希去重
    - 经常返回 11012005「当前系统正忙」，_post 自带重试
    """
    payload = _post(
        f"{FRIENDLY}/lead-portfolio/trade-history",
        {
            "portfolioId": str(portfolio_id),
            "pageNumber": page,
            "pageSize": size,
            "timeRange": "ALL",
        },
        retries=6,
    )
    return payload.get("data") or {"total": 0, "list": []}
