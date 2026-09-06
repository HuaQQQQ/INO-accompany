from typing import Any
from .base import BaseTool

class MemoryTool(BaseTool):
    """Tool for managing INO's long-term memory about Master."""
    name = "manage_memory"
    description = "存取与主人相关的专属记忆。可以搜索过去的经历、偏好或主动记录新的重要信息。"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "操作类型：'search'（搜索记忆）或 'save'（保存新记忆）"
            },
            "content": {
                "type": "string",
                "description": "搜索关键词或要保存的记忆内容文本"
            }
        },
        "required": ["action", "content"]
    }

    def __init__(self, memory_manager=None):
        super().__init__(self.name, self.description, self.parameters)
        self.memory_manager = memory_manager

    def execute(self, action: str, content: str) -> str:
        if not self.memory_manager:
            return "未连接到记忆服务。"
        
        if action == "save":
            self.memory_manager.add_memory(content)
            return f"已成功将新记忆保存至专属记忆抽屉：{content}"
        else:
            memories = self.memory_manager.get_relevant_memories(content)
            if memories:
                return "【检索到的相关记忆】：\n" + "\n".join(memories)
            return "记忆抽屉中暂未找到相关的专属记录。"
