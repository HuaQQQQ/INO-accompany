#!/bin/bash
set -e

# 确保在当前脚本所在目录执行
cd "$(dirname "$0")"

echo "================================================================"
echo "          🌸 INO Accompany 桌面智能守护伴侣 - macOS/Linux 一键部署"
echo "================================================================"
echo ""

# 1. 检查 Python 3 环境
if ! command -v python3 &> /dev/null; then
    echo "❌ 错误: 未检测到 python3。请安装 Python 3.10 或 3.11:"
    echo "   macOS 推荐使用: brew install python@3.11"
    exit 1
fi

PY_VER=$(python3 -V 2>&1)
echo "✅ 检测到 Python: $PY_VER"

# 2. 创建或检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "📦 正在创建虚拟环境 (.venv)..."
    python3 -m venv .venv
    echo "✅ 虚拟环境创建成功！"
else
    echo "✅ 虚拟环境 (.venv) 已存在，跳过创建。"
fi

# 3. 激活虚拟环境并安装依赖
echo "🚀 正在激活虚拟环境并安装依赖包 (可能需要几分钟)..."
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

# 4. 初始化配置文件
if [ ! -f "config.yaml" ]; then
    echo "⚙️ 正在从模板创建默认配置文件 config.yaml..."
    cp config.example.yaml config.yaml
    echo "✅ 已生成 config.yaml。"
else
    echo "✅ 配置文件 config.yaml 已存在。"
fi

# 5. 赋予启动脚本执行权限
chmod +x start_ino_mac.sh 2>/dev/null || true

echo ""
echo "================================================================"
echo " 🎉 恭喜！INO Accompany 在 macOS/Linux 上部署完成！"
echo ""
echo " 【启动方式】："
echo "  在终端中直接运行: ./start_ino_mac.sh"
echo "  启动后浏览器访问: http://localhost:5000"
echo ""
echo " 【模型准备】："
echo "  请启动 LM Studio / Ollama 并开启本地 OpenAI 兼容服务 (默认端口 1234)；"
echo "  或导出环境变量: export DEEPSEEK_API_KEY='你的密钥'"
echo "================================================================"
echo ""