# -*- coding: utf-8 -*-
"""
INO 订阅源新闻抓取模块
========================
魔改自开源 Agent Skill：cclank/news-aggregator-skill
原仓库：https://github.com/cclank/news-aggregator-skill

贴合 INO 项目逻辑的改动：
1. 输出格式与 news_fetcher.py 缓存完全一致：{title, date, link, source, query}
   - query 字段存源分类，侧边栏 News Feed 的 tag 直接显示
2. 订阅源全部配置化：config.yaml → news.sources（实测可用的国内第一梯队源）
3. RSS 解析移植原 skill 的 rss_parser.py（BeautifulSoup 宽容解析 + 3 次重试退避 + CDATA 清洗）
4. API 源解析（澎湃 / 第一财经 / 百度热搜 / B站热门）按 2026-08-08 实测 JSON 结构编写
5. 并行抓取（ThreadPoolExecutor）、单源失败独立容错、72h 新鲜度过滤
6. 标题清洗（去 " - 来源" 后缀）+ 按发布时间降序排序
"""

import concurrent.futures
import re
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime

import requests
import urllib3
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore", category=urllib3.exceptions.InsecureRequestWarning)
try:
    from bs4 import XMLParsedAsHTMLWarning

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except Exception:
    pass

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ---------------------------------------------------------------- RSS 解析
class _RSSFetcher:
    """移植自 news-aggregator-skill/scripts/rss_parser.py，兼容 RSS 2.0 / Atom。"""

    @staticmethod
    def _clean_text(text):
        if not text:
            return ""
        text = re.sub(r"^\s*<!\[CDATA\[|\]\]>\s*$", "", str(text).strip()).strip()
        return text

    @staticmethod
    def _parse(content, source_name, limit):
        soup = BeautifulSoup(content, "html.parser")
        items = []
        for entry in soup.find_all(["item", "entry"])[: max(limit, 20)]:
            title_tag = entry.find("title")
            if title_tag is None:
                continue
            title = _RSSFetcher._clean_text(title_tag.get_text())
            if not title:
                continue
            # link：RSS 用 <link>text</link>，Atom 用 <link href>
            link = ""
            link_tag = entry.find("link")
            if link_tag is not None:
                if link_tag.has_attr("href"):
                    link = link_tag["href"].strip()
                else:
                    link = link_tag.get_text(strip=True)
            if not link:
                guid = entry.find("guid")
                if guid is not None and guid.get_text(strip=True).startswith("http"):
                    link = guid.get_text(strip=True)
            # 时间：RSS pubDate / Atom published / updated
            pub_tag = entry.find(["pubdate", "published", "updated"])
            pub_date = _RSSFetcher._clean_text(pub_tag.get_text()) if pub_tag else ""
            items.append(
                {
                    "title": title,
                    "date": pub_date,
                    "link": link,
                    "source": source_name,
                }
            )
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def fetch(url, source_name, limit, timeout=10):
        """带 3 次重试退避的 RSS/Atom 抓取。"""
        last_error = None
        for attempt in range(3):
            try:
                r = requests.get(
                    url, headers=DEFAULT_HEADERS, timeout=timeout, verify=False
                )
                r.raise_for_status()
                r.encoding = r.apparent_encoding or "utf-8"
                return _RSSFetcher._parse(r.content, source_name, limit)
            except Exception as e:
                last_error = e
                if attempt < 2:
                    time.sleep(1 + attempt)
        print(f"[NewsSource] RSS 抓取失败 {url}: {last_error}")
        return []


# ---------------------------------------------------------------- API 解析
class _APIParser:
    """各 JSON API 的字段解析（2026-08-08 实测结构）。"""

    @staticmethod
    def thepaper(data, limit):
        """澎湃 rightSidebar：data.hotNews[].name / contId / pubTimeNew / nodeInfo.name"""
        items = []
        for n in data.get("data", {}).get("hotNews", [])[:limit]:
            title = n.get("name", "")
            if not title:
                continue
            cont_id = n.get("contId", "")
            node = n.get("nodeInfo", {}) or {}
            items.append(
                {
                    "title": title,
                    "date": _APIParser._relative_time(n.get("pubTimeNew", "")),
                    "link": f"https://www.thepaper.cn/newsDetail_forward_{cont_id}",
                    "source": node.get("name") or "澎湃新闻",
                }
            )
        return items

    @staticmethod
    def yicai(data, limit):
        """第一财经 getlatest：data[].NewsTitle / url / CreateDate / NewsSource"""
        items = []
        for n in data[:limit]:
            title = n.get("NewsTitle", "")
            if not title:
                continue
            url = n.get("url", "")
            if url and not url.startswith("http"):
                url = "https://www.yicai.com" + url
            try:
                dt = datetime.fromisoformat(n["CreateDate"])
                date = format_datetime(dt.replace(tzinfo=timezone.utc))
            except Exception:
                date = ""
            items.append(
                {
                    "title": title,
                    "date": date,
                    "link": url,
                    "source": n.get("NewsSource") or "第一财经",
                }
            )
        return items

    @staticmethod
    def baidu_hot(data, limit):
        """百度热搜：data.cards[].content[] 递归找 word / hotScore / url"""
        items = []

        def walk(node):
            if isinstance(node, dict):
                word = node.get("word")
                if word:
                    items.append(
                        {
                            "title": str(word),
                            "date": "",
                            "link": node.get("url", ""),
                            "source": "百度热搜",
                        }
                    )
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data.get("data", {}))
        return items[:limit]

    @staticmethod
    def bilibili(data, limit):
        """B站热门：data.list[].title / pubdate(epoch) / owner.name / aid"""
        items = []
        for n in data.get("data", {}).get("list", [])[:limit]:
            title = n.get("title", "")
            if not title:
                continue
            try:
                dt = datetime.fromtimestamp(int(n.get("pubdate", 0)), tz=timezone.utc)
                date = format_datetime(dt)
            except Exception:
                date = ""
            owner = n.get("owner", {}) or {}
            items.append(
                {
                    "title": title,
                    "date": date,
                    "link": f"https://www.bilibili.com/video/av{n.get('aid', '')}",
                    "source": owner.get("name") or "B站热门",
                }
            )
        return items

    @staticmethod
    def _relative_time(text):
        """'13小时前' / '10分钟前' / '昨天 23:55' → RFC822 GMT；解析失败原样返回。"""
        if not text:
            return ""
        now = datetime.now(timezone.utc)
        m = re.search(r"(\d+)\s*分钟前", text)
        if m:
            return format_datetime(now - timedelta(minutes=int(m.group(1))))
        m = re.search(r"(\d+)\s*小时前", text)
        if m:
            return format_datetime(now - timedelta(hours=int(m.group(1))))
        m = re.search(r"(\d+)\s*天前", text)
        if m:
            return format_datetime(now - timedelta(days=int(m.group(1))))
        return text


# ---------------------------------------------------------------- 主抓取器
class NewsSourceFetcher:
    """INO 订阅源抓取器：并行抓取 config.yaml → news.sources 中配置的源。

    输出条目格式与 news_fetcher.py 缓存一致：
        {title, date(RFC822 GMT 字符串), link, source, query(=源分类)}
    """

    _API_URLS = {
        "thepaper": "https://api.thepaper.cn/contentapi/wwwIndex/rightSidebar",
        "yicai": "https://www.yicai.com/api/ajax/getlatest?page=1&pagesize=20",
        "baidu_hot": "https://top.baidu.com/api/board?platform=wise&tab=realtime",
        "bilibili": "https://api.bilibili.com/x/web-interface/popular",
    }
    _API_PARSERS = {
        "thepaper": _APIParser.thepaper,
        "yicai": _APIParser.yicai,
        "baidu_hot": _APIParser.baidu_hot,
        "bilibili": _APIParser.bilibili,
    }

    def __init__(self, config=None):
        news_cfg = (config or {}).get("news", {})
        self.sources = news_cfg.get("sources", [])
        self.timeout = news_cfg.get("source_timeout", 10)
        self.max_workers = news_cfg.get("source_max_workers", 6)
        self.fresh_hours = news_cfg.get("fresh_hours", 72)
        # 相似度去重阈值（0 = 关闭），冲突时保留 priority 高的
        dedupe_on = news_cfg.get("dedupe_similar", True)
        self.similar_threshold = news_cfg.get("dedupe_threshold", 0.85) if dedupe_on else 0.0

    def fetch_all(self, limit_per_source=5):
        """并行抓取全部订阅源，返回统一格式列表（已清洗、去重、按时间降序）。"""
        if not self.sources:
            return []
        all_items = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(self.sources))
        ) as executor:
            future_map = {
                executor.submit(self._fetch_one, src, limit_per_source): src.get(
                    "name", "?")
                for src in self.sources
            }
            for future in concurrent.futures.as_completed(future_map):
                name = future_map[future]
                try:
                    all_items.extend(future.result())
                except Exception as e:
                    print(f"[NewsSource] 源 '{name}' 抓取失败: {e}")
        return self._post_process(all_items, self.similar_threshold)

    def _fetch_one(self, src, limit):
        if src.get("type") == "rss":
            return self._normalize_items(
                _RSSFetcher.fetch(src.get("url", ""), src.get("name", ""), limit,
                                  self.timeout),
                src,
            )
        if src.get("type") == "api":
            return self._fetch_api(src, limit)
        if src.get("type") == "html_list":
            return self._normalize_items(self._fetch_html_list(src, limit), src)
        return []

    @staticmethod
    def _main_domain(host: str) -> str:
        """提取注册主域：www.people.com.cn → people.com.cn；news.cctv.com → cctv.com。"""
        parts = (host or "").lower().split(".")
        if (
            len(parts) >= 3
            and parts[-1] in ("cn", "tw", "hk", "jp", "uk", "au")
            and parts[-2] in ("com", "org", "net", "gov", "edu", "ac")
        ):
            return ".".join(parts[-3:])
        return ".".join(parts[-2:])

    def _fetch_html_list(self, src, limit):
        """解析新闻站首页 HTML 的新闻列表（新华社/央视/人民网等无 RSS 的权威站，无需 Docker/RSSHub）。
        过滤规则：标题须含中文、链接须为站内主域（排除导航/外语/外链）。"""
        try:
            from urllib.parse import urlparse
            r = requests.get(src.get("url", ""), headers=DEFAULT_HEADERS, timeout=self.timeout)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
                tag.extract()
            base = re.match(r"^(https?://[^/]+)", src.get("url", "") or "")
            base = base.group(1) if base else ""
            main = NewsSourceFetcher._main_domain(urlparse(src.get("url", "")).hostname)
            items, seen = [], set()
            for a in soup.find_all("a", href=True):
                title = a.get_text(" ", strip=True)
                href = (a.get("href") or "").strip()
                if not (8 <= len(title) <= 60):
                    continue
                if href.startswith(("javascript", "#", "mailto:", "tel:")):
                    continue
                if not href.startswith("http") and base:
                    href = base + (href if href.startswith("/") else "/" + href)
                # 过滤：外域链接 + 不含中文的标题（导航/外语/栏目）
                h = urlparse(href).hostname or ""
                if not (h == main or h.endswith("." + main)):
                    continue
                if not re.search(r"[\u4e00-\u9fff]", title):
                    continue
                # 新闻页 URL 特征：含 8 位数字或日期模式（排除栏目/页脚/许可证页）
                if not re.search(r"\d{8}|20\d{2}/\d{4}|20\d{2}/\d{2}/\d{2}", href):
                    continue
                key = NewsSourceFetcher._norm_key(title)
                if key in seen:
                    continue
                seen.add(key)
                items.append({"title": title, "date": "", "link": href, "source": src.get("name", "")})
                if len(items) >= max(limit * 3, 12):
                    break
            return items[:limit]
        except Exception as e:
            print(f"[NewsSource] HTML 列表源 '{src.get('name')}' 失败: {e}")
            return []

    def _fetch_api(self, src, limit):
        api = src.get("api", "")
        url = self._API_URLS.get(api)
        parser = self._API_PARSERS.get(api)
        if not url or not parser:
            return []
        try:
            r = requests.get(url, headers=DEFAULT_HEADERS, timeout=self.timeout)
            r.raise_for_status()
            return self._normalize_items(parser(r.json(), limit), src)
        except Exception as e:
            print(f"[NewsSource] API 源 '{src.get('name')}' 失败: {e}")
            return []

    # ---------------------------------------------------------- 统一处理
    @staticmethod
    def _normalize_items(items, src):
        """补全字段：query=分类，priority=权威权重，清洗标题，确保核心字段都在。"""
        category = src.get("category", src.get("name", "资讯"))
        priority = int(src.get("priority", 3) or 3)
        result = []
        for it in items:
            title = NewsSourceFetcher.clean_title(it.get("title", ""))
            if not title:
                continue
            result.append(
                {
                    "title": title,
                    "date": it.get("date", ""),
                    "link": it.get("link", ""),
                    "source": it.get("source", src.get("name", "")),
                    "query": category,
                    "priority": priority,
                }
            )
        return result

    @staticmethod
    def clean_title(title):
        """去掉标题尾部 ' - 来源' 类后缀（含全角破折号/连字符/竖线变体），压缩多余空白。"""
        t = str(title).strip()
        # 半角连字符/下划线/竖线 + em/en dash + 全角连字符/竖线
        seps = r"\-\_|\u2014\u2013\uFF0D\uFF5C"
        for _ in range(3):
            new = re.sub(rf"\s*[{seps}]\s*[^{seps}\s]{{1,30}}$", "", t)
            if new == t:
                break
            t = new
        # 清理尾部残留的孤立分隔符（如连续破折号处理后的单个 '-'）
        t = re.sub(rf"\s*[{seps}]\s*$", "", t)
        return re.sub(r"\s+", " ", t).strip()

    @staticmethod
    def _norm_key(title):
        """标题归一化 key：去空白/全半角/小写，用于去重。"""
        t = title.lower()
        t = re.sub(r"[\s\u3000]+\s*", "", t)
        return t.translate(str.maketrans("！？（）【】：，", "!?()[]:,"))

    @staticmethod
    def _parse_date(date_str):
        if not date_str:
            return None
        try:
            dt = parsedate_to_datetime(str(date_str))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    def _post_process(self, items, similar_threshold=0.0):
        """新鲜度过滤（解析不了日期的保留）→ 精确去重 → 相似度去重 → 按时间降序。"""
        if self.fresh_hours:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=self.fresh_hours)
            kept = []
            for it in items:
                dt = self._parse_date(it.get("date"))
                if dt is None or dt >= cutoff:
                    kept.append(it)
            items = kept
        seen, unique = set(), []
        for it in items:
            key = self._norm_key(it["title"])
            if key not in seen:
                seen.add(key)
                unique.append(it)
        if similar_threshold and similar_threshold > 0:
            unique = NewsSourceFetcher._dedupe_similar(unique, similar_threshold)
        unique.sort(
            key=lambda x: (self._parse_date(x.get("date")) is not None,
                           self._parse_date(x.get("date")) or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )
        return unique

    @staticmethod
    def _jaccard_sim(a, b):
        """字符 bigram 的 Jaccard 相似度（中文标题效果好）。"""
        ab = {a[i:i + 2] for i in range(max(len(a) - 1, 0))}
        bb = {b[i:i + 2] for i in range(max(len(b) - 1, 0))}
        if not ab or not bb:
            return 0.0
        return len(ab & bb) / len(ab | bb)

    @staticmethod
    def _dedupe_similar(items, threshold):
        """贪心相似去重：按 priority 降序处理，与已保留项相似度 ≥ 阈值则丢弃。"""
        result = []
        ordered = sorted(
            items, key=lambda x: (x.get("priority", 0), x.get("date", "")), reverse=True
        )
        for it in ordered:
            key = NewsSourceFetcher._norm_key(it["title"])
            if any(
                NewsSourceFetcher._jaccard_sim(key, NewsSourceFetcher._norm_key(kept["title"])) >= threshold
                for kept in result
            ):
                continue
            result.append(it)
        return result


# ---------------------------------------------------------------- 正文提取
def fetch_article_content(url, max_chars=2000):
    """抓取新闻正文（BS4 版，移植自 skill 的 fetch_url_content + 句子边界截断）。

    返回最多 max_chars 字符的纯文本；失败时返回明确错误信息。
    """
    if not url or not url.startswith("http"):
        return "链接为空，无法抓取正文。"
    if "news.google.com" in url:
        return "该链接是 Google News 聚合跳转页，无法直接读取正文；可以让我用标题重新搜索，找到原文网站链接后读取。"
    try:
        r = requests.get(url, headers=DEFAULT_HEADERS, timeout=10, allow_redirects=True)
        if r.status_code != 200:
            return f"抓取失败（HTTP {r.status_code}），链接可能已失效。"
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe", "nav",
                         "footer", "header", "form"]):
            tag.extract()
        text = soup.get_text(separator="\n", strip=True)
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        text = "\n".join(lines)
        if not text:
            return "未能提取到正文内容（页面可能为纯图片或需登录）。"
        # 句子边界截断：优先在 。！？；\n 处截断，避免截断句子
        if len(text) > max_chars:
            cut = text[:max_chars]
            for sep in ("。", "！", "？", "；", "\n"):
                idx = cut.rfind(sep)
                if idx > max_chars * 0.6:
                    cut = cut[: idx + 1]
                    break
            else:
                cut = text[:max_chars]
            text = cut + "……（正文已截断）"
        return text
    except requests.RequestException as e:
        return f"抓取失败: {e}"
    except Exception as e:
        return f"正文解析失败: {e}"
