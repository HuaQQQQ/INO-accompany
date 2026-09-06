#!/bin/bash
cd "$(dirname "$0")"

echo "================================================================"
echo "          🌸 INO Accompany 启动中 (macOS/Linux)..."
echo "================================================================"

if [ ! -d ".venv" ]; then
    echo "⚠️ 未检测到虚拟环境，正在自动执行一键部署..."
    bash setup_mac.sh
fi

source .venv/bin/activate
python app.py