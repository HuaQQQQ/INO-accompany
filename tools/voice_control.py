import yaml
from typing import Any
from .base import BaseTool

class VoiceControlTool(BaseTool):
    """Tool for controlling INO's voice volume, language mode, and speech parameters."""
    name = "control_voice"
    description = "调整 INO 的语音音量、静音状态或朗读语言模式。当主人说'声音大一点'、'声音小一点'、'静音'、'说日文/切换日文'、'切换中文'等时调用。"
    parameters = {
        "type": "object",
        "properties": {
            "volume": {
                "type": "number",
                "description": "音量大小，范围 0.0 到 1.0 (例如 0.5 代表 50% 音量)"
            },
            "mute": {
                "type": "boolean",
                "description": "是否静音 (true 为静音，false 为解除静音)"
            },
            "language_mode": {
                "type": "string",
                "enum": ["ja", "zh"],
                "description": "朗读语言模式：'ja' 为地道日语原生朗读，'zh' 为中文拟音朗读"
            },
            "speed_scale": {
                "type": "number",
                "description": "语速快慢，范围 0.5 到 1.5 (1.0 为标准正常语速，例如 0.8 代表慢速，1.2 代表快速)"
            }
        },
        "required": []
    }

    def __init__(self, voice_engine=None):
        super().__init__(self.name, self.description, self.parameters)
        self.voice_engine = voice_engine

    def _persist(self, **kwargs):
        try:
            with open("config.yaml", "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            if "voice" not in cfg:
                cfg["voice"] = {}
            for k, v in kwargs.items():
                cfg["voice"][k] = v
            with open("config.yaml", "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True)
        except Exception as e:
            print(f"[VoiceControlTool Config Save Error]: {e}")

    def execute(self, volume: float = None, mute: bool = None, language_mode: str = None, speed_scale: float = None) -> str:
        if not self.voice_engine:
            return "未连接到语音服务。"
        
        res = []
        updates = {}
        if volume is not None:
            v = max(0.0, min(1.0, float(volume)))
            self.voice_engine.set_volume(v)
            updates["volume"] = v
            res.append(f"音量已调整为 {int(v * 100)}%")
        if mute is not None:
            m = bool(mute)
            self.voice_engine.set_muted(m)
            updates["is_muted"] = m
            res.append("已开启静音" if m else "已解除静音")
        if language_mode in ["ja", "zh"]:
            self.voice_engine.set_voice_lang_mode(language_mode)
            updates["voice_lang_mode"] = language_mode
            res.append(f"朗读语言已切换为{'日文' if language_mode == 'ja' else '中文'}")
        if speed_scale is not None:
            sp = max(0.5, min(2.0, float(speed_scale)))
            self.voice_engine.set_speed_scale(sp)
            updates["speed_scale"] = sp
            res.append(f"语速已调整为 {round(sp, 2)}x")

        if updates:
            self._persist(**updates)

        return "，".join(res) if res else "未指定音量、语速或语言调整参数。"
