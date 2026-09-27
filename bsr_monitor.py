# -*- coding: utf-8 -*-
"""
亚马逊 BSR TOP100 店铺监控台
================================================
纯本地运行：Python 标准库实现（尽量零第三方依赖）
- 抓取亚马逊美国站便携蓝牙音箱 BSR TOP100（第1、2页）
- 识别目标店铺上榜商品（店铺名称 / 关键词 / 主页 marker 均可在界面⑤设置中配置）
- 本地持久化快照 + 变动对比（新上榜 / 落榜）
- AI 分析（Ollama / OpenAI 兼容 API / 离线模板；AI 分析报告与运营建议的提示词均可在界面⑤设置中编辑）
- 目标店铺运营销售建议（内置规则引擎 + 可选 LLM 增强；顶部「生成 AI 分析」可用当前提示词重跑）

启动：python bsr_monitor.py  （浏览器自动打开 http://127.0.0.1:8965）
打包：build_exe.bat  →  BSRMonitor.exe

日期：2026-09-08
"""

import os
import sys
import json
import re
import csv
import time
import gzip
import random
import html as html_mod
import threading
import webbrowser
import socket
import argparse
import io
from datetime import datetime
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as url_request
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------- 基础路径
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(BASE_DIR, "data")
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")
CHG_DIR = os.path.join(DATA_DIR, "changes")
REP_DIR = os.path.join(DATA_DIR, "reports")
RAW_DIR = os.path.join(DATA_DIR, "raw")
EXP_DIR = os.path.join(DATA_DIR, "exports")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")

# ---------------------------------------------------------------- 可编辑提示词（WebUI ⑤设置 →「提示词设置」可修改）
# 说明：以下为内置默认提示词；data/config.json 中填写了 prompt_analysis / prompt_advice 时优先使用用户内容。
# 提示词支持 {占位符}，生成时按本次真实数据替换；容错：未引用任何数据占位符时自动在末尾追加完整数据块。
PROMPT_PLACEHOLDER_HELP = [
    ("{store_label}", "目标店铺名"),
    ("{gen_at}", "报告生成时刻"),
    ("{fetch_at}", "本次快照抓取时刻"),
    ("{summary_json}", "TOP100 画像统计 JSON（价格带 / 星级 / 评论数等）"),
    ("{top_items}", "榜单头部明细（分析 TOP20 / 建议 TOP15）"),
    ("{store_items}", "目标店铺上榜产品明细"),
    ("{new_in}", "本次新上榜明细"),
    ("{dropped}", "本次落榜明细"),
    ("{new_in_count}", "新上榜款数"),
    ("{dropped_count}", "落榜款数"),
    ("{data_block}", "以上全部数据块（统计 JSON + 全部明细，一次性插入）"),
]

PROMPT_ANALYSIS_DEFAULT = """当前生成时刻：{gen_at}；本次分析所依据的亚马逊 BSR TOP100 快照抓取时刻：{fetch_at}。请严格区分“数据抓取时刻”与“报告生成时刻”，引用日期只能使用快照抓取时刻所在日期，不得混用或自造日期。
你是一名亚马逊美国站销售顾问，正在做便携蓝牙音箱品类基于真实榜单数据的销售向分析。
本次分析的输出将作为 AI 报告的『总结 → 销售向数据分析』定性解读部分。注意：你的分析对象是榜单商品本身，不是任何单一店铺或品牌；不要围绕店铺撰写大段内容，更不要杜撰任何店铺经营数据。

## 本次 TOP100 快照的真实字段统计（JSON）
{summary_json}

## 本次真实变动 diff：新上榜（{new_in_count} 款）
{new_in}

## 本次真实变动 diff：落榜（{dropped_count} 款）
{dropped}

## 榜单头部 TOP20（真实抓取）
{top_items}

请输出一段面向销售顾问的销售向定性分析（Markdown 正文，无需标题、无需编号目录），基于上方 JSON 统计与 diff 数据，围绕以下角度展开：
1. 当前成交价格结构解读：哪个价格带是主战场、哪一端存在供给缺口或机会；结合真实统计给出定价/选品方向；
2. 口碑与流量结构：评分均值、评论数中位体现的竞争门槛，新进入者要追上头部需要的参考动作；
3. 榜单变动（新上榜/落榜）反映的短期趋势与风险；
4. 3-5 条可落地建议（定价、选品、Listing、评价、广告任一维度，必须由上方真实字段推导）。
硬性约束：
① 只能引用上方 JSON 统计与商品明细中真实存在的数字；上方未给出的统计值（如具体销量、销售额、某店铺占比、转化率、月销等）一律不得出现；
② 严禁围绕 {store_label} 等店铺名做成段专题论述（不得编造店铺 / 品牌经营数据）；若商品标题含店铺字样也只是商品名，不做店铺专题；
③ 数据缺失处如实写“该快照未覆盖”，不要补数；
④ 正文一律以中文为主，仅必要专名（ASIN/品牌名/型号/单位）可用英文，严禁整段或大段英文标题刷屏；
⑤ 涉及日期只能引用上面的快照抓取时刻/当前生成时刻，严禁自造或虚构日期；禁止出现“日期：”“XX团队”“报告结束”等落款与冗余收尾行；
⑥ 同一观点或段落严禁自我重复；输出控制在 600 字内。"""

PROMPT_ADVICE_DEFAULT = """当前生成时刻：{gen_at}；本次建议所依据的亚马逊 BSR TOP100 快照抓取时刻：{fetch_at}。请严格区分“数据抓取时刻”与“建议生成时刻”，引用日期只能使用快照抓取时刻所在日期，不得混用或自造日期。
你是资深亚马逊美国站运营专家，专注便携蓝牙音箱品类。下面给你当前 BSR TOP100 快照画像、目标店铺上榜明细、本次榜单变动，请输出一份可直接落地的运营销售建议。

## 当前 TOP100 画像
{summary_json}

## 头部竞品 TOP15
{top_items}

## 目标店铺上榜产品
{store_items}

## 新上榜商品（{new_in_count}）
{new_in}

## 落榜商品（{dropped_count}）
{dropped}

请输出以中文为主的运营建议（可中英结合，但英文仅限 ASIN/品牌/店铺名/型号/单位等必要专名，严禁整段或大段英文），至少覆盖：1) 价格与优惠策略（促销/定价区间/对标竞品参考）；2) 产品优化方向（功能/外观/卖点对标头部差异）；3) Listing 页面优化（标题/五点/A+/图片视频）；4) 评价管理（差评风险与评论维护）；5) 竞争应对（针对新上榜竞品）。要求具体可执行、引用真实榜单数据，不要空泛套话。
输出约束：仅依据上面提供的真实榜单数据，严禁编造不存在的 ASIN/价格/评论，数据缺失处如实说明；涉及日期时只能引用上面的快照抓取时刻/当前生成时刻，严禁自造或虚构日期；禁止出现“日期：”“XX团队”“报告结束”等落款与冗余收尾行；同一观点或段落严禁自我重复。正文语言一律以中文为主，仅必要专名（ASIN/品牌/店铺名/型号/单位等）可用英文，严禁大段或整段英文标题/描述刷屏。"""

_PROMPT_DATA_KEYS = ("summary_json", "top_items", "store_items", "new_in", "dropped", "data_block")


def _render_prompt_template(tpl, mapping):
    """按 {占位符} 渲染提示词；未识别的占位符原样保留（不报错）。"""
    out = tpl or ""
    for k in sorted(mapping.keys(), key=len, reverse=True):
        out = out.replace("{" + k + "}", str(mapping[k]))
    return out


def _prompt_has_data(tpl):
    """提示词模板中是否已引用数据类占位符。"""
    t = tpl or ""
    for k in _PROMPT_DATA_KEYS:
        if ("{" + k + "}") in t:
            return True
    return False


def _cfg_prompt(cfg, key, default):
    """取当前生效提示词；未配置 / 空值时回退内置默认。"""
    val = str((cfg or {}).get(key) or "").strip()
    return val or default


DEFAULT_CONFIG = {
    "store_name": "",                # 目标店铺显示名（看板标题 / 导出文件名 / 报告标题；留空则回退为关键词首项）
    "store_keywords": [],            # 店铺关键词（小写匹配；商品卡店铺名 / 链接 / 标题含任一关键词即判定上榜）
    "store_id": "",                  # 店铺页标识（兼容保留，留空即可）
    "store_page_marker": "",         # 店铺主页 marker（链接中含有的 store id；与关键词共同用于判定）
    "ai_mode": "offline",            # ollama | apikey | offline
    "ai_base_url": "http://127.0.0.1:11434/v1",
    "ai_model": "qwen2.5:7b",
    "ai_api_key": "",
    "prompt_analysis": PROMPT_ANALYSIS_DEFAULT,  # AI 分析报告提示词（⑤设置可编辑；支持 {占位符}）
    "prompt_advice": PROMPT_ADVICE_DEFAULT,      # 运营销售建议提示词（⑤设置可编辑；支持 {占位符}）
    "ai_max_tokens": 8192,           # LLM 单次最大生成长度（0/空=不传，交给服务端默认）
    "ai_num_ctx": 16384,             # 仅 Ollama(11434) 生效：注入 options.num_ctx 上下文窗口
    "scroll_page_timeout_sec": 75,   # 浏览器滚动抓取：单页硬超时护栏（秒）
    "scroll_overall_timeout_sec": 200,  # 浏览器滚动抓取：整体硬超时护栏（秒）
    "http_proxy": "",
    "https_proxy": "",
    "fetch_cooldown_seconds": 60,
    "schedule_minutes": 0,           # 0=关闭定时
    "min_schedule_minutes": 30,
    "fetch_mode": "auto",          # auto=浏览器滚动优先/失败回退HTTP | scroll=强制浏览器滚动 | http=纯HTTP直抓(最多约60款)
}

# 直接抓取美站（英文站）页面，价格即页面渲染的美元标价，不做任何汇率换算。
# 实测：去掉旧版 /-/zh 路径段后，amazon.com 返回英文站、价格为 $ 标价（CNY token 为 0）。
TOP100_PAGE_1 = ("https://www.amazon.com/Best-Sellers-/zgbs/electronics/172623/"
                 "ref=zg_bs_pg_1_electronics?_encoding=UTF8&pg=1")
TOP100_PAGE_2 = ("https://www.amazon.com/Best-Sellers-/zgbs/electronics/172623/"
                 "ref=zg_bs_pg_2_electronics?_encoding=UTF8&pg=2")
PAGES = [TOP100_PAGE_1, TOP100_PAGE_2]
HOME_URL = "https://www.amazon.com/"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 OPR/113.0.0.0",
]

LOCK = threading.RLock()
# 最近一次成功抓取的结果缓存（供界面直接展示）
STATE = {"last_fetch": None, "last_snapshot_id": None,
         "last_diff": None, "last_store_items": None, "busy": False,
         "busy_msg": "", "ai_ok": None, "last_error": None}

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def now_tag():
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def ensure_dirs():
    for d in (DATA_DIR, SNAP_DIR, CHG_DIR, REP_DIR, RAW_DIR, EXP_DIR):
        os.makedirs(d, exist_ok=True)

def collapse(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def fmt_min(ts):
    """把 'YYYY-MM-DD HH:MM:SS' 归一为 'YYYY-MM-DD HH:MM'（报告与前端统一格式）；无法识别时原样返回。"""
    s = collapse(str(ts or ""))
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", s)
    if m:
        return "%s %s" % (m.group(1), m.group(2))
    return s or "—"

def time_from_snapshot_id(sid):
    """从快照 ID / 文件名（top100_YYYYMMDD_HHMMSS）解析 'YYYY-MM-DD HH:MM'；解析失败返回 None。"""
    m = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", str(sid or ""))
    if not m:
        return None
    return "%s-%s-%s %s:%s" % (m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))

def snapshot_display_time(rec=None, sid=None):
    """快照展示时间（YYYY-MM-DD HH:MM）：优先 fetched_at，缺失时由 snapshot_id 内时间戳回填，不留空。"""
    if isinstance(rec, dict):
        sid = rec.get("snapshot_id") or sid
        val = rec.get("fetched_at")
    else:
        val = None
        if isinstance(rec, str) and rec:
            sid = rec
    out = fmt_min(val)
    if out and out != "—":
        return out
    return time_from_snapshot_id(sid) or "时间未知"

# ---------------------------------------------------------------- 配置读写 / 目标店铺
def _as_kw_list(raw):
    """把店铺关键词统一规范为去空、去重的小写字符串列表（兼容字符串 / 列表 / 逗号分隔）。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = re.split(r"[,，;；]+", raw)
    out = []
    for k in (raw or []):
        k = str(k or "").strip().lower()
        if k and k not in out:
            out.append(k)
    return out

def normalize_store_cfg(cfg):
    """归一化目标店铺配置并做旧配置兼容（旧字段自动迁移，不报错）。"""
    if not isinstance(cfg, dict):
        return cfg
    cfg["store_keywords"] = _as_kw_list(cfg.get("store_keywords"))
    marker = str(cfg.get("store_page_marker") or "").strip()
    legacy_id = str(cfg.get("store_id") or "").strip()
    if not marker and legacy_id:
        marker = legacy_id
    cfg["store_page_marker"] = marker
    cfg["store_id"] = marker
    if not str(cfg.get("store_name") or "").strip():
        cfg["store_name"] = cfg["store_keywords"][0] if cfg["store_keywords"] else ""
    return cfg

def _cfg_store_name(cfg):
    return str((cfg or {}).get("store_name") or "").strip()

def _store_label(cfg):
    """界面 / 报告用店铺名；未配置时给出友好占位，不报错。"""
    return _cfg_store_name(cfg) or "未设置目标店铺"

def _store_tag(cfg):
    """报告标题等短标签用店铺名。"""
    return _cfg_store_name(cfg) or "目标店铺"

def _store_configured(cfg):
    c = cfg or {}
    return bool(_cfg_store_name(c) or c.get("store_keywords") or c.get("store_page_marker"))

def _store_file_prefix(cfg):
    """导出文件名前缀（基于店铺名做安全化处理）。"""
    nm = _cfg_store_name(cfg) or "store"
    safe = re.sub(r"[\\/:*?\"<>|\s]+", "_", nm).strip("_")
    return safe or "store"

def _store_name_pattern(cfg):
    """构造店铺名 / 关键词的匹配正则片段；未配置返回 None。"""
    parts = []
    nm = _cfg_store_name(cfg)
    if nm:
        parts.append(re.escape(nm))
    for kw in ((cfg or {}).get("store_keywords") or []):
        if str(kw or "").strip():
            parts.append(re.escape(str(kw).strip()))
    if not parts:
        return None
    return "(?:" + "|".join(parts) + ")"

def load_config():
    ensure_dirs()
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    return normalize_store_cfg(cfg)

def save_config(cfg):
    ensure_dirs()
    prev = {}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                prev = json.load(f) or {}
    except Exception:
        prev = {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    # 通用保存（AI 配置 / 店铺设置等）未显式携带提示词时，保留磁盘上的现有值，避免被重置为内置默认
    for _pk in ("prompt_analysis", "prompt_advice"):
        if _pk not in (cfg or {}) and isinstance(prev, dict) and prev.get(_pk):
            merged[_pk] = prev[_pk]
    normalize_store_cfg(merged)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged

# ---------------------------------------------------------------- 工具函数
def _read_json(path, default=None):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def _write_json(path, obj):
    ensure_dirs()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def _to_float_price(raw):
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").replace("$", "").strip())
    except Exception:
        return None

def _to_int(raw):
    if raw is None:
        return None
    try:
        return int(str(raw).replace(",", "").strip())
    except Exception:
        return None

def _fmt_price(v, currency=None, kind=None):
    """格式化价格显示。currency: 'CNY' / 'USD'（默认 USD 兼容旧调用）；
    kind: 'current' 直接价 | 'from' 起价 | 'deal' 优惠价 | 'range' 价格区间 | None。"""
    if v is None:
        return None
    cur = (currency or "").upper()
    if cur in ("CNY", "CN¥", "RMB", "￥", "¥"):
        cur = "CNY"
    else:
        cur = "USD"
    prefix = {"from": "起价 ", "deal": "优惠价 ", "range": "区间 "}.get(kind or "", "")
    if cur == "CNY":
        return prefix + "CNY " + "%.2f" % v
    # 美元：按美站页面习惯带千分位（如 $1,267.71）
    return prefix + "$" + format(v, ",.2f")

def _log(msg):
    print("[%s] %s" % (now_str(), msg), flush=True)

# ---------------------------------------------------------------- HTTP 抓取
class AmazonFetchError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind   # network | http | captcha | blocked | empty

def _decompress(raw, headers):
    enc = (headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        try:
            return gzip.decompress(raw).decode("utf-8", errors="replace")
        except Exception:
            return raw.decode("utf-8", errors="replace")
    if enc == "deflate":
        try:
            return zlib.decompress(raw).decode("utf-8", errors="replace")
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")

def _build_opener(cfg):
    proxies = {}
    if cfg.get("http_proxy"):
        proxies["http"] = cfg["http_proxy"]
    if cfg.get("https_proxy"):
        proxies["https"] = cfg["https_proxy"]
    handlers = []
    if proxies:
        handlers.append(url_request.ProxyHandler(proxies))
    handlers.append(url_request.HTTPCookieProcessor(CookieJar()))
    return url_request.build_opener(*handlers)

def _random_headers(referer=None):
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        # 强制美元展示（i18n-prefs=USD）+ 英文站界面语言，确保页面直接渲染 $ 标价
        "Cookie": "i18n-prefs=USD; lc-main=en_US",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not.A/Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    if referer:
        headers["Referer"] = referer
    return headers

_BLOCK_MARKERS = [
    "Enter the characters you see below", "Type the characters you see in this image",
    "To discuss automated access to Amazon data please contact",
    "api-services-support@amazon.com", "Robot Check", "validateCaptcha",
    "captcha", "Sorry, we just need to make sure you're not a robot",
]

def http_get_text(url, opener, cfg, timeout=30, retries=1):
    """返回 (text, final_url)；失败抛 AmazonFetchError。"""
    last_err = None
    for attempt in range(retries + 1):
        try:
            headers = _random_headers()
            req = url_request.Request(url, headers=headers)
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                text = _decompress(raw, resp.headers)
                final_url = resp.geturl()
            low = text.lower()
            if any(m.lower() in low for m in _BLOCK_MARKERS):
                raise AmazonFetchError("captcha",
                                       "疑似触发亚马逊人机验证/反爬（captcha 或 Robot Check）。"
                                       "请等待 10-30 分钟后再试，或到设置中配置代理。")
            if len(text) < 5000:
                raise AmazonFetchError("blocked",
                                       "返回页面过短，可能被拦截或页面结构变化。")
            return text, final_url
        except AmazonFetchError:
            raise
        except (HTTPError, URLError, socket.timeout, OSError) as e:
            last_err = e
            wait = random.uniform(8, 20) * (attempt + 1)
            _log("抓取失败(%s)，%s 秒后重试(%d/%d)" % (e, int(wait), attempt + 1, retries))
            time.sleep(wait)
    raise AmazonFetchError("network", "网络请求失败: %s" % last_err)

# ---------------------------------------------------------------- HTML 解析器
class BSRParser(HTMLParser):
    """从 BSR 榜单页提取商品卡。兼容新版 gridItemRoot / 旧版 zg-item-immersion。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.products = []
        self.cur = None
        self.in_script = 0
        self.in_style = 0
        self.in_a = False
        self.a_href = None
        self.a_text = []
        self.buffer = []          # 当前块内全部可见文本（用于店铺名/关键词）
        self.title_candidates = []  # (text, href)
        self.img_alts = []
        self.offscreen_prices = []
        self.icon_alts = []
        self.store_links = []
        self.rating_hints = []
        self.cur_sponsored = False

    def handle_starttag(self, tag, attrs):
        d = dict(attrs or [])
        if tag in ("script", "style"):
            if tag == "script":
                self.in_script += 1
            else:
                self.in_style += 1
            return
        if self.in_script or self.in_style:
            return
        if "data-asin" in d and d.get("data-asin"):
            self._flush_block()
            self.cur = {"asin": d["data-asin"], "raw_rank": None}
            self.buffer = []
            self.title_candidates = []
            self.img_alts = []
            self.offscreen_prices = []
            self.icon_alts = []
            self.rating_hints = []
            self.store_links = []
            self.cur_sponsored = False
        if self.cur is None:
            return
        cls = (d.get("class") or "")
        if tag == "a":
            href = d.get("href") or ""
            if "/dp/" in href or "dp%2F" in href:
                self.in_a = True
                self.a_href = href
                self.a_text = []
            if "stores/page" in href or "/sp?seller" in href or "seller=" in href:
                self.store_links.append(href)
            al = d.get("aria-label") or ""
            if "Sponsored" in al or "广告" in al:
                self.cur_sponsored = True
            if "ratings" in al.lower() or "评价" in al or "评分" in al or "颗星" in al:
                m1 = re.search(r"([\d.]+)\s*out of\s*5 stars?[^\d]*([\d,]+)\s*ratings?", al, re.I)
                m2 = re.search(r"([\d.]+)\s*星[^\d]*([\d,]+)\s*条?(?:评价|评分)?", al)
                m3 = re.search(r"([\d.]+)\s*颗星[^\d]*5\s*颗星[^\d]*([\d,]+)", al)
                m = m1 or m3 or m2
                if m:
                    self.rating_hints.append((m.group(1), m.group(2)))
        elif tag == "span":
            if "a-offscreen" in cls or "p13n-sc-price" in cls or "_cDEzb_p13n-sc-price" in cls:
                self.offscreen_prices.append("")
        elif tag == "img":
            alt = d.get("alt") or ""
            if alt and len(alt) > 8:
                self.img_alts.append(alt)
            if "Sponsored" in alt or "广告" in alt:
                self.cur_sponsored = True
        elif tag == "span" and ("a-icon-alt" in cls or "a-icon" in cls):
            self.icon_alts.append("")

    def handle_startendtag(self, tag, attrs):
        d = dict(attrs or [])
        if tag == "img" and self.cur is not None:
            alt = d.get("alt") or ""
            if alt and len(alt) > 8:
                self.img_alts.append(alt)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = max(0, self.in_script - 1)
            return
        if tag == "style":
            self.in_style = max(0, self.in_style - 1)
            return
        if self.in_script or self.in_style:
            return
        if tag == "a" and self.in_a:
            txt = collapse("".join(self.a_text))
            if txt and len(txt) >= 6:
                self.title_candidates.append((txt, self.a_href or ""))
            self.in_a = False
            self.a_href = None
            self.a_text = []

    def handle_data(self, data):
        if self.in_script or self.in_style:
            return
        if self.cur is not None:
            if data and data.strip():
                self.buffer.append(data)
            if self.in_a:
                self.a_text.append(data)

    def _flush_block(self):
        if not self.cur:
            return
        block_text = collapse(" ".join(self.buffer))
        title = None
        href = None
        # 优先取 /dp/ 链接文本最长的作为标题
        cands = [c for c in self.title_candidates]
        if cands:
            cands.sort(key=lambda x: len(x[0]), reverse=True)
            title, href = cands[0]
        elif self.img_alts:
            self.img_alts.sort(key=len, reverse=True)
            title = self.img_alts[0]
        # 价格语义解析：货币 + 当前价/起价/优惠价/价格区间
        # （当前抓取美站，页面为 $ 美元标价；同时兼容历史中文站 CNY 快照）
        pr = _parse_price_from_text(block_text)
        price = pr["num_raw"] or None
        rating = None
        m = re.search(r"([\d.]+)\s*(?:out of 5|星(?:，|,|最高|\(|$))", block_text, re.I)
        if m:
            rating = m.group(1)
        m2 = re.search(r"([\d.]+)\s*out of\s*5", block_text, re.I)
        if m2:
            rating = m2.group(1)
        reviews = None
        m3 = re.search(r"([\d,]+)\s*(?:ratings|global ratings|global rating|条评价|条全球评价|条评分)", block_text, re.I)
        if m3:
            reviews = m3.group(1)
        # 中文站（/-/zh）星级/评论格式：“X 颗星，最多 5 颗星 Y”
        m_cn1 = re.search(r"([\d.]+)\s*颗星(?:，|,)?\s*最多\s*5\s*颗星[^\d]{0,6}([\d,]+)", block_text)
        if m_cn1:
            if rating is None:
                rating = m_cn1.group(1)
            if reviews is None:
                reviews = m_cn1.group(2)
        elif reviews is None:
            m_cn2 = re.search(r"最多\s*5\s*颗星[^\d]{0,6}([\d,]+)", block_text)
            if m_cn2:
                reviews = m_cn2.group(1)
        if rating is None:
            m_cn3 = re.search(r"([\d.]+)\s*颗星", block_text)
            if m_cn3 and "最多" not in block_text[max(0, m_cn3.start() - 6):m_cn3.start()]:
                rating = m_cn3.group(1)
        if self.rating_hints:
            hr, hn = self.rating_hints[0]
            if rating is None:
                rating = hr
            if reviews is None:
                reviews = hn
        # 店铺名：Visit the X Store / 访问 X 的店铺
        store_name = None
        m4 = re.search(r"Visit the\s+(.+?)\s+Store", block_text, re.I)
        if m4:
            store_name = collapse(m4.group(1))
        else:
            m5 = re.search(r"访问\s*(.{1,80}?)\s*的店铺", block_text)
            if m5:
                store_name = collapse(m5.group(1))
        badge = None
        mb = re.search(r"#\s?(\d{1,3})", block_text)
        if mb:
            badge = int(mb.group(1))
        prod = {
            "asin": self.cur["asin"],
            "raw_rank": badge,
            "title": title,
            "price_raw": price,
            "price": _to_float_price(price),
            "price_display": pr["raw"],
            "price_currency": pr["currency"],
            "price_kind": pr["kind"],
            "price_note": pr["note"],
            "rating": _to_float_price(rating),
            "reviews": _to_int(reviews),
            "url": self._full_url(href),
            "store_url": self.store_links[0] if self.store_links else None,
            "store_name": store_name,
            "sponsored": self.cur_sponsored,
            "text": block_text[:600],
        }
        # 排除明显非商品（无标题、无 dp 链接）
        if title and len(title) >= 8 and not prod["sponsored"]:
            self.products.append(prod)
        self.cur = None

    def _full_url(self, href):
        if not href:
            return None
        if href.startswith("http"):
            return href.split("?")[0]
        return "https://www.amazon.com" + href.split("?")[0]

    def close(self):
        self._flush_block()
        super().close()


def parse_page(html_text, offset=0):
    """解析单页商品，返回列表；offset 为跨页基准排名（第2页为50）。"""
    parser = BSRParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception as e:
        raise AmazonFetchError("parse", "页面解析异常: %s" % e)
    items = parser.products
    # 去重（同 ASIN 多次出现只保留第一次）
    seen = {}
    ordered = []
    for it in items:
        if it["asin"] in seen:
            continue
        seen[it["asin"]] = True
        ordered.append(it)
    # 排名：优先 badge；否则按顺序 offset+i
    out = []
    for i, it in enumerate(ordered):
        rank = it["raw_rank"] or (offset + i + 1)
        it["rank"] = rank
        out.append(it)
    return out


# ---------------------------------------------------------------- 中文站字段提取/历史快照回填
_ZH_PRICE_RE = re.compile(r"(?:CN¥|CNY|￥|¥)\s*([\d,]+(?:\.\d{1,2})?)")
# 美元标价：兼容 "$1,267.71"（千分位）、"$19.99" 以及 "US$"/"USD" 前缀写法
_US_PRICE_RE = re.compile(r"(?:(?:US\$|USD)\s?|(?<![A-Za-z])\$)\s?([\d,]+(?:\.\d{1,2})?)")
_ZH_RR_RE = re.compile(r"([\d.]+)\s*颗星(?:，|,)?\s*最多\s*5\s*颗星[^\d]{0,6}([\d,]+)")
_ZH_R2_RE = re.compile(r"最多\s*5\s*颗星[^\d]{0,6}([\d,]+)")
_ZH_R3_RE = re.compile(r"([\d.]+)\s*颗星")
_US_RATING_RE = re.compile(r"([\d.]+)\s*out of\s*5", re.I)
_US_REVIEWS_RE = re.compile(r"([\d,]+)\s*(?:ratings|global ratings|global rating)", re.I)
_ZH_STORE_RE = re.compile(r"访问\s*(.{1,80}?)\s*的店铺")
_US_STORE_RE = re.compile(r"Visit the\s+(.+?)\s+Store", re.I)

# 价格语义解析：货币符号 + 上下文。
# 中文站：“另有 N 种版本，价格 X 起”=起价；“N 个优惠，价格 X”=优惠价。
# 美站：“from $X” / “N offers from $X”=起价；“$X with N offers”=优惠价；
#        “$8.99 - $19.99”=价格区间（多版本商品）。
# 与上述语境无关的独立货币标价视为当前直接价（网页主价格区，优先级最高）。
_PRICE_CUR_RE = re.compile(
    r"(?P<cur>US\$|CN¥|CNY|USD|￥|¥|\$)\s*(?P<num>[\d,]+(?:\.\d{1,2})?)")
# 起价语境：中文“另有 N 种版本…价格 X 起” / 英文“from $X”“N offers from $X”
_PRICE_FROM_RE = re.compile(
    r"(?:另有\s*[\d,]+\s*种版本[^\n]{0,60}?价格|(?<![A-Za-z])(?:[\d,]+\s+offers?\s+)?from)\s*"
    r"(?P<cur>US\$|CN¥|CNY|USD|￥|¥|\$)\s*(?P<num>[\d,]+(?:\.\d{1,2})?)\s*(?P<qi>起)?",
    re.I)
# 优惠价语境：中文“N 个优惠，价格 X” / 英文“$X with N offers”
_PRICE_DEAL_RE = re.compile(
    r"(?:[\d,]+\s*个优惠|[\d,]+\s+优惠)[^\n]{0,60}?价格\s*"
    r"(?P<cur>US\$|CN¥|CNY|USD|￥|¥|\$)\s*(?P<num>[\d,]+(?:\.\d{1,2})?)"
    r"|(?P<cur2>US\$|\$)\s*(?P<num2>[\d,]+(?:\.\d{1,2})?)\s*with\s*[\d,]+\s*offers?",
    re.I)
# 价格区间：美站多版本商品常见 “$8.99 - $19.99”
_PRICE_RANGE_RE = re.compile(
    r"(?P<cur>US\$|\$)\s*(?P<lo>[\d,]+(?:\.\d{1,2})?)\s*(?:-|–|—|~|至)\s*"
    r"(?P<cur2>US\$|\$)\s*(?P<hi>[\d,]+(?:\.\d{1,2})?)")


def _cur_code(cur_raw):
    """货币符号/代码 → 标准货币代码：'$'/'US$'/'USD' → USD；'CN¥'/'CNY'/'￥'/'¥' → CNY。"""
    c = (cur_raw or "").strip().upper()
    if c in ("$", "US$", "USD"):
        return "USD"
    if c in ("CN¥", "CNY", "￥", "¥", "RMB"):
        return "CNY"
    return "USD" if "$" in c else "CNY"


def _parse_price_from_text(text):
    """从商品卡整段可见文本解析价格区，区分当前直接价/起价/优惠价/价格区间并识别货币。

    返回 dict：raw=含货币的页面原始显示（如 'CNY 201.09' / '$19.99' / '$8.99 - $19.99'）、
    num_raw=纯数字文本、value=float 数值、currency='CNY'/'USD'、
    kind='current'/'from'/'deal'/'range'/None、
    note=页面原始语境（如 '另有 4 种版本，价格 CNY 201.09 起'、'5 offers from $28.49'），
    全部缺省为 None。
    """
    empty = {"raw": None, "num_raw": None, "value": None,
             "currency": None, "kind": None, "note": None}
    if not text:
        return empty
    t = collapse(text)
    if not t:
        return empty
    # 价格区间（多版本商品）：如 “$8.99 - $19.99”
    range_m = _PRICE_RANGE_RE.search(t)
    # 先定位“起价/优惠价”语境片段（中文 + 英文语义）
    ctx_m = None
    ctx_kind = None
    for pat, kind in ((_PRICE_FROM_RE, "from"), (_PRICE_DEAL_RE, "deal")):
        m = pat.search(t)
        if m:
            ctx_m, ctx_kind = m, kind
            break
    # 优先选与语境无关的独立货币标价（当前直接价，更接近网页主价格区）
    best = None
    best_in_ctx = False
    for m in _PRICE_CUR_RE.finditer(t):
        if ctx_m and ctx_m.start() <= m.start() < ctx_m.end():
            continue
        best = m
        break
    if best is None and ctx_m:
        sub = _PRICE_CUR_RE.search(ctx_m.group(0))
        if sub:
            best = sub
            best_in_ctx = True
        else:
            return empty
    if best is None:
        return empty
    num_raw = best.group("num").strip()
    out = dict(empty)
    out["num_raw"] = num_raw
    out["value"] = _to_float_price(num_raw)
    out["currency"] = _cur_code(best.group("cur"))
    out["raw"] = best.group(0).strip()
    out["kind"] = "current"
    # 语境标记：卡面存在独立主价时保持 current（与网页主价格区一致），语境原文写入 note 供 tooltip；
    # 仅当卡面无独立标价、价格必须从语境中取回时，才把 kind 标为 from/deal。
    if ctx_m is not None and ctx_kind is not None:
        out["note"] = collapse(ctx_m.group(0))
        if best_in_ctx:
            out["kind"] = ctx_kind
    # 价格区间：页面主价本身即区间时，取区间低值落价，raw/note 保留完整区间原文
    if range_m is not None and range_m.start() <= best.start() < range_m.end():
        out["kind"] = "range"
        out["raw"] = collapse(range_m.group(0))
        out["note"] = collapse(range_m.group(0))
        if not out["currency"]:
            out["currency"] = "USD"
    return out


def _apply_price_meta(item, text):
    """为 item 补齐/校正价格语义字段（price_display/price_currency/price_kind/price_note）。
    若 item 尚无价格则一并回填 price_raw/price。返回是否发生变更。"""
    if not text:
        return False
    pr = _parse_price_from_text(text)
    changed = False
    if pr.get("value") is None:
        return False
    if item.get("price") is None:
        item["price_raw"] = pr.get("num_raw")
        item["price"] = pr.get("value")
        changed = True
    if not item.get("price_display"):
        item["price_display"] = pr.get("raw")
        changed = True
    if not item.get("price_currency"):
        item["price_currency"] = pr.get("currency")
        changed = True
    if not item.get("price_kind"):
        item["price_kind"] = pr.get("kind")
        changed = True
    if not item.get("price_note") and pr.get("note"):
        item["price_note"] = pr.get("note")
        changed = True
    return changed


def extract_missing_fields_from_text(item):
    """从商品卡 text 中尽量提取缺失的 price/rating/reviews/store_name。
    兼容英文站与中文站（/-/zh）格式；用于历史快照字段回填。返回是否发生任何补全。"""
    text = item.get("text") or ""
    if not text:
        return False
    changed = False
    # 价格：优先按“当前直接价/起价/优惠价”语义解析并补齐 currency/kind 元数据
    if item.get("price") is None or not item.get("price_currency"):
        if _apply_price_meta(item, text):
            changed = True
        elif item.get("price") is None:
            m = _ZH_PRICE_RE.search(text)
            if not m:
                m = _US_PRICE_RE.search(text)
            if m:
                item["price_raw"] = m.group(0).strip()
                item["price"] = _to_float_price(m.group(1))
                changed = True
    if item.get("rating") is None or item.get("reviews") is None:
        m = _ZH_RR_RE.search(text)
        if m:
            if item.get("rating") is None:
                item["rating"] = _to_float_price(m.group(1))
                changed = True
            if item.get("reviews") is None:
                item["reviews"] = _to_int(m.group(2))
                changed = True
        else:
            m = _US_RATING_RE.search(text)
            if m and item.get("rating") is None:
                item["rating"] = _to_float_price(m.group(1))
                changed = True
            if item.get("reviews") is None:
                m2 = _ZH_R2_RE.search(text)
                if not m2:
                    m2 = _US_REVIEWS_RE.search(text)
                if m2:
                    item["reviews"] = _to_int(m2.group(1))
                    changed = True
            if item.get("rating") is None:
                m3 = _ZH_R3_RE.search(text)
                if m3 and "最多" not in text[max(0, m3.start() - 6):m3.start()]:
                    item["rating"] = _to_float_price(m3.group(1))
                    changed = True
    if not item.get("store_name"):
        m = _ZH_STORE_RE.search(text)
        if not m:
            m = _US_STORE_RE.search(text)
        if m:
            item["store_name"] = collapse(m.group(1))
            changed = True
    return changed


def backfill_snapshot_fields(rec, write_back=False):
    """对历史快照中因中文站（/-/zh）解析缺失的字段做回填（价格/星级/评论/店铺）。
    write_back=True 时把回填结果写回对应快照 JSON 与 latest.json（内容级增强，
    不改变 rank/asin/is_store，因此不影响 diff 与历史对比）。返回发生补全的商品数。"""
    if not rec or not rec.get("items"):
        return 0
    n = 0
    for it in rec.get("items") or []:
        if extract_missing_fields_from_text(it):
            n += 1
    if n and write_back:
        sid = rec.get("snapshot_id")
        snap_p = os.path.join(SNAP_DIR, "top100_%s.json" % sid) if sid else None
        try:
            if snap_p and os.path.exists(snap_p):
                _write_json(snap_p, rec)
            latest = _read_json(LATEST_PATH, None)
            if latest and latest.get("snapshot_id") == sid:
                _write_json(LATEST_PATH, rec)
        except Exception as e:
            _log("快照字段回填写回失败: %s" % e)
    return n


def _belong_to_store(product, cfg):
    """判断商品卡是否属于目标店铺。返回 (bool, 命中方式)。"""
    kws = [k.strip().lower() for k in cfg.get("store_keywords", []) if k.strip()]
    sid = (cfg.get("store_page_marker") or "").lower()
    hay = " ".join([
        str(product.get("store_name") or "").lower(),
        str(product.get("store_url") or "").lower(),
        str(product.get("title") or "").lower(),
        str(product.get("text") or "").lower(),
    ])
    if sid and sid.lower() in hay:
        return True, "store_page"
    for kw in kws:
        if kw and kw in hay:
            return True, "keyword:" + kw
    return False, None


def _find_store_name_cfg(product, cfg):
    kws = [k.strip() for k in cfg.get("store_keywords", []) if k.strip()]
    # 卡片没给店铺名时尝试从关键词推断显示名
    return product.get("store_name") or (kws[0] if kws else None)


# ---------------------------------------------------------------- 浏览器滚动抓取（懒加载补全至 100 款）
class ScrollUnavailable(Exception):
    """滚动环境不可用（未安装 playwright / 找不到本机浏览器），用于触发 HTTP 回退。"""
    pass


FETCH_META = {"used": "http", "notice": ""}   # 记录本轮最终抓取模式与兜底提示（供前端横幅展示）
_AUTO_SCROLL_SKIP_UNTIL = 0.0                 # auto 模式下 captcha 被拦后的自动跳过滚动冷却时间戳


def _set_fetch_meta(used, notice=""):
    with LOCK:
        FETCH_META["used"] = used or "http"
        FETCH_META["notice"] = notice or ""


def _get_fetch_meta():
    with LOCK:
        return {"used": FETCH_META.get("used") or "http",
                "notice": FETCH_META.get("notice") or ""}


_SCROLL_JS_COUNT = (
    "(function(){var c=0;var ns=document.querySelectorAll('[data-asin]');"
    "for(var i=0;i<ns.length;i++){var n=ns[i];"
    "if(n.nodeType===1&&n.querySelector&&n.querySelector('a[href*=\"/dp/\"]')){c++;}}return c;})()"
)


def _is_blocked_page(text):
    """判断页面是否被人机验证 / AWS WAF challenge 拦截（滚动模式专用判据）。"""
    low = (text or "").lower()
    if any(m in low for m in _BLOCK_MARKERS):
        return True
    if "awswaf" in low and ("challenge" in low or "waf" in low):
        return True
    return False


def _start_browser(headless=True):
    """惰性启动 playwright 并复用本机 Edge/Chrome（channel 模式，不下载浏览器内核）。
    返回 (playwright, browser)。依赖缺失或启动失败抛 ScrollUnavailable。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise ScrollUnavailable(
            "未安装浏览器滚动依赖 playwright（请运行同目录 install_scroll_deps.bat 或 "
            "`python -m pip install playwright`）: %s" % (str(e)[:120]))
    pw = sync_playwright().start()
    errors = []
    browser = None
    for channel in ("msedge", "chrome"):
        try:
            browser = pw.chromium.launch(
                channel=channel, headless=headless,
                args=["--disable-blink-features=AutomationControlled"])
            break
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (channel, str(e)[:160]))
            browser = None
    if browser is None:
        try:
            pw.stop()
        except Exception:
            pass
        raise ScrollUnavailable(
            "无法启动本机 Edge/Chrome（%s）。请确认浏览器已安装，"
            "或检查 config.json 的代理设置。" % "; ".join(errors))
    return pw, browser


def _scroll_page_full(ctx, url, cfg, target=50, max_wait_sec=45, on_count=None,
                      page_timeout_sec=None):
    """在给定浏览器上下文打开榜单页并自动向下滚动，直到 data-asin 商品卡达到 target 张。

    超时护栏：单页硬超时 page_timeout_sec（默认取 cfg[scroll_page_timeout_sec]，兜底 75s）；
    超过硬时限立即抛 AmazonFetchError(kind='timeout')，禁止无限挂起。
    滚动采用慢速分段轮滑 + 网络空闲/页面稳定判定，增强懒加载触发。
    返回滚动后的完整 HTML。失败抛 AmazonFetchError（captcha/network/timeout）。"""
    deadline = time.time() + float(page_timeout_sec or cfg.get("scroll_page_timeout_sec") or 75)
    page = ctx.new_page()
    try:
        page.set_default_timeout(15000)   # 所有等待/求值单次上限 15s，防单步无限挂起

        def _remain_ms():
            return max(3000, int((deadline - time.time()) * 1000))

        # Amazon 边缘对本机出口 IP 偶发 TCP 层间歇丢弃（ERR_CONNECTION_CLOSED /
        # ERR_CONNECTION_RESET 等，HTTP 直抓亦有此现象，属通窗口期问题而非反爬）。
        # 网络层失败最多整体尝试 3 次（初次 + 重试 2 次），每次间隔 3~7s 随机退避；
        # 时间不足单页护栏余量时立即以 timeout 中止，避免无限挂起。
        last_err = None
        for _attempt in range(3):
            if time.time() + 8 >= deadline:
                raise AmazonFetchError(
                    "timeout", "浏览器打开页面超过单页硬超时护栏，已中止避免挂起。")
            try:
                page.goto(url, timeout=min(30000, _remain_ms()), wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                break
            except Exception as e:
                last_err = e
                if _attempt < 2:
                    time.sleep(random.uniform(3, 7))
        else:
            raise AmazonFetchError(
                "network",
                "浏览器打开页面失败（已自动重试 2 次仍失败）: %s" % (str(last_err)[:200]))
        html0 = page.content()
        if _is_blocked_page(html0) or len(html0) < 8000:
            raise AmazonFetchError(
                "captcha",
                "浏览器打开榜单页疑似被 AWS WAF / 人机验证拦截（页面过短或含 challenge 标记），"
                "请等待 10-30 分钟或配置可用代理后重试；本次自动回退 HTTP 直抓。")
        # ---- 慢速分段轮滑 + 网络空闲/页面稳定判定（增强懒加载触发） ----
        cnt = 0
        last_cnt = -1
        stable = 0
        no_grow = 0
        last_net = [time.time()]
        try:
            page.on("response", lambda _r: last_net.__setitem__(0, time.time()))
        except Exception:
            pass
        while time.time() < deadline:
            if time.time() + 4 >= deadline:
                break
            # 慢速轮滑：按视口高度分步滚动（懒加载对滚动事件更敏感），偶尔辅以鼠标滚轮微动
            try:
                step = max(400, int(page.evaluate("window.innerHeight||900") * 0.75))
                page.evaluate("window.scrollBy(0, %d)" % step)
                if random.random() < 0.25:
                    page.mouse.wheel(0, 240)
            except Exception:
                pass
            time.sleep(random.uniform(0.35, 0.7))
            try:
                prev_h = int(page.evaluate("document.body.scrollHeight") or 0)
            except Exception:
                prev_h = 0
            # 每次再整页到底一次，补触发懒加载锚点
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            time.sleep(random.uniform(0.3, 0.6))
            try:
                cnt = int(page.evaluate(_SCROLL_JS_COUNT) or 0)
            except Exception:
                cnt = last_cnt
            if on_count:
                on_count(cnt, target)
            if cnt >= target:
                # 达标后再短暂观察懒加载是否继续渲染（最多补约 3 秒）
                for _ in range(6):
                    if time.time() >= deadline:
                        break
                    time.sleep(0.5)
                    try:
                        cnt = int(page.evaluate(_SCROLL_JS_COUNT) or 0)
                    except Exception:
                        break
                    if on_count:
                        on_count(cnt, target)
                    if cnt <= last_cnt:
                        break
                    last_cnt = cnt
                break
            try:
                cur_h = int(page.evaluate("document.body.scrollHeight") or 0)
            except Exception:
                cur_h = prev_h
            grew = cur_h > prev_h + 40
            net_idle = (time.time() - last_net[0]) > 1.2
            if cnt == last_cnt and not grew and net_idle:
                stable += 1
            else:
                stable = 0
            no_grow = no_grow + 1 if not grew else 0
            last_cnt = cnt
            # 高度与卡片数都稳定且网络空闲 → 判定懒加载已结束，快速收尾
            if no_grow >= 3 and stable >= 3:
                break
        # 收尾：来回滚动到底，兜底触发最底部懒加载
        if time.time() < deadline:
            for _ in range(2):
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                time.sleep(0.5)
        try:
            page.wait_for_timeout(1200)
        except Exception:
            pass
        html = page.content()
        if _is_blocked_page(html):
            raise AmazonFetchError("captcha", "滚动后页面出现人机验证（captcha），已自动回退 HTTP 直抓。")
        return html
    finally:
        try:
            page.close()
        except Exception:
            pass


def _build_top(all_items):
    """按 rank 排序去重，最多保留 100 款（滚动/http 共用）。"""
    valid = [p for p in all_items if p.get("title")]
    valid.sort(key=lambda p: (p.get("rank") or 9999))
    seen = {}
    top = []
    for p in valid:
        if p["asin"] in seen:
            continue
        seen[p["asin"]] = True
        top.append(p)
        if len(top) >= 100:
            break
    return top


def fetch_top100_scroll(cfg, progress_cb=None, headless=True):
    """【浏览器滚动】用本机 Edge/Chrome 打开两页 BSR，向下滚动懒加载至每页约 50 卡，
    交给 parse_page(offset=0/50) 解析，保证 rank 连续、合计目标 100 款。

    硬超时护栏（防无限挂起）：
    - 单页护栏：_scroll_page_full 内部 deadline（cfg[scroll_page_timeout_sec]，默认 75s）；
    - 整体护栏：本函数整体在 daemon 线程内执行，主线程 join 上限
      cfg[scroll_overall_timeout_sec]（默认 200s），超时立即抛 AmazonFetchError
      (kind=timeout) 并交由 fetch_top100 回退 HTTP；极端卡死也不会永久挂起。
    """
    page_timeout_sec = float(cfg.get("scroll_page_timeout_sec") or 75)
    overall_timeout_sec = float(cfg.get("scroll_overall_timeout_sec") or 200)
    result = {}
    prog_lock = threading.RLock()

    def _prog(msg, pct=None):
        with prog_lock:
            if progress_cb:
                try:
                    progress_cb(msg, pct)
                except TypeError:
                    progress_cb(msg)
            _log(msg)

    def _worker():
        try:
            _prog("正在启动本机浏览器（复用 Edge/Chrome，无需下载内核）...", 2)
            pw, browser = _start_browser(headless=headless)
            try:
                proxy = None
                hp = (cfg.get("http_proxy") or "").strip()
                if hp:
                    proxy = {"server": hp}
                ctx = browser.new_context(
                    user_agent=random.choice(USER_AGENTS),
                    locale="en-US",
                    viewport={"width": 1440, "height": 900},
                    proxy=proxy)
                # 强制美元标价 + 英文站界面（与 HTTP 直抓口径一致，避免浏览器 locale 触发 CNY 渲染）
                try:
                    ctx.add_cookies([
                        {"name": "i18n-prefs", "value": "USD", "domain": ".amazon.com", "path": "/"},
                        {"name": "lc-main", "value": "en_US", "domain": ".amazon.com", "path": "/"},
                    ])
                except Exception:
                    pass
                all_items = []
                for idx, url in enumerate(PAGES):
                    pct_base = 8 if idx == 0 else 36
                    pct_end = 23 if idx == 0 else 47
                    _prog("正在用浏览器打开第 %d/2 页，滚动加载商品卡（目标 50，单页护栏 %ds）..."
                          % (idx + 1, int(page_timeout_sec)), pct_base)
                    html = _scroll_page_full(
                        ctx, url, cfg, target=50, page_timeout_sec=page_timeout_sec,
                        on_count=lambda n, t: _prog(
                            "浏览器滚动加载第 %d/2 页：已加载 %d/%d 商品卡 ..."
                            % (idx + 1, min(n, t), t),
                            min(pct_end, pct_base + int((pct_end - pct_base) * min(n, t) / max(t, 1)))))
                    _prog("第 %d/2 页滚动完成，正在解析榜单卡..." % (idx + 1),
                          24 if idx == 0 else 48)
                    items = parse_page(html, offset=idx * 50)
                    _log("第 %d 页（浏览器滚动）解析到 %d 个商品" % (idx + 1, len(items)))
                    all_items.extend(items)
                    if idx < len(PAGES) - 1:
                        time.sleep(random.uniform(5, 10))
                top = _build_top(all_items)
                _log("浏览器滚动抓取完成：共 %d 款（目标 100）" % len(top))
                result["ok"] = top
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
                try:
                    pw.stop()
                except Exception:
                    pass
        except Exception as e:   # noqa: BLE001
            result["error"] = e

    worker = threading.Thread(target=_worker, daemon=True, name="bsr-scroll-worker")
    worker.start()
    worker.join(overall_timeout_sec)
    if worker.is_alive():
        # 极端卡死：整体硬超时兜底，立即抛错让 fetch_top100 回退 HTTP
        _log("浏览器滚动整体超时（>%ds 护栏），已中止本次滚动抓取并转 HTTP 直抓回退。"
             % int(overall_timeout_sec))
        raise AmazonFetchError(
            "timeout",
            "浏览器滚动超过整体硬超时护栏（%d 秒）仍未完成，已中止避免挂起；本次回退 HTTP 直抓。"
            % int(overall_timeout_sec))
    if result.get("error"):
        e = result["error"]
        if isinstance(e, AmazonFetchError):
            raise e
        raise AmazonFetchError("scroll_worker", "浏览器滚动执行失败: %s" % (str(e)[:200]))
    return result.get("ok") or []


def fetch_top100(cfg, progress_cb=None):
    """统一抓取入口：按 config['fetch_mode'] 路由 http / scroll / auto。
    auto：浏览器滚动优先；滚动环境不可用/失败（含 WAF captcha 冷却）自动回退 HTTP 直抓并记录提示。"""
    mode = str(cfg.get("fetch_mode") or "auto").strip().lower()
    if mode not in ("http", "scroll", "auto"):
        mode = "auto"
    global _AUTO_SCROLL_SKIP_UNTIL

    def _fallback_http(reason):
        _log("滚动模式不可用，回退 HTTP 直抓：%s" % reason)
        try:
            top = fetch_top100_http(cfg, progress_cb=progress_cb)
            if len(top) >= 100:
                _set_fetch_meta("http", "浏览器滚动未生效：%s。已用 HTTP 直抓抓满。" % reason)
            else:
                _set_fetch_meta("http", "%s。已回退 HTTP 直抓，仅获取到 %d/100 款。"
                                % (reason, len(top)))
            return top
        except AmazonFetchError as e:
            raise AmazonFetchError(
                e.kind, "浏览器滚动失败：%s。HTTP 直抓兜底也失败：%s" % (reason[:300], e)) from None
        except Exception as e:
            raise AmazonFetchError(
                "network", "浏览器滚动失败：%s。HTTP 直抓兜底也失败：%s"
                % (reason[:300], str(e)[:300])) from None

    if mode == "http":
        top = fetch_top100_http(cfg, progress_cb=progress_cb)
        _set_fetch_meta("http", "")
        return top
    if mode == "auto" and time.time() < _AUTO_SCROLL_SKIP_UNTIL:
        return _fallback_http(
            "浏览器滚动此前被 AWS WAF / 人机验证拦截，30 分钟内自动跳过滚动重试")
    try:
        top = fetch_top100_scroll(cfg, progress_cb=progress_cb)
        if len(top) >= 100:
            _set_fetch_meta("scroll", "")
        else:
            _set_fetch_meta("scroll",
                            "浏览器滚动模式本次仅获取到 %d/100 款，未达预期（页面结构/懒加载可能变化）。"
                            % len(top))
        return top
    except ScrollUnavailable as e:
        return _fallback_http("浏览器滚动不可用：%s" % e)
    except AmazonFetchError as e:
        if e.kind == "captcha":
            _AUTO_SCROLL_SKIP_UNTIL = time.time() + 1800
        return _fallback_http("浏览器滚动失败（%s）：%s" % (e.kind, e))
    except Exception as e:
        return _fallback_http("浏览器滚动异常：%s" % (str(e)[:200]))


# ---------------------------------------------------------------- 抓取主流程
def fetch_top100_http(cfg, progress_cb=None):
    """【HTTP 直抓】抓取两页服务端渲染卡（约30/页），返回按 rank 排序的 TOP 列表，最多约 60 款。"""
    def prog(msg, pct=None):
        if progress_cb:
            try:
                progress_cb(msg, pct)
            except TypeError:
                progress_cb(msg)
        _log(msg)
    opener = _build_opener(cfg)
    # 预热首页获取 cookie
    try:
        prog("预热访问 amazon.com 获取 Cookie ...", 2)
        http_get_text(HOME_URL, opener, cfg, timeout=30, retries=1)
    except Exception as e:
        _log("首页预热失败(忽略): %s" % e)
    all_items = []
    for idx, url in enumerate(PAGES):
        if idx == 0:
            prog("正在请求第 1/2 页 ...", 8)
        else:
            prog("正在请求第 2/2 页 ...", 36)
        text, _ = http_get_text(url, opener, cfg, timeout=40, retries=2)
        items = parse_page(text, offset=idx * 50)
        _log("第 %d 页解析到 %d 个商品" % (idx + 1, len(items)))
        if idx == 0:
            prog("第 1 页解析完成，正在整理榜单 ...", 24)
        else:
            prog("第 2 页解析完成，正在合并 TOP100 ...", 48)
        all_items.extend(items)
        if idx < len(PAGES) - 1:
            time.sleep(random.uniform(6, 14))
    # 按 rank 排序，去掉无标题杂质
    valid = [p for p in all_items if p.get("title")]
    valid.sort(key=lambda p: (p.get("rank") or 9999))
    seen = {}
    top = []
    for p in valid:
        if p["asin"] in seen:
            continue
        seen[p["asin"]] = True
        top.append(p)
        if len(top) >= 100:
            break
    _log("抓取完成：共 %d 款（目标 100；Amazon 榜单页每页仅服务端渲染约 30 款，其余需浏览器滚动懒加载，HTTP 直抓存在数量上限）" % len(top))
    if len(top) < 60:
        raw_path = os.path.join(RAW_DIR, "page_failed_%s.html" % now_tag())
        try:
            with open(raw_path, "w", encoding="utf-8") as f:
                f.write("URLS=%s\n" % json.dumps(PAGES))
                f.write(text)
        except Exception:
            pass
        raise AmazonFetchError(
            "blocked",
            "页面结构可能变化或榜单内容不足，仅解析到 %d 个商品。"
            "原始 HTML 已保存到 data/raw/%s 便于排查。" % (len(top), os.path.basename(raw_path)))
    return top

# ---------------------------------------------------------------- 快照持久化
def save_snapshot(top_items, cfg, source="manual"):
    sid = now_tag()
    rec = {
        "snapshot_id": sid,
        "fetched_at": now_str(),
        "source": source,
        "count": len(top_items),
        "items": top_items,
    }
    path = os.path.join(SNAP_DIR, "top100_%s.json" % sid)
    _write_json(path, rec)
    _write_json(LATEST_PATH, rec)
    history = _read_json(HISTORY_PATH, [])
    store_items = [p for p in top_items if p.get("is_store")]
    history.insert(0, {
        "snapshot_id": sid,
        "fetched_at": now_str(),
        "count": len(top_items),
        "store_count": len(store_items),
        "file": os.path.basename(path),
    })
    _write_json(HISTORY_PATH, history[:500])
    return sid, rec

def load_latest():
    return _read_json(LATEST_PATH, None)

def load_snapshot(sid=None):
    if sid:
        p = os.path.join(SNAP_DIR, "top100_%s.json" % sid)
        if os.path.exists(p):
            return _read_json(p, None)
        return None
    return load_latest()

def delete_snapshot(sid):
    """删除指定快照：移除 JSON 文件并从 history.json 摘除记录。

    若删除的是当前看板引用的最新快照，同步清空 STATE 中的 last_result /
    last_store_items / last_snapshot_id，避免前端展示与导出引用已删除的数据。
    返回 (ok, message, reset_last)。
    """
    history = _read_json(HISTORY_PATH, []) or []
    target = next((h for h in history if h.get("snapshot_id") == sid), None)
    path = os.path.join(SNAP_DIR, "top100_%s.json" % sid)
    file_existed = os.path.exists(path)
    if not target and not file_existed:
        return False, "快照不存在: %s" % sid, False
    removed_hist = False
    new_hist = []
    for h in history:
        if h.get("snapshot_id") == sid:
            removed_hist = True
            continue
        new_hist.append(h)
    if file_existed:
        try:
            os.remove(path)
        except OSError as e:
            return False, "删除快照文件失败: %s" % e, False
    if removed_hist:
        _write_json(HISTORY_PATH, new_hist)
    reset_last = False
    with LOCK:
        if STATE.get("last_snapshot_id") == sid:
            STATE["last_snapshot_id"] = None
            STATE["last_fetch"] = None
            STATE["last_result"] = None
            STATE["last_store_items"] = None
            reset_last = True
    return True, "已删除快照 %s（JSON 文件 + 历史记录）" % sid, reset_last

# ---------------------------------------------------------------- 变动对比
def diff_snapshots(old_rec, new_rec):
    old_items = {p["asin"]: p for p in (old_rec or {}).get("items", [])}
    new_items = {p["asin"]: p for p in (new_rec or {}).get("items", [])}
    new_in = []
    dropped = []
    for asin, p in new_items.items():
        if asin not in old_items:
            new_in.append(p)
    for asin, p in old_items.items():
        if asin not in new_items:
            dropped.append(p)
    new_in.sort(key=lambda x: x.get("rank") or 9999)
    dropped.sort(key=lambda x: x.get("rank") or 9999)
    return new_in, dropped

def save_diff(sid, old_rec, new_rec, new_in, dropped, cfg):
    rec = {
        "diff_id": now_tag(),
        "compared_at": now_str(),
        "base_snapshot": (old_rec or {}).get("snapshot_id"),
        "new_snapshot": (new_rec or {}).get("snapshot_id"),
        "new_in_count": len(new_in),
        "dropped_count": len(dropped),
        "new_in": new_in,
        "dropped": dropped,
    }
    jp = os.path.join(CHG_DIR, "diff_%s.json" % rec["diff_id"])
    _write_json(jp, rec)
    cp = os.path.join(CHG_DIR, "diff_%s.csv" % rec["diff_id"])
    _write_diff_csv(cp, new_in, dropped)
    return rec, jp, cp

def _write_diff_csv(path, new_in, dropped):
    with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["变动类型", "排名", "ASIN", "标题", "价格", "星级", "评论数", "店铺", "链接"])
        for p in new_in:
            w.writerow(["新上榜", p.get("rank"), p.get("asin"), p.get("title"),
                        _fmt_price(p.get("price"), p.get("price_currency"), p.get("price_kind")),
                        p.get("rating"), p.get("reviews"),
                        p.get("store_name") or "", p.get("url") or ""])
        for p in dropped:
            w.writerow(["落榜", p.get("rank"), p.get("asin"), p.get("title"),
                        _fmt_price(p.get("price"), p.get("price_currency"), p.get("price_kind")),
                        p.get("rating"), p.get("reviews"),
                        p.get("store_name") or "", p.get("url") or ""])

# ---------------------------------------------------------------- 店铺归属标注
def annotate_store(top_items, cfg):
    for p in top_items:
        ok, how = _belong_to_store(p, cfg)
        p["is_store"] = ok
        p["match_how"] = how
        if ok and not p.get("store_name"):
            p["store_name"] = _find_store_name_cfg(p, cfg)
    return top_items

# ---------------------------------------------------------------- 市场摘要
def market_summary(top_items):
    """统计 TOP 榜单整体画像。保留基础分位/均值字段，并补充销售顾问画像指标。

    新增字段：
    - data_quality: 价格/星级/评论字段覆盖率（有数据才能画像）
    - price_zones: 价格档位分布（固定五档 0~20 / 20~50 / 50~100 / 100~200 / >200，
      按商品价格数值直接落入对应档，不做任何汇率换算；label 不含货币符号，
      货币由 summary.currency 统一给出（当前抓取美站美元价，即 USD），
      展示时在标题/前缀标注币种代码）
    - cheapest / dearest / most_reviewed / highest_rated: 款级速览
    - store_stats: 目标店铺上榜款数、排名、价格区间与相对中位价位置
    """
    items = list(top_items)
    n = len(items)

    def _with_price(it):
        return it.get("price") is not None

    priced = [it for it in items if _with_price(it)]
    prices = [it["price"] for it in priced]
    rated = [it for it in items if it.get("rating") is not None]
    ratings = [it["rating"] for it in rated]
    reviewed = [it for it in items if it.get("reviews") is not None]
    reviews = [it["reviews"] for it in reviewed]

    def pct(arr, q):
        if not arr:
            return None
        arr = sorted(arr)
        k = max(0, min(len(arr) - 1, int(round((len(arr) - 1) * q))))
        return arr[k]

    def trim2(x):
        return ("%.2f" % x).rstrip("0").rstrip(".") if x is not None else "—"

    def mini(dic):
        return {k: dic.get(k) for k in ("rank", "asin", "title", "price",
                                        "rating", "reviews", "url")}

    # 货币：按本次快照商品卡实际货币推断（当前抓取美站，应为 USD），供展示与报告使用
    _cur_counter = {}
    for it in items:
        c = it.get("price_currency")
        if c:
            _cur_counter[c] = _cur_counter.get(c, 0) + 1
    summary_currency = (max(_cur_counter, key=_cur_counter.get)
                        if _cur_counter else "USD")
    summary = {
        "count": n,
        "currency": summary_currency,
        "price_p25": pct(prices, 0.25),
        "price_p50": pct(prices, 0.50),
        "price_p75": pct(prices, 0.75),
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "rating_avg": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "reviews_median": pct(reviews, 0.50),
        "data_quality": {
            "price_n": len(priced), "rating_n": len(rated), "reviews_n": len(reviewed),
            "has_price": bool(priced), "has_rating": bool(rated), "has_reviews": bool(reviewed),
        },
        "price_zones": [],
        "cheapest": None, "dearest": None,
        "most_reviewed": None, "highest_rated": None,
        "store_stats": None,
    }
    # 价格档位：固定五档 0~20 / 20~50 / 50~100 / 100~200 / >200（档位边界数值固定不变）
    # 商品价格按页面原样币种落库（当前抓取美站，price_currency=USD，即页面 $ 标价），
    # 不做任何汇率换算；币种由 summary.currency（本次快照多数商品币种）统一标注，
    # 展示标题使用币种代码（USD / CNY），不引入任何换算语义。
    _PRICE_BANDS = [
        (0.0, 20.0, "0~20"),
        (20.0, 50.0, "20~50"),
        (50.0, 100.0, "50~100"),
        (100.0, 200.0, "100~200"),
        (200.0, None, ">200"),
    ]
    for _lo, _hi, _lb in _PRICE_BANDS:
        if _hi is None:
            _cnt = sum(1 for it in priced if (it["price"] or 0) >= _lo)
        else:
            _cnt = sum(1 for it in priced if _lo <= (it["price"] or 0) < _hi)
        summary["price_zones"].append({"label": _lb,
                                       "count": _cnt,
                                       "pct": round(100.0 * _cnt / n, 1) if n else 0})
    if priced:
        priced_sorted = sorted(priced, key=lambda x: x["price"])
        summary["cheapest"] = mini(priced_sorted[0])
        summary["dearest"] = mini(priced_sorted[-1])
    if reviewed:
        summary["most_reviewed"] = mini(max(reviewed, key=lambda x: x.get("reviews") or 0))
    if rated:
        summary["highest_rated"] = mini(max(rated, key=lambda x: x.get("rating") or 0))
    store_items = [it for it in items if it.get("is_store")]
    if store_items:
        cp = [it["price"] for it in store_items if it.get("price") is not None]
        med = pct(prices, 0.50) if prices else None
        vs = None
        if cp and med:
            avg = sum(cp) / len(cp)
            vs = "higher" if avg >= med else "lower"
        summary["store_stats"] = {
            "count": len(store_items),
            "ranks": sorted(it.get("rank") for it in store_items if it.get("rank") is not None),
            "avg_price": round(sum(cp) / len(cp), 2) if cp else None,
            "min_price": min(cp) if cp else None,
            "max_price": max(cp) if cp else None,
            "median_price": med,
            "vs_median": vs,
        }
    return summary

# ---------------------------------------------------------------- AI 接入
def llm_chat(cfg, system, user, temperature=0.3):
    """OpenAI 兼容 chat completions。支持 ollama / apikey 模式。"""
    mode = cfg.get("ai_mode", "offline")
    if mode == "offline":
        raise AmazonFetchError("ai", "当前为离线模式，未配置模型")
    base = (cfg.get("ai_base_url") or "").rstrip("/")
    if not base:
        raise AmazonFetchError("ai", "缺少 ai_base_url")
    if not base.endswith("/v1") and "/chat/completions" not in base:
        # Ollama 通常需要 /v1 前缀
        if "11434" in base and "/v1" not in base:
            base = base + "/v1"
    url = base + "/chat/completions"
    payload = {
        "model": cfg.get("ai_model") or "gpt-4o-mini",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
    }
    # max_tokens：取配置 ai_max_tokens（默认 8192）；空值/0 时不传该字段，交给服务端默认
    _mt = cfg.get("ai_max_tokens")
    if _mt not in (None, ""):
        try:
            _mt_v = int(_mt)
            if _mt_v > 0:
                payload["max_tokens"] = _mt_v
        except (TypeError, ValueError):
            pass
    # Ollama 专用上下文窗口：base url 含 11434（或模式=ollama）且配置 ai_num_ctx 时注入 options.num_ctx
    _is_ollama = ("11434" in base) or str(cfg.get("ai_mode") or "").lower() == "ollama"
    _nc = cfg.get("ai_num_ctx")
    if _is_ollama and _nc not in (None, ""):
        try:
            _nc_v = int(_nc)
            if _nc_v > 0:
                payload["options"] = {"num_ctx": _nc_v}
        except (TypeError, ValueError):
            pass
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "marvis-bsr-local/1.0"}
    if cfg.get("ai_api_key"):
        headers["Authorization"] = "Bearer " + cfg["ai_api_key"]
    opener = _build_opener(cfg)
    try:
        req = url_request.Request(url, data=data, headers=headers, method="POST")
        with opener.open(req, timeout=180) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        obj = json.loads(body)
        return obj["choices"][0]["message"]["content"]
    except AmazonFetchError:
        raise
    except Exception as e:
        raise AmazonFetchError("ai", "AI 调用失败: %s" % e)

def _strip_llm_artifacts(text):
    """剥离本地模型常见的思维链/分隔符残留，避免报告正文混入大段英文 artifacts。"""
    t = re.sub(r"<\|im_start\|>.*?(?:<\|im_end\|>|\Z)", "", text, flags=re.S)
    t = re.sub(r"<\|endoftext\|>", "", t)
    t = re.sub(r"<\|end\|>", "", t)
    t = re.sub(r"</?(?:think|reasoning)[^>]*>", "", t, flags=re.I)
    m = re.search(r"(你?Thinking Process|思考过程[:：]|Let'?s think|Step-by-step reasoning|Here'?s my (?:analysis|reasoning))", t)
    if m:
        t = t[:m.start()].rstrip()
    out = []
    en_lines = []

    def flush():
        if len(en_lines) >= 3:
            en_lines[:] = []
        else:
            out.extend(en_lines)
            en_lines[:] = []

    for ln in t.splitlines():
        st = ln.strip()
        if st and len(st) > 60 and (sum(1 for ch in st if ord(ch) < 128) / float(len(st))) > 0.88:
            en_lines.append(ln)
        else:
            flush()
            out.append(ln)
    flush()
    return "\n".join(out)


def _dedupe_llm_text(text):
    """LLM 输出轻量后处理：去除重复段落 / 重复行（保留首次出现），抑制本地模型长文本退化。"""
    if not text:
        return text
    text = _strip_llm_artifacts(text)
    if not text:
        return ""
    blocks = re.split(r"\n[ \t]*\n", text)
    seen = set()
    kept = []
    for b in blocks:
        key = b.strip()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        kept.append(b)
    prev_key = None
    lines = []
    for raw in "\n\n".join(kept).splitlines():
        key = raw.strip()
        if key and key == prev_key:
            continue
        if key:
            prev_key = key
        lines.append(raw)
    return "\n".join(lines).strip()


def ai_test(cfg):
    if cfg.get("ai_mode") == "offline":
        return {"ok": False, "message": "离线模式无需测试"}
    try:
        ans = llm_chat(cfg, "You are a helpful assistant.",
                       "Reply with exactly: OK", temperature=0)
        return {"ok": True, "message": "连接成功，模型返回: %s" % str(ans)[:80]}
    except Exception as e:
        return {"ok": False, "message": str(e)}

# ---------------------------------------------------------------- Prompt 构建
def _clip_title(s, maxlen=110):
    # 商品标题截短：优先保留中文主体，避免长英文描述刷屏。
    # 美站标题为纯英文：无中文分隔符时按英文常见分隔（" - " / " ("）截断，
    # 仍无分隔则保留前 maxlen 字符 + 省略号（兜底，绝不返回空标题）。
    s = (s or "").strip()
    if len(s) <= maxlen:
        return s
    head = s[:maxlen]
    # 1) 优先在常见中/英分隔处截断，保留中文主体（如 '…双驱动无线扬声器 | BassUp, 24H...'）
    for sep in (" | ", "｜", " |", "| ", " — ", "，", "；", "。", ", ", " - ", " ("):
        idx = head.rfind(sep)
        if idx > 15:
            return head[:idx].strip() + "…"
    # 2) 无合适分隔时：头部含中文且尾部几乎全为英文，则截到最后一个中文字符
    last_cjk = -1
    for i, ch in enumerate(head):
        if "\u4e00" <= ch <= "\u9fff":
            last_cjk = i
    if last_cjk >= 0:
        tail = head[last_cjk + 1:]
        if not re.search(r"[\u4e00-\u9fff]", tail):
            return head[:last_cjk + 1].strip() + "…"
    # 3) 兜底：确保非空返回（纯英文/无有效分隔的极端情况）
    if not head.strip():
        return (s[:maxlen].strip() or s.strip()) + "…"
    return head.rstrip() + "…"

def _item_line(p):
    return ("- 排名 #%s | ASIN %s | %s | 价格 %s | 星级 %s | 评论 %s | 店铺 %s | %s"
            % (p.get("rank"), p.get("asin"), _clip_title(p.get("title")),
               _fmt_price(p.get("price"), p.get("price_currency"), p.get("price_kind")),
               p.get("rating"), p.get("reviews"),
               p.get("store_name") or "—", p.get("url") or ""))

def _items_block(items):
    """商品明细文本块：空列表返回“无”，避免模型误解。"""
    items = list(items or [])
    if not items:
        return "无"
    return "\n".join(_item_line(p) for p in items)


def _cur_sym(summary):
    """价格展示前缀：CNY 用 'CNY'，USD（美站抓取口径）用 '$'。"""
    return "CNY" if (summary or {}).get("currency") == "CNY" else "$"

def _cur_name(summary):
    """币种代码（用于档位标题/币种说明等文案）：'CNY' / 'USD'。"""
    return "CNY" if (summary or {}).get("currency") == "CNY" else "USD"

def build_summary_section(new_in, dropped, top_items, summary, fetched_at=None, gen_at=None,
                          prev_fetched_at=None, prev_snapshot_id=None, snapshot_id=None):
    """报告「总结」板块的模板化正文：仅由真实抓取数据生成（不经过 LLM，杜绝编造）。

    严格两块：
      一、上榜与落榜产品 —— 真实 diff（ASIN/标题/排名/价格/星级/评论）；
      二、本次 TOP100 销售向数据分析 —— 仅使用快照可见真实字段统计。
    任何缺失都如实说明，不做推断性补数。
    """
    cur = _cur_sym(summary)
    dq = (summary or {}).get("data_quality") or {}
    lines = []
    lines.append("## 总结")
    lines.append("")
    _base_txt = ("%s（快照 %s）" % (fmt_min(prev_fetched_at), prev_snapshot_id)) if prev_fetched_at else "无（本次为基线快照，未做变动对比）"
    _cur_txt = ("%s（快照 %s）" % (fmt_min(fetched_at), snapshot_id)) if snapshot_id else fmt_min(fetched_at)
    lines.append("> 报告生成时间：%s ｜ 本次数据抓取时间：%s ｜ 对比基准快照：%s"
                 % (fmt_min(gen_at or now_str()), _cur_txt, _base_txt))
    lines.append(">")
    lines.append("> 本总结全部数据条目来自真实抓取结果与字段统计，不包含模型推导数字。")
    lines.append("")
    # ---- 板块一：上榜与落榜产品（真实 diff）----
    lines.append("### 一、上榜与落榜产品（基于两次快照真实 diff）")
    lines.append("")
    lines.append("> 本次快照：%s；对比基准快照：%s（上榜 / 落榜均为两份快照之间 ASIN 的真实增减）。"
                 % (_cur_txt, _base_txt))
    lines.append("")
    lines.append("**① 新上榜（进入 TOP100）**：%s" % ("共 %d 款" % len(new_in) if new_in else "无（本次与上一快照对比未发现新增进入 TOP100 的商品；如仅有基线快照，请抓取第二次后再对比）"))
    lines.append("")
    if new_in:
        lines.append("| 现排名 | ASIN | 产品标题 | 价格 | 星级 | 评论数 |")
        lines.append("|---|---|---|---|---|---|")
        for p in new_in:
            lines.append("| %s | %s | %s | %s | %s | %s |"
                         % (p.get("rank"), p.get("asin") or "—",
                            _clip_title(p.get("title"), 55) or "—",
                            _fmt_price(p.get("price"), p.get("price_currency"), p.get("price_kind")),
                            p.get("rating") if p.get("rating") is not None else "—",
                            p.get("reviews") if p.get("reviews") is not None else "—"))
        lines.append("")
    lines.append("**② 落榜（跌出 TOP100）**：%s" % ("共 %d 款" % len(dropped) if dropped else "无（本次与上一快照对比未发现跌出 TOP100 的商品）"))
    lines.append("")
    if dropped:
        lines.append("| 原排名 | ASIN | 产品标题 | 价格 | 星级 | 评论数 |")
        lines.append("|---|---|---|---|---|---|")
        for p in dropped:
            lines.append("| %s | %s | %s | %s | %s | %s |"
                         % (p.get("rank"), p.get("asin") or "—",
                            _clip_title(p.get("title"), 55) or "—",
                            _fmt_price(p.get("price"), p.get("price_currency"), p.get("price_kind")),
                            p.get("rating") if p.get("rating") is not None else "—",
                            p.get("reviews") if p.get("reviews") is not None else "—"))
        lines.append("")
    # ---- 板块二：TOP100 销售向数据分析（快照真实字段）----
    lines.append("### 二、本次 TOP100 销售向数据分析（仅基于快照可见真实字段）")
    lines.append("")
    items = top_items or []
    n = len(items)
    n_price = dq.get("price_n")
    n_rating = dq.get("rating_n")
    n_rev = dq.get("reviews_n")
    lines.append("- **数据概况**：本次快照共抓取 %d 款商品；含价格字段 %s 款，含星级 %s 款，含评论数 %s 款。币种：%s。"
                 % (n,
                    n_price if n_price is not None else ("—" if not summary.get("price_p50") else n),
                    n_rating if n_rating is not None else ("—" if summary.get("rating_avg") is None else n),
                    n_rev if n_rev is not None else ("—" if summary.get("reviews_median") is None else n),
                    _cur_name(summary)))
    if summary.get("price_p50"):
        lines.append("- **价格带**：区间 %s ~ %s；四分位 P25=%s、中位数 P50=%s、P75=%s。"
                     % (_fmt_price(summary.get("price_min"), cur), _fmt_price(summary.get("price_max"), cur),
                        _fmt_price(summary.get("price_p25"), cur), _fmt_price(summary.get("price_p50"), cur),
                        _fmt_price(summary.get("price_p75"), cur)))
    elif n_price:
        lines.append("- **价格带**：本次快照含价格商品较少，无法计算分位统计（有 %s 款价格数据）。" % n_price)
    else:
        lines.append("- **价格带**：本次快照未解析到价格字段，无法统计。")
    zones = (summary or {}).get("price_zones") or []
    if zones:
        zparts = ["%s %s（%d 款/%d%%）" % (cur, z.get("label") or "", z.get("count") or 0, z.get("pct") or 0) for z in zones]
        lines.append("- **价格档位分布（%s · 固定五档 0~20/20~50/50~100/100~200/>200）**：%s。"
                     % (_cur_name(summary), "、".join(zparts)))
    if summary.get("rating_avg") is not None:
        lines.append("- **评分**：榜单平均星级 %s（覆盖 %s 款）。" % (summary.get("rating_avg"), n_rating if n_rating is not None else n))
    if summary.get("reviews_median") is not None:
        lines.append("- **评论数（销量代理指标）**：中位数 %s（覆盖 %s 款）；评论数最高的单品见下。"
                     % (summary.get("reviews_median"), n_rev if n_rev is not None else n))
    if summary.get("cheapest"):
        lines.append("- **入门款参考（最低价）**：#%s %s，%s。" % (summary["cheapest"].get("rank"), _clip_title(summary["cheapest"].get("title"), 45), _fmt_price(summary["cheapest"].get("price"), cur, summary["cheapest"].get("price_kind"))))
    if summary.get("dearest"):
        lines.append("- **高端款参考（最高价）**：#%s %s，%s。" % (summary["dearest"].get("rank"), _clip_title(summary["dearest"].get("title"), 45), _fmt_price(summary["dearest"].get("price"), cur, summary["dearest"].get("price_kind"))))
    if summary.get("most_reviewed"):
        lines.append("- **人气锚点（评论数最高）**：#%s %s，评论 %s。" % (summary["most_reviewed"].get("rank"), _clip_title(summary["most_reviewed"].get("title"), 45), summary["most_reviewed"].get("reviews")))
    if summary.get("highest_rated"):
        lines.append("- **口碑锚点（星级最高）**：#%s %s，星级 %s。" % (summary["highest_rated"].get("rank"), _clip_title(summary["highest_rated"].get("title"), 45), summary["highest_rated"].get("rating")))
    # 头部梯队（真实排名数据）
    heads = [p for p in items if p.get("rank") is not None][:10]
    if heads:
        lines.append("- **TOP10 头部梯队**：%s。" % "；".join(
            "#%s %s（评论 %s）" % (p.get("rank"), _clip_title(p.get("title"), 28),
                                  p.get("reviews") if p.get("reviews") is not None else "—")
            for p in heads))
    # 基于真实统计的轻量机会提示（规则化，无编造）
    lines.append("- **基于上述真实统计的机会提示**：")
    tip_added = False
    if summary.get("price_p50") and zones:
        ztop = max(zones, key=lambda z: z.get("count") or 0)
        if ztop:
            lines.append("  - 主流成交带集中在 %s %s（%d 款），新品/选品可优先评估该价格带的差异化切入；若自身已在该带，重点看评分与评论增速是否跑赢同档。"
                         % (cur, ztop.get("label") or "", ztop.get("count") or 0))
            tip_added = True
    if summary.get("reviews_median") is not None:
        lines.append("  - 评论数中位数 %s 是该类目竞争门槛的近似参照：低于中位数较多的商品若无广告/站外流量加持，短期冲榜难度大；高于中位数 2 倍以上的商品为头部流量锚点，可作对标对象。"
                     % summary.get("reviews_median"))
        tip_added = True
    if summary.get("rating_avg") is not None:
        lines.append("  - 榜单平均星级 %s：低于该值的新品差评风险更高；高于该值的商品若同时位于成交密集价格带，是值得拆解的爆款画像。"
                     % summary.get("rating_avg"))
        tip_added = True
    if not tip_added:
        lines.append("  - 本次快照字段覆盖有限，建议先抓取完整字段（价格/星级/评论）后再给出带数字的机会提示；当前仅能确认榜单构成以 %d 款为基线。" % n)
    if new_in:
        lines.append("  - 新上榜 %d 款集中在榜单中后段时，通常代表细分功能或新流量正在冒头，可结合其标题卖点关键词反查搜索趋势。" % len(new_in))
    if n < 100:
        lines.append("  - 本次仅覆盖 %d/100（Amazon 懒加载限制），上述统计基于已获取部分，趋势结论请待抓满后再下。" % n)
    return "\n".join(lines)

def _summary_allowed_numbers(summary):
    """收集模板统计中可能出现的数值，供 LLM 观察兜底审计使用（防止模型编造价格/评论数字）。"""
    allowed = set()
    if not summary:
        return allowed
    for k in ("price_min", "price_p25", "price_p50", "price_p75", "price_max", "rating_avg", "reviews_median"):
        v = summary.get(k)
        if isinstance(v, (int, float)):
            allowed.add(round(float(v), 2))
    for z in (summary.get("price_zones") or []):
        if z.get("count") is not None:
            allowed.add(int(z["count"]))
        if z.get("pct") is not None:
            allowed.add(int(z["pct"]))
    dq = summary.get("data_quality") or {}
    for k in ("price_n", "rating_n", "reviews_n"):
        if isinstance(dq.get(k), (int, float)):
            allowed.add(int(dq[k]))
    cm = summary.get("store_stats") or {}
    for k in ("avg_price", "median_price", "min_price", "max_price"):
        if isinstance(cm.get(k), (int, float)):
            allowed.add(round(float(cm[k]), 2))
    return allowed

def _sanitize_summary_llm(text, summary=None, cfg=None):
    """总结板块 LLM 输出的后处理兜底：
    1) 复用去 artifact / 去重管线；
    2) 大量店铺专题式表述 → 整体丢弃（报告只保留模板真实数据）；
    3) 出现模板统计之外的货币数值且无『约/左右/起/约合』等限定词 → 视为可疑编造，累计 2 处以上整体降级；
    4) 出现『我们店铺/本公司/品牌销量』等自述店铺结论 → 删除对应块。
    返回清理后文本；为空表示应丢弃 LLM 观察，仅保留模板。
    """
    if not text or not text.strip():
        return ""
    text = _dedupe_llm_text(text)
    if not text:
        return ""
    store_pat = _store_name_pattern(cfg)
    if store_pat:
        if len(re.findall(store_pat, text, flags=re.I)) > 4:
            return ""
        if re.search(r"(%s.{0,20}(?:店铺|品牌|旗舰店)|(?:我们|我司|本公司)(?:的)?(?:店铺|品牌)|(?:品牌|店铺).{0,6}(?:销量|营业额|销售额|月销))" % store_pat, text, flags=re.I):
            return ""
    elif re.search(r"((?:我们|我司|本公司)(?:的)?(?:店铺|品牌)|(?:品牌|店铺).{0,6}(?:销量|营业额|销售额|月销))", text, flags=re.I):
        return ""
    # 块级：剔除疑似店铺专题块
    blocks = [b for b in re.split(r"\n[ \t]*\n", text) if b.strip()]
    keep = []
    for b in blocks:
        if store_pat and re.search(store_pat, b, flags=re.I) and re.search(r"(店铺|品牌|旗舰店|我们|我司|本公司|销量|销售额|月销|订单)", b):
            continue
        if re.search(r"(?:我们|我司|本公司)(?:的)?(?:店铺|品牌)", b):
            continue
        keep.append(b)
    text = "\n\n".join(keep).strip()
    if not text:
        return ""
    allowed = _summary_allowed_numbers(summary)
    suspicious = []
    if allowed:
        # 只审计『货币+数字』这类可被编造的最敏感数值
        for m in re.finditer(r"(?:US\$|USD|CNY|CN¥|￥|¥|\$)\s?(\d{1,3}(?:,\d{3})*|\d+(?:\.\d+)?)", text):
            num = float(m.group(1).replace(",", ""))
            if abs(num - round(num, 2)) < 1e-9:
                num = round(num, 2)
            fuzzy = any(abs(num - a) <= max(1.0, a * 0.02) for a in allowed if isinstance(a, (int, float)))
            if not fuzzy:
                suspicious.append(m.group(0))
        if len(suspicious) >= 2:
            return ""
    return text.strip()

def _analysis_data_block(new_in, dropped, store_items, summary, top_items=None):
    """AI 分析报告的完整真实数据块（统计 JSON + 变动明细 + 头部明细 + 店铺明细）。"""
    d = []
    d.append("## 本次 TOP100 快照的真实字段统计（JSON）")
    d.append(json.dumps(summary, ensure_ascii=False))
    d.append("")
    d.append("## 本次真实变动 diff：新上榜（%d 款）" % len(new_in))
    d.append(_items_block(new_in))
    d.append("")
    d.append("## 本次真实变动 diff：落榜（%d 款）" % len(dropped))
    d.append(_items_block(dropped))
    d.append("")
    d.append("## 榜单头部 TOP20（真实抓取）")
    d.append(_items_block((top_items or [])[:20]))
    d.append("")
    d.append("## 目标店铺上榜产品（%d 款）" % len(store_items))
    d.append(_items_block(store_items))
    return "\n".join(d)


def build_analysis_prompt(new_in, dropped, store_items, summary, fetched_at=None, top_items=None, store_label=None, cfg=None):
    """构建 AI 分析报告提示词：用户可编辑提示词（或内置默认）+ 真实数据块。

    容错：提示词未引用任何数据占位符时，自动在末尾追加完整真实数据块，避免模型无数据可分析。
    """
    _gen_at = now_str()
    _fetch_at = fetched_at or _gen_at
    _label = store_label or _store_label(cfg)
    tpl = _cfg_prompt(cfg, "prompt_analysis", PROMPT_ANALYSIS_DEFAULT)
    data_block = _analysis_data_block(new_in, dropped, store_items, summary, top_items=top_items)
    mapping = {
        "gen_at": _gen_at,
        "fetch_at": _fetch_at,
        "store_label": _label,
        "summary_json": json.dumps(summary, ensure_ascii=False),
        "new_in_count": str(len(new_in)),
        "dropped_count": str(len(dropped)),
        "new_in": _items_block(new_in),
        "dropped": _items_block(dropped),
        "top_items": _items_block((top_items or [])[:20]),
        "store_items": _items_block(store_items),
        "data_block": data_block,
    }
    prompt = _render_prompt_template(tpl, mapping).strip()
    if not _prompt_has_data(tpl):
        prompt = prompt + "\n\n## 本次真实数据（系统自动附加，供分析引用）\n\n" + data_block
    return prompt


# ---------------------------------------------------------------- 运营建议（规则引擎）
FEATURE_KEYWORDS = [
    ("防水", ["waterproof", "ipx", "ip67", "ipx7", "shower"]),
    ("低音", ["bass", "deep bass", "subwoofer"]),
    ("长续航", ["battery", "hours", "playtime", "续航"]),
    ("降噪", ["noise cancel", "anc"]),
    ("RGB氛围灯", ["rgb", "led light"]),
    ("无线充电", ["wireless charging", "qi"]),
    ("TWS互联", ["tws", "pair", "stereo"]),
    ("挂绳便携", ["strap", "carabiner", "clip", "portable"]),
    ("户外/露营", ["outdoor", "camping", "hiking", "beach"]),
    ("免提通话", ["handsfree", "speakerphone", "built-in mic", "call"]),
    ("多设备连接", ["multipoint", "dual connect", "2 devices"]),
    ("复古外观", ["retro", "vintage", "wood"]),
]

def _detect_features(items):
    text = " ".join((p.get("title") or "").lower() for p in items)
    hits = []
    for zh, kws in FEATURE_KEYWORDS:
        n = sum(text.count(k) for k in kws)
        if n > 0:
            hits.append((zh, n))
    hits.sort(key=lambda x: -x[1])
    return hits

def build_advice_rules(top_items, store_items, new_in, dropped, summary, cfg=None):
    """不依赖模型的规则引擎版运营建议。"""
    L = []
    tag = _store_tag(cfg)
    L.append("# %s 运营销售建议（规则引擎版 · %s）" % (tag, now_str()))
    L.append("")
    L.append("> 数据基础：本次 BSR TOP100 快照（%d 款），%s 上榜 %d 款，新上榜 %d 款，落榜 %d 款。"
             % (len(top_items), tag, len(store_items), len(new_in), len(dropped)))
    L.append("")
    cur = _cur_sym(summary)
    curw = cur if cur == "$" else (cur + " ")   # 展示用货币前缀（CNY 后补空格）

    # 1. 价格与优惠策略
    L.append("## 一、价格与优惠策略")
    if summary["price_p50"]:
        L.append("- 当前榜单价格带：P25=%s%.2f，中位数 %s%.2f，P75=%s%.2f。"
                 % (curw, summary["price_p25"], curw, summary["price_p50"], curw, summary["price_p75"]))
    else:
        L.append("- 当前榜单价格数据不足。")
    if store_items:
        cp = [p["price"] for p in store_items if p.get("price") is not None]
        if cp:
            avg = sum(cp) / len(cp)
            L.append("- %s 上榜产品平均标价 %s%.2f；若高于榜单中位数，说明处于中高端定位，需强化功能/品牌溢价理由；若低于中位数，注意避免陷入价格战。" % (tag, curw, avg))
            if summary["price_p50"] and avg > summary["price_p50"] * 1.25:
                L.append("- 建议：可在不伤品牌调性的前提下推出限时 Coupon（如 10-15% off）或捆绑促销，测试价格弹性；对标竞品头部价格后再决定是否做入门款引流。")
            elif summary["price_p50"] and avg < summary["price_p50"] * 0.75:
                L.append("- 建议：低价定位需把『性价比』写进主图与五点；同时留意低价竞品是否在评价数上形成碾压，避免只拼价格。")
    else:
        L.append("- %s 当前无上榜产品。若要回榜：先锁定 %s20-45 主流成交带（以榜单中位数 %s%.2f 为锚点）打造一至两款走量款。"
                 % (tag, curw, curw, summary["price_p50"] or 30))
    L.append("")

    # 2. 产品优化方向
    feats = _detect_features(top_items[:60])
    L.append("## 二、产品优化方向")
    L.append("- 榜单头部（TOP60）标题出现的高频卖点（按出现次数）：%s" %
             ("、".join("%s×%d" % (z, n) for z, n in feats[:6]) if feats else "暂未识别到明显功能词"))
    L.append("- 重点对标 TOP10 竞品的功能/外观组合（见界面 TOP10 列表），逐项对比：发声单元口径与低音、防水等级（IPX7 及以上是户外款标配）、续航宣传口径、是否带灯效/挂绳/免提通话等差异化点。")
    if new_in:
        L.append("- 新上榜 %d 款竞品值得快速拆解：若集中于某价格带或功能点，说明该细分正在放量，可评估快速跟进或做差异化升级。" % len(new_in))
    L.append("")

    # 3. Listing 页面优化
    L.append("## 三、Listing 页面优化")
    L.append("- 标题：参考头部竞品结构『品牌+核心功能+场景+规格参数+认证』；把最能打的差异化点放在前 80 字符内，覆盖搜索词（防水/低音/续航/便携等）。")
    L.append("- 五点描述：每点一个卖点+一个使用场景+一个数据支撑；避免纯形容词。")
    L.append("- 主图/视频：首图白底合规；副图放 15-30 秒使用场景短视频（户外/浴室/厨房）、防水实测、与竞品尺寸对比；A+ 页面用对比模块突出『价格 vs 功能』。")
    L.append("- 搜索词后端埋词：参考本榜单标题高频词（%s）。" % ("、".join(z for z, _ in feats[:10]) if feats else "见上方卖点词"))
    L.append("")

    # 4. 评价管理
    L.append("## 四、评价管理")
    if summary["rating_avg"]:
        L.append("- 当前榜单平均星级约 %.2f；%s 若低于该值，需优先排查差评集中点（续航虚标、低音失真、连接断连、品控）。" % (summary["rating_avg"], tag))
    if store_items:
        low = [p for p in store_items if (p.get("rating") or 5) < 4.0]
        if low:
            for p in low:
                L.append("- 风险提示：%s（#%s）星级仅 %s，注意近期差评趋势并针对性回复/改进。" % (p.get("title"), p.get("rank"), p.get("rating")))
    L.append("- 评论维护动作：用买家之声(Voice of the Customer)监控缺陷率；新品期通过 Vine 计划获取种子评价；对 1-3 星差评逐条分析关键词并反哺产品改进。")
    L.append("- 若竞品大量出现『XXX 功能好评』而自身 Listing 无此词，考虑补货/改款时加入该卖点并同步更新五点。")
    L.append("")

    # 5. 竞争应对
    L.append("## 五、竞争应对")
    if new_in:
        L.append("- 新上榜竞品往往带新流量，短期会分走类目转化；应对：① 对比其价格/卖点，判断是否正面拦截；② 保持自身排名稳定，用 Coupon/广告维持位置；③ 若新竞品来自头部大卖，关注其打法，避免硬碰硬。")
    if dropped:
        L.append("- 本次落榜 %d 款，提示榜单波动快：守住现有排名比冲新高更重要，建议每日固定时间记录并复盘自己的排名变化。" % len(dropped))
    L.append("- 建立竞品监控清单（工具已记录历史快照），每周看一次 TOP50 进出名单与价格带漂移。")
    L.append("")
    L.append("---")
    L.append("生成方式：内置规则引擎自动产出（未调用外部 AI）。如需更定制化的分析，请在设置中配置模型后点击『用 AI 生成增强版建议』。")
    return "\n".join(L)

def _advice_data_block(top_items, store_items, new_in, dropped, summary):
    """运营建议的完整真实数据块。"""
    d = []
    d.append("## 当前 TOP100 画像")
    d.append(json.dumps(summary, ensure_ascii=False))
    d.append("")
    d.append("## 头部竞品 TOP15")
    d.append(_items_block((top_items or [])[:15]))
    d.append("")
    d.append("## 目标店铺上榜产品")
    d.append(_items_block(store_items))
    d.append("")
    d.append("## 新上榜商品（%d）" % len(new_in))
    d.append(_items_block(new_in))
    d.append("")
    d.append("## 落榜商品（%d）" % len(dropped))
    d.append(_items_block(dropped))
    return "\n".join(d)


def build_advice_llm(top_items, store_items, new_in, dropped, summary, cfg, fetched_at=None):
    """构建运营销售建议提示词：用户可编辑提示词（或内置默认）+ 真实数据块。"""
    _gen_at = now_str()
    _fetch_at = fetched_at or _gen_at
    tpl = _cfg_prompt(cfg, "prompt_advice", PROMPT_ADVICE_DEFAULT)
    data_block = _advice_data_block(top_items, store_items, new_in, dropped, summary)
    mapping = {
        "gen_at": _gen_at,
        "fetch_at": _fetch_at,
        "store_label": _store_tag(cfg),
        "summary_json": json.dumps(summary, ensure_ascii=False),
        "new_in_count": str(len(new_in)),
        "dropped_count": str(len(dropped)),
        "new_in": _items_block(new_in),
        "dropped": _items_block(dropped),
        "top_items": _items_block((top_items or [])[:15]),
        "store_items": _items_block(store_items),
        "data_block": data_block,
    }
    prompt = _render_prompt_template(tpl, mapping).strip()
    if not _prompt_has_data(tpl):
        prompt = prompt + "\n\n## 本次真实数据（系统自动附加，供分析引用）\n\n" + data_block
    return prompt


def gen_ai_analysis_report(top, store_items, summary, new_in, dropped, cfg,
                           prog=None, do_ai=True, fetched_at=None,
                           prev_fetched_at=None, prev_snapshot_id=None, snapshot_id=None):
    """生成 AI 分析报告（真实数据模板总结 + 可选模型定性观察）并写盘 REP_DIR。

    供 gen_ai_reports（抓取流程）与 regenerate_analysis_from_latest（重跑 AI 分析）复用。
    返回 (ai_report_path, ai_text)。
    """
    def _prog(msg, pct=None):
        if prog:
            try:
                prog(msg, pct)
            except TypeError:
                prog(msg)

    _gen_at = now_str()
    summary_body = build_summary_section(new_in, dropped, top, summary,
                                         fetched_at=fetched_at, gen_at=_gen_at,
                                         prev_fetched_at=prev_fetched_at,
                                         prev_snapshot_id=prev_snapshot_id,
                                         snapshot_id=snapshot_id)
    if do_ai and cfg.get("ai_mode") != "offline":
        try:
            _prog("正在生成 AI 分析报告 ...", 86)
            prompt = build_analysis_prompt(new_in, dropped, store_items, summary,
                                           fetched_at=fetched_at, top_items=top,
                                           store_label=_store_label(cfg), cfg=cfg)
            llm_txt = _sanitize_summary_llm(llm_chat(
                cfg, "你是一名亚马逊品类数据分析师，输出严谨、简洁、数据导向。你的报告正文一律以中文为主，可中英结合但英文仅限必要专名（ASIN/品牌/店铺名/型号/单位等），严禁大段或整段英文。",
                prompt, temperature=0.3), summary, cfg)
            ai_report_path = os.path.join(REP_DIR, "ai_analysis_%s.md" % now_tag())
            if llm_txt:
                body = (summary_body + "\n\n---\n\n**AI 综合观察（模型基于上方真实字段生成的定性解读；若与模板数据冲突，一律以上方模板真实数据为准）**\n\n"
                        + llm_txt)
            else:
                body = summary_body + "\n\n---\n\n（本次 AI 观察输出未能通过真实性校验，已自动省略；以上总结内容均来自真实抓取数据。）"
            with open(ai_report_path, "w", encoding="utf-8") as f:
                f.write("# AI 榜单变动分析（生成时间 %s）\n\n%s" % (fmt_min(_gen_at), body))
            return ai_report_path, body
        except Exception as e:
            _log("AI 分析失败: %s" % e)
            try:  # 失败仍产出模板版总结，避免 AI 报告区空白
                ai_report_path = os.path.join(REP_DIR, "ai_analysis_%s.md" % now_tag())
                with open(ai_report_path, "w", encoding="utf-8") as f:
                    f.write("# AI 榜单变动分析（生成时间 %s）\n\n%s\n\n---\n\n（本次 AI 调用失败：%s；以上总结内容均来自真实抓取数据。）"
                            % (fmt_min(_gen_at), summary_body, e))
                return ai_report_path, summary_body
            except Exception as e2:
                _log("AI 报告模板写盘失败: %s" % e2)
                return None, None
    # 离线模式（或未启用 AI）：产出模板化总结（真实数据版），避免 AI 报告区空白
    try:
        ai_report_path = os.path.join(REP_DIR, "ai_analysis_%s.md" % now_tag())
        with open(ai_report_path, "w", encoding="utf-8") as f:
            f.write("# AI 榜单变动分析（生成时间 %s）\n\n%s" % (fmt_min(_gen_at), summary_body))
        return ai_report_path, summary_body
    except Exception as e:
        _log("AI 报告写盘失败: %s" % e)
        return None, None


# ---------------------------------------------------------------- 报告生成（抓取 / 快照重生成共用）
def gen_ai_reports(top, store_items, summary, new_in, dropped, cfg,
                   prog=None, do_ai=True, fetched_at=None,
                   prev_fetched_at=None, prev_snapshot_id=None, snapshot_id=None):
    """基于已就绪的榜单数据生成 AI 分析报告 + 运营建议并写盘到 REP_DIR。

    供两类流程复用：
    1) run_full_update（抓取新快照后）；
    2) regenerate_advice_from_latest（基于最近快照重生成，不抓页面）。
    返回 (ai_report_path, ai_text, advice_path, advice_text)。
    """
    def _prog(msg, pct=None):
        if prog:
            try:
                prog(msg, pct)
            except TypeError:
                prog(msg)

    ai_report_path, ai_text = gen_ai_analysis_report(
        top, store_items, summary, new_in, dropped, cfg,
        prog=prog, do_ai=do_ai, fetched_at=fetched_at,
        prev_fetched_at=prev_fetched_at, prev_snapshot_id=prev_snapshot_id,
        snapshot_id=snapshot_id)

    advice_path = None
    advice_text = None
    try:
        if cfg.get("ai_mode") != "offline":
            prompt = build_advice_llm(top, store_items, new_in, dropped, summary, cfg,
                                      fetched_at=fetched_at)
            advice_text = _dedupe_llm_text(llm_chat(
                cfg, "你是资深亚马逊美国站运营专家，输出可执行的中文建议，逻辑清晰。建议正文一律以中文为主，可中英结合但英文仅限必要专名（ASIN/品牌/店铺名/型号/单位等），严禁大段或整段英文。",
                prompt, temperature=0.4))
            if not advice_text:
                raise AmazonFetchError("ai", "AI 返回内容为空，回退规则引擎")
            advice_path = os.path.join(REP_DIR, "advice_%s.md" % now_tag())
            with open(advice_path, "w", encoding="utf-8") as f:
                f.write("# %s 运营销售建议（AI 增强版 · %s）\n\n%s" % (_store_tag(cfg), now_str(), advice_text))
        else:
            advice_text = build_advice_rules(top, store_items, new_in, dropped, summary, cfg)
            advice_path = os.path.join(REP_DIR, "advice_%s.md" % now_tag())
            with open(advice_path, "w", encoding="utf-8") as f:
                f.write(advice_text)
    except Exception as e:
        advice_text = build_advice_rules(top, store_items, new_in, dropped, summary, cfg)
        advice_path = os.path.join(REP_DIR, "advice_%s.md" % now_tag())
        with open(advice_path, "w", encoding="utf-8") as f:
            f.write(advice_text)
        _log("AI 建议失败，已回退规则引擎: %s" % e)
    return ai_report_path, ai_text, advice_path, advice_text


def regenerate_analysis_from_latest(cfg=None, progress_cb=None):
    """基于最近一次快照，用当前生效提示词重新生成 AI 分析报告。

    不抓页面、不新增快照、不写 history.json / latest.json，也不重算运营建议；
    对应前端顶部「生成 AI 分析」按钮。
    """
    cfg = cfg or load_config()
    ensure_dirs()
    with LOCK:
        if STATE["busy"]:
            return {"ok": False, "message": "已有任务在运行，请稍候"}
        STATE["busy"] = True
        STATE["busy_msg"] = "正在读取最近快照..."
    try:
        def prog(msg, pct=None):
            if progress_cb:
                try:
                    progress_cb(msg, pct)
                except TypeError:
                    progress_cb(msg)
            with LOCK:
                STATE["busy_msg"] = msg

        latest = load_latest()
        if not latest or not latest.get("items"):
            return {"ok": False, "message": "请先抓取一次榜单"}
        sid = latest.get("snapshot_id")
        fetched_at = latest.get("fetched_at") or now_str()
        prog("读取最近快照，对历史快照回填缺失字段（兼容中文站）...", 12)
        backfill_snapshot_fields(latest, write_back=True)
        items = list(latest.get("items") or [])
        prog("读取最近快照，识别目标店铺商品与市场画像 ...", 15)
        annotate_store(items, cfg)      # 快照可能早于关键词配置更新，重新打标
        store_items = [p for p in items if p.get("is_store")]
        summary = market_summary(items)
        # 上一份快照：只读对比，不写任何历史
        history = _read_json(HISTORY_PATH, []) or []
        prev_sid = None
        for h in history:
            if h.get("snapshot_id") != sid:
                prev_sid = h.get("snapshot_id")
                break
        old = load_snapshot(prev_sid) if prev_sid else None
        new_in, dropped = [], []
        if old:
            prog("正在与上一份快照对比变动 ...", 30)
            backfill_snapshot_fields(old)   # 旧快照同样回填，保证落榜明细字段完整
            new_in, dropped = diff_snapshots(old, latest)
        prog("正在使用当前提示词生成 AI 分析报告 ...", 60)
        ai_report_path, ai_text = gen_ai_analysis_report(
            items, store_items, summary, new_in, dropped, cfg, prog=prog,
            do_ai=True, fetched_at=fetched_at,
            prev_fetched_at=(snapshot_display_time(old) if old else None),
            prev_snapshot_id=(old.get("snapshot_id") if old else None),
            snapshot_id=sid)
        with LOCK:
            STATE["last_error"] = None
        prev_res = STATE.get("last_result") or {}
        prog("完成，正在汇总结果 ...", 99)
        _log("AI 分析重生成完成: 基于快照 %s, 提示词来源=%s"
             % (sid, "用户自定义" if str(cfg.get("prompt_analysis") or "").strip() else "内置默认"))
        return {
            "ok": True,
            "snapshot_id": sid,
            "fetched_at": fetched_at,
            "count": len(items),
            "store_items": store_items,
            "store_count": len(store_items),
            "summary": summary,
            "top_items": items,
            "new_in": new_in,
            "dropped": dropped,
            "diff": None,
            "has_prev": bool(old),
            "prev_snapshot_id": (old.get("snapshot_id") if old else None),
            "prev_fetched_at": (snapshot_display_time(old) if old else None),
            "report_gen_at": now_str(),
            "ai_report_path": ai_report_path,
            "ai_text": ai_text,
            "advice_path": prev_res.get("advice_path"),
            "advice_text": prev_res.get("advice_text"),
            "ai_mode": cfg.get("ai_mode"),
            "fetch_mode_used": "snapshot_reuse",
            "fetch_notice": "本次为基于最近快照重新生成 AI 分析，未重新抓取页面，未改动其他区块。",
            "regen_analysis_only": True,
        }
    except AmazonFetchError as e:
        with LOCK:
            STATE["last_error"] = str(e)
        return {"ok": False, "error_kind": e.kind, "message": str(e)}
    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            STATE["last_error"] = str(e)
        return {"ok": False, "error_kind": "internal", "message": "内部错误: %s" % e}
    finally:
        with LOCK:
            STATE["busy"] = False
            STATE["busy_msg"] = ""


def regenerate_advice_from_latest(cfg=None, progress_cb=None):
    """基于最近一次快照重新生成 AI 分析报告 + 运营建议。

    不抓页面、不新增快照、不写 history.json / latest.json，也不污染历史对比；
    网络不通时不再卡在“打开第一页”。对应前端「生成运营建议(再跑一次AI)」。
    """
    cfg = cfg or load_config()
    ensure_dirs()
    with LOCK:
        if STATE["busy"]:
            return {"ok": False, "message": "已有任务在运行，请稍候"}
        STATE["busy"] = True
        STATE["busy_msg"] = "正在读取最近快照..."
    try:
        def prog(msg, pct=None):
            if progress_cb:
                try:
                    progress_cb(msg, pct)
                except TypeError:
                    progress_cb(msg)
            with LOCK:
                STATE["busy_msg"] = msg

        latest = load_latest()
        if not latest or not latest.get("items"):
            return {"ok": False, "message": "请先抓取一次榜单"}
        items = list(latest.get("items") or [])
        sid = latest.get("snapshot_id")
        fetched_at = latest.get("fetched_at") or now_str()
        prog("读取最近快照，对历史快照回填缺失字段（兼容中文站）...", 12)
        backfill_snapshot_fields(latest, write_back=True)
        items = list(latest.get("items") or [])
        prog("读取最近快照，识别目标店铺商品与市场画像 ...", 15)
        annotate_store(items, cfg)      # 快照可能早于关键词配置更新，重新打标
        store_items = [p for p in items if p.get("is_store")]
        summary = market_summary(items)
        # 上一份快照：只读对比，不写任何历史
        history = _read_json(HISTORY_PATH, []) or []
        prev_sid = None
        for h in history:
            if h.get("snapshot_id") != sid:
                prev_sid = h.get("snapshot_id")
                break
        old = load_snapshot(prev_sid) if prev_sid else None
        new_in, dropped = [], []
        if old:
            prog("正在与上一份快照对比变动 ...", 30)
            backfill_snapshot_fields(old)   # 旧快照同样回填，保证落榜明细字段完整
            new_in, dropped = diff_snapshots(old, latest)
        prog("正在生成 AI 分析报告与运营建议 ...", 60)
        _prev_time = snapshot_display_time(old) if old else None
        ai_report_path, ai_text, advice_path, advice_text = gen_ai_reports(
            items, store_items, summary, new_in, dropped, cfg, prog=prog,
            do_ai=True, fetched_at=fetched_at,
            prev_fetched_at=_prev_time,
            prev_snapshot_id=(old.get("snapshot_id") if old else None),
            snapshot_id=sid)
        with LOCK:
            STATE["last_error"] = None
        prog("完成，正在汇总结果 ...", 99)
        _log("快照重生成完成: 基于快照 %s, %s=%d, 新上榜=%d, 落榜=%d"
             % (sid, _store_tag(cfg), len(store_items), len(new_in), len(dropped)))
        return {
            "ok": True,
            "snapshot_id": sid,
            "fetched_at": fetched_at,
            "count": len(items),
            "store_items": store_items,
            "store_count": len(store_items),
            "summary": summary,
            "top_items": items,
            "new_in": new_in,
            "dropped": dropped,
            "diff": None,
            "has_prev": bool(old),
            "prev_snapshot_id": (old.get("snapshot_id") if old else None),
            "prev_fetched_at": _prev_time,
            "report_gen_at": now_str(),
            "ai_report_path": ai_report_path,
            "ai_text": ai_text,
            "advice_path": advice_path,
            "advice_text": advice_text,
            "ai_mode": cfg.get("ai_mode"),
            "fetch_mode_used": "snapshot_reuse",
            "fetch_notice": "本次为基于最近快照重新生成，未重新抓取页面。",
            "regen_from_snapshot": True,
        }
    except AmazonFetchError as e:
        with LOCK:
            STATE["last_error"] = str(e)
        return {"ok": False, "error_kind": e.kind, "message": str(e)}
    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            STATE["last_error"] = str(e)
        return {"ok": False, "error_kind": "internal", "message": "内部错误: %s" % e}
    finally:
        with LOCK:
            STATE["busy"] = False
            STATE["busy_msg"] = ""


# ---------------------------------------------------------------- 核心业务流程
def run_full_update(source="manual", cfg=None, do_ai=None, progress_cb=None):
    """执行：抓取→保存→对比→(可选)AI分析→返回汇总。do_ai: None=跟随配置。"""
    cfg = cfg or load_config()
    ensure_dirs()
    if do_ai is None:
        do_ai = cfg.get("ai_mode") != "offline"
    with LOCK:
        if STATE["busy"]:
            return {"ok": False, "message": "已有抓取任务在运行，请稍候"}
        STATE["busy"] = True
        STATE["busy_msg"] = "开始抓取 BSR TOP100 ..."
    try:
        def prog(msg, pct=None):
            if progress_cb:
                try:
                    progress_cb(msg, pct)
                except TypeError:
                    progress_cb(msg)
            with LOCK:
                STATE["busy_msg"] = msg
        prog("任务启动，正在请求榜单页面（按抓取模式执行，约需 20-120 秒）...", 1)
        top = fetch_top100(cfg, progress_cb=progress_cb)
        fetch_meta = _get_fetch_meta()
        prog("榜单抓取完成，正在识别目标店铺商品并统计市场画像 ...", 58)
        top = annotate_store(top, cfg)
        summary = market_summary(top)
        store_items = [p for p in top if p.get("is_store")]
        # 保存快照
        prog("正在保存本次快照到本地 data/ ...", 66)
        sid, rec = save_snapshot(top, cfg, source)
        old = None
        history = _read_json(HISTORY_PATH, [])
        if len(history) >= 2:
            # 找上一份（排除刚写入的第一条）
            prev = history[1]
            old = load_snapshot(prev.get("snapshot_id"))
        new_in, dropped = [], []
        diff_rec = None
        if old:
            prog("正在对比上次快照，识别新上榜 / 落榜 ...", 76)
            new_in, dropped = diff_snapshots(old, rec)
            diff_rec, jp, cp = save_diff(sid, old, rec, new_in, dropped, cfg)
        else:
            diff_rec = {"new_in": [], "dropped": [], "base_snapshot": None,
                        "new_snapshot": sid, "new_in_count": 0, "dropped_count": 0,
                        "compared_at": now_str()}
        # AI 分析报告 + 运营建议（抽成公共生成函数，供抓取与快照重生成复用）
        _prev_time = snapshot_display_time(old) if old else None
        ai_report_path, ai_text, advice_path, advice_text = gen_ai_reports(
            top, store_items, summary, new_in, dropped, cfg,
            prog=prog, do_ai=bool(do_ai), fetched_at=rec.get("fetched_at"),
            prev_fetched_at=_prev_time,
            prev_snapshot_id=(old.get("snapshot_id") if old else None),
            snapshot_id=sid)
        with LOCK:
            STATE["last_fetch"] = now_str()
            STATE["last_snapshot_id"] = sid
            STATE["last_store_items"] = store_items
            STATE["last_error"] = None
        prog("更新完成，正在汇总结果 ...", 99)
        _log("更新完成: TOP100=%d, %s=%d, 新上榜=%d, 落榜=%d"
             % (len(top), _store_tag(cfg), len(store_items), len(new_in), len(dropped)))
        return {
            "ok": True,
            "snapshot_id": sid,
            "fetched_at": rec.get("fetched_at") or now_str(),
            "report_gen_at": now_str(),
            "prev_snapshot_id": (old.get("snapshot_id") if old else None),
            "prev_fetched_at": _prev_time,
            "count": len(top),
            "store_items": store_items,
            "store_count": len(store_items),
            "summary": summary,
            "top_items": top,
            "new_in": new_in,
            "dropped": dropped,
            "diff": diff_rec,
            "has_prev": bool(old),
            "ai_report_path": ai_report_path,
            "ai_text": ai_text,
            "advice_path": advice_path,
            "advice_text": advice_text,
            "ai_mode": cfg.get("ai_mode"),
            "fetch_mode_used": fetch_meta.get("used", "http"),
            "fetch_notice": fetch_meta.get("notice", ""),
        }
    except AmazonFetchError as e:
        with LOCK:
            STATE["last_error"] = str(e)
        return {"ok": False, "error_kind": e.kind, "message": str(e)}
    except Exception as e:
        import traceback
        traceback.print_exc()
        with LOCK:
            STATE["last_error"] = str(e)
        return {"ok": False, "error_kind": "internal", "message": "内部错误: %s" % e}
    finally:
        with LOCK:
            STATE["busy"] = False
            STATE["busy_msg"] = ""

# ---------------------------------------------------------------- 导出
def export_result(result, kind="summary", cfg=None):
    ensure_dirs()
    ts = now_tag()
    if kind == "store_csv":
        path = os.path.join(EXP_DIR, "%s_top100_%s.csv" % (_store_file_prefix(cfg), ts))
        items = result.get("store_items") or []
        with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["BSR排名", "ASIN", "产品标题", "价格", "星级", "评论数", "店铺", "产品链接"])
            for p in items:
                w.writerow([p.get("rank"), p.get("asin"), p.get("title"),
                            _fmt_price(p.get("price"), p.get("price_currency"), p.get("price_kind")),
                            p.get("rating"), p.get("reviews"),
                            p.get("store_name") or "", p.get("url") or ""])
        return path
    if kind == "changes_csv":
        path = os.path.join(EXP_DIR, "changes_%s.csv" % ts)
        with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["变动类型", "BSR排名", "ASIN", "产品标题", "价格", "星级", "评论数", "店铺", "产品链接"])
            for p in (result.get("new_in") or []):
                w.writerow(["新上榜", p.get("rank"), p.get("asin"), p.get("title"),
                            _fmt_price(p.get("price"), p.get("price_currency"), p.get("price_kind")),
                            p.get("rating"), p.get("reviews"),
                            p.get("store_name") or "", p.get("url") or ""])
            for p in (result.get("dropped") or []):
                w.writerow(["落榜", p.get("rank"), p.get("asin"), p.get("title"),
                            _fmt_price(p.get("price"), p.get("price_currency"), p.get("price_kind")),
                            p.get("rating"), p.get("reviews"),
                            p.get("store_name") or "", p.get("url") or ""])
        return path
    if kind == "summary_txt":
        path = os.path.join(EXP_DIR, "summary_%s.txt" % ts)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write("%s BSR TOP100 汇总\n抓取时间: %s\n\n" % (_store_label(cfg), result.get("fetched_at", now_str())))
            f.write("%s 上榜产品数: %d\n\n" % (_store_label(cfg), len(result.get("store_items") or [])))
            for p in (result.get("store_items") or []):
                f.write("#%s  %s\nASIN: %s\n价格: %s | 星级: %s | 评论: %s\n链接: %s\n\n"
                        % (p.get("rank"), p.get("title"), p.get("asin"),
                           _fmt_price(p.get("price"), p.get("price_currency"), p.get("price_kind")),
                           p.get("rating"), p.get("reviews"),
                           p.get("url") or ""))
        return path
    if kind == "advice_md":
        path = os.path.join(EXP_DIR, "%s_advice_%s.md" % (_store_file_prefix(cfg), ts))
        text = result.get("advice_text") or "无"
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    if kind == "ai_md":
        path = os.path.join(EXP_DIR, "ai_analysis_%s.md" % ts)
        text = result.get("ai_text") or "无"
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    return None

# ---------------------------------------------------------------- 定时抓取
def scheduler_loop(cfg_holder, stop_event):
    """cfg_holder: dict with 'cfg' key 可热更新"""
    while not stop_event.is_set():
        time.sleep(20)
        cfg = load_config()
        minutes = int(cfg.get("schedule_minutes") or 0)
        if minutes <= 0:
            continue
        last = STATE.get("schedule_last_ts")
        now = time.time()
        if last is None or (now - last) >= minutes * 60:
            if STATE["busy"]:
                continue
            STATE["schedule_last_ts"] = now
            _log("定时抓取触发（间隔 %d 分钟）..." % minutes)
            run_full_update(source="schedule", cfg=cfg)

# ---------------------------------------------------------------- HTTP 服务
HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>亚马逊 BSR TOP100 店铺监控台</title>
<style>
:root{--bg:#edf1f8;--card:#fff;--line:#e3e9f3;--txt:#16233c;--sub:#64748b;--brand:#d6382c;--ok:#0f9d58;--warn:#d97706;--bad:#dc2626;--blue:#2563eb;
--shadow:0 2px 12px rgba(23,35,59,.06);--shadow-lg:0 10px 30px rgba(23,35,59,.12);--radius:16px}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;font-size:14px;line-height:1.65}
.wrap{max-width:1280px;margin:0 auto;padding:20px 18px 70px}
header.dash-head{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;padding:20px 24px;margin-bottom:18px;border-radius:20px;color:#fff;background:linear-gradient(118deg,#0f1f3d,#1d3b70 58%,#31589f);box-shadow:var(--shadow-lg)}
header.dash-head h1{font-size:22px;letter-spacing:.6px}
.badge{font-size:12px;color:#b8c7e8}
.subline{font-size:12px;color:#c9d6f2;margin-top:5px}
.topmsg{margin-bottom:14px}
.topbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:4px 0 16px}
button{background:#fff;border:1px solid var(--line);color:var(--txt);padding:8px 16px;border-radius:10px;cursor:pointer;font-size:13px;box-shadow:var(--shadow);transition:all .18s}
button:hover{border-color:#b8c6dd;transform:translateY(-1px)}
button:active{transform:translateY(0)}
button.primary{background:linear-gradient(120deg,var(--brand),#ef6a3e);border-color:transparent;color:#fff;font-weight:600}
button.danger{color:#dc2626;border-color:#f0c4c4}
button.danger:hover{background:#fdeaea;border-color:#eebcbc}
button.primary:disabled{opacity:.6;cursor:not-allowed;transform:none;box-shadow:none}
button.primary.loading{background:linear-gradient(120deg,#9b1f16,#cf4a2b);cursor:wait}
button.small{padding:4px 10px;font-size:12px;border-radius:8px}
textarea.prompt-box{width:100%;min-height:160px;font-family:Consolas,"Courier New",monospace;font-size:12px;line-height:1.55;padding:10px;border:1px solid var(--line);border-radius:10px;background:#fbfcfe;color:var(--txt);resize:vertical}
.ph-code{font-family:Consolas,"Courier New",monospace;background:#f1f5fb;border:1px solid var(--line);border-radius:6px;padding:1px 5px;font-size:11.5px}
button.ghost{background:transparent;border:none;color:var(--blue);padding:0;font-size:12px;box-shadow:none}
button.ghost:hover{border:none;transform:none;text-decoration:underline}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:18px;margin-bottom:16px;box-shadow:var(--shadow)}
.card h2{font-size:15px;margin-bottom:12px;display:flex;align-items:center;gap:8px;padding-bottom:10px;border-bottom:1px dashed var(--line)}
.card h2::before{content:"";width:4px;height:15px;border-radius:3px;background:linear-gradient(180deg,var(--blue),#60a5fa)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat{background:#f8fafd;border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stat .n{font-size:24px;font-weight:700}
.stat .l{color:var(--sub);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:7px 8px;text-align:left;vertical-align:top;word-break:break-all}
th{background:#f6f8fc;color:var(--sub);font-weight:600;white-space:nowrap}
tr:hover td{background:#fafcff}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
.muted{color:var(--sub)}
.empty{color:var(--sub);padding:8px 0}
.tag{display:inline-block;padding:1px 9px;border-radius:12px;font-size:12px;margin-right:6px}
.tag.up{background:#e5f6ed;color:var(--ok)}
.tag.down{background:#fdeaea;color:var(--bad)}
.note{background:#fff8e8;border:1px solid #f0dfae;border-radius:10px;padding:10px 14px;color:#6d5510;font-size:13px;margin-bottom:14px}
.errbox{background:#fdeaea;border:1px solid #eebcbc;border-radius:10px;padding:10px 14px;color:#8c2626;margin-bottom:14px;font-size:13px}
.okbox{background:#e5f6ed;border:1px solid #b5e2c8;border-radius:10px;padding:10px 14px;color:#146b41;margin-bottom:14px;font-size:13px}
pre.article{white-space:pre-wrap;word-break:break-word;font-family:"Microsoft YaHei",sans-serif;font-size:13px;background:#f8fafd;border:1px solid var(--line);border-radius:12px;padding:14px;max-height:560px;overflow:auto}
select,input[type=text],input[type=password],input[type=number]{border:1px solid var(--line);border-radius:10px;padding:6px 10px;font-size:13px;width:100%}
.f2{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
label{display:block;font-size:12px;color:var(--sub);margin:6px 0 3px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.hidden{display:none}
#toast{position:fixed;top:16px;right:16px;background:#1c2b47;color:#fff;padding:11px 18px;border-radius:12px;font-size:13px;opacity:0;transition:opacity .25s;pointer-events:none;max-width:440px;z-index:99;box-shadow:var(--shadow-lg)}
.tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);margin-bottom:16px;flex-wrap:wrap}
.tab{padding:9px 16px;cursor:pointer;border-bottom:3px solid transparent;color:var(--sub);font-size:13px;border-radius:8px 8px 0 0}
.tab:hover{background:#f1f5fb;color:var(--txt)}
.tab.on{color:var(--brand);border-color:var(--brand);font-weight:600}
.sec{display:none}.sec.on{display:block}
table.wrap-td td{white-space:normal}
.mono{font-family:Consolas,monospace;font-size:12px}
/* ===== 销售看板组件 ===== */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:16px}
.kpi{position:relative;overflow:hidden;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:16px 18px 14px;box-shadow:var(--shadow)}
.kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--kc,#2563eb)}
.kpi .k-label{font-size:12px;color:var(--sub);display:flex;align-items:center;gap:6px}
.kpi .k-num{font-size:30px;font-weight:800;line-height:1.25;margin-top:4px;color:var(--txt)}
.kpi .k-sub{font-size:12px;color:#94a3b8;margin-top:3px}
.kpi.kp-store{--kc:#d6382c}.kpi.kp-new{--kc:#0f9d58}.kpi.kp-drop{--kc:#dc2626}.kpi.kp-total{--kc:#2563eb}.kpi.kp-time{--kc:#8b5cf6}
.kpi .trend{font-size:12px;font-weight:600;margin-left:2px}
.kpi .trend.up{color:var(--ok)}.kpi .trend.down{color:var(--bad)}
.panel-progress{background:linear-gradient(180deg,#ffffff,#fbfcff);border:1px solid var(--line);border-radius:var(--radius);padding:16px 20px;margin-bottom:16px;box-shadow:var(--shadow)}
.pp-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.pp-title{font-size:14px;font-weight:700;display:flex;align-items:center;gap:8px}
.spinner{display:inline-block;width:15px;height:15px;border:2.5px solid rgba(37,99,235,.22);border-top-color:var(--blue);border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.spinner.hidden{display:none!important}
.pp-stage{font-size:13px;color:var(--sub)}
.prog-track{height:10px;background:#e7ecf4;border-radius:8px;overflow:hidden}
.prog-fill{height:100%;width:0%;border-radius:8px;background:linear-gradient(90deg,#2563eb,#38bdf8,#0f9d58);background-size:200% 100%;transition:width .5s ease;animation:flow 1.4s linear infinite}
@keyframes flow{to{background-position:-200% 0}}
.prog-meta{display:flex;justify-content:space-between;align-items:center;margin-top:8px;font-size:12px;color:var(--sub)}
.prog-pct{font-weight:800;color:var(--blue);font-size:16px}
.steps-bar{display:flex;align-items:center;gap:4px;margin:12px 0 2px;flex-wrap:wrap}
.step{display:flex;align-items:center;gap:6px;font-size:12px;color:#94a3b8;padding:4px 8px;border-radius:20px}
.step .dot{width:20px;height:20px;border-radius:50%;background:#e7ecf4;color:#7b8794;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex:none}
.step.active{background:#eaf1ff;color:#1e4fd8;font-weight:700}
.step.active .dot{background:var(--blue);color:#fff;animation:pulse 1.1s ease infinite}
@keyframes pulse{50%{box-shadow:0 0 0 5px rgba(37,99,235,.15)}}
.step.done{color:#0f9d58;font-weight:600}
.step.done .dot{background:var(--ok);color:#fff}
.step .arr{color:#cbd5e1;margin:0 2px}
.pbar{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:12px}
.pbar-track{flex:1;max-width:340px;height:10px;background:#e7ecf4;border-radius:8px;overflow:hidden}
.pbar-fill{height:100%;border-radius:8px;background:linear-gradient(90deg,#2563eb,#38bdf8)}
</style>
</head>
<body>
<div id="toast"></div>
<div class="wrap">
<header class="dash-head">
  <div>
    <h1 id="headTitle">亚马逊 BSR TOP100 销售看板</h1>
    <div class="subline">品类：便携蓝牙音箱 / 美国站 / 数据仅存本机 · 本地监控台</div>
  </div>
  <span class="badge" id="headLastFetch">尚未抓取</span>
</header>
<!-- 顶部指标卡（销售看板 KPI） -->
<div class="kpi-grid" id="kpiRow">
  <div class="kpi kp-store"><div class="k-label" id="kpiStoreLabel">目标店铺上榜</div><div class="k-num" id="kpiStore">—</div><div class="k-sub">BSR TOP100 上榜款数</div></div>
  <div class="kpi kp-new"><div class="k-label">新上榜</div><div class="k-num" id="kpiNew">—</div><div class="k-sub">对比上一快照新入榜</div></div>
  <div class="kpi kp-drop"><div class="k-label">落榜</div><div class="k-num" id="kpiDrop">—</div><div class="k-sub">对比上一快照跌出榜</div></div>
  <div class="kpi kp-total"><div class="k-label">榜单抓取</div><div class="k-num" id="kpiTotal">—</div><div class="k-sub">本次 TOP100 有效商品</div></div>
  <div class="kpi kp-time"><div class="k-label">最后更新时间</div><div class="k-num" id="kpiTime" style="font-size:19px;padding-top:8px">—</div><div class="k-sub" id="kpiTimeSub">尚未执行抓取</div></div>
</div>
<!-- 抓取进度面板 -->
<div class="panel-progress hidden" id="fetchPanel">
  <div class="pp-head">
    <span class="pp-title"><span class="spinner" id="progSpinner"></span><span id="progTitle">抓取更新中</span></span>
    <span class="pp-stage" id="progStage">准备中...</span>
  </div>
  <div class="prog-track"><div class="prog-fill" id="progBar"></div></div>
  <div class="prog-meta"><span class="steps-bar" id="stepsBar"></span><span class="prog-pct" id="progPct">0%</span></div>
  <div id="progSummary"></div>
</div>
<div id="topmsg" class="topmsg"></div>
<div class="topbar">
  <button class="primary" id="btnFetch">立即抓取更新</button>
  <button id="btnExportStore">导出上榜CSV</button>
  <button id="btnExportChanges">导出变动CSV</button>
  <button id="btnAdvice">生成运营建议(再跑一次AI)</button>
  <button id="btnGenAnalysis">生成 AI 分析</button>
  <span class="muted" id="fetchStatus"></span>
</div>

<div class="tabs">
  <div class="tab on" data-t="sec1" id="tabStore">① 目标店铺上榜汇总</div>
  <div class="tab" data-t="sec2">② 榜单变动对比</div>
  <div class="tab" data-t="sec3">③ AI分析报告</div>
  <div class="tab" data-t="sec4">④ 运营销售建议</div>
  <div class="tab" data-t="sec5">⑤ 设置</div>
</div>

<!-- ① 目标店铺上榜汇总 -->
<div class="sec on" id="sec1">
  <div class="card">
    <h2 id="sec1Title">目标店铺上榜汇总</h2>
    <div class="note">判定依据：商品卡内出现店铺链接/店铺名/标题含关键词「<span id="kwShow">未设置</span>」。仅公开榜单页数据，个别未渲染店铺名的卡片可能漏判。</div>
    <div class="grid" id="storeStats"></div>
    <div id="storeList" style="margin-top:12px"></div>
  </div>
  <div class="card">
    <h2>TOP100 榜单整体画像（本次快照）</h2>
    <div class="note hidden" id="topCovNote"></div>
    <div class="grid" id="marketStats"></div>
    <div id="marketExtra" style="margin-top:12px"></div>
    <div style="margin-top:12px" class="muted">以下为 TOP10 快速参考：</div>
    <div id="top10" style="margin-top:6px"></div>
  </div>
</div>

<!-- ② 变动 -->
<div class="sec" id="sec2">
  <div class="card">
    <h2>最近一次快照对比</h2>
    <div class="note" style="font-size:12px;padding:6px 12px;margin-bottom:10px">说明：本区变动仅统计相对基准快照的 ASIN 集合差 —— 新进入榜单（新上榜）与跌出榜单（落榜），不涉及 TOP100 内部商品的排名升降。</div>
    <div id="diffInfo"></div>
    <div id="diffBody"></div>
  </div>
  <div class="card">
    <h2>历史快照</h2>
    <div class="muted" style="margin-bottom:8px">点击两条记录可回溯任意两次对比（第1行为基准、第2行为目标）。</div>
    <table><thead><tr><th>抓取时间</th><th>上榜数</th><th>目标店铺</th><th>文件</th><th>操作</th></tr></thead>
    <tbody id="histBody"></tbody></table>
  </div>
</div>

<!-- ③ AI -->
<div class="sec" id="sec3">
  <div class="card">
    <h2>AI 分析报告</h2>
    <div class="muted" id="aiMeta"></div>
    <pre class="article" id="aiReport" style="margin-top:10px">尚未生成。点击上方「立即抓取更新」后，若已配置模型会自动分析；如需用当前提示词重新生成，可点击顶部「生成 AI 分析」（提示词可在 ⑤ 设置 →「提示词设置」中修改），该操作不会覆盖抓取结果与其他区块。</pre>
  </div>
</div>
<!-- ④ 建议 -->
<div class="sec" id="sec4">
  <div class="card">
    <h2 id="sec4Title">目标店铺运营销售建议</h2>
    <div class="muted" id="advMeta"></div>
    <pre class="article" id="advReport" style="margin-top:10px">抓取后自动生成（配置模型则用 AI 增强版，否则用内置规则引擎）。也可点击顶部「生成运营建议」用当前快照与当前提示词重新生成；提示词可在 ⑤ 设置 →「提示词设置」中修改。</pre>
    <div style="margin-top:10px"><button id="btnExportAdvice">导出建议 MD</button></div>
  </div>
</div>

<!-- ⑤ 设置 -->
<div class="sec" id="sec5">
  <div class="card">
    <h2>店铺设置（目标店铺）</h2>
    <div class="f2">
      <div><label>店铺名称（看板标题 / 导出文件名 / 报告标题使用；留空则取首个关键词）</label><input type="text" id="cfgStoreName" placeholder="例如：MyBrand">
        <div style="margin-top:6px"><button id="btnSaveStore">保存店铺设置</button> <span class="muted" id="storeSaveMsg"></span></div></div>
      <div><label>店铺关键词（逗号分隔，小写匹配；商品卡店铺名 / 链接 / 标题含任一关键词即判定上榜）</label><input type="text" id="cfgKws" placeholder="例如：mybrand, my brand"></div>
      <div><label>店铺主页 marker（链接中含有的 store ID，可留空）</label><input type="text" id="cfgSid" placeholder="例如：8FC1CC6E-..."></div>
    </div>
    <div class="note" style="margin-top:10px">保存后立即生效：看板标题、上榜汇总、导出文件名与报告标题均按该店铺名呈现；未设置时显示“未设置目标店铺”，不影响抓取与导出。</div>
  </div>
  <div class="card">
    <h2>AI 模型配置</h2>
    <div class="row" style="margin-bottom:8px">
      <label style="display:inline"><input type="radio" name="aiMode" value="offline"> 离线模板（不调用模型）</label>&nbsp;
      <label style="display:inline"><input type="radio" name="aiMode" value="ollama"> 本地 Ollama</label>&nbsp;
      <label style="display:inline"><input type="radio" name="aiMode" value="apikey"> OpenAI 兼容 API</label>
    </div>
    <div class="f2">
      <div><label>Base URL</label><input type="text" id="cfgBase"></div>
      <div><label>Model</label><input type="text" id="cfgModel"></div>
      <div><label>API Key（留空则不携带）</label><input type="password" id="cfgKey"></div>
      <div><label>单次最大生成长度 max_tokens（0=不传，用服务端默认；默认8192）</label><input type="number" id="cfgMaxTokens" min="0" step="1"></div>
      <div><label>上下文窗口 num_ctx（仅本地 Ollama 生效；默认16384）</label><input type="number" id="cfgNumCtx" min="0" step="1"></div>
    </div>
    <div class="row" style="margin-top:8px"><button id="btnTestAi">测试连接</button><button id="btnSaveAi">保存配置</button><span class="muted" id="aiTestMsg"></span></div>
  </div>
  <div class="card">
    <h2>提示词设置（可编辑）</h2>
    <div class="note">下面两段分别是「AI 分析报告」和「运营销售建议」实际发送给模型的提示词，可直接修改，点「保存提示词」后立即生效；点「恢复默认」一键还原内置预设。未填写或清空时自动回退内置默认提示词。</div>
    <div class="note" style="margin-top:6px">可用变量占位符（生成时自动替换为本次真实数据）：<span id="phHelp"></span></div>
    <div style="margin-top:12px">
      <label>AI 分析报告提示词（prompt_analysis）</label>
      <textarea id="cfgPromptAnalysis" class="prompt-box" spellcheck="false" placeholder="留空则使用内置默认提示词"></textarea>
      <div class="row" style="margin-top:6px"><button class="small" id="btnResetAnalysis">恢复默认（分析）</button><span class="muted" id="promptAnalysisMsg"></span></div>
    </div>
    <div style="margin-top:14px">
      <label>运营销售建议提示词（prompt_advice）</label>
      <textarea id="cfgPromptAdvice" class="prompt-box" spellcheck="false" placeholder="留空则使用内置默认提示词"></textarea>
      <div class="row" style="margin-top:6px"><button class="small" id="btnResetAdvice">恢复默认（建议）</button><span class="muted" id="promptAdviceMsg"></span></div>
    </div>
    <div class="row" style="margin-top:12px"><button class="primary" id="btnSavePrompts">保存提示词</button><span class="muted" id="promptSaveMsg"></span></div>
    <div class="note" style="margin-top:8px">容错说明：若提示词中未包含任何数据占位符（如 {summary_json} / {data_block}），生成时会自动在末尾追加完整真实数据块，确保模型有数据可分析；若提示词中写了占位符，则按占位符原位替换，不会重复追加。</div>
  </div>
  <div class="card">
    <h2>抓取与代理</h2>
    <div class="f2">
      <div><label>定时自动抓取间隔（分钟，≥30，0=关闭；仅程序运行期间生效）</label><input type="number" id="cfgSched" min="0" step="1"></div>
      <div><label>HTTP 代理（可选，如 http://127.0.0.1:7890）</label><input type="text" id="cfgProxy"></div>
      <div><label>抓取模式（榜单页默认每页仅渲染约30款，HTTP 最多约60款；浏览器滚动可抓满100）</label>
        <select id="cfgFetchMode">
          <option value="auto">自动（推荐）：浏览器滚动优先，失败自动回退 HTTP</option>
          <option value="scroll">浏览器滚动：用本机 Edge/Chrome 滚动抓满 100 款</option>
          <option value="http">HTTP 直抓：不启动浏览器（最多约 60 款）</option>
        </select>
      </div>
    </div>
    <div class="row" style="margin-top:8px"><button id="btnSaveOther">保存</button></div>
  </div>
  <div class="card">
    <h2>本地数据</h2>
    <div class="row">
      <button id="btnOpenData">打开数据目录</button>
      <span class="muted">所有快照/变动/AI报告保存在本机 data/ 目录，不会上传。</span>
    </div>
  </div>
</div>
</div>

<script>
const $=s=>document.querySelector(s);
let CFG=null, LAST=null; // LAST = 最近一次 run_full_update 结果
let selHistA=null, selHistB=null;

function toast(m){const t=$('#toast');t.textContent=m;t.style.opacity=1;setTimeout(()=>t.style.opacity=0,3200);}
function esc(s){if(s==null)return '';return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function curSym(c){c=String(c||'USD').toUpperCase();return(c==='CNY'||c==='CN¥'||c==='RMB'||c==='￥'||c==='¥')?'CNY ':'$';}
function kindPre(k){return k==='from'?'起价 ':k==='deal'?'优惠价 ':k==='range'?'区间 ':'';}
function fmtP(v,cur,k){if(v==null||isNaN(v))return '—';const s=String(cur||'USD').toUpperCase();const cny=(s==='CNY'||s==='CN¥'||s==='RMB'||s==='￥'||s==='¥');return kindPre(k)+curSym(cur)+(cny?Number(v).toFixed(2):Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}));}
function pLink(p){return p.url?(' <a href="'+esc(p.url)+'" target="_blank">链接</a>'):'';}
function itemRow(p){
  const tags=(p.is_store?'<span class="tag up">'+esc(storeTagText())+'</span>':'')+(p.rank!=null?'<span class="tag">#'+p.rank+'</span>':'');
  const priceTxt=(p.price!=null?fmtP(p.price,p.price_currency,p.price_kind):'—')+(p.price_note?'<span title="'+esc(p.price_note)+'">*</span>':'');
  return '<div style="padding:7px 0;border-bottom:1px solid var(--line)">'+tags+'<b>'+esc(p.title)+'</b><br>'+
    '<span class="muted">ASIN: '+esc(p.asin)+' | 价格: '+priceTxt+' | 星级: '+(p.rating==null?'—':p.rating)+
    ' | 评论: '+(p.reviews==null?'—':p.reviews)+' | 店铺: '+esc(p.store_name||'—')+'</span>'+pLink(p)+'</div>';
}

async function api(path,opt){const r=await fetch('/api'+path,opt||{});const j=await r.json();if(!j.ok)throw new Error(j.message||'请求失败');return j;}

function storeLabelText(){return String((CFG&&CFG.store_name)||'').trim()||'未设置目标店铺';}
function storeTagText(){return String((CFG&&CFG.store_name)||'').trim()||'目标店铺';}
function applyStoreLabels(){
  const lab=storeLabelText(), tag=storeTagText(), kws=(CFG&&CFG.store_keywords)||[];
  document.title=lab+' · 亚马逊 BSR TOP100 本地监控台';
  const ht=$('#headTitle'); if(ht)ht.textContent=lab+' · 亚马逊 BSR TOP100 销售看板';
  const kt=$('#kpiStoreLabel'); if(kt)kt.textContent=tag+' 上榜';
  const tb=$('#tabStore'); if(tb)tb.textContent='① '+lab+'上榜汇总';
  const t1=$('#sec1Title'); if(t1)t1.textContent=lab+' 上榜汇总';
  const t4=$('#sec4Title'); if(t4)t4.textContent=tag+' 运营销售建议';
  const kw=$('#kwShow'); if(kw)kw.textContent=kws.length?kws.join(' / '):'（未设置，请到⑤店铺设置填写）';
}
async function loadConfig(){
  const j=await api('/config');
  CFG=j.config;
  $('#cfgStoreName').value=CFG.store_name||'';
  $('#cfgKws').value=(CFG.store_keywords||[]).join(', ');
  $('#cfgSid').value=CFG.store_page_marker||'';
  applyStoreLabels();
  document.querySelectorAll('input[name=aiMode]').forEach(r=>r.checked=(r.value===CFG.ai_mode));
  $('#cfgBase').value=CFG.ai_base_url||'';
  $('#cfgModel').value=CFG.ai_model||'';
  $('#cfgKey').value=CFG.ai_api_key||'';
  $('#cfgMaxTokens').value=CFG.ai_max_tokens||0;
  $('#cfgNumCtx').value=CFG.ai_num_ctx||0;
  $('#cfgSched').value=CFG.schedule_minutes||0;
  $('#cfgProxy').value=CFG.http_proxy||'';
  $('#cfgFetchMode').value=CFG.fetch_mode||'auto';
  const st=j.state;
  $('#fetchStatus').textContent=(st.last_fetch?('最近抓取: '+st.last_fetch):'尚未抓取')+(st.busy?' ｜ 抓取中...':'');
  loadHistory();
}

// ===== 销售看板：KPI 渲染 / 进度条机制 =====
// 三类动作各自的进度步骤（与后端 /api/progress 返回的 kind 对应）
const STEP_PLANS={
  fetch:[
    {label:'请求榜单页',max:56},
    {label:'解析识别',max:70},
    {label:'保存快照',max:80},
    {label:'对比榜单',max:90},
    {label:'生成分析',max:100}
  ],
  analysis:[
    {label:'读取最近快照',max:20},
    {label:'组装数据',max:45},
    {label:'调用模型生成',max:90},
    {label:'写入报告',max:100}
  ],
  advice:[
    {label:'读取最近快照',max:20},
    {label:'组装数据',max:45},
    {label:'调用模型生成',max:90},
    {label:'写入建议',max:100}
  ]
};
let CUR_KIND='fetch';
function stepPlan(kind){return STEP_PLANS[kind]||STEP_PLANS.fetch;}
// 统一时间展示：YYYY-MM-DD HH:MM
function fmtMin(t){const s=String(t==null?'':t).trim();const m=s.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/);return m?(m[1]+' '+m[2]):(s||'—');}
function baseSnapLabel(r){
  if(!r||!r.prev_fetched_at)return '无（本次为基线快照）';
  return fmtMin(r.prev_fetched_at)+(r.prev_snapshot_id?'（'+r.prev_snapshot_id+'）':'');
}
function timeLineText(r){
  if(!r)return '';
  return '本次抓取时间：'+fmtMin(r.fetched_at)+' ｜ 对比基准快照：'+baseSnapLabel(r);
}
function renderKpi(res){
  if(!res||res.ok===false){return;}
  const cs=res.store_items||[];
  const ni=res.new_in||[], dr=res.dropped||[];
  $('#kpiStore').textContent=cs.length;
  $('#kpiNew').textContent=ni.length;
  $('#kpiDrop').textContent=dr.length;
  $('#kpiTotal').textContent=res.count!=null?res.count:'—';
  if(res.fetched_at){
    const parts=String(res.fetched_at).split(' ');
    $('#kpiTime').textContent=parts[1]||res.fetched_at;
    $('#kpiTimeSub').textContent=parts[0]||'';
    $('#headLastFetch').textContent='最近抓取: '+fmtMin(res.fetched_at);
  }
}
function renderProgress(p,kindHint){
  const panel=$('#fetchPanel');if(!panel)return;
  panel.classList.remove('hidden');
  const kind=kindHint||(p&&p.kind)||CUR_KIND;
  CUR_KIND=kind;
  const plan=stepPlan(kind);
  const pct=Math.max(0,Math.min(100,Math.round((p&&p.pct!=null)?p.pct:0)));
  $('#progPct').textContent=pct+'%';
  $('#progBar').style.width=pct+'%';
  $('#progStage').textContent=(p&&p.stage)?p.stage:'准备中...';
  let curIdx=-1;
  plan.forEach((s,i)=>{if(pct>=s.max)curIdx=i;});
  $('#stepsBar').innerHTML=plan.map((s,i)=>{
    const cls=(i<curIdx||pct>=s.max)?'done':(i===curIdx?'active':'todo');
    const mark=(i<curIdx||pct>=s.max)?'✓':String(i+1);
    return '<span class="step '+cls+'"><span class="dot">'+mark+'</span><span class="lbl">'+s.label+'</span></span>'+(i<plan.length-1?'<span class="arr">›</span>':'');
  }).join('');
}
async function pollProgress(){
  try{const j=await api('/progress');return j.progress||null;}catch(e){return null;}
}
async function runTask(opts){
  const btn=opts.btn;
  btn.disabled=true;btn.classList.add('loading');
  btn.dataset.orig=btn.textContent;
  btn.textContent=opts.btnText||'抓取中…';
  const panel=$('#fetchPanel');
  panel.classList.remove('hidden');
  const myKind=opts.kind||'fetch';
  CUR_KIND=myKind;
  $('#progTitle').textContent=opts.title||'抓取更新中';
  setRunUI(true);
  $('#progPct').textContent='0%';$('#progBar').style.width='0%';
  $('#progStage').textContent='任务启动中...';
  $('#progSummary').innerHTML='';
  $('#stepsBar').innerHTML='';
  $('#fetchStatus').textContent=opts.runningMsg||'任务进行中，请稍候...';
  const timer=setInterval(async()=>{
    const p=await pollProgress();
    if(p)renderProgress(p,myKind);
    if(p&&!p.running){clearInterval(timer);}
  },1200);
  try{
    const j=await api(opts.path,{method:'POST'});
    clearInterval(timer);
    if(j.ok){
      setRunUI(false);
      $('#progTitle').textContent='任务已完成';
      renderProgress({pct:100,stage:'任务已完成'});
      await renderResult(j.result);
      const r=j.result;
      const msg=opts.okMsg||('本次更新完成：TOP100 共 '+r.count+' 款，'+storeLabelText()+' 上榜 '+r.store_count+' 款，新上榜 '+(r.new_in||[]).length+' 款，落榜 '+(r.dropped||[]).length+' 款');
      const tl=timeLineText(r);
      $('#progSummary').innerHTML='<div class="okbox">✅ '+esc(msg)+(tl?'<br>'+esc(tl):'')+'</div>';
      toast('✅ '+msg+(tl?'（'+tl+'）':''));
      if(opts.gotoTab){const t=document.querySelector('.tab[data-t="'+opts.gotoTab+'"]');if(t)t.click();}
    }else{
      const lastPct=parseFloat(String($('#progPct').textContent||'0').replace('%',''))||5;
      const em=esc(j.message||'未知错误');
      setRunUI(false);
      $('#progTitle').textContent='任务失败';
      renderProgress({pct:lastPct,stage:'任务失败'});
      const el=opts.errLabel||'抓取失败';
      $('#progSummary').innerHTML='<div class="errbox">❌ '+el+'：'+em+'</div>';
      $('#topmsg').innerHTML='<div class="errbox">'+el+'：'+em+'</div>';
      toast('❌ '+el+': '+(j.message||''));
    }
  }catch(e){
    clearInterval(timer);
    setRunUI(false);
    $('#progTitle').textContent='任务失败';
    $('#progSummary').innerHTML='<div class="errbox">❌ 请求失败：'+esc(e.message)+'</div>';
    toast('❌ 请求失败: '+e.message);
  }finally{
    btn.disabled=false;btn.classList.remove('loading');
    btn.textContent=btn.dataset.orig||btn.textContent;
    await loadConfig();
  }
}
function setRunUI(running){
  const sp=$('#progSpinner');
  if(sp){sp.classList.toggle('hidden',!running);}
}
async function doFetch(source){await runTask({btn:$('#btnFetch'),path:'/fetch',btnText:'抓取中…',title:'抓取更新中',kind:'fetch',runningMsg:'抓取进行中，请稍候（约 20-120 秒）...',gotoTab:'sec1'});}

async function renderResult(res){
  LAST=res;
  renderKpi(res);
  // 目标店铺上榜
  const cs=res.store_items||[];
  $('#storeStats').innerHTML =
    '<div class="stat"><div class="n">'+cs.length+'</div><div class="l">'+esc(storeLabelText())+' 上榜 BSR TOP100 款数</div></div>'+
    '<div class="stat"><div class="n">'+(res.count||0)+'</div><div class="l">本次抓取到总商品数</div></div>'+
    '<div class="stat"><div class="n">'+(res.has_prev?'已对比':'基线快照')+'</div><div class="l">历史快照状态</div></div>';
  const box=$('#storeList');
  if(res.ok===false){box.innerHTML='<div class="empty">'+esc(res.message||'未知错误')+'</div>';return;}
  if(!cs.length){
    if(!String((CFG&&CFG.store_name)||'').trim()&&!((CFG&&CFG.store_keywords)||[]).length&&!String((CFG&&CFG.store_page_marker)||'').trim()){
      box.innerHTML='<div class="empty"><b>尚未设置目标店铺。</b><br>请到 ⑤ 设置 →「店铺设置」填写店铺名称与关键词并保存，再重新抓取或点击「生成运营建议」。</div>';
    }else{
      box.innerHTML='<div class="empty"><b>本次未识别到「'+esc(storeLabelText())+'」的产品进入 BSR TOP100。</b><br>若确信有产品上榜，请到 ⑤ 设置 →「店铺设置」核对店铺关键词 / 主页 marker，并确认商品卡渲染了店铺信息。</div>';
    }
  }else{
    box.innerHTML='<div class="muted">共 '+cs.length+' 款「'+esc(storeLabelText())+'」产品进入蓝牙音箱 BSR TOP100：</div>'+cs.map(itemRow).join('');
  }
  // market stats（画像区：有数据显示、无数据占位）
  const s=res.summary||{};
  const dq=s.data_quality||{};
  const totalCnt=res.count||0;
  const covNote=$('#topCovNote');
  if(totalCnt>0&&totalCnt<100&&covNote){
    covNote.classList.remove('hidden');
    const nt=(res.fetch_notice||'').trim();
    const um=(res.fetch_mode_used||'http');
    if(nt){
      covNote.innerHTML='本次仅获取到 <b>'+totalCnt+'</b>/100 款（抓取模式：'+esc(um)+'）。<br>'+esc(nt);
    }else{
      covNote.innerHTML='本次仅获取到 <b>'+totalCnt+'</b>/100 款（抓取模式：'+esc(um)+'）：Amazon 榜单页默认每页只渲染约 30 款，其余需浏览器向下滚动懒加载。下方画像 / 对比均基于已获取部分；如需抓满 100，请在⑤设置 →「抓取与代理」把「抓取模式」选为「自动（推荐）」或「浏览器滚动」并保存。';
    }
  }else if(covNote){covNote.classList.add('hidden');covNote.innerHTML='';}
  if(!dq.has_price&&!dq.has_rating&&!dq.has_reviews){
    $('#marketStats').innerHTML='<div class="empty" style="grid-column:1/-1">暂无画像数据：本次页面未解析到价格 / 星级 / 评论字段。请重新执行一次「立即抓取更新」，将按当前 Amazon 商品卡结构抓取完整字段。</div>';
  }else{
    let cs='';
    if(s.store_stats&&s.store_stats.count&&s.store_stats.avg_price!=null){
      cs=stat(storeLabelText()+' 均价 对比 中位价', fmtP(s.store_stats.avg_price,s.currency)+' / 中位 '+fmtP(s.store_stats.median_price,s.currency)+(s.store_stats.vs_median==='higher'?'（高于）':(s.store_stats.vs_median==='lower'?'（低于）':'')));
    }
    $('#marketStats').innerHTML =
      stat('价格带 P25/P50/P75', fmtP(s.price_p25,s.currency)+' / '+fmtP(s.price_p50,s.currency)+' / '+fmtP(s.price_p75,s.currency))+
      stat('价格区间', fmtP(s.price_min,s.currency)+' ~ '+fmtP(s.price_max,s.currency))+
      stat('平均星级', s.rating_avg==null?'—':s.rating_avg)+
      stat('评论数中位数', s.reviews_median==null?'—':s.reviews_median)+
      stat('含价格商品数', (dq.price_n!=null?(dq.price_n+' / '+totalCnt):'—'))+
      cs;
  }
  $('#marketExtra').innerHTML=marketExtraHtml(s);
  $('#top10').innerHTML=(res.top_items||[]).slice(0,10).map(p=>'<div style="padding:4px 0;border-bottom:1px dashed var(--line)">#'+p.rank+' · <a href="'+esc(p.url||'#')+'" target="_blank">'+esc((p.title||'').slice(0,90))+'</a> <span class="muted">'+fmtP(p.price,p.price_currency,p.price_kind)+' · ★'+(p.rating==null?'—':p.rating)+'</span></div>').join('')||'<div class="empty">暂无 TOP10 数据，请先抓取</div>';
  // diff
  const diff=$('#diffBody');
  const ni=res.new_in||[], dr=res.dropped||[];
  $('#diffInfo').innerHTML = res.has_prev
    ? '<div class="okbox">对比完成：新上榜 <b>'+ni.length+'</b> 款，落榜 <b>'+dr.length+'</b> 款<br>本次抓取时间：<b>'+esc(fmtMin(res.fetched_at))+'</b> ｜ 基准快照：<b>'+esc(baseSnapLabel(res))+'</b></div>'
    : '<div class="note">暂无上一次历史快照，本次已保存为<b>基线快照</b>（本次抓取时间：<b>'+esc(fmtMin(res.fetched_at))+'</b>）；下次抓取将自动生成变动对比。</div>';
  let h='';
  if(ni.length||dr.length){
    h+='<div style="margin-top:8px"><b>✅ 新上榜产品（'+ni.length+'）</b></div>';
    h+=ni.length?ni.map(itemRow).join(''):'<div class="empty">无</div>';
    h+='<div style="margin-top:10px"><b>❌ 落榜产品（'+dr.length+'）</b></div>';
    h+=dr.length?dr.map(itemRow).join(''):'<div class="empty">无</div>';
  }
  diff.innerHTML=h||'<div class="empty">暂无变动数据</div>';
  // ai & advice
  if(res.ai_report_path){$('#aiMeta').innerHTML='报告文件: '+esc(res.ai_report_path)+'<br>报告生成时间：'+esc(fmtMin(res.report_gen_at||res.fetched_at))+' ｜ 本次数据抓取时间：'+esc(fmtMin(res.fetched_at))+' ｜ 对比基准快照：'+esc(baseSnapLabel(res));}
  if(res.ai_text){$('#aiReport').textContent=res.ai_text;}
  if(res.advice_path){$('#advMeta').textContent='建议文件: '+res.advice_path;}
  if(res.advice_text){$('#advReport').textContent=res.advice_text;}
}
function stat(l,n){return '<div class="stat"><div class="n" style="font-size:16px">'+esc(n)+'</div><div class="l">'+esc(l)+'</div></div>';}

/* doFetch 已由上方 runTask 驱动的同名函数接管 */

function itemMini(p){
  if(!p)return '—';
  const t=esc((p.title||p.asin||'').slice(0,80));
  const a=p.url?('<a href="'+esc(p.url)+'" target="_blank">'+t+'</a>'):t;
  let extra='';
  if(p.rank!=null)extra+=' #'+p.rank;
  if(p.price!=null)extra+=' · '+fmtP(p.price,p.price_currency,p.price_kind);
  if(p.reviews!=null)extra+=' · '+p.reviews+' 条评论';
  return a+'<span class="muted">'+extra+'</span>';
}
function marketExtraHtml(s){
  if(!s||typeof s!=='object')return '';
  const dq=s.data_quality||{};
  if(!dq.price_n&&!dq.rating_n&&!dq.reviews_n)return '<div class="empty">暂无画像明细：该快照未包含价格 / 星级 / 评论字段。</div>';
  const parts=[];
  if((dq.price_n===false||dq.rating_n===false||dq.reviews_n===false)&&dq.price_n!=null){
    parts.push('<div class="note" style="margin:2px 0 8px">字段覆盖率：价格 '+dq.price_n+' 款，星级 '+dq.rating_n+' 款，评论 '+dq.reviews_n+' 款（Amazon 对部分商品卡不渲染价格 / 评论属正常现象）。</div>');
  }
  const zs=s.price_zones||[];
  if(zs.length){
    const tot=zs.reduce((a,z)=>a+(z.count||0),0)||1;
    const zoneCode=String(s.currency||'USD').toUpperCase();
    const zoneIsCNY=(zoneCode==='CNY'||zoneCode==='CN¥'||zoneCode==='RMB'||zoneCode==='￥'||zoneCode==='¥');
    const zoneCur=zoneIsCNY?'CNY ':'$';
    const zoneName=zoneIsCNY?'CNY':'USD';
    parts.push('<div style="font-size:13px"><b>价格档位分布（'+zoneName+'）</b> <span class="muted">（'+tot+' 款含价格，固定五档 0~20/20~50/50~100/100~200/&gt;200）</span></div>');
    parts.push(zs.map(z=>'<div class="pbar"><span style="width:64px;flex:none">'+esc(zoneCur+String(z.label).replace('$',''))+'</span><div class="pbar-track"><div class="pbar-fill" style="width:'+Math.max(2,Math.round(100*(z.count||0)/tot))+'%"></div></div><span class="muted">'+z.count+' 款 · '+(z.pct||0)+'%</span></div>').join(''));
  }
  const rows=[];
  if(s.cheapest)rows.push(['最低价款',s.cheapest]);
  if(s.dearest)rows.push(['最高价款',s.dearest]);
  if(s.most_reviewed)rows.push(['评论数最高',s.most_reviewed]);
  if(s.highest_rated)rows.push(['星级最高',s.highest_rated]);
  if(rows.length){
    parts.push('<div style="font-size:13px;margin-top:6px"><b>销售顾问速览</b></div>');
    parts.push(rows.map(r=>'<div style="font-size:13px;padding:3px 0"><b>'+r[0]+'：</b>'+itemMini(r[1])+'</div>').join(''));
  }
  const cm=s.store_stats;
  if(cm&&cm.count){
    let pos='';
    if(cm.avg_price!=null&&cm.median_price!=null){
      pos=' ｜ 均价 '+fmtP(cm.avg_price,s.currency)+(cm.vs_median==='higher'?' 高于榜单中位价 ':' 低于榜单中位价 ')+fmtP(cm.median_price,s.currency);
    }
    parts.push('<div style="font-size:13px;margin-top:6px;background:#fff7f5;border:1px solid #f3d6d0;border-radius:8px;padding:8px 10px"><b>'+esc(storeLabelText())+' 价格位置</b><br>上榜排名：#'+(cm.ranks&&cm.ranks.length?cm.ranks.join(', #'):'—')+'<br>价格区间：'+fmtP(cm.min_price,s.currency)+' ~ '+fmtP(cm.max_price,s.currency)+pos+'</div>');
  }
  return parts.join('');
}
// ---- 提示词设置（⑤ 设置）：加载 / 保存 / 恢复默认
async function loadPrompts(){
  try{
    const j=await api('/prompts');
    const pa=$('#cfgPromptAnalysis'), pv=$('#cfgPromptAdvice');
    if(pa)pa.value=j.prompt_analysis||j.default_analysis||'';
    if(pv)pv.value=j.prompt_advice||j.default_advice||'';
    const ph=$('#phHelp');
    if(ph)ph.innerHTML=' '+(j.placeholders||[]).map(p=>'<span class="ph-code">'+esc(p.token)+'</span> '+esc(p.desc)).join('； ');
  }catch(e){toast('加载提示词失败: '+e.message);}
}
async function savePrompts(){
  const m=$('#promptSaveMsg'); if(m)m.textContent='保存中...';
  const pa=$('#cfgPromptAnalysis'), pv=$('#cfgPromptAdvice');
  try{
    await api('/prompts_save',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({prompt_analysis:pa?pa.value:'',prompt_advice:pv?pv.value:''})});
    if(m)m.textContent='已保存，后续生成立即生效';
    toast('提示词已保存');
    await loadPrompts();
  }catch(e){if(m)m.textContent='保存失败: '+e.message;toast('保存失败: '+e.message);}
}
async function resetPrompt(which){
  const m=$(which==='analysis'?'#promptAnalysisMsg':'#promptAdviceMsg');
  try{
    const j=await api('/prompts_reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({which:which})});
    const pa=$('#cfgPromptAnalysis'), pv=$('#cfgPromptAdvice');
    if(pa&&j.prompt_analysis!=null)pa.value=j.prompt_analysis;
    if(pv&&j.prompt_advice!=null)pv.value=j.prompt_advice;
    if(m)m.textContent='已恢复内置默认';
    toast('已恢复默认提示词');
  }catch(e){if(m)m.textContent='恢复失败: '+e.message;toast('恢复默认失败: '+e.message);}
}
async function loadHistory(){
  try{
    const j=await api('/history');
    const hist=j.history||[];
    if(selHistA&&!hist.some(h=>h.snapshot_id===selHistA))selHistA=null;
    if(selHistB&&!hist.some(h=>h.snapshot_id===selHistB))selHistB=null;
    $('#histBody').innerHTML=hist.map(h=>{
      const base=(selHistA===h.snapshot_id), target=(selHistB===h.snapshot_id);
      const flag=(base?'<span class="tag">基准A</span>':'')+(target?'<span class="tag up">目标B</span>':'');
      return '<tr><td>'+esc(h.fetched_at)+'</td><td>'+h.count+'</td><td>'+h.store_count+'</td><td>'+esc(h.file)+'</td><td style="white-space:nowrap">'+
        flag+
        '<button class="small" onclick="pickA(\\''+h.snapshot_id+'\\')">基准</button> '+
        '<button class="small" onclick="pickB(\\''+h.snapshot_id+'\\')">目标</button> '+
        '<button class="small danger" onclick="removeSnapshot(\\''+h.snapshot_id+'\\')">删除</button></td></tr>';
    }).join('')||'<tr><td colspan="5" class="empty">暂无历史快照</td></tr>';
  }catch(e){}
}
async function removeSnapshot(sid){
  let rec=null;
  try{const j=await api('/history');rec=(j.history||[]).find(h=>h.snapshot_id===sid);}catch(e){}
  const used=[];
  if(selHistA===sid)used.push('基准A');
  if(selHistB===sid)used.push('目标B');
  const lines=[];
  if(rec)lines.push('抓取时间：'+rec.fetched_at+'，共 '+rec.count+' 款，'+storeLabelText()+' 上榜 '+rec.store_count+' 款');
  if(used.length)lines.push('注意：该快照正被选为 '+used.join(' / ')+'，删除后将自动解除选择');
  if(!window.confirm('确定删除历史快照 '+sid+' 吗？\\n'+lines.join('\\n')+'\\n删除会同时移除本地 JSON 文件并从历史列表移除，且不可恢复。'))return;
  try{
    const j=await api('/snapshot?snapshot_id='+encodeURIComponent(sid),{method:'DELETE'});
    let diffCleared=false;
    if(selHistA===sid)selHistA=null;
    if(selHistB===sid){selHistB=null;diffCleared=true;}
    if(diffCleared){$('#diffInfo').innerHTML='<div class="note">所选对比目标快照已被删除，回溯对比已解除。</div>';$('#diffBody').innerHTML='';}
    toast(j.message||'已删除');
    loadHistory();
    if(j.reset_last||(LAST&&LAST.snapshot_id===sid)){LAST=null;renderEmptyBoard();}
  }catch(e){toast('删除失败: '+e.message);}
}
function renderEmptyBoard(){
  ['kpiStore','kpiNew','kpiDrop','kpiTotal'].forEach(id=>{const el=$('#'+id);if(el)el.textContent='—';});
  const kt=$('#kpiTime');if(kt)kt.textContent='—';
  const kts=$('#kpiTimeSub');if(kts)kts.textContent='尚未执行抓取';
  const hlf=$('#headLastFetch');if(hlf)hlf.textContent='尚未抓取';
  const cs=$('#storeStats');if(cs)cs.innerHTML='';
  const cl=$('#storeList');if(cl)cl.innerHTML='<div class="empty">暂无数据，请先抓取。</div>';
  const cn=$('#topCovNote');if(cn){cn.classList.add('hidden');cn.innerHTML='';}
  const ms=$('#marketStats');if(ms)ms.innerHTML='';
  const me=$('#marketExtra');if(me)me.innerHTML='';
  const t10=$('#top10');if(t10)t10.innerHTML='';
  const tm=$('#topmsg');if(tm)tm.innerHTML='';
  const fs=$('#fetchStatus');if(fs)fs.textContent='尚未抓取';
}
async function pickA(id){selHistA=id;loadHistory();toast('已选基准 '+id);}
async function pickB(id){selHistB=id;loadHistory();if(selHistA&&selHistB){await compareHist();}}
async function compareHist(){
  try{
    const j=await api('/compare?base='+selHistA+'&target='+selHistB);
    $('#diffBody').innerHTML = '<div class="okbox">回溯对比：基准 '+esc(fmtMin(j.base_time))+' → 目标 '+esc(fmtMin(j.target_time))+'</div>'+
      '<b>✅ 新上榜（'+j.new_in.length+'）</b>'+j.new_in.map(itemRow).join('')+
      '<div style="margin-top:10px"><b>❌ 落榜（'+j.dropped.length+'）</b></div>'+(j.dropped.length?j.dropped.map(itemRow).join(''):'<div class="empty">无</div>');
  }catch(e){toast(e.message);}
}

// ---- 事件绑定
$('#btnFetch').onclick=()=>doFetch('manual');
$('#btnAdvice').onclick=()=>runTask({btn:$('#btnAdvice'),path:'/regenerate_advice',btnText:'生成中…',title:'AI 建议生成中',kind:'advice',runningMsg:'正在生成运营销售建议（读取最近快照，不重新抓取）...',okMsg:'AI 运营建议已重新生成',gotoTab:'sec4'});
$('#btnGenAnalysis').onclick=()=>runTask({btn:$('#btnGenAnalysis'),path:'/regenerate_analysis',btnText:'生成中…',title:'AI 分析生成中',kind:'analysis',runningMsg:'正在生成 AI 分析报告（读取最近快照，不重新抓取）...',okMsg:'AI 分析报告已按当前提示词重新生成（未覆盖抓取结果与其他区块）',gotoTab:'sec3',errLabel:'AI 分析生成失败'});
if($('#btnSavePrompts'))$('#btnSavePrompts').onclick=savePrompts;
if($('#btnResetAnalysis'))$('#btnResetAnalysis').onclick=()=>resetPrompt('analysis');
if($('#btnResetAdvice'))$('#btnResetAdvice').onclick=()=>resetPrompt('advice');
$('#btnExportStore').onclick=async()=>{try{const j=await api('/export?kind=store_csv');toast('已导出: '+j.path);}catch(e){toast(e.message);}};
$('#btnExportChanges').onclick=async()=>{try{const j=await api('/export?kind=changes_csv');toast('已导出: '+j.path);}catch(e){toast(e.message);}};
$('#btnExportAdvice').onclick=async()=>{try{const j=await api('/export?kind=advice_md');toast('已导出: '+j.path);}catch(e){toast(e.message);}};
$('#btnOpenData').onclick=async()=>{try{const j=await api('/open_data');toast(j.message);}catch(e){toast(e.message);}};
$('#btnSaveStore').onclick=async()=>{const m=$('#storeSaveMsg');m.textContent='保存中...';const ok=await saveConfig(false);m.textContent=ok?'已保存：看板标题 / 导出文件名 / 报告标题已按新店铺名生效':'保存失败，请查看提示';};
$('#btnSaveAi').onclick=saveConfig;
$('#btnSaveOther').onclick=saveConfig;
$('#btnTestAi').onclick=async()=>{const msg=$('#aiTestMsg');msg.textContent='测试中...';try{await saveConfig(false);const j=await api('/ai_test',{method:'POST'});msg.textContent=j.message;}catch(e){msg.textContent='失败: '+e.message;}};
async function saveConfig(showToast=true){
  const mode=[...document.querySelectorAll('input[name=aiMode]')].find(r=>r.checked).value;
  const body={store_name:$('#cfgStoreName').value.trim(),
    store_keywords:$('#cfgKws').value.split(/[,，]/).map(s=>s.trim()).filter(Boolean),
    store_page_marker:$('#cfgSid').value.trim(),
    ai_mode:mode, ai_base_url:$('#cfgBase').value.trim(),
    ai_model:$('#cfgModel').value.trim(), ai_api_key:$('#cfgKey').value.trim(),
    ai_max_tokens:parseInt($('#cfgMaxTokens').value)||0,
    ai_num_ctx:parseInt($('#cfgNumCtx').value)||0,
    schedule_minutes:Math.max(0,parseInt($('#cfgSched').value)||0),
    http_proxy:$('#cfgProxy').value.trim(), https_proxy:$('#cfgProxy').value.trim(),
    fetch_mode:$('#cfgFetchMode').value||'auto'};
  try{await api('/config_save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(showToast){toast('配置已保存');}await loadConfig();return true;}catch(e){toast('保存失败: '+e.message);return false;}
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.sec').forEach(x=>x.classList.remove('on'));
  t.classList.add('on');document.getElementById(t.dataset.t).classList.add('on');
});
(async function init(){
  try{await loadConfig();}catch(e){toast('加载失败: '+e.message);}
  try{await loadPrompts();}catch(e){}
  // 若已有最近结果直接展示
  try{const j=await api('/last_result');if(j.result)await renderResult(j.result);}catch(e){}
})();
</script>
</body>
</html>
"""

def _send_json(handler, obj, status=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

class ApiHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def _run_task(self, kind, source="manual", do_ai=None):
        """同步执行抓取/建议任务，同时维护 STATE['progress'] 供前端轮询展示进度。

        run_full_update 本身会阻塞当前请求（抓取防反爬约 20-60 秒），但服务端是
        ThreadingHTTPServer，前端在此期间可通过 GET /api/progress 轮询实时进度。
        """
        with LOCK:
            STATE["progress"] = {"running": True, "kind": kind, "pct": 0,
                                 "stage": "任务启动中...", "ok": None, "message": "",
                                 "started_at": now_str(), "finished_at": None,
                                 "summary": None}

        def cb(msg, pct=None):
            with LOCK:
                cur = STATE.get("progress") or {}
                cur["stage"] = msg
                if pct is not None:
                    cur["pct"] = pct

        try:
            if kind == "advice":
                # 基于最近快照重生成：不抓页面、不新增快照，网络不通时不再卡在打开第一页
                res = regenerate_advice_from_latest(cfg=load_config(), progress_cb=cb)
            elif kind == "analysis":
                # 基于最近快照用当前提示词重生成 AI 分析报告（不抓取、不改动其他区块）
                res = regenerate_analysis_from_latest(cfg=load_config(), progress_cb=cb)
            else:
                res = run_full_update(source=source, cfg=load_config(),
                                      do_ai=do_ai, progress_cb=cb)
        except Exception as e:
            res = {"ok": False, "message": "内部错误: %s" % e}
        with LOCK:
            cur = STATE.setdefault("progress", {})
            cur["running"] = False
            cur["ok"] = bool(res.get("ok"))
            cur["message"] = res.get("message", "")
            cur["finished_at"] = now_str()
            cur["pct"] = 100 if res.get("ok") else max(cur.get("pct", 0), 5)
            if res.get("ok"):
                cur["summary"] = {
                    "count": res.get("count"),
                    "store_count": res.get("store_count"),
                    "new_in_count": len(res.get("new_in") or []),
                    "dropped_count": len(res.get("dropped") or []),
                    "fetched_at": res.get("fetched_at"),
                    "has_prev": res.get("has_prev"),
                }
                # 缓存最近一次成功结果：供界面刷新后展示与导出接口使用
                # 仅「局部重生成」任务（analysis / advice：不重新抓取页面）才用旧结果补齐本次未重算的字段，
                # 且仅限旧内容白名单（报告/建议文本与路径）；
                # 完整抓取（fetch）必须原样使用本次真实结果：本次真实"0 新上榜 / 0 落榜"属于合法空值，
                # 若被上次的非空 new_in / dropped 回填，会导致界面显示与本次快照不符（对比口径错乱）。
                if kind in ("analysis", "advice"):
                    prev_res = STATE.get("last_result")
                    if isinstance(prev_res, dict):
                        for _k in ("ai_report_path", "ai_text", "advice_path", "advice_text"):
                            _v = prev_res.get(_k)
                            if res.get(_k) in (None, "", [], {}) and _v not in (None, "", [], {}):
                                res[_k] = _v
                STATE["last_result"] = res
        return res

    def route(self):
        path = self.path.split("?")[0]
        qs = {}
        if "?" in self.path:
            try:
                from urllib.parse import parse_qs
                qs = {k: v[0] for k, v in parse_qs(self.path.split("?", 1)[1]).items()}
            except Exception:
                qs = {}
        try:
            if path == "/" or path == "/index.html":
                body = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/config":
                cfg = load_config()
                with LOCK:
                    st = {"last_fetch": STATE["last_fetch"], "busy": STATE["busy"],
                          "last_error": STATE["last_error"], "last_snapshot_id": STATE["last_snapshot_id"]}
                _send_json(self, {"ok": True, "config": cfg, "state": st})
            elif path == "/api/config_save":
                cfg = save_config(self._read_body())
                _send_json(self, {"ok": True, "config": cfg})
            elif path == "/api/fetch":
                if self.command != "POST":
                    _send_json(self, {"ok": False, "message": "method not allowed"}, 405)
                    return
                res = self._run_task("fetch", source="manual")
                _send_json(self, {"ok": res.get("ok", False), "message": res.get("message", ""),
                                 "result": res})
            elif path == "/api/history":
                _send_json(self, {"ok": True, "history": _read_json(HISTORY_PATH, [])})
            elif path == "/api/snapshot":
                sid = qs.get("snapshot_id") or qs.get("id") or ""
                if not sid:
                    _send_json(self, {"ok": False, "message": "缺少 snapshot_id"}, 400)
                    return
                if self.command != "DELETE":
                    _send_json(self, {"ok": False, "message": "请使用 DELETE 方法"}, 405)
                    return
                ok, msg, reset_last = delete_snapshot(sid)
                _send_json(self, {"ok": ok, "message": msg, "reset_last": reset_last})
            elif path == "/api/compare":
                base = load_snapshot(qs.get("base") or None)
                target = load_snapshot(qs.get("target") or None)
                if not base or not target:
                    _send_json(self, {"ok": False, "message": "快照不存在"}, 400)
                    return
                ni, dr = diff_snapshots(base, target)
                _send_json(self, {"ok": True,
                                  "base_time": snapshot_display_time(base),
                                  "target_time": snapshot_display_time(target),
                                  "new_in": ni, "dropped": dr})
            elif path == "/api/export":
                with LOCK:
                    res = STATE.get("last_result")
                kind = qs.get("kind", "store_csv")
                if not res:
                    _send_json(self, {"ok": False, "message": "还没有抓取结果，请先执行「立即抓取更新」"}, 400)
                    return
                p = export_result(res, kind, load_config())
                _send_json(self, {"ok": True, "path": p})
            elif path == "/api/regenerate_advice":
                res = self._run_task("advice", source="regen_advice", do_ai=True)
                _send_json(self, {"ok": res.get("ok"), "message": res.get("message", ""), "result": res})
            elif path == "/api/prompts":
                cfg = load_config()
                _send_json(self, {"ok": True,
                                  "prompt_analysis": str(cfg.get("prompt_analysis") or ""),
                                  "prompt_advice": str(cfg.get("prompt_advice") or ""),
                                  "default_analysis": PROMPT_ANALYSIS_DEFAULT,
                                  "default_advice": PROMPT_ADVICE_DEFAULT,
                                  "placeholders": [{"token": t, "desc": d} for t, d in PROMPT_PLACEHOLDER_HELP]})
            elif path == "/api/prompts_save":
                body = self._read_body()
                cfg = load_config()
                if "prompt_analysis" in body:
                    cfg["prompt_analysis"] = str(body.get("prompt_analysis") or "")
                if "prompt_advice" in body:
                    cfg["prompt_advice"] = str(body.get("prompt_advice") or "")
                save_config(cfg)
                _send_json(self, {"ok": True, "message": "提示词已保存，后续生成立即生效"})
            elif path == "/api/prompts_reset":
                body = self._read_body()
                which = str(body.get("which") or "both")
                cfg = load_config()
                if which in ("analysis", "both"):
                    cfg["prompt_analysis"] = PROMPT_ANALYSIS_DEFAULT
                if which in ("advice", "both"):
                    cfg["prompt_advice"] = PROMPT_ADVICE_DEFAULT
                save_config(cfg)
                _send_json(self, {"ok": True, "message": "已恢复内置默认提示词",
                                  "prompt_analysis": cfg["prompt_analysis"],
                                  "prompt_advice": cfg["prompt_advice"]})
            elif path == "/api/regenerate_analysis":
                res = self._run_task("analysis", source="regen_analysis", do_ai=True)
                _send_json(self, {"ok": res.get("ok"), "message": res.get("message", ""), "result": res})
            elif path == "/api/ai_test":
                cfg = load_config()
                cfg.update(self._read_body())
                _send_json(self, ai_test(cfg))
            elif path == "/api/last_result":
                with LOCK:
                    res = STATE.get("last_result")
                _send_json(self, {"ok": True, "result": res})
            elif path == "/api/progress":
                with LOCK:
                    p = STATE.get("progress") or {"running": False, "pct": 0,
                                                  "stage": "", "ok": None, "message": "",
                                                  "started_at": None, "finished_at": None,
                                                  "summary": None, "kind": ""}
                    st = {"busy": STATE["busy"], "busy_msg": STATE["busy_msg"],
                          "last_fetch": STATE["last_fetch"]}
                _send_json(self, {"ok": True, "progress": p, "state": st})
            elif path == "/api/open_data":
                try:
                    os.startfile(DATA_DIR)  # noqa
                    _send_json(self, {"ok": True, "message": "已打开数据目录: " + DATA_DIR})
                except Exception as e:
                    _send_json(self, {"ok": True, "message": "目录: " + DATA_DIR + "（请手动打开）"})
            else:
                _send_json(self, {"ok": False, "message": "not found"}, 404)
        except AmazonFetchError as e:
            _send_json(self, {"ok": False, "kind": e.kind, "message": str(e)})
        except Exception as e:
            _send_json(self, {"ok": False, "message": "服务错误: %s" % e})

# ---------------------------------------------------------------- 入口

def restore_state_from_latest():
    """服务启动引导：若存在 data/latest.json，自动加载并恢复 STATE 缓存，
    使首页无需重新抓取即可直接展示最近一次快照结果与变动对比。"""
    try:
        latest = load_latest()
        if not latest or not latest.get("items"):
            _log("启动恢复: 未找到最近快照 (data/latest.json 缺失或为空)")
            return False
        items = list(latest.get("items") or [])
        sid = latest.get("snapshot_id")
        if not sid:
            _log("启动恢复: 快照缺少 snapshot_id，跳过")
            return False
        cfg = load_config()
        ensure_dirs()
        backfill_snapshot_fields(latest)   # 兼容历史快照字段缺失
        annotate_store(items, cfg)        # 快照可能早于关键词配置更新，重新打标
        store_items = [p for p in items if p.get("is_store")]
        summary = market_summary(items)
        # 与上一份快照做变动对比（只读历史，不新增快照/不写历史）
        history = _read_json(HISTORY_PATH, []) or []
        prev_sid = None
        for h in history:
            if h.get("snapshot_id") != sid:
                prev_sid = h.get("snapshot_id")
                break
        old = load_snapshot(prev_sid) if prev_sid else None
        new_in, dropped = [], []
        if old:
            backfill_snapshot_fields(old)
            new_in, dropped = diff_snapshots(old, latest)
        fetched_at = latest.get("fetched_at") or now_str()
        res = {
            "ok": True,
            "snapshot_id": sid,
            "fetched_at": fetched_at,
            "report_gen_at": now_str(),
            "prev_snapshot_id": (old.get("snapshot_id") if old else None),
            "prev_fetched_at": (snapshot_display_time(old) if old else None),
            "count": len(items),
            "store_items": store_items,
            "store_count": len(store_items),
            "summary": summary,
            "top_items": items,
            "new_in": new_in,
            "dropped": dropped,
            "diff": None,
            "has_prev": bool(old),
            "ai_report_path": None,
            "ai_text": None,
            "advice_path": None,
            "advice_text": None,
            "ai_mode": cfg.get("ai_mode"),
            "fetch_mode_used": "snapshot_restored",
            "fetch_notice": "服务重启后从最近快照恢复展示（未重新抓取页面）。如需最新数据请点击「立即抓取」。",
        }
        with LOCK:
            STATE["last_result"] = res
            STATE["last_snapshot_id"] = sid
            STATE["last_store_items"] = store_items
            STATE["last_fetch"] = fetched_at
            STATE["busy"] = False
            STATE["busy_msg"] = ""
            STATE["last_error"] = None
        _log("启动恢复成功: 快照 %s | TOP100=%d | %s=%d | 与上份快照对比 新上榜=%d / 落榜=%d"
             % (sid, len(items), _store_tag(cfg), len(store_items), len(new_in), len(dropped)))
        return True
    except Exception as e:
        _log("启动恢复失败（忽略，继续正常启动）: %s" % e)
        return False
def find_free_port(start=8965):
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start

def main():
    ap = argparse.ArgumentParser(description="亚马逊 BSR TOP100 店铺监控台（目标店铺可在⑤设置中配置）")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    ensure_dirs()
    cfg = load_config()
    _log("数据目录: %s" % DATA_DIR)
    restore_state_from_latest()
    if cfg.get("ai_mode") == "offline":
        _log("AI 模式: 离线模板（可在界面配置 Ollama / API Key）")
    else:
        _log("AI 模式: %s | %s | %s" % (cfg["ai_mode"], cfg["ai_base_url"], cfg["ai_model"]))
    _log("店铺关键词: %s" % (cfg.get("store_keywords") or []))
    if cfg.get("schedule_minutes"):
        _log("定时抓取: 每 %s 分钟（运行期间生效）" % cfg["schedule_minutes"])

    port = args.port or find_free_port()
    stop_event = threading.Event()
    if cfg.get("schedule_minutes"):
        threading.Thread(target=scheduler_loop, args=({}, stop_event), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), ApiHandler)
    url = "http://127.0.0.1:%d" % port
    _log("服务已启动: %s" % url)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("正在退出 ...")
    finally:
        stop_event.set()
        server.server_close()

if __name__ == "__main__":
    main()
