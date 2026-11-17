# 手机接收推送（一加 Ace 6T / ColorOS，无 GMS 场景）

链路：`haiwai(leadmon) → haiwai 本机 ntfy → 手机 ntfy App`（不经过任何中转机器）

> **已完成（2026-11-17，通过 adb 装在这台一加 Ace 6T 上）**：
> `io.heckel.ntfy 1.25.2`（fdroid 版，不依赖 GMS）、已订阅 `leadmon-alerts`、
> 已加服务器账号 `zjj`、已加入电池白名单、通知渠道重要性已调到 HIGH（会弹横幅）。
> 下面保留手动步骤，重装/换机时照做。

## 1. 装 App

| 版本 | 地址 | 适用 |
|---|---|---|
| **fdroid**（推荐） | `https://haiwai.kvzjj.top:9447/ntfy-1.25.2-fdroid-release.apk` | 没有 Google 服务的国行机，App 自己保持长连接 |
| play | `https://haiwai.kvzjj.top:9447/ntfy-1.25.2-play-release.apk` | 装了 GMS 的机器，走 FCM 最省电 |

> 来自 GitHub 官方 release，已镜像到 gn 方便国内下载。安装时允许"未知来源"。

## 2. 配置服务器

App → `Settings → Manage users → Add`：

- **Server URL**: `https://haiwai.kvzjj.top:9445`
- **Username**: `zjj`（密码见 `.secrets/ntfy.env` 的 `NTFY_SUB_PASS`）
- **Password**: 见 `.secrets/ntfy.env` 的 `NTFY_SUB_PASS`
- 勾选 **Use instant delivery**（fdroid 版必须勾，否则靠轮询会很慢）

保存后订阅主题 **`leadmon-alerts`**。

## 2.5 一次性命令（USB 连着电脑时）

```bash
adb install -r -g ntfy-1.25.2-fdroid-release.apk
adb shell cmd deviceidle whitelist +io.heckel.ntfy          # 电池白名单
adb shell am start -a android.intent.action.VIEW \
  -d "ntfy://haiwai.kvzjj.top:9445/leadmon-alerts"              # 深链建订阅
# 然后在 App 里：⋮ → 设置 → 管理用户 → 添加新用户（服务器/账号/密码）
# 系统设置 → 通知 → ntfy → 把「默认优先级」渠道改成 高（横幅+声音+振动）
```

## 3. 关掉省电限制（关键）

ColorOS 杀后台长连接会让通知延迟或收不到：

1. 设置 → 电池 → 应用耗电管理 → ntfy → **允许后台运行 / 不限制**
2. 设置 → 应用管理 → ntfy → 通知 → 允许横幅、锁屏通知
3. 最近任务里把 ntfy **锁定**（下拉卡片上的锁图标），防一键清理

## 4. 验证

```bash
ssh haiwai 'sudo -u leadmon python3 /opt/leadmon/tools/notify_test.py'
```
手机应立刻弹出「leadmon 测试推送」。收不到就先检查第 3 步。

## 5. 点通知跳转面板

通知里的 `Open panel` 按钮 → `https://haiwai.kvzjj.top`（Basic Auth，浏览器记住一次即可）。
