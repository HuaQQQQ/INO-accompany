import os
import time
import re
import json
import random
import threading
import yaml
import requests
from typing import Dict, Any, List, Optional
from openai import OpenAI

os.environ.setdefault("MEM0_TELEMETRY", "False")  # 关闭 mem0 的 PostHog 遥测（须在 import mem0 之前）

import dns_resolver  # DoH DNS 兜底：应对 UDP 53 被污染导致的域名解析失败

from presence_detector import PresenceDetector
from news_fetcher import NewsFetcher
from character_parser import CharacterParser
from memory_manager import MemoryManager
from voice_engine import VoiceEngine
from expression_parser import parse_expression, clean_model_text

from tools import (
    ToolRegistry,
    WebSearchTool,
    NewsFeedTool,
    SystemStatusTool,
    MemoryTool,
    VoiceControlTool,
    ManageNewsTopicsTool,
    UpdateSidebarNewsTool,
    ReadNewsDetailTool
)

def extract_openclaw_tool_calls(content: str) -> tuple[str, List[Dict[str, Any]]]:
    """
    Extract OpenClaw style <tool_call>...</tool_call> or ```json tool calls from LLM output.
    Returns (cleaned_content, list_of_tool_call_dicts)

    FIX(1.2): 未闭合的 <tool_call> 不再吞掉后续正文 ——
    已闭合标签严格配对解析；未闭合的只尝试解析行内 JSON，
    解析失败时仅剥掉标签本身，正文全部保留。
    """
    if not content:
        return "", []

    tool_calls = []
    cleaned_content = content

    def _parse_call(json_str: str):
        data = None
        try:
            data = json.loads(json_str)
        except Exception:
            try:
                data = json.loads(json_str.replace("'", '"'))
            except Exception:
                data = None
        if not data:
            return None
        name = data.get("name") or data.get("tool")
        args = data.get("arguments") or data.get("parameters") or data.get("args") or {}
        if not name:
            return None
        return {"name": name, "arguments": args}

    # 1. Match fully closed <tool_call> ... </tool_call> pairs ONLY
    pattern1 = r'<tool_call>\s*(.*?)\s*</tool_call>'
    matches = list(re.finditer(pattern1, content, re.DOTALL | re.IGNORECASE))
    for m in matches:
        raw_snippet = m.group(0)
        parsed = _parse_call(m.group(1).strip())
        if parsed:
            tool_calls.append({**parsed, "raw": raw_snippet})
            cleaned_content = cleaned_content.replace(raw_snippet, '')

    # 2. Unclosed <tool_call>: parse JSON up to line end; on failure strip ONLY the tag
    for m in list(re.finditer(r'<tool_call>\s*([^\n]*)', cleaned_content, re.IGNORECASE)):
        json_str = m.group(1).strip()
        parsed = _parse_call(json_str) if json_str else None
        if parsed:
            tool_calls.append({**parsed, "raw": m.group(0)})
            cleaned_content = cleaned_content.replace(m.group(0), '')
        else:
            # 解析失败 → 只移除 <tool_call> 标签本身，保留后续正文（防吞回复）
            cleaned_content = cleaned_content.replace('<tool_call>', '', 1)

    # 3. Fallback: match ```json ... ``` or ```tool_call ... ``` blocks
    if not tool_calls:
        block_matches = list(re.finditer(r'```(?:tool_call|json)?\s*(\{\s*"(?:name|tool)"\s*:.*?\})\s*```', content, re.DOTALL | re.IGNORECASE))
        for m in block_matches:
            raw_snippet = m.group(0)
            parsed = _parse_call(m.group(1).strip())
            if parsed:
                tool_calls.append({**parsed, "raw": raw_snippet})
                cleaned_content = cleaned_content.replace(raw_snippet, '')

    return cleaned_content.strip(), tool_calls

class INOAgent:
    """
    INO Agent - Intelligent Desktop Companion Brain.
    Orchestrates Persona, Long-Term Memory, Perception, Tools, and ReAct/Function Calling Loop.
    """
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        self.llm_config = self.config.get("llm", {})
        self.presence_config = self.config.get("presence", {})
        self.news_config = self.config.get("news", {})
        self.memory_config = self.config.get("memory", {})
        self.autonomy_config = self.config.get("autonomy", {})

        # 近期工具活动摘要（跨轮连续性，注入 system prompt）
        self.tool_activity: List[str] = []

        # Sub-services
        self.presence_detector = PresenceDetector(self.config)
        self.news_fetcher = NewsFetcher(self.config)
        self.memory = MemoryManager(self.config)

        # Character Parser & Persona Prompt
        char_config = self.config.get("character", {})
        self.parser = CharacterParser(char_config.get("card_file", "character.json"))
        self.base_system_prompt = self.parser.get_system_prompt()

        # Voice Engine
        voice_config = self.config.get("voice", {})
        self.voice = VoiceEngine(
            engine_dir=voice_config.get("engine_dir", "voicevox_engine"),
            exe_name=voice_config.get("exe_name", "run.exe"),
            speaker_id=voice_config.get("speaker_id", 16),
            volume=voice_config.get("volume", 0.3),
            voice_lang_mode=voice_config.get("voice_lang_mode", "ja"),
            is_muted=voice_config.get("is_muted", False),
            speed_scale=voice_config.get("speed_scale", 1.0),
            llm_translator=self._llm_translate_ja,  # Google 翻译不可达时的 LLM 翻译兜底
        )

        # Initialize OpenAI Client (LM Studio / OpenAI-compatible endpoint，局域网不可达时自动切换备用 API)
        self._llm_mode = "local"  # local(局域网 LM Studio) | deepseek(备用 API)
        self.client, self.model = self._build_client_and_model()
        self._last_model_check = 0.0
        if self._llm_mode == "local":
            self._ensure_valid_model(force=True)
        else:
            print(f"🎯 [LLM Fallback] 当前使用备用 API 模型: {self.model}")

        # Tool Registry & Core Tools Initialization
        self.tools = ToolRegistry()
        self._register_default_tools()

        # Conversation History & Session Summary
        self.session_summary: str = ""
        self.conversation_history: List[Dict[str, Any]] = [
            {"role": "system", "content": self._build_system_prompt()}
        ]
        self.last_state = "active"
        # 记忆自动保存线程标记（防重复启动）
        self._memory_save_thread_started = False
        self._memory_save_queue = []
        self._memory_save_lock = threading.Lock()
        # 对话进行中标记：记忆提取不与对话争抢局域网 LLM（防连续点击时“走神”）
        self._chatting = False
        # 上下文异步压缩：后台线程总结旧对话，不阻塞主对话；总结结果直接注入上下文
        self._compressing = False
        self._history_lock = threading.Lock()

    @staticmethod
    def _probe_llm(base_url: str, timeout: float = 5.0, retries: int = 2) -> bool:
        """探测 LLM 服务是否可达（局域网 LM Studio / 备用 API）。
        只要收到 HTTP 响应（含 401 等鉴权拒绝）即视为可达；仅 DNS/连接层失败才算不可达。
        带重试：服务启动早期可能繁忙，避免误判 fallback。"""
        for attempt in range(retries + 1):
            try:
                requests.get(f"{base_url}/models", timeout=timeout)
                return True
            except Exception:
                if attempt < retries:
                    time.sleep(2)
        return False

    @staticmethod
    def _load_api_key(path: str) -> str:
        """从环境变量或本地文件读取 API 密钥（首行非注释内容），不落盘到配置文件。"""
        env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if env_key:
            return env_key
        if not path:
            return ""
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            return line
        except Exception as e:
            print(f"[API Key Load Error]: {e}")
        return ""

    def _build_client_and_model(self):
        """严格遵循用户持久化的 llm.mode 选择（不自动 fallback）；未保存时自动探测（本地优先，不可达切 DeepSeek）。"""
        fb = self.llm_config.get("fallback", {})
        local_base = self.llm_config.get("base_url", "http://127.0.0.1:1234/v1")
        saved_mode = (self.llm_config.get("mode") or "").strip().lower()

        # 用户保存过 DeepSeek → 严格使用，不探测不 fallback（连不上靠 DNS 兜底 + 调用重试解决）
        if saved_mode == "deepseek":
            ds_base = fb.get("base_url", "https://api.deepseek.com")
            key = self._load_api_key(fb.get("api_key_file", ""))
            if not key:
                print("⚠️ [LLM 持久化模式] 未找到 DeepSeek API 密钥文件，请检查配置！")
            print("🎯 [LLM 持久化模式] 按用户选择使用 DeepSeek（不自动回退本地）")
            self._llm_mode = "deepseek"
            return (OpenAI(base_url=ds_base, api_key=key or "not-needed", timeout=300.0),
                    fb.get("model", "deepseek-chat"))

        # 用户保存过 local → 严格使用，不探测不 fallback
        if saved_mode == "local":
            print("🎯 [LLM 持久化模式] 按用户选择使用本地 LM Studio（不自动回退）")
            self._llm_mode = "local"
            return (
                OpenAI(base_url=local_base, api_key=self.llm_config.get("api_key", "not-needed"), timeout=300.0),
                self.llm_config.get("model", "qwen3.6-35b-a3b-uncensored-heretic-native-mtp-preserved")
            )

        # 未保存（auto）：自动探测，本地优先，不可达切 DeepSeek
        if fb.get("enabled", True) and not self._probe_llm(local_base):
            key = self._load_api_key(fb.get("api_key_file", ""))
            if key:
                print(f"⚠️ [LLM Fallback] 局域网 LM Studio 不可达，自动切换备用 API: {fb.get('base_url')}")
                self._llm_mode = "deepseek"
                return (
                    OpenAI(base_url=fb.get("base_url", "https://api.deepseek.com"), api_key=key, timeout=300.0),
                    fb.get("model", "deepseek-chat")
                )
            print("⚠️ [LLM Fallback] 局域网不可达且未找到备用 API 密钥，仍尝试直连")
        self._llm_mode = "local"
        return (
            OpenAI(base_url=local_base, api_key=self.llm_config.get("api_key", "not-needed"), timeout=300.0),
            self.llm_config.get("model", "qwen3.6-35b-a3b-uncensored-heretic-native-mtp-preserved")
        )

    def _save_llm_mode(self, mode: str):
        """把用户手动选择的模型通道持久化到 config.yaml → llm.mode。"""
        import yaml as _yaml
        try:
            cfg = {}
            if os.path.exists("config.yaml"):
                with open("config.yaml", "r", encoding="utf-8") as f:
                    cfg = _yaml.safe_load(f) or {}
            if "llm" not in cfg:
                cfg["llm"] = {}
            cfg["llm"]["mode"] = mode
            with open("config.yaml", "w", encoding="utf-8") as f:
                _yaml.safe_dump(cfg, f, allow_unicode=True)
            self.llm_config["mode"] = mode  # 同步内存，供运行期判断「已保存模式」
            print(f"💾 [LLM 持久化] 已保存模型通道选择: {mode}")
        except Exception as e:
            print(f"[LLM 持久化失败]: {e}")

    def _apply_llm_mode(self, mode: str) -> tuple:
        """按指定模式重建 client（不检查对话状态，供手动切换与自动恢复共用）。
        返回 (ok: bool, message: str)。"""
        fb = self.llm_config.get("fallback", {})
        if mode == "deepseek":
            ds_base = fb.get("base_url", "https://api.deepseek.com")
            key = self._load_api_key(fb.get("api_key_file", ""))
            if not key:
                return False, "未找到 DeepSeek API 密钥文件，无法切换到 DeepSeek。"
            self.client = OpenAI(base_url=ds_base, api_key=key, timeout=300.0)
            self.model = fb.get("model", "deepseek-chat")
            self._llm_mode = "deepseek"
            print(f"🔀 [LLM Manual] 手动切换到 DeepSeek: {self.model}")
            self._save_llm_mode("deepseek")  # 持久化用户选择，重启后保持
            return True, f"已手动切换到 DeepSeek（{self.model}）"
        # mode == "local"
        local_base = self.llm_config.get("base_url", "http://127.0.0.1:1234/v1")
        self.client = OpenAI(
            base_url=local_base,
            api_key=self.llm_config.get("api_key", "not-needed"),
            timeout=300.0,
        )
        self.model = self.llm_config.get(
            "model", "qwen3.6-35b-a3b-uncensored-heretic-native-mtp-preserved"
        )
        self._llm_mode = "local"
        self._ensure_valid_model(force=True)  # auto 跟随实际加载的活动模型
        print(f"🔀 [LLM Manual] 手动切换回本地 LM Studio: {self.model}")
        self._save_llm_mode("local")  # 持久化用户选择，重启后保持
        return True, f"已切换回本地 LM Studio（{self.model}）"

    def switch_llm_mode(self, mode: str) -> tuple:
        """手动强制切换模型通道：'local'(局域网 LM Studio) | 'deepseek'(备用 API)。
        返回 (ok: bool, message: str)。对话进行中禁止切换，避免打断正在生成的回复。"""
        if self._chatting:
            return False, "INO 正在思考中，请稍等片刻再切换模型。"
        return self._apply_llm_mode(mode)

    def _llm_translate_ja(self, text: str, timeout: float = 20.0):
        """LLM 日语翻译（语音链路专用）：翻译为可爱日语口语并自然融入情绪语气词。
        语音 worker 线程调用；带 20 条/5 分钟缓存防重复；失败返回 None（调用方回退原文）。"""
        cache = getattr(self, "_translate_cache", None)
        if cache is None:
            cache = self._translate_cache = {}
        now = time.time()
        cached = cache.get(text)
        if cached and now - cached[0] < 300:
            return cached[1]
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": (
                        "你是 INO 的日语翻译官，把中文翻译成自然、可爱、适合语音合成的日语口语。\n"
                        "要求：\n"
                        "1. 只输出译文本身，不要任何解释、引号、注释或多余文字；\n"
                        "2. 使用软萌可爱的日语口语体（です/ます 或 だよ/だね），像元气桌宠在说话；\n"
                        "3. 根据原文情绪自然添加日语语气词（よ、ね、だよ、だね、かしら、～等），\n"
                        "4. 保留原文中已有的日文假名、英文专有名词（如 RTX、OpenAI、master）；\n"
                        "5. 保留标点语气（！？～）便于语音断句，译文长度与原文相当，不扩写。"
                    )},
                    {"role": "user", "content": text},
                ],
                temperature=0.3,
                max_tokens=2048,
                timeout=timeout,
            )
            out = (resp.choices[0].message.content or "").strip()
            if out:
                cache[text] = (now, out)
                if len(cache) > 20:
                    oldest = min(cache, key=lambda k: cache[k][0])
                    cache.pop(oldest)
                return out
        except Exception as e:
            print(f"[LLM 翻译失败]: {e}")
        return None

    def _try_switch_to_deepseek(self) -> bool:
        """运行时切换到备用 API（本地 LLM 调用失败且局域网仍不可达时）。"""
        if self._llm_mode != "local":
            return False
        fb = self.llm_config.get("fallback", {})
        if not fb.get("enabled", True):
            return False
        if self._probe_llm(self.llm_config.get("base_url", "")):
            return False
        key = self._load_api_key(fb.get("api_key_file", ""))
        if not key:
            print("⚠️ [LLM Fallback] 未找到备用 API 密钥文件，无法切换")
            return False
        self.client = OpenAI(base_url=fb.get("base_url", "https://api.deepseek.com"), api_key=key, timeout=300.0)
        self.model = fb.get("model", "deepseek-chat")
        self._llm_mode = "deepseek"
        print(f"🔄 [LLM Fallback] 已切换备用 API: {self.model}")
        return True

    def _ensure_valid_model(self, force: bool = False):
        """Query currently loaded model dynamically with a 60-second cache to prevent redundant API calls."""
        if self._llm_mode != "local":
            return  # 备用 API 模式：模型固定，不做 auto 探测
        now = time.time()
        if not force and (now - self._last_model_check < 60.0):
            return

        self._last_model_check = now
        try:
            models = self.client.models.list()
            if models and hasattr(models, 'data') and len(models.data) > 0:
                available_ids = [m.id for m in models.data]
                # Filter out embedding models
                text_models = [m_id for m_id in available_ids if "embed" not in m_id.lower()]
                
                # Check for currently loaded active model
                loaded_model = None
                for m in models.data:
                    m_dict = m.model_dump() if hasattr(m, 'model_dump') else (m.__dict__ if hasattr(m, '__dict__') else {})
                    if m_dict.get("state") == "loaded" or m_dict.get("loaded") is True:
                        loaded_model = m.id
                        break
                
                if loaded_model:
                    if loaded_model != self.model:
                        print(f"⚠️ [Model Auto-switch] 检测到活动模型变化: {self.model} → {loaded_model}（自动跟随，避免行为不一致）")
                    self.model = loaded_model
                    print(f"🎯 自动捕获当前推载中的活动大模型: {self.model}")
                elif self.model not in available_ids and text_models:
                    print(f"⚠️ [Model Auto-switch] 配置的模型 '{self.model}' 不在可用列表，自动切换为: {text_models[0]}")
                    self.model = text_models[0]
                    print(f"🔄 自动切换为终端可用模型: {self.model}")
                else:
                    print(f"当前连通的大模型 ID: {self.model}")
        except Exception as e:
            print(f"Model auto-detect warning: {e}")

    def _register_default_tools(self):
        """Register default modular tools for INO."""
        self.tools.register(WebSearchTool(self.news_fetcher))
        self.tools.register(NewsFeedTool(self.news_fetcher))
        self.tools.register(SystemStatusTool(self.presence_detector))
        self.tools.register(MemoryTool(self.memory))
        self.tools.register(VoiceControlTool(self.voice))
        self.tools.register(ManageNewsTopicsTool(self.news_fetcher))
        self.tools.register(UpdateSidebarNewsTool(self.news_fetcher))
        self.tools.register(ReadNewsDetailTool(self.news_fetcher))
        print(f"[INO Agent] 已成功注册 {len(self.tools.list_tools())} 个核心工具: {[t.name for t in self.tools.list_tools()]}")

    def register_tool(self, tool_instance):
        """External API for registering new custom tools dynamically."""
        self.tools.register(tool_instance)
        print(f"[INO Agent] 动态注册新工具: {tool_instance.name}")

    def _record_tool_activity(self, tool_name: str, result: str):
        """记录近期工具活动摘要（最多 5 条），供 system prompt 注入保持跨轮连续性。"""
        summary = f"{tool_name} → {result[:100]}".replace('\n', ' ')
        self.tool_activity.append(summary)
        if len(self.tool_activity) > 5:
            self.tool_activity.pop(0)

    def _get_hardware_context(self) -> str:
        """读取本机硬件状态（5 秒缓存，避免频繁 nvidia-smi 子进程），注入 INO 上下文。"""
        import time as _time
        now = _time.time()
        if not hasattr(self, '_hw_cache') or now - self._hw_cache[0] > 5:
            parts = []
            try:
                import psutil
                parts.append(f"CPU {psutil.cpu_percent(interval=None)}%")
                mem = psutil.virtual_memory()
                parts.append(f"内存 {mem.percent}% ({round(mem.used/1024**3,1)}/{round(mem.total/1024**3,1)}G)")
            except Exception:
                parts.append("CPU/内存: 未知")
            try:
                import subprocess
                r = subprocess.run(
                    ["nvidia-smi",
                     "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=8,
                )
                if r.returncode == 0 and r.stdout.strip():
                    p = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
                    if len(p) >= 5:
                        parts.append(f"{p[0]} {p[1]}°C 占用{p[2]}% 显存{p[3]}/{p[4]}MB")
            except Exception:
                pass
            self._hw_cache = (now, "；".join(parts))
        return self._hw_cache[1]

    def _rebuild_system_prompt(self):
        """每次对话前重建 system prompt，刷新硬件上下文与资讯速览等动态数据。"""
        if self.conversation_history and self.conversation_history[0].get("role") == "system":
            self.conversation_history[0]["content"] = self._build_system_prompt()
        else:
            self.conversation_history.insert(0, {"role": "system", "content": self._build_system_prompt()})

    def _build_system_prompt(self) -> str:
        """Compose character persona + strict tone + OpenClaw Tool protocol."""
        tools_guide = self.tools.get_tools_prompt_description()
        summary_part = f"\n【前情提要 / 历史对话要点摘要】:\n{self.session_summary}\n" if self.session_summary else ""
        activity_part = ""
        if self.tool_activity:
            lines = "\n".join(f"  - {a}" for a in self.tool_activity)
            activity_part = f"\n【近期工具活动记录（你刚刚执行过的操作，回答时可直接引用其结果）】:\n{lines}\n"
        # 按配置概率随机注入副屏资讯速览（news_prompt_chance）
        news_part = ""
        try:
            chance = float(self.news_config.get("news_prompt_chance", 0.05) or 0)
            if random.random() < chance:
                snippet = self.news_fetcher.get_news_prompt_snippet(count=2)
                if snippet:
                    news_part = f"\n【副屏资讯随机速览（顺带看一眼即可，可向 master 提一嘴或默默记住）】:\n{snippet}\n"
        except Exception as e:
            print(f"[NewsPrompt] 资讯速览注入失败: {e}")
        # 实时硬件状态上下文（每次对话前刷新）
        hardware_part = ""
        try:
            hw = self._get_hardware_context()
            hardware_part = f"\n【当前本机硬件状态（你感知到的电脑实时状态，可自然提及）】：{hw}\n"
        except Exception as e:
            print(f"[HardwareContext] 硬件状态注入失败: {e}")
        # 当前时间上下文（每次对话前刷新，让 INO 知道现在几点，能自然地说“下午好”“这么晚了”）
        time_part = ""
        try:
            import datetime as _dt
            now = _dt.datetime.now()
            weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]
            time_part = f"\n【当前时间】：{now.strftime('%Y年%m月%d日')} {weekday_cn} {now.strftime('%H:%M')}\n"
        except Exception as e:
            print(f"[TimeContext] 时间注入失败: {e}")
        addon = f"""

【INO Agent 工具调用协议 (OpenClaw Style)】:
你是拥有自主思考与工具调用能力的桌面伴侣 INO。当 master 询问新鲜资讯、硬件数码、最新科技、要求查询记忆、管理订阅或调节音量时，你可以主动调用工具。

工具调用格式（必须严格使用 <tool_call> 标记，内部为 JSON 格式）：
<tool_call>
{{"name": "工具名称", "arguments": {{"参数名": "参数值"}}}}
</tool_call>

规则要求：
1. 当 master 询问新鲜资讯、显卡/手机价格、最新科技或不确定的实时事实时，必须主动输出 `<tool_call>` 调用 `web_search` 检索最新事实，严禁盲目凭空捏造！
2. 当 master 提及某个特定前沿科技/数码话题（如 '看看最新大模型动态'、'挂载量子计算进展'），或你想把搜寻到的新科技展示在右侧列时，可以输出 `<tool_call>` 调用 `update_sidebar_news` 检索并把它挂载更新到右侧副屏资讯列！
3. 每次调用工具时，只需直接输出 `<tool_call>` 块。系统会在后台自动执行工具并将结果反馈给你，随后你再以萌系可爱的 INO 口吻做出最终回答。
4. 始终保持活泼软萌、略带元气撒娇的台湾女生口吻（常用「啦、喔、餒、醬子、耶～」），称呼 master。
5. 说话时多使用标点符号（逗号「，」、感叹号「！」、波浪号「～」）自然切分短句，让语音播报有自然的停顿与换气感。
6. 严禁在回答中暴露任何思维链（如 <think>...</think> 或'根据规则...'）。
7. 当你觉得还有事情可以做（比如补充搜索、整理记忆、更新侧边栏资讯）时，可在回复末尾加上 <continue> 标记，系统会允许你继续自主行动最多 4 轮。

【简洁原则（重要，违反即失败）】:
1. 日常对话回复控制在 1~3 句、60 字以内，说完就停！除非 master 明确要求详细讲解。
2. 禁止复述 master 的提问、禁止重复自己刚说过的话、禁止“好的，让我来…”之类的开场套路，直接进入正题。
3. 颜文字/动作标签不要每句都加，一段回复最多 1 个；问候语只 1 句（如“おかえり～ マスター！”）。
4. 自主发言（打招呼/吐槽/分享）永远只 1 句，短而自然。
5. 讲解新闻/资讯时：先说结论 1 句，再按需补细节，不要从头到尾平铺。
{time_part}
{hardware_part}
{news_part}
{summary_part}
{activity_part}
【可用工具列表】：
{tools_guide}
"""
        return self.base_system_prompt + addon

    def compress_context(self, force: bool = False) -> str:
        """
        Intelligently summarize older conversation turns to compress context window size
        without losing important topics or facts.
        保留最近 KEEP_RECENT 条（对齐 user 起始保证配对完整），避免激进裁剪导致"突然失忆"。
        """
        KEEP_RECENT = 10  # 保留最近 10 条（≈5 轮对话）
        if len(self.conversation_history) <= KEEP_RECENT + 2 and not force:
            return self.session_summary or "当前对话较短，暂无需压缩。"

        if len(self.conversation_history) > KEEP_RECENT + 2:
            recent_keep = self.conversation_history[-KEEP_RECENT:]
            # 对齐：保留段必须以 user 消息开头，确保 user/assistant 配对完整
            while recent_keep and recent_keep[0].get("role") != "user":
                recent_keep = recent_keep[1:]
            to_summarize = self.conversation_history[1:len(self.conversation_history) - len(recent_keep)]
        else:
            # 短历史（含 /compact 强制压缩）：至少保留最近 4 条，避免压缩后彻底清空
            keep_n = min(4, len(self.conversation_history) - 1)
            recent_keep = self.conversation_history[-keep_n:] if keep_n > 0 else []
            while recent_keep and recent_keep[0].get("role") != "user":
                recent_keep = recent_keep[1:]
            to_summarize = self.conversation_history[1:len(self.conversation_history) - len(recent_keep)]

        if not to_summarize:
            return self.session_summary or "当前对话暂无可压缩内容。"

        dialog_text = []
        for msg in to_summarize:
            role = "Master (主人)" if msg.get("role") == "user" else "INO"
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                clean_c = re.sub(r'【(?:相关记忆碎片|过往记忆)[^】]*】:.*?\n\n', '', content, flags=re.DOTALL).strip()
                dialog_text.append(f"{role}: {clean_c[:300]}")

        text_block = "\n".join(dialog_text)
        prompt = f"""请对以下历史对话内容进行精炼的要点摘要（用 2~3 句话总结讨论过的核心主题、关键提问与结论）：

历史对话：
{text_block}

已有前情摘要：
{self.session_summary or '无'}

请直接输出更新后的精炼摘要要点（不要包含思维链，直接输出摘要内容）："""

        try:
            res = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=2048,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}}  # 推理模型关思维链，防 content 为空
            )
            msg = res.choices[0].message
            new_summary = (msg.content or "").strip()
            if not new_summary:
                reasoning = getattr(msg, "reasoning_content", None)
                if reasoning:
                    new_summary = str(reasoning).strip()[:400]
            new_summary = re.sub(r'<think>.*?</think>', '', new_summary, flags=re.DOTALL).strip()
            if new_summary:
                self.session_summary = new_summary
                system_msg = {"role": "system", "content": self._build_system_prompt()}
                self.conversation_history = [system_msg] + recent_keep
                print(f"🧹 [上下文自动压缩完成]: {self.session_summary}")
                return self.session_summary
            print("⚠️ [上下文压缩] LLM 摘要返回空，启用确定性兜底裁剪")
        except Exception as e:
            print(f"[Compress Context Error]: {e}")

        # 确定性兜底：LLM 摘要失败/为空时也必须裁剪，否则上下文无限膨胀 → 超出窗口后"突然失忆"
        if len(self.conversation_history) > KEEP_RECENT + 2:
            # 无 LLM 摘要时用被裁对话的话题线索拼一份粗摘要，尽量留住关键信息
            if not self.session_summary:
                topics = [m.get("content", "")[:40] for m in to_summarize if m.get("role") == "user"]
                if topics:
                    self.session_summary = "【自动整理话题线索】" + "；".join(topics[-6:])
            system_msg = {"role": "system", "content": self._build_system_prompt()}
            self.conversation_history = [system_msg] + recent_keep
            print(f"⚠️ [上下文压缩降级] 已确定性裁剪至 {len(self.conversation_history)} 条（保留最近 {len(recent_keep)} 条）")

        return self.session_summary or "压缩处理完成。"

    def compress_context_async(self) -> bool:
        """后台线程异步总结旧对话并回写上下文（不阻塞主对话）。
        保留最近 KEEP_RECENT 条（对齐 user 起始），更早的消息交给后台 LLM 总结，
        总结结果写入 session_summary 并随 system prompt 直接作为上下文。"""
        if self._compressing:
            return False
        KEEP_RECENT = 10
        hist = self.conversation_history
        if len(hist) <= KEEP_RECENT + 2:
            return False
        recent_keep = hist[-KEEP_RECENT:]
        while recent_keep and recent_keep[0].get("role") != "user":
            recent_keep = recent_keep[1:]
        if not recent_keep:
            return False
        cut = len(hist) - len(recent_keep)
        to_summarize = hist[1:cut]
        if not to_summarize:
            return False
        boundary = hist[cut]  # 保留段首条消息（对象锚点，回写时定位）
        self._compressing = True
        threading.Thread(target=self._compress_worker, args=(list(to_summarize), boundary), daemon=True).start()
        print(f"🧵 [后台上下文压缩启动] 总结 {len(to_summarize)} 条旧对话，保留最近 {len(recent_keep)} 条")
        return True

    def _compress_worker(self, to_summarize: list, boundary: dict):
        """后台总结线程：LLM 摘要（失败则降级话题线索）→ 回写上下文。"""
        try:
            dialog_text = []
            for msg in to_summarize:
                role = "Master (主人)" if msg.get("role") == "user" else "INO"
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    clean_c = re.sub(r'【(?:相关记忆碎片|过往记忆)[^】]*】:.*?\n\n', '', content, flags=re.DOTALL).strip()
                    dialog_text.append(f"{role}: {clean_c[:300]}")

            prompt = f"""请把以下历史对话浓缩成一份中文要点摘要。严格规则：
- 只输出摘要正文本身（2~4 句中文），绝对禁止输出任何分析过程、英文、编号步骤或思维链
- 必须保留 master 提到的重要事实（人名/宠物名/约定/偏好）

历史对话：
{chr(10).join(dialog_text)}

已有前情摘要：
{self.session_summary or '无'}

直接输出更新后的中文摘要正文："""

            summary = ""
            try:
                res = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=2048,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}}  # 推理模型关思维链，防 content 为空
                )
                msg = res.choices[0].message
                summary = (msg.content or "").strip()
                # 推理模型可能把内容放进 reasoning_content → 降级提取
                if not summary:
                    reasoning = getattr(msg, "reasoning_content", None)
                    if reasoning:
                        summary = str(reasoning).strip()[:400]
                summary = re.sub(r'<think>.*?</think>', '', summary, flags=re.DOTALL).strip()
                # 质量闸门：模型若吐的是英文/分析过程而非摘要 → 重写一次
                if summary and (re.match(r'^[A-Za-z]', summary) or 'thinking' in summary.lower()):
                    try:
                        rewrite = self.client.chat.completions.create(
                            model=self.model,
                            messages=[
                                {"role": "user", "content": f"把下面的内容改写成 2~3 句中文摘要，只输出摘要本身，不要任何解释：\n{summary[:800]}"}
                            ],
                            temperature=0.2,
                            max_tokens=1024,
                            extra_body={"chat_template_kwargs": {"enable_thinking": False}}
                        )
                        rewritten = (rewrite.choices[0].message.content or "").strip()
                        if rewritten and not re.match(r'^[A-Za-z]', rewritten):
                            summary = rewritten
                    except Exception:
                        pass
            except Exception as e:
                print(f"[后台压缩 LLM Error]: {e}")

            # LLM 摘要失败/为空 → 降级：用被裁对话的话题线索拼粗摘要（仍不丢关键线索）
            if not summary:
                topics = [m.get("content", "")[:40] for m in to_summarize if m.get("role") == "user"]
                if topics:
                    summary = "【自动整理话题线索】" + "；".join(topics[-6:])

            # 回写：以 boundary 锚点定位，总结期间新增的对话轮次全部保留
            with self._history_lock:
                hist = self.conversation_history
                idx = next((i for i, m in enumerate(hist) if m is boundary), None)
                if idx is None:
                    print("⚠️ [后台压缩] 上下文结构已变化（可能被 /new 重置），放弃本次回写")
                    return
                if summary:
                    self.session_summary = summary
                system_msg = {"role": "system", "content": self._build_system_prompt()}
                self.conversation_history = [system_msg] + hist[idx:]
                print(f"🧹 [后台上下文压缩完成] 当前 {len(self.conversation_history)} 条 | 摘要: {str(summary)[:120]}")
        except Exception as e:
            print(f"[后台压缩 Warning]: {e}")
        finally:
            self._compressing = False

    def reset_conversation(self) -> str:
        """Clear active conversation history and session summary (/new or /reset command)."""
        self.session_summary = ""
        self.conversation_history = [
            {"role": "system", "content": self._build_system_prompt()}
        ]
        print("✨ [对话上下文已重置 (/new)]")
        return "*探頭* 好的！前情對話已全部重置，全新的一頁開始啦～master 今天想跟我聊些什麼呢？"

    def extract_action_and_expression(self, text: str) -> tuple[str, str, str]:
        """
        Extract expression tag ([happy], etc.) and action label (*歪头*, （眨眼）).
        已模块化拆分为 expression_parser.parse_expression（纯函数、前后端共享协议）。
        """
        return parse_expression(text)

    def chat(self, prompt: str, is_user: bool = True, max_iterations: int = 3,
             image_data: Optional[str] = None) -> Dict[str, Any]:
        """
        Agent Main Reasoning & Execution Loop.
        Executes Tool Calls (Function Calling) autonomously and synthesizes character responses.

        image_data: base64 编码的图片字符串（带 data:image/...;base64, 前缀），用于视觉模型识别。

        CRITICAL DESIGN:
        1. Only REAL user conversation turns are recorded in conversation_history as strict user/assistant pairs.
        2. Background/spontaneous prompts do NOT touch or pollute conversation_history.
        3. Automatic compression occurs smoothly when history exceeds 20 turns without arbitrary sliding window drops.
        """
        # Auto-compress: 后台线程异步总结旧对话（不阻塞主对话），总结直接作为上下文注入
        if is_user and len(self.conversation_history) >= 20:
            self.compress_context_async()

        # 每次对话前重建 system prompt，刷新硬件上下文 / 资讯速览等动态数据
        if is_user:
            self._rebuild_system_prompt()

        # 1. Retrieve long-term memories
        memories = self.memory.get_relevant_memories(prompt) if is_user else []
        memory_context = f"【过往记忆（非当前发言，仅供参考）】:\n" + "\n".join(memories) + "\n\n" if memories else ""

        # 2. Prepare message thread
        if is_user:
            # 真实用户消息纯净入库；记忆碎片作为【独立 user 消息】仅注入当轮 messages，
            # 不入库、不污染对话历史与后续压缩摘要（FIX 1.3）
            # 支持图片输入：有 image_data 时 user content 改为数组（text + image_url）
            if image_data:
                user_msg = {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data}},
                ]}
            else:
                user_msg = {"role": "user", "content": prompt}
            self.conversation_history.append(user_msg)
            messages = list(self.conversation_history)
            if memory_context:
                messages.insert(len(messages) - 1, {"role": "user", "content": memory_context})
        else:
            # ── BACKGROUND / SPONTANEOUS PROMPT ──
            # Build an isolated temporary message list: full history context + one-shot system instruction.
            # Never stored in self.conversation_history.
            messages = list(self.conversation_history) + [{"role": "user", "content": prompt}]

        openai_tools = self.tools.get_openai_tools()
        executed_tool_calls = []
        final_text = ""
        # <continue> 多轮自主续跑协议（1.5）
        continue_parts = []
        continue_rounds = 0
        enable_continue = bool(self.autonomy_config.get("enable_continue", True))
        max_continue = int(self.autonomy_config.get("max_continue_turns", 4))
        total_iters = max_iterations + (max_continue if enable_continue else 0)

        print(f"\n[INO Agent 思考触发 ({'用户消息' if is_user else '后台感知'})]: {prompt[:120]}")
        self._chatting = True  # 对话进行中：记忆提取暂缓，避免争抢 LLM

        # Dynamically check active loaded model with 60s cache
        self._ensure_valid_model()
        iteration = 0
        while iteration < total_iters:
            iteration += 1
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=openai_tools if openai_tools else None,
                    temperature=0.7,
                    max_tokens=4096  # 推理模型思考会消耗大量 token（LM Studio 无法关 thinking），必须留足空间
                )
            except Exception as e:
                print(f"[INO Agent LLM Error]: {e}")
                # 1) 重试机制：网络抖动/DNS 波动时先重试 2 次（想办法解决连接，而非切换模式）
                response = None
                for retry_i in range(2):
                    time.sleep(1.5)
                    try:
                        response = self.client.chat.completions.create(
                            model=self.model,
                            messages=messages,
                            tools=openai_tools if openai_tools else None,
                            temperature=0.7,
                            max_tokens=4096
                        )
                        print(f"🔁 [INO Agent LLM Retry] 第 {retry_i + 1} 次重试成功")
                        break
                    except Exception as e2:
                        print(f"🔁 [INO Agent LLM Retry {retry_i + 1}/2 失败]: {e2}")
                if response is not None:
                    choice = response.choices[0]
                    msg = choice.message
                    raw_content = msg.content or ""
                    # 重试成功后直接走工具调用/文本处理（避免重复 try 结构）
                    if msg.tool_calls and len(msg.tool_calls) > 0:
                        messages.append(msg)
                        for tc in msg.tool_calls:
                            fn_name = tc.function.name
                            fn_args = tc.function.arguments
                            print(f"🔧 [INO Agent Native Tool Call -> {fn_name}]: {fn_args}")
                            tool_result = self.tools.execute(fn_name, fn_args)
                            executed_tool_calls.append({
                                "tool": fn_name,
                                "args": fn_args,
                                "result_preview": tool_result[:120]
                            })
                            print(f"✅ [INO Agent Tool Result]: {tool_result[:150]}...")
                            self._record_tool_activity(fn_name, tool_result)
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": tool_result
                            })
                        continue
                    cleaned_text, openclaw_calls = extract_openclaw_tool_calls(raw_content)
                    if openclaw_calls:
                        print(f"🔧 [INO Agent OpenClaw Tool Calls]: {[c['name'] for c in openclaw_calls]}")
                        executed = []
                        for idx, tc in enumerate(openclaw_calls):
                            fn_name = tc["name"]
                            fn_args = tc["arguments"]
                            print(f"🔧 [INO Agent OpenClaw Tool Call -> {fn_name}]: {fn_args}")
                            tool_result = self.tools.execute(fn_name, fn_args)
                            executed_tool_calls.append({
                                "tool": fn_name,
                                "args": fn_args,
                                "result_preview": tool_result[:120]
                            })
                            print(f"✅ [INO Agent Tool Result]: {tool_result[:150]}...")
                            self._record_tool_activity(fn_name, tool_result)
                            executed.append((f"openclaw_{iteration}_{idx}", fn_name, fn_args, tool_result))
                        pseudo_calls = [
                            {"id": tid, "type": "function",
                             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                            for tid, name, args, _ in executed
                        ]
                        messages.append({"role": "assistant", "content": raw_content, "tool_calls": pseudo_calls})
                        for tid, _, _, result in executed:
                            messages.append({"role": "tool", "tool_call_id": tid, "content": result})
                        continue
                    # 重试成功且无工具调用 → 正常文本回复
                    print(f"[INO Agent Final] {raw_content[:200]}")
                    final_text = raw_content
                    break
                # 2) 重试仍失败：仅在「未保存模式」(auto) 时允许自动切换；用户保存的模式永不切换
                if not self.llm_config.get("mode") and self._try_switch_to_deepseek():
                    print("🔄 [LLM Auto] 未保存模式，本地不可达，自动切换 DeepSeek 重试本轮")
                    continue
                # Force refresh model cache on API failure
                self._ensure_valid_model(force=True)
                if not continue_parts:
                    final_text = "哎呀～我的小腦袋剛剛稍微打了個瞌睡餒！master 你剛剛說什麼，可以再跟我說一次嗎？"
                break

            choice = response.choices[0]
            msg = choice.message
            raw_content = msg.content or ""

            # 1. Check Native OpenAI tool_calls
            if msg.tool_calls and len(msg.tool_calls) > 0:
                messages.append(msg)
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    fn_args = tc.function.arguments
                    print(f"🔧 [INO Agent Native Tool Call -> {fn_name}]: {fn_args}")

                    tool_result = self.tools.execute(fn_name, fn_args)
                    executed_tool_calls.append({
                        "tool": fn_name,
                        "args": fn_args,
                        "result_preview": tool_result[:120]
                    })
                    print(f"✅ [INO Agent Tool Result]: {tool_result[:150]}...")
                    self._record_tool_activity(fn_name, tool_result)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result
                    })
                continue

            # 2. Check OpenClaw Style <tool_call> in text content
            cleaned_text, openclaw_calls = extract_openclaw_tool_calls(raw_content)
            if openclaw_calls:
                print(f"🔧 [INO Agent OpenClaw Tool Calls]: {[c['name'] for c in openclaw_calls]}")
                # 统一以标准 assistant.tool_calls + tool 角色回灌（FIX 1.2）
                executed = []
                for idx, tc in enumerate(openclaw_calls):
                    fn_name = tc["name"]
                    fn_args = tc["arguments"]
                    print(f"🔧 [INO Agent OpenClaw Tool Call -> {fn_name}]: {fn_args}")

                    tool_result = self.tools.execute(fn_name, fn_args)
                    executed_tool_calls.append({
                        "tool": fn_name,
                        "args": fn_args,
                        "result_preview": tool_result[:120]
                    })
                    print(f"✅ [INO Agent Tool Result]: {tool_result[:150]}...")
                    self._record_tool_activity(fn_name, tool_result)
                    executed.append((f"openclaw_{iteration}_{idx}", fn_name, fn_args, tool_result))

                pseudo_calls = [
                    {"id": tid, "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                    for tid, name, args, _ in executed
                ]
                messages.append({"role": "assistant", "content": raw_content, "tool_calls": pseudo_calls})
                for tid, _, _, result in executed:
                    messages.append({"role": "tool", "tool_call_id": tid, "content": result})
                continue
            else:
                final_text = raw_content
                # <continue> 多轮自主续跑协议（1.5）：模型可要求继续自主行动
                if enable_continue and "<continue>" in final_text and continue_rounds < max_continue:
                    continue_rounds += 1
                    spoken_part = final_text.replace("<continue>", "").strip()
                    if spoken_part:
                        continue_parts.append(spoken_part)
                    print(f"🔄 [INO Agent 自主续跑] 第 {continue_rounds}/{max_continue} 轮")
                    messages.append({"role": "user", "content": "【自主续跑】你还可以调用工具继续完成目标（如需联网查证可再次调用工具）；如果已完成，直接给出最终回答。"})
                    continue
                break

        # 拼接 <continue> 多轮回复 + 清理残留标记（1.5）
        if continue_parts:
            final_text = "\n".join(continue_parts + [final_text]).strip()
        final_text = re.sub(r'<continue>', '', final_text).strip()

        # Clean <think> and residual <tool_call> tags from final text (模块化: expression_parser)
        final_text = clean_model_text(final_text)

        # If user message got an empty text response, execute 1 quick retry with direct instruction
        if is_user and not final_text.strip():
            print("⚠️ [INO Agent Warning]: Model returned empty text on user prompt, attempting direct retry...")
            try:
                # 裁剪上下文：仅保留 system + 最近 5 条纯文本消息（剔除工具消息，减少干扰）
                safe_msgs = [m for m in messages if m.get("role") in ("system", "user", "assistant") and m.get("content")]
                retry_messages = safe_msgs[:1] + safe_msgs[-5:] + [{"role": "user", "content": "请直接针对上述对话使用可爱、简短的 i no 口吻给出回复（不要思考、不要调用工具，直接回复）："}]
                retry_res = self.client.chat.completions.create(
                    model=self.model,
                    messages=retry_messages,
                    temperature=0.6,
                    max_tokens=2048
                )
                final_text = retry_res.choices[0].message.content or ""
                final_text = clean_model_text(final_text)
            except Exception as e:
                print(f"[INO Agent Retry Error]: {e}")

        # Final empty text fallback
        if not final_text.strip():
            if is_user:
                final_text = "唔……抱歉餒，我剛剛想事情走神了！master 可以再跟我說一次嗎？"
            else:
                final_text = ""

        # ONLY update main conversation_history for real user conversations (pure user/assistant pairs)
        if is_user and final_text.strip():
            self.conversation_history.append({"role": "assistant", "content": final_text})

        # 自动记忆保存（1.3，模块化: memory_config.auto_save）——异步后台线程，不阻塞主流程
        if is_user and final_text.strip() and self.memory_config.get("auto_save", True):
            self._enqueue_memory_save(prompt, final_text)

        # Extract expressions and actions (模块化: expression_parser)
        clean_text, action_label, expression = self.extract_action_and_expression(final_text)
        self._chatting = False  # 对话结束，记忆提取可恢复

        return {
            "raw_text": final_text,
            "clean_speech_text": clean_text,
            "action_label": action_label,
            "expression": expression,
            "tools_called": executed_tool_calls
        }

    def _enqueue_memory_save(self, user_prompt: str, reply: str):
        """异步自动保存本轮对话要点到长期记忆（后台线程，不阻塞主流程）。"""
        with self._memory_save_lock:
            self._memory_save_queue.append({"user": user_prompt[:300], "reply": reply[:300]})
            if self._memory_save_thread_started:
                return
            self._memory_save_thread_started = True
        threading.Thread(target=self._memory_save_worker, daemon=True).start()

    def _memory_save_worker(self):
        while True:
            # 对话进行中时等待（记忆提取 LLM 不与对话争抢，防“走神”/空回复）；最多等 120s 防卡死
            wait_until = time.time() + 120
            while self._chatting and time.time() < wait_until:
                time.sleep(2)
            with self._memory_save_lock:
                if not self._memory_save_queue:
                    self._memory_save_thread_started = False
                    return
                item = self._memory_save_queue.pop(0)
            try:
                fact = f"master 提到: {item['user']}\nINO 回应: {item['reply']}"
                ok = self.memory.add_memory(fact)
                if ok:
                    print("🧠 [记忆自动保存] 已沉淀本轮对话要点")
                else:
                    print("⚠️ [记忆自动保存] 写入失败（embedding/LLM 不可用），本条要点已跳过")
            except Exception as e:
                print(f"[Memory Auto-save Warning]: {e}")


# Compatibility alias for existing references
CoreAgent = INOAgent
