# -*- coding: utf-8 -*-
"""
autonomy.py — INO 多轮自主行动模块（计划 1.5）

职责：
1. should_act(config)           : 按 background_action_chance 概率判断本次后台触发是否走"行动优先"
2. build_action_prompt(agent)   : 构建"自主做一件实事"的后台行动提示词
3. can_run(action_type, sec)    : 检查某类行动的冷却期（防重复）
4. log_activity(type, summary)  : 记录活动到 activity_log.json

与 core_agent.chat() 的 <continue> 多轮续跑协议配合，构成完整的自主能力：
后台感知 → 自主决定做一件事（查资讯/更新侧边栏/整理记忆）→ 工具调用 → 一句话汇报。
"""

import json
import os
import random
import time

ACTIVITY_LOG_FILE = "activity_log.json"
DEFAULT_COOLDOWN = 600  # 同类行动默认冷却 10 分钟


# ── 概率与冷却 ─────────────────────────────────────────────

def should_act(autonomy_config: dict) -> bool:
    """按 background_action_chance 概率决定本次后台触发是否走行动优先。"""
    chance = float(autonomy_config.get("background_action_chance", 0.15))
    return random.random() < chance


def _load_activity_log() -> dict:
    if os.path.exists(ACTIVITY_LOG_FILE):
        try:
            with open(ACTIVITY_LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_activity_log(log: dict):
    try:
        with open(ACTIVITY_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Autonomy Log Save Error]: {e}")


def can_run(action_type: str, cooldown_sec: int = DEFAULT_COOLDOWN) -> bool:
    """检查某类行动是否已过冷却期。"""
    log = _load_activity_log()
    last_ts = log.get(action_type, {}).get("ts", 0)
    return (time.time() - last_ts) > cooldown_sec


def log_activity(action_type: str, summary: str):
    """记录一次自主行动（时间戳 + 摘要），用于去重与审计。"""
    log = _load_activity_log()
    log[action_type] = {"ts": time.time(), "summary": summary[:200]}
    history = log.get("_history", [])
    history.append({"type": action_type, "ts": time.time(), "summary": summary[:100]})
    log["_history"] = history[-20:]
    _save_activity_log(log)


# ── 行动提示词 ─────────────────────────────────────────────

def build_action_prompt(agent) -> str:
    """
    构建后台"自主行动"提示词：让 INO 自主决定做一件有意义的事。
    携带前情摘要与最近对话上下文，引导调用工具后一句话汇报。
    """
    context_parts = []
    if agent.session_summary:
        context_parts.append(f"【前情要点摘要】: {agent.session_summary}")

    recent_msgs = [m for m in agent.conversation_history if m.get("role") in ["user", "assistant"]]
    if recent_msgs:
        snippets = []
        for m in recent_msgs[-2:]:
            role = "master" if m["role"] == "user" else "INO"
            snippets.append(f"  {role}: {m.get('content', '')[:120]}")
        context_parts.append("【最近用户对话】:\n" + "\n".join(snippets))

    context_str = "\n\n".join(context_parts)

    return f"""[系统自主行动提醒]
{context_str}

【自主行动机会】：现在你有一段完全自主的时间，请主动做一件有意义的"实事"（任选其一，或自创）：
1. 主动搜索一条新的科技/硬件动态，并用 <tool_call> 调用 update_sidebar_news 挂载到右侧资讯栏
2. 主动整理或搜索自己的长期记忆（<tool_call> manage_memory）
3. 查看当前系统感知状态（<tool_call> get_system_status），判断 master 是否方便被打扰
4. 刷新资讯库（<tool_call> manage_news_topics action=refresh）

要求：
- 优先调用工具完成一件实事（必须用 <tool_call> 标记），不要只说不做
- 完成后，用 1 句可爱的话向 master 简短汇报成果（适合语音朗读）
- 若当前没有值得做的事，安静休息即可（直接回答"…（安静）"）
请开始："""
