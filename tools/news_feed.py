from typing import Any
from .base import BaseTool

class NewsFeedTool(BaseTool):
    """Tool to read the current right-side dashboard tech news feed."""
    name = "get_sidebar_news"
    description = "读取电脑副屏右侧资讯面板 (News Feed) 当前展示的实时硬件、AI 与科技动态。当主人问及右侧面板、右边资讯、显卡行情、硬件新闻或想了解当前资讯流时调用。"
    parameters = {
        "type": "object",
        "properties": {
            "count": {
                "type": "number",
                "description": "获取的新闻条数，默认为 5 条"
            }
        },
        "required": []
    }

    def __init__(self, news_fetcher=None):
        super().__init__(self.name, self.description, self.parameters)
        self.news_fetcher = news_fetcher

    def execute(self, count: int = 5) -> str:
        if not self.news_fetcher:
            return "未连接到资讯获取服务。"
        snippet = self.news_fetcher.get_sidebar_news_prompt(count=int(count or 5))
        return snippet or "当前右侧面板暂无最新资讯。"
