import os
import sys
import time
import threading
import webbrowser
import random
import subprocess
import socket
import yaml

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from flask import Flask, render_template, jsonify, request
from core_agent import INOAgent
from expression_parser import EXPRESSIONS, ACTION_MAP, KAOMOJI_MAP, EXPRESSION_LABELS
import autonomy

app = Flask(__name__, template_folder="templates")
app.config["TEMPLATES_AUTO_RELOAD"] = True  # 前端模板改动无需重启服务器

# 单实例锁引用（见 _ensure_single_instance；进程退出时自动释放）
_INSTANCE_LOCK_FILE = None
_INSTANCE_MUTEX = None

# Global state
agent: INOAgent = None
last_spontaneous_prompt_time = 0
is_thinking = False
current_expression = "idle"
chat_version = 0  # Bumped on every chat list change for efficient frontend polling

# Thread safety: prevents concurrent LLM calls from corrupting conversation state
_agent_lock = threading.Lock()

shared_chat_history = [
    {
        "sender": "ino",
        "text": "Master！我是 INO，我就在桌面副屏陪伴着你喔～随时可以跟我聊天、语音对话，或者让我自主联网查询最新的科技动态与任何新资讯！",
        "expression": "happy",
        "action_label": "微笑",
        "timestamp": time.time()
    }
]


def enrich_news_request(prompt: str) -> str:
    """若用户请求指向某条具体资讯（讲讲/介绍…），自动搜索真实链接并附带详细正文，
    让 INO 能基于正文详细讲解而非只靠标题。"""
    import re as _re
    m = _re.search(r'(?:讲讲|介绍|说说|看看|说一下|讲一下|了解一下|介绍一下)[^：:]{0,6}[：:]\s*(.+)', prompt)
    if not m:
        return prompt
    title_key = m.group(1).strip()
    if len(title_key) < 4:
        return prompt
    try:
        # 1) 匹配侧边栏缓存里的新闻标题（缩小搜索范围）
        news = agent.news_fetcher.get_news()
        matched = [n for n in news if title_key[:10] in n.get('title', '') or n.get('title', '')[:12] in prompt]
        target_title = matched[0]['title'] if matched else title_key
        # 2) 搜索该标题的真实原文链接（DDG 等），读取正文
        search_res = agent.news_fetcher.search_web(target_title)
        real_links = _re.findall(r'\((https?://[^)]+)\)', search_res)
        real_links = [l for l in real_links if 'news.google.com' not in l]
        content = ''
        if real_links:
            content = agent.news_fetcher.fetch_article_content(real_links[0])
        if not content or len(content) < 80:
            return prompt
        # 新闻偏好学习：用户点击讲解 → 记录兴趣关键词，后续排序加权
        try:
            agent.news_fetcher.record_interest(target_title)
        except Exception as e:
            print(f"[NewsPrefs] 记录失败: {e}")
        print(f"📰 [News Enrich] 已附加正文（{len(content)} 字符）: {target_title[:40]}")
        return f"{prompt}\n\n【以下为这条资讯的详细正文内容，请根据正文给 Master 详细讲解，不要只复述标题】\n{content}"
    except Exception as e:
        print(f"[News Enrich Warning]: {e}")
        return prompt


def init_agent():
    global agent
    if agent is None:
        agent = INOAgent()


spontaneous_interrupted = False
recent_spontaneous_replies = []  # Tracks recent background spontaneous replies to prevent topic repetition
user_request_pending = False  # 用户消息排队中：后台行动让路，防止抢答/空回复

# ── 话题续聊机制：话题结束后主动延续或换新话题 ──
last_user_msg_time = 0.0        # 用户最后一次发言时间
last_user_topic = ""            # 用户最后一条消息（话题锚点）
last_followup_topic = ""        # 已续聊过的话题（防重复续聊同一句）


def should_followup(last_user_topic, last_followup_topic, state, idle_sec, followup_cooldown, since_last_spontaneous):
    """话题续聊触发判定（纯函数：后台循环与冒烟测试共用同一判定）。
    条件：有话题锚点、未续聊过该话题、当前在场状态、空闲时间落在
    冷却~900s 窗口内、且距上次自主发言已超过冷却。"""
    if not last_user_topic:
        return False
    if last_followup_topic == last_user_topic:
        return False
    if state not in ("active", "desk_companion", "returned"):
        return False
    return followup_cooldown <= idle_sec <= 900 and since_last_spontaneous > followup_cooldown

def trigger_agent_speech(context_prompt, is_user=False, image_data=None):
    """
    Trigger INO Agent thinking, tool execution loop, and speech synthesis.
    Delegates all tool decisions and persona reasoning to INOAgent Brain.
    Supports user interrupt preemption: user input immediately cuts off background prompts & speech.
    image_data: base64 编码的图片字符串（data:image/...;base64,），用于视觉模型识别。
    """
    global agent, shared_chat_history, last_spontaneous_prompt_time
    global is_thinking, current_expression, chat_version, spontaneous_interrupted
    global user_request_pending

    if not agent or not context_prompt:
        return ""

    # If it's a background spontaneous prompt, drop it if busy or interrupted
    if not is_user:
        if _agent_lock.locked() or is_thinking or spontaneous_interrupted:
            return ""

    if is_user:
        spontaneous_interrupted = True
        if agent and agent.voice:
            agent.voice.stop_speech()

    with _agent_lock:
        if not is_user and spontaneous_interrupted:
            return ""

        if is_user:
            spontaneous_interrupted = False
            recent_spontaneous_replies.clear()
            user_request_pending = False  # 用户消息开始处理，解除后台让路

        is_thinking = True
        current_expression = "think"

        # Add thinking indicator
        thinking_item = {
            "sender": "ino",
            "text": "思考中...",
            "is_thinking": True,
            "expression": "think",
            "timestamp": time.time()
        }
        shared_chat_history.append(thinking_item)
        chat_version += 1

        try:
            # 1. Execute Agent reasoning and tool calling loop
            result = agent.chat(context_prompt, is_user=is_user, image_data=image_data)
            raw_text = result.get("raw_text", "")
            clean_speech = result.get("clean_speech_text", "")
            action_label = result.get("action_label", "")
            expression = result.get("expression", "idle")

            # Check if user interrupted while background prompt was generating
            if not is_user:
                if spontaneous_interrupted:
                    print("[Background spontaneous speech cancelled due to user interruption]")
                    shared_chat_history = [m for m in shared_chat_history if not m.get("is_thinking")]
                    return ""
                last_spontaneous_prompt_time = time.time()

            # 空回复保护：LLM 返回空文本时绝不留下空气泡
            if not raw_text or not raw_text.strip():
                shared_chat_history = [m for m in shared_chat_history if not m.get("is_thinking")]
                chat_version += 1
                print("[空回复已丢弃，不写入聊天记录]")
                return ""

            # Track recent spontaneous replies to prevent topic repetition when user hasn't replied
            if not is_user:
                recent_spontaneous_replies.append(raw_text)
                if len(recent_spontaneous_replies) > 5:
                    recent_spontaneous_replies.pop(0)

            # Cleanly remove all thinking placeholders and append final reply
            shared_chat_history = [m for m in shared_chat_history if not m.get("is_thinking")]
            shared_chat_history.append({
                "sender": "ino",
                "text": raw_text,
                "expression": expression,
                "action_label": action_label,
                "timestamp": time.time()
            })

            current_expression = expression
            chat_version += 1

            # 2. Asynchronous voice playback with dynamic tone modulation
            agent.voice.speak(clean_speech, expression=expression)

            return clean_speech

        except Exception as e:
            print(f"[INO Agent Dialog Error]: {e}")
            shared_chat_history = [m for m in shared_chat_history if not m.get("is_thinking")]
            # 配对保护：用户消息已入库但回复异常时，补一条 assistant 占位，
            # 防止连续两条 user 消息破坏上下文配对（导致后续压缩/回复错乱）
            if is_user and agent and agent.conversation_history and agent.conversation_history[-1].get("role") == "user":
                agent.conversation_history.append({"role": "assistant", "content": "(回复被意外中断)"})
            err_reply = "哎呀～我的小腦袋剛剛稍微打了個瞌睡餒！"
            shared_chat_history.append({
                "sender": "ino",
                "text": err_reply,
                "expression": "idle",
                "action_label": "",
                "timestamp": time.time()
            })
            current_expression = "idle"
            chat_version += 1
            return err_reply
        finally:
            is_thinking = False
            current_expression = "idle"
            if agent:
                agent._chatting = False  # 异常退出时也复位对话标记（防记忆提取永久等待）


def build_spontaneous_prompt(state: str, agent: INOAgent) -> str:
    """Pure random dice-roll driven prompt builder for maximum natural desk companion spontaneity."""
    global recent_spontaneous_replies

    context_parts = []
    if agent.session_summary:
        context_parts.append(f"【前情要点摘要】: {agent.session_summary}")

    # Extract last 2 real user dialogue turns
    recent_msgs = [m for m in agent.conversation_history if m.get("role") in ["user", "assistant"]]
    if recent_msgs:
        recent_snippets = []
        for m in recent_msgs[-2:]:
            role = "master" if m["role"] == "user" else "INO"
            text = m.get("content", "")[:120]
            recent_snippets.append(f"  {role}: {text}")
        context_parts.append("【最近用户对话】:\n" + "\n".join(recent_snippets))

    # Anti-repetition context
    if recent_spontaneous_replies:
        spont_lines = [f"  - {text[:80]}" for text in recent_spontaneous_replies[-3:]]
        context_parts.append("【你近期发出的搭话（master 尚未回应，绝不重复以下切入点）】:\n" + "\n".join(spont_lines))

    context_str = "\n\n".join(context_parts)

    # 🎲 Pure Random Dice Roll Selection (Zero LLM decision burden)
    dice = random.random()

    # Rule 1: Special case for 'returned' state (Master just came back to desk)
    if state == "returned":
        instruction = "🎲 【纯随机指令 - 刚回到桌前】: master 刚刚回到电脑桌前，请用极简短（1句）、超级萌的日中混搭口吻打个招呼（例：*探頭* おかえり～ master！或 欢迎回来呀 master～）。严禁背新闻或长篇大论！"
    elif dice < 0.40:
        # Branch 1: 40% Chance -> Pure Natural Greeting & Cute Desk Companion Interaction
        instruction = "🎲 【纯随机指令 - 自由搭话/撒娇】: 用 1 句话极其自然地跟 master 打个招呼、探个头、伸个懒腰或随性撒个娇，完全自由发挥！"
    elif dice < 0.70:
        # Branch 2: 30% Chance -> Warm Daily Care
        instruction = "🎲 【纯随机指令 - 温情关怀】: 用 1 句话极其温柔地提醒 master 喝水、活动一下肩膀、或润润眼睛，非常轻柔贴心。"
    elif dice < 0.85:
        # Branch 3: 15% Chance -> Funny Desk Pet Monologue
        instruction = "🎲 【纯随机指令 - 桌宠吐槽/发呆】: 用 1 句话随性吐槽一下你在副屏上的日常（如 CPU 发烫、搬运数据累了、或者副屏风扇呼哧呼哧的），可爱又搞怪。"
    elif dice < 0.95:
        # Branch 4: 10% Chance -> Quiet Companion & Encouragement
        instruction = "🎲 【纯随机指令 - 默默陪伴】: 用 1 句话表达你一直在副屏上默默看着 master 忙碌，给 master 送上一句轻松温暖的鼓励。"
    else:
        # Branch 5: 5% Chance -> Rare News Curiosity Drop
        news_item = ""
        try:
            all_news = agent.news_fetcher.get_news()
            if all_news:
                sampled = random.choice(all_news)
                news_item = f"\n副屏资讯参考: [{sampled.get('query','科技')}] {sampled['title']}"
        except Exception:
            pass
        instruction = f"🎲 【纯随机指令 - 好奇分享】: 用 1 句话极其自然地提起一个你刚刚在副屏看到的科技新鲜事或硬件动态（例：欸 master，我刚刚翻到了一个很有趣的消息喔！）。{news_item}"

    anti_dup = "\n⚠️ 必须完全换一个全新的表达方式，严禁重复上述已用过的句子和切入点！" if len(recent_spontaneous_replies) > 0 else ""

    return f"""[系统感知搭话提醒]
{context_str}

{instruction}{anti_dup}
请用可爱的 INO 口吻说出这 1 句话（只说一句话，适合语音朗读）："""


def agent_background_loop():
    """Background low-frequency perception and spontaneous presence companion loop."""
    global agent, last_spontaneous_prompt_time, is_thinking, spontaneous_interrupted, last_followup_topic
    if agent is None:
        return
    print("INO 后台低频感知守护线程已启动 (开启 10 分钟自主冷却机制)...")
    check_interval = agent.presence_config.get("check_interval", 5.0)
    active_prob = agent.presence_config.get("active_prompt_chance", 0.005)
    desk_prob = agent.presence_config.get("desk_companion_prompt_chance", 0.002)
    min_cooldown = agent.presence_config.get("min_prompt_interval", 600)

    while True:
        try:
            time.sleep(check_interval)
            
            # Skip if agent is busy with user
            if _agent_lock.locked() or is_thinking or spontaneous_interrupted or user_request_pending:
                continue

            state = agent.presence_detector.evaluate_state()
            now = time.time()

            # ── 话题续聊：上一个话题结束后 master 一段时间没回应，主动续聊或换新话题 ──
            idle_sec = now - last_user_msg_time
            followup_cooldown = agent.presence_config.get("followup_min_interval", 150)
            if should_followup(
                last_user_topic, last_followup_topic, state,
                idle_sec=idle_sec,
                followup_cooldown=followup_cooldown,
                since_last_spontaneous=now - last_spontaneous_prompt_time,
            ):
                last_followup_topic = last_user_topic
                mins = int(idle_sec // 60)
                followup_prompt = (
                    f"[话题续聊] 你和 master 刚才聊到的话题是：「{last_user_topic}」\n"
                    f"master 已经 {mins} 分钟没回应了。请像贴心好友接住话头一样：自然地续上这个话题"
                    "（关心后续进展、补充你的想法、问 master 后来怎么样了），或者顺畅地开启一个你想聊的新话题。"
                    "只说 1 句话，轻松自然，不要质问语气："
                )
                print(f"💬 [话题续聊触发] idle={mins}min topic={last_user_topic[:30]}")
                trigger_agent_speech(followup_prompt, is_user=False)
                continue

            should_trigger = False
            if state == "returned" and (now - last_spontaneous_prompt_time > min_cooldown):
                should_trigger = True
            elif state == "active" and (now - last_spontaneous_prompt_time > min_cooldown) and random.random() < active_prob:
                should_trigger = True
            elif state == "desk_companion" and (now - last_spontaneous_prompt_time > min_cooldown) and random.random() < desk_prob:
                should_trigger = True

            if should_trigger:
                # 自主行动优先分支（1.5）：概率触发"做实事"而非闲聊
                if autonomy.should_act(agent.autonomy_config) and autonomy.can_run('background_action'):
                    action_prompt = autonomy.build_action_prompt(agent)
                    autonomy.log_activity('background_action', action_prompt[:100])
                    print("🎯 [INO 自主行动] 本次后台触发走『行动优先』模式")
                    trigger_agent_speech(action_prompt, is_user=False)
                else:
                    spontaneous_prompt = build_spontaneous_prompt(state, agent)
                    trigger_agent_speech(spontaneous_prompt, is_user=False)

        except Exception as e:
            print(f"Background loop error: {e}")


def save_voice_config(**kwargs):
    """Safely persist updated voice/system settings to config.yaml."""
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        if "voice" not in cfg:
            cfg["voice"] = {}
        for k, v in kwargs.items():
            cfg["voice"][k] = v
        with open("config.yaml", "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)
        print(f"[Config Saved] 成功持久化设置到 config.yaml: {kwargs}")
    except Exception as e:
        print(f"[Config Save Error]: {e}")


# ── Flask Routes ──────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def get_status():
    global agent, is_thinking, current_expression
    if not agent:
        return jsonify({"status": "initializing"})

    detector = agent.presence_detector
    state = detector.last_state

    state_desc_map = {
        "active": "笔记本使用活跃中",
        "desk_companion": "桌面副屏陪伴模式 (Master 在桌前忙碌主电脑)",
        "immersive": "全屏沉浸免打扰",
        "away": "未在桌前 / 离开状态",
        "returned": "刚刚重返电脑桌前"
    }

    return jsonify({
        "state": state,
        "state_desc": state_desc_map.get(state, state),
        "idle_seconds": round(detector.get_input_idle_seconds(), 1),
        "primary_pc_online": detector.is_primary_pc_online(),
        "camera_human_detected": detector.is_human_present_by_camera(),
        "audio_playing": detector.is_audio_playing(),
        "is_locked": detector.is_session_locked(),
        "is_thinking": is_thinking,
        "volume": getattr(agent.voice, 'volume', 0.3) if agent else 0.3,
        "is_muted": getattr(agent.voice, 'is_muted', False) if agent else False,
        "voice_lang_mode": getattr(agent.voice, 'voice_lang_mode', 'ja') if agent else 'ja',
        "speaker_id": agent.voice.speaker_id if agent else 16,
        "speed_scale": getattr(agent.voice, 'speed_scale', 1.0) if agent else 1.0,
        "use_voicevox": True,
        "model": agent.model,
        "llm_mode": getattr(agent, '_llm_mode', 'local') if agent else 'local',
        "expression": current_expression,
        "tools_count": len(agent.tools.list_tools()) if agent else 0
    })


@app.route("/api/expressions", methods=["GET"])
def get_expressions():
    """表情协议端点：前后端共享单一事实源（expression_parser）。"""
    return jsonify({
        "expressions": EXPRESSIONS,
        "action_map": ACTION_MAP,
        "kaomoji_map": [{"pattern": p, "expression": e} for p, e in KAOMOJI_MAP],
        "labels": EXPRESSION_LABELS,
        "updated_at": int(time.time())
    })


@app.route("/api/news", methods=["GET"])
def get_news():
    global agent
    if not agent:
        return jsonify([])
    news = agent.news_fetcher.get_news()
    return jsonify(news)


@app.route("/api/news/refresh", methods=["POST"])
def refresh_news():
    global agent
    if not agent:
        return jsonify({"error": "Agent not initialized"}), 500
    news = agent.news_fetcher.get_news(force_refresh=True)
    return jsonify({"status": "ok", "count": len(news), "news": news})


@app.route("/api/news/search", methods=["POST"])
def search_and_pin_news():
    global agent
    if not agent:
        return jsonify({"error": "Agent not initialized"}), 500
    data = request.json or {}
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"error": "Missing query"}), 400

    items = agent.news_fetcher.update_sidebar_with_search(query)
    all_news = agent.news_fetcher.get_news()
    return jsonify({"status": "ok", "query": query, "count": len(items), "news": all_news})


@app.route("/api/model/refresh", methods=["POST"])
def refresh_model():
    global agent
    if not agent:
        return jsonify({"error": "Agent not initialized"}), 500
    agent._ensure_valid_model(force=True)
    return jsonify({"status": "ok", "model": agent.model})


@app.route("/api/model/switch", methods=["POST"])
def switch_model():
    """手动强制切换模型通道：deepseek(备用 API) | local(局域网 LM Studio)。"""
    global agent
    if not agent:
        return jsonify({"error": "Agent not initialized"}), 500
    data = request.json or {}
    mode = (data.get("mode") or "").strip().lower()
    if mode not in ("deepseek", "local"):
        return jsonify({"error": "mode 必须是 deepseek 或 local"}), 400
    ok, msg = agent.switch_llm_mode(mode)
    return jsonify({
        "status": "ok" if ok else "error",
        "message": msg,
        "model": agent.model,
        "llm_mode": agent._llm_mode,
    }), (200 if ok else 409)


@app.route("/api/system/restart", methods=["POST"])
def restart_system():
    """热更新：自动重启服务加载最新代码（无需手动杀进程）。
    流程：语法预检 → 后台 spawn 独立新进程（日志写 ino_hot.log）→ 本进程延迟退出。"""
    global agent
    # 1) 语法预检（防止改坏代码导致服务彻底起不来）
    try:
        import py_compile
        for f in ("app.py", "core_agent.py", "news_fetcher.py", "news_sources.py", "dns_resolver.py"):
            if os.path.exists(f):
                py_compile.compile(f, doraise=True)
    except Exception as e:
        return jsonify({"status": "error", "message": f"语法预检失败，已拒绝重启: {e}"}), 400

    log_path = os.path.join(os.getcwd(), "ino_hot.log")

    def _do_restart():
        time.sleep(2.0)  # 确保客户端完整收到响应后再开始重启
        try:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            # 中间人：负责强杀本进程（沙箱下 os._exit 不可靠）+ 等端口/记忆锁释放 + 启动新服务
            subprocess.Popen(
                [sys.executable, "hot_launcher.py", str(os.getpid())],
                cwd=os.getcwd(),
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            print("[HotReload] 中间人已 spawn，将由 hot_launcher 接管重启流程")
        except Exception as e:
            print(f"[HotReload] spawn 失败: {e}")

    threading.Thread(target=_do_restart, daemon=True).start()
    print("[HotReload] 热更新已触发")
    return jsonify({"status": "ok", "message": "热更新已触发，服务将自动重启（约 15 秒），日志见 ino_hot.log"})


@app.route("/api/system/shutdown", methods=["POST"])
def shutdown_system():
    global agent
    print("⚠️ 收到网页端退出指令，正在终止 INO Companion 及其相关后台线程进程...")

    # Stop Voicevox engine process safely
    if agent and agent.voice:
        try:
            agent.voice.stop_engine()
        except Exception as e:
            print(f"Error stopping voice engine: {e}")

    def kill_process_tree():
        time.sleep(0.8)
        print("👋 INO Companion 进程已安全退出。")
        os._exit(0)

    threading.Thread(target=kill_process_tree, daemon=True).start()
    return jsonify({"status": "ok", "message": "INOCompanion 正在关闭..."})




@app.route("/api/chat/history", methods=["GET"])
def chat_history():
    global shared_chat_history, chat_version
    return jsonify({"v": chat_version, "messages": shared_chat_history})


@app.route("/api/debug/state", methods=["GET"])
def debug_state():
    """调试端点：对话上下文与语音引擎内部状态快照（供多轮回归验证使用）。"""
    if not agent:
        return jsonify({"status": "initializing"})
    hist = agent.conversation_history
    roles = [m.get("role") for m in hist]
    # 校验 user/assistant 是否严格交替（压缩裁剪可能破坏配对）
    pair_ok = True
    prev = None
    for r in roles[1:]:
        if r in ("user", "assistant"):
            if r == prev:
                pair_ok = False
                break
            prev = r
    voice = agent.voice
    return jsonify({
        "history_len": len(hist),
        "roles": roles,
        "pair_alternating": pair_ok,
        "session_summary": agent.session_summary,
        "summary_len": len(agent.session_summary or ""),
        "voice": {
            "queue_size": voice._speech_queue.qsize(),
            "worker_alive": voice._speech_thread.is_alive(),
            "playback_locked": voice._playback_lock.locked(),
            "play_gen": voice._play_gen,
            "buffer_samples": int(len(voice.player._buffer)),
            "player_finished": voice.player._finished,
            "stream_active": bool(voice.player._stream and voice.player._stream.active),
        },
        "is_thinking": is_thinking,
    })


@app.route("/api/debug/compress", methods=["POST"])
def debug_compress():
    """调试端点：手动触发后台异步上下文压缩（验证总结回写链路）。"""
    if not agent:
        return jsonify({"error": "Agent not initialized"}), 500
    started = agent.compress_context_async()
    return jsonify({"started": started, "history_len": len(agent.conversation_history),
                    "compressing": agent._compressing})


@app.route("/api/debug/memories", methods=["GET"])
def debug_memories():
    """调试端点：列出全部长期记忆（含 id）。"""
    if not agent:
        return jsonify({"error": "Agent not initialized"}), 500
    return jsonify({"memories": agent.memory.list_all_memories()})


@app.route("/api/debug/memories/purge", methods=["POST"])
def debug_memories_purge():
    """调试端点：删除内容包含任一关键词的长期记忆（清理测试污染）。"""
    if not agent:
        return jsonify({"error": "Agent not initialized"}), 500
    data = request.get_json() or {}
    keywords = data.get("keywords", [])
    if not keywords:
        return jsonify({"error": "Missing keywords"}), 400
    deleted = []
    for m in agent.memory.list_all_memories():
        text = m.get("memory", "")
        if any(k in text for k in keywords):
            if agent.memory.delete_memory(m["id"]):
                deleted.append(text[:60])
    return jsonify({"deleted_count": len(deleted), "deleted": deleted})


@app.route("/api/hardware", methods=["GET"])
def get_hardware():
    """硬件状态：CPU/内存占用 + NVIDIA 显卡温度/占用/显存（供侧边栏与 INO 感知）。"""
    hw = {"cpu_percent": None, "mem_percent": None, "mem_used_gb": None,
          "mem_total_gb": None, "gpu": None}
    try:
        import psutil
        hw["cpu_percent"] = round(psutil.cpu_percent(interval=None), 1)
        mem = psutil.virtual_memory()
        hw["mem_percent"] = round(mem.percent, 1)
        hw["mem_used_gb"] = round(mem.used / 1024 ** 3, 1)
        hw["mem_total_gb"] = round(mem.total / 1024 ** 3, 1)
    except Exception as e:
        print(f"[Hardware] CPU 读取失败: {e}")
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode == 0 and r.stdout.strip():
            p = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
            if len(p) >= 5:
                hw["gpu"] = {"name": p[0], "temp_c": float(p[1]),
                             "util_percent": float(p[2]),
                             "vram_used_mb": int(p[3]), "vram_total_mb": int(p[4])}
    except Exception as e:
        print(f"[Hardware] GPU 读取失败: {e}")
    return jsonify(hw)


@app.route("/api/chat/send", methods=["POST"])
def chat_send():
    """Non-blocking: immediately returns 202 and processes Agent in background."""
    global shared_chat_history, chat_version, spontaneous_interrupted, agent
    global last_user_msg_time, last_user_topic
    data = request.get_json() or {}
    user_msg = data.get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "Empty message"}), 400
    # 防御：拦截乱码/问号串消息（编码损坏的 UTF-8 中文会变成大量 '?'，语音识别噪音也常产出）
    q_count = user_msg.count('?') + user_msg.count('？')
    if q_count >= 3 and q_count / max(len(user_msg), 1) > 0.5:
        print(f"[ChatGuard] 拦截疑似乱码消息: {user_msg[:40]!r}")
        return jsonify({"error": "Garbled message"}), 400
    import re as _chat_re  # 局部导入（模块无全局 re）
    if len(_chat_re.findall(r'[?？！!。，,、\s]', user_msg)) >= 2 and not _chat_re.search(r'[\u4e00-\u9fa5a-zA-Z0-9]', user_msg):
        print(f"[ChatGuard] 拦截纯符号消息: {user_msg[:40]!r}")
        return jsonify({"error": "Invalid message"}), 400

    print(f"\n[收到 Master 消息/语音]: {user_msg}")
    # 支持图片输入（视觉模型识别）
    image_data = data.get("image_data")  # base64 字符串，带 data:image/...;base64, 前缀
    image_url = None
    if image_data:
        print(f"[图片输入] 附带图片 {len(image_data)} 字符")
        # 保存图片到磁盘（static/uploads/），供历史渲染调用
        try:
            import re as _img_re, base64 as _img_b64
            m = _img_re.match(r'data:image/(\w+);base64,(.+)', image_data, _img_re.DOTALL)
            if m:
                ext = m.group(1)
                b64 = m.group(2)
                fname = f"{int(time.time() * 1000)}.{ext}"
                os.makedirs("static/uploads", exist_ok=True)
                with open(f"static/uploads/{fname}", "wb") as f:
                    f.write(_img_b64.b64decode(b64))
                image_url = f"/static/uploads/{fname}"
                print(f"[图片输入] 已保存: {image_url}")
        except Exception as e:
            print(f"[图片输入] 保存失败: {e}")

    # 记录话题锚点，供后台话题续聊机制使用
    last_user_msg_time = time.time()
    last_user_topic = user_msg

    # Immediately interrupt any ongoing background speech or spontaneous tasks
    spontaneous_interrupted = True
    recent_spontaneous_replies.clear()
    if agent and agent.voice:
        agent.voice.stop_speech()

    # Clean out any stale background thinking indicators before user message
    shared_chat_history = [m for m in shared_chat_history if not m.get("is_thinking")]

    # ── Slash Commands Handler (OpenClaw style: /new, /reset, /clear, /compact, /summary, /help) ──
    cmd = user_msg.lower().split()[0] if user_msg.startswith("/") else ""
    if cmd in ["/new", "/reset", "/clear"]:
        reset_text = agent.reset_conversation() if agent else "*探頭* 好的！前情對話已全部重置！"
        shared_chat_history = [
            {"sender": "user", "text": user_msg, "timestamp": time.time()},
            {"sender": "ino", "text": reset_text, "expression": "happy", "action_label": "探頭", "timestamp": time.time()}
        ]
        chat_version += 1
        if agent and agent.voice:
            agent.voice.speak("好的！前情对话已全部重置，全新的一页开始啦～")
        return jsonify({"status": "ok", "command": cmd}), 200

    elif cmd in ["/compact", "/summary", "/compress"]:
        summary_text = agent.compress_context(force=True) if agent else "暂无前情要点。"
        reply_msg = f"*微笑* 已为 Master 完成上下文压缩与记忆沉淀！\n\n【当前前情要点摘要】：\n{summary_text}"
        shared_chat_history.append({
            "sender": "user", "text": user_msg, "timestamp": time.time()
        })
        shared_chat_history.append({
            "sender": "ino",
            "text": reply_msg,
            "expression": "idle",
            "action_label": "微笑",
            "timestamp": time.time()
        })
        chat_version += 1
        if agent and agent.voice:
            agent.voice.speak("已为主人完成上下文压缩与记忆沉淀！")
        return jsonify({"status": "ok", "command": cmd}), 200

    elif cmd in ["/help", "/h"]:
        help_msg = """*探頭* 好的！以下是 INO 支持的快捷指令（Slash Commands）喔：

• **/new** 或 **/reset** 或 **/clear**：开启新对话（重置当前对话上下文）
• **/compact** 或 **/summary**：立即对当前上下文进行智能压缩与要点沉淀
• **/help**：查看可用指令列表"""
        shared_chat_history.append({
            "sender": "user", "text": user_msg, "timestamp": time.time()
        })
        shared_chat_history.append({
            "sender": "ino",
            "text": help_msg,
            "expression": "idle",
            "action_label": "探頭",
            "timestamp": time.time()
        })
        chat_version += 1
        return jsonify({"status": "ok", "command": cmd}), 200

    # 若请求指向某条具体资讯，自动附加详细正文（点击新闻后 INO 能详细讲解）
    user_msg = enrich_news_request(user_msg)

    # 用户请求排队中：后台行动让路（防止后台抢答导致空回复/走神）
    global user_request_pending
    user_request_pending = True

    shared_chat_history.append({
        "sender": "user",
        "text": user_msg,
        "image_url": image_url,
        "timestamp": time.time()
    })
    chat_version += 1

    # Process Agent response in background — frontend picks up result via polling
    threading.Thread(target=trigger_agent_speech, args=(user_msg, True, image_data), daemon=True).start()

    return jsonify({"status": "processing"}), 202


@app.route("/api/settings/voice", methods=["POST"])
def set_voice():
    global agent
    data = request.get_json() or {}
    voice = data.get("speaker_id") if data.get("speaker_id") is not None else data.get("voice_name")
    if agent and voice is not None:
        try:
            spk_id = int(voice)
            agent.voice.speaker_id = spk_id
            agent.voice.use_voicevox = True
            save_voice_config(speaker_id=spk_id)
            print(f"动态切换语音音色 (VOICEVOX): Speaker ID {spk_id}")
            return jsonify({"status": "ok", "speaker_id": spk_id})
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid speaker_id"}), 400

    return jsonify({"error": "Failed to set voice"}), 400


@app.route("/api/settings/voice_lang_mode", methods=["POST"])
def set_voice_lang_mode():
    global agent
    data = request.get_json() or {}
    mode = data.get("voice_lang_mode", "ja")
    if agent:
        agent.voice.set_voice_lang_mode(mode)
        save_voice_config(voice_lang_mode=mode)
        return jsonify({"status": "ok", "voice_lang_mode": mode})
    return jsonify({"error": "Agent not initialized"}), 400


@app.route("/api/settings/volume", methods=["POST"])
def set_volume():
    global agent
    data = request.get_json() or {}
    volume = data.get("volume", 0.3)
    if agent:
        vol_float = float(volume)
        agent.voice.set_volume(vol_float)
        save_voice_config(volume=vol_float)
        return jsonify({"status": "ok", "volume": vol_float})
    return jsonify({"error": "Failed to set volume"}), 400


@app.route("/api/settings/mute", methods=["POST"])
def set_mute():
    global agent
    data = request.get_json() or {}
    muted = data.get("muted", False)
    if agent:
        muted_bool = bool(muted)
        agent.voice.set_muted(muted_bool)
        save_voice_config(is_muted=muted_bool)
        return jsonify({"status": "ok", "is_muted": agent.voice.is_muted})
    return jsonify({"error": "Failed to set mute"}), 400


@app.route("/api/settings/speed", methods=["POST"])
def set_speed():
    global agent
    data = request.get_json() or {}
    speed = float(data.get("speed_scale", 1.0))
    if agent:
        agent.voice.set_speed_scale(speed)
        save_voice_config(speed_scale=speed)
        return jsonify({"status": "ok", "speed_scale": speed})
    return jsonify({"error": "Failed to set speed"}), 400


def open_browser():
    time.sleep(1.5)
    webbrowser.open("http://localhost:5000")


def _ensure_single_instance():
    """单实例守护：防止双实例（Qdrant 锁冲突/双 VoiceEngine 同时播放）。
    三重保险：
    1. 命名互斥体（内核对象，跨进程可靠，进程崩溃自动释放）
    2. 端口探测（已有服务在监听时拒绝启动）
    3. 文件锁（防同时启动竞态的补充）"""
    # 1) 命名互斥体：内核级互斥，任何时刻只有一个实例能创建成功
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ERROR_ALREADY_EXISTS = 183
        global _INSTANCE_MUTEX
        _INSTANCE_MUTEX = kernel32.CreateMutexW(None, True, "Global\\INO_Companion_SingleInstance")
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            print("⚠️ [SingleInstance] 已有 INO 实例持有互斥体，本实例拒绝启动（防止双实例）。")
            kernel32.CloseHandle(_INSTANCE_MUTEX)
            sys.exit(0)
    except Exception as e:
        print(f"[SingleInstance] 命名互斥体不可用: {e}")

    # 2) 端口探测：已有服务在监听 → 拒绝启动
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        if s.connect_ex(("127.0.0.1", 5000)) == 0:
            print("⚠️ [SingleInstance] 端口 5000 已有 INO 服务在运行，本实例拒绝启动（防止双实例）。")
            print("   如需重启请使用页面上的「🔄 热更新」按钮，或先关闭旧实例。")
            sys.exit(0)
    finally:
        s.close()

    # 3) 文件锁：两个实例同时通过端口探测时，只有持锁者能继续（进程退出/被杀时锁自动释放）
    try:
        import msvcrt
        lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ino_instance.lock")
        if not os.path.exists(lock_path):
            with open(lock_path, "w"):
                pass
        lock_f = open(lock_path, "r+")
        try:
            msvcrt.locking(lock_f.fileno(), msvcrt.LK_NBLCK, 1)
            lock_f.seek(0)
            lock_f.truncate()
            lock_f.write(str(os.getpid()))
            lock_f.flush()
            global _INSTANCE_LOCK_FILE
            _INSTANCE_LOCK_FILE = lock_f  # 保持引用，锁随进程生命周期自动释放
        except OSError:
            print("⚠️ [SingleInstance] 检测到另一实例持有实例锁，本实例拒绝启动（防止双实例）。")
            sys.exit(0)
    except Exception as e:
        print(f"[SingleInstance] 文件锁不可用（仅端口/互斥体保护）: {e}")


if __name__ == "__main__":
    _ensure_single_instance()  # 必须放在 init_agent 之前，避免重复占用记忆库
    init_agent()

    # 后台静默刷新新闻缓存（daemon 线程）
    try:
        bg_minutes = agent.news_config.get("background_refresh_minutes", 30)
        agent.news_fetcher.start_background_refresh(bg_minutes)
    except Exception as e:
        print(f"[NewsFetcher] 后台刷新启动失败: {e}")

    t = threading.Thread(target=agent_background_loop, daemon=True)
    t.start()

    threading.Thread(target=open_browser, daemon=True).start()

    print("\n========================================================")
    print("INO Companion Agent Server Started!")
    print("Access URL: http://localhost:5000 (Browser opened automatically)")
    print("========================================================\n")

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)  # threaded：慢请求不阻塞其他 API（含退出指令）
