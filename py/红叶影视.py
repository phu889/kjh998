# -*- coding: utf-8 -*-
# ============ 红叶影院 hyyycn.cc 源 v1.1 ============
# 基于71us模板v7.5套写 | 2026-09-13
# v1.1 修复: 分类翻页pagecount恒=1(漏配conch /vodtype/{tid}-{n}.html 数字翻页), 新增vodtype/vodshow双匹配+尾页兜底
# 站点: https://www.hyyycn.cc | 苹果CMS + conch模板
# 路由: 首页(hl-list-item) /vodtype/{tid}{-页}.html(分类) /voddetail/{id}.html(详情)
#       /vodplay/{id}-{线路}-{集}.html(播放) /vodsearch/{词}-------------.html(搜索,7结果实测)
# 播放: 苹果CMS标准 player_ 变量 JSON url/url_next + _dec 解密
# 注意: 本环境出口IP被站点封锁, 解析逻辑用首页HTML快照+web_fetch页面验证; 详情/播放页为苹果CMS标准结构
# 13接口=init/homeContent/categoryContent/detailContent/searchContent/playerContent/localProxy/isVideoFormat/manualVideoCheck/getDependence/destroy/progressVideo/setVideoFlags
# ★版本兼容铁律: 全文件禁3.9+API(random.randbytes/removeprefix/removesuffix等)
# ★分隔符铁律: $=名称/地址 | #=选集 | $$$=线路; 线路名与地址$$$段数必须相等
# ★链路策略v4: 资源默认直连输出, 仅403/防盗链/KEY404/需特殊头才走 localProxy 兜底
# ★链路策略v4: 资源默认直连输出, 仅403/防盗链/KEY404/需特殊头才走 localProxy 兜底
import sys, re, json, time, base64, hashlib, threading, http.server, socket, struct
from urllib.parse import urljoin, quote, unquote
from concurrent.futures import ThreadPoolExecutor
import requests

sys.path.append('..')
try:
    from base.spider import Spider
except ImportError:
    class Spider:
        def fetch(self, url, headers=None, **kw):
            kw.pop('timeout', None)
            r = requests.get(url, headers=headers, timeout=15, **kw)
            r.encoding = 'utf-8'
            return r

# ============ ★ CONFIG ============
HOSTS = ['https://www.hyyycn.cc']  # ★ 多域名轮询(主在前), 防封容灾
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
CATEGORIES = {'1': '电影', '2': '电视剧', '3': '综艺', '4': '动漫', '5': '短剧'}  # ★ 一级; 二三级展平 '1|剧情' '1|剧情|2024'
PK = ''  # ★ 接口密钥(签名/AES key/解密用)
REFERER = 'https://www.hyyycn.cc/'  # ★ 播放/资源防盗链Referer(空=用self.base)
PIC_REFERER = 'https://www.hyyycn.cc/'  # ★ 图片防盗链Referer(空=无)
FD_ZONE = 0  # ★ 分片区段(71us .fd 协议用, 无则0)
PROBE = 0  # ★ 详情多线路实测排序开关 1/0
SITE_KEY = 'hyyycn'  # ★ 壳源标识(海阔setVideoFlags回调时上报, 调试多源用)
VIDEO_EXTS = 'm3u8|mp4|flv|mkv|avi|ts'  # ★ isVideoFormat判定扩展名(竖线分隔)

# ============ AES 纯Python引擎(Crypto不可用时降级) ============
SBOX = [99, 124, 119, 123, 242, 107, 111, 197, 48, 1, 103, 43, 254, 215, 171, 118, 202, 130, 201, 125, 250, 89, 71, 240, 173, 212, 162, 175, 156, 164, 114, 192, 183, 253, 147, 38, 54, 63, 247, 204, 52, 165, 229, 241, 113, 216, 49, 21, 4, 199, 35, 195, 24, 150, 5, 154, 7, 18, 128, 226, 235, 39, 178, 117, 9, 131, 44, 26, 27, 110, 90, 160, 82, 59, 214, 179, 41, 227, 47, 132, 83, 209, 0, 237, 32, 252, 177, 91, 106, 203, 190, 57, 74, 76, 88, 207, 208, 239, 170, 251, 67, 77, 51, 133, 69, 249, 2, 127, 80, 60, 159, 168, 81, 163, 64, 143, 146, 157, 56, 245, 188, 182, 218, 33, 16, 255, 243, 210, 205, 12, 19, 236, 95, 151, 68, 23, 196, 167, 126, 61, 100, 93, 25, 115, 96, 129, 79, 220, 34, 42, 144, 136, 70, 238, 184, 20, 222, 94, 11, 219, 224, 50, 58, 10, 73, 6, 36, 92, 194, 211, 172, 98, 145, 149, 228, 121, 231, 200, 55, 109, 141, 213, 78, 169, 108, 86, 244, 234, 101, 122, 174, 8, 186, 120, 37, 46, 28, 166, 180, 198, 232, 221, 116, 31, 75, 189, 139, 138, 112, 62, 181, 102, 72, 3, 246, 14, 97, 53, 87, 185, 134, 193, 29, 158, 225, 248, 152, 17, 105, 217, 142, 148, 155, 30, 135, 233, 206, 85, 40, 223, 140, 161, 137, 13, 191, 230, 66, 104, 65, 153, 45, 15, 176, 84, 187, 22]
IS = [0] * 256
for _i, _v in enumerate(SBOX):
    IS[_v] = _i
RCON = [1, 2, 4, 8, 16, 32, 64, 128, 27, 54, 108, 216, 171, 77]
G2 = [0] * 256
G3 = [0] * 256
for _i in range(256):
    _t = _i << 1
    if _i & 128:
        _t ^= 0x11b
    G2[_i] = _t
    G3[_i] = G2[_i] ^ _i


def _ke(k):
    nk = len(k) // 4
    nr = nk + 6
    w = [list(k[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = w[i - 1][:]
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [SBOX[b] for b in t]
            t[0] ^= RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            t = [SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return w


def _enc(b, w):
    s = [[b[r + 4 * c] for c in range(4)] for r in range(4)]
    def add(r):
        for i in range(4):
            for j in range(4):
                s[i][j] ^= w[r * 4 + j][i]
    def sub():
        for i in range(4):
            for j in range(4):
                s[i][j] = SBOX[s[i][j]]
    def sh():
        for r in range(1, 4):
            s[r] = s[r][r:] + s[r][:r]
    def mx():
        for c in range(4):
            a = [s[r][c] for r in range(4)]
            s[0][c] = G2[a[0]] ^ G3[a[1]] ^ a[2] ^ a[3]
            s[1][c] = a[0] ^ G2[a[1]] ^ G3[a[2]] ^ a[3]
            s[2][c] = a[0] ^ a[1] ^ G2[a[2]] ^ G3[a[3]]
            s[3][c] = G3[a[0]] ^ a[1] ^ a[2] ^ G2[a[3]]
    add(0)
    nr = len(w) // 4 - 1
    for rnd in range(1, nr):
        sub()
        sh()
        mx()
        add(rnd)
    sub()
    sh()
    add(nr)
    return bytes(s[r][c] for c in range(4) for r in range(4))


def _gm(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = (a << 1) ^ 0x11b if a & 0x80 else a << 1
        b >>= 1
    return p & 0xff


def _dec(b, w):
    s = [[b[r + 4 * c] for c in range(4)] for r in range(4)]
    def add(r):
        for i in range(4):
            for j in range(4):
                s[i][j] ^= w[r * 4 + j][i]
    def isub():
        for i in range(4):
            for j in range(4):
                s[i][j] = IS[s[i][j]]
    def ish():
        for r in range(1, 4):
            s[r] = s[r][-r:] + s[r][:-r]
    def imx():
        for c in range(4):
            a = [s[r][c] for r in range(4)]
            s[0][c] = _gm(a[0], 14) ^ _gm(a[1], 11) ^ _gm(a[2], 13) ^ _gm(a[3], 9)
            s[1][c] = _gm(a[0], 9) ^ _gm(a[1], 14) ^ _gm(a[2], 11) ^ _gm(a[3], 13)
            s[2][c] = _gm(a[0], 13) ^ _gm(a[1], 9) ^ _gm(a[2], 14) ^ _gm(a[3], 11)
            s[3][c] = _gm(a[0], 11) ^ _gm(a[1], 13) ^ _gm(a[2], 9) ^ _gm(a[3], 14)
    nr = len(w) // 4 - 1
    add(nr)
    for rnd in range(nr - 1, 0, -1):
        ish()
        isub()
        add(rnd)
        imx()
    ish()
    isub()
    add(0)
    return bytes(s[r][c] for c in range(4) for r in range(4))


def aes_ecb(data, key, mode=1):
    w = _ke(key)
    out = b''
    if mode:
        pad = 16 - len(data) % 16
        data += bytes([pad]) * pad
        for i in range(0, len(data), 16):
            out += _enc(data[i:i + 16], w)
    else:
        for i in range(0, len(data), 16):
            out += _dec(data[i:i + 16], w)
        if out and 0 < out[-1] <= 16:
            out = out[:-out[-1]]
    return out


def aes_cbc(data, key, iv, enc=1):
    w = _ke(key)
    out = b''
    prev = iv
    if enc:
        pad = 16 - len(data) % 16
        data += bytes([pad]) * pad
        for i in range(0, len(data), 16):
            blk = bytes(data[i + j] ^ prev[j] for j in range(16))
            ct = _enc(blk, w)
            out += ct
            prev = ct
    else:
        for i in range(0, len(data), 16):
            blk = _dec(data[i:i + 16], w)
            out += bytes(blk[j] ^ prev[j] for j in range(16))
            prev = data[i:i + 16]
        if out and 0 < out[-1] <= 16:
            out = out[:-out[-1]]
    return out


class Spider(Spider):
    def init(self, extend=''):
        self.base = HOSTS[0].rstrip('/')  # ★ 主域(init内可被重定向更新)
        self.ua = UA
        self.pk = PK
        self.ref = REFERER or self.base
        self.types = dict(CATEGORIES)
        self.filters = {}  # ★ {'1':[{'key':'class','name':'类型','value':[{'n':'剧情','v':'剧情'}]}]}
        self._pc = {}  # 线路probe缓存 {md5:[ts,froms,urls]}
        self._cache = {}  # ★ 防限速缓存 {key:(ts,val)}
        self._srv = None  # 本地代理线程(延迟启动)
        try:
            r = self.fetch(self.base, headers={'User-Agent': self.ua}, timeout=10000)
            if hasattr(r, 'url') and r.url and r.url != self.base:
                self.base = r.url.rstrip('/')
        except:
            pass

    # ========== 容灾: 多HOST轮询 + requests双保险 ==========
    def _get(self, url, headers=None, timeout=15000):
        hd = headers or {'User-Agent': self.ua, 'Referer': self.ref}
        try:
            r = self.fetch(url, headers=hd, timeout=timeout)
        except TypeError:
            try:
                r = self.fetch(url, headers=hd)
            except Exception:
                return ''
        except Exception:
            return ''
        try:
            return r.text if hasattr(r, 'text') else str(r)
        except Exception:
            return ''
    def _pic(self, u):
        if not u:
            return ''
        if u.startswith('//'):
            u = 'https:' + u
        if not u.startswith('http'):
            u = urljoin(self.base, u)
        return u  # 直连优先; 403时 playerContent/localProxy 兜底

    def _pagecount(self, h, cur=1):
        mx = cur
        # conch数字翻页: /vodtype/{tid}-{n}.html (红叶影院实测翻页结构, 原正则漏配导致无下一页)
        for m in re.finditer(r'/vodtype/\d+-(\d+)\.html', h):
            try:
                n = int(m.group(1))
                if n > mx:
                    mx = n
            except:
                pass
        # vodshow筛选翻页: /vodshow/{tid}-...-{n}---.html
        for m in re.finditer(r'/vodshow/[^"\']*?-(\d+)---\.html', h):
            try:
                n = int(m.group(1))
                if n > mx:
                    mx = n
            except:
                pass
        # 旧模板兼容: /s/{tid}... /wfmwusw/... / page=参数
        for m in re.finditer(r"/(?:s|wfmwusw)/\d+[^\"']*?(\d+)(?:---|-)\.html|page=(\d+)", h):
            try:
                n = int(m.group(1) or m.group(2))
                if n > mx:
                    mx = n
            except:
                pass
        # 下一页兜底: 仅当下一页带真实链接时才+1(避免末页"尾页"纯文本按钮误判)
        if re.search(r'<a[^>]+href="[^"]+"[^>]*>[^<]{0,6}下一页', h) or re.search(r'hl-page-next[^>]*href=|href="[^"]*"[^>]*class="[^"]*next', h):
            mx = max(mx, cur + 1)
        return mx

    # ========== 首页 ==========
    def homeContent(self, filter=False):
        r = {'class': [{'type_id': k, 'type_name': v} for k, v in self.types.items()]}
        if filter and self.filters:
            r['filters'] = self.filters
        r['list'] = self.homeVideoContent().get('list', [])
        return r

    def homeVideoContent(self):
        now = time.time()
        c = self._cache.get('home')
        if c and c[0] > now - 60:
            return {'list': c[1]}
        h = self._get(self.base)
        items = self._items(h) if h else []
        self._cache['home'] = (now, items)
        return {'list': items}

    # ========== 分类(1/2/3级展平+筛选+动态翻页) ==========
    def categoryContent(self, tid, pg=1, filter=False, extend=''):
        try:
            pn = max(int(str(pg)), 1)
        except:
            pn = 1
        t, cls, ex2 = str(tid), '', ''
        if '|' in t:
            p = t.split('|')
            t, cls = p[0], p[1] if len(p) > 1 else ''
            ex2 = p[2] if len(p) > 2 else ''
        ex = {}
        if extend:
            try:
                ex = json.loads(extend) if isinstance(extend, str) else dict(extend)
            except:
                ex = {}
        ck = f'cat:{t}:{cls}:{pn}'
        now = time.time()
        c = self._cache.get(ck)
        if c and c[0] > now - 600:
            return {'page': pn, 'pagecount': c[2], 'limit': 42, 'total': len(c[1]), 'list': c[1]}
        h = self._get(self._cat_url(t, pn, cls, ex2, ex), timeout=20000)
        if not h:
            return {'page': pn, 'pagecount': 1, 'limit': 42, 'total': 0, 'list': []}
        items = self._items(h)
        pc = self._pagecount(h, pn)
        self._cache[ck] = (now, items, pc)
        return {'page': pn, 'pagecount': pc, 'limit': 42, 'total': len(items), 'list': items}

    def _cat_url(self, t, pn, cls='', ex2='', ex=None):
        # ★ hyyycn: /vodtype/{tid}.html(第1页) /vodtype/{tid}-{pg}.html(翻页)
        if cls:
            return f'{self.base}/vodshow/{t}--------{pn}---.html'
        return f'{self.base}/vodtype/{t}-{pn}.html' if pn > 1 else f'{self.base}/vodtype/{t}.html'

    # ========== 详情(多线路 + probe实测排序) ==========
    def detailContent(self, ids, quick='1'):
        vid = str(ids[0] if isinstance(ids, list) else ids or '')
        m = re.search(r'(\d+)', vid)
        vid = m.group(1) if m else ''
        if not vid:
            return {'list': []}
        ck = f'd:{vid}'
        now = time.time()
        c = self._cache.get(ck)
        if c and c[0] > now - 600:
            return {'list': [c[1]]}
        h = self._get(f'{self.base}/voddetail/{vid}.html')
        if not h:
            return {'list': []}
        d = {'vod_id': vid, 'vod_name': '', 'vod_pic': '', 'vod_year': '', 'vod_area': '',
             'vod_class': '', 'vod_director': '', 'vod_actor': '', 'vod_content': '',
             'vod_remarks': '', 'vod_play_from': '', 'vod_play_url': ''}
        tn = re.search(r'<h1[^>]*>(.*?)</h1>', h) or re.search(r'<title>(.*?)</title>', h)
        if tn:
            d['vod_name'] = re.sub(r'<[^>]+>', '', tn.group(1)).split('-')[0].replace('免费在线观看', '').replace('高清完整版', '').strip()
            nm2 = re.match(r'^《([^》]+)》', d['vod_name'])
            if nm2:
                d['vod_name'] = nm2.group(1).strip()
        p = re.search(r'hl-dc-pic[\s\S]{0,500}?data-original="([^"]+)"', h)
        if not p:
            p = re.search(r'data-original="([^"]+)"', h)
        if p:
            d['vod_pic'] = self._pic(p.group(1))
        dm = re.search(r'class="detail-content"[^>]*>([\s\S]*?)</span>', h) or re.search(r'class="[^"]*(?:vod-content|detail-sketch)[^"]*"[^>]*>([\s\S]*?)</span>', h)
        if dm:
            d['vod_content'] = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', dm.group(1))).strip()[:500]
        for k, pat in (('vod_year', r'年份[：:]?\s*(\d{4})'), ('vod_area', r'地区[：:]?\s*([^<\n]+?)(?:\s|</)'),
                       ('vod_class', r'(?:类型|分类)[：:]?\s*([^<\n]+?)(?:\s|</)'), ('vod_remarks', r'更新[：:]</span>\s*([^<\n]+?)(?:\s|</)'),
                       ('vod_remarks', r'状态[：:]?\s*<[^>]*>([^<\n]+?)(?:</|\s|&)')):
            if d[k] == '':
                m2 = re.search(pat, h)
                if m2:
                    d[k] = re.sub(r'<[^>]+>', '', m2.group(1)).strip().replace('&nbsp;', ' ').replace('&amp;', '&').rstrip('，').strip()
        dm2 = re.search(r'导演[：:]\s*([\s\S]*?)(?:</p>|</div>|<div)', h)
        if dm2:
            d['vod_director'] = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', dm2.group(1))).strip().replace('&nbsp;', ' ').replace('&amp;', '&').rstrip('，').strip()
        ac = re.search(r'主演[：:]\s*([\s\S]*?)(?:</p>|</div>|<div)', h)
        if ac:
            d['vod_actor'] = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', ac.group(1))).strip().replace('&nbsp;', ' ').replace('&amp;', '&').rstrip('，').strip()
        pf, pu = self._play_sources(h)
        if pf:
            if PROBE and len(pf) > 1:
                pf, pu = self._sort_lines(pf, pu)
            d['vod_play_from'] = '$$$'.join(pf)
            d['vod_play_url'] = '$$$'.join(pu)
        self._cache[ck] = (now, d)
        return {'list': [d]}

    # ========== 搜索 ==========
    def searchContent(self, key, quick=False, pg='1'):
        try:
            pn = max(int(str(pg)), 1)
        except:
            pn = 1
        h = self._get(f'{self.base}/vodsearch/{quote(key)}-------------.html')
        return {'list': self._items(h) if h else [], 'page': pn}

    def _play_sources(self, h):
        pf, pu = [], []
        # 线路名: conch data-tab="hl-pl-list-N" 或 标准 #playlistN
        tab_names = {}
        for m in re.finditer(r'<a class="hl-tabs-btn[^"]*" href="[^"]*/vodplay/\d+-(\d+)-\d+\.html" alt="([^"]*)">', h):
            if m.group(2).strip():
                tab_names[m.group(1)] = m.group(2).strip()
        for m in re.finditer(r'data-tab="(hl-pl-list-\d+)"[^>]*>([^<]+)</a>', h):
            n = m.group(2).strip()
            if n and len(n) < 20:
                tab_names[m.group(1)] = n
        for m in re.finditer(r'href="#(playlist\d+)"[^>]*>([^<]+)</a>', h):
            n = m.group(2).strip()
            if n and len(n) < 20:
                tab_names[m.group(1)] = n
        # 选集容器(conch: ul.hl-plays-list 每线路一块, 块内链接同sid)
        boxes = []
        for mid in re.finditer(r'<ul class="[^"]*hl-plays-list[^"]*"[^>]*>([\s\S]*?)</ul>', h):
            seg = mid.group(1)
            links, seen_ep = [], set()
            for lm in re.finditer(r'href="(/vodplay/(?:\d+)-(\d+)-(\d+)\.html)"[^>]*>([\s\S]{0,120}?)</a>', seg):
                epn = lm.group(3)
                if epn in seen_ep:
                    continue
                seen_ep.add(epn)
                t = re.sub(r'<[^>]+>', '', lm.group(4)).replace('&nbsp;', ' ').strip() or epn
                links.append((lm.group(1), t))
            if links:
                sm = re.search(r'-(\d+)-\d+\.html', links[0][0])
                boxes.append((sm.group(1) if sm else '', links))
        for pid, links in boxes:
            pf.append(tab_names.get(pid, f'线路{len(pf) + 1}'))
            pu.append('#'.join(f'{ep.strip().replace("#", "-").replace("$", "|")}${urljoin(self.base, href)}' for href, ep in links))
        if not pf:  # 兜底: 全局按线路分组
            routes = {}
            for href, route, ep in re.findall(r'href="(/vodplay/\d+-(\d+)-\d+\.html)"[^>]*>([^<]+)</a>', h):
                if not ep.strip():
                    continue
                routes.setdefault(route, []).append(f'{ep.strip().replace("#", "-").replace("$", "|")}${urljoin(self.base, href)}')
            for i, route in enumerate(sorted(routes.keys(), key=lambda x: int(x) if x.isdigit() else 999)):
                pf.append(tab_names.get(f'hl-pl-list-{route}', f'线路{i + 1}'))
                pu.append('#'.join(routes[route]))
        return pf, pu

    # ========== 播放: 直连优先 → 解密 → VIP插槽 ==========
    def playerContent(self, flag, id, vipFlags=None):
        url = str(id) if id else str(flag)
        if '://' in url and re.search(r'\.(m3u8|mp4|flv|mp3)(\?|$)', url, re.I):
            return {'parse': 0, 'url': url}  # 直连
        full = url if url.startswith('http') else urljoin(self.base, url)
        h = self._get(full)
        if not h:
            return {'parse': 0, 'url': ''}
        u = self._parse_play(h, full)
        if not u:
            u = self._vip_try(full, h, vipFlags)  # 会员: 能破则破, 服务端硬锁放弃
        return {'parse': 0, 'url': u}

    def _parse_play(self, h, page_url):
        pd = re.search(r'var\s+player_\w+\s*=\s*(\{[\s\S]*?\})\s*[;<]', h)
        if pd:
            try:
                j = json.loads(pd.group(1))
                u = j.get('url', '') or j.get('url_next', '')
                if u:
                    u2 = self._dec(u, page_url)
                    if u2:
                        return u2
            except:
                pass
        m = re.search(r'var\s*(?:now|url)\s*=\s*["\']([^"\']+)["\']', h)
        if m:
            u2 = self._dec(m.group(1), page_url)
            if u2:
                return u2
        for m in re.finditer(r'(https?://[^\s"\'<>]+\.(?:m3u8|mp4|flv))', h):
            return m.group(1)
        iframe = re.search(r'<iframe[^>]+src="([^"]+)"', h, re.I)
        if iframe:
            u = iframe.group(1)
            if u.startswith('http'):
                h2 = self._get(u)
                if h2:
                    return self._parse_play(h2, u)
        return ''

    def _dec(self, u, page_url):
        u = u.strip()
        if re.search(r'\.(m3u8|mp4|flv)(\?|$)', u, re.I):
            return u
        try:  # base64
            s = u.encode()
            s2 = base64.b64decode(s + b'=' * (-len(s) % 4)).decode('utf-8', 'ignore')
            if re.search(r'\.(m3u8|mp4|flv)(\?|$)', s2, re.I):
                return s2
        except:
            pass
        # ★ AES-CBC 解密插槽: aes_cbc(base64.b64decode(s2), key, iv, 0)
        return u if u.startswith('http') else ''

    def _vip_try(self, page_url, h, vipFlags):
        # ★ 模板插槽: 会员能破则破(拼token/签名/老接口); 服务端硬锁返回''
        return ''

    # ========== 四壳13接口扩展钩子(v7.5): isVideoFormat/manualVideoCheck/getDependence/destroy/progressVideo/setVideoFlags ==========
    def isVideoFormat(self, url):
        if not url:
            return False
        if '.m3u8' in url:
            return True
        return bool(re.search(r'\.(?:%s)(?:\?|$)' % (VIDEO_EXTS or 'm3u8|mp4|flv'), url, re.I))

    def manualVideoCheck(self):
        return False

    def getDependence(self):
        return ''

    def destroy(self):
        try:
            self._c.clear()
            self._pc.clear()
            self._srv = None
        except Exception:
            pass

    def progressVideo(self, speed, time, end):
        return False

    def setVideoFlags(self, siteKey, flags):
        try:
            self._siteKey = siteKey or SITE_KEY
            self._vflags = flags or {}
        except Exception:
            pass

    # ========== 本地代理(9979-9988): m3u8 KEY/分片重写 + 图片转码 ==========
    def localProxy(self, param):
        p = param.split('url=', 1)[-1] if 'url=' in param else param
        p = unquote(p) if '%' in p else p
        if re.search(r'\.(jpe?g|png|webp|gif)(\?|$)', p, re.I):
            return self._img(p)
        if '.m3u8' in p:
            return self._rewrite_m3u8(p)
        try:
            r = self.fetch(p, headers={'User-Agent': self.ua, 'Referer': self.ref}, timeout=20000)
            if hasattr(r, 'status_code') and r.status_code != 200:
                return {'code': r.status_code, 'content': b'', 'headers': {}}
            return {'code': 200, 'content': r.content, 'headers': {'Content-Type': r.headers.get('Content-Type', 'application/octet-stream')}}
        except:
            return {'code': 404, 'content': b'', 'headers': {}}

    def _rewrite_m3u8(self, url):
        try:
            r = self.fetch(url, headers={'User-Agent': self.ua, 'Referer': self.ref}, timeout=20000)
            if hasattr(r, 'status_code') and r.status_code != 200:
                return {'code': r.status_code, 'content': b'', 'headers': {}}
            body = r.text if hasattr(r, 'text') else str(r)
        except:
            return {'code': 404, 'content': b'', 'headers': {}}
        base = url.rsplit('/', 1)[0] + '/'
        origin = re.match(r'https?://[^/]+', url)
        origin = origin.group(0) if origin else ''
        out = []
        for ln in body.splitlines():
            if ln.startswith('#EXT-X-KEY'):
                m = re.search(r'URI="([^"]+)"', ln)
                if m:
                    ku = m.group(1)
                    if ku.startswith('/'):
                        ku = origin + ku  # 根相对路径拼origin
                    elif not ku.startswith('http'):
                        ku = base + ku
                    ln = ln.replace('URI="%s"' % m.group(1), 'URI="%s"' % ('proxy?url=' + quote(ku, safe='')))
            elif ln.startswith('http'):
                ln = 'proxy?url=' + quote(ln, safe='')
            elif ln.startswith('/') and not ln.startswith('//'):
                ln = 'proxy?url=' + quote(origin + ln, safe='')
            out.append(ln)
        return {'code': 200, 'content': '\n'.join(out), 'headers': {'Content-Type': 'application/vnd.apple.mpegurl'}}

    def _img(self, u):
        try:
            r = requests.get(u, headers={'User-Agent': self.ua, 'Referer': PIC_REFERER or self.ref}, timeout=15)
            data, ct = r.content, r.headers.get('Content-Type', 'image/jpeg')
            if data[:4] == b'RIFF' or 'webp' in ct:
                try:
                    from PIL import Image
                    import io
                    buf = io.BytesIO()
                    Image.open(io.BytesIO(data)).convert('RGB').save(buf, 'JPEG', quality=85)
                    data, ct = buf.getvalue(), 'image/jpeg'
                except:
                    ct = 'image/webp'
            return {'code': 200, 'content': data, 'headers': {'Content-Type': ct}}
        except:
            return {'code': 404, 'content': b'', 'headers': {}}

    # ========== 列表解析(conch hl-lazy卡片: a内 href+title+data-original 同源提取, 防错位) ==========
    def _items(self, h):
        items, seen = [], set()
        for m in re.finditer(r'<a class="[^"]*hl-lazy[^"]*"([^>]*)>([\s\S]*?)</a>', h):
            attrs, inner = m.group(1), m.group(2)
            hm = re.search(r'href="/(?:voddetail|wfmwudt)/(\d+)\.html"', attrs)
            if not hm:
                continue
            vid = hm.group(1)
            if vid in seen:
                continue
            seen.add(vid)
            tm = re.search(r'title="([^"]*)"', attrs)
            name = re.sub(r'<[^>]+>', '', tm.group(1)).strip() if tm else ''
            if not name:
                nm = re.search(r'hl-item-title[^>]*>\s*<a[^>]*>([^<]+)</a>', h[m.end():m.end() + 400])
                name = nm.group(1).strip() if nm else ''
            if not name or len(name) > 100:
                continue
            pm = re.search(r'data-original="([^"]*)"', attrs)
            if not pm:
                pm = re.search(r'(?:data-original|data-src|src)="([^"]*)"', inner)
            pic = self._pic(pm.group(1).strip()) if pm and pm.group(1).strip() else ''
            rmk = re.search(r'class="[^"]*(?:remarks|pic-text|note|status|br-sub)[^"]*"[^>]*>([^<]+)', inner)
            rmk_txt = rmk.group(1).split('&nbsp;')[0].strip()[:30] if rmk else ''
            items.append({'vod_id': vid, 'vod_name': name[:50],
                          'vod_pic': pic,
                          'vod_remarks': rmk_txt})
        if not items:
            items = self._items_fb(h)
        return items

    def _items_fb(self, h):
        items, seen = [], set()
        for m in re.finditer(r'href="/(?:voddetail|wfmwudt)/(\d+)\.html"[^>]*title="([^"]*)"', h):
            vid, name = m.group(1), re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if not name or len(name) > 100 or vid in seen:
                continue
            seen.add(vid)
            after = h[m.end():m.end() + 1000]
            cover = re.search(r'(?:data-original|original|src)="([^"]*)"', after, re.I)
            remark = re.search(r'class="[^"]*remarks[^"]*"[^>]*>([^<]+)<', after)
            items.append({'vod_id': vid, 'vod_name': name[:50],
                          'vod_pic': self._pic(cover.group(1)) if cover and cover.group(1).strip() else '',
                          'vod_remarks': remark.group(1).strip() if remark else ''})
        return items
