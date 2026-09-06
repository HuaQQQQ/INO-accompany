from typing import Any
from .base import BaseTool

class SystemStatusTool(BaseTool):
    """Tool to inspect INO companion device & presence status."""
    name = "get_system_status"
    description = "查询当前电脑与主人的感知状态。包括：主人是否在桌前、是否在使用主电脑、笔记本空闲时长、是否全屏沉浸免打扰等。"
    parameters = {
        "type": "object",
        "properties": {},
        "required": []
    }

    def __init__(self, presence_detector=None):
        super().__init__(self.name, self.description, self.parameters)
        self.presence_detector = presence_detector

    def execute(self) -> str:
        if not self.presence_detector:
            return "未连接到感知检测服务。"
        
        state = self.presence_detector.last_state
        state_desc_map = {
            "active": "主人正在活跃使用这台笔记本",
            "desk_companion": "桌面副屏陪伴模式 (Master 在桌前忙碌主电脑)",
            "immersive": "全屏沉浸免打扰模式 (正在玩游戏或看全屏内容)",
            "away": "主人离开座位中",
            "returned": "主人刚刚回到桌前"
        }
        
        idle_s = round(self.presence_detector.get_input_idle_seconds(), 1)
        primary_pc = "在线 (在主电脑前)" if self.presence_detector.is_primary_pc_online() else "未连通/离线"
        
        return (
            f"【当前系统与感知状态】\n"
            f"- 当前陪伴状态: {state_desc_map.get(state, state)}\n"
            f"- 笔记本键盘鼠标空闲时间: {idle_s} 秒\n"
            f"- 主电脑状态: {primary_pc}\n"
        )
