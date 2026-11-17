"""推送：事件 → ntfy（gn.kvzjj.top:9445）→ 手机。

设计要点：
- 只依赖标准库，POST 一个 JSON 就行
- 消息先落 push_log 表再发，发失败下轮重试（不会因为网络抖动丢通知）
- 同一轮里同一个 (币种, 动作) 的多笔事件合并成一条，避免"一次下单推 80 条"
"""

from __future__ import annotations

import base64
import logging
import time
import os
import urllib.error
import urllib.request

log = logging.getLogger("leadmon.notify")

ACTION_STYLE = {
    "OPEN": ("🟢", "开仓"),
    "ADD": ("🔵", "加仓"),
    "REDUCE": ("🟡", "部分平仓"),
    "CLOSE": ("⚪", "全平"),
    "FLIP": ("🔄", "反手"),
}

CLOSED_STYLE = ("✅", "平仓完成")

# 交易事件都是低频高价值信息：用 high 让通知**弹出**（横幅），而不是只躺在通知栏
EVENT_PRIORITY = "high"


def load_env(path: str) -> dict:
    """读 KEY=VALUE 文件（推送账号密码不写在 config.json 里）。"""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip()
    except OSError as exc:
        log.warning("读取推送凭据失败 %s: %s", path, exc)
    return out


def _fmt_num(value, digits: int = 2) -> str:
    if value is None:
        return "–"
    try:
        return f"{value:,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _money(value) -> str:
    if value is None:
        return "–"
    sign = "-" if value < 0 else "+"
    return f"{sign}{abs(value):,.0f}"


class Pusher:
    """ntfy 发布端。"""

    def __init__(self, cfg: dict, base_dir: str):
        push = cfg.get("push") or {}
        self.enabled = bool(push.get("enabled", False))
        env_path = push.get("env_file", ".secrets/ntfy.env")
        if not os.path.isabs(env_path):
            env_path = os.path.join(base_dir, env_path)
        env = load_env(env_path)

        self.url = (push.get("url") or "https://gn.kvzjj.top:9445").rstrip("/")
        self.topic = env.get("NTFY_TOPIC", "leadmon-alerts")
        self.user = env.get("NTFY_PUB_USER", "")
        self.password = env.get("NTFY_PUB_PASS", "")
        self.panel_url = push.get("panel_url", "")
        self.timeout = int(push.get("timeout_sec", 10))
        self.max_attempts = int(push.get("max_attempts", 5))

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.user and self.password)

    def send(self, title: str, body: str, tags: str = "", priority: str = "") -> tuple[bool, str]:
        if not self.ready:
            return False, "推送未启用或缺少凭据"
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
        # ntfy 的 HTTP 头只能是 ASCII，中文标题按 RFC2047 做 Base64 编码（ntfy 会自动解码）
        encoded_title = "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?="
        headers = {
            "Authorization": f"Basic {token}",
            "Title": encoded_title,
            "Content-Type": "text/plain; charset=utf-8",
        }
        if tags:
            headers["Tags"] = tags
        if priority:
            headers["Priority"] = str(priority)
        if self.panel_url:
            headers["Click"] = self.panel_url
            # HTTP 头必须纯 ASCII，按钮文案用英文
            headers["Actions"] = f"view, Open panel, {self.panel_url}, clear=true"

        req = urllib.request.Request(
            f"{self.url}/{self.topic}",
            data=body.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
                if 200 <= resp.status < 300:
                    return True, ""
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "ignore")[:200]
            except Exception:  # noqa: BLE001
                pass
            return False, f"HTTP {exc.code} {detail}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


# ------------------------------------------------------------------ 消息组装

def _event_message(item: dict) -> tuple[str, str, str]:
    """(title, body, tags)"""
    emoji, label = ACTION_STYLE.get(item["action"], ("•", item["action"]))
    sym = item["symbol"]
    side = "多" if item["side"] == "LONG" else "空"
    title = f"{emoji} {label} {sym} {side}"

    lines = []
    if item["action"] == "REDUCE":
        lines.append(f"卖出 {_fmt_num(item['qty'], 4)} @ {_fmt_num(item['price'], 4)}")
        lines.append(f"已平 {_fmt_num(item['closed_ratio'], 1)}%，剩余 {_fmt_num(item['remaining'], 4)}")
        lines.append(f"本步已实现 {_money(item['realized'])} USDT")
    elif item["action"] == "CLOSE":
        lines.append(f"平仓量 {_fmt_num(item['qty'], 4)} @ {_fmt_num(item['price'], 4)}")
        lines.append(f"已实现 {_money(item['realized'])} USDT")
    else:
        lines.append(f"买入 {_fmt_num(item['qty'], 4)} @ {_fmt_num(item['price'], 4)}")
        lines.append(f"名义约 {_fmt_num(item['notional'], 0)} USDT")
        lines.append(f"持仓累计 {_fmt_num(item['remaining'], 4)}，均价 {_fmt_num(item['avg_cost'], 4)}")
    if item.get("ts"):
        lines.append("时间 " + time.strftime("%m-%d %H:%M:%S", time.localtime(item["ts"] / 1000)))
    return title, "\n".join(lines), ""


def _cycle_message(cycle: dict) -> tuple[str, str, str]:
    emoji, label = CLOSED_STYLE
    side = "多" if cycle["side"] == "LONG" else "空"
    pnl = cycle.get("pnl") or 0.0
    title = f"{emoji} {label} {cycle['symbol']} {side} {_money(pnl)} USDT"
    roi = cycle.get("roi")
    lines = [
        f"开仓 {_fmt_num(cycle.get('avg_cost'), 4)} → 平仓 {_fmt_num(cycle.get('avg_close'), 4)}",
        f"数量 {_fmt_num(cycle.get('qty'), 4)}，杠杆 {_fmt_num(cycle.get('leverage'), 0)}x",
        f"已实现 {_money(pnl)} USDT" + (f"，ROI {roi * 100:.1f}%" if roi is not None else ""),
        f"持仓 {_fmt_num((cycle.get('duration_min') or 0) / 60, 1)} 小时",
    ]
    return title, "\n".join(lines), ""


def build_messages(items: list[dict]) -> list[dict]:
    """items -> [{uid, kind, title, body, tags}]"""
    out = []
    for item in items:
        try:
            if item["kind"] == "cycle":
                title, body, tags = _cycle_message(item["data"])
            else:
                title, body, tags = _event_message(item["data"])
            out.append({"uid": item["uid"], "uids": item.get("uids") or [item["uid"]],
                        "kind": item["kind"], "title": title, "body": body, "tags": tags,
                        "priority": EVENT_PRIORITY})
        except Exception:  # noqa: BLE001
            log.exception("消息组装失败: %s", item.get("uid"))
    return out


def summarize_event(event: dict, position: dict) -> dict:
    """把状态机事件 + 当前持仓信息合成一条推送素材。"""
    return {
        "action": event["action"],
        "symbol": position["symbol"],
        "side": position["side"],
        "qty": event.get("buy") or event.get("sell") or 0.0,
        "price": event.get("price"),
        "realized": event.get("realized_api") or 0.0,
        "remaining": position.get("qty"),
        "avg_cost": position.get("avg_cost"),
        "closed_ratio": position.get("closed_ratio"),
        "notional": (event.get("buy") or 0.0) * (event.get("price") or 0.0),
        "ts": event.get("ts"),
    }
