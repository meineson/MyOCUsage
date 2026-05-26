#!/usr/bin/env python3
"""OpenCode 月用量监控 - macOS 状态栏应用"""

import json
import os
import re
import sys
import urllib.parse
import logging
from datetime import datetime, timedelta

import requests
import rumps

import warnings
warnings.filterwarnings("ignore", message=".*urllib3.*")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
ICON_PATH = os.path.join(SCRIPT_DIR, "bottle_icon_small.png")

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
    """将 Convex 返回的 JS 表达式转换为 JSON"""
    s = js_str
    # !0 → true, !1 → false
    s = re.sub(r'!0(?=[,}:])', 'true', s)
    s = re.sub(r'!1(?=[,}:])', 'false', s)
    # 给属性名加引号: word: → "word":
    s = re.sub(r'([a-zA-Z_$]\w*)(?=\s*:)', r'"\1"', s)
    # 去掉 $R[N]= 引用定义
    s = re.sub(r'\$R\[\d+\]=', '', s)
    # 去掉残留的裸 $R[N]
    s = re.sub(r'\$R\[\d+\]', 'null', s)
    return json.loads(s)


def _parse_convex_response(raw):
    if "location" in raw and ("/auth/authorize" in raw or "/login" in raw):
        raise AuthExpiredError("认证已过期，请更新 config.json 中 cookies 字段")

    # 提取 $R[0]= 之后的 JS 表达式
    m = re.search(r'\$R\[0\]=(.+)', raw)
    if m:
        val = m.group(1)
        # 去掉末尾的 )($R[...]) 和可能的末尾括号
        val = re.sub(r'\)\(.*$', '', val)
        val = val.rstrip(')')
        try:
            return _js_to_json(val)
        except Exception as e:
            log.warning(f"JS→JSON 转换失败: {e}, raw={val[:200]}")

    # 兜底：尝试直接解析 JSON
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
        raise ValueError(f"非 JSON 响应，已保存到 ~/.myocusage_response.txt")


# ── 从 API 数据提取用量信息 ────────────────────────

PERIOD_MAP = {
    "rollingUsage": "5h", "hourly": "5h", "5h": "5h",
    "weeklyUsage": "weekly", "weekly": "weekly", "week": "weekly",
    "monthlyUsage": "monthly", "monthly": "monthly", "month": "monthly",
}
PERIOD_LABELS = {"5h": "5小时", "weekly": "本周", "monthly": "本月"}


def _fmt_reset(secs):
    """秒数 → 友好倒计时格式
    ≤24h: HH:MM:SS
    >24h: MM-DD HH:MM（具体重置日期）
    """
    if secs < 86400:
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        return f"{h:02d}:{m:02d}:{s:02d}"
    dt = datetime.now() + timedelta(seconds=secs)
    return dt.strftime("%m-%d %H:%M")


def parse_usage(data):
    """
    返回 {
      "5h":     {"used": N, "limit": N|None, "resetInSec": N},
      "weekly":  {"used": N, "limit": N|None, "resetInSec": N},
      "monthly": {"used": N, "limit": N|None, "resetInSec": N},
    }
    """
    raw_data = data
    if isinstance(data, dict) and "value" in data:
        data = data["value"]

    results = {}

    if not isinstance(data, dict):
        return results

    # 模式1: 直接包含 rollingUsage / weeklyUsage / monthlyUsage
    for src_key, period in PERIOD_MAP.items():
        entry = data.get(src_key)
        if isinstance(entry, dict):
            used = entry.get("cost") or entry.get("amount") or entry.get("usagePercent") or entry.get("usage")
            reset = entry.get("resetInSec")
            limit = entry.get("limit") or entry.get("max") or entry.get("quota") or entry.get("budget") or entry.get("total")
            if used is not None:
                results[period] = {"used": used, "limit": limit, "resetInSec": reset}

    # 用法: 如果 usagePercent 是 0-100 的百分比，limit 固定为 100
    for period, entry in list(results.items()):
        if entry["limit"] is None and entry["used"] is not None:
            # 如果 used 像是百分比 (0-100 的整数)
            v = entry["used"]
            if isinstance(v, (int, float)) and v <= 100 and v >= 0:
                # 暂时不自动推断，以免误判
                pass

    # 兜底：用 plan_monthly_limit
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
    """ASCII 进度条 [###------]"""
    filled = max(0, min(width, round(pct / 100 * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


# ── 状态栏应用 ─────────────────────────────────────

class MyocUsageApp(rumps.App):
    def __init__(self):
        icon_path = ICON_PATH if os.path.exists(ICON_PATH) else None
        super().__init__("OC", icon=icon_path, title="...",
                         quit_button=rumps.MenuItem("🚪 退出", callback=self.quit_app))
        self.config = load_config()
        self.usage_data = {}
        self.last_error = None
        self.last_raw_data = None
        self.refresh_timer = None

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

    def _update_display(self):
        monthly = self.usage_data.get("monthly")
        weekly = self.usage_data.get("weekly")
        hourly = self.usage_data.get("5h")

        # 显示用量值最大的那个时段
        candidates = []
        for entry, key in [(hourly, "5h"), (weekly, "weekly"), (monthly, "monthly")]:
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
                self.title = f"{used:.0f}%"
        else:
            self.title = "--"

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
        if self.refresh_timer:
            self.refresh_timer.stop()
        rumps.quit_application()


if __name__ == "__main__":
    if not os.path.exists(CONFIG_PATH):
        log.error(f"配置文件未找到: {CONFIG_PATH}")
        sys.exit(1)

    # 首次启动 → 创建守护进程后退出终端
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
