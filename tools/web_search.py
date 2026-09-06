import urllib.parse
import xml.etree.ElementTree as ET
import requests
import re
from .base import BaseTool

class WebSearchTool(BaseTool):
    """Tool for performing live web and news searches."""
    name = "web_search"
    description = "全网实时联网搜索。当主人询问关于 iPhone、苹果、华为、任天堂、显卡、AI、最新科技新闻、数码产品、实时事件或任何你不确定事实的新鲜事物时，调用此工具自主查询最新资讯。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要搜索的核心关键词，例如 'ROG 笔记本 新产品'、'iPhone 16 最新资讯'。请提炼为精炼关键词，避免直接传入带有语气助词的口语长句。"
            }
        },
        "required": ["query"]
    }

    def __init__(self, news_fetcher=None):
        super().__init__(self.name, self.description, self.parameters)
        self.news_fetcher = news_fetcher
        self.headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    def clean_query(self, text: str) -> str:
        q = text.strip()
        fillers = [
            '能不能帮我', '可以帮我', '麻烦帮我', '帮我', '请问', '你知道', '告诉我',
            '我想知道', '我想要了解下', '我想要了解', '我想了解下', '我想了解一下', '我想了解', '我想看下', '我想看', '我想查下', '我想查', '我想听听', '我想', '我要',
            '查一下', '查查', '查', '搜一下', '搜搜', '搜', '搜索一下', '搜索', '找找', '找一下', '找',
            '了解一下', '了解下', '了解', '看一下', '看下', '看看', '讲讲', '聊聊', '说说', '介绍下', '介绍一下',
            '有没有关于', '关于', '最近有没有', '有没有', '最近有什么', '有什么', '最近的', '最新的', '最新',
            '的最新资讯', '的最新消息', '的最新动态', '的最新新闻', '的新闻', '的动态', '的资讯', '的消息',
            '怎么样了', '怎么样', '如何', '什么时候出', '什么时候发布', '什么时候上市', '发布了吗', '多少钱'
        ]
        for f in fillers:
            q = q.replace(f, ' ')
        q = re.sub(r'[？?！!，,。.\n\t]+', ' ', q).strip()
        if len(q) < 2:
            return text
        return ' '.join(q.split())

    def execute(self, query: str) -> str:
        if self.news_fetcher:
            return self.news_fetcher.search_web(query)

        search_term = self.clean_query(query)
        encoded_q = urllib.parse.quote(search_term)
        rss_url = f"https://news.google.com/rss/search?q={encoded_q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        try:
            r = requests.get(rss_url, headers=self.headers, timeout=6)
            if r.status_code == 200:
                root = ET.fromstring(r.text)
                items = root.findall('.//item')
                results = []
                for item in items[:5]:
                    title = item.find('title')
                    if title is not None and title.text:
                        results.append(f"- {title.text.strip()}")
                if results:
                    return "\n".join(results)
        except Exception as e:
            return f"搜索发生异常: {e}"
        return "未检索到更多实时信息"
