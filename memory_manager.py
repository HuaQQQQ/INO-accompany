import time
import threading
import gc
import requests

from mem0 import Memory


class MemoryManager:
    """长期记忆门面（Mem0 + Qdrant）。

    资源策略（按需加载 / 空闲自动卸载）：
    - Mem0 实例（含 embedding 模型）懒加载：首次 add/search 时才构建
    - 空闲超过 idle_unload_minutes 分钟自动卸载（释放 embedding 模型内存）
    - embedding provider 支持：
        fastembed  = 本地 ONNX 轻量模型（默认推荐，出门可用、不占局域网依赖）
        lmstudio   = 局域网 LM Studio 的 embedding 模型（现状行为）
        auto       = 优先 fastembed，失败自动回退 lmstudio
    """

    def __init__(self, config=None):
        self.llm_config = config.get("llm", {}) if config else {}
        memory_config = config.get("memory", {}) if config else {}
        self.base_url = self.llm_config.get("base_url", "http://127.0.0.1:1234/v1")
        self.llm_model = self.llm_config.get("model", "local-model")

        embed_cfg = memory_config.get("embedding", {})
        self.provider = str(embed_cfg.get("provider", "auto")).lower()
        self.local_model = embed_cfg.get("model", "BAAI/bge-small-zh-v1.5")
        self.idle_unload_minutes = max(1, int(embed_cfg.get("idle_unload_minutes", 30)))

        self.user_id = "default_user"
        self.memory = None  # 懒加载：首次记忆操作时构建
        self._last_use = 0.0
        self._lock = threading.Lock()
        self._unload_thread_started = False

        # 启动时预热构建一次（报告就绪状态），此后按需卸载/重载
        self._ensure_memory()
        self._start_unload_watchdog()

    # ── 构建 / 卸载 ─────────────────────────────────────

    def _build_memory(self):
        """按 provider 构建 Mem0 实例（embedding 本地/局域网）。"""
        if self.provider in ("fastembed", "auto"):
            try:
                return self._build_local_memory()
            except Exception as e:
                if self.provider == "fastembed":
                    raise
                print(f"⚠️ [MemoryManager] 本地 embedding 初始化失败，回退局域网 LM Studio embedding: {e}")
        return self._build_lmstudio_memory()

    def _build_local_memory(self):
        """本地 ONNX embedding（fastembed，轻量、出门可用、零局域网依赖）。"""
        print(f"🧠 [MemoryManager] 使用本地 embedding: {self.local_model} (fastembed/ONNX)")
        # 动态获取模型向量维度（Qdrant 集合必须与 embedding 维度一致，默认 1536 会不匹配）
        dims = self._probe_local_dims(self.local_model)
        mem0_config = {
            "llm": self._llm_config(),
            "custom_instructions": "提取长期记忆时请始终使用中文输出，保留关键人名与专有名词。",
            "embedder": {
                "provider": "fastembed",
                "config": {"model": self.local_model}
            },
            # 与局域网 embedding 维度不同，独立集合避免维度冲突
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "path": "/tmp/qdrant",
                    "collection_name": "mem0_local",
                    "embedding_model_dims": dims
                }
            }
        }
        return Memory.from_config(mem0_config)

    def _llm_config(self) -> dict:
        """LLM 配置：优先局域网 LM Studio（lmstudio provider 强制 text 格式）；
        不可达时自动切换备用 API（如 DeepSeek，openai provider 兼容 json_object）。"""
        fb = self.llm_config.get("fallback", {})
        if fb.get("enabled", True) and not self._probe_llm(self.base_url):
            key = self._load_api_key(fb.get("api_key_file", ""))
            if key:
                print(f"⚠️ [MemoryManager] 局域网 LLM 不可达，事实提取改用备用 API: {fb.get('base_url')}")
                return {
                    "provider": "openai",
                    "config": {
                        "model": fb.get("model", "deepseek-chat"),
                        "api_key": key,
                        "openai_base_url": fb.get("base_url", "https://api.deepseek.com")
                    }
                }
        return {
            "provider": "lmstudio",
            "config": {
                "model": self.llm_model,
                "lmstudio_base_url": self.base_url,
                "lmstudio_response_format": {"type": "text"}
            }
        }

    @staticmethod
    def _probe_llm(base_url: str, timeout: float = 3.0) -> bool:
        """探测 LLM 服务是否可达。"""
        try:
            r = requests.get(f"{base_url}/models", timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    @staticmethod
    def _load_api_key(path: str) -> str:
        """从本地文件读取 API 密钥（首行非注释内容），不落盘到配置文件。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
        except Exception:
            return ""

    @staticmethod
    def _probe_local_dims(model_name: str) -> int:
        """获取本地 fastembed 模型的向量维度（失败时回退常见值 512）。"""
        try:
            from fastembed import TextEmbedding
            return int(TextEmbedding(model_name=model_name).embedding_size)
        except Exception as e:
            print(f"[MemoryManager] 维度探测失败（回退 512）: {e}")
            return 512

    def _build_lmstudio_memory(self):
        """局域网 LM Studio embedding（原方案，作为 fastembed 的回退）。"""
        embed_model = self._detect_embedding_model(self.base_url)
        if not embed_model:
            print("⚠️ [MemoryManager] 未检测到局域网 LM Studio embedding 模型！请先加载 embedding 模型")
            print("⚠️    （如 jina-embeddings / text-embedding-3-small），否则记忆系统将完全失效！")
        else:
            print(f"🧠 [MemoryManager] 使用局域网 embedding: {embed_model}")
        mem0_config = {
            "llm": self._llm_config(),
            "custom_instructions": "提取长期记忆时请始终使用中文输出，保留关键人名与专有名词。",
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": embed_model or "text-embedding-3-small",
                    "api_key": "not-needed",
                    "openai_base_url": self.base_url
                }
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "path": "/tmp/qdrant",
                    "collection_name": "mem0"
                }
            }
        }
        return Memory.from_config(mem0_config)

    def _ensure_memory(self):
        """懒加载：确保 Mem0 实例可用；返回是否就绪。"""
        with self._lock:
            if self.memory is not None:
                self._last_use = time.time()
                return True
            try:
                self.memory = self._build_memory()
                self._last_use = time.time()
                print("🧠 [MemoryManager] 长期记忆系统已就绪 (Mem0)")
                return True
            except Exception as e:
                self.memory = None
                print(f"⚠️ [MemoryManager] 长期记忆未启用（初始化失败）: {e}")
                print("⚠️   请确认 LM Studio 可连接（LLM 用于事实提取），或检查本地 embedding 依赖")
                return False

    def _unload_memory(self):
        """空闲超时卸载整个 Mem0 实例（含 embedding 模型），释放内存。"""
        with self._lock:
            if self.memory is None:
                return
            self.memory = None
            gc.collect()
            print(f"🧹 [MemoryManager] 已卸载（空闲 {self.idle_unload_minutes} 分钟），下次记忆操作时自动重新加载")

    def _start_unload_watchdog(self):
        if self._unload_thread_started:
            return
        self._unload_thread_started = True
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _watchdog(self):
        while True:
            time.sleep(60)
            try:
                if self.memory is not None and (time.time() - self._last_use) > self.idle_unload_minutes * 60:
                    self._unload_memory()
            except Exception:
                pass

    @staticmethod
    def _detect_embedding_model(base_url: str):
        """探测 LM Studio /v1/models 中第一个 embedding 类模型；无则返回 None。"""
        try:
            res = requests.get(f"{base_url}/models", timeout=5)
            if res.status_code != 200:
                return None
            models = res.json().get("data", [])
            for m in models:
                mid = str(m.get("id", "")).lower()
                if any(k in mid for k in ("embed", "bge", "nomic", "gte", "e5-", "minilm")):
                    return m["id"]
        except Exception as e:
            print(f"[MemoryManager] embedding 探测失败: {e}")
        return None

    # ── 对外 API ────────────────────────────────────────

    def add_memory(self, text: str) -> bool:
        """写入一条长期记忆；返回是否成功（供调用方准确上报）。"""
        if not self._ensure_memory():
            return False
        try:
            self.memory.add(text, user_id=self.user_id)
            return True
        except Exception as e:
            print(f"Memory add error: {e}")
            return False

    def get_relevant_memories(self, query: str) -> list:
        if not self._ensure_memory():
            return []

        results = None
        try:
            results = self.memory.search(query, filters={"user_id": self.user_id})
        except Exception:
            try:
                results = self.memory.search(query, user_id=self.user_id)
            except Exception as e:
                print(f"Memory search warning: {e}")
                return []

        if not results:
            return []

        if isinstance(results, dict) and "results" in results:
            return [res['memory'] for res in results['results']]

        if isinstance(results, list):
            return [res.get('memory', '') for res in results if isinstance(res, dict) and 'memory' in res]

        return []

    def list_all_memories(self) -> list:
        """列出全部长期记忆（含 id，供清理/审查）。"""
        if not self._ensure_memory():
            return []
        try:
            try:
                res = self.memory.get_all(filters={"user_id": self.user_id})
            except TypeError:
                res = self.memory.get_all(user_id=self.user_id)
            items = res.get("results", res) if isinstance(res, dict) else res
            return [{"id": m.get("id"), "memory": m.get("memory", "")}
                    for m in (items or []) if isinstance(m, dict)]
        except Exception as e:
            print(f"Memory list error: {e}")
            return []

    def delete_memory(self, memory_id: str) -> bool:
        """按 id 删除一条长期记忆。"""
        if not self._ensure_memory():
            return False
        try:
            self.memory.delete(memory_id)
            return True
        except Exception as e:
            print(f"Memory delete error: {e}")
            return False
