from typing import Any
from .base import BaseTool

class UpdateSidebarNewsTool(BaseTool):
    """Tool for INO (or user) to directly search for a specific tech/hardware topic and update the right sidebar news feed."""
    name = "update_sidebar_news"
    description = "搜寻指定的主题/关键词，并把搜寻到的最新前沿资讯更新显示到右侧的 (News Feed) 侧边栏列中。当 master 提及某个特定新硬件/技术话题，或你想把搜索到的新科技展示在右侧列时调用。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要搜寻并挂载到右侧侧边栏的新闻关键词/主题，例如 '量子计算新进展'、'开源AI大模型' 或 '半导体技术动态'"
            }
        },
        "required": ["query"]
    }

    def __init__(self, news_fetcher=None):
        super().__init__(self.name, self.description, self.parameters)
        self.news_fetcher = news_fetcher

    def execute(self, query: str = "") -> str:
        if not self.news_fetcher:
            return "未连接到资讯获取服务。"
        if not query:
            return "请提供要检索并更新到右侧侧边栏的主题关键词。"

        items = self.news_fetcher.update_sidebar_with_search(query)
        if not items:
            return f"未能找到关于 '{query}' 的最新新闻，侧边栏保持原样。"

        titles_str = "\n".join([f"- {item['title']}" for item in items])
        return f"✅ 成功搜寻并把关于 '{query}' 的最新动态更新挂载到了右侧面板！\n包含以下内容：\n{titles_str}"
