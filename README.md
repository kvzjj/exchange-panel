# leadmon

监控币安跟单（复制交易）带单员的**当前持仓**与**开平仓记录**，维护一个只读面板。
当前配置盯的是「熬鹰资本」(`portfolioId=5075281354358777856`)。

纯标准库（urllib + sqlite3 + http.server），零第三方依赖，1G 内存小机器上常驻约 **25MB**。

## 架构

```
┌──────────────── haiwai（腾讯云·海外，无公网入站） ────────────────┐
│ leadmon                                                          │
│   采集成交/周期 → 反推仓位 → 状态机判定（开/加/部分平/全平/反手）│
│     ├── 事件 → HTTPS 出站 → gn 推送网关                          │
│     └── 面板 127.0.0.1:8791                                      │
└───────────┬──────────────────────────────────────▲───────────────┘
            │ 出站 9445（Basic Auth）               │ SSH 反向隧道 -R 8791
            ▼                                      │
┌──────────────── gn.kvzjj.top（公网网关） ────────────────────────┐
│ Caddy（Let's Encrypt 自动续期）                                  │
│   :9445  → ntfy 127.0.0.1:2586   （推送服务，自带账号/主题权限） │
│   :9446  → 127.0.0.1:8791        （面板，Basic Auth + 隧道）     │
│   :9447  → /var/www/apk          （ntfy APK 镜像，方便国内下载） │
└───────────┬──────────────────────────────────────────────────────┘
            │ WebSocket / FCM
            ▼
   一加 Ace 6T：ntfy App（实时通知）＋ 浏览器看面板
```

为什么这么切：haiwai 只做出站（不暴露任何入站端口，被扫面风险最低），
gn 已有 Caddy 和证书，负责 TLS、鉴权和把面板通过 SSH 反向隧道接出来。
443 被 frps 占用，所以沿用这台机器既有的 `:8443/:9443/:9444` 风格，新服务走 `:9445/:9446/:9447`。

## 手机推送

- App：ntfy（Android），APK 已镜像在本机 gn，见 `docs/phone-setup.md`
- 服务器：`https://gn.kvzjj.top:9445`，主题 `leadmon-alerts`
- 权限：发布账号 `leadmon` 只有该主题**写**权限，手机订阅账号 `zjj` 只有**读**权限（默认拒绝）
- 事件去重：`push_log` 表按 uid 记；一条消息吞掉多笔拆单时，其余 uid 记为 `merged` 不再重发
- 失败重试：先落库再发，网络抖动时下轮重试，最多 5 次

推送消息示例：

```
🔵 加仓 NVDAUSDT 多
买入 1,199.7600 @ 160.5733
名义约 192,649 USDT
持仓累计 4,392.1500，均价 162.1546
时间 11-16 22:55:20
```

## 数据源（全部实测，没有官方 API）

| 用途 | 接口 | 方法 | 可用性 |
|---|---|---|---|
| 逐笔成交 | `/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/trade-history` | **POST** | ✅ **仓位唯一真实来源** |
| 已平仓周期 | `/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/position-history` | **POST** | ✅ |
| 官方绩效 | `/bapi/futures/v1/public/future/copy-trade/lead-portfolio/performance?portfolioId=&timeRange=7D` | GET | ✅ |
| 官方持仓 | `/bapi/futures/v1/friendly/future/copy-trade/lead-data/positions?portfolioId=` | GET | ❌ **匿名恒返回 `[]`** |
| 标记价格 | `https://fapi.binance.com/fapi/v1/premiumIndex` | GET | ✅ |

### 最大的坑：`lead-data/positions` 匿名拿不到数据

它会返回 `200 + {"data": [], "success": true}`，**即使对方正在持仓**。

一开始我用"历史 108 条全是 `All Closed`"佐证"空仓"，结论是错的：
2026-11-16 该带单员开了 NVDA 多单（成交明细里 81 笔 BUY），这个接口依旧返回 `[]`。

还会误导人的两个现象：

- 假 ID（19 位乱填）会返回 `400 参数非法`，真 ID 返回 `200 []` —— 看起来像"接口工作正常，只是没仓位"
- `portfolioId=1` 也返回 `200 []`，那是 2021 年的真实老组合，不是"无效 ID 的默认响应"

**结论：仓位只能自己用成交明细反推。** 别信这个接口。

### trade-history 的坑（都踩过）

- `pageSize` 最大 **200**，超过会返回错位数据
- 分页用 `pageNumber`，响应里的 `indexValue` **不是游标**（传回去无效）
- `timeRange` / `startTime` / `endTime` 全部被忽略；`total` 恒为 `6000`（历史总量上限）
- **接口会返回完全重复的行**（同 time/price/qty/fee 重复 2~3 次），必须按内容哈希去重
- 常返回 `11012005 当前系统正忙`，重试即可
- 字段：`time symbol side(BUY/SELL) positionSide(LONG/SHORT) price qty(基础币) quantity(计价) fee realizedProfit activeBuy`

### position-history 的坑

- 必须 **POST**（GET 一律 `400 参数非法`）
- 只在 `friendly` 命名空间有效；换成 `public` 会返回 `200` 但 `list` 恒空（静默空结果）
- 一笔 = 一个持仓周期（`opened`→`closed`），`maxOpenInterest` 是周期内峰值，`closedVolume` 是累计平量
- **`closed` 比该周期最后一批成交早约 1 秒**（分批平仓的尾巴），反推时必须加宽限

## 仓位是怎么反推的

```
当前持仓 = 该币种「上一轮周期平仓之后」的成交净额
```

不对全历史累加（分页重叠会累计误差），而是以 `position-history` 的 `closed` 为切点：

1. 每个 (币种, 方向) 取最后一轮周期的 `closed`，加 60s 宽限（`fill_boundary_grace_sec`）
2. 只统计这个时间点之后的成交：`BUY` 加、`SELL` 减
3. 同向加仓累加成本，反向平仓按比例减成本 → 得到持仓量 + 均价成本
4. 最后用 `fapi` 标记价算浮动盈亏

实测校验（2026-11-17）：反推得 NVDA 多 4392.15 股 / 均价 162.15 / 247 笔成交，
与对方页面「当前持仓」一致。

两个已知边界：

- 若某仓位**开在 7 天之前**（`fill_backfill_days`）且从未加减仓，可能漏检 —— 目前成交量约 500 笔/天，7 天窗口足够
- 周期处于**部分平仓**状态时（`status != All Closed`），以成交反推为准确；`position-history` 的行只用于统计

## 部分平仓 vs 全平：状态机判定

只看净持仓是分不出"全平"和"减了一半"的，所以把成交聚合成**操作事件**再跑状态机：

```
同类拆单聚合（默认 2 秒窗口）  →  加权均价  →  判定动作
```

| 动作 | 判定依据 |
|---|---|
| `开仓 OPEN` | 之前净仓为 0 |
| `加仓 ADD` | 与当前同向增加 |
| `部分平仓 REDUCE` | 反向且平掉量 < 当前持仓 → **剩余 > 0** |
| `全平 CLOSE` | 反向且平掉量 == 当前持仓 → 剩余 = 0 |
| `反手 FLIP` | 反向且平掉量 > 当前持仓 |

他下单是"一次砸几十上百笔"（NVDA 开仓那一下 81 笔、加仓 90/40/36 笔），
不聚合的话事件流没法看，所以按时间桶合并。

**盈亏怎么算**：每笔成交自带 `realizedProfit`，已实现直接用接口自报值（权威）；
本地加权平均成本法算出的模型值只做**交叉校验**——两者差得多，说明我们聚合错了
或者成交被 6000 条上限截断了。

实测校验（用已平仓周期的时间窗跑状态机）：

| 周期 | 窗口净量 | 模型已实现 | 接口已实现 | 差额 |
|---|---|---|---|---|
| BTC 11-15→11-16 | 0.03 | 12,254.68 | 12,258.01 | −3.33 |
| ETH 11-10→11-13 | 6.72 | 44,279.06 | 44,282.87 | −3.81 |
| ZEC 11-14→11-15 | 2.08 | 14,830.18 | 14,920.13 | −89.94 |
| BTC 11-13 | 0.57 | 26,662.74 | 27,690.13 | −1,027.39 |

小周期差 0.02%~0.6%，吻合；差到 3.7% 的都是**窗口内成交被截断**（净量本该是 0 却剩几股），
所以已实现盈亏一律以接口为准。

> 为什么状态机只跑"上一轮平仓之后"的成交：接口最多给 6000 条历史，
> 从零开始累加会在早期留下虚假的反向残余仓（实测 NVDA 出现过 −477.99 的假空单）。

## 面板内容

```
当前持仓     由逐笔成交反推：数量/均价成本/标记价/名义价值/浮动盈亏/已平比例/周期状态
持仓周期明细 开仓-加仓-部分平仓-全平 的事件流：每步的买/卖量、成交价、操作后剩余、该步已实现
最近成交明细 即页面上的「操作记录」，每笔的价、量、手续费、已实现
绩效         官方口径（ROI/盈亏/胜率/回撤/夏普）+ 本地口径（按周期加总）并排
已平仓周期   每轮的均价/平仓价/数量/已实现/ROI/杠杆/持仓时长
币种聚合     哪几个标的反复做、各自赚多少
```

## 用法

```bash
# 侦察任意带单员（换 ID 就能盯别人）
python3 tools/probe.py <portfolio_id>

# 本地跑
python3 -m leadmon.main config.json

# 看面板：服务只绑 127.0.0.1，必须走隧道
ssh -f -N haiwai-web        # 本机 ~/.ssh/config 里已配 LocalForward 8791
open http://127.0.0.1:8791
```

## 部署

```bash
rsync -az --exclude data --exclude __pycache__ ./ haiwai:/tmp/leadmon-deploy/
ssh haiwai 'sudo cp -a /tmp/leadmon-deploy/. /opt/leadmon/ && sudo bash /opt/leadmon/deploy/install.sh'
ssh haiwai 'sudo journalctl -u leadmon -f'
```

改了 `config.json` 里任何东西都要 `sudo systemctl restart leadmon`。

## 配置

| 字段 | 说明 |
|---|---|
| `portfolio_id` | 带单员 ID（`copy-trading/lead-details/<ID>`） |
| `poll_interval_sec` | 持仓轮询，默认 60（别低于 30） |
| `history_interval_sec` | 周期+官方绩效同步，默认 300（增量：只翻到出现已知 positionId 就停） |
| `fill_backfill_days` | 成交回溯窗口，默认 7 天（停机后恢复会按此补齐） |
| `fill_boundary_grace_sec` | 周期平仓边界宽限，默认 60 秒（见上文坑） |
| `event_bucket_sec` | 拆单聚合窗口，默认 2 秒（同窗口内的成交算一次操作） |
| `official_ranges` | 官方绩效窗口，默认 `["7D","30D","90D"]` |
| `bind` / `port` | 默认 `127.0.0.1:8791` |

## 目录

```
leadmon/client.py      bapi/fapi 抓取：伪装头、重试、空响应与 11012005 繁忙识别
leadmon/normalize.py   字段映射（接口字段漂移时只改这一处，原始 JSON 已全量落库可重放）
leadmon/ledger.py      成交反推仓位 / 窗口统计 / 币种聚合
leadmon/store.py       sqlite：snapshot(原始JSON) + position(当前) + closed_position(周期)
leadmon/panel.py       只读 HTTP：/ /api/state /healthz
leadmon/main.py        双节奏轮询循环 + 面板线程
web/panel.html         单页面板（无依赖，5s 自刷新）
deploy/                systemd unit（加固：ProtectSystem/PrivateTmp/NoNewPrivileges）+ 安装脚本
tools/probe.py         接口侦察：换带单员 ID 用这个，会打印真实字段和可用的历史接口
```
