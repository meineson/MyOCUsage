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
    NSImage, NSFont, NSFontAttributeName, NSParagraphStyleAttributeName,
    NSView, NSTextField, NSProgressIndicator, NSImageView,
    NSTextAlignmentRight, NSMakeRect, NSLineBreakByClipping,
    NSProgressIndicatorBarStyle,
)
from Foundation import NSData, NSAttributedString, NSMutableAttributedString, NSMutableParagraphStyle, NSTextTab

import warnings
import threading
import CoreFoundation
warnings.filterwarnings("ignore", message=".*urllib3.*")

VERSION = "0.1.5"
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


# ── 动态瓶子图标 ─────────────────────────────────

_LIQUID_COLORS = [
    (30,  (60, 130, 220)),    # 蓝
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
    """魔法药水瓶图标 — 大肚子圆身造型"""
    S = 512

    body_w = int(S * 0.68)
    body_w = body_w if body_w % 2 == 0 else body_w + 1
    body_h = body_w
    cork_h = 20
    neck_h = 42
    
    cork_y = S - body_h - cork_h - neck_h
    neck_y = cork_y + cork_h
    body_y = neck_y + neck_h

    body_x = (S - body_w) // 2
    neck_w = int(body_w * 0.24)
    neck_x = (S - neck_w) // 2
    cork_w = neck_w + int(neck_w * 0.25)
    cork_x = (S - cork_w) // 2

    def _draw_sparkle(d, cx, cy, r, color):
        pts = []
        for i in range(8):
            a = math.pi * 2 * i / 8 - math.pi / 2
            rr = r if i % 2 == 0 else r * 0.35
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        d.polygon(pts, fill=color)

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    glass = (180, 180, 180)

    # 软木塞
    draw.rounded_rectangle([cork_x, cork_y, cork_x + cork_w, cork_y + cork_h],
                           radius=4, fill=(160, 120, 80), outline=(130, 95, 55))

    # 瓶颈
    draw.rectangle([neck_x, neck_y, neck_x + neck_w, body_y],
                   fill=(240, 240, 240), outline=glass, width=3)

    body_r = int(body_w * 0.45)
    bb = (body_x, body_y, body_x + body_w, body_y + body_h)
    draw.rounded_rectangle(bb, radius=body_r, outline=glass, width=4)

    # 瓶身填充
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
            radius=int(body_r * 0.8), fill=color)
        img = Image.alpha_composite(
            img,
            Image.composite(layer, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask),
        )
        # 魔法气泡
        if liquid_h > 30:
            bubble = ImageDraw.Draw(img)
            for bx, by in [(body_x + body_w // 3, body_y + body_h - liquid_h + 20),
                           (body_x + body_w * 2 // 3, body_y + body_h - liquid_h + 45)]:
                bubble.ellipse([bx - 3, by - 3, bx + 3, by + 3],
                               fill=(255, 255, 255, 100))

    # 高光白点
    draw = ImageDraw.Draw(img)
    hx = body_x + body_w * 0.10
    for dy in [body_h * 0.20, body_h * 0.38, body_h * 0.56]:
        r = int(body_w * 0.025)
        draw.ellipse([int(hx - r), int(body_y + dy - r),
                      int(hx + r), int(body_y + dy + r)],
                     fill=(255, 255, 255, 60))

    # 瓶底
    base_h = int(body_h * 0.04)
    draw.rounded_rectangle(
        [body_x + 6, body_y + body_h - base_h, body_x + body_w - 6, body_y + body_h],
        radius=4, fill=(200, 200, 200), outline=glass)

    # 瓶口喷发粒子
    origin_x = neck_x + neck_w // 2
    origin_y = neck_y
    for i in range(28):
        a = math.radians(-75 + (i % 7) * 25)
        d = 15 + (i // 7) * 22
        px = origin_x + int(d * math.cos(a))
        py = origin_y + int(d * math.sin(a))
        r = 2 + (i % 4)
        bright = 180 + (i % 4) * 20
        draw.ellipse([px - r, py - r, px + r, py + r],
                     fill=(255, bright + 20, bright, 200 - i * 4))
    # 大星芒喷口
    _draw_sparkle(draw, origin_x, origin_y - 6, 14, (255, 230, 100))
    _draw_sparkle(draw, origin_x, origin_y - 6, 8, (255, 255, 240))

    sparkle = (255, 235, 150)
    _draw_sparkle(draw, body_x - 15, body_y + body_h // 3, 8, sparkle)
    _draw_sparkle(draw, body_x + body_w + 15, body_y + body_h * 2 // 3, 7, sparkle)
    _draw_sparkle(draw, S // 2, cork_y - 8, 5, sparkle)

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
PIE_VIEW_H = 120
PIE_CHART_S = 80
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
    y = (ROW_H - 16) // 2
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

    title = _make_label(title_text, 8, 40)
    bar = _make_progress()
    bar.setFrame_(NSMakeRect(54, 6, PROGRESS_W, PROGRESS_H))
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

class MyocUsageApp(rumps.App):
    def __init__(self):
        super().__init__("", title="", quit_button=rumps.MenuItem("❌ 退出", callback=self.quit_app))
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
        # 标题行（可点击跳转 GitHub）
        title_item = rumps.MenuItem("", callback=self.open_update)
        ns_item = title_item._menuitem
        bold = NSFont.boldSystemFontOfSize_(13)
        reg = NSFont.systemFontOfSize_(10)
        para = NSMutableParagraphStyle.alloc().init()
        tab = NSTextTab.alloc().initWithTextAlignment_location_options_(NSTextAlignmentRight, 240, None)
        para.setTabStops_([tab])
        part1 = NSAttributedString.alloc().initWithString_attributes_(
            "MyOCUsage", {NSFontAttributeName: bold, NSParagraphStyleAttributeName: para})
        part2 = NSAttributedString.alloc().initWithString_attributes_(
            f"\tv{VERSION}", {NSFontAttributeName: reg})
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
        # 饼图菜单行
        self._model_costs = []
        self._model_total = 0.0
        self._pie_view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, ROW_W, PIE_VIEW_H))
        pie_title = NSTextField.alloc().initWithFrame_(NSMakeRect(8, PIE_VIEW_H - 18, 200, 16))
        pie_title.setBordered_(False)
        pie_title.setDrawsBackground_(False)
        pie_title.setEditable_(False)
        pie_title.setSelectable_(False)
        pie_title.setFont_(NSFont.boldSystemFontOfSize_(11))
        pie_title.setStringValue_("模型用量")
        self._pie_view.addSubview_(pie_title)
        pie_chart_y = PIE_VIEW_H - 18 - 8 - PIE_CHART_S
        self._pie_image_view = NSImageView.alloc().initWithFrame_(NSMakeRect(8, pie_chart_y, PIE_CHART_S, PIE_CHART_S))
        self._pie_image_view.setImage_(_render_pie_image([], 0))
        self._pie_view.addSubview_(self._pie_image_view)
        self._pie_legend_labels = []
        for i in range(5):
            y = pie_chart_y + 2 + i * 17
            label = NSTextField.alloc().initWithFrame_(NSMakeRect(98, y, 155, 16))
            label.setBordered_(False)
            label.setDrawsBackground_(False)
            label.setEditable_(False)
            label.setSelectable_(False)
            label.setFont_(NSFont.systemFontOfSize_(11))
            label.setStringValue_("")
            self._pie_view.addSubview_(label)
            self._pie_legend_labels.append(label)
        # Total 行
        total_y = pie_chart_y + 2 + 5 * 17
        self._pie_total_label = NSTextField.alloc().initWithFrame_(NSMakeRect(98, total_y, 155, 16))
        self._pie_total_label.setBordered_(False)
        self._pie_total_label.setDrawsBackground_(False)
        self._pie_total_label.setEditable_(False)
        self._pie_total_label.setSelectable_(False)
        self._pie_total_label.setFont_(NSFont.boldSystemFontOfSize_(11))
        self._pie_total_label.setStringValue_("")
        self._pie_view.addSubview_(self._pie_total_label)
        pie_item = rumps.MenuItem("", callback=None)
        pie_item._menuitem.setView_(self._pie_view)
        self.menu.add(pie_item)
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("📊 用量详情", callback=self.open_usage))
        self.menu.add(rumps.MenuItem("🔄 手动刷新", callback=self.manual_refresh))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("📥 自动更新", callback=self.check_update))
        title = "✅ 开机自启" if _autostart_enabled() else "🔳 开机自启"
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

        # 更新饼图
        nsimg = _render_pie_image(self._model_costs, self._model_total)
        self._pie_image_view.setImage_(nsimg)
        for i, label in enumerate(self._pie_legend_labels):
            if i < len(self._model_costs):
                model, cost = self._model_costs[i]
                label.setStringValue_(f"● {model}  ${cost:.2f}")
            else:
                label.setStringValue_("")
        if self._model_total > 0:
            self._pie_total_label.setStringValue_(f"Total: ${self._model_total:.2f}")
        else:
            self._pie_total_label.setStringValue_("暂无模型用量")

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
            # 模型用量
            try:
                self._model_costs, self._model_total = fetch_model_usage(self.config)
            except Exception as e2:
                log.warning(f"模型用量请求失败: {e2}")
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

    def open_update(self, _):
        subprocess.Popen(["open", "https://github.com/meineson/MyOCUsage"])

    def check_update(self, sender):
        sender.title = "📥 检查中..."
        t = threading.Thread(target=self._check_update_bg, args=(sender,), daemon=True)
        t.start()

    def _main_ui(self, fn):
        CoreFoundation.CFRunLoopPerformBlock(CoreFoundation.CFRunLoopGetMain(), 0, fn)
        CoreFoundation.CFRunLoopWakeUp(CoreFoundation.CFRunLoopGetMain())

    def _check_update_bg(self, sender):
        try:
            resp = requests.get(_VERSION_URL,
                                headers={"Accept": "application/vnd.github.v3+json"},
                                timeout=15)
            resp.raise_for_status()
            data = resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
        except Exception as e:
            self._main_ui(lambda: self._check_result(sender, "检查失败", str(e)[:60]))
            return
        m = re.search(r'^VERSION\s*=\s*"(.+?)"', content, re.MULTILINE)
        if not m:
            self._main_ui(lambda: self._check_result(sender, "解析失败", "无法解析版本号"))
            return
        remote_ver = m.group(1)
        current = tuple(int(x) for x in VERSION.split("."))
        remote = tuple(int(x) for x in remote_ver.split("."))
        if remote <= current:
            self._main_ui(lambda: self._check_result(sender, "已是最新", f"当前 {VERSION}", False))
            return
        def ask():
            r = rumps.alert(f"发现新版本 {remote_ver}", "是否自动更新并重启？",
                            ok="更新", cancel="取消")
            if r:
                self._apply_update(content, remote_ver, sender)
            else:
                sender.title = "📥 自动更新"
        self._main_ui(ask)

    def _check_result(self, sender, label, detail, show_alert=True):
        sender.title = f"📥 {label}"
        if show_alert:
            rumps.alert("自动更新", detail)
        else:
            rumps.notification("自动更新", label, detail)

    def _apply_update(self, content, ver, sender):
        new_path = SCRIPT_PATH + ".new"
        bak_path = SCRIPT_PATH + ".bak"
        try:
            with open(new_path, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(SCRIPT_PATH, bak_path)
            os.replace(new_path, SCRIPT_PATH)
            sender.title = f"📥 已更新 v{ver}"
            rumps.notification("自动更新", "更新完成", f"已升级到 v{ver}，即将重启")
            self._restart_app()
        except Exception as e:
            sender.title = "📥 更新失败"
            rumps.notification("自动更新", "更新失败", str(e)[:60])

    def _restart_app(self):
        self._stop_anim()
        if self.refresh_timer:
            self.refresh_timer.stop()
        pid_file = os.path.expanduser("~/.myocusage.pid")
        if os.path.exists(pid_file):
            os.unlink(pid_file)
        subprocess.Popen(
            [sys.executable, SCRIPT_PATH, "--daemon"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        rumps.quit_application()

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
