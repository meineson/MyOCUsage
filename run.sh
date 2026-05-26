#!/bin/bash
# OpenCode 用量监控 - 安装依赖并启动

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== OpenCode 用量监控 ==="
echo ""

if [ ! -f "config.json" ]; then
  echo "[!] 请先创建 config.json："
  echo "    cp config.json.sample config.json"
  echo "    然后参考 README.md 填写配置"
  exit 1
fi

echo "[1/2] 安装依赖..."
pip3 install -r requirements.txt

echo "[2/2] 启动应用（后台运行）..."
python3 myocusage_status.py
