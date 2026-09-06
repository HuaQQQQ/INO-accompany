import os
import json
import time
import re
import threading
import urllib.parse
import concurrent.futures
import xml.etree.ElementTree as ET
import requests
import random


class NewsFetcher:
    def __init__(self, config=None):
        self.config = config or {}
        news_cfg = self.config.get("news", {})
        self.cache_file = news_cfg.get("cache_file", "news_cache.json")
        self.refresh_interval = news_cfg.get("refresh_interval", 3600) # 1 hour default
        self.queries = news_cfg.get("queries", [
            "NVIDIA AMD AI",
            "GPU 显卡 价格",
            "AI 芯片 算力",
            "GeForce Radeon 显卡"
        ])
        self.headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        # 健壮性配置
        self.fetch_retries = news_cfg.get("fetch_retries", 1)
        self.fetch_retry_delay = news_cfg.get("fetch_retry_delay", 0.5)
        self.fresh_hours = news_cfg.get("fresh_hours", 72)
        dedupe_on = news_cfg.get("dedupe_similar", True)
        self.dedupe_threshold = news_cfg.get("dedupe_threshold", 0.85) if dedupe_on else 0.0
        # 缓存写锁 + 后台刷新线程
        self._cache_lock = threading.Lock()
        self._bg_thread = None
        # 新闻偏好学习：用户点击/讲解过的新闻关键词，排序时加权提升
        self.prefs_file = news_cfg.get("prefs_file", "news_preferences.json")
        self._prefs_cache = None  # (data, ts) 懒加载缓存

    def fetch_latest_news(self) -> list:
        all_news = []

        # 第一梯队：固定订阅源（国内实测可用，链接为真实原文，正文可直接抓取）
        try:
            from news_sources import NewsSourceFetcher
            source_items = NewsSourceFetcher(self.config).fetch_all(limit_per_source=4)
            if source_items:
                all_news.extend(source_items)
                print(f"[NewsFetcher] 订阅源抓取 {len(source_items)} 条（第一梯队）")
        except Exception as e:
            print(f"[NewsFetcher] 订阅源抓取异常: {e}")

        # 第二梯队：关键词搜索（每查询 Google + Bing 并行，Bing 始终合并，带重试退避）
        if self.queries:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(self.queries), 4)
            ) as executor:
                future_map = {
                    executor.submit(self._fetch_search_engines, q): q for q in self.queries
                }
                for future in concurrent.futures.as_completed(future_map):
                    q = future_map[future]
                    try:
                        items = future.result()
                        if items:
                            all_news.extend(items)
                    except Exception as e:
                        print(f"[NewsFetcher] 搜索源 '{q}' 异常: {e}")

        return self._post_process(all_news)

    def _fetch_search_engines(self, query: str, per_engine: int = 4) -> list:
        """单个查询的 Google News + Bing News 并行抓取。"""
        encoded_q = urllib.parse.quote(query)
        urls = [
            f"https://news.google.com/rss/search?q={encoded_q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
            f"https://www.bing.com/news/search?q={encoded_q}&format=rss",
        ]
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_map = {
                executor.submit(self._fetch_rss_source, url, query, per_engine): url
                for url in urls
            }
            for future in concurrent.futures.as_completed(future_map):
                try:
                    results.extend(future.result())
                except Exception as e:
                    print(f"[NewsFetcher] {query} 子源异常: {e}")
        return results

    def _fetch_rss_source(self, url: str, query: str, limit: int) -> list:
        """抓取单个 RSS 搜索源，带重试退避。"""
        for attempt in range(self.fetch_retries + 1):
            try:
                r = requests.get(url, headers=self.headers, timeout=5)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                root = ET.fromstring(r.text)
                items = []
                for item in root.findall('.//item')[:limit]:
                    title = item.find('title')
                    if title is None or not title.text:
                        continue
                    items.append({
                        'title': title.text.strip(),
                        'date': (item.findtext('pubDate') or '').strip(),
                        'link': (item.findtext('link') or '').strip(),
                        'source': (item.findtext('source') or '').strip(),
                        'query': query,
                        'priority': 1,  # 搜索源权威权重低于订阅源
                    })
                return items
            except Exception as e:
                if attempt < self.fetch_retries:
                    time.sleep(self.fetch_retry_delay)
                    continue
                print(f"[NewsFetcher] RSS 搜索源失败 {url}: {e}")
        return []

    # ────────────────────────── 新闻偏好学习 ──────────────────────────
    _INTEREST_KEYWORDS = [
        "显卡", "芯片", "AI", "GPU", "CPU", "英伟达", "AMD", "苹果", "华为", "小米",
        "折叠屏", "半导体", "大模型", "RTX", "Radeon", "游戏", "笔记本", "平板",
        "汽车", "机器人", "OpenAI", "特斯拉", "手机", "骁龙", "天玑", "算力",
        "量子", "光伏", "新能源", "存储", "内存", "硬盘", "显示器", "耳机", "摄影",
    ]

    @staticmethod
    def _extract_interest_terms(text: str) -> list:
        """从文本提取兴趣关键词：预置词典命中 + 字母数字 token（如 RTX5090）。"""
        if not text:
            return []
        low = text.lower()
        terms = set()
        for kw in NewsFetcher._INTEREST_KEYWORDS:
            if kw.lower() in low:
                terms.add(kw)
        for tok in re.findall(r"[a-zA-Z][a-zA-Z0-9\-]{2,12}", text):
            if len(tok) >= 3:
                terms.add(tok)
        return list(terms)[:12]

    def _load_prefs(self) -> dict:
        """懒加载偏好文件 {词: 次数}，60 秒缓存。"""
        now = time.time()
        if self._prefs_cache and now - self._prefs_cache[1] < 60:
            return self._prefs_cache[0]
        data = {}
        try:
            if os.path.exists(self.prefs_file):
                with open(self.prefs_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
        except Exception:
            data = {}
        self._prefs_cache = (data, now)
        return data

    def record_interest(self, text: str):
        """记录用户感兴趣的新闻文本（点击讲解等场景），偏好词计数 +1。"""
        terms = self._extract_interest_terms(text)
        if not terms:
            return
        with self._cache_lock:
            data = self._load_prefs()
            for t in terms:
                data[t] = data.get(t, 0) + 1
            # 只保留计数最高的 50 个词，防止膨胀
            top = sorted(data.items(), key=lambda kv: kv[1], reverse=True)[:50]
            data = dict(top)
            try:
                with open(self.prefs_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[NewsPrefs] 保存失败: {e}")
            self._prefs_cache = (data, time.time())
        print(f"[NewsPrefs] 已记录兴趣词: {terms[:6]}")

    def get_preference_terms(self) -> list:
        """返回偏好词列表（计数 >= 2 视为稳定兴趣，避免单次噪声）。"""
        return [t for t, c in self._load_prefs().items() if c >= 2]

    def _post_process(self, all_news: list) -> list:
        """统一后处理：新鲜度过滤 → 标题清洗 → 精确去重（权威优先）→ 相似去重 → 时间降序。"""
        from datetime import datetime, timedelta, timezone
        from news_sources import NewsSourceFetcher as _NSF

        # 1) 新鲜度过滤（无日期条目保留，容错）
        if self.fresh_hours:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=self.fresh_hours)
            kept = []
            for it in all_news:
                d = it.get('date', '')
                try:
                    dt = _NSF._parse_date(d)
                    if dt is None or dt >= cutoff:
                        kept.append(it)
                except Exception:
                    kept.append(it)
            all_news = kept

        # 2) 标题清洗（去 " - 来源" 后缀）
        for it in all_news:
            it['title'] = _NSF.clean_title(it.get('title', ''))

        # 3) 精确去重：冲突时保留 priority 高 / 有真实链接的
        seen = {}
        for it in all_news:
            key = _NSF._norm_key(it.get('title', ''))
            if not key:
                continue
            if key not in seen:
                seen[key] = it
                continue
            old = seen[key]
            old_p, new_p = old.get('priority', 0), it.get('priority', 0)
            if new_p > old_p or (new_p == old_p and it.get('link') and not old.get('link')):
                seen[key] = it
        unique_news = list(seen.values())

        # 4) 相似度去重（同主题不同标题，保留权威源）
        if self.dedupe_threshold and self.dedupe_threshold > 0:
            unique_news = _NSF._dedupe_similar(unique_news, self.dedupe_threshold)

        # 5) 排序：偏好命中优先 → 有日期优先 → 时间降序（偏好词加权，让感兴趣的主题靠前）
        prefs = self.get_preference_terms()

        def _rank(it):
            title_low = (it.get('title') or '').lower()
            hits = sum(1 for w in prefs if w.lower() in title_low)
            dt = _NSF._parse_date(it.get('date'))
            return (
                hits,
                dt is not None,
                dt or datetime.min.replace(tzinfo=timezone.utc),
            )

        unique_news.sort(key=_rank, reverse=True)
        return unique_news

    def fetch_article_content(self, url: str, max_chars: int = 800) -> str:
        """抓取新闻链接的详细正文内容（BS4 提取，句子边界截断）。
        返回最多 max_chars 字符的正文；失败时返回明确错误信息。"""
        from news_sources import fetch_article_content as _fetch_content
        return _fetch_content(url, max_chars)

    def get_news(self, force_refresh=False) -> list:
        now = time.time()
        # 读缓存（加锁，避免读到半截文件）
        with self._cache_lock:
            if not force_refresh and os.path.exists(self.cache_file):
                try:
                    with open(self.cache_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        if now - data.get('timestamp', 0) < self.refresh_interval:
                            return data.get('news', [])
                except Exception:
                    pass

        # 刷新（锁外执行抓取，避免长时间占用锁）
        news = self.fetch_latest_news()
        if news:
            with self._cache_lock:
                try:
                    with open(self.cache_file, 'w', encoding='utf-8') as f:
                        json.dump({'timestamp': now, 'news': news}, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    print(f"Error saving news cache: {e}")
            return news

        # stale-while-revalidate：刷新失败时回退旧缓存，避免侧边栏被清空
        with self._cache_lock:
            try:
                if os.path.exists(self.cache_file):
                    with open(self.cache_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        if data.get('news'):
                            print(f"[NewsFetcher] 刷新失败，回退旧缓存（{len(data['news'])} 条）")
                            return data['news']
            except Exception:
                pass
        return []

    def start_background_refresh(self, interval_minutes=30):
        """启动后台静默刷新线程（daemon，随主进程退出）。"""
        if self._bg_thread is not None and self._bg_thread.is_alive():
            return

        def _loop():
            while True:
                time.sleep(max(int(interval_minutes or 30), 5) * 60)
                try:
                    news = self.get_news(force_refresh=True)
                    print(f"[NewsFetcher] 后台静默刷新完成，当前 {len(news)} 条")
                except Exception as e:
                    print(f"[NewsFetcher] 后台刷新异常: {e}")

        self._bg_thread = threading.Thread(target=_loop, daemon=True, name="news-bg-refresh")
        self._bg_thread.start()
        print(f"[NewsFetcher] 后台刷新线程已启动（每 {interval_minutes} 分钟）")

    def _save_queries_to_config(self):
        """Persist current queries list to config.yaml on disk."""
        import yaml
        try:
            cfg = {}
            if os.path.exists("config.yaml"):
                with open("config.yaml", "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            if "news" not in cfg:
                cfg["news"] = {}
            cfg["news"]["queries"] = self.queries
            with open("config.yaml", "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True)
            print(f"📰 [NewsFetcher] 成功将新闻关注列表持久化到 config.yaml: {self.queries}")
        except Exception as e:
            print(f"[NewsFetcher Config Save Error]: {e}")

    def add_query(self, query_topic: str) -> str:
        """Add a new news query topic dynamically and force-refresh the news feed."""
        q = query_topic.strip()
        if not q:
            return "主题名称不能为空。"
        if q in self.queries:
            return f"主题 '{q}' 已经在当前的资讯关注列表中了。"
        
        self.queries.append(q)
        self._save_queries_to_config()
        self.get_news(force_refresh=True)
        return f"✨ 成功添加新的资讯关注主题 '{q}' 并已实时刷新抓取！当前关注主题数：{len(self.queries)}。"

    def remove_query(self, query_topic: str) -> str:
        """Remove a news query topic dynamically and refresh the news feed."""
        q = query_topic.strip()
        matched = [existing for existing in self.queries if q.lower() in existing.lower()]
        if not matched:
            return f"未在关注列表中找到匹配 '{q}' 的主题。当前关注列表：{self.queries}"
        
        for item in matched:
            self.queries.remove(item)
        self._save_queries_to_config()
        self.get_news(force_refresh=True)
        return f"🗑️ 成功从资讯关注列表中移除 '{', '.join(matched)}' 并重新刷新资讯库。"

    def list_queries(self) -> str:
        """List all currently subscribed news query topics."""
        if not self.queries:
            return "当前暂未关注任何资讯主题。"
        lines = [f"{i+1}. {q}" for i, q in enumerate(self.queries)]
        return "【INO 当前订阅关注的资讯主题列表】：\n" + "\n".join(lines)

    def get_random_news_item(self) -> dict:
        news = self.get_news()
        if news:
            return random.choice(news)
        return {}

    def get_sidebar_news_prompt(self, count=5) -> str:
        news = self.get_news()
        if not news:
            return ""
        items = news[:count]
        lines = [f"{i+1}. [{item.get('query', '硬件/科技')}] {item['title']}" for i, item in enumerate(items)]
        return "【当前副屏右侧资讯面板 (News Feed) 实时内容】：\n" + "\n".join(lines) + "\n"

    def find_relevant_news(self, query: str, top_k=3) -> list:
        news = self.get_news()
        if not news:
            return []
        q_lower = query.lower()
        # 提取检索关键词（支持空格、标点切分，过滤单字干扰）
        q_tokens = [t for t in re.split(r'[\s,，、。！？!?]+', q_lower) if len(t) >= 2]
        matched = []
        for item in news:
            title_lower = item.get('title', '').lower()
            tag_lower = item.get('query', '').lower()
            if q_tokens and any(t in title_lower or t in tag_lower for t in q_tokens):
                matched.append(item)
        return matched[:top_k]

    def get_news_prompt_snippet(self, count=2) -> str:
        news = self.get_news()
        if not news:
            return ""
        samples = random.sample(news, min(count, len(news)))
        lines = [f"- {item['title']}" for item in samples]
        return "\n".join(lines)

    def clean_search_query(self, text: str) -> str:
        """Clean conversational user prompt into concise keywords for web search."""
        import re
        q = text.strip()
        # 1. Strip common prefix tags
        q = re.sub(r'^(新闻|搜索|查询|消息|资讯)[：:\s]+', '', q)
        
        # 2. Normalize Taiwanese/informal tech terms
        q = q.replace('笔电', '笔记本')
        
        # 3. Strip conversational fillers
        fillers = [
            '能不能帮我', '可以帮我', '麻烦帮我', '帮我', '请问', '你知道', '告诉我',
            '我想知道', '我想要了解下', '欧想要了解', '我想要了解', '我想了解下', '我想了解一下', '我想了解', '我想看下', '我想看', '我想查下', '我想查', '我想听听', '我想', '我要',
            '查一下', '查查', '查', '搜一下', '搜搜', '搜', '搜索一下', '搜索', '找找', '找一下', '找',
            '了解一下', '了解下', '了解', '看一下', '看下', '看看', '讲讲', '聊聊', '说说', '介绍下', '介绍一下',
            '有没有关于', '关于', '最近有没有', '有没有', '最近有什么', '有什么', '最近的', '最新的', '最新',
            '的最新资讯', '的最新消息', '的最新动态', '的最新新闻', '的新闻', '的动态', '的资讯', '的消息',
            '怎么样了', '怎么样', '如何', '什么时候出', '什么时候发布', '什么时候上市', '发布了吗', '多少钱',
            '和我', '跟我', '吧', '呀', '呢', '啊', '特别', '尤其', '不那么', '常规的', '常规', '普通的', '普通',
            '一些', '那些', '这个', '那个', '产品', '动态', '新品'
        ]
        cleaned = q
        for f in fillers:
            cleaned = cleaned.replace(f, ' ')
        cleaned = re.sub(r'[？?！!，,。.\n\t：:]+', ' ', cleaned).strip()
        cleaned = ' '.join(cleaned.split())
        
        if len(cleaned) < 2:
            return q
        return cleaned

    def _format_rss_item(self, item: ET.Element) -> str:
        """格式化 RSS 条目：- 标题 [来源] (链接)"""
        title = item.findtext('title') or ''
        source = (item.findtext('source') or '').strip()
        link = (item.findtext('link') or '').strip()
        line = f"- {title.strip()}"
        if source:
            line += f" [{source}]"
        if link:
            line += f" ({link})"
        return line
    
    def _search_duckduckgo(self, query: str, max_results: int = 4) -> list:
        """DuckDuckGo HTML 搜索：返回真实原文链接（标题 + 域名 + 链接），供正文抓取。"""
        try:
            r = requests.post(
                'https://html.duckduckgo.com/html/',
                data={'q': query},
                headers=self.headers,
                timeout=8
            )
            if r.status_code != 200:
                return []
            results = []
            for m in re.finditer(
                r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                r.text, re.DOTALL
            ):
                href, title_html = m.group(1), m.group(2)
                title = re.sub(r'<[^>]+>', '', title_html).strip()
                if not title:
                    continue
                if href.startswith('//'):
                    href = 'https:' + href
                domain = re.sub(r'^https?://(www\.)?', '', href).split('/')[0]
                results.append(f"- {title} [{domain}] ({href})")
                if len(results) >= max_results:
                    break
            return results
        except Exception as e:
            print(f"[DDG Search Warning]: {e}")
            return []

    def search_web(self, query: str) -> str:
        """Perform multi-engine search (Google News RSS + Bing News RSS fallback) with query normalization."""
        search_term = self.clean_search_query(query)
    
        # Build search candidate terms to try sequentially if first fails
        terms_to_try = [search_term]
    
        # Fallback term: extract brand/alphanumeric words + core tech nouns
        import re
        brands = re.findall(r'[a-zA-Z0-9\+\#\-]+', query)
        chinese_nouns = [w for w in ['笔记本', '电脑', '显卡', '手机', '芯片', '游戏', '折叠屏', '平板'] if w in query or w in search_term]
        if brands:
            fallback_term = ' '.join(brands + chinese_nouns).strip()
            if fallback_term and fallback_term not in terms_to_try:
                terms_to_try.append(fallback_term)
            if brands[0] not in terms_to_try:
                terms_to_try.append(brands[0])
    
        for term in terms_to_try:
            results = []
            encoded_q = urllib.parse.quote(term)
    
            # 1. Google News RSS
            try:
                g_url = f"https://news.google.com/rss/search?q={encoded_q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
                r = requests.get(g_url, headers=self.headers, timeout=5)
                if r.status_code == 200:
                    root = ET.fromstring(r.text)
                    for item in root.findall('.//item')[:5]:
                        line = self._format_rss_item(item)
                        if line != "- ":
                            results.append(line)
            except Exception as e:
                print(f"[Google RSS Warning]: {e}")
    
            # 2. Bing News RSS（补充真实可抓取的原文链接，与 Google 结果去重合并）
            try:
                b_url = f"https://www.bing.com/news/search?q={encoded_q}&format=rss"
                r = requests.get(b_url, headers=self.headers, timeout=5)
                if r.status_code == 200:
                    root = ET.fromstring(r.text)
                    for item in root.findall('.//item')[:5]:
                        line = self._format_rss_item(item)
                        if line != "- " and line not in results:
                            results.append(line)
            except Exception as e:
                print(f"[Bing RSS Warning]: {e}")

            # 3. DuckDuckGo HTML（真实原文链接源，INO 可直接读取正文）
            try:
                ddg_lines = self._search_duckduckgo(term, max_results=4)
                for line in ddg_lines:
                    if line not in results:
                        results.append(line)
            except Exception as e:
                print(f"[DDG Warning]: {e}")

            if results:
                return "\n".join(results[:6])
    
        return "未检索到更多实时信息（网络搜索可能受限）"

    def update_sidebar_with_search(self, query: str) -> list:
        """Search the web for a targeted query and inject fresh results at the top of the sidebar news cache."""
        from news_sources import NewsSourceFetcher as _NSF

        search_term = self.clean_search_query(query)
        new_items = []

        encoded_q = urllib.parse.quote(search_term)
        # Google News RSS（带 link/source 字段）
        try:
            g_url = f"https://news.google.com/rss/search?q={encoded_q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
            r = requests.get(g_url, headers=self.headers, timeout=5)
            if r.status_code == 200:
                root = ET.fromstring(r.text)
                for item in root.findall('.//item')[:4]:
                    title = item.find('title')
                    if title is not None and title.text:
                        new_items.append({
                            'title': title.text.strip(),
                            'date': (item.findtext('pubDate') or '').strip(),
                            'link': (item.findtext('link') or '').strip(),
                            'source': (item.findtext('source') or '').strip(),
                            'query': search_term,
                            'priority': 1,
                        })
        except Exception as e:
            print(f"[NewsFetcher] Google RSS failed: {e}")

        # Bing News RSS（始终合并，不再纯兜底）
        try:
            b_url = f"https://www.bing.com/news/search?q={encoded_q}&format=rss"
            r = requests.get(b_url, headers=self.headers, timeout=5)
            if r.status_code == 200:
                root = ET.fromstring(r.text)
                for item in root.findall('.//item')[:4]:
                    title = item.find('title')
                    if title is not None and title.text:
                        new_items.append({
                            'title': title.text.strip(),
                            'date': (item.findtext('pubDate') or '').strip(),
                            'link': (item.findtext('link') or '').strip(),
                            'source': (item.findtext('source') or '').strip(),
                            'query': search_term,
                            'priority': 1,
                        })
        except Exception as e:
            print(f"[NewsFetcher] Bing RSS failed: {e}")

        # 标题清洗 + 补全真实链接（Google 跳转壳 → DDG 匹配原文链接）
        for it in new_items:
            it['title'] = _NSF.clean_title(it.get('title', ''))
        try:
            real_links = {}  # 归一化标题 → (真实链接, 域名)
            for line in self._search_duckduckgo(search_term, max_results=8):
                m = re.match(r'- (.*?) \[(.*?)\] \((https?://[^)]+)\)', line)
                if m:
                    real_links[_NSF._norm_key(m.group(1))] = (m.group(3), m.group(2))
            for it in new_items:
                if not it.get('link') or 'news.google.com' in it.get('link', ''):
                    key = _NSF._norm_key(it['title'])
                    if key in real_links:
                        it['link'], domain = real_links[key]
                        if not it.get('source'):
                            it['source'] = domain
                    else:
                        best = max(
                            real_links.items(),
                            key=lambda kv: _NSF._jaccard_sim(key, kv[0]),
                            default=None,
                        )
                        if best and _NSF._jaccard_sim(key, best[0]) > 0.5:
                            it['link'], domain = best[1]
                            if not it.get('source'):
                                it['source'] = domain
        except Exception as e:
            print(f"[NewsFetcher] DDG 链接解析失败: {e}")

        # 挂载条目新鲜度过滤（与 fetch_latest_news 一致：72h 内或无日期），防止 2024 老新闻上侧边栏
        if self.fresh_hours and new_items:
            from datetime import datetime, timedelta, timezone
            cutoff = datetime.now(timezone.utc) - timedelta(hours=self.fresh_hours)
            kept = []
            for it in new_items:
                dt = _NSF._parse_date(it.get('date', ''))
                if dt is None or dt >= cutoff:
                    kept.append(it)
            if len(kept) < len(new_items):
                print(f"[NewsFetcher] 挂载过滤掉 {len(new_items) - len(kept)} 条过期新闻（> {self.fresh_hours}h）")
            new_items = kept

        if not new_items:
            return []

        # 合并缓存（锁内读写），归一化去重，新条目置顶
        existing = self.get_news()
        with self._cache_lock:
            seen = {_NSF._norm_key(it['title']): True for it in new_items}
            filtered_existing = [
                it for it in existing
                if _NSF._norm_key(it.get('title', '')) not in seen
            ]
            merged_news = new_items + filtered_existing
            try:
                with open(self.cache_file, 'w', encoding='utf-8') as f:
                    json.dump({'timestamp': time.time(), 'news': merged_news}, f,
                              ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[NewsFetcher Error saving cache]: {e}")

        return new_items
