#!/usr/bin/env python3
"""
Witchly.host MC 服务器自动监控脚本 —— SeleniumBase UC Mode 版
功能：
  1. SeleniumBase UC Mode 自动绕过 Cloudflare Turnstile
  2. Discord Token 注入登录
  3. 检测服务器状态，离线自动启动
  4. Stability 剩余 < 3 天自动续期（扣 500 Coins）
  5. 🆕 自动关闭公告弹窗（Got it）
  6. 🆕 录屏功能（ffmpeg，可选）
"""

import os
import re
import sys
import json
import time
import shutil
import threading
import subprocess
import traceback
from pathlib import Path
from urllib.request import Request, urlopen

from seleniumbase import Driver

# ── 环境变量 ──────────────────────────────────────────────
DISCORD_TOKEN        = os.environ.get("WITCHLY_DISCORD_TOKEN", "").strip()
SERVER_ID            = os.environ.get("WITCHLY_SERVER_ID", "").strip()
TG_BOT_TOKEN         = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID           = os.environ.get("TG_CHAT_ID", "").strip()
WX_APP_TOKEN         = os.environ.get("WX_APP_TOKEN", "").strip()   # WxPusher AppToken
WX_UID               = os.environ.get("WX_UID", "").strip()          # WxPusher UID
RENEW_THRESHOLD_DAYS = float(os.environ.get("RENEW_THRESHOLD_DAYS", "3"))
ENABLE_RECORDING     = os.environ.get("ENABLE_RECORDING", "true").strip().lower() == "true"
# 由 Uptime Kuma webhook 触发时设为 true，跳过续期只做启动检查
SKIP_RENEW           = os.environ.get("SKIP_RENEW", "false").strip().lower() == "true"

BASE_URL       = "https://dash.witchly.host"
SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)
REC_FRAME_DIR  = Path("screenshots/rec")   # 录屏帧单独放，不污染截图目录
REC_FRAME_DIR.mkdir(exist_ok=True)
RECORDING_DIR  = Path("recordings")
RECORDING_DIR.mkdir(exist_ok=True)

# ── 日志 ──────────────────────────────────────────────────
def log(msg):  print(f"[INFO]  {msg}", flush=True)
def warn(msg): print(f"[WARN]  {msg}", flush=True)
def err(msg):  print(f"[ERROR] {msg}", flush=True)

# ── Telegram 推送 ─────────────────────────────────────────
def send_tg(text: str, img_path: str | None = None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        if img_path and Path(img_path).exists():
            img_bytes = Path(img_path).read_bytes()
            boundary  = "----WitchlyBoundary"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TG_CHAT_ID}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{text}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"snap.png\"\r\n"
                f"Content-Type: image/png\r\n\r\n"
            ).encode() + img_bytes + f"\r\n--{boundary}--\r\n".encode()
            req = Request(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
        else:
            req = Request(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                data=json.dumps({"chat_id": TG_CHAT_ID, "text": text}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        with urlopen(req, timeout=20):
            log("TG 推送成功")
    except Exception as e:
        warn(f"TG 推送失败: {e}")

# ── WxPusher 推送 ─────────────────────────────────────────
def send_wx(title: str, content: str):
    if not WX_APP_TOKEN or not WX_UID:
        return
    uids = [u.strip() for u in WX_UID.split(",") if u.strip()]
    payload = {
        "appToken":    WX_APP_TOKEN,
        "content":     content,
        "summary":     title,
        "contentType": 1,
        "uids":        uids,
    }
    for attempt in range(3):   # 最多重试 3 次
        try:
            req = Request(
                "https://wxpusher.zjiecode.com/api/send/message",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:   # 10s 超时，失败快，重试快
                result = json.loads(resp.read())
                if result.get("success"):
                    log("WxPusher 推送成功")
                    return
                else:
                    warn(f"WxPusher 返回异常: {result.get('msg')}")
                    return   # 返回异常不是网络问题，不重试
        except Exception as e:
            warn(f"WxPusher 推送失败 [{attempt+1}/3]: {e}")
            if attempt < 2:
                time.sleep(3)   # 等 3 秒再重试

# ── 统一推送入口 ──────────────────────────────────────────
def send_notify(title: str, content: str, img_path: str | None = None):
    """同时发 TG（如已配置）和 WxPusher（如已配置）。"""
    full_text = f"{title}\n\n{content}"
    send_tg(full_text, img_path)
    send_wx(title, content)

# ── 截图 ──────────────────────────────────────────────────
def snap(sb, name: str) -> str | None:
    try:
        path = str(SCREENSHOT_DIR / f"{name}.png")
        sb.save_screenshot(path)
        log(f"截图: {path}")
        return path
    except Exception as e:
        warn(f"截图失败: {e}")
        return None

# ── 🆕 录屏（ffmpeg 截图序列转视频）────────────────────────
class ScreenRecorder:
    """
    用 ffmpeg 把 screenshots 目录下按时间顺序生成的 PNG 拼成 MP4。
    录制期间每 N 秒自动截一帧（不依赖 X11，纯截图模式）。
    """
    def __init__(self, sb, interval: float = 2.0):
        self.sb       = sb
        self.interval = interval
        self._frames: list[Path] = []
        self._running = False
        self._thread: threading.Thread | None = None
        self._idx = 0

    def start(self):
        if not ENABLE_RECORDING:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log("🎬 录屏开始")

    def _loop(self):
        while self._running:
            try:
                name = f"rec_{self._idx:04d}"
                path = REC_FRAME_DIR / f"{name}.png"   # 存到 screenshots/rec/
                self.sb.save_screenshot(str(path))
                self._frames.append(path)
                self._idx += 1
            except Exception:
                pass
            time.sleep(self.interval)

    def stop(self, output_name: str = "run") -> str | None:
        if not ENABLE_RECORDING:
            return None
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if not self._frames:
            warn("录屏：没有帧，跳过生成视频")
            return None
        return self._compile(output_name)

    def _compile(self, output_name: str) -> str | None:
        """把帧列表写成 ffmpeg concat 文件，生成 MP4。"""
        if not shutil.which("ffmpeg"):
            warn("ffmpeg 未安装，跳过视频合成（截图帧已保留在 screenshots/rec_*.png）")
            return None

        concat_file = RECORDING_DIR / "frames.txt"
        with open(concat_file, "w") as f:
            for p in self._frames:
                f.write(f"file '{p.resolve()}'\n")
                f.write(f"duration {self.interval}\n")
            # ffmpeg concat demuxer 需要最后一帧再写一次（无 duration）
            if self._frames:
                f.write(f"file '{self._frames[-1].resolve()}'\n")

        out = RECORDING_DIR / f"{output_name}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",   # 保证偶数尺寸
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-r", "10",          # 输出 10fps，文件小
            str(out),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                log(f"🎬 视频已生成: {out}")
                return str(out)
            else:
                warn(f"ffmpeg 失败:\n{result.stderr[-500:]}")
                return None
        except Exception as e:
            warn(f"ffmpeg 异常: {e}")
            return None

# ── 等待 URL 包含关键字 ───────────────────────────────────
def wait_for_url(sb, keyword: str, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if keyword in sb.get_current_url():
            return True
        time.sleep(0.5)
    return False

# ── Cloudflare Turnstile 处理 ─────────────────────────────
def handle_cloudflare(sb):
    url = sb.get_current_url()
    is_challenge = (
        "challenge" in url
        or "turnstile" in url.lower()
        or sb.is_element_present("iframe[src*='challenges.cloudflare.com']")
    )
    if is_challenge:
        log("检测到 Cloudflare Turnstile，UC Mode 自动处理...")
        try:
            sb.uc_gui_click_captcha()
            log("Turnstile 处理完毕")
        except Exception as e:
            warn(f"uc_gui_click_captcha 异常: {e}")
        time.sleep(3)

# ── 🆕 关闭公告弹窗（Got it / ×）────────────────────────────
def dismiss_popups(sb):
    """
    Witchly 首页/My Servers 页可能出现公告弹窗，包含
    'Got it' 按钮或右上角 × 关闭按钮。循环尝试关掉所有弹窗。
    """
    closed = 0
    for _ in range(5):           # 最多关 5 层弹窗
        result = sb.execute_script("""
            // 1. 找所有包含 'Got it' / 'Got It' / 'Close' 文字的按钮
            var btns = document.querySelectorAll('button, [role="button"], a');
            for (var i = 0; i < btns.length; i++) {
                var t = (btns[i].innerText || '').trim().toLowerCase();
                if (t === 'got it' || t === 'close' || t === '×' || t === 'x') {
                    btns[i].click();
                    return 'clicked:' + btns[i].innerText.trim();
                }
            }
            // 2. 找带 modal / dialog / announcement 类的关闭按钮
            var modals = document.querySelectorAll(
                '[class*="modal"], [class*="dialog"], [class*="announcement"], [class*="popup"]'
            );
            for (var i = 0; i < modals.length; i++) {
                var closeBtn = modals[i].querySelector('button, [role="button"]');
                if (closeBtn) {
                    closeBtn.click();
                    return 'modal-close';
                }
            }
            return 'none';
        """)
        if result == "none":
            break
        log(f"关闭弹窗: {result}")
        closed += 1
        time.sleep(0.8)
    if closed:
        log(f"共关闭 {closed} 个弹窗")
        time.sleep(1)

# ── Discord OAuth 授权 ────────────────────────────────────
def handle_oauth(sb):
    """
    点击 Discord OAuth 授权页的 Authorize 按钮。
    策略：先等按钮渲染完，再用 JS 直接点（Driver 模式下更可靠）。
    """
    log("处理 Discord OAuth 授权...")
    # 等授权页完全渲染（按钮需要 JS 加载）
    time.sleep(3)

    for attempt in range(15):
        cur = sb.get_current_url()
        if "discord.com" not in cur:
            log(f"OAuth 完成，已离开 Discord: {cur}")
            return

        # 滚动到底部让按钮可见
        sb.execute_script("""
            document.querySelectorAll('div').forEach(el => {
                if (el.scrollHeight > el.clientHeight + 10) el.scrollTop = el.scrollHeight;
            });
            window.scrollTo(0, document.body.scrollHeight);
        """)
        time.sleep(0.5)

        # 方式1：JS 直接点击 Authorize（最可靠，不受可见性限制）
        clicked = sb.execute_script("""
            var btns = document.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                var t = (btns[i].innerText || btns[i].textContent || '').trim().toLowerCase();
                // 点 Authorize / 授权，跳过 Cancel / Deny
                if ((t.includes('authoriz') || t === '授权') &&
                    !t.includes('cancel') && !t.includes('deny')) {
                    btns[i].click();
                    return 'js:' + btns[i].innerText.trim();
                }
            }
            // 兜底：找唯一的 submit 按钮
            var submit = document.querySelector('button[type="submit"]');
            if (submit) {
                var st = (submit.innerText || '').toLowerCase();
                if (!st.includes('cancel') && !st.includes('deny')) {
                    submit.click();
                    return 'submit:' + submit.innerText.trim();
                }
            }
            return 'not_found';
        """)
        log(f"OAuth 点击结果 [{attempt+1}/15]: {clicked}")

        if clicked and "not_found" not in str(clicked):
            # 等待跳回 witchly（最多 15 秒）
            if wait_for_url(sb, "witchly.host", timeout=15):
                log("OAuth 授权成功，已跳回 Witchly")
                return
            # 没跳回就继续重试
            time.sleep(1)
        else:
            # 按钮没找到，截图一次帮助调试
            if attempt == 5:
                snap(sb, "oauth-btn-not-found")
            time.sleep(1.5)

    # 最终仍在 Discord，截图并报错
    snap(sb, "oauth-timeout")
    raise RuntimeError(f"OAuth 授权超时，停留在: {sb.get_current_url()}")

# ── Discord Token 注入登录 ────────────────────────────────
def discord_login(sb):
    log("打开 Witchly 首页...")
    sb.uc_open_with_reconnect(BASE_URL, reconnect_time=2)  # 从 4 → 2
    # 等页面真正加载（URL 稳定），最多 8 秒
    wait_for_url(sb, "witchly.host", timeout=8)
    handle_cloudflare(sb)

    log(f"当前页面: {sb.get_current_url()}")

    # 如果已登录直接跳过
    if "dash.witchly.host" in sb.get_current_url() and "/servers" in sb.get_current_url():
        log("已登录，跳过登录步骤")
        dismiss_popups(sb)
        return

    clicked = False
    for sel in [
        'button:contains("Sign In with Discord")',
        'a:contains("Sign In with Discord")',
        'button:contains("Login with Discord")',
        'a:contains("Login with Discord")',
    ]:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                log(f"点击: {sel}")
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        try:
            sb.uc_click('[class*="discord"]')
            clicked = True
        except Exception:
            pass

    if not clicked:
        snap(sb, "login-btn-not-found")
        raise RuntimeError("未找到 Discord 登录按钮，请查看截图")

    if not wait_for_url(sb, "discord.com", timeout=15):
        snap(sb, "discord-redirect-failed")
        raise RuntimeError("未能跳转到 Discord，当前: " + sb.get_current_url())

    log("已到达 Discord，注入 Token...")

    sb.execute_script("""
        var token = arguments[0];
        var f = document.createElement('iframe');
        f.style.display = 'none';
        document.body.appendChild(f);
        f.contentWindow.localStorage.setItem('token', '"' + token + '"');
        try { localStorage.setItem('token', '"' + token + '"'); } catch(e) {}
        document.body.removeChild(f);
    """, DISCORD_TOKEN)

    sb.refresh()
    # 等 Discord 重定向，不用固定 sleep
    time.sleep(1.5)

    if "discord.com/login" in sb.get_current_url():
        snap(sb, "token-invalid")
        raise RuntimeError("Discord Token 无效或已过期，请重新获取")

    log("Token 注入成功")

    if "discord.com/oauth2/authorize" in sb.get_current_url():
        handle_oauth(sb)

    # 等待跳回 Witchly（handle_oauth 内部已经在等了）
    if "discord.com" in sb.get_current_url():
        # OAuth 页面还没处理完，再调一次
        handle_oauth(sb)

    # 最终确认已在 Witchly
    if not wait_for_url(sb, "witchly.host", timeout=10):
        snap(sb, "not-witchly")
        raise RuntimeError("未能跳回 Witchly，当前: " + sb.get_current_url())

    handle_cloudflare(sb)
    dismiss_popups(sb)

    log(f"✅ 登录成功！当前: {sb.get_current_url()}")

# ── 解析 Stability 时间 ───────────────────────────────────
def parse_stability_days(text: str) -> float | None:
    if not text:
        return None
    t = text.lower().strip()
    d = re.search(r"(\d+)\s*d", t)
    h = re.search(r"(\d+)\s*h", t)
    m = re.search(r"(\d+)\s*m", t)
    total = (int(d.group(1)) if d else 0) \
          + (int(h.group(1)) if h else 0) / 24.0 \
          + (int(m.group(1)) if m else 0) / 1440.0
    return total if total > 0 else None

def fmt_days(v: float) -> str:
    d, h = int(v), int((v - int(v)) * 24)
    return f"{d}d {h}h" if d > 0 else f"{h}h"

# ── 读取 My Servers 页信息 ────────────────────────────────
def get_server_info(sb) -> dict:
    log("打开 My Servers 页...")
    sb.uc_open_with_reconnect(f"{BASE_URL}/servers", reconnect_time=2)
    time.sleep(3)  # Next.js hydration 需要一点时间
    handle_cloudflare(sb)
    dismiss_popups(sb)

    # ── 策略A：直接读整页可见文字，用正则抓 Stability 值 ──
    # 截图证明 "6d 11h" 在页面上可见，innerText 应该能拿到
    # 同时把 "STABILITY\n6d 11h" 这种换行格式也覆盖到
    page_text = sb.execute_script("return document.body.innerText || '';")
    log(f"[DEBUG] 页面文字片段: {page_text[:300]!r}")  # 调试用，确认能拿到文字

    stability_text = ""
    # 匹配 "STABILITY" 附近（上下 60 字符内）的 Nd Nh 格式
    m = re.search(
        r"STABILITY[\s\S]{0,60}?(\d+d\s*\d+h|\d+d\s*\d+m|\d+d|\d+h)",
        page_text, re.IGNORECASE
    )
    if m:
        stability_text = m.group(1).strip()
    else:
        # 退而求其次：页面任意位置找 Nd Nh
        m2 = re.search(r"\b(\d+d\s+\d+h|\d+d\s+\d+m|\d+d|\d+h)\b", page_text)
        if m2:
            stability_text = m2.group(1).strip()

    # ── 服务器在线状态：读绿点颜色 或 文字 ──
    status = sb.execute_script(f"""
        var SERVER_ID = "{SERVER_ID}";
        // 找包含 server id 的卡片
        var card = null;
        var all = document.querySelectorAll('*');
        for (var i = 0; i < all.length; i++) {{
            var t = all[i].textContent || '';
            if (t.includes(SERVER_ID) && all[i].children.length > 0 && all[i].children.length < 40)
                card = all[i];
        }}
        var root = card || document.body;

        // 绿色圆点 = online（rgb 接近 34,197,94 即 green-500）
        var dots = root.querySelectorAll('span, div, i');
        for (var i = 0; i < dots.length; i++) {{
            var bg = getComputedStyle(dots[i]).backgroundColor || '';
            if (bg.match(/rgb\\(34,\\s*197/) || bg.match(/rgb\\(74,\\s*222/))
                return 'online';
            if (bg.match(/rgb\\(239,\\s*68/) || bg.match(/rgb\\(248,\\s*113/))
                return 'offline';
        }}

        // 文字兜底
        var txt = (root.textContent || '').toLowerCase();
        if (/\\bonline\\b/.test(txt))  return 'online';
        if (/\\boffline\\b/.test(txt)) return 'offline';
        return 'unknown';
    """)

    stab_days = parse_stability_days(stability_text)
    stab_str  = (stability_text or "?") + (f" ({fmt_days(stab_days)})" if stab_days else "")
    log(f"状态: {status}  |  Stability: {stab_str}")

    return {
        "status":         status or "unknown",
        "stability_text": stability_text,
        "stability_days": stab_days,
    }

# ── 进入控制台（点 Manage 按钮，跟着真实跳转走）────────────
def open_manage_page(sb) -> bool:
    """
    从 My Servers 页点击 Manage 按钮进入控制台。
    实际 URL 是 /servers/{id}/manage/home，不是 /console。
    返回 True 表示成功进入 manage 页面。
    """
    # 已经在 manage 页面，直接返回
    if "/manage/" in sb.get_current_url():
        return True

    # 如果不在 My Servers 列表页，先导航过去
    cur = sb.get_current_url()
    on_servers_list = (
        cur.rstrip("/").endswith("/servers") or
        cur.rstrip("/").endswith("/servers#")
    )
    if not on_servers_list:
        sb.uc_open_with_reconnect(f"{BASE_URL}/servers", reconnect_time=2)
        time.sleep(3)
        handle_cloudflare(sb)
        dismiss_popups(sb)

    log("点击 Manage 按钮进入控制台...")

    # 方式1：SeleniumBase 直接找按钮点
    clicked = False
    for sel in [
        'button:contains("Manage")',
        'a:contains("Manage")',
        '[role="button"]:contains("Manage")',
    ]:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                clicked = True
                log(f"已点击 Manage: {sel}")
                break
        except Exception:
            continue

    # 方式2：JS 点击
    if not clicked:
        result = sb.execute_script("""
            var btns = document.querySelectorAll('button, a, [role="button"]');
            for (var i = 0; i < btns.length; i++) {
                var t = (btns[i].innerText || '').trim().toLowerCase();
                if (t === 'manage') {
                    btns[i].click();
                    return 'js:' + btns[i].tagName;
                }
            }
            return 'not_found';
        """)
        log(f"JS Manage 结果: {result}")
        clicked = "not_found" not in str(result)

    if not clicked:
        snap(sb, "manage-btn-not-found")
        warn("未找到 Manage 按钮")
        return False

    # 等待跳转到 manage 页面（URL 里包含 /manage/）
    deadline = time.time() + 15
    while time.time() < deadline:
        if "/manage/" in sb.get_current_url():
            break
        time.sleep(0.5)

    time.sleep(2)
    handle_cloudflare(sb)
    dismiss_popups(sb)

    current = sb.get_current_url()
    log(f"控制台页面: {current}")
    if "/manage/" not in current:
        snap(sb, "manage-nav-failed")
        warn(f"未能进入控制台，当前: {current}")
        return False
    return True


# ── 在 manage 页面原地刷新读状态（启动后轮询用）────────────
def read_manage_status(sb) -> str:
    """
    已在 /manage/home 页面时，直接刷新页面读取电源状态。
    不需要重新找 Manage 按钮，避免启动过程中按钮消失的问题。
    """
    sb.refresh()
    time.sleep(4)
    handle_cloudflare(sb)
    dismiss_popups(sb)

    page_text = sb.execute_script("return document.body.innerText || '';")

    status_match = re.search(
        r"\b(ONLINE|OFFLINE|STARTING|STOPPING|RUNNING|STOPPED)\b",
        page_text, re.IGNORECASE
    )
    if status_match:
        raw = status_match.group(1).upper()
        mapping = {
            "ONLINE": "running", "RUNNING": "running",
            "OFFLINE": "offline", "STOPPED": "offline",
            "STARTING": "starting", "STOPPING": "stopping",
        }
        status = mapping.get(raw, "unknown")
        log(f"  刷新状态: {status}（原文: {raw}）")
        return status

    btn_status = sb.execute_script("""
        var btns = document.querySelectorAll('button, [role="button"]');
        for (var i = 0; i < btns.length; i++) {
            var t = (btns[i].innerText || '').toLowerCase().trim();
            if (t === 'stop' || t === 'restart' || t === 'kill') return 'running';
            if (t === 'start') return 'offline';
        }
        return 'unknown';
    """)
    log(f"  刷新状态（按钮）: {btn_status}")
    return btn_status or "unknown"


# ── 检测控制台电源状态 ────────────────────────────────────
def get_power_status(sb) -> str:
    """
    读控制台页面的电源状态。
    控制台真实 URL: /servers/{id}/manage/home
    判断依据（按优先级）：
      1. 页面状态标签：ONLINE / OFFLINE / STARTING / STOPPING
      2. POWER CONTROLS 区域的按钮：Stop/Restart = running，Start = offline
    """
    if not open_manage_page(sb):
        warn("无法进入控制台，电源状态未知")
        return "unknown"

    page_text = sb.execute_script("return document.body.innerText || '';")
    log(f"[DEBUG] 控制台文字片段: {page_text[:300]!r}")

    # 优先找状态标签（截图里是 "● ONLINE" 绿色徽标）
    # 必须精确匹配，避免把 URL 或其他文字误判
    status_match = re.search(
        r"\b(ONLINE|OFFLINE|STARTING|STOPPING|RUNNING|STOPPED)\b",
        page_text, re.IGNORECASE
    )
    if status_match:
        raw = status_match.group(1).upper()
        mapping = {
            "ONLINE": "running", "RUNNING": "running",
            "OFFLINE": "offline", "STOPPED": "offline",
            "STARTING": "starting", "STOPPING": "stopping",
        }
        status = mapping.get(raw, "unknown")
        log(f"电源状态（标签）: {status}（原文: {raw}）")
        return status

    # 兜底：按钮文字
    btn_status = sb.execute_script("""
        var btns = document.querySelectorAll('button, [role="button"]');
        for (var i = 0; i < btns.length; i++) {
            var t = (btns[i].innerText || '').toLowerCase().trim();
            if (t === 'stop' || t === 'stop server' || t === 'restart' || t === 'kill')
                return 'running';
            if (t === 'start' || t === 'start server')
                return 'offline';
        }
        return 'unknown';
    """)
    log(f"电源状态（按钮）: {btn_status}")
    return btn_status or "unknown"


# ── 启动服务器（在控制台页点 Start）──────────────────────
def start_server(sb) -> bool:
    """
    在当前控制台页面（/manage/home）点击 Start 按钮。
    如果不在控制台页则先导航过去。
    """
    if "manage" not in sb.get_current_url():
        if not open_manage_page(sb):
            return False

    log("点击 Start 按钮...")

    # 方式1：SeleniumBase
    for sel in ['button:contains("Start")', 'a:contains("Start")']:
        try:
            if sb.is_element_visible(sel):
                # 确认不是 Stop/Restart
                txt = sb.get_text(sel).strip().lower()
                if txt == "start" or txt == "start server":
                    sb.uc_click(sel)
                    log(f"已点击 Start: {sel}")
                    return True
        except Exception:
            continue

    # 方式2：JS 精确匹配
    result = sb.execute_script("""
        var btns = document.querySelectorAll('button, [role="button"], a');
        for (var i = 0; i < btns.length; i++) {
            var t = (btns[i].innerText || '').trim().toLowerCase();
            if (t === 'start' || t === 'start server') {
                btns[i].click();
                return 'clicked:' + btns[i].innerText.trim();
            }
        }
        return 'not_found';
    """)
    log(f"JS Start 结果: {result}")
    return "not_found" not in str(result)

# ── 续期 Extend Realm Life ────────────────────────────────
def renew_server(sb) -> bool:
    log("执行续期...")
    sb.uc_open_with_reconnect(f"{BASE_URL}/servers", reconnect_time=3)
    time.sleep(3)
    handle_cloudflare(sb)
    time.sleep(2)
    dismiss_popups(sb)   # 🆕 续期前也关弹窗

    clicked = sb.execute_script(f"""
        var SERVER_ID = "{SERVER_ID}";
        var card = null;
        var all = document.querySelectorAll('*');
        for (var i = 0; i < all.length; i++) {{
            var el = all[i];
            if ((el.innerText || '').includes(SERVER_ID) &&
                el.children.length > 0 && el.children.length < 30)
                card = el;
        }}
        var root = card || document.body;

        var btns = root.querySelectorAll('button, [role="button"]');
        for (var i = 0; i < btns.length; i++) {{
            var btn = btns[i];
            var cls = (btn.className || '').toString();
            var bg  = getComputedStyle(btn).backgroundColor || '';
            if (cls.includes('purple') || cls.includes('violet') ||
                bg.match(/rgb\\(139,\\s*92/) || bg.match(/rgb\\(124,\\s*58/)) {{
                btn.click();
                return 'purple-btn';
            }}
        }}

        for (var i = 0; i < all.length; i++) {{
            var el = all[i];
            if ((el.innerText || '').trim().toUpperCase() === 'STABILITY') {{
                var area = el.closest('[class]') || el.parentElement;
                if (area) {{
                    var ab = area.querySelectorAll('button, [role="button"]');
                    if (ab.length > 0) {{
                        ab[ab.length - 1].click();
                        return 'stability-btn';
                    }}
                }}
            }}
        }}

        for (var i = 0; i < btns.length; i++) {{
            var btn = btns[i];
            if (btn.querySelector('svg')) {{
                var label = (btn.title || btn.getAttribute('aria-label') || '').toLowerCase();
                if (/renew|extend|stab/.test(label)) {{
                    btn.click();
                    return 'svg-btn:' + label;
                }}
            }}
        }}

        return 'not_found';
    """)

    log(f"续期按钮: {clicked}")
    if "not_found" in str(clicked):
        warn("未找到续期按钮")
        snap(sb, "renew-not-found")
        return False

    time.sleep(2)

    for sel in [
        'button:contains("Proceed")',
        'button[class*="purple"]',
        'button[class*="bg-purple"]',
    ]:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                log(f"已点击 Proceed: {sel}")
                time.sleep(3)
                return True
        except Exception:
            continue

    r = sb.execute_script("""
        var btns = document.querySelectorAll('button');
        for (var i = 0; i < btns.length; i++) {
            if ((btns[i].innerText || '').toLowerCase().includes('proceed')) {
                btns[i].click();
                return 'js-proceed';
            }
        }
        return 'not_found';
    """)
    if "not_found" not in str(r):
        log(f"JS Proceed: {r}")
        time.sleep(3)
        return True

    warn("未找到 Proceed 按钮")
    snap(sb, "proceed-not-found")
    return False

# ── 主流程 ────────────────────────────────────────────────
def run():
    if not DISCORD_TOKEN:
        raise RuntimeError("缺少: WITCHLY_DISCORD_TOKEN")
    if not SERVER_ID:
        raise RuntimeError("缺少: WITCHLY_SERVER_ID")

    log(f"▶ 监控服务器 [{SERVER_ID}]，续期阈值 < {RENEW_THRESHOLD_DAYS}d")
    if ENABLE_RECORDING:
        log("🎬 录屏已启用（设置 ENABLE_RECORDING=false 可关闭）")

    driver = Driver(
        uc=True,
        headless=False,          # Driver(uc=True) 不需要 headless，配合 Xvfb 运行
        undetectable=True,
        chromium_arg="--no-sandbox,--disable-dev-shm-usage,--disable-gpu",
    )
    sb = driver                  # 其余代码全部用 sb，无需任何修改
    with driver:
        # 🆕 启动录屏
        recorder = ScreenRecorder(sb, interval=2.0)
        recorder.start()

        try:
            # ① 登录
            discord_login(sb)
            snap(sb, "01-after-login")

            # ② 读取服务器信息（Stability + 状态）
            info           = get_server_info(sb)
            stability_days = info["stability_days"]
            stability_text = info["stability_text"]
            snap(sb, "02-my-servers")

            # ③ 续期检查（Uptime Kuma 紧急触发时跳过）
            if SKIP_RENEW:
                log("⏩ SKIP_RENEW=true，跳过续期检查")
            elif stability_days is not None and stability_days < RENEW_THRESHOLD_DAYS:
                log(f"⚠ Stability {fmt_days(stability_days)} < {RENEW_THRESHOLD_DAYS}d，触发续期")
                ok = renew_server(sb)
                snap(sb, "03-after-renew")
                if ok:
                    new_info       = get_server_info(sb)
                    new_stab_text  = new_info["stability_text"] or "?"
                    new_stab_days  = new_info["stability_days"]
                    new_stab_fmt   = fmt_days(new_stab_days) if new_stab_days else new_stab_text
                    log(f"🔄 续期成功，现在剩余: {new_stab_fmt}")
                    stability_text = new_stab_text
                    stability_days = new_stab_days
                    # ✅ 只在续期成功时推送
                    send_notify(
                        title   = "🔄 Witchly 服务器续期成功",
                        content = f"续期完成，剩余稳定时间：{new_stab_fmt}",
                        img_path= snap(sb, "03-renew-ok"),
                    )
                else:
                    warn(f"⚠️ 续期失败（Coins 不足或按钮未找到），剩余: {stability_text}")
            elif stability_days is not None:
                log(f"✅ Stability {fmt_days(stability_days)}，无需续期")
            else:
                warn("未能解析 Stability 时间")

            # ④ 电源状态检查（进入 /manage/home 点 Manage 按钮）
            power = get_power_status(sb)
            snap(sb, "04-manage-home")

            if power == "running":
                log("✅ 服务器运行中")

            elif power in ("offline", "stopped"):
                log("🔴 服务器离线，自动启动...")
                start_server(sb)
                time.sleep(6)

                # 点完 Start 先等 15 秒，让服务器有时间从 offline 变 starting
                log("  等待服务器响应 Start 指令（15秒）...")
                time.sleep(15)

                # 然后每 10 秒刷新一次，最多等 20 次（200秒）
                # 只有 running(ONLINE) 才算成功，offline 连续出现 3 次才认为失败
                final_power = "unknown"
                offline_count = 0
                for i in range(20):
                    final_power = read_manage_status(sb)
                    log(f"  等待启动 [{i+1}/20] {final_power}")
                    if final_power == "running":
                        break
                    elif final_power == "offline":
                        offline_count += 1
                        if offline_count >= 3:
                            warn("  连续 3 次检测到 offline，确认启动失败")
                            break
                    else:
                        offline_count = 0  # starting/unknown 重置计数
                    time.sleep(10)

                snap(sb, "05-after-start")
                log(f"服务器启动结果: {final_power}")
                if final_power == "running":
                    send_notify(
                        title   = "🚀 Witchly 服务器已重新上线",
                        content = "检测到服务器离线，已自动执行 Start，服务器现已 ONLINE。",
                    )
                else:
                    send_notify(
                        title   = "⚠️ Witchly 服务器启动中",
                        content = f"已发送 Start 指令，当前状态：{final_power}，请稍后手动确认是否在线。",
                    )

            elif power in ("starting", "stopping"):
                log(f"⏳ 服务器 {power} 中...")

            else:
                log(f"❓ 状态未知（{power}），请手动检查")

            log("一切正常，静默退出")

        except Exception as e:
            err(f"异常: {e}")
            traceback.print_exc()
            send_notify(
                title   = "❌ Witchly 监控脚本异常",
                content = str(e),
                img_path= snap(sb, "error"),
            )
            recorder.stop("run-error")
            sys.exit(1)

        finally:
            # 🆕 停止录屏，生成视频
            video = recorder.stop("run")
            if video:
                log(f"录屏保存于: {video}")

    log("▶ 完成")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
