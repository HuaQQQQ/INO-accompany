from tools.base import BaseTool


class ReadNewsDetailTool(BaseTool):
    """读取一条新闻链接的详细正文内容（INO 需要展开讲细节时调用）。"""

    name = "read_news_detail"
    description = (
        "读取一条新闻链接的详细正文内容。当 master 想深入了解某条新闻的细节、"
        "或搜索结果只有标题需要展开时，传入该新闻的链接（url）即可返回正文摘要。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要读取的新闻链接（搜索结果的括号链接）"
            }
        },
        "required": ["url"]
    }

    def __init__(self, fetcher):
        super().__init__()
        self.fetcher = fetcher

    def execute(self, url: str = "") -> str:
        return self.fetcher.fetch_article_content(url)
