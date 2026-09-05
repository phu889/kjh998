# -*- coding: utf-8 -*-
"""
天寒影视 Python Spider — 兼容 FongMi/TV (T3) 与 WebHomeTV / PeekPro (T4)
站点: https://v.sjzsyjxx.com/

特性:
  - HTML 解析模式，直连 m3u8 播放
  - 四级筛选：类型 + 地区 + 年份 + 排序（服务端筛选）
  - 分类页多页聚合，视频数量翻倍（36+ 部）
  - 多线路播放：hhm3u8 / snm3u8 / okm3u8 等
  - 搜索：POST 提交，自动跟随 meta 重定向
  - 三级缓存：首页 + 详情页 + 播放地址
  - 播放地址后台预热，点击秒开
  - Session 连接复用，全链路短超时
  - SSL 禁验证
"""

import sys
import re
import json
import time
import html as _html
import threading
from urllib.parse import quote

sys.path.append('..')

# ===== 兼容导入 =====
try:
    from base.spider import Spider
except ImportError:
    import requests as _rq
    try:
        import urllib3
        urllib3.disable_warnings()
    except Exception:
        pass

    class Spider:
        def fetch(self, url, headers=None, **kw):
            timeout = kw.pop('timeout', 15)
            r = _rq.get(url, headers=headers, timeout=timeout, verify=False, **kw)
            r.encoding = 'utf-8'
            return r


# ============================================================
# 常量
# ============================================================

HOST = "https://v.sjzsyjxx.com"
UA = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"

# 一级分类
CLASSES = [
    {"type_name": "电影", "type_id": "dianying"},
    {"type_name": "电视剧", "type_id": "dianshiju"},
    {"type_name": "动漫", "type_id": "dongman"},
    {"type_name": "综艺", "type_id": "zongyi"},
    {"type_name": "爽文短剧", "type_id": "shuangwenduanju"},
    {"type_name": "影视解说", "type_id": "yingshijieshuo"},
]

# 通用类型列表（苹果CMS标准类型）
_TYPE_LIST = [
    {"n": "全部", "v": "0"},
    {"n": "动作", "v": "1"},
    {"n": "喜剧", "v": "2"},
    {"n": "爱情", "v": "3"},
    {"n": "科幻", "v": "4"},
    {"n": "恐怖", "v": "5"},
    {"n": "剧情", "v": "6"},
    {"n": "战争", "v": "7"},
    {"n": "悬疑", "v": "8"},
    {"n": "犯罪", "v": "9"},
    {"n": "动画", "v": "10"},
    {"n": "惊悚", "v": "11"},
    {"n": "冒险", "v": "12"},
    {"n": "奇幻", "v": "13"},
    {"n": "武侠", "v": "14"},
    {"n": "古装", "v": "15"},
    {"n": "运动", "v": "16"},
    {"n": "家庭", "v": "17"},
    {"n": "传记", "v": "18"},
    {"n": "历史", "v": "19"},
]

# 地区列表
_AREA_LIST = [
    {"n": "全部", "v": "0"},
    {"n": "内地", "v": "1"},
    {"n": "中国香港", "v": "2"},
    {"n": "中国台湾", "v": "3"},
    {"n": "美国", "v": "4"},
    {"n": "日本", "v": "5"},
    {"n": "韩国", "v": "6"},
    {"n": "泰国", "v": "7"},
    {"n": "法国", "v": "8"},
    {"n": "加拿大", "v": "9"},
    {"n": "德国", "v": "10"},
    {"n": "英国", "v": "11"},
    {"n": "印度", "v": "12"},
    {"n": "其他", "v": "13"},
]

# 年份列表
_YEAR_LIST = [
    {"n": "全部", "v": "0"},
    {"n": "2026", "v": "2026"},
    {"n": "2025", "v": "2025"},
    {"n": "2024", "v": "2024"},
    {"n": "2023", "v": "2023"},
    {"n": "2022", "v": "2022"},
    {"n": "2021", "v": "2021"},
    {"n": "2020", "v": "2020"},
    {"n": "2019", "v": "2019"},
    {"n": "2018", "v": "2018"},
    {"n": "2017", "v": "2017"},
    {"n": "2016", "v": "2016"},
    {"n": "2015", "v": "2015"},
    {"n": "2014", "v": "2014"},
    {"n": "2013", "v": "2013"},
    {"n": "2012", "v": "2012"},
    {"n": "2011", "v": "2011"},
    {"n": "2010", "v": "2010"},
    {"n": "2000-2009", "v": "2000"},
    {"n": "1999以前", "v": "1999"},
]

# 排序列表
_SORT_LIST = [
    {"n": "最新", "v": "time"},
    {"n": "人气", "v": "hits"},
    {"n": "评分", "v": "score"},
]

# 线路显示名称映射
LINE_NAMES = {
    "hhm3u8": "辉煌资源",
    "snm3u8": "索尼资源",
    "okm3u8": "OK资源",
    "m3u8": "默认线路",
    "xhm3u8": "新火资源",
    "bdm3u8": "百度资源",
    "qim3u8": "奇葩资源",
    "wjm3u8": "无尽资源",
    "wolong": "卧龙资源",
    "dbm3u8": "豆瓣资源",
    "ffzy": "非凡资源",
    "zuidam3u8": "最大资源",
    "tpm3u8": "淘片资源",
}

# 构建各分类筛选器（类型 + 地区 + 年份 + 排序）
FILTERS = {}
for c in CLASSES:
    tid = c["type_id"]
    FILTERS[tid] = [
        {"key": "type", "name": "类型", "value": _TYPE_LIST},
        {"key": "area", "name": "地区", "value": _AREA_LIST},
        {"key": "year", "name": "年份", "value": _YEAR_LIST},
        {"key": "sort", "name": "排序", "value": _SORT_LIST},
    ]

# 每页视频数（网站固定值）
PAGE_SIZE = 18
# 分类页聚合页数（前5页并发获取，78+ 部）
AGGREGATE_PAGES = 5
# 首页最大视频数
HOME_MAX = 72


# ============================================================
# Spider 主类
# ============================================================

class Spider(Spider):

    def getName(self):
        return "天寒影视"

    # ===== 初始化 =====
    def init(self, extend=""):
        if isinstance(extend, list):
            self.extend = ""
        else:
            self.extend = extend or ""

        self.header = {
            "User-Agent": UA,
            "Referer": HOST + "/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        # requests session（保持 cookie + 连接复用）
        try:
            import requests
            self._session = requests.Session()
            self._session.verify = False
            self._session.headers.update({"User-Agent": UA})
        except Exception:
            self._session = None

        # 三级缓存
        self._home_cache = []
        self._home_cache_time = 0
        self._play_url_cache = {}   # play_page_url -> m3u8
        self._detail_cache = {}     # vod_id -> detail_data
        self._list_cache = {}       # list_cache_key -> (vods, pagecount, timestamp)
        self._search_cache = {}     # keyword -> (vods, timestamp)

    # ===== 网络工具 =====
    def _rsp_text(self, rsp):
        """获取响应文本（强制 UTF-8）"""
        try:
            if hasattr(rsp, 'encoding'):
                rsp.encoding = 'utf-8'
            return rsp.text
        except Exception:
            try:
                return rsp.content.decode('utf-8', 'ignore')
            except Exception:
                return ""

    def _get_html(self, url, timeout=6, referer=None):
        """GET 请求返回 HTML"""
        try:
            headers = dict(self.header)
            if referer:
                headers["Referer"] = referer
            if self._session:
                rsp = self._session.get(url, headers=headers, timeout=timeout)
            else:
                rsp = self.fetch(url, headers=headers, timeout=timeout)
            return self._rsp_text(rsp)
        except Exception:
            return ""

    def _post_html(self, url, data, timeout=8, referer=None):
        """POST 请求，跟随 meta/JS 重定向"""
        try:
            import requests
            headers = dict(self.header)
            if referer:
                headers["Referer"] = referer
            headers["Content-Type"] = "application/x-www-form-urlencoded"

            if self._session:
                rsp = self._session.post(url, data=data, headers=headers, timeout=timeout)
            else:
                rsp = requests.post(url, data=data, headers=headers,
                                   timeout=timeout, verify=False)
            rsp.encoding = 'utf-8'
            text = rsp.text

            # 跟随 meta refresh 重定向
            redirect_url = self._match(r'URL=(' + re.escape(HOST) + r'[^\"]+)', text)
            if not redirect_url:
                redirect_url = self._match(r'location\.href=\"(' + re.escape(HOST) + r'[^\"]+)\"', text)
            if redirect_url:
                return self._get_html(redirect_url, timeout=timeout, referer=url)
            return text
        except Exception:
            return ""

    def _match(self, pattern, text, flags=0):
        """正则匹配第一个分组"""
        m = re.search(pattern, text, flags)
        return m.group(1) if m else ""

    # ===== 列表页解析 =====
    def _parse_list(self, html):
        """解析视频列表（li 逐个解析，不遗漏）"""
        vods = []
        seen = set()

        li_pattern = re.compile(r'<li[^>]*>(.*?)</li>', re.S)
        for li_html in li_pattern.findall(html):
            vid_match = re.search(
                r'href="' + re.escape(HOST) + r'/gd/([^"]+)\.jsp"',
                li_html
            )
            if not vid_match:
                continue
            vid = vid_match.group(1)
            if vid in seen:
                continue
            seen.add(vid)

            # 图片
            pic = ""
            pic_m = re.search(r'data-original="([^"]+)"', li_html)
            if pic_m:
                pic = pic_m.group(1)
                if pic.startswith("//"):
                    pic = "https:" + pic
            else:
                src_m = re.search(r'src="([^"]+\.(?:webp|jpg|png))"', li_html, re.I)
                if src_m:
                    pic = src_m.group(1)
                    if pic.startswith("//"):
                        pic = "https:" + pic

            # 备注
            remark = ""
            span_m = re.search(r'<span[^>]*>([^<]+)</span>', li_html)
            if span_m:
                remark = _html.unescape(span_m.group(1).strip())

            # 标题
            title = ""
            biaoti_m = re.search(
                r'class="[^"]*biaoti[^"]*"[^>]*>\s*<a[^>]*>([^<]+)</a>',
                li_html
            )
            if biaoti_m:
                title = _html.unescape(biaoti_m.group(1).strip())
            else:
                title_m = re.search(
                    r'href="' + re.escape(HOST) + r'/gd/' + re.escape(vid) + r'\.jsp"[^>]*>([^<]+)</a>',
                    li_html
                )
                if title_m:
                    title = _html.unescape(title_m.group(1).strip())

            if not title:
                title = vid

            vods.append({
                "vod_id": vid,
                "vod_name": title,
                "vod_pic": pic,
                "vod_remarks": remark or "HD",
            })

        # 降级：从 biaoti 类提取
        if not vods:
            items = re.findall(
                r'class="[^"]*biaoti[^"]*"[^>]*>\s*<a[^>]*href="' +
                re.escape(HOST) + r'/gd/([^"]+)\.jsp"[^>]*>([^<]+)</a>',
                html, re.S
            )
            for vid, title in items:
                if vid in seen:
                    continue
                seen.add(vid)
                vods.append({
                    "vod_id": vid,
                    "vod_name": _html.unescape(title.strip()),
                    "vod_pic": "",
                    "vod_remarks": "HD",
                })

        return vods

    def _parse_pagecount(self, html):
        """解析总页数
        网站分页格式: <b>1/100</b>
        同时也有 /p2/ 形式的翻页链接
        """
        # 优先匹配 <b>当前/总页</b> 格式
        m = re.search(r'<b>\s*\d+\s*/\s*(\d+)\s*</b>', html)
        if m:
            return int(m.group(1))
        # 降级：从分页链接中取最大值
        pages = re.findall(r'/p(\d+)/', html)
        if pages:
            return max(int(p) for p in pages)
        return 1

    def _build_list_url(self, tid, year="0", area="0", typ="0", sort="", page=1):
        """构建分类列表 URL
        格式: /{tid}/y{year}-d{area}-t{type}-sort-{sort}-p{page}/
        无筛选时: /{tid}/ 或 /{tid}/p{page}/
        """
        has_filter = (typ and typ != "0") or (sort and sort != "time")
        has_year = year and year != "0"
        has_area = area and area != "0"

        # 无任何筛选参数
        if not has_filter and not has_year and not has_area:
            if page and int(page) > 1:
                return HOST + "/%s/p%s/" % (tid, page)
            return HOST + "/%s/" % tid

        # 有筛选参数
        parts = []
        parts.append("y%s" % year)
        parts.append("d%s" % area)
        if typ and typ != "0":
            parts.append("t%s" % typ)
        if sort and sort != "time":
            parts.append("sort-%s" % sort)
        if page and int(page) > 1:
            parts.append("p%s" % page)

        return HOST + "/%s/%s/" % (tid, "-".join(parts))

    # ===== 详情页解析 =====
    def _parse_detail(self, html, vod_id):
        """解析详情页"""
        # 标题
        title = self._match(r'<h1[^>]*>(.*?)</h1>', html, re.S)
        title = _html.unescape(re.sub(r'<[^>]+>', '', title or '').strip())

        # 图片
        pic = self._match(r'data-original="([^"]+)"', html)
        if pic.startswith("//"):
            pic = "https:" + pic

        # 导演
        director = _html.unescape(self._match(r'导演[：:]\s*([^<\n]+)', html).strip())

        # 演员
        actor = _html.unescape(self._match(r'(?:主演|演员)[：:]\s*([^<\n]+)', html).strip())

        # 年代/语言
        year_area_lang = self._match(r'年代[：:]\s*([^<\n]+)', html).strip()
        year = ""
        area = ""
        if year_area_lang:
            year_area_lang = _html.unescape(year_area_lang)
            # 格式通常：2022语言：汉语普通话
            ym = re.match(r'(\d{4})', year_area_lang)
            if ym:
                year = ym.group(1)

        # 类型（从vod_class或页面中提取，可能没有）
        type_name = ""

        # 简介
        intro = self._match(r'简介[：:]\s*(.*?)</(?:div|p|li)>', html, re.S)
        if not intro:
            intro = self._match(r'class="[^"]*desc[^"]*"[^>]*>(.*?)</div>', html, re.S)
        intro = _html.unescape(re.sub(r'<[^>]+>', '', intro or '').strip())
        intro = re.sub(r'\s+', ' ', intro)[:500]

        # 备注/状态
        remark = self._match(r'<span[^>]*>.*?(更新至\d+集|全\d+集|HD|TC|抢先版|正片|完结|4K)[^<]*</span>', html)
        if not remark:
            remark = "HD"

        # ===== 解析播放线路和剧集 =====
        play_pattern = re.compile(
            r'href="(' + re.escape(HOST) + r'/l\d+/' + re.escape(vod_id) +
            r'-([^-/]+)-(\d+)\.jsp)"[^>]*>([^<]+)</a>'
        )
        all_plays = play_pattern.findall(html)

        line_eps = {}
        for url, line_name, ep_idx, ep_name in all_plays:
            ep_name = _html.unescape(ep_name.strip())
            if not ep_name or ep_name == "立即播放":
                continue
            if line_name not in line_eps:
                line_eps[line_name] = []
            line_eps[line_name].append((ep_name, url, int(ep_idx)))

        # 构建 play_from / play_url
        play_from = []
        play_url = []
        for line_name, eps in line_eps.items():
            eps.sort(key=lambda x: x[2])
            display_name = LINE_NAMES.get(line_name, line_name)
            play_from.append(display_name)
            ep_strs = []
            for ep_name, ep_url, _ in eps:
                ep_strs.append("%s$%s" % (ep_name, ep_url))
            play_url.append("#".join(ep_strs))

        vod = {
            "vod_id": vod_id,
            "vod_name": title,
            "vod_pic": pic,
            "type_name": type_name,
            "vod_year": year,
            "vod_area": area,
            "vod_remarks": remark,
            "vod_actor": actor,
            "vod_director": director,
            "vod_content": intro,
            "vod_play_from": "$$$".join(play_from) if play_from else "天寒影视",
            "vod_play_url": "$$$".join(play_url) if play_url else "",
        }
        return vod

    # ===== 播放地址解析 =====
    def _extract_m3u8(self, html):
        """从播放页提取 m3u8 地址"""
        m3u8 = self._match(r"url:\s*['\"]([^'\"]+\.m3u8[^'\"]*)['\"]", html)
        if m3u8:
            return m3u8
        m3u8 = self._match(r'(https?://[^\s\"\\]+\.m3u8[^\s\"\\]*)', html)
        return m3u8 if m3u8 else ""

    def _get_play_url(self, play_page_url):
        """获取播放页 m3u8（带缓存）"""
        if play_page_url in self._play_url_cache:
            return self._play_url_cache[play_page_url]

        html = self._get_html(play_page_url, timeout=6, referer=HOST + "/")
        m3u8 = self._extract_m3u8(html)

        if m3u8:
            self._play_url_cache[play_page_url] = m3u8
        return m3u8

    def _prewarm_play_urls(self, vod):
        """后台预热：预解析首条线路前3集"""
        try:
            if not vod.get("vod_play_url"):
                return
            url_groups = vod["vod_play_url"].split("$$$")
            if not url_groups:
                return
            first_line = url_groups[0]
            eps = first_line.split("#")[:3]
            for ep in eps:
                if "$" in ep:
                    _, ep_url = ep.split("$", 1)
                    if ep_url and ep_url not in self._play_url_cache:
                        self._get_play_url(ep_url)
        except Exception:
            pass

    # ============================================================
    # 首页
    # ============================================================

    def homeContent(self, filter):
        return {
            "class": CLASSES,
            "filters": FILTERS,
        }

    def homeVideoContent(self):
        """首页推荐：首页内容 + 第一分类补充，带缓存"""
        now = int(time.time())
        if self._home_cache and now - self._home_cache_time < 300:
            return {"list": self._home_cache}

        html = self._get_html(HOST + "/", timeout=6)
        videos = self._parse_list(html)

        # 不足时补充第一个分类
        if len(videos) < 30 and CLASSES:
            extra_html = self._get_html(
                HOST + "/%s/" % CLASSES[0]["type_id"], timeout=5
            )
            if extra_html:
                extra = self._parse_list(extra_html)
                seen = set(v["vod_id"] for v in videos)
                for v in extra:
                    if v["vod_id"] not in seen:
                        videos.append(v)
                        seen.add(v["vod_id"])

        self._home_cache = videos[:HOME_MAX]
        self._home_cache_time = now
        return {"list": self._home_cache}

    # ============================================================
    # 分类列表（多页聚合）
    # ============================================================

    def categoryContent(self, tid, pg, filter, extend):
        try:
            page = int(pg or 1)
            if page < 1:
                page = 1

            # 解析筛选参数
            ext = {}
            if extend:
                if isinstance(extend, dict):
                    ext = extend
                elif isinstance(extend, str):
                    try:
                        ext = json.loads(extend)
                    except Exception:
                        ext = {}

            year = str(ext.get("year", "0") or "0")
            area = str(ext.get("area", "0") or "0")
            typ = str(ext.get("type", "0") or "0")
            sort = str(ext.get("sort", "") or "")

            # 列表缓存 key
            cache_key = "%s_%s_%s_%s_%s" % (tid, year, area, typ, sort)

            def _fetch_page(p):
                url = self._build_list_url(tid, year, area, typ, sort, p)
                html = self._get_html(url, timeout=6, referer=HOST + "/")
                if not html:
                    return [], 1
                vods = self._parse_list(html)
                pc = self._parse_pagecount(html)
                return vods, pc

            # 检查缓存（2分钟有效）
            now = time.time()
            if cache_key in self._list_cache:
                cached_vods, cached_pagecount, ts = self._list_cache[cache_key]
                if now - ts < 120:
                    # 缓存命中：优先从缓存返回
                    start = (page - 1) * PAGE_SIZE
                    end = start + PAGE_SIZE
                    page_vods = cached_vods[start:end]
                    # 如果缓存内有数据，直接返回
                    if page_vods:
                        return {
                            "list": page_vods,
                            "page": page,
                            "pagecount": cached_pagecount,
                            "limit": PAGE_SIZE,
                            "total": cached_pagecount * PAGE_SIZE,
                        }
                    # 缓存内无数据但请求页超出聚合范围，单独请求
                    if page > AGGREGATE_PAGES:
                        page_vods, _ = _fetch_page(page)
                        return {
                            "list": page_vods,
                            "page": page,
                            "pagecount": cached_pagecount,
                            "limit": PAGE_SIZE,
                            "total": cached_pagecount * PAGE_SIZE,
                        }

            # ===== 多页并发聚合 =====
            all_vods = []
            seen_ids = set()
            total_pagecount = 1

            # 并发获取前 AGGREGATE_PAGES 页
            pages_to_fetch = AGGREGATE_PAGES
            results = {}
            threads = []

            def _worker(p):
                v, pc = _fetch_page(p)
                results[p] = (v, pc)

            for p in range(1, pages_to_fetch + 1):
                t = threading.Thread(target=_worker, args=(p,))
                threads.append(t)
                t.start()

            for t in threads:
                t.join()

            # 按页顺序合并去重
            for p in range(1, pages_to_fetch + 1):
                if p in results:
                    vods, pc = results[p]
                    if p == 1:
                        total_pagecount = pc
                    for v in vods:
                        if v["vod_id"] not in seen_ids:
                            seen_ids.add(v["vod_id"])
                            all_vods.append(v)

            # 存入缓存
            self._list_cache[cache_key] = (all_vods, total_pagecount, now)

            # 按请求页码返回
            start = (page - 1) * PAGE_SIZE
            end = start + PAGE_SIZE
            page_vods = all_vods[start:end]

            # 请求页超出聚合范围，单独请求
            if not page_vods and page > AGGREGATE_PAGES:
                page_vods, _ = _fetch_page(page)

            return {
                "list": page_vods,
                "page": page,
                "pagecount": max(total_pagecount, page),
                "limit": PAGE_SIZE,
                "total": total_pagecount * PAGE_SIZE,
            }
        except Exception:
            return {"page": 1, "pagecount": 1, "limit": PAGE_SIZE, "total": 0, "list": []}

    # ============================================================
    # 详情页
    # ============================================================

    def detailContent(self, ids):
        if isinstance(ids, str):
            ids = [ids]
        vod_id = str(ids[0])

        # 缓存
        if vod_id in self._detail_cache:
            return {"list": [self._detail_cache[vod_id]]}

        url = HOST + "/gd/%s.jsp" % vod_id

        # 重试
        html = ""
        for attempt in range(2):
            html = self._get_html(url, timeout=8, referer=HOST + "/")
            if html and len(html) > 2000:
                break
            if attempt < 1:
                time.sleep(0.3)

        if not html or len(html) < 1000:
            return {"list": []}

        vod = self._parse_detail(html, vod_id)

        if not vod.get("vod_play_url"):
            return {"list": []}

        # 缓存
        self._detail_cache[vod_id] = vod

        # 后台预热播放地址
        try:
            t = threading.Thread(target=self._prewarm_play_urls, args=(vod,))
            t.daemon = True
            t.start()
        except Exception:
            pass

        return {"list": [vod]}

    # ============================================================
    # 搜索
    # ============================================================

    def searchContent(self, key, quick, pg="1"):
        try:
            page = int(pg or 1)
            if page < 1:
                page = 1

            # 搜索缓存（60秒）
            now = int(time.time())
            cache_key = key.lower()
            if cache_key in self._search_cache:
                cached_vods, ts = self._search_cache[cache_key]
                if now - ts < 60:
                    return {"list": cached_vods}

            vods = []

            # 方式1：POST 搜索 + 跟随 meta 重定向
            try:
                # 先访问搜索页获取 cookie
                self._get_html(HOST + "/10.jsp", timeout=5)

                search_url = HOST + "/cgi/search"
                data = "search_key=%s&search_leixing=影片" % quote(key)

                html = self._post_html(
                    search_url, data,
                    timeout=10,
                    referer=HOST + "/10.jsp"
                )
                if html:
                    vods = self._parse_list(html)
            except Exception:
                pass

            # 方式2：如果方式1无结果，尝试 GET /search.php?q=
            if not vods:
                try:
                    import requests
                    url = HOST + "/search.php?q=%s" % quote(key)
                    if self._session:
                        rsp = self._session.get(url, headers=self.header, timeout=8)
                    else:
                        rsp = requests.get(url, headers=self.header, timeout=8, verify=False)
                    rsp.encoding = 'utf-8'
                    if rsp.text:
                        vods = self._parse_list(rsp.text)
                except Exception:
                    pass

            # 方式3：如果仍无结果，尝试 GET /so/keyword/
            if not vods:
                try:
                    import requests
                    url = HOST + "/so/%s/" % quote(key)
                    if self._session:
                        rsp = self._session.get(url, headers=self.header, timeout=8)
                    else:
                        rsp = requests.get(url, headers=self.header, timeout=8, verify=False)
                    rsp.encoding = 'utf-8'
                    if rsp.text:
                        vods = self._parse_list(rsp.text)
                except Exception:
                    pass

            # 缓存
            if vods:
                self._search_cache[cache_key] = (vods, now)

            return {"list": vods}
        except Exception:
            return {"list": []}

    # ============================================================
    # 播放解析
    # ============================================================

    def playerContent(self, flag, id, vipFlags):
        if not id:
            return {"parse": 0, "playUrl": "", "url": ""}

        play_url = str(id).replace("\\/", "/")

        # 已经是直链
        if ".m3u8" in play_url.lower() or ".mp4" in play_url.lower():
            is_m3u8 = ".m3u8" in play_url.lower()
            try:
                if "://" in play_url:
                    scheme = play_url.split("://")[0]
                    host = play_url.split("://")[1].split("/")[0]
                    media_referer = scheme + "://" + host + "/"
                else:
                    media_referer = HOST + "/"
            except Exception:
                media_referer = HOST + "/"

            return {
                "parse": 0,
                "playUrl": "",
                "url": play_url,
                "header": {
                    "User-Agent": UA,
                    "Referer": media_referer,
                },
                "format": "application/x-mpegURL" if is_m3u8 else "",
                "contentType": "application/x-mpegURL" if is_m3u8 else "",
            }

        # 播放页 -> 提取 m3u8
        if HOST in play_url and "/l" in play_url:
            m3u8 = self._get_play_url(play_url)
            if m3u8 and (".m3u8" in m3u8.lower() or ".mp4" in m3u8.lower()):
                is_m3u8 = ".m3u8" in m3u8.lower()
                try:
                    if "://" in m3u8:
                        scheme = m3u8.split("://")[0]
                        host = m3u8.split("://")[1].split("/")[0]
                        media_referer = scheme + "://" + host + "/"
                    else:
                        media_referer = HOST + "/"
                except Exception:
                    media_referer = HOST + "/"

                return {
                    "parse": 0,
                    "playUrl": "",
                    "url": m3u8,
                    "header": {
                        "User-Agent": UA,
                        "Referer": media_referer,
                    },
                    "format": "application/x-mpegURL" if is_m3u8 else "",
                    "contentType": "application/x-mpegURL" if is_m3u8 else "",
                }
            # 解析失败，交给壳子嗅探
            return {
                "parse": 1,
                "playUrl": "",
                "url": play_url,
                "header": {
                    "User-Agent": UA,
                    "Referer": HOST + "/",
                },
            }

        # 其他 URL
        return {
            "parse": 0,
            "playUrl": "",
            "url": play_url,
            "header": {
                "User-Agent": UA,
                "Referer": HOST + "/",
            },
        }

    # ===== 本地代理 =====
    def localProxy(self, param):
        return [200, "video/MP2T", b"", ""]

    # ===== 清理 =====
    def destroy(self):
        self._play_url_cache.clear()
        self._detail_cache.clear()
        self._list_cache.clear()
        self._search_cache.clear()

    def close(self):
        self.destroy()
