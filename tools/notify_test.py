#!/usr/bin/env python3
"""推送连通性测试。

    python3 tools/notify_test.py            # 发一条固定测试消息
    python3 tools/notify_test.py --event    # 发一条"开仓"样例消息（验证中文/格式）
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from leadmon import notify  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "config.json")


def main() -> int:
    cfg = json.load(open(CFG, encoding="utf-8"))
    pusher = notify.Pusher(cfg, ROOT)
    if not pusher.ready:
        print("推送未启用：检查 config.json 的 push.enabled 和 .secrets/ntfy.env")
        return 1

    if "--event" in sys.argv:
        import time
        event = {
            "action": "OPEN", "symbol": "NVDAUSDT", "side": "LONG",
            "qty": 1619.53, "price": 162.7113, "realized": 0.0,
            "remaining": 1619.53, "avg_cost": 162.7113, "closed_ratio": 0.0,
            "notional": 1619.53 * 162.7113, "ts": int(time.time() * 1000),
        }
        title, body, tags = notify._event_message(event)
    else:
        import time
        title = "leadmon 测试推送"
        body = "链路：haiwai -> gn.kvzjj.top:9445 (ntfy) -> 手机\n时间 " + \
               time.strftime("%Y-%m-%d %H:%M:%S")
        tags = "white_check_mark"

    ok, err = pusher.send(title, body, tags)
    print(("✅ 已发送" if ok else "❌ 失败") + (f": {err}" if err else ""))
    print(f"   标题: {title}")
    print(f"   正文: {body}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
