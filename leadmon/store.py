"""sqlite 存储：原始快照（全量留档）+ 当前持仓 + 已平仓历史。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time as _time

SCHEMA = """
PRAGMA journal_mode=WAL;

-- 每次都落一份原始 JSON：接口字段再变，历史也能重放
CREATE TABLE IF NOT EXISTS snapshot (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    kind    TEXT    NOT NULL,
    payload TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_kind_ts ON snapshot(kind, ts);

-- 当前持仓：接口给的是全量快照，每轮整表替换
CREATE TABLE IF NOT EXISTS position (
    portfolio_id TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    qty          REAL,
    entry_price  REAL,
    mark_price   REAL,
    leverage     REAL,
    margin       REAL,
    upnl         REAL,
    opened_ts    INTEGER,
    ts           INTEGER NOT NULL,
    raw          TEXT,
    PRIMARY KEY (portfolio_id, symbol, side)
);

-- 已平仓历史：一笔 = 一个持仓周期（可能分批），按 positionId 幂等
CREATE TABLE IF NOT EXISTS closed_position (
    position_id  TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    status       TEXT,
    opened       INTEGER,
    closed       INTEGER,
    duration_min REAL,
    avg_cost     REAL,
    avg_close    REAL,
    qty          REAL,
    volume       REAL,
    pnl          REAL,
    roi          REAL,
    leverage     REAL,
    isolated     TEXT,
    is_open      INTEGER,
    seen_ts      INTEGER,
    raw          TEXT
);
CREATE INDEX IF NOT EXISTS idx_closed_portfolio ON closed_position(portfolio_id, opened DESC);

-- 逐笔成交：这是仓位的真实来源（官方 positions 接口匿名不可用）
CREATE TABLE IF NOT EXISTS fill (
    uid           TEXT PRIMARY KEY,
    portfolio_id  TEXT NOT NULL,
    time          INTEGER NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,       -- BUY / SELL
    position_side TEXT NOT NULL,       -- LONG / SHORT
    price         REAL,
    qty           REAL,
    quote_qty     REAL,
    fee           REAL,
    realized      REAL,
    active_buy    INTEGER,
    seen_ts       INTEGER,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_fill_time ON fill(portfolio_id, time DESC);

-- 推送出库：先落库再发，失败下轮重试，避免网络抖动丢通知
CREATE TABLE IF NOT EXISTS push_log (
    uid        TEXT PRIMARY KEY,
    kind       TEXT,
    title      TEXT,
    body       TEXT,
    tags       TEXT,
    priority   TEXT,
    created_ts INTEGER,
    status     TEXT DEFAULT 'pending',
    attempts   INTEGER DEFAULT 0,
    sent_ts    INTEGER,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_push_status ON push_log(status, created_ts);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

FIELDS = (
    "position_id", "portfolio_id", "symbol", "side", "status", "opened", "closed",
    "duration_min", "avg_cost", "avg_close", "qty", "volume", "pnl", "roi",
    "leverage", "isolated", "is_open", "seen_ts", "raw",
)


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.executescript(SCHEMA)
            self._migrate()
            self.db.commit()

    def _migrate(self) -> None:
        """SQLite 加列只能 ALTER，CREATE TABLE IF NOT EXISTS 不会补列。"""
        migrations = {
            "push_log": {"priority": "TEXT"},
        }
        for table, columns in migrations.items():
            existing = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if existing and name not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def snapshot(self, kind: str, ts: int, payload) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO snapshot(ts, kind, payload) VALUES (?,?,?)",
                (ts, kind, json.dumps(payload, ensure_ascii=False)),
            )
            self.db.commit()

    def replace_positions(self, portfolio_id: str, rows: list[dict], ts: int) -> None:
        with self._lock:
            self.db.execute("DELETE FROM position WHERE portfolio_id=?", (portfolio_id,))
            self.db.executemany(
                """INSERT OR REPLACE INTO position
                   (portfolio_id, symbol, side, qty, entry_price, mark_price,
                    leverage, margin, upnl, opened_ts, ts, raw)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        portfolio_id, r["symbol"], r["side"], r.get("qty"),
                        r.get("entry_price"), r.get("mark_price"), r.get("leverage"),
                        r.get("margin"), r.get("upnl"), r.get("opened_ts"), ts,
                        json.dumps(r.get("raw"), ensure_ascii=False),
                    )
                    for r in rows
                ],
            )
            self.db.commit()

    def upsert_closed(self, portfolio_id: str, rows: list[dict], ts: int) -> int:
        """按 positionId 幂等写入，返回新增（之前没见过）的条数。"""
        added = 0
        with self._lock:
            for r in rows:
                record = dict(r)
                record["portfolio_id"] = portfolio_id
                record["seen_ts"] = ts
                record["is_open"] = 1 if record.get("is_open") else 0
                record["raw"] = json.dumps(r.get("raw"), ensure_ascii=False)
                # 用 upsert 而不是 IGNORE：周期可能先以"部分平仓"入库，后续才全平，
                # 数量和盈亏都会变，必须刷新；新增条数另算
                exists = self.db.execute(
                    "SELECT 1 FROM closed_position WHERE position_id=?",
                    (record.get("position_id"),),
                ).fetchone()
                updates = ",".join(f"{k}=excluded.{k}" for k in FIELDS[1:])
                self.db.execute(
                    f"INSERT INTO closed_position ({','.join(FIELDS)}) "
                    f"VALUES ({','.join('?' * len(FIELDS))}) "
                    f"ON CONFLICT(position_id) DO UPDATE SET {updates}",
                    tuple(record.get(k) for k in FIELDS),
                )
                if not exists:
                    added += 1
            self.db.commit()
        return added

    def closed_positions(self, portfolio_id: str, limit: int | None = None) -> list[dict]:
        sql = ("SELECT * FROM closed_position WHERE portfolio_id=? "
               "ORDER BY opened DESC")
        params: tuple = (portfolio_id,)
        if limit:
            sql += " LIMIT ?"
            params += (limit,)
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def count_closed(self, portfolio_id: str) -> int:
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS n FROM closed_position WHERE portfolio_id=?",
                (portfolio_id,),
            ).fetchone()
            return int(row["n"])

    def upsert_fills(self, portfolio_id: str, rows: list[dict], ts: int) -> int:
        """按内容哈希幂等写入，返回新增条数（相邻页重叠的行会被自动吃掉）。"""
        added = 0
        with self._lock:
            for r in rows:
                cur = self.db.execute(
                    """INSERT OR IGNORE INTO fill
                       (uid, portfolio_id, time, symbol, side, position_side, price, qty,
                        quote_qty, fee, realized, active_buy, seen_ts, raw)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        r["uid"], portfolio_id, r["time"], r["symbol"], r["side"],
                        r["position_side"], r.get("price"), r.get("qty"),
                        r.get("quote_qty"), r.get("fee"), r.get("realized"),
                        r.get("active_buy"), ts,
                        json.dumps(r.get("raw"), ensure_ascii=False),
                    ),
                )
                added += cur.rowcount
            self.db.commit()
        return added

    def fills(self, portfolio_id: str, limit: int | None = None) -> list[dict]:
        """按时间升序返回成交（重算仓位用）。"""
        sql = "SELECT * FROM fill WHERE portfolio_id=? ORDER BY time ASC, uid ASC"
        params: tuple = (portfolio_id,)
        with self._lock:
            rows = [dict(r) for r in self.db.execute(sql, params).fetchall()]
        return rows[-limit:] if limit else rows

    def fill_count(self, portfolio_id: str) -> int:
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS n FROM fill WHERE portfolio_id=?", (portfolio_id,)
            ).fetchone()
            return int(row["n"])

    def oldest_fill_time(self, portfolio_id: str) -> int:
        with self._lock:
            row = self.db.execute(
                "SELECT MIN(time) AS t FROM fill WHERE portfolio_id=?", (portfolio_id,)
            ).fetchone()
            return int(row["t"] or 0)

    def recent_fills(self, portfolio_id: str, limit: int = 40) -> list[dict]:
        with self._lock:
            cur = self.db.execute(
                "SELECT time, symbol, side, position_side, price, qty, quote_qty, fee, realized "
                "FROM fill WHERE portfolio_id=? ORDER BY time DESC LIMIT ?",
                (portfolio_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    # ---------------- 推送队列 ----------------
    def meta_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))
            self.db.commit()

    def push_seen(self, uid: str) -> bool:
        with self._lock:
            row = self.db.execute("SELECT 1 FROM push_log WHERE uid=?", (uid,)).fetchone()
            return row is not None

    def push_save(self, items: list[dict], ts: int) -> int:
        added = 0
        with self._lock:
            for item in items:
                # 一条消息可能吞了多个事件（拆单合并），除主 uid 外其余记为 merged，
                # 只做去重标记、不再单独发送
                uids = item.get("uids") or [item["uid"]]
                for idx, uid in enumerate(uids):
                    cur = self.db.execute(
                        """INSERT OR IGNORE INTO push_log
                           (uid, kind, title, body, tags, created_ts, status, attempts)
                           VALUES (?,?,?,?,?,?, ?, 0)""",
                        (uid, item.get("kind"), item["title"], item["body"],
                         item.get("tags", ""), ts, "pending" if idx == 0 else "merged"),
                    )
                    added += cur.rowcount
            self.db.commit()
        return added

    def push_pending(self, limit: int = 8, max_attempts: int = 5) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM push_log WHERE status='pending' AND attempts < ? "
                "ORDER BY created_ts LIMIT ?",
                (max_attempts, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def push_mark(self, uid: str, ok: bool, error: str = "", max_attempts: int = 5) -> None:
        with self._lock:
            if ok:
                self.db.execute(
                    "UPDATE push_log SET status='sent', sent_ts=?, error=NULL WHERE uid=?",
                    (int(_time.time()), uid),
                )
            else:
                self.db.execute(
                    "UPDATE push_log SET attempts=attempts+1, error=?, "
                    "status=CASE WHEN attempts+1 >= ? THEN 'failed' ELSE 'pending' END WHERE uid=?",
                    (error[:300], max_attempts, uid),
                )
            self.db.commit()

    def push_recent(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT uid, kind, title, status, created_ts, sent_ts, attempts, error "
                "FROM push_log ORDER BY created_ts DESC LIMIT ?", (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def prune_fills(self, portfolio_id: str, before_ts: int) -> int:
        """删掉老成交，控制库体积。调用方负责确保不会砍掉未平仓的开仓记录。"""
        with self._lock:
            cur = self.db.execute(
                "DELETE FROM fill WHERE portfolio_id=? AND time < ?",
                (portfolio_id, before_ts),
            )
            self.db.commit()
            return cur.rowcount

    def position_snapshots_between(self, start_ts: int, end_ts: int) -> list[str]:
        """取某时间区间内所有 positions 快照的原始 payload（用于漏检自检）。"""
        with self._lock:
            rows = self.db.execute(
                "SELECT payload FROM snapshot WHERE kind='positions' AND ts BETWEEN ? AND ? "
                "ORDER BY ts",
                (start_ts, end_ts),
            ).fetchall()
            return [r["payload"] for r in rows]
