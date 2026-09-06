from typing import Any
from .base import BaseTool

class ManageNewsTopicsTool(BaseTool):
    """Tool for INO to autonomously manage her subscribed news query topics."""
    name = "manage_news_topics"
    description = "动态管理与自主更新 INO 关注和抓取的科技/硬件/数码新闻主题列表。当主人要求你关注、追踪某些新领域（如折叠屏、智能家居、量子计算、AI智能体），或你想自主添加/删除/查看订阅的主题时，调用此工具。"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "remove", "list", "refresh"],
                "description": "操作类型: 'add' (添加新关注主题), 'remove' (移除已有主题), 'list' (列出所有当前关注主题), 'refresh' (强行刷新最新新闻)"
            },
            "topic": {
                "type": "string",
                "description": "要添加或移除的主题关键词，例如 '折叠屏 手机 芯片' 或 '量子计算'（仅在 action 为 add 或 remove 时需要）"
            }
        },
        "required": ["action"]
    }

    def __init__(self, news_fetcher=None):
        super().__init__(self.name, self.description, self.parameters)
        self.news_fetcher = news_fetcher

    def execute(self, action: str = "list", topic: str = "") -> str:
        if not self.news_fetcher:
            return "未连接到资讯服务。"

        act = action.lower().strip()
        if act == "add":
            return self.news_fetcher.add_query(topic)
        elif act == "remove":
            return self.news_fetcher.remove_query(topic)
        elif act == "list":
            return self.news_fetcher.list_queries()
        elif act == "refresh":
            news = self.news_fetcher.get_news(force_refresh=True)
            return f"🔄 成功重新抓取全网最新资讯！当前资讯库共有 {len(news)} 条文章。"
        else:
            return f"未知的操作类型 '{action}'。有效操作包括: add, remove, list, refresh。"
