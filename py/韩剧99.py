# -*- coding: utf-8 -*-
"""
韩剧99 (hanju99.cc) - TVBox 爬虫源 (maccms)
================================================
接口：homeContent / homeVideoContent / categoryContent(含二级分类+筛选) /
      detailContent(懒加载) / playerContent(直链+预取) / searchContent(多策略)

站点特性（已实测确认）：
1. 移动端 / PC UA 均可访问（全站可用）
2. 播放链路：详情 /voddetail/{slug}.html -> 播放页 /vodplay/{slug}-{sid}-{nid}.html
   -> 播放页内 player_xxxx JSON 的 url 字段为百分号编码的 m3u8 直链（含 url_next 可预取下一集）
3. 分类页：/vodshow/{tid}-----------.html 结构，12 段模板：
   {tid}-{地区}-{排序}-----{页码}---{年份}.html
4. 二级分类：站点未开放 class 字段，改用 maccms 类型标签页 /vodsearch/----{类型}---------.html
   实现（已实测：剧情/爱情/真人秀/脱口秀等标签均可用）
5. 搜索：站点后台关闭站内搜索，使用 suggest 接口(JSON,精准) + GET 搜索页(兜底补充)

核心优化（加载速度 / 播放速度）：
- 详情页懒加载：不预解析所有集数 m3u8，秒开
- 播放页解析后后台线程立即预取下一集(url_next)，连播零等待
- 多级缓存：首页10分钟 / 分类5分钟 / 类型5分钟 / 详情5分钟(失败30秒) / 搜索3分钟 / 播放30分钟
- 全链路短超时(8s/5s/4s) + 快速重试(0.3s) + 429限流等待(2s)
- 连接池复用(HTTPAdapter) + gzip 自动解压
- 纯正则解析（无 BeautifulSoup 依赖，解析更快更省内存）

域名跟踪（适配所有壳子）：
- 内置候选域名列表，启动自动探测可用域名（页面特征校验）
- 请求连续失败自动触发域名再探测（故障转移）
- 支持 extend 参数覆盖域名（如 extend="https://新域名"）
- 相对链接统一基于当前可用域名拼接，换域名后全站自动跟随
"""

import re
import json
import time
import threading
from urllib.parse import quote, urlencode, unquote

import requests
from requests.adapters import HTTPAdapter

try:
    from concurrent.futures import ThreadPoolExecutor, as_completed
except ImportError:
    ThreadPoolExecutor = None
    as_completed = None

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
# 候选域名（按优先级，故障时自动切换）
HOSTS = [
    "https://hanju99.cc",
    "https://www.hanju99.cc",
]
DEFAULT_HOST = HOSTS[0]

# 站点特征校验（探测域名时检查页面是否包含这些特征）
SITE_MARKERS = ('voddetail', '韩剧99', '韩剧')

# 移动端 UA（保底；本站 PC UA 亦可）
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

# 超时（秒）
TIMEOUT_PAGE = 8
TIMEOUT_API = 5
TIMEOUT_PLAY = 8        # 播放页较大且站点响应偏慢，需充足超时

# 缓存 TTL（秒）
TTL_HOME = 600
TTL_CAT = 300
TTL_DETAIL_OK = 300
TTL_DETAIL_EMPTY = 30
TTL_SEARCH = 180
TTL_PLAY = 1800        # m3u8 直链缓存 30 分钟
TTL_HOST = 600         # 域名探测结果缓存 10 分钟

# 每页条数（站点固定 30/页）
PAGE_SIZE = 30

# 年份范围
YEARS = [str(y) for y in range(2026, 2004, -1)]

# 地区（站点仅韩国）
AREAS = ["韩国"]

# 一级分类：id(站点 vodtype id) / name / 二级分类标签
CATS = [
    {"id": "21", "name": "韩剧", "subs": [
        "剧情", "爱情", "喜剧", "悬疑", "动作", "古装",
        "犯罪", "惊悚", "奇幻", "科幻", "家庭", "历史",
        "音乐", "战争", "灾难",
    ]},
    {"id": "27", "name": "韩综艺", "subs": [
        "真人秀", "脱口秀", "纪录片", "美食", "旅行", "亲子",
        "音乐", "选秀", "竞技", "游戏", "访谈", "观察",
    ]},
]


def _build_filters(cat):
    """构建筛选器：二级分类(类型标签) / 地区 / 年份 / 排序"""
    filters = [{
        "key": "class", "name": "类型",
        "value": [{"n": "全部", "v": ""}] + [{"n": t, "v": t} for t in cat["subs"]],
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
        ],
    })
    return filters


ALL_CLASSES = [{"type_id": c["id"], "type_name": c["name"], "filter": 1} for c in CATS]
ALL_FILTERS = {c["id"]: _build_filters(c) for c in CATS}

# 全部二级分类标签（供快速检索）
ALL_TAGS = sorted({t for c in CATS for t in c["subs"]})


# ============================================================
# URL 构造（模板已按站内真实链接实测验证）
# ============================================================
def _cat_url(tid, area='', by='', year='', pg=1):
    """分类页 URL：12 段模板 {tid}-{area}-{by}-----{pg}---{year}.html"""
    segs = [
        str(tid), area, by,
        '', '', '', '', '',
        (str(pg) if pg and int(pg) > 1 else ''),
        '', '', year,
    ]
    return "/vodshow/" + "-".join(segs) + ".html"


def _type_url(tag, pg=1, year=''):
    """二级分类(类型标签)页 URL：/vodsearch/----{类型}-----{pg}---{year}.html"""
    q = quote(tag)
    try:
        pg = int(pg or 1)
    except Exception:
        pg = 1
    if pg > 1 and year:
        return "/vodsearch/----%s------%d---%s.html" % (q, pg, year)
    if year:
        return "/vodsearch/----%s---------%s.html" % (q, year)
    if pg > 1:
        return "/vodsearch/----%s------%d---.html" % (q, pg)
    return "/vodsearch/----%s---------.html" % (q,)


# ============================================================
# Spider 主类
# ============================================================
_Base = _BaseSpider if _BaseSpider is not None else object


class Spider(_Base):
    siteUrl = DEFAULT_HOST
    filterable = True
    headers = {
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Referer': DEFAULT_HOST + '/',
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

        self._lock = threading.Lock()
        self._host = None            # 当前可用域名（首次访问前探测）
        self._host_time = 0          # 域名探测时间
        self._host_fail = 0          # 当前域名连续失败次数
        self._extend_host = ''       # extend 传入的域名覆盖

        # 缓存容器
        self._home_cache = []
        self._home_cache_time = 0
        self._cat_cache = {}
        self._detail_cache = {}
        self._search_cache = {}
        self._play_cache = {}
        self._prefetching = set()

    def init(self, extend=""):
        self.extend = extend or ""
        host = self._extract_host(self.extend)
        if host:
            self._extend_host = host
            with self._lock:
                self._host = host
                self._host_time = int(time.time())

    @staticmethod
    def _extract_host(extend):
        """从 extend 参数提取域名覆盖（如 https://新域名 或 新域名）"""
        if not extend:
            return ''
        s = str(extend).strip()
        m = re.search(r'https?://[^\s,;|]+', s)
        if m:
            return m.group(0).rstrip('/')
        if re.match(r'^[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', s):
            return 'https://' + s.rstrip('/')
        return ''

    # ===== 域名跟踪 =====
    def _probe_host(self, force=False):
        """探测可用域名：按优先级尝试候选域名 + extend 覆盖，校验站点特征"""
        now = int(time.time())
        with self._lock:
            if not force and self._host and now - self._host_time < TTL_HOST and self._host_fail < 2:
                return self._host

        candidates = []
        if self._extend_host:
            candidates.append(self._extend_host)
        candidates.extend(HOSTS)

        seen = set()
        for host in candidates:
            if host in seen:
                continue
            seen.add(host)
            try:
                r = self.session.get(host, timeout=TIMEOUT_API)
                if r.status_code == 200 and any(mk in r.text for mk in SITE_MARKERS):
                    with self._lock:
                        self._host = host
                        self._host_time = int(time.time())
                        self._host_fail = 0
                    return host
            except Exception:
                continue

        # 全部失败：保留原域名，重置失败计数（避免无限重探）
        with self._lock:
            self._host_fail = 0
            if not self._host:
                self._host = DEFAULT_HOST
        return self._host

    def _current_host(self):
        # 先快照锁内状态，再在锁外决定是否探测（避免非重入锁死锁）
        with self._lock:
            host = self._host
            fail = self._host_fail
            t = self._host_time
        if host is None:
            return self._probe_host()
        if fail >= 2 and int(time.time()) - t >= 60:
            # 连续失败且距上次探测已超 60s：强制重新探测（域名跟踪）
            return self._probe_host(force=True)
        return host

    def _abs(self, u):
        """相对链接 -> 基于当前可用域名的绝对链接"""
        u = (u or '').strip()
        if not u:
            return ''
        if u.startswith('//'):
            return 'https:' + u
        if u.startswith('http'):
            return u
        return self._current_host() + '/' + u.lstrip('/')

    # ===== 网络工具（带域名故障转移） =====
    def _request(self, method, url, data=None, timeout=TIMEOUT_PAGE, referer='', failover=True):
        """带域名故障转移的请求。
        failover=True：连接失败时自动切换备用域名（域名跟踪）；
        超时(Timeout)不触发换域名（可能只是单次网络抖动），快速重试一次后放弃。
        """
        headers = {'Connection': 'keep-alive'}
        if referer:
            headers['Referer'] = referer

        # 若传入相对路径，先拼当前域名
        if url.startswith('/'):
            url = self._current_host() + url

        for attempt in range(2):
            try:
                if method == 'POST':
                    r = self.session.post(url, data=data, timeout=timeout, headers=headers)
                else:
                    r = self.session.get(url, timeout=timeout, headers=headers)
                if r.status_code == 429:
                    time.sleep(2.0)
                    continue
                if r.status_code == 404 and failover:
                    # 404 可能是域名失效（站点整体换域名）：换备用域名重试
                    with self._lock:
                        self._host_fail += 1
                    r2 = self._try_failover(url, headers, timeout, method, data)
                    if r2 is not None:
                        return r2
                    break
                r.raise_for_status()
                r.encoding = r.apparent_encoding or 'utf-8'
                with self._lock:
                    self._host_fail = 0
                return r
            except requests.exceptions.Timeout:
                # 超时不触发域名切换：只重试一次，避免无谓等待
                if attempt == 0:
                    continue
                return None
            except requests.exceptions.ConnectionError:
                # 连接失败：域名可能失效，尝试故障转移
                with self._lock:
                    self._host_fail += 1
                if failover:
                    r = self._try_failover(url, headers, timeout, method, data)
                    if r is not None:
                        return r
                if attempt == 0:
                    time.sleep(0.3)
                    continue
                return None
            except Exception:
                with self._lock:
                    self._host_fail += 1
                if failover:
                    r = self._try_failover(url, headers, timeout, method, data)
                    if r is not None:
                        return r
                if attempt == 0:
                    time.sleep(0.2)
                    continue
                return None
        return None

    def _try_failover(self, url, headers, timeout, method, data):
        """当前域名不可用时，换备用域名重试"""
        cur = self._current_host()
        for host in ([self._extend_host] if self._extend_host else []) + HOSTS:
            if host == cur:
                continue
            alt_url = host + url[len(cur):] if url.startswith(cur) else url
            try:
                if method == 'POST':
                    r = self.session.post(alt_url, data=data, timeout=timeout, headers=headers)
                else:
                    r = self.session.get(alt_url, timeout=timeout, headers=headers)
                if r.status_code != 200:
                    continue
                r.raise_for_status()
                r.encoding = r.apparent_encoding or 'utf-8'
                with self._lock:
                    self._host = host
                    self._host_time = int(time.time())
                    self._host_fail = 0
                return r
            except Exception:
                continue
        return None

    def _get(self, url, referer='', timeout=TIMEOUT_PAGE, failover=True):
        r = self._request('GET', url, timeout=timeout, referer=referer, failover=failover)
        return r

    def _get_text(self, url, referer='', timeout=TIMEOUT_PAGE, failover=True):
        r = self._get(url, referer, timeout, failover=failover)
        return r.text if r is not None else ""

    def _post_text(self, url, data=None, referer='', timeout=TIMEOUT_API, failover=True):
        r = self._request('POST', url, data=data, timeout=timeout, referer=referer, failover=failover)
        return r.text if r is not None else ""

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

    # ===== 通用解析 =====
    @staticmethod
    def _extract_slug(href):
        m = re.search(r'/vod(detail|play|type)/([a-zA-Z0-9_\-]+)', (href or '').strip())
        if m:
            return m.group(2)
        m = re.search(r'/(\d{3,10})\.(?:html|shtml)$', (href or '').strip())
        return m.group(1) if m else None

    @staticmethod
    def _clean_name(raw):
        if not raw:
            return raw
        return re.sub(r'\s*[（(]\s*\d{4}\s*[）)]\s*$', '', (raw or '').strip()).strip()

    _CARD_RE = re.compile(
        r'<a[^>]+class="[^"]*video-pic[^"]*"[^>]*>(.*?)</a>', re.S)
    _CARD_ATTR_RE = re.compile(
        r'(data-original|data-src|src|href|title|style)="([^"]*)"')

    def _parse_cards(self, html, limit=36):
        """解析视频卡片（分类页/首页/搜索页通用）"""
        if not html:
            return []
        items = {}
        for block in self._CARD_RE.finditer(html):
            seg = block.group(0)
            attrs = {k: v for k, v in self._CARD_ATTR_RE.findall(seg)}
            href = attrs.get('href', '') or ''
            slug = self._extract_slug(href)
            if not slug or 'voddetail' not in href:
                continue
            title = (attrs.get('title', '') or '').strip()
            if not title:
                m = re.search(r'<strong>([^<]+)</strong>', seg)
                if m:
                    title = m.group(1).strip()
            if not title:
                continue

            # 图片：data-original 优先，其次 style 背景图
            pic = (attrs.get('data-original', '') or attrs.get('data-src', '') or '')
            if not pic:
                style = attrs.get('style', '') or ''
                m = re.search(r'background(?:-image)?:\s*url\(([^)]+)\)', style)
                if m:
                    pic = m.group(1).strip().strip('"').strip("'")
            pic = self._abs(pic)

            # 备注：卡片内 note 标签，兜底从标题提取
            remarks = ''
            m = re.search(r'<span[^>]*class="[^"]*note[^"]*"[^>]*>([^<]+)</span>', seg)
            if m:
                remarks = m.group(1).strip()
            if not remarks:
                m = re.search(
                    r'(更新至[^\s]{0,12}|更新到[^\s]{0,12}|全\d+集|全集|已完结|正片'
                    r'|TC中字|HD中字|HD国语|抢先版|完结)', title
                )
                if m:
                    remarks = m.group(1)

            if slug not in items:
                items[slug] = {
                    'vod_id': slug,
                    'vod_name': self._clean_name(title),
                    'vod_pic': pic,
                    'vod_remarks': remarks,
                }
        return list(items.values())[:limit]

    @staticmethod
    def _parse_total_pages(html, pattern):
        """从分页区域解析总页数"""
        m = re.search(r'尾页</a>', html)
        if m:
            seg = html[max(0, m.start() - 400):m.start()]
            nums = [int(x) for x in re.findall(pattern, seg)]
            if nums:
                return max(nums)
        nums = [int(x) for x in re.findall(pattern, html)]
        return max(nums) if nums else 1

    # ============================================================
    # 首页
    # ============================================================
    def _fetch_home(self):
        """并行抓取各分类首页合成推荐列表（分类页响应快，且内容按更新排序）"""
        results = []
        if ThreadPoolExecutor is not None:
            try:
                with ThreadPoolExecutor(max_workers=2) as ex:
                    futs = [
                        ex.submit(self._get_text, _cat_url(c['id'], pg=1), '', 5)
                        for c in CATS
                    ]
                    for f in as_completed(futs, timeout=9):
                        try:
                            html = f.result(timeout=6)
                            results.extend(self._parse_cards(html, limit=30))
                        except Exception:
                            pass
            except Exception:
                pass
        if not results:
            for c in CATS:
                try:
                    html = self._get_text(_cat_url(c['id'], pg=1), timeout=5)
                    results.extend(self._parse_cards(html, limit=30))
                except Exception:
                    pass
        return results[:60]

    def homeContent(self, filter=False):
        vod_list = []
        now = int(time.time())
        with self._lock:
            if self._home_cache and now - self._home_cache_time < TTL_HOME:
                vod_list = self._home_cache[:60]
        if not vod_list:
            vod_list = self._fetch_home()
            if vod_list:
                with self._lock:
                    self._home_cache = vod_list
                    self._home_cache_time = int(time.time())
        return {
            "class": ALL_CLASSES,
            "filters": ALL_FILTERS,
            "list": vod_list,
        }

    def homeVideoContent(self):
        now = int(time.time())
        with self._lock:
            if self._home_cache and now - self._home_cache_time < TTL_HOME:
                return {"list": self._home_cache[:60]}
        vod_list = self._fetch_home()
        if vod_list:
            with self._lock:
                self._home_cache = vod_list
                self._home_cache_time = int(time.time())
        return {"list": vod_list[:60]}

    # ============================================================
    # 分类列表（含二级分类）
    # ============================================================
    def _empty_category(self, page=1):
        return {"list": [], "page": page, "pagecount": 1, "limit": PAGE_SIZE, "total": 0}

    def categoryContent(self, tid, pg, filter, extend):
        page = 1
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

            tag = (ext.get('class') or '').strip()
            area = (ext.get('area') or '').strip()
            year = (ext.get('year') or '').strip()
            by = (ext.get('by') or '').strip()

            # 二级分类（类型标签）优先走 vodsearch 类型页
            if tag:
                ckey = "T|%s|%d|%s" % (tag, page, year)
                cached = self._cache_get(self._cat_cache, ckey, TTL_CAT)
                if cached is not None:
                    return cached
                url = _type_url(tag, page, year)
                html = self._get_text(url)
                if not html:
                    return self._empty_category(page)
                pagecount = self._parse_total_pages(
                    html, r'vodsearch/----%s------(\d+)---\.html' % re.escape(quote(tag)))
                vod_list = self._parse_cards(html, limit=PAGE_SIZE)
                result = {
                    "list": vod_list,
                    "page": page,
                    "pagecount": pagecount,
                    "limit": PAGE_SIZE,
                    "total": pagecount * PAGE_SIZE,
                }
                self._cache_set(self._cat_cache, ckey, result, TTL_CAT)
                return result

            # 一级分类走 vodshow 分类页
            ckey = "C|%s|%d|%s|%s|%s" % (tid, page, area, year, by)
            cached = self._cache_get(self._cat_cache, ckey, TTL_CAT)
            if cached is not None:
                return cached

            url = _cat_url(tid, area, by, year, page)
            html = self._get_text(url)
            if not html:
                return self._empty_category(page)

            pagecount = self._parse_total_pages(
                html, r'vodshow/%s--------(\d+)---\.html' % re.escape(str(tid)))
            vod_list = self._parse_cards(html, limit=PAGE_SIZE)

            result = {
                "list": vod_list,
                "page": page,
                "pagecount": pagecount,
                "limit": PAGE_SIZE,
                "total": pagecount * PAGE_SIZE,
            }
            self._cache_set(self._cat_cache, ckey, result, TTL_CAT)
            return result
        except Exception:
            return self._empty_category(page)

    # ============================================================
    # 详情页（懒加载）
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

        if result.get("list"):
            self._prefetch_play(result["list"][0])
        return result

    def _fetch_detail(self, vid):
        html = self._get_text("/voddetail/%s.html" % vid)
        if not html:
            return {"list": []}

        # --- 基本信息 ---
        name = ''
        m = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
        if m:
            name = self._clean_name(m.group(1).strip())

        pic = ''
        m = re.search(
            r'<img[^>]+class="[^"]*img-responsive[^"]*"[^>]+(?:data-original|data-src|src)="([^"]+)"',
            html)
        if m:
            pic = self._abs(m.group(1))
        if not pic:
            m = re.search(r'class="[^"]*video-pic[^"]*"[^>]+style="[^"]*url\(([^)]+)\)', html)
            if m:
                pic = self._abs(m.group(1).strip().strip('"').strip("'"))

        # 分类名（第一个 vodtype 链接）
        type_name = ''
        m = re.search(r'<a[^>]+href="/vodtype/(\d+)\.html"[^>]*>([^<]+)</a>', html)
        if m:
            type_name = m.group(2).strip()

        # 信息字段
        fields = {}
        key_map = {
            '主演': 'actor', '导演': 'director', '国家/地区': 'area',
            '地区': 'area', '语言/字幕': 'lang', '年代': 'year',
            '类型': 'type', '状态': 'state',
        }
        # 匹配 <span>主演：</span>...</li> 形式的 li 块
        for m in re.finditer(
                r'<span[^>]*>\s*([^<]{2,8}?)[:：]?\s*</span>(.*?)</li>', html, re.S):
            label = m.group(1).strip()
            body = m.group(2)
            if label not in key_map:
                continue
            # 去掉 a 标签保留文本，并清理 HTML 实体与空白
            text = re.sub(r'<[^>]+>', ' ', body)
            text = text.replace('&nbsp;', ' ').replace('&amp;', '&')
            text = re.sub(r'\s+', ' ', text).strip().strip('：:')
            if not text:
                continue
            key = key_map[label]
            if key == 'year':
                # 年代字段只保留 4 位年份
                ym = re.search(r'\b(19\d{2}|20\d{2})\b', text)
                if ym:
                    fields.setdefault(key, ym.group(1))
            else:
                fields.setdefault(key, text)

        actor = fields.get('actor', '')
        director = fields.get('director', '')
        area = fields.get('area', '')
        lang = fields.get('lang', '')
        year = fields.get('year', '')
        remarks = fields.get('state', '')
        if not remarks:
            m = re.search(r'(全集|更新至第?\d+集|已完结|连载中)', html)
            if m:
                remarks = m.group(1)

        # 简介
        content = ''
        m = re.search(r'<span[^>]*>剧情简介[:：]?</span>(.*?)</(?:p|div|li)>', html, re.S)
        if m:
            content = re.sub(r'<[^>]+>', '', m.group(1))
            content = content.replace('&nbsp;', ' ')
            content = re.sub(r'\s+', ' ', content).strip()
            # 去掉常见的“...详细”尾巴
            content = re.sub(r'\.{2,}详细$', '', content).strip()
        if not content:
            m = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', html)
            if m:
                content = m.group(1).strip()[:300]

        # --- 播放源与集数 ---
        play_groups = []
        sources = re.findall(
            r'<a[^>]+href="#(con_playlist_\d+)"[^>]*>\s*([^<]+?)\s*</a>', html)
        if sources:
            for pid, src_name in sources:
                pm = re.search(r'<ul[^>]*id="%s"[^>]*>(.*?)</ul>' % pid, html, re.S)
                if not pm:
                    continue
                eps = re.findall(
                    r'<a[^>]+href="(/vodplay/[^"]+)"[^>]*>([^<]+)</a>', pm.group(1))
                eps = [(nm.strip() or '播放', self._abs(u)) for u, nm in eps]
                if eps:
                    play_groups.append((src_name.strip(), eps))

        if not play_groups:
            eps = re.findall(
                r'<a[^>]+href="(/vodplay/[^"]+)"[^>]*>([^<]+)</a>', html)
            eps = [(nm.strip() or '播放', self._abs(u)) for u, nm in eps]
            if eps:
                play_groups.append(('默认线路', eps))

        play_from, play_url = '', ''
        for src_name, eps in play_groups:
            ep_parts = [f"{n}${u}" for n, u in eps]
            play_from = (play_from + '$$$' + src_name) if play_from else src_name
            play_url = (play_url + '$$$' + '#'.join(ep_parts)) if play_url else '#'.join(ep_parts)

        detail = {
            "vod_id": vid,
            "vod_name": name or f"视频{vid}",
            "vod_pic": pic or self._current_host(),
            "type_name": type_name,
            "vod_remarks": remarks or '',
            "vod_year": year,
            "vod_area": area,
            "vod_lang": lang,
            "vod_director": director,
            "vod_actor": actor,
            "vod_content": content,
            "vod_play_from": play_from or '默认',
            "vod_play_url": play_url or '',
        }
        return {"list": [detail]}

    # ============================================================
    # 播放解析（m3u8 直链 + 下一集预取）
    # ============================================================
    def _resolve_play(self, play_url):
        """解析播放页内 player_xxxx JSON 的 m3u8 直链"""
        cached = self._cache_get(self._play_cache, play_url, TTL_PLAY)
        if cached:
            return cached

        real = ''
        next_link = ''
        next_m3u8 = ''
        try:
            # 播放页解析不做域名故障转移（页面 URL 已带完整域名，失败即换线路）
            text = self._get_text(
                play_url, referer=self._current_host() + '/',
                timeout=TIMEOUT_PLAY, failover=False)
            if text:
                m = re.search(r'player_\w+\s*=\s*(\{.*?\})\s*</script>', text, re.S)
                if m:
                    try:
                        data = json.loads(m.group(1))
                        u = unquote((data.get('url') or '').replace('\\/', '/')).strip()
                        if u:
                            real = self._abs(u)
                        # 下一集：link_next 是播放页 URL，url_next 是 m3u8 直链
                        ln = (data.get('link_next') or '').replace('\\/', '/').strip()
                        if ln:
                            next_link = self._abs(ln)
                        nu = unquote((data.get('url_next') or '').replace('\\/', '/')).strip()
                        if nu:
                            next_m3u8 = self._abs(nu)
                    except Exception:
                        pass
                if not real:
                    m2 = re.search(r'"url"\s*:\s*"([^"]+\.m3u8[^"]*)"', text)
                    if m2:
                        real = self._abs(unquote(m2.group(1).replace('\\/', '/')))
        except Exception:
            real = ''

        if real:
            with self._lock:
                self._cache_set(self._play_cache, play_url, real, TTL_PLAY)
            # 下一集 m3u8 直链直接缓存（连播零等待）
            if next_m3u8 and next_link and next_link != play_url:
                with self._lock:
                    self._cache_set(self._play_cache, next_link, next_m3u8, TTL_PLAY)
        return real

    def _prefetch_url(self, target):
        """后台线程预取指定播放页的 m3u8（避免阻塞 + 去重）"""
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

    def _first_play_url(self, vod):
        for seg in (vod.get("vod_play_url") or "").split("$$$"):
            for item in seg.split("#"):
                parts = item.split("$", 1)
                if len(parts) == 2 and parts[1]:
                    return parts[1]
        return None

    def _prefetch_play(self, vod):
        """详情返回后预取第一集 m3u8"""
        target = self._first_play_url(vod)
        if target:
            self._prefetch_url(target)

    def _play_payload(self, playurl):
        is_m3u8 = '.m3u8' in playurl.lower()
        return {
            "parse": 0,
            "playUrl": "",
            "url": playurl,
            "header": {
                "User-Agent": UA,
                "Referer": self._current_host() + "/",
                "Origin": self._current_host(),
            },
            "format": "application/x-mpegURL" if is_m3u8 else "",
            "contentType": "application/x-mpegURL" if is_m3u8 else "",
        }

    def playerContent(self, flag, id, vipFlags):
        if not id:
            return {"parse": 0, "playUrl": "", "url": ""}
        play_url = self._abs(str(id))

        cached = self._cache_get(self._play_cache, play_url, TTL_PLAY)
        if cached:
            return self._play_payload(cached)

        m3u8 = self._resolve_play(play_url)
        if m3u8:
            return self._play_payload(m3u8)

        # 当前线路解析失败：尝试其他线路
        alts = self._alt_play_urls(play_url)
        if alts and ThreadPoolExecutor is not None:
            try:
                with ThreadPoolExecutor(max_workers=3) as ex:
                    futs = [ex.submit(self._resolve_play, u) for u in alts]
                    for f in as_completed(futs, timeout=TIMEOUT_PLAY * 2):
                        u = f.result(timeout=TIMEOUT_PLAY)
                        if u:
                            with self._lock:
                                self._cache_set(self._play_cache, play_url, u, TTL_PLAY)
                            return self._play_payload(u)
            except Exception:
                pass
        else:
            for u in alts:
                m = self._resolve_play(u)
                if m:
                    with self._lock:
                        self._cache_set(self._play_cache, play_url, m, TTL_PLAY)
                    return self._play_payload(m)

        return {
            "parse": 1,
            "playUrl": "",
            "url": play_url,
            "header": {"User-Agent": UA, "Referer": self._current_host() + "/"},
        }

    def _alt_play_urls(self, play_url, limit=6):
        """从详情缓存中取同剧其他线路的播放页 URL"""
        slug = self._extract_slug(play_url) or ''
        if not slug:
            return []
        cached = self._cache_get(self._detail_cache, slug, TTL_DETAIL_OK)
        if not cached or not cached.get("list"):
            return []
        vod = cached["list"][0]
        out, seen = [], {play_url}
        for seg in (vod.get("vod_play_url") or "").split("$$$"):
            for item in seg.split("#"):
                parts = item.split("$", 1)
                if len(parts) == 2 and parts[1]:
                    u = parts[1]
                    if u not in seen:
                        seen.add(u)
                        out.append(u)
                        if len(out) >= limit:
                            return out
        return out

    # ============================================================
    # 搜索（四策略：suggest 接口 -> GET 搜索页 -> POST 搜索页 -> 分类页模糊匹配）
    # ============================================================
    def _search_suggest(self, key):
        """maccms suggest 接口（JSON，精准，快速失败不换域名）"""
        try:
            url = "/index.php/ajax/suggest?mid=1&wd=" + quote(key)
            r = self._request('GET', url, timeout=4, failover=False)
            if r is None:
                return []
            data = json.loads(r.text)
            if not data or data.get('code') != 1:
                return []
            out = []
            for it in data.get('list') or []:
                slug = (it.get('en') or '').strip()
                name = (it.get('name') or '').strip()
                if not slug or not name:
                    continue
                pic = (it.get('pic') or '').strip()
                out.append({
                    'vod_id': slug,
                    'vod_name': name,
                    'vod_pic': self._abs(pic),
                    'vod_remarks': '',
                })
            return out
        except Exception:
            return []

    def _search_page(self, key, method='GET'):
        """GET/POST 搜索页（maccms 标准搜索路由）"""
        try:
            url = "/vodsearch/-------------.html"
            if method == 'POST':
                html = self._post_text(url, data={'wd': key}, timeout=5)
            else:
                html = self._get_text(url + '?' + urlencode({'wd': key}), timeout=5)
            return self._parse_cards(html, limit=36)
        except Exception:
            return []

    def _search_by_category(self, key, limit=3):
        """终极兜底：遍历分类页前几页，标题模糊匹配（慢但稳）"""
        found, seen = [], set()
        kw = (key or '').lower()
        try:
            for c in CATS:
                for pg in range(1, 4):
                    html = self._get_text(_cat_url(c['id'], pg=pg), timeout=5)
                    for it in self._parse_cards(html, limit=36):
                        if it['vod_id'] in seen:
                            continue
                        seen.add(it['vod_id'])
                        if kw and kw in (it['vod_name'] or '').lower():
                            found.append(it)
                            if len(found) >= limit:
                                return found
        except Exception:
            pass
        return found

    def searchContent(self, key, quick, *args, **kwargs):
        """多策略搜索。兼容壳子 2/3/4 参数调用（新版 TVBox 可能传 pg/dict）"""
        key = (key or '').strip()
        if not key:
            return {"list": []}

        cache_key = 'S:' + key
        cached = self._cache_get(self._search_cache, cache_key, TTL_SEARCH)
        if cached is not None:
            return {"list": cached}

        items = {}
        # 1) suggest 接口（精准快速）
        for it in self._search_suggest(key):
            if it['vod_id'] not in items:
                items[it['vod_id']] = it

        # 2) GET 搜索页补充
        if len(items) < 20:
            for it in self._search_page(key, 'GET'):
                if it['vod_id'] not in items:
                    items[it['vod_id']] = it

        # 3) POST 搜索页补充（部分站点 GET 失效）
        if not items:
            for it in self._search_page(key, 'POST'):
                if it['vod_id'] not in items:
                    items[it['vod_id']] = it

        # 4) 分类页模糊匹配兜底（最后手段）
        if not items:
            for it in self._search_by_category(key):
                if it['vod_id'] not in items:
                    items[it['vod_id']] = it

        result = list(items.values())[:50]
        # 关键：空结果不缓存（避免失败结果导致长时间搜不到）
        if result:
            self._cache_set(self._search_cache, cache_key, result, TTL_SEARCH)
        return {"list": result}

    def search(self, key, quick, *args, **kwargs):
        """部分壳子调用 search() 而非 searchContent()，做别名适配"""
        return self.searchContent(key, quick)


# ============================================================
# 本地自测
# ============================================================
if __name__ == "__main__":
    s = Spider()
    home = s.homeContent()
    print("[home]", home["class"])
    print("[home list]", len(home["list"]))
    cat = s.categoryContent("21", 1, True, "{}")
    print("[cat 韩剧第1页]", len(cat["list"]), "pagecount:", cat["pagecount"])
    cat2 = s.categoryContent("21", 1, True, '{"class":"剧情"}')
    print("[cat 二级分类-剧情]", len(cat2["list"]), "pagecount:", cat2["pagecount"])
    if cat.get("list"):
        vid = cat["list"][0]["vod_id"]
        d = s.detailContent([vid])
        v = d["list"][0]
        n_line = len((v.get("vod_play_url") or "").split("$$$"))
        n_ep = len((v.get("vod_play_url") or "").split("$$$")[0].split("#"))
        print("[detail]", vid, v["vod_name"], "线路:", n_line, "首线集数:", n_ep)
        # 测试播放解析
        pid = (v.get("vod_play_url") or "").split("#")[0].split("$")[-1]
        if pid:
            p = s.playerContent('', pid, [])
            print("[player]", p["url"][:80] if p.get("url") else "FAIL")
    sc = s.searchContent("孤单", False)
    print("[search 孤单]", len(sc["list"]), [x["vod_name"] for x in sc["list"][:5]])
