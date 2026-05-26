#!/usr/bin/env python3
"""OpenCode 月用量监控 - macOS 状态栏应用"""

import json
import os
import re
import sys
import math
import urllib.parse
import logging
from datetime import datetime, timedelta

import requests
import rumps
from PIL import Image, ImageDraw

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
PERIOD_LABELS = {"5h": "5小时", "weekly": "本周", "monthly": "本月"}


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


def progress_bar(pct, width=10):
    filled = max(0, min(width, round(pct / 100 * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


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
    """在 88x88 画布绘制瓶子 → 缩放到 44x44 → 保存到 ICON_FILE"""
    S = 88
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx = S // 2
    glass = (160, 160, 160)

    nw, nh = S // 5, S // 4
    nx, ny = cx - nw // 2, S // 16 + 2
    draw.rounded_rectangle([nx, ny, nx + nw, ny + nh], radius=3, outline=glass, width=3)

    bw, bh = S // 2, S // 2 + 4
    bx, by = cx - bw // 2, ny + nh - 3
    body_box = (bx, by, bx + bw, by + bh)
    draw.rounded_rectangle(body_box, radius=8, outline=glass, width=3)

    liquid_h = int(bh * (100 - usage_pct) / 100)
    if liquid_h > 0:
        color = _liquid_color(usage_pct)
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).rounded_rectangle(body_box, radius=8, fill=255)
        ImageDraw.Draw(mask).rectangle([bx, by + bh - liquid_h, bx + bw, by + bh], fill=255)

        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            [bx + 3, by + bh - liquid_h, bx + bw - 3, by + bh - 3],
            radius=7, fill=color,
        )
        img = Image.alpha_composite(
            img,
            Image.composite(layer, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask),
        )

    draw = ImageDraw.Draw(img)
    hx, hy = bx + 8, by + 6
    draw.rectangle([hx, hy, hx + 7, hy + bh - 12], fill=(255, 255, 255, 35))

    if angle != 0:
        img = img.rotate(angle, expand=True, fillcolor=(0, 0, 0, 0), resample=Image.BICUBIC)
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))

    img = img.resize((44, 44), Image.LANCZOS)
    img.save(ICON_FILE, "PNG")


def _apply_icon(app, pct, angle):
    _render_bottle(pct, angle)
    app.icon = ICON_FILE


# ── 菜单栏应用 ────────────────────────────────────

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

        self.menu_items = {
            "5h": rumps.MenuItem("5小时: --", callback=None),
            "weekly": rumps.MenuItem("本周: --", callback=None),
            "monthly": rumps.MenuItem("本月: --", callback=None),
        }
        self.menu.add(self.menu_items["5h"])
        self.menu.add(self.menu_items["weekly"])
        self.menu.add(self.menu_items["monthly"])
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("🔄 手动刷新", callback=self.manual_refresh))

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

    def _anim_tick(self, _):
        if self._anim_idx >= len(self._anim_frames):
            self._stop_anim()
            # Final frame — stable icon
            self._set_icon(self._display_pct, _bottle_angle(self._display_pct))
            return
        angle = self._anim_frames[self._anim_idx]
        self._set_icon(self._display_pct, angle)
        self._anim_idx += 1

    def _start_anim(self, from_pct, to_pct, n=8):
        self._stop_anim()
        from_a = _bottle_angle(from_pct)
        to_a = _bottle_angle(to_pct)
        diff_pct = abs(to_pct - from_pct)
        is_reset = diff_pct >= 30

        frames = []
        if is_reset:
            # 大量下降 → 重置摇晃动画
            for i in range(n):
                t = i / max(n - 1, 1)
                decay = 1 - t * 0.8
                wobble = 6 * decay * math.sin(t * 5 * math.pi)
                frames.append(to_a + wobble)
        else:
            # 小幅升降 → 过冲回弹
            for i in range(n):
                t = i / max(n - 1, 1)
                c1, c3 = 1.70158, 2.70158
                ease = 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2
                angle = from_a + (to_a - from_a) * ease
                frames.append(angle)

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
            if limit:
                pct = used / limit * 100
                self.title = f"{pct:.0f}%"
            else:
                pct = used
                self.title = f"{pct:.0f}%"
        else:
            self.title = "--"

        # 菜单项
        for period in ("5h", "weekly", "monthly"):
            entry = self.usage_data.get(period)
            item = self.menu_items[period]
            label = PERIOD_LABELS.get(period, period)
            if entry and entry["used"] is not None:
                used = entry["used"]
                limit = entry.get("limit")
                reset = entry.get("resetInSec")
                pct_str = f"{used:.0f}%".rjust(4)
                bar = progress_bar(used, 10)
                reset_str = _fmt_reset(reset) if reset is not None else ""
                item.title = f"{label}:  {pct_str}  {bar}  {reset_str}"
            else:
                item.title = f"{label}:  --"

        # 瓶子图标
        candidates2 = [(e["used"], e) for e in (hourly, weekly, monthly) if e and e["used"] is not None]
        if candidates2:
            _, best = max(candidates2, key=lambda x: x[0])
            used = best["used"]
            limit = best.get("limit")
            new_pct = used / limit * 100 if limit else used

            if abs(new_pct - self._display_pct) > 0.5:
                old_pct = self._display_pct
                self._display_pct = new_pct
                self._start_anim(old_pct, new_pct)
            else:
                self._set_icon(new_pct, _bottle_angle(new_pct))
        else:
            self._set_icon(0, 0)

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
        self.title = "..."
        self.refresh_data(None)

    def quit_app(self, _):
        self._stop_anim()
        if self.refresh_timer:
            self.refresh_timer.stop()
        if os.path.exists(ICON_FILE):
            os.unlink(ICON_FILE)
        rumps.quit_application()


if __name__ == "__main__":
    if not os.path.exists(CONFIG_PATH):
        log.error(f"配置文件未找到: {CONFIG_PATH}")
        sys.exit(1)

    if "--daemon" not in sys.argv:
        import subprocess
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("守护进程已启动")
        sys.exit(0)

    MyocUsageApp().run()
