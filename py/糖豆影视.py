# -*- coding: utf-8 -*-
"""
糖豆影视 (www.tdys.cc) - TVBox 爬虫源 (maccms / mxpro 模板)
============================================================
接口：homeContent / categoryContent(含筛选) / detailContent(懒加载) / playerContent(按需解析m3u8直链) / searchContent

站点特性（已实测确认）：
1. 站内搜索已关闭：/so/、/index.php/vod/search.html、/search/ 均 404，suggest 接口一律返回"参数错误"
   -> searchContent 用"分类爬取兜底 + 分词模糊评分"实现（与蓝天影视同策略）
2. 播放链路：详情页 /vod-detail/{id}.html -> 播放入口 /free-play/{id}-{sid}-{nid}.html
   -> 播放页内 player_xxxx JSON 的 url 字段即真实 m3u8 直链（URL 编码，unquote 即得直链，无需二次解析）
3. 分类列表：/show/{tidslug}------------.html(第1页)，翻页 /show/{tidslug}--------{n}---.html
4. 筛选 URL 用"-"占位定长；已验证 class+area 组合格式 /show/{slug}-{area}--{class}--------.html

核心优化：
- 详情页懒加载：只解析集数列表，不进播放页，秒开
- playerContent 按需解析 m3u8 直链 + 15分钟缓存 + 后台预取第1集/下一集
- 多级缓存：首页10分钟 / 分类5分钟 / 详情5分钟(失败30秒) / 搜索3分钟 / 播放15分钟
- 全链路短超时(8s/5s) + 快速重试(0.3s) + 429限流等待(2s)
- 连接池复用 + gzip 自动解压
- 筛选：类型(二级分类) / 地区 / 年份 / 排序
"""

import re
import json
import time
import threading
from urllib.parse import quote, unquote, urlencode

import requests
from requests.adapters import HTTPAdapter

try:
    from concurrent.futures import ThreadPoolExecutor, as_completed
except ImportError:
    ThreadPoolExecutor = None
    as_completed = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

try:
    import sys
    sys.path.append('..')
    from base.spider import Spider as _BaseSpider
except ImportError:
    _BaseSpider = None


# ============================================================
# 常量
# ============================================================
HOST = "https://www.tdys.cc"

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

# 超时（秒）
TIMEOUT_PAGE = 8
TIMEOUT_API = 5
TIMEOUT_PLAY = 4

# 缓存 TTL（秒）
TTL_HOME = 600
TTL_CAT = 300
TTL_DETAIL_OK = 300
TTL_DETAIL_EMPTY = 30
TTL_SEARCH = 180
TTL_PLAY = 900

# 地区（与站点筛选 tab 保持一致）
AREAS = [
    "大陆", "香港", "台湾", "美国", "法国", "英国",
    "日本", "韩国", "德国", "泰国", "意大利", "西班牙",
    "加拿大", "印度", "新加坡", "其它",
]

# 年份
YEARS = [str(y) for y in range(2026, 2005, -1)]

# 一级分类（tidslug 稳定）+ 二级分类（类型筛选，来自站点实测 tab）
CATS = [
    {"id": "dianying", "name": "电影", "subs": [
        ("qita", "其它"), ("maoxian", "冒险"), ("juqing", "剧情"),
        ("dongzuo", "动作"), ("donghua", "动画"), ("lishi", "历史"),
        ("xiju", "喜剧"), ("qihuan", "奇幻"), ("kongbu", "恐怖"),
        ("xuanyi", "悬疑"), ("jingsong", "惊悚"), ("zhanzheng", "战争"),
        ("gewu", "歌舞"), ("zainan", "灾难"), ("aiqing", "爱情"),
        ("fanzui", "犯罪"), ("kehuan", "科幻"), ("jilu", "纪录"),
        ("diezhan", "谍战"),
    ]},
    {"id": "juji", "name": "剧集", "subs": [
        ("xiangcun", "乡村"), ("qita", "其它"), ("juqing", "剧情"),
        ("dongzuo", "动作"), ("lishi", "历史"), ("guzhuang", "古装"),
        ("xiju", "喜剧"), ("qihuan", "奇幻"), ("jiating", "家庭"),
        ("xuanyi", "悬疑"), ("qingjing", "情景"), ("zhanzheng", "战争"),
        ("fanzui", "犯罪"), ("jingdian", "经典"), ("wangju", "网剧"),
        ("qingchunouxiang", "青春偶像"),
    ]},
    {"id": "zongyi", "name": "综艺", "subs": [
        ("tiyu", "体育"), ("bagua", "八卦"), ("qita", "其它"),
        ("shaoer", "少儿"), ("qinggan", "情感"), ("gaoxiao", "搞笑"),
        ("bobao", "播报"), ("shishang", "时尚"), ("wanhui", "晚会"),
        ("quyi", "曲艺"), ("gewu", "歌舞"), ("qiche", "汽车"),
        ("youxi", "游戏"), ("shenghuo", "生活"), ("zhenrenxiu", "真人秀"),
        ("kejiao", "科教"), ("jishi", "纪实"), ("tuokouxiu", "脱口秀"),
        ("fangtan", "访谈"), ("caijing", "财经"), ("xuanxiu", "选秀"),
        ("yinyue", "音乐"),
    ]},
    {"id": "dongman", "name": "动漫", "subs": [
        ("maoxian", "冒险"), ("dongzuo", "动作"), ("shaonian", "少年"),
        ("qinggan", "情感"), ("zhanzheng", "战争"), ("tuili", "推理"),
        ("gaoxiao", "搞笑"), ("jizhan", "机战"), ("xiaoyuan", "校园"),
        ("rexue", "热血"), ("kehuan", "科幻"), ("luoli", "萝莉"),
        ("yundong", "运动"),
    ]},
]

CATS_TR = {  # 类型 slug -> 中文（filter value 直接用中文，URL 需 quote）
    "sub_hash": None,  # 占位
}


def _build_filters(cat):
    """构建筛选器：类型(二级分类) / 地区 / 年份 / 排序
    注意：此站 URL 用中文值 + 定长'-'占位，value 传中文，构造时 quote。
    """
    subs = [{"n": name, "v": name} for _slug, name in cat["subs"]]
    filters = [{
        "key": "class", "name": "类型",
        "value": [{"n": "全部", "v": ""}] + subs,
    }]
    filters.append({
        "key": "area", "name": "地区",
        "value": [{"n": "全部", "v": ""}] + [{"n": a, "v": a} for a in AREAS],
    })
    filters.append({
        "key": "year", "name": "年份",
        "value": [{"n": "全部", "v": ""}] + [{"n": y, "v": y} for y in YEARS],
    })
    filters.append({
        "key": "by", "name": "排序",
        "value": [
            {"n": "最新", "v": "time"},
            {"n": "最热", "v": "hits"},
            {"n": "评分", "v": "score"},
        ],
    })
    return filters


# 全部分类 + 筛选器
ALL_CLASSES = [{"type_id": c["id"], "type_name": c["name"], "filter": 1} for c in CATS]
ALL_FILTERS = {c["id"]: _build_filters(c) for c in CATS}


def _show_url(tid, cls='', area='', year='', by='', page=1):
    """按站点定长'-'占位格式构造列表页 URL。
    已实测：无筛选翻页 /show/{tid}--------{n}---.html 可用；
    带筛选的组合翻页不稳定，故带筛选时只用第 1 页（由调用方将 page 置 1）。
    """
    if cls and area:
        return f"{HOST}/show/{tid}-{quote(area)}--{quote(cls)}--------.html"
    if cls:
        return f"{HOST}/show/{tid}---{quote(cls)}--------.html"
    if area:
        return f"{HOST}/show/{tid}-{quote(area)}----------.html"
    if year:
        return f"{HOST}/show/{tid}-----------{quote(year)}.html"
    if by:
        return f"{HOST}/show/{tid}--{quote(by)}---------.html"
    page = max(1, int(page or 1))
    if page > 1:
        return f"{HOST}/show/{tid}--------{page}---.html"
    return f"{HOST}/show/{tid}-----------.html"


# ============================================================
# Spider 主类
# ============================================================
_Base = _BaseSpider if _BaseSpider is not None else object


class Spider(_Base):
    siteUrl = HOST
    headers = {
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Referer': HOST + '/',
    }

    # ===== 初始化 =====
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.session.headers['Connection'] = 'keep-alive'
        self.session.verify = False
        adapter = HTTPAdapter(
            pool_connections=20, pool_maxsize=40,
            max_retries=0, pool_block=False,
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

        self._warm_thread = None

        # 缓存容器 + 锁
        self._lock = threading.Lock()
        self._home_cache = []
        self._home_cache_time = 0
        self._cat_cache = {}
        self._detail_cache = {}
        self._search_cache = {}
        self._play_cache = {}
        self._prefetching = set()

    def init(self, extend=""):
        self.extend = extend or ""

    # ===== 网络工具 =====
    def _get(self, url, referer='', timeout=TIMEOUT_PAGE):
        headers = {'Connection': 'keep-alive'}
        if referer:
            headers['Referer'] = referer
        for attempt in range(2):
            try:
                r = self.session.get(url, timeout=timeout, headers=headers)
                if r.status_code == 429:
                    time.sleep(2.0)
                    continue
                r.raise_for_status()
                r.encoding = r.apparent_encoding or 'utf-8'
                return r
            except Exception:
                if attempt == 0:
                    time.sleep(0.2)
                else:
                    return None
        return None

    def _get_text(self, url, referer='', timeout=TIMEOUT_PAGE):
        r = self._get(url, referer, timeout)
        return r.text if r is not None else ""

    def _warm(self):
        with self._lock:
            if self._home_cache:
                return
        try:
            html = self._get_text(HOST, timeout=TIMEOUT_API)
            cards = self._parse_cards(html, limit=48)
            if cards:
                with self._lock:
                    self._home_cache = cards
                    self._home_cache_time = int(time.time())
        except Exception:
            pass

    # ===== 缓存 =====
    @staticmethod
    def _cache_get(cache, key, ttl=None):
        item = cache.get(key)
        if item and time.time() - item[0] < (ttl if ttl is not None else item[2]):
            return item[1]
        return None

    @staticmethod
    def _cache_set(cache, key, value, ttl=TTL_CAT):
        if len(cache) > 512:
            cache.clear()
        cache[key] = (time.time(), value, ttl)

    # ===== HTML 解析 =====
    @staticmethod
    def _soup(html):
        if not html or BeautifulSoup is None:
            return None
        try:
            return BeautifulSoup(html, 'lxml')
        except Exception:
            try:
                return BeautifulSoup(html, 'html.parser')
            except Exception:
                return None

    @staticmethod
    def _abs(u):
        u = (u or '').strip()
        if not u:
            return ''
        if u.startswith('//'):
            return 'https:' + u
        if u.startswith('/'):
            return HOST + u
        if not u.startswith('http'):
            return HOST + '/' + u
        return u

    @staticmethod
    def _extract_id(href):
        m = re.search(r'/vod-detail/(\d+)\.html', (href or '').strip())
        return m.group(1) if m else None

    @staticmethod
    def _clean_name(raw):
        if not raw:
            return raw
        return re.sub(r'\s*[（(]\s*\d{4}\s*[）)]\s*$', '', raw.strip())

    @staticmethod
    def _pick_pic(el):
        if el is None:
            return ''
        img = el if el.name == 'img' else el.find('img')
        if img is not None:
            for attr in ('data-original', 'data-src', 'src'):
                val = str(img.get(attr) or '').strip()
                if val and 'load.gif' not in val:
                    return Spider._abs(val)
        # banner 用 style background-image
        style = str(el.get('style') or '')
        m = re.search(r'background[^:]*:\s*url\(([^)]+)\)', style)
        if m:
            return Spider._abs(m.group(1).strip().strip('"').strip("'"))
        return ''

    def _parse_cards(self, html, limit=36):
        """统一解析首页/列表/搜索结果卡片。
        支持 module-item 卡片 与 首页 banner 轮播（标题在 .mobile-v-info .v-title）。
        """
        if not html:
            return []
        soup = self._soup(html)
        if soup is None:
            return []
        items = {}
        cards = soup.select('a[href*="/vod-detail/"]')
        for a in cards:
            href = str(a.get('href') or '')
            vid = self._extract_id(href)
            if not vid or vid in items:
                continue
            # 标题
            title = ''
            te = a.select_one('.module-poster-item-title')
            if te is not None:
                title = te.get_text(strip=True)
            if not title and 'banner' in (a.get('class') or []):
                vb = a.find_next('div', class_='mobile-v-info')
                if vb is not None:
                    vte = vb.select_one('.v-title span') or vb.select_one('.v-title')
                    if vte is not None:
                        title = vte.get_text(strip=True)
            if not title:
                title = str(a.get('title') or '').strip()
            if not title:
                img = a.find('img')
                if img is not None and img.get('alt'):
                    title = str(img.get('alt')).strip()
            if not title:
                continue

            pic = self._pick_pic(a)
            remarks = ''
            note = a.select_one('.module-item-note') or a.select_one('.module-item-remarks')
            if note:
                remarks = note.get_text(strip=True)
            if not remarks:
                m = re.search(
                    r'(更新至[^\s]{0,12}|更新到[^\s]{0,12}|全\d+集|更新至第\d+集|完结|全集'
                    r'|已完结|HD中字|HD国语|TC中字|高清|抢先版)', title
                )
                if m:
                    remarks = m.group(1)
            items[vid] = {
                'vod_id': vid,
                'vod_name': self._clean_name(title),
                'vod_pic': pic,
                'vod_remarks': remarks or '',
            }
        return list(items.values())[:limit]

    # ============================================================
    # 首页
    # ============================================================
    def homeContent(self, filter=False):
        vod_list = []
        now = int(time.time())
        with self._lock:
            if self._home_cache and now - self._home_cache_time < TTL_HOME:
                vod_list = self._home_cache[:48]
        if not vod_list:
            try:
                html = self._get_text(HOST)
                vod_list = self._parse_cards(html, limit=48)
                if vod_list:
                    with self._lock:
                        self._home_cache = vod_list
                        self._home_cache_time = int(time.time())
            except Exception:
                pass
        return {
            "class": ALL_CLASSES,
            "filters": ALL_FILTERS,
            "list": vod_list,
        }

    def homeVideoContent(self):
        now = int(time.time())
        with self._lock:
            if self._home_cache and now - self._home_cache_time < TTL_HOME:
                return {"list": self._home_cache[:48]}
        try:
            html = self._get_text(HOST)
            vod_list = self._parse_cards(html, limit=48)
            if vod_list:
                with self._lock:
                    self._home_cache = vod_list
                    self._home_cache_time = int(time.time())
            return {"list": vod_list[:48]}
        except Exception:
            return {"list": []}

    # ============================================================
    # 分类列表（含筛选）
    # ============================================================
    def _empty_category(self, page=1):
        return {"list": [], "page": page, "pagecount": 1, "limit": 36, "total": 0}

    def categoryContent(self, tid, pg, filter, extend):
        try:
            page = max(1, int(pg or 1))
            ext = {}
            if extend:
                if isinstance(extend, dict):
                    ext = extend
                elif isinstance(extend, str):
                    try:
                        ext = json.loads(extend)
                    except Exception:
                        ext = {}
            # 提取筛选值（v 传的是中文，URL 构造时 quote）
            cls = (ext.get('class') or '').strip()
            area = (ext.get('area') or '').strip()
            year = (ext.get('year') or '').strip()
            by = (ext.get('by') or '').strip()
            # 带筛选时该站各组合翻页不稳定，统一只用第 1 页
            if cls or area or year or by:
                page = 1

            ckey = "%s|%d|%s|%s|%s|%s|%s" % (
                tid, page, cls, area, year, by,
                json.dumps(ext, ensure_ascii=False, sort_keys=True),
            )
            cached = self._cache_get(self._cat_cache, ckey, TTL_CAT)
            if cached is not None:
                return cached

            url = _show_url(tid, cls, area, year, by, page)
            html = self._get_text(url)
            if not html:
                return self._empty_category(page)

            vod_list = self._parse_cards(html, limit=36)

            # 估算总页数（列表页含 `--------{n}---` 翻页链接）
            pagecount = 1
            nums = [int(x) for x in re.findall(r'/show/[a-z-]+-+(\d{1,4})-+\.html', html)]
            pagecount = max(nums) if nums else 1
            if not vod_list:
                pagecount = 1

            result = {
                "list": vod_list,
                "page": page,
                "pagecount": pagecount,
                "limit": 36,
                "total": 0,
            }
            self._cache_set(self._cat_cache, ckey, result, TTL_CAT)
            return result
        except Exception:
            return self._empty_category(1)

    # ============================================================
    # 详情页（懒加载，只解析元信息 + 集数表，不进播放页）
    # ============================================================
    def detailContent(self, ids):
        if isinstance(ids, str):
            ids = [ids]
        vid = str(ids[0]).split(',')[0].strip()
        if not vid:
            return {"list": []}

        cached = self._cache_get(self._detail_cache, vid, None)
        if cached is not None:
            return cached

        result = self._fetch_detail(vid)
        ttl = TTL_DETAIL_OK if result.get("list") else TTL_DETAIL_EMPTY
        self._cache_set(self._detail_cache, vid, result, ttl)
        # 后台预取第1集 m3u8（不影响详情返回，秒开）
        if result.get("list"):
            self._prefetch_play(result["list"][0])
        return result

    def _fetch_detail(self, vid):
        html = self._get_text(f"{HOST}/vod-detail/{vid}.html")
        if not html:
            return {"list": []}
        soup = self._soup(html)
        if soup is None:
            return {"list": []}

        # --- 标题 ---
        name = ''
        h1 = soup.find('h1')
        if h1:
            name = self._clean_name(h1.get_text(strip=True))

        # --- 图片 ---
        pic = ''
        for sel in ('.module-info-cover img', '.module-item-pic img', '.video-cover img'):
            img = soup.select_one(sel)
            if img:
                pic = self._pick_pic(img)
                if pic:
                    break

        # --- 基本信息：导演/主演/更新/集数等 ---
        meta = {}
        for item in soup.select('.module-info-item'):
            label_el = item.select_one('.module-info-item-title')
            content_el = item.select_one('.module-info-item-content')
            if label_el is None or content_el is None:
                continue
            label = (label_el.get_text(strip=True) or '').rstrip('：:')
            val = content_el.get_text(' ', strip=True)
            if not val:
                continue
            key = {'导演': 'director', '主演': 'actor', '类型': 'type'}.get(label)
            if key:
                meta.setdefault(key, val)

        def _clean_list(raw):
            # "陈伟霆 /// 陈瑶 /// 曾舜晞" -> "陈伟霆,陈瑶,曾舜晞"
            if not raw:
                return ''
            parts = [p.strip().rstrip('/').strip()
                     for p in re.split(r'[/,，]', raw) if p.strip()]
            return ','.join(dict.fromkeys(parts))[:200]

        director = _clean_list(meta.get('director', ''))
        actor = _clean_list(meta.get('actor', ''))
        type_name = meta.get('type', '')

        # --- 地区 / 年份 / 类型：从详情页面包屑标签链接提取 ---
        area = ''
        year = ''
        for m in re.finditer(r'/show/\d+-([^<>"\']+)----------\.html', html):
            area = unquote(m.group(1)).strip()
            if area:
                break
        for m in re.finditer(r'/show/\d*-+(\d{4})\.html', html):
            year = m.group(1)
            if year:
                break
        # 类型：取形如 /show/\d+---{值}--------(二级分类) 的面包屑
        type_names = [
            unquote(x).strip()
            for x in re.findall(r'/show/\d+---([^<>"]{2,30})--------\.html', html)
            if unquote(x).strip()
        ]
        if type_names:
            type_name = type_names[-1]

        # --- 剧情简介：meta description 兜底 ---
        content = ''
        md = soup.find('meta', attrs={'name': 'description'})
        if md:
            c = str(md.get('content') or '').strip()
            c = re.sub(r'^《[^》]*》[：:]?', '', c)
            if len(c) >= 8:
                content = c[:300]
        if not content:
            intro = soup.select_one('.module-info-introduction-content') or soup.select_one('.show-intro')
            if intro:
                content = intro.get_text(' ', strip=True)[:300]

        remarks = self._pick_remarks(soup)

        # --- 播放源与集数（懒加载：只建表，且排除"立即播放"跳按钮） ---
        play_groups = []
        tabs = soup.select('.module-tab-item, .module-tab-title')
        def _play_links(root):
            """抓取集数链接：优先 module-play-list-link，排除 main-btn 播放按钮"""
            links = root.select('a.module-play-list-link[href*="/free-play/"]')
            if not links:
                links = root.select('a[href*="/free-play/"]')
            out = []
            for a in links:
                t = str(a.get('title') or '')
                if t and '立即播放' in t:
                    continue
                ep_url = self._abs(str(a.get('href') or ''))
                if not ep_url:
                    continue
                nm = a.get_text(strip=True)
                if not nm and t:
                    m = re.search(r'第(.+?)集', t)
                    nm = f"第{m.group(1)}集" if m else '播放'
                out.append((nm or '播放', ep_url))
            return out
        if tabs:
            for tab in tabs:
                src_name = tab.get_text(strip=True) or '线路'
                pane_id = ''
                h = str(tab.get('href') or '')
                if '#' in h:
                    pane_id = h.split('#')[-1]
                pane = soup.find(id=pane_id) if pane_id else None
                eps = _play_links(pane) if pane is not None else []
                if eps:
                    play_groups.append((src_name, eps))
        if not play_groups:
            eps = _play_links(soup)
            if eps:
                play_groups.append(('线路一', eps))

        play_from, play_url = '', ''
        for src_name, eps in play_groups:
            ep_parts = [f"{n}${u}" for n, u in eps]
            play_from = (play_from + '$$$' + src_name) if play_from else src_name
            play_url = (play_url + '$$$' + '#'.join(ep_parts)) if play_url else '#'.join(ep_parts)

        detail = {
            "vod_id": vid,
            "vod_name": name or f"视频{vid}",
            "vod_pic": pic or HOST,
            "type_name": type_name,
            "vod_remarks": remarks or '',
            "vod_year": year,
            "vod_area": area,
            "vod_director": director,
            "vod_actor": actor,
            "vod_content": content,
            "vod_play_from": play_from or '默认',
            "vod_play_url": play_url or '',
        }
        return {"list": [detail]}

    # ============================================================
    # 播放解析（free-play 页 player_ JSON -> m3u8 直链）
    # ============================================================
    def _resolve_play(self, play_url):
        cached = self._cache_get(self._play_cache, play_url, TTL_PLAY)
        if cached:
            return cached
        real = ''
        try:
            text = self._get_text(play_url, referer=HOST + '/', timeout=TIMEOUT_PLAY)
            if text:
                # player_aaaa={"flag":...,"url":"<urlencoded m3u8>",...}
                m = re.search(r'player_\w+\s*=\s*(\{.*?\})\s*[;<]', text, re.S)
                if m:
                    try:
                        data = json.loads(m.group(1))
                        u = (data.get('url') or '').strip()
                        if u:
                            real = self._abs(unquote(u))
                    except Exception:
                        pass
                if not real:
                    # 兜底：明文 url 字段
                    m2 = re.search(r'"url"\s*:\s*"([^"]+\.m3u8[^"]*)"', text)
                    if m2:
                        real = self._abs(unquote(m2.group(1)))
                if not real:
                    m3 = re.search(r'"url"\s*:\s*"(https?://[^"]+)"', text)
                    if m3:
                        u = unquote(m3.group(1)).strip()
                        if '.m3u8' in u or '.mp4' in u:
                            real = self._abs(u)
        except Exception:
            real = ''
        if real:
            self._cache_set(self._play_cache, play_url, real, TTL_PLAY)
        return real

    def _first_play_url(self, vod):
        for seg in (vod.get("vod_play_url") or "").split("$$$"):
            for item in seg.split("#"):
                parts = item.split("$", 1)
                if len(parts) == 2 and parts[1]:
                    return parts[1]
        return None

    def _prefetch_play(self, vod):
        target = self._first_play_url(vod)
        if not target:
            return
        with self._lock:
            if self._cache_get(self._play_cache, target, TTL_PLAY) or target in self._prefetching:
                return
            self._prefetching.add(target)

        def _job():
            try:
                self._resolve_play(target)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._prefetching.discard(target)

        threading.Thread(target=_job, daemon=True).start()

    def _next_play_url(self, vod, cur_url):
        """找当前集的下一个 m3u8 页，用于播放时后台预取下一集"""
        for seg in (vod.get("vod_play_url") or "").split("$$$"):
            items = seg.split("#")
            for i, item in enumerate(items):
                parts = item.split("$", 1)
                if len(parts) == 2 and self._abs(parts[1]) == self._abs(cur_url):
                    if i + 1 < len(items):
                        np = items[i + 1].split("$", 1)
                        if len(np) == 2 and np[1]:
                            return self._abs(np[1])
                    return None
        return None

    def _play_payload(self, playurl):
        is_m3u8 = '.m3u8' in playurl.lower()
        return {
            "parse": 0,
            "playUrl": "",
            "url": playurl,
            "header": {"User-Agent": UA, "Referer": HOST + "/"},
            "format": "application/x-mpegURL" if is_m3u8 else "",
            "contentType": "application/x-mpegURL" if is_m3u8 else "",
        }

    def playerContent(self, flag, id, vipFlags):
        if not id:
            return {"parse": 0, "playUrl": "", "url": ""}
        play_url = self._abs(str(id))
        vod_id = self._vid_of(play_url)

        cached = self._cache_get(self._play_cache, play_url, TTL_PLAY)
        if cached:
            self._prefetch_next(vod_id, play_url)
            return self._play_payload(cached)

        m3u8 = self._resolve_play(play_url)
        if m3u8:
            self._prefetch_next(vod_id, play_url)
            return self._play_payload(m3u8)

        return {
            "parse": 1,
            "playUrl": "",
            "url": play_url,
            "header": {"User-Agent": UA, "Referer": HOST + "/"},
        }

    @staticmethod
    def _vid_of(play_url):
        m = re.search(r'/free-play/(\d+)-', play_url)
        return m.group(1) if m else None

    def _prefetch_next(self, vod_id, cur_url):
        if not vod_id:
            return
        cached = self._cache_get(self._detail_cache, vod_id, TTL_DETAIL_OK)
        if not cached or not cached.get("list"):
            return
        vod = cached["list"][0]
        nxt = self._next_play_url(vod, cur_url)
        if not nxt:
            return
        with self._lock:
            if self._cache_get(self._play_cache, nxt, TTL_PLAY) or nxt in self._prefetching:
                return
            self._prefetching.add(nxt)

        def _job():
            try:
                self._resolve_play(nxt)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._prefetching.discard(nxt)

        threading.Thread(target=_job, daemon=True).start()

    # ============================================================
    # 工具：状态 / 年份提取
    # ============================================================
    @staticmethod
    def _pick_remarks(soup):
        txt = soup.get_text(' ', strip=True) if soup else ''
        patterns = [
            r'(连载至\s*[\d]+集)', r'(更新至第?\s*[\d]+集)',
            r'(更新至\s*[\d]+集)', r'(全[\d]+集)',
            r'(已完结|完结|全集|正片|HD中字|HD国语|TC中字)',
        ]
        for p in patterns:
            m = re.search(p, txt)
            if m:
                return m.group(1)
        return ''

    # ============================================================
    # 搜索（站内搜索已关闭 -> 分类爬取兜底 + 分词评分）
    # ============================================================
    @staticmethod
    def _filter_by_keyword(cards, raw, limit=24):
        """分词模糊匹配 + 评分排序"""
        if not raw:
            return cards[:limit]
        raw = raw.lower().replace(' ', '').strip()
        if not raw:
            return cards[:limit]
        if len(raw) <= 2:
            tokens = [raw]
        else:
            tokens = [raw[i:i + 2] for i in range(0, len(raw) - 1)]
        tokens += list(raw)

        def score(name):
            name = (name or '').lower().replace(' ', '')
            if not name:
                return 0
            if raw in name:
                return 100
            return sum(1 for t in tokens if t in name)

        matched = [(score(c.get('vod_name') or ''), c) for c in cards]
        matched = [c for s, c in matched if s > 0]
        matched.sort(key=lambda c: score(c.get('vod_name') or ''), reverse=True)
        return matched[:limit]

    def _scrape_search(self, raw, page):
        """分类爬取兜底：并行抓取首页+各分类，分词过滤"""
        if page > 1:
            return None
        pages = (1, 2, 3) if len(raw) >= 3 else (1, 2)

        def _fetch(url, timeout=TIMEOUT_API):
            html = self._get_text(url, timeout=timeout)
            return self._parse_cards(html, limit=36) if html else []

        all_cards = []
        # 首页 + 各一级分类前几页
        urls = [HOST]
        for c in CATS:
            for p in pages:
                urls.append(_show_url(c['id'], page=p))
        # 并行抓（限 worker，避免打太猛）
        if ThreadPoolExecutor is not None and len(urls) > 1:
            with ThreadPoolExecutor(max_workers=5) as ex:
                futs = [ex.submit(_fetch, u) for u in urls]
                for f in as_completed(futs, timeout=TIMEOUT_API * 5):
                    try:
                        all_cards.extend(f.result(timeout=TIMEOUT_API))
                    except Exception:
                        continue
        else:
            for u in urls:
                try:
                    all_cards.extend(_fetch(u))
                except Exception:
                    continue

        matched = self._filter_by_keyword(all_cards, raw, limit=24)
        return {"list": matched} if matched else None

    def searchContent(self, keyword, quick=False, pg=1):
        kw = quote((keyword or '').strip())
        raw = (keyword or '').strip().lower()
        if not kw:
            return {"list": [], "msg": "请输入搜索关键词"}
        page = int(pg or 1)
        ckey = "%s|%s" % (raw, page)
        cached = self._cache_get(self._search_cache, ckey, TTL_SEARCH)
        if cached is not None:
            return cached

        result = self._scrape_search(raw, page)

        if result and result.get("list"):
            self._cache_set(self._search_cache, ckey, result, TTL_SEARCH)
            return result

        result = {"list": [], "msg": "站内搜索已关闭，未能在热门分类中找到相关内容，请尝试其他关键词"}
        self._cache_set(self._search_cache, ckey, result, TTL_SEARCH)
        return result

    # ============================================================
    # 本地代理 & 清理
    # ============================================================
    def localProxy(self, param):
        return [200, "video/MP2T", b"", ""]

    def destroy(self):
        try:
            self.session.close()
        except Exception:
            pass

    def close(self):
        self.destroy()


# ============================================================
# 本地测试
# ============================================================
if __name__ == '__main__':
    s = Spider()
    action = sys.argv[1] if len(sys.argv) > 1 else 'home'
    if action == 'home':
        print(json.dumps(s.homeContent(), ensure_ascii=False)[:800])
    elif action == 'category':
        tid = sys.argv[2] if len(sys.argv) > 2 else 'dianying'
        pg = sys.argv[3] if len(sys.argv) > 3 else '1'
        cl = sys.argv[4] if len(sys.argv) > 4 else ''
        ar = sys.argv[5] if len(sys.argv) > 5 else ''
        print(json.dumps(
            s.categoryContent(tid, pg, False, {'class': cl, 'area': ar}),
            ensure_ascii=False
        )[:800])
    elif action == 'detail':
        vid = sys.argv[2] if len(sys.argv) > 2 else '98017'
        r = s.detailContent(vid)
        d = r['list'][0] if r.get('list') else {}
        print(json.dumps(d, ensure_ascii=False)[:800])
    elif action == 'play':
        pid = sys.argv[2] if len(sys.argv) > 2 else 'https://www.tdys.cc/free-play/98017-1-1.html'
        print(json.dumps(s.playerContent('', pid, []), ensure_ascii=False)[:500])
    elif action == 'search':
        kw = sys.argv[2] if len(sys.argv) > 2 else '九门'
        print(json.dumps(s.searchContent(kw), ensure_ascii=False)[:500])