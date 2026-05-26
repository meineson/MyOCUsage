# OpenCode 用量监控

macOS 菜单栏应用，在状态栏实时显示 OpenCode 的 5 小时 / 本周 / 本月用量。

![screenshot](screenshot.png)

## 功能

-   **菜单栏图标**：蓝色瓶子图标 + 用量百分比
-   **三个时段**：5 小时滚动、本周、本月用量
-   **进度条**：用量可视化的 ASCII 进度条
-   **倒计时**：距离用量重置的剩余时间（≤24h 显示 HH:MM:SS，>24h 显示 MM-DD HH:MM）
-   **自动刷新**：默认每 60 秒自动更新
-   **后台运行**：启动后自动守护进程化，不占用终端

## 安装

### 前置要求

-   macOS 10.15+
-   Python 3.10+
-   已注册 [OpenCode](https://opencode.ai) 账号并登录

### 步骤

```bash
# 1. 克隆仓库
git clone https://github.com/你的用户名/myocusage.git
cd myocusage

# 2. 安装依赖
pip3 install -r requirements.txt

# 3. 配置
cp config.json.sample config.json
```

## 配置

打开 `config.json`，参照以下方式填写：

### 获取配置信息

| 字段 | 获取方式 |
|------|----------|
| `cookies` | 浏览器打开 [opencode.ai](https://opencode.ai) 并登录，按 `F12` → **Network** → 过滤 `_server` → 点击任意请求 → **Request Headers** → 复制 `Cookie` 字段的完整值（格式如 `oc_locale=zh; auth=Fe26.2**...**`） |
| `workspace_id` | 访问 `https://opencode.ai/workspace/{你的工作区ID}/usage`，从地址栏复制中间那串 ID（格式如 `wrk_xxx...`） |
| `server_id` | 同上页面，F12 → Network → `_server` 请求 → **Request Headers** → 复制 `x-server-id` |
| `server_instance` | 同上，复制 `x-server-instance`（通常为 `server-fn:3`） |
| `plan_monthly_limit` | 可选。如果菜单栏只显示百分比不显示数值，可在此填入你的月计划金额上限（美元），用于计算进度条 |

### 配置文件示例

```json
{
  "cookies": "oc_locale=zh; auth=Fe26.2**你的认证token**",
  "workspace_id": "wrk_xxxxxxxxxxxxxxxxxxxxx",
  "server_id": "xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "server_instance": "server-fn:3",
  "plan_monthly_limit": null,
  "refresh_interval": 60
}
```

> **注意**：Cookie 会过期（通常几天到几周），过期后状态栏会显示 🔒 图标，重新按上述步骤复制即可。

## 使用

```bash
python3 myocusage_status.py
```

启动后终端自动返回，应用在后台运行。

### 菜单项

-   **5 小时 / 本周 / 本月**：各时段用量百分比、进度条和重置倒计时
-   **🔄 手动刷新**：立即刷新用量数据
-   **🚪 退出**：退出应用

## 技术说明

-   通过 OpenCode 的 Convex RPC 端点（`_server`）获取用量数据
-   内置 Convex JavaScript 响应格式解析器（处理 `!0`/`!1`、`$R[N]={...}` 等 JS 表达式）
-   使用 `rumps` (Ridiculously Uncomplicated macOS Python Statusbar apps) 实现菜单栏

## 文件结构

```
myocusage/
├── myocusage_status.py   # 主程序
├── config.json           # 配置文件
├── config.json.sample    # 配置模板
├── bottle_icon_small.png # 菜单栏图标
├── requirements.txt      # Python 依赖
└── run.sh                # 一键安装启动脚本
```

## License

MIT
