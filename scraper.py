# Created by 青云 on 2026/10/21.
"""Match Day 数据抓取：足球 / 篮球 / 综合赛事（直播吧）+ 电竞（Liquipedia），ESPN 兜底。只用标准库。

输出：
- data.json：未来 14 天 + 过去 3 天的统一时间线 events，各联赛整季赛程 leagues，来源状态 sources
- china.ics / football.ics / basketball.ics / esports.ics：可订阅日历，开赛前 30 分钟提醒
- logos/：队徽 / 赛事图标缓存（来源有防盗链，下载到仓库同源访问；按文件名去重，已有的不再下载）
"""
import gzip, hashlib, html, json, re, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data.json"
CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
LIQ_UA = "MatchDay/1.0 (https://github.com/climb0516/match-day)"   # Liquipedia 要求自定义 UA
PAST_DAYS, FUTURE_DAYS = 3, 14
LOGO_DIR = ROOT / "logos"
SITE = "https://climb0516.github.io/match-day/"

# 联赛：名称、项目、直播吧 type、ESPN 兜底路径
LEAGUES = [
    ("中超", "football", "football", "soccer/chn.1"),
    ("英超", "football", "football", "soccer/eng.1"),
    ("西甲", "football", "football", "soccer/esp.1"),
    ("意甲", "football", "football", "soccer/ita.1"),
    ("德甲", "football", "football", "soccer/ger.1"),
    ("法甲", "football", "football", "soccer/fra.1"),
    ("欧冠", "football", "football", "soccer/uefa.champions"),
    ("亚洲杯", "football", "football", "soccer/afc.asian.cup"),
    ("NBA", "basketball", "basketball", "basketball/nba"),
    ("CBA", "basketball", "basketball", None),
]

# 电竞：Liquipedia wiki、中文名、重点赛事关键词、中国战队（Liquipedia 全名）
GAMES = [
    ("leagueoflegends", "英雄联盟", r"\b(Worlds|MSI|LPL|LCK|LEC|LTA)\b(?! (AS|CL|Academy))|Esports World Cup",
     {"Bilibili Gaming", "JD Gaming", "Top Esports", "Anyone's Legend", "Weibo Gaming", "Invictus Gaming",
      "LNG Esports", "EDward Gaming", "Team WE", "Oh My God", "Ultra Prime", "LGD Gaming", "ThunderTalk Gaming",
      "Ninjas in Pyjamas.CN"}),
    ("honorofkings", "王者荣耀", r"\bKPL\b|World Cup|Esports World Cup|HoK Championship|Honor of Kings World", set()),
    ("valorant", "无畏契约", r"\bVCT\b|Champions|Masters|Esports World Cup",
     {"EDward Gaming", "Trace Esports", "Bilibili Gaming", "Dragon Ranger Gaming", "FunPlus Phoenix", "JDG Esports",
      "TYLOO", "Nova Esports", "All Gamers", "Wolves Esports", "Xi Lai Gaming", "Titan Esports Club"}),
    ("counterstrike", "CS2", r"Major|\bIEM\b|BLAST|PGL|ESL Pro League|Esports World Cup",
     {"TYLOO", "Rare Atom", "Lynn Vision Gaming", "The MongolZ.CN"}),
    ("dota2", "DOTA2", r"The International|DreamLeague|PGL|BLAST|ESL One|Riyadh|Esports World Cup|FISSURE",
     {"Xtreme Gaming", "Azure Ray", "Team Zero", "Yakult Brothers", "Team Tidebound"}),
]
CN_ESPORTS_LEAGUE = re.compile(r"\b(LPL|KPL|China|CN)\b")
CN_TEXT = re.compile(r"中国|国足|国奥|国乒|国羽|女足|男篮|女篮")

# 直播吧首页链接里不是频道的文字
NOT_TV = {"文字", "手机看直播", "比分", "动画", "互动直播", "直播吧视频直播", "视频直播", "集锦", "录像", "展开"}
# 综合项目在首页会把项目名填进对阵位置（如「环贵州 vs 自行车」），遇到这些词就当作没有对阵
SPORT_WORDS = {"自行车", "游泳", "体操", "田径", "乒乓球", "羽毛球", "网球", "斯诺克", "击剑", "排球", "赛车", "F1",
               "马拉松", "高尔夫", "拳击", "台球", "跳水", "举重", "射击", "滑冰", "冰壶", "滑雪", "综合", "其他"}


def fetch(url, timeout=20, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "ignore")


def fetch_bytes(url, timeout=10, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if not r.headers.get("Content-Type", "").startswith("image/"):
            raise ValueError("not image")
        return r.read()


# ── 队徽缓存 ──────────────────────────────────────────────
# 直播吧（duihui.duoduocdn.com）、Liquipedia 图片都校验 Referer，不能外链 → 下载到 logos/，页面同源引用。
# 直播吧走阿里云 OSS 缩放成 64px 宽；Liquipedia 用页面自带的约 50px 高缩略图，单线程、每张间隔 1 秒。
# 下载在后台线程里跑（与 Liquipedia 的 31 秒限流等待重叠），总时长有上限，没下完的下次运行再补。
class Logos:
    def __init__(self):
        self.pools = {"zb": ThreadPoolExecutor(6), "lq": ThreadPoolExecutor(1)}
        self.futs, self.lock, self.stop = {}, threading.Lock(), False
        self.new = self.fail = 0

    @staticmethod
    def plan(url):
        """远程 URL → (仓库相对路径, 实际下载地址, 线程池)；不认识的来源返回 None"""
        u = urllib.parse.urlparse(url if "://" in url else "https:" + url if url.startswith("//") else
                                  "https://liquipedia.net" + url)
        safe = lambda x: re.sub(r"[^A-Za-z0-9._-]", "_", urllib.parse.unquote(x))[-80:]
        if u.netloc.endswith("duoduocdn.com"):
            name = safe(u.path.strip("/").replace("/", "_"))
            return f"logos/zb/{name}", f"https://{u.netloc}{u.path}?x-oss-process=image/resize,w_64", "zb"
        if u.netloc.endswith("espncdn.com"):
            path = re.sub(r"^/combiner/i$", "", u.path) or urllib.parse.parse_qs(u.query).get("img", [""])[0]
            name = safe(path.strip("/").replace("/i/", "").replace("/", "_"))
            return f"logos/espn/{name}", f"https://a.espncdn.com/combiner/i?img={path}&h=64&w=64", "zb"
        if u.netloc == "liquipedia.net" and "/commons/images/" in u.path:
            base = re.sub(r"^\d+px-", "", u.path.rsplit("/", 1)[-1])
            name = safe(base) if re.fullmatch(r"[A-Za-z0-9._-]+", base) else \
                hashlib.md5(base.encode()).hexdigest()[:12] + Path(base).suffix
            return f"logos/lq/{name}", f"https://liquipedia.net{u.path}", "lq"
        return None

    def want(self, url):
        """登记一个远程图片，返回将来的相对路径（下载失败的在 finish 后由 exists 过滤掉）"""
        if not url:
            return None
        try:
            p = self.plan(url)
        except Exception:
            return None
        if not p:
            return None
        rel, src, pool = p
        with self.lock:
            if rel not in self.futs and not (ROOT / rel).exists():
                self.futs[rel] = self.pools[pool].submit(self._get, rel, src, pool)
        return rel

    def _get(self, rel, src, pool):
        if self.stop:
            return
        try:
            data = fetch_bytes(src, headers={"User-Agent": LIQ_UA} if pool == "lq" else None)
            if len(data) < 200:
                raise ValueError("too small")
            f = ROOT / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(f.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(f)
            self.new += 1
        except Exception:
            self.fail += 1
        if pool == "lq":
            time.sleep(0.3)

    def finish(self, budget):
        """最多再等 budget 秒，剩下的放弃（下次运行再补）"""
        wait(list(self.futs.values()), timeout=budget)
        self.stop = True
        for p in self.pools.values():
            p.shutdown(wait=True, cancel_futures=True)
        pending = sum(not (ROOT / r).exists() for r in self.futs) - self.fail
        return {"new": self.new, "fail": self.fail, "skipped": max(pending, 0)}

    @staticmethod
    def ok(rel):
        return rel if rel and (ROOT / rel).exists() else None


def text(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).split()


def iso(ts):
    return datetime.fromtimestamp(ts, CST).isoformat(timespec="minutes")


# ── 直播吧 联赛整季赛程 ─────────────────────────────────
def qmb_league(name, typ):
    q = urllib.parse.urlencode({"_url": "/data/index", "league": name, "tab": "赛程", "type": typ})
    d = json.loads(fetch("https://dc.qiumibao.com/shuju/public/index.php?" + q))
    groups = []
    for g in d.get("data") or []:
        ms = []
        for m in g.get("list", []):
            ts = int(m.get("timestamp") or 0)
            if not ts:
                continue
            score = m.get("比分") or ""
            fin = bool(m.get("is_finish"))
            ms.append({"id": "zb" + str(m["saishi_id"]), "ts": ts, "home": m.get("主队名称") or m.get("主队"),
                       "away": m.get("客队名称") or m.get("客队"), "score": score if fin and re.search(r"\d", score) else "",
                       "fin": fin, "stage": m.get("赛事", ""), "_hl": m.get("主队图标"), "_al": m.get("客队图标")})
        if ms:
            groups.append({"title": g.get("title", ""), "matches": ms})
    if not groups:
        raise ValueError("empty")
    return groups


# ── ESPN 兜底：最近 PAST_DAYS ~ 未来 7 天，逐天请求 ──────────
def espn_league(path, today):
    ms = []
    for i in range(-PAST_DAYS, 8):
        day = (today + timedelta(days=i)).strftime("%Y%m%d")
        d = json.loads(fetch(f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates={day}"))
        for ev in d.get("events", []):
            comp = ev["competitions"][0]
            teams = {c["homeAway"]: c for c in comp["competitors"]}
            fin = ev["status"]["type"].get("completed", False)
            ts = int(datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).timestamp())
            ms.append({"id": "espn" + ev["id"], "ts": ts, "home": teams["home"]["team"]["displayName"],
                       "away": teams["away"]["team"]["displayName"],
                       "score": f'{teams["home"].get("score", "")}-{teams["away"].get("score", "")}' if fin else "",
                       "fin": fin, "stage": "", "_hl": teams["home"]["team"].get("logo"), "_al": teams["away"]["team"].get("logo")})
    if not ms:
        raise ValueError("empty")
    return [{"title": "近期（ESPN）", "matches": sorted(ms, key=lambda m: m["ts"])}]


# ── 直播吧 首页：未来 2~3 天全部赛事（含转播频道、综合项目）──
def zhibo8_home():
    t = fetch("https://www.zhibo8.com/")
    out = []
    for m in re.finditer(r'<li label="([^"]*)" id="saishi(\d+)" data-time="([^"]+)"[^>]*data-type="([^"]*)"[^>]*>(.*?)</li>', t, re.S):
        label, sid, when, typ, body = m.groups()
        ts = int(datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=CST).timestamp())
        league = " ".join(text((re.search(r'class="_league">(.*?)</span>', body) or [None, ""])[1]))
        tm = re.search(r'class="_teams">(.*?)</span></b>', body, re.S)
        home = away = hl = al = ""
        if tm:
            sides = re.split(r"<span>\s*-\s*</span>", tm.group(1))
            if len(sides) == 2:
                home, away = " ".join(text(sides[0])), " ".join(text(sides[1]))
                img = lambda x: (re.search(r'<img[^>]+src="([^"]+)"', x) or [None, ""])[1]
                hl, al = img(sides[0]), img(sides[1])
        # 比赛页：「文字」等链接指向 /zhibo/<类>/<年>/match<id>….htm，去掉 ?redirect 等参数
        page = re.search(r'href="((?:https?:)?(?://www\.zhibo8\.com)?/zhibo/[^"?]*match' + sid + r'[^"?]*\.htm)', body)
        url = urllib.parse.urljoin("https://www.zhibo8.com/", page.group(1)) if page else ""
        tv = []
        for a in re.findall(r"<a [^>]*>(.*?)</a>", body):
            tv += [x for x in text(a) if x not in NOT_TV]
        out.append({"id": "zb" + sid, "ts": ts, "type": typ, "labels": label.split(","), "league": league,
                    "home": home, "away": away, "hl": hl, "al": al, "tv": list(dict.fromkeys(tv))[:4], "url": url})
    if not out:
        raise ValueError("empty")
    return out


# ── Liquipedia：每个游戏的 Liquipedia:Matches（即将开始 50 + 最近结束 50）──
def liquipedia(wiki):
    url = f"https://liquipedia.net/{wiki}/api.php?action=parse&page=Liquipedia:Matches&format=json&prop=text"
    t = json.loads(fetch(url, timeout=30, headers={"User-Agent": LIQ_UA}))["parse"]["text"]["*"]
    out = []
    for blk in t.split('<div class="match-info">')[1:]:
        ts = re.search(r'data-timestamp="(\d+)"', blk)
        if not ts:
            continue
        teams = re.findall(r'<span class="name"[^>]*>(?:<a [^>]*title="([^"]*)"[^>]*>)?([^<]*)', blk)
        if len(teams) < 2:
            teams = (teams + [("", "TBD"), ("", "TBD")])[:2]
        scores = re.findall(r'scoreholder-score[^"]*">([^<]*)', blk)
        bo = re.search(r"\(Bo(\d+)\)", blk)
        tour = re.search(r'match-info-tournament-name"><a [^>]*><span>([^<]+)', blk)
        tour_href = re.search(r'match-info-tournament-name"><a href="(/[^"#]+)', blk)   # 赛事页（去掉 #日期锚点）
        head, _, tail = blk.partition('<div class="match-info-tournament">')
        opp = [lq_imgs(x) for x in head.split('<div class="match-info-header-opponent')[1:3]]
        opp = (opp + [(None, None)] * 2)[:2]
        tl = lq_imgs(tail.split('match-info-tournament-name')[0])
        out.append({"hl": opp[0][0], "hld": opp[0][1], "al": opp[1][0], "ald": opp[1][1], "tl": tl[0], "tld": tl[1],
                    "ts": int(ts.group(1)), "home": html.unescape(teams[0][1] or "TBD"), "away": html.unescape(teams[1][1] or "TBD"),
                    "homeFull": html.unescape(teams[0][0]).replace(" (page does not exist)", ""),
                    "awayFull": html.unescape(teams[1][0]).replace(" (page does not exist)", ""),
                    "score": ":".join(scores[:2]) if len(scores) >= 2 and all(s.strip() for s in scores[:2]) else "",
                    "bo": int(bo.group(1)) if bo else None, "tour": html.unescape(tour.group(1)) if tour else "",
                    "url": "https://liquipedia.net" + html.unescape(tour_href.group(1)) if tour_href else ""})
    if not out:
        raise ValueError("empty")
    # 同一场比赛会同时出现在两个区（倒计时 / 已结束），按时间 + 对阵去重，优先带比分的
    uniq = {}
    for m in out:
        k = (m["ts"], m["home"], m["away"])
        if k not in uniq or m["score"]:
            uniq[k] = m
    return sorted(uniq.values(), key=lambda m: m["ts"])


def lq_imgs(chunk):
    """Liquipedia 图标：(浅色/通用, 深色)；只有通用版时深色为 None"""
    light = dark = None
    for cls, src in re.findall(r'<span class="(?:team-template-image-icon|league-icon-small-image)([^"]*)">.*?<img [^>]*src="([^"]+)"', chunk, re.S):
        if "darkmode" in cls:
            dark = dark or src
        else:
            light = light or src
    return light, dark


# ── ICS ────────────────────────────────────────────────
def ics(events, name):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    esc = lambda s: s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//match-day//CN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{esc(name)}", "X-WR-TIMEZONE:Asia/Shanghai", "REFRESH-INTERVAL;VALUE=DURATION:PT6H"]
    for e in events:
        if e["status"] == "post":
            continue
        start = datetime.fromtimestamp(e["ts"], timezone.utc)
        dur = {"football": 2, "basketball": 2.5}.get(e["sport"], (e.get("bo") or 1) * 0.8 + 0.2)
        title = f'{e["home"]} vs {e["away"]}' if e["home"] and e["away"] else e["league"]
        desc = " · ".join(x for x in [e["league"], e.get("stage", ""), "转播：" + " ".join(e["tv"]) if e.get("tv") else ""] if x)
        lines += ["BEGIN:VEVENT", f'UID:{e["id"]}@match-day', f"DTSTAMP:{stamp}",
                  f'DTSTART:{start.strftime("%Y%m%dT%H%M%SZ")}',
                  f'DTEND:{(start + timedelta(hours=dur)).strftime("%Y%m%dT%H%M%SZ")}',
                  f'SUMMARY:{esc(e["icon"] + " " + title)}', f"DESCRIPTION:{esc(desc)}",
                  "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT30M", f'DESCRIPTION:{esc(title)} 30 分钟后开始', "END:VALARM",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main():
    now = datetime.now(CST)
    stamp = now.isoformat(timespec="seconds")
    nowts = now.timestamp()
    lo, hi = nowts - PAST_DAYS * 86400, nowts + FUTURE_DAYS * 86400
    old = json.loads(DATA_FILE.read_text("utf-8")) if DATA_FILE.exists() else {}
    sources = []

    def run(name, fn, count=len):
        try:
            v = fn()
            sources.append({"name": name, "ok": True, "count": count(v), "updated": stamp})
            return v
        except Exception as e:
            prev = next((s for s in old.get("sources", []) if s["name"] == name), {})
            sources.append({"name": name, "ok": False, "error": str(e)[:120], "updated": prev.get("updated")})
            print("FAIL", name, e)
            return None

    def status(ts, fin, hours):
        if fin:
            return "post"
        return "live" if ts <= nowts < ts + hours * 3600 else ("pre" if ts > nowts else "post")

    events, leagues = [], {}
    logos = Logos()
    nmatches = lambda gs: sum(len(g["matches"]) for g in gs)

    # 1. 联赛：直播吧优先，失败用 ESPN，再失败沿用旧数据
    for name, sport, typ, espn in LEAGUES:
        groups, src = run(f"直播吧 {name}", lambda: qmb_league(name, typ), nmatches), "直播吧"
        if groups is None and espn:
            groups, src = run(f"ESPN {name}", lambda: espn_league(espn, now.date()), nmatches), "ESPN"
        if groups is None:
            prev = old.get("leagues", {}).get(name)
            if not prev:
                continue
            groups, src = prev["groups"], prev["src"] + "（旧数据）"
        # 队名 → 队徽（本联赛范围）；旧数据没有图标 URL，沿用上次的映射
        team_logo = dict((old.get("leagues", {}).get(name) or {}).get("logos") or {})
        for g in groups:
            for m in g["matches"]:
                for side, k in (("home", "_hl"), ("away", "_al")):
                    rel = logos.want(m.pop(k, None))
                    if rel and m[side]:
                        team_logo[m[side]] = rel
        leagues[name] = {"sport": sport, "src": src, "groups": groups, "logos": team_logo}
        hours = 2 if sport == "football" else 2.5
        for g in groups:
            for m in g["matches"]:
                if lo <= m["ts"] <= hi:
                    events.append({"id": m["id"], "ts": m["ts"], "t": iso(m["ts"]), "sport": sport, "league": name,
                                   "stage": m.get("stage") or ("" if re.match(r"\d{4}-\d{2}-\d{2}", g["title"]) else g["title"]), "home": m["home"], "away": m["away"],
                                   "homeLogo": team_logo.get(m["home"]), "awayLogo": team_logo.get(m["away"]),
                                   "score": m["score"], "status": status(m["ts"], m["fin"], hours), "tv": [],
                                   "china": bool(CN_TEXT.search(m["home"] + m["away"]))})

    # 2. 直播吧首页：给联赛比赛补转播频道；联赛以外的（杯赛、综合项目等）单独加入
    home = run("直播吧 首页", zhibo8_home)
    if home:
        by_key = {}
        for e in events:
            by_key.setdefault(datetime.fromtimestamp(e["ts"], CST).date(), []).append(e)
        for h in home:
            day = datetime.fromtimestamp(h["ts"], CST).date()
            hit = next((e for e in by_key.get(day, []) if h["home"] and h["away"] and
                        {h["home"], h["away"]} == {e["home"], e["away"]}), None)
            if hit:
                hit["tv"] = h["tv"]
                if h["url"]:
                    hit["url"] = h["url"]
                side = {h["home"]: h["hl"], h["away"]: h["al"]}
                hit["homeLogo"] = hit.get("homeLogo") or logos.want(side.get(hit["home"]))
                hit["awayLogo"] = hit.get("awayLogo") or logos.want(side.get(hit["away"]))
                continue
            sport = {"football": "football", "basketball": "basketball"}.get(h["type"], "other")
            if "电竞" in h["labels"]:
                continue                                    # 电竞以 Liquipedia 为准
            if h["home"] == h["away"] or {h["home"], h["away"]} & SPORT_WORDS:
                h["home"] = h["away"] = h["hl"] = h["al"] = ""
            events.append({"id": h["id"], "ts": h["ts"], "t": iso(h["ts"]), "sport": sport,
                           "league": h["labels"][0] if sport == "other" and h["labels"] else h["league"],
                           "stage": h["league"], "home": h["home"], "away": h["away"],
                           "homeLogo": logos.want(h["hl"]), "awayLogo": logos.want(h["al"]), "score": "",
                           "status": status(h["ts"], False, 2.5), "tv": h["tv"], "url": h["url"],
                           "china": bool(CN_TEXT.search(" ".join(h["labels"]) + h["home"] + h["away"]))})

    # 3. 电竞：Liquipedia 限流（parse 请求间隔 ≥ 30 秒）
    prev_es = [e for e in old.get("events", []) if e["sport"] == "esports"]
    LQ_KEYS = {"hl": "homeLogo", "al": "awayLogo", "hld": "homeLogoDark", "ald": "awayLogoDark",
               "tl": "leagueLogo", "tld": "leagueLogoDark"}

    def queue_es_logos(evs):
        """电竞图标排队下载（后台线程，与下一次 31 秒限流等待重叠）：先未开始的比赛、先队徽后赛事图标 / 深色版"""
        for keys in (("hl", "al"), ("hld", "ald", "tl", "tld")):
            for e in sorted(evs, key=lambda e: (e["status"] == "post", e["ts"])):
                for k in keys:
                    e[LQ_KEYS[k]] = logos.want(e["_m"][k])
        for e in evs:
            e.pop("_m")

    for i, (wiki, cn, key, cn_teams) in enumerate(GAMES):
        if i:
            time.sleep(31)
        ms = run(f"Liquipedia {cn}", lambda: liquipedia(wiki))
        if ms is None:
            events += [e for e in prev_es if e.get("game") == cn and lo <= e["ts"] <= hi]
            continue
        n0 = len(events)
        for m in ms:
            if not lo <= m["ts"] <= hi:
                continue
            china = bool(CN_ESPORTS_LEAGUE.search(m["tour"]) or {m["homeFull"], m["awayFull"]} & cn_teams
                         or (wiki == "honorofkings" and "KPL" in m["tour"]))
            events.append({"id": f'lq-{wiki}-{m["ts"]}-{re.sub(r"[^A-Za-z0-9]", "", m["home"] + m["away"])}',
                           "ts": m["ts"], "t": iso(m["ts"]), "sport": "esports", "game": cn, "league": m["tour"],
                           "stage": f'Bo{m["bo"]}' if m["bo"] else "", "home": m["home"], "away": m["away"],
                           "homeFull": m["homeFull"], "awayFull": m["awayFull"], "_m": m, "score": m["score"],
                           "status": status(m["ts"], bool(m["score"]), (m["bo"] or 1) * 0.8 + 0.3),
                           "bo": m["bo"], "tv": [], "url": m["url"], "major": bool(re.search(key, m["tour"])), "china": china})
        queue_es_logos(events[n0:])

    # 队徽：电竞阶段已经和限流等待重叠下载了大半，这里最多再等 30 秒，剩下的下次运行补
    stat = logos.finish(30)
    for e in events:
        for k in ("homeLogo", "awayLogo", "homeLogoDark", "awayLogoDark", "leagueLogo", "leagueLogoDark"):
            v = Logos.ok(e.get(k))
            if v and not (k.endswith("Dark") and v == e.get(k[:-4])):
                e[k] = v
            else:
                e.pop(k, None)
    for L in leagues.values():
        L["logos"] = {t: r for t, r in L["logos"].items() if Logos.ok(r)}

    events.sort(key=lambda e: (e["ts"], e["league"]))
    icon = {"football": "⚽", "basketball": "🏀", "esports": "🎮", "other": "🏅"}
    for e in events:
        e["icon"] = icon[e["sport"]]

    # 可订阅日历：只放未来 14 天、未结束的比赛
    feeds = {
        "china.ics": ("Match Day · 中国", [e for e in events if e["china"]]),
        "football.ics": ("Match Day · 足球", [e for e in events if e["sport"] == "football"]),
        "basketball.ics": ("Match Day · 篮球", [e for e in events if e["sport"] == "basketball"]),
        "esports.ics": ("Match Day · 电竞重点赛事", [e for e in events if e["sport"] == "esports" and (e["major"] or e["china"])]),
    }
    for fn, (title, evs) in feeds.items():
        (ROOT / fn).write_text(ics(evs, title), "utf-8")

    data = {
        "updated": stamp,               # 每次都变 = 心跳，保证每次运行都有提交
        "logoBase": SITE,               # events[].homeLogo 等是相对路径，站外使用时拼上这个前缀
        "events": events,
        "leagues": leagues,
        "feeds": {fn: {"title": t, "count": sum(e["status"] != "post" for e in evs)} for fn, (t, evs) in feeds.items()},
        "sources": sources,
    }
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    from collections import Counter
    print("events", len(events), Counter(e["sport"] for e in events), "china", sum(e["china"] for e in events))
    print("leagues", {k: (v["src"], nmatches(v["groups"])) for k, v in leagues.items()})
    print("failed", [s["name"] for s in sources if not s["ok"]])
    has = lambda e: bool(e.get("homeLogo") and e.get("awayLogo"))
    print("logos", stat, {sp: f'{sum(has(e) for e in events if e["sport"] == sp and e["home"])}/{sum(1 for e in events if e["sport"] == sp and e["home"])}'
                          for sp in ("football", "basketball", "esports", "other")})


if __name__ == "__main__":
    main()
