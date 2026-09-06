# -*- coding: utf-8 -*-
"""
expression_parser.py — INO 表情/动作/颜文字解析模块（纯函数、无依赖）

职责：
1. 从模型输出文本中提取显式表情标签 [happy] 等
2. 提取动作标签 *歪头*、（眨眼）
3. 识别颜文字并映射到表情

协议：
    parse_expression(text) -> (clean_text, action_label, expression)
优先级：显式 [标签] > *动作* 标签 > 颜文字 > 默认 idle

本模块是前后端表情协议的【唯一事实源】：
app.py 的 /api/expressions 端点从这里读取 EXPRESSIONS / ACTION_MAP /
KAOMOJI_MAP / EXPRESSION_LABELS，前端像素表情引擎消费同一份数据。
"""

import re

# ── 表情枚举（前后端共享）──────────────────────────────────
EXPRESSIONS = [
    "idle", "happy", "excited", "love", "shy", "wink", "think",
    "surprised", "sad", "cry", "sleepy", "angry", "pout",
    "curious", "confused", "tired", "blink",
]

# 表情中文标签（前端展示用）
EXPRESSION_LABELS = {
    "idle": "待機中", "happy": "開心！", "excited": "好興奮！",
    "love": "最喜歡你", "shy": "害羞啦", "wink": "眨眼～",
    "think": "思考中...", "surprised": "嚇一跳！", "sad": "有點難過",
    "cry": "嗚嗚...", "sleepy": "好睏...", "angry": "哼！",
    "pout": "嘟嘴", "curious": "好奇？", "confused": "一頭霧水",
    "tired": "累累的", "blink": "眨眼",
}

# ── 显式标签正则（仅匹配 EXPRESSIONS 中的枚举）────────────
_EXPR_TAG_RE = re.compile(
    r'\[(' + '|'.join(EXPRESSIONS) + r')\]', re.IGNORECASE
)

# ── 动作标签 → 表情映射（*歪头*、（眨眼）等）──────────────
ACTION_MAP = {
    "歪头": "curious", "歪頭": "curious", "歪著頭": "curious",
    "眨眼": "wink", "眨眼笑": "wink", "眯眼": "wink",
    "探头": "surprised", "探頭": "surprised",
    "微笑": "happy", "笑": "happy", "甜甜地笑": "happy",
    "戳": "pout", "嘟嘴": "pout", "噘嘴": "pout",
    "思考": "think", "托腮": "think", "歪著頭想": "think",
    "摸头": "shy", "摸頭": "shy", "害羞": "shy", "捂脸": "shy", "捂臉": "shy",
    "吐舌": "happy", "吐舌頭": "happy",
    "叹气": "tired", "嘆氣": "tired",
    "伸懒腰": "sleepy", "伸懶腰": "sleepy", "打哈欠": "sleepy",
    "疑惑": "confused", "發愁": "confused", "发愁": "confused",
    "惊讶": "surprised", "驚嚇": "surprised", "嚇一跳": "surprised",
    "惊喜": "excited", "開心": "excited", "开心": "excited", "雀跃": "excited", "雀躍": "excited",
    "流泪": "cry", "哭": "cry", "揉眼睛": "cry",
    "生气": "angry", "生氣": "angry", "气鼓鼓": "angry", "氣鼓鼓": "angry",
    "脸红": "shy", "臉紅": "shy",
    "探头探脑": "curious", "探頭探腦": "curious",
}

# ── 颜文字 → 表情映射（有序列表，先匹配先赢）─────────────
# 每项: (正则模式, 表情名)。排列顺序即优先级：越靠前越优先。
KAOMOJI_MAP = [
    # cry（最特殊符号先匹配）
    (r'T_T|T\.T|ToT|TωT|Q﹏Q|QAQ|\(个_个\)|\(＞﹏＜\)|\(╥﹏╥\)|\(´；ω；`\)|\(；ω；\)|\(｡•́︿•̀｡\)|\(´；д；`\)|\(´•̥̥̥ω•̥̥̥`\)', "cry"),
    # excited
    (r'≧[^（）()\s]{0,4}∇[^（）()\s]{0,4}|\(ノ◕ヮ◕\)ノ|\\\(\^[oO]\^\)/|٩\(◕‿◕\)۶|\(ﾉ≧∀≦\)ﾉ|✧|✦|☆彡|\(๑•̀ㅂ•́\)و✧', "excited"),
    # love
    (r'♥|❤|💕|💗|\(♡[^（）()]*♡\)|\(´,,•ω•,,\)♡|\(˘︶˘\)\.｡\.:\*♡|\(｡♥‿♥｡\)|\(￣ε￣\)♡', "love"),
    # shy
    (r'〃∀〃|⁄⁄•⁄ω⁄•⁄⁄|\(｡･ω･｡\)|\(,,•́[^（）()]{0,4}•̀,,\)|\(´•ω•̥`?\)|\(/ω＼\)|\(⁄ ⁄>⁄ ▽ ⁄<⁄ ⁄\)|\(*ﾉωﾉ\)', "shy"),
    # surprised
    (r'\(?[°º](?:□|ロ|д|Д)[°º]\)?|\(⊙_⊙\)|\(O_O\)|O_O|0_o|\(O\.O\)|\(ﾟ⊿ﾟ\)|\(☉_☉\)|\(°△°\)|\(ﾟДﾟ\)', "surprised"),
    # sad
    (r'>_<|>﹏<|\(；∀；\)|\(╥ω╥\)|\(TωT\)|\(´-﹏-`\)|\(｡•́︿•̀｡\)|\(︶︹︶\)', "sad"),
    # sleepy
    (r'\(´-ω-`\)|\(-_-\)|\(￣o￣\)|\(´ρ`\)|\(눈_눈\)|\(￣ω￣\)zZ?|zZ?Z?', "sleepy"),
    # angry
    (r'\(｀皿´\)|\(╬[^（）()]{0,6}\)|\(￣︿￣\)|\(＃｀д´\)|\(｀ε´\)|>:\(|\(╯‵□′\)╯|\(￣へ￣\)', "angry"),
    # pout
    (r'\(｡>﹏<\)|\(｀へ´\)|\(・ε・\)|\(￣ε￣\)|\(´･ω･`\)', "pout"),
    # curious
    (r'OwO|oWo|\(・∀・\)|\(｀・ω・´\)|\(・o・\)|\(・ω・\)', "curious"),
    # confused
    (r'\(・_・\?\)|\(。_。\)|\(￣□￣；\)|\(？\s*？\s*？\)|\(ーー;\)|\(・＿・\)|\(・д・\)', "confused"),
    # tired
    (r'\(´Д｀\)|\(；´Д｀\)|\(・へ・\)|\(￣Д￣\)|\(-д-\)', "tired"),
    # think
    (r'\(￣～￣\)|\(。・ω・。\)|\(￣ω￣；\)|\(・_・\)|\(￣▽￣\)\*|\(；一_一\)', "think"),
    # wink
    (r'\(￣▽￣\)~\*|\(\^_-\)|\(\^_~\)|\(-ω-\)ゞ|\(◕‿◕✿\)|;-P|;P', "wink"),
    # happy（通用笑脸最后兜底）
    (r'\(◕‿◕\)|\(◕ᴗ◕\)|\(\^▽\^\)|\(\^ω\^\)|\(＾▽＾\)|\(＾ω＾\)|\(\^_\^\)|\(\*\^▽\^\*\)|\(✿◡‿◡\)|\(●´ω｀●\)|\^_\^|\^ω\^|\(￣▽￣\)|\(´∀`\)|\(・∀・\)ノ', "happy"),
]

# 预编译颜文字正则
_KAOMOJI_RES = [(re.compile(p), e) for p, e in KAOMOJI_MAP]


# ── 对外 API ───────────────────────────────────────────────

def detect_kaomoji(text: str) -> str:
    """检测文本中的颜文字，返回映射表情名；未命中返回空串。"""
    if not text:
        return ""
    for pattern, expr in _KAOMOJI_RES:
        if pattern.search(text):
            return expr
    return ""


def detect_action(text: str) -> str:
    """检测动作标签（*歪头*、（眨眼）），返回映射表情名；未命中返回空串。"""
    if not text:
        return ""
    m = re.search(r'[\*（\(]([^\*（\)\n]{1,6})[\*）\)]', text)
    if not m:
        return ""
    return ACTION_MAP.get(m.group(1).strip(), "")


def clean_model_text(text: str) -> str:
    """清理模型输出中的思维链与工具调用残留，返回纯文本。
    只删除【闭合】的 <tool_call> 块或剥掉孤立标签，绝不吞掉后续正文。"""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    text = re.sub(r'<tool_call>\s*.*?\s*</tool_call>', '', text,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<tool_call>|</tool_call>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'```(?:json|tool_call)?\s*\{\s*"(?:name|tool)"\s*:.*?\}\s*```',
                  '', text, flags=re.DOTALL | re.IGNORECASE).strip()
    return text


def parse_expression(text: str):
    """
    从模型输出文本中提取表情与动作。

    返回 (clean_text, action_label, expression)
    优先级：显式 [标签] > *动作* 标签 > 颜文字 > 默认 idle
    """
    if not text:
        return "", "", "idle"

    clean_text = clean_model_text(text)

    # 1. 显式表情标签 [happy]
    expr_match = _EXPR_TAG_RE.search(clean_text)
    if expr_match:
        expression = expr_match.group(1).lower()
        clean_text = clean_text.replace(expr_match.group(0), '')

    # 2. 动作标签 *歪头* /（眨眼）——仅当内容命中动作映射表时才作为动作标签，
    #    否则保留原文本（避免把 (◕‿◕) 等颜文字误当动作标签吞掉）
    action_label = ""
    action_match = re.search(r'[\*（\(]([^\*（\)\n]{1,6})[\*）\)]', clean_text)
    if action_match:
        candidate = action_match.group(1).strip()
        if candidate in ACTION_MAP:
            action_label = candidate
            clean_text = clean_text.replace(action_match.group(0), '')

    # 3. 优先级决策：显式标签 > 动作映射 > 颜文字
    if not expr_match:
        action_expr = ACTION_MAP.get(action_label, "")
        if action_expr:
            expression = action_expr
        else:
            expression = detect_kaomoji(clean_text) or "idle"

    # 收尾：压缩多余空白；无内容时兜底保留原文本
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    if not clean_text and text:
        clean_text = re.sub(r'[*\[\]（）()]', '', text).strip()

    return clean_text, action_label, expression
