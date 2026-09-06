# 📖 INO Accompany 部署与使用完整指南 (Windows & macOS)

本文档提供 `INO Accompany` 在 **Windows** 与 **macOS** 系统下的详细部署、运行与调优指南。

---

## 🖥️ 1. 系统要求与环境准备

| 运行平台 | 操作系统版本 | 推荐 Python 版本 | 核心特性支持 |
| :--- | :--- | :--- | :--- |
| **Windows** | Windows 10 / 11 (64-bit) | Python 3.10 或 3.11 | 全部特性（Win32在位感知、0%CPU空闲检测、全屏免打扰、VOICEVOX语音） |
| **macOS** | macOS Monterey (12.0) 及以上 | Python 3.10 或 3.11 | Web CRT点阵、双通道LLM、Mem0记忆、VOICEVOX语音、摄像头感知 |

> [!TIP]
> 推荐使用 **Python 3.10 或 3.11**。安装时请务必勾选 **“Add python.exe to PATH”**（将 Python 添加到系统环境变量）。

---

## 🪟 2. Windows 部署与运行

### 2.1 极简一键部署（推荐）
1. 双击运行根目录下的 **`setup_windows.bat`**。
2. 脚本将自动完成以下操作：
   - 检查 Python 版本并创建独立的虚拟环境 `.venv`；
   - 升级 pip 并自动安装 `requirements.txt` 中所有的依赖项；
   - 若不存在 `config.yaml`，自动从 `config.example.yaml` 复制生成默认配置。
3. 看到 `🎉 恭喜！INO Accompany 一键部署完成！` 即表示准备就绪。

### 2.2 启动与常驻
- **日常使用启动**：双击 **`start_ino.bat`**（如果未部署会自动先触发部署），控制台会显示伴侣的思考与状态日志。
- **静默后台常驻**：双击 **`start_ino_silent.vbs`**，无黑框、静默常驻后台运行。
- **一键创建桌面快捷方式**：
  在当前目录右键打开 PowerShell，执行：
  ```powershell
  powershell -ExecutionPolicy Bypass -File create_shortcut.ps1
  ```
  即可在 Windows 桌面上生成可爱的 `INO Companion` 快捷方式。

### 2.3 Windows 常见问题与排错
- **Q: 提示 `pip install` 下载超时或很慢？**  
  A: 可以配置国内镜像加速源，在终端执行：
  ```powershell
  pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
  ```
- **Q: 提示 `找不到 python`？**  
  A: 说明安装 Python 时未勾选 PATH。请重新打开 Python 安装包，选择 “Modify” -> 勾选 “Add to PATH”。

---

## 🍎 3. macOS 部署与运行

### 3.1 极简一键部署
1. 打开 macOS **终端 (Terminal)**，`cd` 进入本项目目录：
   ```bash
   cd /path/to/INO-accompany-packup
   ```
2. 执行一键部署脚本：
   ```bash
   bash setup_mac.sh
   ```
3. 脚本会自动创建虚拟环境、安装针对 macOS 适配的依赖项（自动跳过 Windows 独占的 Win32 API 模块），并生成默认配置文件。

### 3.2 启动伴侣
在终端运行：
```bash
./start_ino_mac.sh
```
启动后在浏览器中打开：**`http://localhost:5000`** 即可进入 CRT 复古荧光绿交互界面。

### 3.3 macOS 平台特性说明
- **在位感知**：macOS 默认通过摄像头人脸采样（低功耗模式）和页面活跃事件判断是否在位；Windows 独占的 Win32 `GetLastInputInfo` 底层键盘鼠标事件已做跨平台平滑降级，不影响核心聊天与伴侣逻辑。
- **权限申请**：首次启用摄像头人脸采样时，macOS 会弹出摄像头访问权限弹窗，请点击“允许”。

---

## 🧠 4. 大语言模型 (LLM) 配置

INO 默认采用 **本地优先 (Local-First) + 云端兜底** 架构。

### 选项 A：本地 LM Studio（强烈推荐，100% 本地隐私、免费）
1. 下载并安装 [LM Studio](https://lmstudio.ai/)。
2. 下载任意对话模型（推荐 `Qwen 2.5 7B/14B/32B`、`Gemma 2` 或 `Llama 3`）。
3. 点击左侧 **“Local Server”**，加载模型并点击 **“Start Server”**（默认端口为 `1234`）。
4. INO 启动时会自动连接 `http://127.0.0.1:1234/v1`，无需任何额外配置！

### 选项 B：本地 Ollama
若习惯使用 Ollama，启动后在 `config.yaml` 中修改：
```yaml
llm:
  base_url: http://127.0.0.1:11434/v1
  model: qwen2.5:7b # 填写你 pull 的模型名
```

### 选项 C：DeepSeek 在线 API 备用兜底
当显卡算力不足或未开启本地 LM Studio 时，INO 可自动无缝切换至 DeepSeek：
- **方式 1 (推荐)**：在系统环境变量中设置 `DEEPSEEK_API_KEY`：
  - Windows PowerShell: `[Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "sk-xxx", "User")`
  - macOS: `export DEEPSEEK_API_KEY="sk-xxx"`
- **方式 2**：在 `config.yaml` 中的 `llm.fallback.api_key_file` 填入保存有 Key 的文本文件路径。

---

## 🎙️ 5. 语音引擎 (VOICEVOX) 配置
 
项目全面集成 **VOICEVOX** 作为专属声音引擎，支持丰富的二次元声线、原生日语情感合成以及基于拼音映射的拟音朗读：
 
1. 前往 [VOICEVOX 官网](https://voicevox.hiroshiba.jp/) 下载 **VOICEVOX Engine (绿色免安装版)** 或完整版客户端。
2. 将引擎放置于项目根目录的 `voicevox_engine/` 文件夹下（确保包含 `run.exe`），或直接在后台运行已安装的 VOICEVOX（默认服务端口 50021）。
3. 启动 INO 后，在 Web 界面顶部语音下拉栏中可自由切换数十种声线（如九州Sora、四国めたん、冥鸣Himari等），并实时调整语速与音量。
4. 两种朗读模式说明：
   - **`ja`（日文原生，推荐）**：自动将回复进行日文口语转换后朗读，情感丰富且极其萌系。
   - **`zh`（中文拟音）**：通过拼音-假名音素拟音映射，让二次元角色直接开口说中文。可在 `config.yaml` 或 Web 界面中即时切换。

---

## ⚙️ 6. 核心配置文件速查 (`config.yaml`)

```yaml
autonomy:
  background_action_chance: 0.15   # 0.15 概率自主触发思考或搭话
  enable_continue: true            # 允许追发消息

llm:
  mode: auto                       # auto (本地优先，掉线切云端)
  base_url: http://127.0.0.1:1234/v1

memory:
  auto_save: true                  # 对话后自动提取事实记忆并持久化
  embedding:
    idle_unload_minutes: 30        # 闲置 30 分钟自动卸载向量显存

news:
  background_refresh_minutes: 30   # 每半小时后台多源抓取最新资讯
  fresh_hours: 72                  # 保留72小时内新鲜新闻

presence:
  idle_threshold: 300              # 离开座位 300 秒判定为 away
  enable_fullscreen_suppress: true # 游戏或视频全屏时静默免打扰
```
