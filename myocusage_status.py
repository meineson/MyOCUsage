#!/usr/bin/env python3
"""OpenCode 月用量监控 - macOS 状态栏应用"""

import json
import os
import re
import sys
import math
import plistlib
import subprocess
import urllib.parse
import logging
from datetime import datetime, timedelta

import requests
import rumps
from PIL import Image, ImageDraw
from AppKit import (
    NSImage, NSFont,
    NSView, NSTextField, NSProgressIndicator,
    NSTextAlignmentRight, NSMakeRect,
    NSProgressIndicatorBarStyle,
)

import warnings
warnings.filterwarnings("ignore", message=".*urllib3.*")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
ICON_FILE = os.path.expanduser("~/.myocusage_icon.png")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(os.path.expanduser("~/.myocusage.log")), logging.StreamHandler()],
)
log = logging.getLogger("myocusage")


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


class AuthExpiredError(Exception):
    pass


# ── Convex JS 响应 → JSON ──────────────────────────

def _js_to_json(js_str):
    s = js_str
    s = re.sub(r'!0(?=[,}:])', 'true', s)
    s = re.sub(r'!1(?=[,}:])', 'false', s)
    s = re.sub(r'([a-zA-Z_$]\w*)(?=\s*:)', r'"\1"', s)
    s = re.sub(r'\$R\[\d+\]=', '', s)
    s = re.sub(r'\$R\[\d+\]', 'null', s)
    return json.loads(s)


def _parse_convex_response(raw):
    if "location" in raw and ("/auth/authorize" in raw or "/login" in raw):
        raise AuthExpiredError("认证已过期，请更新 config.json 中 cookies 字段")

    m = re.search(r'\$R\[0\]=(.+)', raw)
    if m:
        val = m.group(1)
        val = re.sub(r'\)\(.*$', '', val)
        val = val.rstrip(')')
        try:
            return _js_to_json(val)
        except Exception as e:
            log.warning(f"JS→JSON 转换失败: {e}, raw={val[:200]}")

    for m in re.finditer(r'(\{.*\}|\[.*\])', raw):
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            continue

    raise ValueError(f"无法解析 Convex 响应: {raw[:200]}")


# ── API 请求 ──────────────────────────────────────

def make_api_request(config):
    args = {
        "t": {"t": 9, "i": 0, "l": 1, "a": [{"t": 1, "s": config["workspace_id"]}], "o": 0},
        "f": 31, "m": [],
    }
    encoded_args = urllib.parse.quote(json.dumps(args, separators=(",", ":")))
    url = f"https://opencode.ai/_server?id={config['server_id']}&args={encoded_args}"

    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "cookie": config["cookies"],
        "referer": f"https://opencode.ai/workspace/{config['workspace_id']}/usage",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "x-server-id": config["server_id"],
        "x-server-instance": config["server_instance"],
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }

    log.info(f"请求 URL: {url}")

    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    raw = resp.text
    log.debug(f"API 响应 ({len(raw)} bytes)")

    if not raw.strip():
        raise ValueError("空响应")

    content_type = resp.headers.get("content-type", "")
    if "javascript" in content_type or raw.startswith(";"):
        return _parse_convex_response(raw)

    try:
        return resp.json()
    except json.JSONDecodeError:
        with open(os.path.expanduser("~/.myocusage_response.txt"), "w") as f:
            f.write(raw)
        raise ValueError("非 JSON 响应，已保存到 ~/.myocusage_response.txt")


# ── 用量数据解析 ─────────────────────────────────

PERIOD_MAP = {
    "rollingUsage": "5h", "hourly": "5h", "5h": "5h",
    "weeklyUsage": "weekly", "weekly": "weekly", "week": "weekly",
    "monthlyUsage": "monthly", "monthly": "monthly", "month": "monthly",
}


def _fmt_reset(secs):
    if secs < 86400:
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        return f"{h:02d}:{m:02d}:{s:02d}"
    dt = datetime.now() + timedelta(seconds=secs)
    return dt.strftime("%m-%d %H:%M")


def parse_usage(data):
    raw_data = data
    if isinstance(data, dict) and "value" in data:
        data = data["value"]

    results = {}

    if not isinstance(data, dict):
        return results

    for src_key, period in PERIOD_MAP.items():
        entry = data.get(src_key)
        if isinstance(entry, dict):
            used = entry.get("cost") or entry.get("amount") or entry.get("usagePercent") or entry.get("usage")
            reset = entry.get("resetInSec")
            limit = entry.get("limit") or entry.get("max") or entry.get("quota") or entry.get("budget") or entry.get("total")
            if used is not None:
                results[period] = {"used": used, "limit": limit, "resetInSec": reset}

    config = load_config()
    fallback = config.get("plan_monthly_limit")
    if fallback:
        for p in results:
            if results[p]["limit"] is None and p in ("monthly", "total"):
                results[p]["limit"] = fallback

    if not results:
        log.warning(f"无法解析用量数据: {json.dumps(raw_data, ensure_ascii=False)[:300]}")

    return results


# ── 开机自启 ─────────────────────────────────────

PLIST_PATH = os.path.expanduser("~/Library/LaunchAgents/com.myocusage.plist")


def _install_launchd():
    python = sys.executable
    script = os.path.abspath(__file__)
    plist = {
        "Label": "com.myocusage",
        "ProgramArguments": [python, script, "--daemon"],
        "RunAtLoad": True,
        "KeepAlive": False,
    }
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)
    log.info("开机自启已开启（下次登录生效）")


def _uninstall_launchd():
    if os.path.exists(PLIST_PATH):
        os.unlink(PLIST_PATH)
        log.info("开机自启已关闭")


def _autostart_enabled():
    return os.path.exists(PLIST_PATH)


def _uninstall_launchd():
    if os.path.exists(PLIST_PATH):
        subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True)
        os.unlink(PLIST_PATH)
        log.info("开机自启已关闭")


def _autostart_enabled():
    if not os.path.exists(PLIST_PATH):
        return False
    r = subprocess.run(["launchctl", "list", "com.myocusage"], capture_output=True, text=True)
    return r.returncode == 0


# ── 动态瓶子图标 ─────────────────────────────────

_LIQUID_COLORS = [
    (30,  (50, 180, 60)),     # 绿
    (60,  (245, 166, 35)),    # 橙
    (80,  (245, 124, 0)),     # 深橙
    (100, (229, 57, 53)),     # 红
]


def _liquid_color(pct):
    for threshold, color in _LIQUID_COLORS:
        if pct <= threshold:
            return color
    return _LIQUID_COLORS[-1][1]


def _bottle_angle(pct):
    return pct / 100 * 20


def _render_bottle(usage_pct, angle=0):
    """简洁风格瓶子图标 — 512x512 绘制 → 44x44 保存"""
    S = 512
    cx = S // 2

    # 比例: 全身高 10 份, 瓶身宽 5 份
    total_h = S * 0.82
    body_w = int(total_h * 0.50)
    cap_h = int(total_h * 0.10)
    neck_h = int(total_h * 0.12)
    body_h = total_h - cap_h - neck_h
    body_w = int(body_w * 1.0 if body_w % 2 == 0 else body_w + 1)

    cap_y = int(S * 0.04)
    neck_y = cap_y + cap_h
    body_y = int(neck_y + neck_h * 0.85)

    body_x = (S - body_w) // 2
    neck_w = int(body_w * 0.30)
    neck_x = (S - neck_w) // 2
    cap_w = neck_w + int(neck_w * 0.20)
    cap_x = (S - cap_w) // 2

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    glass = (180, 180, 180)

    # 瓶盖 — 简洁
    draw.rounded_rectangle([cap_x, cap_y, cap_x + cap_w, cap_y + cap_h],
                           radius=5, fill=(55, 125, 200), outline=(35, 95, 170))

    # 瓶颈
    draw.rectangle([neck_x, neck_y, neck_x + neck_w, body_y],
                   fill=(240, 240, 240), outline=glass, width=3)

    # 瓶身 — 圆角矩形
    body_r = 24
    bb = (body_x, body_y, body_x + body_w, body_y + body_h)
    draw.rounded_rectangle(bb, radius=body_r, outline=glass, width=4)

    # 瓶身填充 (透明玻璃)
    bmask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(bmask).rounded_rectangle(bb, radius=body_r, fill=255)
    bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(bg).rounded_rectangle(bb, radius=body_r, fill=(240, 240, 240))
    img = Image.alpha_composite(img, Image.composite(bg, Image.new("RGBA", (S, S), (0, 0, 0, 0)), bmask))

    # 瓶内液体
    liquid_h = int(body_h * (100 - usage_pct) / 100)
    if liquid_h > 0:
        color = _liquid_color(usage_pct)
        mask = Image.new("L", (S, S), 0)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle(bb, radius=body_r, fill=255)
        md.rectangle([body_x, body_y + body_h - liquid_h, body_x + body_w, body_y + body_h], fill=255)

        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            [body_x + 6, body_y + body_h - liquid_h, body_x + body_w - 6, body_y + body_h - 4],
            radius=20, fill=color)
        img = Image.alpha_composite(
            img,
            Image.composite(layer, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask),
        )

    # 高光
    draw = ImageDraw.Draw(img)
    hx = body_x + body_w * 0.12
    hw = body_w * 0.10
    draw.rectangle([int(hx), body_y + 20, int(hx + hw), body_y + body_h - 20],
                   fill=(255, 255, 255, 45))

    # 瓶底
    base_h = int(body_h * 0.04)
    draw.rounded_rectangle(
        [body_x + 6, body_y + body_h - base_h, body_x + body_w - 6, body_y + body_h],
        radius=4, fill=(200, 200, 200), outline=glass)

    # 旋转
    if angle != 0:
        img = img.rotate(angle, expand=True, fillcolor=(0, 0, 0, 0), resample=Image.BICUBIC)
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))

    img = img.resize((44, 44), Image.LANCZOS)
    img.save(ICON_FILE, "PNG")


def _apply_icon(app, pct, angle):
    _render_bottle(pct, angle)
    app.icon = ICON_FILE
    try:
        btn = app._ns_status_bar_button
        if btn:
            ns_img = btn.image
            if ns_img:
                ns_img.setSize_((22, 22))
    except AttributeError:
        pass


# ── 菜单栏应用 ────────────────────────────────────

ROW_W = 260
ROW_H = 22
PROGRESS_W = 85
PROGRESS_H = 10


def _make_label(text, x, w, align_right=False, bold=False):
    """创建 NSTextField 标签"""
    f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, 0, w, ROW_H))
    f.setStringValue_(text)
    f.setBordered_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setFont_(NSFont.boldSystemFontOfSize_(11) if bold else NSFont.systemFontOfSize_(11))
    if align_right:
        f.setAlignment_(NSTextAlignmentRight)
    return f


def _make_progress():
    """创建原生进度条"""
    p = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(0, 4, PROGRESS_W, PROGRESS_H))
    p.setStyle_(NSProgressIndicatorBarStyle)
    p.setIndeterminate_(False)
    p.setMinValue_(0)
    p.setMaxValue_(100)
    p.setDoubleValue_(0)
    p.setDisplayedWhenStopped_(True)
    return p


def _create_row_view(title_text):
    """创建一行菜单项: 标题 + 进度条 + 百分比 + 重置时间"""
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, ROW_W, ROW_H))

    title = _make_label(title_text, 8, 45)
    bar = _make_progress()
    bar.setFrame_(NSMakeRect(54, 6, PROGRESS_W, PROGRESS_H))
    pct_label = _make_label("", 148, 32, align_right=True, bold=True)
    reset_label = _make_label("", 185, 70, align_right=True)

    view.addSubview_(title)
    view.addSubview_(bar)
    view.addSubview_(pct_label)
    view.addSubview_(reset_label)

    return view, title, bar, pct_label, reset_label

class MyocUsageApp(rumps.App):
    def __init__(self):
        super().__init__("OC", title="...", quit_button=rumps.MenuItem("🚪 退出", callback=self.quit_app))
        self.config = load_config()
        self.usage_data = {}
        self.last_error = None
        self.last_raw_data = None
        self.refresh_timer = None

        self._prev_display_pct = 0
        self._display_pct = 0
        self._anim_timer = None
        self._anim_frames = []
        self._anim_idx = 0
        self._manual_refreshing = False

        self._period_views = {}
        self.menu_items = {}
        for period, p_label in [("5h", "5小时"), ("weekly", "本周"), ("monthly", "本月")]:
            view, title, bar, pct_label, reset_label = _create_row_view(p_label)
            item = rumps.MenuItem("", callback=None)
            item._menuitem.setView_(view)
            self.menu.add(item)
            self.menu_items[period] = item
            self._period_views[period] = {"title": title, "bar": bar, "pct": pct_label, "reset": reset_label}
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("📊 用量详情", callback=self.open_usage))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("🔄 手动刷新", callback=self.manual_refresh))
        title = "✅ 开机自启" if _autostart_enabled() else "☐ 开机自启"
        self.autostart_item = rumps.MenuItem(title, callback=self.toggle_autostart)
        self.menu.add(self.autostart_item)

        self.refresh_data(None)
        interval = self.config.get("refresh_interval", 60)
        self.refresh_timer = rumps.Timer(self.refresh_data, interval)
        self.refresh_timer.start()
        log.info("MyocUsage 已启动")

    # ── 图标动画 ──

    def _set_icon(self, pct, angle):
        _apply_icon(self, pct, angle)

    def _stop_anim(self):
        if self._anim_timer:
            self._anim_timer.stop()
        self._anim_timer = None
        self._anim_frames = []
        self._anim_idx = 0
        self._manual_refreshing = False

    def _anim_tick(self, _):
        if self._anim_idx >= len(self._anim_frames):
            self._stop_anim()
            # Final frame — stable icon
            self._set_icon(self._display_pct, _bottle_angle(self._display_pct))
            return
        angle = self._anim_frames[self._anim_idx]
        self._set_icon(self._display_pct, angle)
        self._anim_idx += 1

    def _start_anim(self, from_pct, to_pct, n=10):
        self._stop_anim()
        from_a = _bottle_angle(from_pct)
        to_a = _bottle_angle(to_pct)
        diff_pct = abs(to_pct - from_pct)
        is_reset = diff_pct >= 30

        frames = []
        if is_reset:
            # 重置：先回正 → 过冲 → 摇晃站稳
            for i in range(n):
                t = i / max(n - 1, 1)
                if t < 0.4:
                    t2 = t / 0.4
                    ease = t2 * t2 * (3 - 2 * t2)  # smoothstep
                    angle = from_a + (to_a - from_a) * ease * 0.8
                    angle += (1 - t2) * 10  # 前半程附加摇晃
                else:
                    t2 = (t - 0.4) / 0.6
                    decay = 1 - t2 * 0.85
                    wobble = 7 * decay * math.sin(t2 * 5 * math.pi)
                    angle = to_a + wobble
                frames.append(angle)
        else:
            # 小幅升降 → 过冲回弹（最小过冲幅度 2°）
            min_overshoot = 2.0
            overshoot = max(abs(to_a - from_a), min_overshoot)
            for i in range(n):
                t = i / max(n - 1, 1)
                c1, c3 = 1.70158, 2.70158
                ease = 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2
                angle = from_a + (to_a - from_a) * ease
                if t < 0.5 and overshoot > abs(to_a - from_a):
                    t2 = t / 0.5
                    extra = (overshoot - abs(to_a - from_a)) * math.sin(t2 * math.pi)
                    angle += extra if to_a >= from_a else -extra
                frames.append(angle)

        self._anim_frames = frames
        self._anim_idx = 0
        self._anim_timer = rumps.Timer(self._anim_tick, 0.04)
        self._anim_timer.start()

    # ── 显示更新 ──

    def _update_display(self, force_shake=False):
        monthly = self.usage_data.get("monthly")
        weekly = self.usage_data.get("weekly")
        hourly = self.usage_data.get("5h")

        candidates = []
        for entry in (hourly, weekly, monthly):
            if entry and entry["used"] is not None:
                candidates.append((entry["used"], entry))
        if candidates:
            _, best = max(candidates, key=lambda x: x[0])
            used = best["used"]
            limit = best.get("limit")
            if limit:
                pct = used / limit * 100
                self.title = f"{pct:.0f}%"
            else:
                pct = used
                self.title = f"{pct:.0f}%"
        else:
            self.title = "--"

        # 菜单项（原生控件渲染）
        for period in ("5h", "weekly", "monthly"):
            entry = self.usage_data.get(period)
            v = self._period_views.get(period)
            if not v:
                continue
            bar = v["bar"]
            detail = v["detail"]
            if entry and entry["used"] is not None:
                used = entry["used"]
                limit = entry.get("limit")
                reset = entry.get("resetInSec")
                pct = used / limit * 100 if limit else used
                v["bar"].setDoubleValue_(pct)
                v["pct"].setStringValue_(f"{pct:.0f}%")
                v["reset"].setStringValue_(_fmt_reset(reset) if reset is not None else "")
            else:
                v["bar"].setDoubleValue_(0)
                v["pct"].setStringValue_("--")
                v["reset"].setStringValue_("")

        # 瓶子图标
        candidates2 = [(e["used"], e) for e in (hourly, weekly, monthly) if e and e["used"] is not None]
        if candidates2:
            _, best = max(candidates2, key=lambda x: x[0])
            used = best["used"]
            limit = best.get("limit")
            new_pct = used / limit * 100 if limit else used

            if force_shake or self._manual_refreshing:
                self._shake_anim(new_pct)
            elif abs(new_pct - self._display_pct) > 0.5:
                old_pct = self._display_pct
                self._display_pct = new_pct
                self._start_anim(old_pct, new_pct)
            else:
                self._set_icon(new_pct, _bottle_angle(new_pct))
        else:
            self._set_icon(0, 0)

    def _shake_anim(self, pct):
        """手动刷新时的摇晃"""
        self._stop_anim()
        cur_a = _bottle_angle(pct)
        frames = []
        for i in range(10):
            t = i / 9
            decay = 1 - t * 0.75
            wobble = 5 * decay * math.sin(t * 5 * math.pi)
            frames.append(cur_a + wobble)
        self._display_pct = pct
        self._anim_frames = frames
        self._anim_idx = 0
        self._anim_timer = rumps.Timer(self._anim_tick, 0.04)
        self._anim_timer.start()

    def refresh_data(self, _):
        try:
            data = make_api_request(self.config)
            self.last_raw_data = data
            if data is None:
                self.title = "--"
                self.last_error = "响应为空"
                return

            debug_path = os.path.expanduser("~/.myocusage_latest.json")
            with open(debug_path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)

            self.usage_data = parse_usage(data)
            self.last_error = None
            self._update_display()
            log.info(f"刷新成功: {self.usage_data}")
        except AuthExpiredError:
            self.title = "🔒"
            self.menu_items["monthly"].title = "认证过期，更新 config.json cookies"
            self.last_error = "认证过期"
            log.warning("认证已过期")
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status in (401, 403):
                self.title = "🔒"
                self.menu_items["monthly"].title = "认证过期，更新 config.json cookies"
                self.last_error = f"HTTP {status}"
            else:
                self.title = "ERR"
                self.last_error = str(e)
            log.error(f"API 错误: {e}")
        except Exception as e:
            self.title = "ERR"
            self.last_error = str(e)
            log.error(f"刷新失败: {e}")

    def manual_refresh(self, _):
        self._manual_refreshing = True
        self.refresh_data(None)
        self._manual_refreshing = False

    def open_usage(self, _):
        wid = self.config.get("workspace_id", "")
        url = f"https://opencode.ai/workspace/{wid}/usage"
        subprocess.Popen(["open", url])

    def toggle_autostart(self, _):
        if _autostart_enabled():
            _uninstall_launchd()
            self.autostart_item.title = "☐ 开机自启"
        else:
            _install_launchd()
            self.autostart_item.title = "✅ 开机自启"

    def quit_app(self, _):
        self._stop_anim()
        if self.refresh_timer:
            self.refresh_timer.stop()
        if os.path.exists(ICON_FILE):
            os.unlink(ICON_FILE)
        PID_FILE = os.path.expanduser("~/.myocusage.pid")
        if os.path.exists(PID_FILE):
            os.unlink(PID_FILE)
        rumps.quit_application()


if __name__ == "__main__":
    if not os.path.exists(CONFIG_PATH):
        log.error(f"配置文件未找到: {CONFIG_PATH}")
        sys.exit(1)

    if "--install" in sys.argv:
        _install_launchd()
        sys.exit(0)
    if "--uninstall" in sys.argv:
        _uninstall_launchd()
        sys.exit(0)

    if "--daemon" not in sys.argv:
        # 防止重复启动
        PID_FILE = os.path.expanduser("~/.myocusage.pid")
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            try:
                os.kill(old_pid, 0)
                print(f"[!] MyOCUsage 已在运行 (PID {old_pid})")
                sys.exit(0)
            except (OSError, ValueError):
                os.unlink(PID_FILE)
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        with open(PID_FILE, "w") as f:
            f.write(str(proc.pid))
        log.info("守护进程已启动")
        sys.exit(0)

    MyocUsageApp().run()
