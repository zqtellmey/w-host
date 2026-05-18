# 🎮 Witchly MC 服务器自动监控

> 基于 GitHub Actions + SeleniumBase 的 Witchly.host 免费 MC 服务器全自动管理工具。
> 实现定时续期、宕机自动重启、实时推送通知，配合 Uptime Kuma 做 7×24 小时监控。

---

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| 🔄 **自动续期** | Stability 剩余不足 3 天时自动点击续期按钮（消耗 500 Coins） |
| 🚀 **自动重启** | 检测到服务器 OFFLINE 时自动点击 Start，等待确认 ONLINE |
| 🛡️ **Cloudflare 绕过** | 通过 Cloudflare WARP + SeleniumBase UC Mode 绕过 Turnstile 验证 |
| 🔔 **消息推送** | 支持 WxPusher（微信）和 Telegram 双渠道推送，仅在关键事件时触发 |
| 🎬 **录屏存档** | 每次运行自动录制操作视频（MP4），上传至 GitHub Artifacts |
| 📸 **截图调试** | 关键步骤自动截图，方便排查问题 |
| ⚡ **Uptime Kuma 联动** | 监控到宕机立即触发紧急启动 workflow，无需等待定时任务 |
| 🧹 **自动清理** | 运行记录自动清理，只保留最新几条 |

---

## 📁 项目结构

```
witchly_renew/
├── watchdog.py                          # 主监控脚本
└── .github/
    └── workflows/
        ├── mc-watchdog.yml              # 定时监控（每 6 小时）：续期 + 状态检查
        └── mc-start-only.yml            # 紧急启动（Uptime Kuma 触发）：只做重启
```

---

## 🔧 工作原理

### 两套工作流并行运行

```
mc-watchdog.yml          每 6 小时自动运行
  ├── 登录 Witchly
  ├── 读取 Stability 剩余时间
  ├── 不足 3 天 → 自动续期 → 推送「续期成功」
  ├── 进入控制台
  └── OFFLINE → 自动 Start → 等待 ONLINE → 推送「已重启」

mc-start-only.yml        由 Uptime Kuma Webhook 触发
  ├── 登录 Witchly
  ├── 读取服务器状态
  ├── 已 ONLINE → 静默退出（UP 恢复触发，无需操作）
  └── OFFLINE → 自动 Start → 等待 ONLINE → 推送「已重启」
```

### Uptime Kuma 联动流程

```
MC 服务器宕机
  → Uptime Kuma 每 60 秒 ping 一次
  → 连续 2 次失败（约 2 分钟后确认）
  → 发送 Webhook 到 GitHub API
  → mc-start-only.yml 触发运行（约 2-3 分钟启动）
  → watchdog.py 登录 Witchly 点 Start
  → 等待最多 ~3.5 分钟确认 ONLINE
  → WxPusher 推送「服务器已重新上线」
总响应时间：约 7-10 分钟
```

---

## ⚙️ 配置步骤

### 第一步：Fork 或创建仓库

将本项目代码放入你的 GitHub 仓库，结构如上。

### 第二步：配置 GitHub Secrets

在仓库 **Settings → Secrets and variables → Actions → New repository secret** 中添加以下变量：

| Secret 名称 | 必填 | 说明 |
|-------------|------|------|
| `WITCHLY_DISCORD_TOKEN` | ✅ | Discord 账号 Token（用于登录 Witchly） |
| `WITCHLY_SERVER_ID` | ✅ | Witchly 服务器 ID（URL 中的那串字符） |
| `WX_APP_TOKEN` | ⬜ | WxPusher 应用 AppToken（微信推送，可选） |
| `WX_UID` | ⬜ | WxPusher 用户 UID（微信推送，可选） |
| `TG_BOT_TOKEN` | ⬜ | Telegram Bot Token（可选） |
| `TG_CHAT_ID` | ⬜ | Telegram Chat ID（可选） |

> WxPusher 和 Telegram 至少配置一个，否则没有推送通知。两个都配则同时推送。

#### 获取 Discord Token

1. 打开 Discord 网页版，按 F12 打开开发者工具
2. Application → Local Storage → `https://discord.com`
3. 找到 `token` 字段，复制值（注意去掉首尾引号）

#### 获取 Witchly Server ID

登录 Witchly → 点 Manage → 查看地址栏：
```
https://dash.witchly.host/servers/【这里就是 Server ID】/manage/home
```

#### 获取 WxPusher 配置

1. 访问 [wxpusher.zjiecode.com](https://wxpusher.zjiecode.com)
2. 注册账号 → 创建应用 → 获取 `AppToken`
3. 扫码关注公众号 → 获取个人 `UID`

### 第三步：启用 GitHub Actions

进入仓库 **Actions** 页面，点击 **Enable workflows**。

首次可以手动触发测试：**Actions → MC 服务器自动监控 → Run workflow**。

---

## 🚨 Uptime Kuma 联动配置

> Uptime Kuma 是开源监控工具，需要部署在独立服务器上（推荐 Oracle Cloud 永久免费 VPS）。

### 部署 Uptime Kuma（Oracle Cloud 免费方案）

1. 注册 [Oracle Cloud](https://cloud.oracle.com) 免费账号（需要信用卡验证，不收费）
2. 创建实例：Ubuntu 22.04 + VM.Standard.E2.1.Micro（Always Free）
3. 开放 3001 端口（Security List → Add Ingress Rule → TCP 3001）
4. SSH 登录后安装 Docker 和 Uptime Kuma：

```bash
# 安装 Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
# 重新登录后运行 Uptime Kuma
docker run -d \
  --restart=always \
  -p 3001:3001 \
  -v uptime-kuma:/app/data \
  --name uptime-kuma \
  louislam/uptime-kuma:2
```

5. 浏览器访问 `http://你的IP:3001`，设置管理员账号

### 获取 GitHub Personal Access Token

1. 打开 [github.com/settings/tokens](https://github.com/settings/tokens) → **Generate new token (classic)**
2. 勾选 **`repo`** 权限（包含 Actions 写权限）
3. 生成并复制 Token（只显示一次）

### 配置 Uptime Kuma Webhook 通知

**Settings → Notifications → Add Notification：**

| 字段 | 填写内容 |
|------|---------|
| 通知类型 | `Webhook` |
| 显示名称 | `GitHub Actions 紧急启动` |
| Post URL | `https://api.github.com/repos/你的用户名/witchly_renew/dispatches` |
| 请求体 | 选 `自定义内容` |

**请求体内容：**
```json
{
  "event_type": "server-down",
  "client_payload": {
    "reason": "Uptime Kuma alert",
    "heartbeat": "{{heartbeatJSON}}"
  }
}
```

**打开「额外 Header」开关，填入：**
```json
{
  "Authorization": "Bearer github_pat_你的token",
  "Accept": "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28"
}
```

点 **测试**，去 GitHub Actions 页面确认出现新的运行记录。

### 配置 MC 服务器监控项

**Add New Monitor：**

| 字段 | 填写内容 |
|------|---------|
| 监控类型 | `TCP Port` |
| 显示名称 | `witchly监控` |
| 主机名 | `de1-free.witchly.host`（在 Witchly 控制台查看） |
| 端口 | `40270`（你的实际端口） |
| 心跳间隔 | `60`（秒） |
| 重试次数 | `2` |
| 连续失败时重复发送通知的间隔次数 | `9999`（防止重复触发） |
| 通知 | 选刚配置的 `GitHub Actions 紧急启动` |

---

## 📬 推送通知说明

脚本只在以下两种情况发送推送，平时静默运行：

| 事件 | 推送内容 |
|------|---------|
| ✅ 续期成功 | `🔄 Witchly 服务器续期成功`<br>续期完成，剩余稳定时间：Xd Xh |
| 🚀 服务器重启成功 | `🚀 Witchly 服务器已重新上线`<br>检测到服务器离线，已自动执行 Start，服务器现已 ONLINE |
| ⚠️ 启动未确认 | `⚠️ Witchly 服务器启动中`<br>已发送 Start 指令，当前状态：xxx，请稍后手动确认 |
| ❌ 脚本异常 | `❌ Witchly 监控脚本异常`<br>异常信息 |

---

## 🔑 环境变量完整列表

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `WITCHLY_DISCORD_TOKEN` | 无 | Discord Token（必填） |
| `WITCHLY_SERVER_ID` | 无 | Witchly 服务器 ID（必填） |
| `WX_APP_TOKEN` | 无 | WxPusher AppToken |
| `WX_UID` | 无 | WxPusher UID（多个用英文逗号分隔） |
| `TG_BOT_TOKEN` | 无 | Telegram Bot Token |
| `TG_CHAT_ID` | 无 | Telegram Chat ID |
| `RENEW_THRESHOLD_DAYS` | `3` | Stability 低于多少天触发续期 |
| `ENABLE_RECORDING` | `true` | 是否启用录屏（`false` 可关闭） |
| `SKIP_RENEW` | `false` | 跳过续期检查（紧急启动 workflow 自动设为 true） |

---

## 🎬 Artifacts 说明

每次运行后可在 Actions → 对应运行记录 → Artifacts 下载：

| Artifact | 内容 | 保留时间 |
|----------|------|---------|
| `screenshots-N` | 关键步骤截图（登录、My Servers、控制台等） | 3 天 |
| `recording-N` | 完整操作录屏 MP4 | 3 天 |
| `emergency-screenshots-N` | 紧急启动截图 | 3 天 |
| `emergency-recording-N` | 紧急启动录屏 | 3 天 |

> 录屏帧存储在 `screenshots/rec/` 子目录，不会混入正式截图。

---

## ❓ 常见问题

**Q：Discord Token 怎么保持有效？**
Token 只要不手动退出登录一般长期有效。如果失效会在截图 `token-invalid.png` 中体现，重新获取填入 Secret 即可。

**Q：Coins 不足怎么办？**
续期需要 500 Coins，可以在 Witchly 的 Earn Coins 页面完成任务获取。Coins 不足时脚本会打印警告日志但不报错。

**Q：为什么有时候会触发多次紧急启动？**
Uptime Kuma 的「连续失败时重复发送通知的间隔次数」需要设为 `9999`，否则每次检测失败都会发 Webhook。如果已设置，多次触发是 GitHub 处理历史积压请求导致的，等队列清空即可。

**Q：录屏没有生成怎么办？**
录屏依赖 `ffmpeg`，yml 中已自动安装。如果没有生成视频，截图帧仍保存在 `screenshots/rec/` 目录中，可手动合成。

**Q：脚本运行总时长是多少？**
- `mc-watchdog.yml`：正常约 2-3 分钟，需要续期约 4-5 分钟
- `mc-start-only.yml`：服务器在线时约 1.5 分钟，需要重启约 5-7 分钟

---

## 🛠️ 技术栈

- **Python 3.12** + **SeleniumBase UC Mode** — 浏览器自动化 + Cloudflare 绕过
- **GitHub Actions** — 定时任务 + CI/CD 运行环境
- **Cloudflare WARP** — 出口 IP 优化，避免被 Witchly 封锁
- **ffmpeg** — 录屏截图序列合成 MP4
- **WxPusher / Telegram Bot API** — 消息推送
- **Uptime Kuma** — 实时端口监控 + Webhook 触发

---

感谢代码原作者：https://github.com/ssd000012345/witchly_renew

## ⚠️ 免责声明

本项目仅供学习交流使用。使用本工具需遵守 Witchly.host 的服务条款。因使用本工具导致的账号封禁或其他损失，作者不承担任何责任。
