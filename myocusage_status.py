#!/usr/bin/env python3
"""OpenCode 月用量监控 - macOS 状态栏应用"""

import json
import os
import re
import sys
import math
import base64
import plistlib
import subprocess
import urllib.parse
import logging
from datetime import datetime, timedelta

import requests
import rumps
from PIL import Image, ImageDraw
from AppKit import (
    NSImage, NSFont, NSFontAttributeName, NSColor, NSForegroundColorAttributeName,
    NSParagraphStyleAttributeName, NSApplication, NSAlert,
    NSView, NSTextField, NSProgressIndicator, NSImageView,
    NSTextAlignmentRight, NSMakeRect, NSLineBreakByClipping,
    NSProgressIndicatorBarStyle,
    NSAlertFirstButtonReturn,
    NSPanel, NSBorderlessWindowMask, NSFloatingWindowLevel,
    NSBackingStoreBuffered, NSTitledWindowMask, NSClosableWindowMask,
    NSMiniaturizableWindowMask,
    NSButton, NSRoundedBezelStyle,
)
from Foundation import (
    NSData, NSAttributedString, NSMutableAttributedString,
    NSMutableParagraphStyle, NSTextTab, NSObject,
)

import warnings
import threading
warnings.filterwarnings("ignore", message=".*urllib3.*")

VERSION = "0.2.0"
_VERSION_URL = "https://api.github.com/repos/meineson/MyOCUsage/contents/myocusage_status.py"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.abspath(__file__)
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
            used = None
            for k in ("cost", "amount", "usagePercent", "usage"):
                v = entry.get(k)
                if v is not None:
                    used = v
                    break
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


def fetch_model_usage(config):
    now = datetime.now()
    tz = config.get("timezone", "+08:00")
    server_id = config.get("model_server_id", config["server_id"])
    server_instance = config.get("model_server_instance", config.get("server_instance", "server-fn:0"))
    args = {
        "t": {"t": 9, "i": 0, "l": 4, "a": [
            {"t": 1, "s": config["workspace_id"]},
            {"t": 0, "s": now.year},
            {"t": 0, "s": now.month - 1},
            {"t": 1, "s": tz},
        ], "o": 0},
        "f": 31, "m": [],
    }
    encoded_args = urllib.parse.quote(json.dumps(args, separators=(",", ":")))
    url = f"https://opencode.ai/_server?id={server_id}&args={encoded_args}"
    headers = {
        "accept": "*/*", "accept-language": "zh-CN,zh;q=0.9",
        "cookie": config["cookies"],
        "referer": f"https://opencode.ai/workspace/{config['workspace_id']}/usage",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "x-server-id": server_id,
        "x-server-instance": server_instance,
        "sec-fetch-dest": "empty", "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    raw = resp.text
    content_type = resp.headers.get("content-type", "")
    data = _parse_convex_response(raw) if ("javascript" in content_type or raw.startswith(";")) else resp.json()
    if isinstance(data, dict) and "value" in data:
        data = data["value"]
    # 尝试提取 model 明细数据
    usage_list = None
    if isinstance(data, dict):
        usage_list = data.get("usage")
    if usage_list is None and isinstance(data, list):
        usage_list = data
    if usage_list and isinstance(usage_list, list):
        model_totals = {}
        for entry in usage_list:
            if isinstance(entry, dict) and "model" in entry and "totalCost" in entry:
                m = entry["model"]
                model_totals[m] = model_totals.get(m, 0) + entry["totalCost"]
        if model_totals:
            total_raw = sum(model_totals.values())
            cost_list = [(m, c / 100_000_000) for m, c in sorted(model_totals.items(), key=lambda x: -x[1])]
            return cost_list, total_raw / 100_000_000
    log.info("当前 server-fn 不返回模型明细，如需模型用量请更新 config.json 中 server_id / server_instance")
    return [], 0.0


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
        subprocess.run(["launchctl", "unload", PLIST_PATH], capture_output=True)
        os.unlink(PLIST_PATH)
        log.info("开机自启已关闭")


def _autostart_enabled():
    return os.path.exists(PLIST_PATH)


# ── 动态瓶杯图标（🍶emoji底图 + 液位着色）───────────

_LIQUID_COLORS = [
    (30,  (229, 57, 53)),     # 红：剩余≤30%
    (60,  (245, 166, 35)),    # 橙：剩余≤60%
    (100, (60, 130, 220)),    # 蓝：剩余≤100%
]

_EMOJI_CACHE = os.path.join(SCRIPT_DIR, "icon.png")


def _liquid_color(pct):
    for threshold, color in _LIQUID_COLORS:
        if pct <= threshold:
            return color
    return _LIQUID_COLORS[-1][1]


def _tint_emoji(base_arr, pct, col, y_start, y_end):
    """用 numpy 对指定行范围着色（原图 60% + 液体色 40%）"""
    if pct <= 0 or y_start >= y_end:
        return
    import numpy as np
    h, w = base_arr.shape[:2]
    y_start = max(0, min(h, y_start))
    y_end = max(0, min(h, y_end))
    region = base_arr[y_start:y_end]
    opaque = region[:, :, 3] > 30
    blend = 0.4
    col_f = col[:3]
    region[opaque, 0] = (region[opaque, 0] * (1 - blend) + col_f[0] * blend).astype(np.uint8)
    region[opaque, 1] = (region[opaque, 1] * (1 - blend) + col_f[1] * blend).astype(np.uint8)
    region[opaque, 2] = (region[opaque, 2] * (1 - blend) + col_f[2] * blend).astype(np.uint8)


def _render_icon(weekly_rem, fiveh_rem, angle=0):
    """基于 emoji 底图 + 液位着色"""
    import numpy as np
    try:
        emoji = Image.open(_EMOJI_CACHE).convert("RGBA")
    except Exception:
        emoji = Image.new("RGBA", (88, 88), (0, 0, 0, 0))
    emoji = emoji.resize((88, 88), Image.LANCZOS)

    arr = np.array(emoji, dtype=np.uint8)

    # 瓶身区域（emoji 右半部分，大约 x:40-88, y:0-80）
    # 杯子区域（emoji 左半部分，大约 x:0-40, y:30-80）
    if weekly_rem > 0:
        col = _liquid_color(weekly_rem)
        bottle_top = int(88 * 0.15)
        bottle_bot = int(88 * 0.85)
        liq_h = int((bottle_bot - bottle_top) * weekly_rem / 100)
        _tint_emoji(arr, weekly_rem, col, bottle_bot - liq_h, bottle_bot)

    if fiveh_rem > 0:
        col = _liquid_color(fiveh_rem)
        cup_top = int(88 * 0.40)
        cup_bot = int(88 * 0.88)
        liq_h = int((cup_bot - cup_top) * fiveh_rem / 100)
        _tint_emoji(arr, fiveh_rem, col, cup_bot - liq_h, cup_bot)

    img = Image.fromarray(arr)

    if angle != 0:
        img = img.rotate(angle, expand=True, fillcolor=(0, 0, 0, 0), resample=Image.BICUBIC)
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))

    img.save(ICON_FILE, "PNG")


def _apply_icon(app, weekly_rem, fiveh_rem, angle):
    _render_icon(weekly_rem, fiveh_rem, angle)
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
ROW_H = 26
PROGRESS_W = 85
PROGRESS_H = 12
PIE_VIEW_H = 120
PIE_CHART_S = 80
MAX_MODELS = 6
_PIE_COLORS = [
    (60, 130, 220),   # 蓝
    (245, 155, 35),   # 橙
    (80, 180, 80),    # 绿
    (160, 80, 200),   # 紫
    (230, 80, 80),    # 红
    (80, 190, 190),   # 青
]


def _make_label(text, x, w, align_right=False, bold=False):
    """创建 NSTextField 标签"""
    y = (ROW_H - 16) // 2 + 1
    f = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, 16))
    f.setStringValue_(text)
    f.setBordered_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    f.setFont_(NSFont.boldSystemFontOfSize_(11) if bold else NSFont.systemFontOfSize_(11))
    if align_right:
        f.setAlignment_(NSTextAlignmentRight)
    f.setLineBreakMode_(NSLineBreakByClipping)
    return f


def _make_progress():
    """创建原生进度条"""
    y = (ROW_H - PROGRESS_H) // 2
    p = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(0, y, PROGRESS_W, PROGRESS_H))
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

    title = _make_label(title_text, 8, 40)
    bar = _make_progress()
    bar.setFrame_(NSMakeRect(54, (ROW_H - PROGRESS_H) // 2, PROGRESS_W, PROGRESS_H))
    pct_label = _make_label("", 143, 28, align_right=True, bold=True)
    reset_label = _make_label("", 174, 82, align_right=True)

    view.addSubview_(title)
    view.addSubview_(bar)
    view.addSubview_(pct_label)
    view.addSubview_(reset_label)

    return view, title, bar, pct_label, reset_label


_MODEL_ICONS = ["●", "●", "●", "●", "●", "●"]


def _render_pie_image(cost_list, total):
    from io import BytesIO
    s = PIE_CHART_S
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if total <= 0 or not cost_list:
        draw.ellipse([3, 3, s - 3, s - 3], fill=(230, 230, 230), outline=(180, 180, 180))
    else:
        start = 0
        for i, (_, cost) in enumerate(cost_list):
            pct = cost / total
            end = start + pct * 360
            color = _PIE_COLORS[i % len(_PIE_COLORS)]
            draw.pieslice([3, 3, s - 3, s - 3], start, end, fill=color, outline=(255, 255, 255, 200))
            start = end
    buf = BytesIO()
    img.save(buf, format="PNG")
    data = NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue()))
    return NSImage.alloc().initWithData_(data)


# ── 浮动窗口 ──────────────────────────────────────────

FLOAT_W = 220
FLOAT_H = 100
FLOAT_ROW_H = 26
FLOAT_BAR_W = 100
FLOAT_BAR_H = 10


class FloatingWindow:
    def __init__(self):
        style = NSTitledWindowMask | 32768  # NSFullSizeContentViewWindowMask
        from AppKit import NSScreen
        screen = NSScreen.mainScreen().frame()
        cx = (screen.size.width - FLOAT_W) / 2
        cy = (screen.size.height - FLOAT_H) / 2
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(cx, cy, FLOAT_W, FLOAT_H),
            style,
            NSBackingStoreBuffered,
            False
        )
        self.panel.setLevel_(NSFloatingWindowLevel)
        self.panel.setOpaque_(True)
        self.panel.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.15, 0.15, 0.15, 0.95))
        self.panel.setHasShadow_(True)
        self.panel.setTitlebarAppearsTransparent_(True)
        self.panel.setTitleVisibility_(1)  # NSWindowTitleHidden
        self.panel.setMovableByWindowBackground_(True)
        self.panel.setBecomesKeyOnlyIfNeeded_(False)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setCollectionBehavior_(2)

        content = self.panel.contentView()
        content.setWantsLayer_(True)

        title_label = NSTextField.alloc().initWithFrame_(NSMakeRect(10, FLOAT_H - 20, 200, 16))
        title_label.setBordered_(False)
        title_label.setDrawsBackground_(False)
        title_label.setEditable_(False)
        title_label.setSelectable_(False)
        title_label.setFont_(NSFont.boldSystemFontOfSize_(11))
        title_label.setTextColor_(NSColor.whiteColor())
        title_label.setStringValue_("MyOCUsage")
        content.addSubview_(title_label)

        # 关闭按钮
        close_btn = NSButton.alloc().initWithFrame_(NSMakeRect(FLOAT_W - 22, FLOAT_H - 22, 16, 16))
        close_btn.setBezelStyle_(NSRoundedBezelStyle)
        close_btn.setBordered_(False)
        close_btn.setTitle_("✕")
        close_btn.setFont_(NSFont.boldSystemFontOfSize_(11))
        close_btn.setTarget_(self.panel)
        close_btn.setAction_("orderOut:")
        attr = NSAttributedString.alloc().initWithString_attributes_(
            "✕", {NSForegroundColorAttributeName: NSColor.grayColor(),
                  NSFontAttributeName: NSFont.boldSystemFontOfSize_(11)})
        close_btn.setAttributedTitle_(attr)
        content.addSubview_(close_btn)

        self._bars = {}
        labels = [("5h", "5小时"), ("weekly", "本周"), ("monthly", "本月")]
        for i, (key, label_text) in enumerate(labels):
            y = FLOAT_H - 46 - i * 22

            label = NSTextField.alloc().initWithFrame_(NSMakeRect(8, y, 44, 16))
            label.setBordered_(False)
            label.setDrawsBackground_(False)
            label.setEditable_(False)
            label.setSelectable_(False)
            label.setFont_(NSFont.boldSystemFontOfSize_(11))
            label.setTextColor_(NSColor.whiteColor())
            label.setStringValue_(label_text)
            content.addSubview_(label)

            bar = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(
                56, y + 2, FLOAT_BAR_W, FLOAT_BAR_H))
            bar.setStyle_(NSProgressIndicatorBarStyle)
            bar.setIndeterminate_(False)
            bar.setMinValue_(0)
            bar.setMaxValue_(100)
            bar.setDoubleValue_(0)
            bar.setDisplayedWhenStopped_(True)
            content.addSubview_(bar)

            pct = NSTextField.alloc().initWithFrame_(NSMakeRect(162, y, 50, 16))
            pct.setBordered_(False)
            pct.setDrawsBackground_(False)
            pct.setEditable_(False)
            pct.setSelectable_(False)
            pct.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(11, 0.4))
            pct.setTextColor_(NSColor.whiteColor())
            pct.setAlignment_(NSTextAlignmentRight)
            pct.setStringValue_("--")
            content.addSubview_(pct)

            self._bars[key] = {"bar": bar, "pct": pct}

    def show(self):
        self.panel.orderFrontRegardless()

    def hide(self):
        self.panel.orderOut_(None)

    def isVisible(self):
        return self.panel.isVisible()

    def toggle(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()

    def update(self, usage_data):
        for period, views in self._bars.items():
            entry = usage_data.get(period)
            bar = views["bar"]
            pct = views["pct"]
            if entry and entry["used"] is not None:
                used = entry["used"]
                limit = entry.get("limit")
                p = used / limit * 100 if limit else used
                bar.setDoubleValue_(p)
                pct.setStringValue_(f"{p:.0f}%")
            else:
                bar.setDoubleValue_(0)
                pct.setStringValue_("--")


class MyocUsageApp(rumps.App):
    def __init__(self):
        super().__init__("", title="", quit_button=rumps.MenuItem("❌ 退出", callback=self.quit_app))
        self.config = load_config()
        self.usage_data = {}
        self.last_error = None
        self.last_raw_data = None
        self.refresh_timer = None

        self._display_weekly = 0
        self._display_fiveh = 0
        self._anim_timer = None
        self._anim_frames = []
        self._anim_idx = 0
        self._update_timer = None
        self._float_sync_timer = None
        self._pending_restart = False
        self._refresh_state = None
        self._refresh_poll_timer = None
        self._bg_usage = {}
        self._bg_model_costs = []
        self._bg_model_total = 0.0
        self._bg_error = None
        self._floating = None

        self._period_views = {}
        self.menu_items = {}
        # 标题行（可点击跳转 GitHub）
        title_item = rumps.MenuItem("", callback=self.open_update)
        ns_item = title_item._menuitem
        bold = NSFont.boldSystemFontOfSize_(13)
        reg = NSFont.systemFontOfSize_(10)
        para = NSMutableParagraphStyle.alloc().init()
        tab = NSTextTab.alloc().initWithTextAlignment_location_options_(NSTextAlignmentRight, 240, {})
        para.setTabStops_([tab])
        part1 = NSAttributedString.alloc().initWithString_attributes_(
            "MyOCUsage", {NSFontAttributeName: bold, NSParagraphStyleAttributeName: para})
        part2 = NSAttributedString.alloc().initWithString_attributes_(
            f"\tv{VERSION}", {NSFontAttributeName: reg, NSForegroundColorAttributeName: NSColor.grayColor()})
        attr = NSMutableAttributedString.alloc().init()
        attr.appendAttributedString_(part1)
        attr.appendAttributedString_(part2)
        ns_item.setAttributedTitle_(attr)
        self.menu.add(title_item)
        self.menu.add(rumps.separator)
        for period, p_label in [("5h", "5小时"), ("weekly", "本周"), ("monthly", "本月")]:
            view, title, bar, pct_label, reset_label = _create_row_view(p_label)
            item = rumps.MenuItem("", callback=None)
            item._menuitem.setView_(view)
            self.menu.add(item)
            self.menu_items[period] = item
            self._period_views[period] = {"title": title, "bar": bar, "pct": pct_label, "reset": reset_label}
        self.menu.add(rumps.separator)
        self.error_item = rumps.MenuItem("", callback=None)
        self.error_item.hide()
        self.menu.add(self.error_item)
        # 饼图菜单行
        self._model_costs = []
        self._model_total = 0.0
        self._pie_view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, ROW_W, PIE_VIEW_H))
        pie_title = NSTextField.alloc().initWithFrame_(NSMakeRect(8, PIE_VIEW_H - 18, 244, 16))
        pie_title.setBordered_(False)
        pie_title.setDrawsBackground_(False)
        pie_title.setEditable_(False)
        pie_title.setSelectable_(False)
        pie_title.setFont_(NSFont.boldSystemFontOfSize_(11))
        pie_title.setStringValue_("当月模型用量")
        self._pie_title = pie_title
        self._pie_view.addSubview_(pie_title)
        pie_chart_y = PIE_VIEW_H - 18 - 8 - PIE_CHART_S
        self._pie_image_view = NSImageView.alloc().initWithFrame_(NSMakeRect(8, pie_chart_y, PIE_CHART_S, PIE_CHART_S))
        self._pie_image_view.setImage_(_render_pie_image([], 0))
        self._pie_view.addSubview_(self._pie_image_view)
        self._pie_legend_labels = []
        for i in range(MAX_MODELS):
            y = pie_chart_y + 2 + i * 12
            label = NSTextField.alloc().initWithFrame_(NSMakeRect(96, y, 157, 16))
            label.setBordered_(False)
            label.setDrawsBackground_(False)
            label.setEditable_(False)
            label.setSelectable_(False)
            label.setFont_(NSFont.systemFontOfSize_(11))
            label.setAlignment_(NSTextAlignmentRight)
            label.setStringValue_("")
            self._pie_view.addSubview_(label)
            self._pie_legend_labels.append(label)
        pie_item = rumps.MenuItem("", callback=None)
        pie_item._menuitem.setView_(self._pie_view)
        self.menu.add(pie_item)
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("📊 用量详情", callback=self.open_usage))
        self.refresh_item = rumps.MenuItem("🔄 手动刷新", callback=self.manual_refresh)
        self.menu.add(self.refresh_item)
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("📥 检查更新", callback=self.check_update))
        title = "✅ 开机自启" if _autostart_enabled() else "🔳 开机自启"
        self.autostart_item = rumps.MenuItem(title, callback=self.toggle_autostart)
        self.menu.add(self.autostart_item)
        self.float_item = rumps.MenuItem("🔼 显示浮动窗", callback=self.toggle_floating)
        self.menu.add(self.float_item)

        self.refresh_data(None)
        interval = self.config.get("refresh_interval", 300)
        self.refresh_timer = rumps.Timer(self.refresh_data, interval)
        self.refresh_timer.start()
        self._float_sync_timer = rumps.Timer(self._sync_float_menu, 0.5)
        self._float_sync_timer.start()
        log.info("MyocUsage 已启动")

    def _set_ns_title(self, text, emoji="", font_size=11):
        rumps.App.title.fset(self, text + emoji)
        if emoji:
            attr = NSMutableAttributedString.alloc().init()
            text_part = NSAttributedString.alloc().initWithString_attributes_(
                text, {NSFontAttributeName: NSFont.menuBarFontOfSize_(font_size)})
            emoji_part = NSAttributedString.alloc().initWithString_attributes_(
                emoji, {NSFontAttributeName: NSFont.menuBarFontOfSize_(font_size // 2)})
            attr.appendAttributedString_(text_part)
            attr.appendAttributedString_(emoji_part)
            btn = getattr(self, '_ns_status_bar_button', None)
            if btn:
                btn.setAttributedTitle_(attr)

    # ── 图标动画 ──

    def _set_icon(self, weekly_rem, fiveh_rem, angle):
        _apply_icon(self, weekly_rem, fiveh_rem, angle)

    def _stop_anim(self):
        if self._anim_timer:
            self._anim_timer.stop()
        self._anim_timer = None
        self._anim_frames = []
        self._anim_idx = 0
        if self._refresh_poll_timer:
            self._refresh_poll_timer.stop()
            self._refresh_poll_timer = None
        self._refresh_state = None

    def _anim_tick(self, _):
        if self._anim_idx >= len(self._anim_frames):
            self._stop_anim()
            # Final frame — stable icon
            self._set_icon(self._display_weekly, self._display_fiveh, 0)
            return
        angle = self._anim_frames[self._anim_idx]
        self._set_icon(self._display_weekly, self._display_fiveh, angle)
        self._anim_idx += 1

    def _start_anim(self, weekly_rem, fiveh_rem):
        self._stop_anim()
        self._display_weekly = weekly_rem
        self._display_fiveh = fiveh_rem
        frames = []
        for i in range(8):
            t = i / 7
            decay = 1 - t * 0.75
            wobble = 6 * decay * math.sin(t * 5 * math.pi)
            frames.append(wobble)
        frames.append(0)
        self._anim_frames = frames
        self._anim_idx = 0
        self._anim_timer = rumps.Timer(self._anim_tick, 0.04)
        self._anim_timer.start()

    # ── 显示更新 ──

    def _update_display(self):
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
            best_pct = used / limit * 100 if limit else used
            hourly_pct_val = 0
            emoji = "😊"
            if hourly and hourly["used"] is not None:
                h_used = hourly["used"]
                h_limit = hourly.get("limit")
                hourly_pct_val = h_used / h_limit * 100 if h_limit else h_used
                reset = hourly.get("resetInSec")
                if reset is not None:
                    elapsed_ratio = 1 - reset / 18000
                    if elapsed_ratio > 0:
                        step_ratio = hourly_pct_val / (elapsed_ratio * 100) if elapsed_ratio > 0 else 0
                        if step_ratio < 0.85:
                            emoji = "😊"
                        elif step_ratio <= 1.30:
                            emoji = "😐"
                        else:
                            emoji = "😰"
            self._set_ns_title(f"{best_pct:.0f}%", emoji)
        else:
            self._set_ns_title("--")

        # 菜单项（原生控件渲染）
        for period in ("5h", "weekly", "monthly"):
            entry = self.usage_data.get(period)
            v = self._period_views.get(period)
            if not v:
                continue
            bar = v["bar"]
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

        # 更新浮动窗口
        if self._floating and self._floating.isVisible():
            self._floating.update(self.usage_data)

        # 瓶子+小杯图标（德利=本周剩余，小杯=5h剩余）
        w_entry = self.usage_data.get("weekly")
        f_entry = self.usage_data.get("5h")
        if w_entry and w_entry["used"] is not None:
            w_pct = w_entry["used"] / w_entry.get("limit") * 100 if w_entry.get("limit") else w_entry["used"]
            weekly_rem = max(0, 100 - w_pct)
        else:
            weekly_rem = 100
        if f_entry and f_entry["used"] is not None:
            f_pct = f_entry["used"] / f_entry.get("limit") * 100 if f_entry.get("limit") else f_entry["used"]
            fiveh_rem = max(0, 100 - f_pct)
        else:
            fiveh_rem = 100
        self._start_anim(weekly_rem, fiveh_rem)

        # 更新饼图
        if len(self._model_costs) > MAX_MODELS:
            display_costs = self._model_costs[:MAX_MODELS - 1]
            other_cost = sum(c for _, c in self._model_costs[MAX_MODELS - 1:])
            display_costs = display_costs + [("其他", other_cost)]
        else:
            display_costs = self._model_costs
        nsimg = _render_pie_image(display_costs, self._model_total)
        self._pie_image_view.setImage_(nsimg)
        for i, label in enumerate(self._pie_legend_labels):
            if i < len(display_costs):
                model, cost = display_costs[i]
                color = _PIE_COLORS[i % len(_PIE_COLORS)]
                c = NSColor.colorWithRed_green_blue_alpha_(color[0]/255, color[1]/255, color[2]/255, 1)
                attr = NSMutableAttributedString.alloc().init()
                dot = NSAttributedString.alloc().initWithString_attributes_(
                    "● ", {NSForegroundColorAttributeName: c})
                rest = NSAttributedString.alloc().initWithString_attributes_(
                    f"{model}  ${cost:.2f}", {NSForegroundColorAttributeName: NSColor.labelColor()})
                attr.appendAttributedString_(dot)
                attr.appendAttributedString_(rest)
                label.setAttributedStringValue_(attr)
            else:
                label.setStringValue_("")
        if self._model_total > 0:
            attr = NSMutableAttributedString.alloc().init()
            title_part = NSAttributedString.alloc().initWithString_attributes_(
                "当月模型用量 ", {NSFontAttributeName: NSFont.boldSystemFontOfSize_(11), NSForegroundColorAttributeName: NSColor.labelColor()})
            total_part = NSAttributedString.alloc().initWithString_attributes_(
                f"Total: ${self._model_total:.2f}", {NSForegroundColorAttributeName: NSColor.secondaryLabelColor(), NSFontAttributeName: NSFont.systemFontOfSize_(10)})
            attr.appendAttributedString_(title_part)
            attr.appendAttributedString_(total_part)
            self._pie_title.setAttributedStringValue_(attr)
        else:
            self._pie_title.setStringValue_("当月模型用量")



    def refresh_data(self, _):
        if self._refresh_state == "busy":
            return
        self._refresh_state = "busy"
        self._bg_usage = {}
        self._bg_model_costs = []
        self._bg_model_total = 0.0
        self._bg_error = None
        t = threading.Thread(target=self._refresh_bg, daemon=True)
        t.start()
        self._refresh_poll_timer = rumps.Timer(self._refresh_poll, 0.1)
        self._refresh_poll_timer.start()

    def _refresh_bg(self):
        try:
            data = make_api_request(self.config)
            self.last_raw_data = data
            if data is None:
                self._bg_error = "响应为空"
                self._refresh_state = "done"
                return

            debug_path = os.path.expanduser("~/.myocusage_latest.json")
            with open(debug_path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)

            self._bg_usage = parse_usage(data)
            try:
                new_costs, new_total = fetch_model_usage(self.config)
                if new_costs or new_total > 0:
                    self._bg_model_costs, self._bg_model_total = new_costs, new_total
            except Exception as e2:
                log.warning(f"模型用量请求失败: {e2}")
                self._bg_model_costs = self._model_costs
                self._bg_model_total = self._model_total
            self._bg_error = None
        except AuthExpiredError:
            self._bg_error = "auth_expired"
            log.warning("认证已过期")
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            self._bg_error = f"http_{status}"
            log.error(f"API 错误: {e}")
        except Exception as e:
            self._bg_error = str(e)
            log.error(f"刷新失败: {e}")
        self._refresh_state = "done"

    def _refresh_poll(self, _):
        if self._refresh_state != "done":
            return
        if self._refresh_poll_timer:
            self._refresh_poll_timer.stop()
        if self._bg_error == "auth_expired":
            self._set_ns_title("🔒")
            self.menu_items["monthly"].title = "认证过期，更新 config.json cookies"
            self.last_error = "认证过期"
            self.error_item.title = "⚠️ 认证已过期，请更新 cookies"
            self.error_item.show()
        elif self._bg_error and str(self._bg_error).startswith("http_"):
            status = self._bg_error.split("_")[1]
            if status in ("401", "403"):
                self._set_ns_title("🔒")
                self.menu_items["monthly"].title = "认证过期，更新 config.json cookies"
                self.last_error = f"HTTP {status}"
                self.error_item.title = f"⚠️ 认证失败 (HTTP {status})"
                self.error_item.show()
            else:
                self._set_ns_title("ERR")
                self.last_error = f"HTTP {status}"
                self.error_item.title = f"⚠️ 请求错误: HTTP {status}"
                self.error_item.show()
        elif self._bg_error:
            self._set_ns_title("ERR")
            self.last_error = str(self._bg_error)
            self.error_item.title = f"⚠️ {str(self._bg_error)[:40]}"
            self.error_item.show()
        else:
            self.usage_data = self._bg_usage
            self._model_costs = self._bg_model_costs if self._bg_model_costs else self._model_costs
            self._model_total = self._bg_model_total if self._bg_model_total > 0 else self._model_total
            self.last_error = None
            self.error_item.hide()
            self._update_display()
            log.info(f"刷新成功: {self.usage_data}")
        self.refresh_item.title = "🔄 手动刷新"
        self._refresh_state = None

    def manual_refresh(self, _):
        if self._refresh_state == "busy":
            return
        self.refresh_item.title = "🔄 刷新中..."
        self.refresh_data(None)

    def open_update(self, _):
        subprocess.Popen(["open", "https://github.com/meineson/MyOCUsage"])

    def check_update(self, sender):
        if self._pending_restart:
            sender.title = "📥 重启中..."
            self._restart_app()
            return
        sender.title = "📥 检查中..."
        self._update_sender = sender
        self._update_pending = True
        self._update_error = None
        self._update_remote_ver = None
        self._update_content = None
        t = threading.Thread(target=self._check_update_bg, daemon=True)
        t.start()
        self._update_timer = rumps.Timer(lambda _: self._check_update_tick(sender), 0.5)
        self._update_timer.start()

    def _check_update_bg(self):
        try:
            resp = requests.get(_VERSION_URL,
                                headers={"Accept": "application/vnd.github.v3+json"},
                                timeout=15)
            resp.raise_for_status()
            data = resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
        except Exception as e:
            self._update_pending = False
            self._update_error = str(e)
            return
        m = re.search(r'^VERSION\s*=\s*"(.+?)"', content, re.MULTILINE)
        if not m:
            self._update_pending = False
            self._update_error = "无法解析版本号"
            return
        self._update_remote_ver = m.group(1)
        self._update_content = content
        self._update_pending = False
        self._update_error = None

    def _check_update_tick(self, sender):
        if self._update_pending:
            return
        self._update_timer.stop()
        try:
            del self._update_timer
        except AttributeError:
            pass
        if self._update_error:
            sender.title = "📥 检查失败"
            rumps.notification("自动更新", "检查失败", self._update_error[:60])
            return
        remote_ver = self._update_remote_ver
        content = self._update_content
        current = tuple(int(x) for x in VERSION.split("."))
        remote = tuple(int(x) for x in remote_ver.split("."))
        if remote <= current:
            sender.title = f"📥 已是最新 v{VERSION}"
            rumps.notification("自动更新", "已是最新", f"当前版本 {VERSION}")
            return
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_(f"发现新版本 {remote_ver}")
        alert.setInformativeText_("自动下载，更新后手动点击菜单重启生效")
        alert.addButtonWithTitle_("下载")
        alert.addButtonWithTitle_("取消")
        r = alert.runModal()
        if r != NSAlertFirstButtonReturn:
            sender.title = "📥 检查更新"
            return
        new_path = SCRIPT_PATH + ".new"
        bak_path = SCRIPT_PATH + ".bak"
        try:
            with open(new_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(SCRIPT_PATH, bak_path)
            os.replace(new_path, SCRIPT_PATH)
        except Exception as e:
            sender.title = "📥 更新失败"
            rumps.notification("自动更新", "更新失败", f"{e}")
            return
        sender.title = "📥 已下载，点击重启"
        self._pending_restart = True
        rumps.notification("自动更新", "更新完成", f"已下载 v{remote_ver}，点击菜单「📥 已下载，点击重启」重启")

    def _restart_app(self):
        self._pending_restart = False
        self._stop_anim()
        if self.refresh_timer:
            self.refresh_timer.stop()
        pid_file = os.path.expanduser("~/.myocusage.pid")
        if os.path.exists(pid_file):
            os.unlink(pid_file)
        subprocess.Popen(
            ["/bin/sh", "-c",
             f"sleep 1 && '{sys.executable}' '{SCRIPT_PATH}' --daemon &"],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os._exit(0)

    def open_usage(self, _):
        wid = self.config.get("workspace_id", "")
        url = f"https://opencode.ai/workspace/{wid}/usage"
        subprocess.Popen(["open", url])

    def toggle_autostart(self, _):
        if _autostart_enabled():
            _uninstall_launchd()
            self.autostart_item.title = "🔳 开机自启"
        else:
            _install_launchd()
            self.autostart_item.title = "✅ 开机自启"

    def toggle_floating(self, _):
        if not self._floating:
            try:
                self._floating = FloatingWindow()
            except Exception as e:
                log.error(f"浮动窗口创建失败: {e}")
                return
        self._floating.toggle()
        if self._floating.isVisible():
            self._floating.update(self.usage_data)
            self.float_item.title = "🔽 隐藏浮动窗"
        else:
            self.float_item.title = "🔼 显示浮动窗"

    def _sync_float_menu(self, _=None):
        if self._floating and not self._floating.isVisible():
            self.float_item.title = "🔼 显示浮动窗"

    def quit_app(self, _):
        self._stop_anim()
        if self.refresh_timer:
            self.refresh_timer.stop()
        if self._float_sync_timer:
            self._float_sync_timer.stop()
        if self._floating:
            self._floating.hide()
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
