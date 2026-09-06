"""
INO Agent Tools Package
Modular tool definitions and registry for INO Companion.
"""
from .base import BaseTool, tool
from .registry import ToolRegistry
from .web_search import WebSearchTool
from .news_feed import NewsFeedTool
from .system_status import SystemStatusTool
from .hardware_status import HardwareStatusTool
from .memory_tool import MemoryTool
from .voice_control import VoiceControlTool
from .manage_news_topics import ManageNewsTopicsTool
from .update_sidebar_news import UpdateSidebarNewsTool
from .read_news_detail import ReadNewsDetailTool

__all__ = [
    "BaseTool",
    "tool",
    "ToolRegistry",
    "WebSearchTool",
    "NewsFeedTool",
    "SystemStatusTool",
    "HardwareStatusTool",
    "MemoryTool",
    "VoiceControlTool",
    "ManageNewsTopicsTool",
    "UpdateSidebarNewsTool",
    "ReadNewsDetailTool"
]
