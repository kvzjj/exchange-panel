# 手机接收推送（一加 Ace 6T / ColorOS，无 GMS 场景）

链路：`haiwai(leadmon) → gn.kvzjj.top:9445 (ntfy) → 手机 ntfy App`

## 1. 装 App

| 版本 | 地址 | 适用 |
|---|---|---|
| **fdroid**（推荐） | `https://gn.kvzjj.top:9447/ntfy-1.25.2-fdroid-release.apk` | 没有 Google 服务的国行机，App 自己保持长连接 |
| play | `https://gn.kvzjj.top:9447/ntfy-1.25.2-play-release.apk` | 装了 GMS 的机器，走 FCM 最省电 |

> 来自 GitHub 官方 release，已镜像到 gn 方便国内下载。安装时允许"未知来源"。

## 2. 配置服务器

App → `Settings → Manage users → Add`：

- **Server URL**: `https://gn.kvzjj.top:9445`
- **Username**: `zjj`
- **Password**: 见 `.secrets/ntfy.env` 的 `NTFY_SUB_PASS`
- 勾选 **Use instant delivery**（fdroid 版必须勾，否则靠轮询会很慢）

保存后订阅主题 **`leadmon-alerts`**。

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

通知里的 `Open panel` 按钮 → `https://gn.kvzjj.top:9446`（Basic Auth，浏览器记住一次即可）。
