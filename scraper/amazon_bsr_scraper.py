"""
Amazon BSR + Reviews Scraper for 知微Agent.
零依赖自动化: 无需登录/Cookie, 从产品页内嵌评论采集。
品类: Pet Water Fountain | 市场: amazon.com
产出: 含 weight 和 date 字段的结构化 Markdown 快照
"""

import argparse
import asyncio
import random
import re
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import dataclass, field, asdict
from playwright.async_api import async_playwright, Page


# ============================================================
# DEFAULT CONFIG —— 默认品类参数 (可被命令行 --category 覆盖)
# ============================================================
DEFAULT_CONFIG = {
    "category": "Pet Water Fountain",
    "search_keyword": "pet water fountain",
    "market": "amazon.com",
    "base_url": "https://www.amazon.com",
    "bsr_url": "https://www.amazon.com/gp/bestsellers/pet-supplies/2975263011",
    "bsr_node_id": "2975263011",
    "bsr_node_path": "Pet Supplies > Cat Fountains",
    "max_products": 10,
    "max_review_pages": 3,
    "min_review_words": 50,
    "max_review_age_months": 18,
    "headless": os.getenv("CI", "").lower() == "true" or False,
    "delay_min": 2.0,
    "delay_max": 5.0,
    "product_cooldown": 8.0,
    "output_dir": str(Path(__file__).parent / "output"),
}


# ============================================================
# CATALOG —— 品类目录加载与解析
# ============================================================
CATALOG_PATH = Path(__file__).parent / "category_catalog.json"


def load_catalog() -> dict:
    """加载 category_catalog.json，返回 categories 字典。"""
    if not CATALOG_PATH.exists():
        return {}
    try:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8")).get("categories", {})
    except Exception as e:
        print(f"[Catalog] 加载失败: {e}", file=sys.stderr)
        return {}


def normalize_name(s: str) -> str:
    return (s or "").strip().lower().replace("_", " ").replace("-", " ")


def resolve_category(name: str, catalog: dict) -> tuple[str | None, dict | None]:
    """根据输入名（任意大小写/中文/别名）找到 catalog 中的规范条目。

    返回 (规范键名, 配置字典)，未匹配返回 (None, None)。
    """
    if not name:
        return None, None
    target = normalize_name(name)
    for key, info in catalog.items():
        if normalize_name(key) == target:
            return key, info
        for alias in info.get("aliases", []) or []:
            if normalize_name(alias) == target:
                return key, info
    return None, None


def infer_search_keyword(category: str) -> str:
    """从英文 Title Case category 推断 Amazon 搜索关键词。
      "Smart Watch"          → "smart watch"
      "Pet Water Fountain"   → "pet water fountain"
    输入若全为中文（用户输入污染场景），原样返回；
    Amazon US 站对中文关键词响应弱，会触发"无搜索结果"异常，由调用方处理。
    """
    return (category or "").strip().lower()


def append_to_catalog(category: str, info: dict, extra_aliases: list | None = None) -> bool:
    """把动态发现/更新的品类条目写回 category_catalog.json。

    并发安全：先写 .tmp 再 os.replace（原子操作，跨平台）；
    Action self-healing commit 流程会处理 git push 时的冲突。
    """
    try:
        catalog_data = {}
        if CATALOG_PATH.exists():
            catalog_data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        catalog_data.setdefault(
            "_doc",
            "Category catalog · 品类目录。新增品类时在此追加。"
            "键名建议用 Amazon 主流叫法的 Title Case。"
            "'aliases' 列出中文/同义词，N2D 会做归一化匹配。",
        )
        cats = catalog_data.setdefault("categories", {})

        existing_key, existing_info = resolve_category(category, cats)
        target_key = existing_key or category.strip()

        merged = dict(existing_info or {})
        merged.update({k: v for k, v in info.items() if v is not None})

        old_aliases = list(merged.get("aliases") or [])
        new_aliases = list(extra_aliases or [])
        merged["aliases"] = sorted(
            {
                a.strip()
                for a in (old_aliases + new_aliases)
                if a and a.strip().lower() != target_key.lower()
            }
        )

        cats[target_key] = merged
        catalog_data["_updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        tmp = CATALOG_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(catalog_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, CATALOG_PATH)
        action = "更新" if existing_key else "新增"
        print(
            f"[Catalog] ✏️ {action}品类条目: '{target_key}' "
            f"(bsr_node_id={merged.get('bsr_node_id', '?')})",
            file=sys.stderr,
        )
        return True
    except Exception as e:
        print(f"[Catalog] ⚠️ 写回失败: {e}", file=sys.stderr)
        return False


def build_config(args) -> dict:
    """根据命令行参数 + catalog 构建最终 CONFIG。

    决策表（严格化，禁止悄悄 fallback 到默认品类）：

      | 用户显式 --category | catalog 命中     | --allow-discover | 行为                                |
      |---------------------|------------------|------------------|-------------------------------------|
      | 否（定时任务）      | —                | —                | 沿用 DEFAULT_CONFIG（定时任务用）  |
      | 是                  | 是 + bsr_url OK  | —                | 直接抓                              |
      | 是                  | 是 + TODO_REPLACE| 否               | sys.exit(2) 报错                    |
      | 是                  | 是 + TODO_REPLACE| 是               | 进入动态发现（discover + 回填 catalog） |
      | 是                  | 否（catalog miss）| 否              | sys.exit(4) 报错，绝不抓 Pet Water Fountain |
      | 是                  | 否（catalog miss）| 是              | 进入动态发现（discover + 写入 catalog） |
    """
    cfg = dict(DEFAULT_CONFIG)
    catalog = load_catalog()
    cli_requested = (args.category or os.getenv("SCRAPE_CATEGORY", "")).strip()
    requested = cli_requested or cfg["category"]

    key, info = resolve_category(requested, catalog)

    if not info:
        if not cli_requested:
            print(
                f"[Catalog] 未找到默认品类 '{requested}'，沿用 DEFAULT_CONFIG（仅用于定时任务）",
                file=sys.stderr,
            )
            return cfg
        known = sorted(catalog.keys())
        if not args.allow_discover:
            print(
                f"[Catalog] ❌ 显式指定的品类 '{cli_requested}' 不在 catalog 中。\n"
                f"           已注册品类：{known or '(空)'}\n"
                f"           ① 已知品类 → 加入 category_catalog.json 后重试\n"
                f"           ② 新品类 → 加 --allow-discover（或 env ALLOW_DISCOVER=1）\n"
                f"              脚本会自动从 Amazon 搜索→产品页提取 BSR 节点并写回 catalog\n"
                f"           为避免静默回退到 DEFAULT_CONFIG（Pet Water Fountain），本次中止。",
                file=sys.stderr,
            )
            sys.exit(4)
        cfg["category"] = cli_requested.strip()
        cfg["search_keyword"] = infer_search_keyword(cli_requested)
        cfg["market"] = "amazon.com"
        cfg["bsr_url"] = ""
        cfg["bsr_node_id"] = ""
        cfg["bsr_node_path"] = ""
        cfg["_discover_mode"] = True
        cfg["_discover_existing_aliases"] = []
        if args.max_products:
            cfg["max_products"] = args.max_products
        print(
            f"[Catalog] 🔍 catalog miss + --allow-discover 启用 → 进入动态发现模式\n"
            f"           category='{cfg['category']}' search_keyword='{cfg['search_keyword']}'",
            file=sys.stderr,
        )
        return cfg

    bsr_unset = "TODO_REPLACE" in str(info.get("bsr_url", "")) or "TODO_REPLACE" in str(info.get("bsr_node_id", ""))
    if bsr_unset:
        if not args.allow_discover:
            print(
                f"[Catalog] 品类 '{key}' 的 BSR 节点尚未填充（still TODO_REPLACE）。\n"
                f"           请在 category_catalog.json 替换为真实值，"
                f"或加 --allow-discover 让脚本自动发现并回填。",
                file=sys.stderr,
            )
            sys.exit(2)
        cfg["category"] = key
        cfg["search_keyword"] = info.get("search_keyword") or infer_search_keyword(key)
        cfg["market"] = info.get("market", cfg["market"])
        cfg["bsr_url"] = ""
        cfg["bsr_node_id"] = ""
        cfg["bsr_node_path"] = ""
        cfg["_discover_mode"] = True
        cfg["_discover_existing_aliases"] = list(info.get("aliases") or [])
        if args.max_products:
            cfg["max_products"] = args.max_products
        print(
            f"[Catalog] 🔍 品类 '{key}' 标记为 TODO + --allow-discover 启用 → 进入动态发现模式",
            file=sys.stderr,
        )
        return cfg

    cfg["category"] = key
    cfg["search_keyword"] = info.get("search_keyword", cfg["search_keyword"])
    cfg["market"] = info.get("market", cfg["market"])
    cfg["bsr_url"] = info["bsr_url"]
    cfg["bsr_node_id"] = info["bsr_node_id"]
    cfg["bsr_node_path"] = info.get("bsr_node_path", "")
    cfg["_discover_mode"] = False

    if args.max_products:
        cfg["max_products"] = args.max_products

    print(f"[Catalog] 加载品类: {key}  |  BSR: {cfg['bsr_url']}", file=sys.stderr)
    return cfg


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="知微 Amazon BSR Scraper")
    parser.add_argument("--category", "-c", default="",
                        help="目标品类名（任意大小写/中文/英文/别名，将自动归一化到 catalog 中的规范键）")
    parser.add_argument("--max-products", type=int, default=0, help="覆盖默认产品数")
    parser.add_argument(
        "--allow-discover",
        action="store_true",
        default=os.getenv("ALLOW_DISCOVER", "").strip().lower() in ("1", "true", "yes", "on"),
        help=(
            "catalog 找不到品类或为 TODO 时，自动从 Amazon 搜索发现 BSR 节点并写回 catalog。"
            "本地默认关闭（防误抓），GitHub Action 工作流默认开启。"
            "也可通过环境变量 ALLOW_DISCOVER=1 启用。"
        ),
    )
    return parser.parse_args(argv)


CONFIG = DEFAULT_CONFIG


# ============================================================
# STEALTH —— 反反爬资源池
# ============================================================
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1680, "height": 1050},
]

STEALTH_INIT_JS = """
// 1. 抹除 navigator.webdriver 痕迹
Object.defineProperty(navigator, 'webdriver', { get: () => false });

// 2. 伪造 plugins / mimeTypes (无头浏览器默认为空)
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', description: '' },
        { name: 'Native Client', description: '' }
    ]
});
Object.defineProperty(navigator, 'mimeTypes', {
    get: () => [{ type: 'application/pdf' }]
});

// 3. 伪造 navigator.languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en']
});

// 4. 伪造 chrome.runtime (Chromium headless 缺失)
window.chrome = {
    runtime: { connect: () => {}, sendMessage: () => {} },
    loadTimes: () => {},
    csi: () => {},
    app: {}
};

// 5. 修复 permissions.query (无头浏览器 notifications 永远 default)
const origQuery = navigator.permissions && navigator.permissions.query;
if (origQuery) {
    navigator.permissions.query = (parameters) => (
        parameters && parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(parameters)
    );
}

// 6. 隐藏 Playwright 注入痕迹
delete window.__playwright;
delete window.__pw_manual;
delete window.__PW_inspect;

// 7. WebGL 指纹随机化 (固定指纹会被识别)
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, parameter);
};
"""


def is_blocked_page(title: str, body_text: str = "") -> bool:
    """快速检测是否命中 Amazon 反爬拦截页。

    判断依据（必须**真正的拦截页**才返回 True，正常商品页绝不应误报）：
    1. body 中含 Captcha 明确关键短语
    2. title 是纯 "Amazon.com" / "Robot Check" / "Sorry!" 类反爬页特征
    3. title 含 "captcha"

    注意：正常商品页 title 形如 "Amazon.com : Product Name..."，
    含冒号+商品名，是正常页面，绝不能误判为拦截。
    """
    if not title and not body_text:
        return False

    body_low = (body_text or "").lower()
    captcha_phrases = [
        "enter the characters you see",
        "type the characters you see",
        "to discuss automated access",
        "we just need to make sure you're not a robot",
        "click the button below to continue shopping",
    ]
    if any(p in body_low for p in captcha_phrases):
        return True

    t = (title or "").strip().lower()
    blocked_titles = {
        "amazon.com",
        "robot check",
        "sorry! something went wrong on our end.",
        "sorry!",
        "amazon.com: page not found",
    }
    if t in blocked_titles:
        return True
    if "captcha" in t or "robot check" in t:
        return True
    # Amazon 软反爬：标题以 "sorry! something went wrong" 开头 + body 极短/空
    # （正常商品页 body 至少 5000+ 字符，软反爬页只有几十字甚至空）
    if t.startswith("sorry! something went wrong"):
        if len(body_low.strip()) < 200:
            return True

    return False


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class Review:
    asin: str
    star: int
    date: str
    title: str
    body: str
    verified: bool
    weight: float


@dataclass
class Product:
    bsr_rank: int
    asin: str
    title: str
    rating: float
    rating_count: int
    price: str
    weight: float
    reviews: list[Review] = field(default_factory=list)


# ============================================================
# UTILS
# ============================================================

def bsr_weight(rank: int) -> float:
    """BSR#1→1.0, #2→0.9, ..., #10→0.1"""
    return round(max(0.1, 1.0 - (rank - 1) * 0.1), 2)


def random_delay(cfg: dict = CONFIG) -> float:
    return random.uniform(cfg["delay_min"], cfg["delay_max"])


def word_count(text: str) -> int:
    return len(text.split())


def parse_amazon_date(date_text: str) -> str | None:
    """'Reviewed in the United States on January 15, 2025' → '2025-01-15'"""
    months = {
        "January": 1, "February": 2, "March": 3, "April": 4,
        "May": 5, "June": 6, "July": 7, "August": 8,
        "September": 9, "October": 10, "November": 11, "December": 12,
    }
    m = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s*(\d{4})",
        date_text,
    )
    if m:
        month = months[m.group(1)]
        day = int(m.group(2))
        year = int(m.group(3))
        return f"{year}-{month:02d}-{day:02d}"
    return None


def is_within_months(date_str: str, months: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return d >= cutoff
    except ValueError:
        return False


# ============================================================
# SCRAPER
# ============================================================

class AmazonScraper:
    """零依赖 Amazon 采集器: BSR榜单 + 产品页内嵌评论."""

    def __init__(self, cfg: dict = CONFIG):
        self.cfg = cfg
        self.output_dir = Path(cfg["output_dir"])
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.products: list[Product] = []
        self.all_reviews: list[Review] = []

    # ---- Browser lifecycle ----

    async def setup(self):
        ua = random.choice(USER_AGENTS)
        viewport = random.choice(VIEWPORTS)

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.cfg["headless"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-site-isolation-trials",
                "--disable-web-security",
                f"--window-size={viewport['width']},{viewport['height']}",
                "--lang=en-US,en",
            ],
        )
        self.context = await self.browser.new_context(
            user_agent=ua,
            viewport=viewport,
            locale="en-US",
            timezone_id="America/Los_Angeles",
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "max-age=0",
                "Sec-Ch-Ua": '"Chromium";v="140", "Not?A_Brand";v="24", "Google Chrome";v="140"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        await self.context.add_init_script(STEALTH_INIT_JS)
        self.page: Page = await self.context.new_page()
        print(f"[setup] Browser ready  UA={ua[:60]}…  Viewport={viewport['width']}x{viewport['height']}")

    async def human_like_scroll(self, total_steps: int = 6):
        """模拟真人阅读：分段滚动 + 随机停顿。"""
        for i in range(total_steps):
            scroll_y = random.randint(300, 700)
            await self.page.mouse.wheel(0, scroll_y)
            await asyncio.sleep(random.uniform(0.8, 1.8))
        try:
            await self.page.mouse.move(
                random.randint(100, 800),
                random.randint(100, 600),
                steps=random.randint(5, 15)
            )
        except Exception:
            pass

    async def detect_block(self) -> bool:
        """快速检测当前页是否被 Amazon 拦截。"""
        try:
            title = await self.page.title()
            body_text = await self.page.evaluate("() => document.body ? document.body.innerText.substring(0, 800) : ''")
            if is_blocked_page(title, body_text):
                print(f"[BLOCK DETECTED] title='{title[:60]}'  body_snippet='{body_text[:200]}'")
                try:
                    await self.page.screenshot(path=str(self.output_dir / f"blocked_{datetime.now().strftime('%H%M%S')}.png"))
                except Exception:
                    pass
                return True
        except Exception:
            pass
        return False

    async def teardown(self):
        await self.browser.close()
        await self.playwright.stop()
        print("[teardown] Browser closed")

    async def sleep(self, label: str = ""):
        s = random_delay(self.cfg)
        if label:
            print(f"  [wait] {label}: {s:.1f}s")
        await asyncio.sleep(s)

    # ---- Step 0: BSR 节点自动发现（catalog 未注册品类用）----

    async def _dump_discover_failure(self, tag: str) -> None:
        """保存搜索/发现失败时的现场：截图 + HTML 片段 + title + body 摘要。
        所有产物落到 output_dir，会被 Action self-healing commit 一起 push，
        方便事后到仓库直接看（GitHub 上 .png 可直接渲染）。
        """
        try:
            ts = datetime.now().strftime("%H%M%S")
            shot_path = self.output_dir / f"discover_{tag}_{ts}.png"
            try:
                await self.page.screenshot(path=str(shot_path), full_page=False)
                print(f"[Discover] 📸 现场截图: {shot_path.name}")
            except Exception as e:
                print(f"[Discover] 截图失败: {e}")

            try:
                title = await self.page.title()
            except Exception:
                title = "<title_unavailable>"
            try:
                url_now = self.page.url
            except Exception:
                url_now = "<url_unavailable>"
            try:
                body_snip = await self.page.evaluate(
                    "() => (document.body && document.body.innerText || '').slice(0, 1500)"
                )
            except Exception:
                body_snip = "<body_unavailable>"
            try:
                html_snip = await self.page.evaluate(
                    "() => (document.documentElement && document.documentElement.outerHTML || '').slice(0, 5000)"
                )
            except Exception:
                html_snip = ""

            print(f"[Discover] 当前 URL  : {url_now}")
            print(f"[Discover] 页面 title: {title!r}")
            print(f"[Discover] body 前 1500 字 ↓\n{body_snip}\n[Discover] body 摘要结束")

            if html_snip:
                html_path = self.output_dir / f"discover_{tag}_{ts}.html"
                html_path.write_text(html_snip, encoding="utf-8")
                print(f"[Discover] 🧾 HTML 片段(前 5KB): {html_path.name}")
        except Exception as e:
            print(f"[Discover] dump 失败（不阻塞流程）: {e}")

    # ---- Path B: 通过第三方搜索引擎（DDG HTML）找 BSR URL，绕过 Amazon 自家反爬 ----

    # 全局 BSR URL 模式，复用于多个搜索引擎结果解析
    _BSR_URL_RE = re.compile(
        r"amazon\.com(/gp/bestsellers/[a-z0-9\-]+/(\d+))",
        re.IGNORECASE,
    )

    def _grep_bsr_hits(self, html: str) -> list:
        """从任意 HTML 文本里抽取 amazon BSR URL 候选，去重后返回 [(path, node_id), ...]"""
        seen = set()
        hits = []
        for m in self._BSR_URL_RE.finditer(html):
            path, nid = m.group(1), m.group(2)
            if nid in seen:
                continue
            seen.add(nid)
            hits.append((path, nid))
        return hits

    def _query_bing_html(self, query: str) -> str:
        url = "https://www.bing.com/search?" + urllib.parse.urlencode({
            "q": query, "form": "QBLH", "setlang": "en-US", "cc": "us"
        })
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    def _query_brave_html(self, query: str) -> str:
        """Brave Search 公开端点，公开宣称不对 bot 限速，反爬最宽松。"""
        url = "https://search.brave.com/search?" + urllib.parse.urlencode({
            "q": query, "source": "web"
        })
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    def _query_ddg_html(self, query: str) -> str:
        data = urllib.parse.urlencode({"q": query}).encode("utf-8")
        req = urllib.request.Request(
            "https://html.duckduckgo.com/html/",
            data=data,
            method="POST",
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    # 各引擎响应中"被反爬"的特征关键词
    _SE_ANTIBOT_MARKERS = (
        "anomaly.js",              # DuckDuckGo
        "/sorry/index",            # Google sorry page
        "unusual traffic",         # Google
        "turnstile-widget",        # Cloudflare Turnstile（Bing/Brave）
        "captcha_header",          # Bing captcha 页结构
        "challenge-form",          # 通用挑战页
        "please verify you are a human",
    )

    def _discover_via_search_engine(self, keyword: str) -> dict | None:
        """搜 'site:amazon.com {keyword} best sellers'，从结果 URL 里 grep
        `/gp/bestsellers/<dept>/<NODE_ID>/...` 模式。

        引擎链：Brave（最宽松）→ Bing → DuckDuckGo（兜底）
        urllib 直接打，无需 Playwright，绕过 Amazon `/s?k=` 软反爬。
        失败时 dump 响应前 800 字到日志，便于在 Action 端诊断。
        """
        queries = [
            f"site:amazon.com {keyword} best sellers",
            f"site:amazon.com {keyword} bestsellers",
            f"amazon best sellers {keyword}",
        ]
        engines = [
            ("Brave", self._query_brave_html),
            ("Bing", self._query_bing_html),
            ("DDG", self._query_ddg_html),
        ]

        for q in queries:
            for engine_name, fetcher in engines:
                try:
                    print(f"[Discover/SE] {engine_name} : q={q!r}")
                    html = fetcher(q)
                    if not html or len(html) < 500:
                        print(f"[Discover/SE]   {engine_name} 响应过短 ({len(html)} bytes)")
                        continue

                    low = html.lower()
                    blocker = next((s for s in self._SE_ANTIBOT_MARKERS if s in low), None)
                    if blocker:
                        print(f"[Discover/SE]   {engine_name} 命中反爬特征 '{blocker}'，跳过")
                        continue

                    # 直接全文 grep（适用 Brave/Bing 直接给原 URL 的场景）
                    hits = self._grep_bsr_hits(html)

                    # DDG 把 URL 包在 uddg= 里，需先解码再 grep
                    if not hits and "uddg=" in html:
                        decoded = "\n".join(
                            urllib.parse.unquote(m.group(1))
                            for m in re.finditer(r"uddg=([^&\"]+)", html)
                        )
                        hits = self._grep_bsr_hits(decoded)

                    print(f"[Discover/SE]   {engine_name} → 命中 BSR URL {len(hits)} 条 (html={len(html)}B)")
                    for p, nid in hits[:5]:
                        print(f"    - node_id={nid}  path={p}")

                    if hits:
                        path, nid = hits[0]
                        return {
                            "bsr_url": f"https://www.amazon.com{path}",
                            "bsr_node_id": nid,
                            "bsr_node_path": "",
                            "_se_engine": engine_name,
                        }
                    # 0 命中但响应正常 → 截一小段响应给日志，便于事后诊断
                    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                    title = (title_m.group(1).strip() if title_m else "")[:120]
                    body_strip = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))[:600]
                    print(f"[Discover/SE]   {engine_name} 0 命中。title={title!r}")
                    print(f"[Discover/SE]   body 前 600 字: {body_strip[:600]}")
                except Exception as e:
                    print(f"[Discover/SE]   {engine_name} 查询失败: {str(e)[:120]}")
                    continue

        return None

    async def _enrich_bsr_path_from_static_page(self, bsr_url: str) -> str:
        """进 BSR 静态榜单页读 title/breadcrumb，补全 bsr_node_path。
        失败返回空串，不阻塞主流程（path 仅用于 catalog 元信息，不影响抓取）。
        """
        try:
            await self.page.goto(bsr_url, wait_until="domcontentloaded", timeout=30000)
            await self.sleep("bsr meta")
            title = await self.page.title()
            m = re.search(r"Best Sellers in\s+(.+?)\s*$", title or "", re.IGNORECASE)
            if m:
                return m.group(1).strip()[:200]
            try:
                breadcrumb = await self.page.evaluate(
                    "() => { const el = document.querySelector('#zg_browseRoot, .zg_browseRoot, [data-cy=\"breadcrumb\"]'); return el ? (el.textContent || '').trim() : ''; }"
                )
                if breadcrumb:
                    return re.sub(r"\s+", " ", breadcrumb)[:200]
            except Exception:
                pass
        except Exception as e:
            print(f"[Discover] enrich path 失败（不阻塞）: {e}")
        return ""

    async def discover_bsr_node(self) -> dict:
        """从 search_keyword 出发，自动找出该品类在 Amazon 的 BSR 叶子节点。

        双路径设计：
          Path A: Amazon /s?k=xxx → 商品页 BSR 块（最准，但 Action IP 经常被软拦截）
          Path B: 第三方搜索引擎（DDG HTML）→ 从 site:amazon.com 结果里 grep BSR URL
                  （绕过 Amazon 自家反爬；命中后进 BSR 静态页补 path）

        任一路径成功即返回 + 回填 self.cfg。两路都失败抛 RuntimeError。
        """
        keyword = (self.cfg.get("search_keyword") or "").strip()
        if not keyword:
            raise RuntimeError("discover: 空 search_keyword")

        path_a_err: Exception | None = None
        try:
            return await self._discover_via_amazon_search(keyword)
        except Exception as e:
            path_a_err = e
            print(f"\n[Discover] ⚠️ Path A (Amazon 搜索) 失败: {e}")
            print(f"[Discover] 🔄 切换 Path B (第三方搜索引擎绕过 Amazon 反爬)")

        result_b = self._discover_via_search_engine(keyword)
        if result_b and result_b.get("bsr_node_id"):
            bsr_path = await self._enrich_bsr_path_from_static_page(result_b["bsr_url"])
            self.cfg["bsr_url"] = result_b["bsr_url"]
            self.cfg["bsr_node_id"] = result_b["bsr_node_id"]
            self.cfg["bsr_node_path"] = bsr_path or result_b["bsr_node_path"]
            print(
                f"[Discover] ✅ Path B 命中 | node_id={result_b['bsr_node_id']} "
                f"path={(bsr_path or '?')!r} url={result_b['bsr_url']}"
            )
            return {
                "bsr_url": self.cfg["bsr_url"],
                "bsr_node_id": self.cfg["bsr_node_id"],
                "bsr_node_path": self.cfg["bsr_node_path"],
            }

        raise RuntimeError(
            f"discover: Path A 和 Path B 均失败（Path A: {path_a_err}; Path B: 无 BSR URL 命中）"
        )

    async def _discover_via_amazon_search(self, keyword: str) -> dict:
        """Path A：Amazon 自家搜索 → 取非赞助 ASIN → 进商品页提 BSR 块。
        被反爬时立刻抛异常，让上层切到 Path B。
        """
        # 带 ref=nb_sb_noss 模拟从首页搜索框跳转的引用来源，降低被识别为爬虫的概率
        search_url = f"{self.cfg['base_url']}/s?k={keyword.replace(' ', '+')}&ref=nb_sb_noss"
        print(f"\n[Discover] 🔍 搜索: {search_url}")
        try:
            await self.page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            raise RuntimeError(f"discover: 搜索页加载失败 {e}")
        await self.sleep("search page")

        # Amazon 搜索结果是动态注入的，等 networkidle（最多 6s）以确保 SSR/JS 全部完成
        try:
            await self.page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass

        if await self.detect_block():
            await self._dump_discover_failure("blocked_search")
            raise RuntimeError("discover: 搜索页被反爬拦截")

        await self.human_like_scroll(total_steps=3)

        # 多选择器兜底：覆盖 Amazon 搜索页历年来的 DOM 变体
        candidate_selectors = [
            "div[data-component-type='s-search-result']",
            "div[data-asin][data-component-type]",
            "div.s-result-item[data-asin]",
            "div[role='listitem'][data-asin]",
            "div[data-cel-widget^='search_result_']",
            "div[data-asin]:not([data-asin=''])",
        ]
        cards = []
        sel_report: list[str] = []
        for sel in candidate_selectors:
            try:
                elems = await self.page.query_selector_all(sel)
                sel_report.append(f"{sel} → {len(elems)}")
                if elems and not cards:
                    cards = elems
            except Exception as e:
                sel_report.append(f"{sel} → err:{str(e)[:30]}")
        print(f"[Discover] 选择器命中情况:")
        for line in sel_report:
            print(f"  - {line}")
        print(f"[Discover] 选用首个非空候选集，共 {len(cards)} 张")

        candidate_asins: list[str] = []
        for card in cards:
            try:
                sponsored = await card.query_selector("span:has-text('Sponsored')")
                if sponsored:
                    continue
                asin = await card.get_attribute("data-asin") or ""
                if asin and len(asin) == 10 and asin.startswith("B") and asin not in candidate_asins:
                    candidate_asins.append(asin)
                if len(candidate_asins) >= 5:
                    break
            except Exception:
                continue

        if not candidate_asins:
            for card in cards[:8]:
                try:
                    asin = await card.get_attribute("data-asin") or ""
                    if asin and len(asin) == 10 and asin.startswith("B") and asin not in candidate_asins:
                        candidate_asins.append(asin)
                except Exception:
                    continue

        if not candidate_asins:
            await self._dump_discover_failure("no_asin")
            raise RuntimeError(f"discover: 搜索 '{keyword}' 无可用 ASIN")

        print(f"[Discover] 过滤后候选 ASIN: {candidate_asins[:5]}")

        last_err = None
        for asin in candidate_asins[:3]:
            try:
                info = await self._extract_bsr_from_product(asin)
                if info and info.get("bsr_node_id"):
                    self.cfg["bsr_url"] = info["bsr_url"]
                    self.cfg["bsr_node_id"] = info["bsr_node_id"]
                    self.cfg["bsr_node_path"] = info["bsr_node_path"]
                    print(
                        f"[Discover] ✅ 节点确认 | node_id={info['bsr_node_id']} "
                        f"path='{info['bsr_node_path']}' url={info['bsr_url']}"
                    )
                    return info
            except Exception as e:
                last_err = e
                print(f"[Discover] ASIN {asin} 提取失败: {e}，尝试下一个")
            await asyncio.sleep(random.uniform(2.0, 4.0))

        raise RuntimeError(f"discover: 3 个候选 ASIN 均未能提取 BSR 节点 (last_err={last_err})")

    async def _extract_bsr_from_product(self, asin: str) -> dict | None:
        """打开商品详情页，从 BSR 块取最深一级节点链接 + ID + 路径文本。

        Amazon 详情页里 BSR 链接通常出现在：
          - #detailBullets_feature_div 内的 li
          - #productDetails_detailBullets_sections1 表格
          - "Product information" 折叠区
        我们用 JS 全页扫描 `a[href*='/gp/bestsellers/']`，挑路径最深的（叶子品类）。
        """
        url = f"{self.cfg['base_url']}/dp/{asin}/"
        print(f"  [extract] {url}")
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  [extract] 加载失败: {e}")
            return None
        await self.sleep(f"product {asin}")
        if await self.detect_block():
            print(f"  [extract] {asin} 被反爬拦截")
            return None

        await self.human_like_scroll(total_steps=4)
        try:
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        await asyncio.sleep(random.uniform(1.0, 2.0))

        bsr_links = await self.page.evaluate(
            """
            () => {
                const anchors = Array.from(document.querySelectorAll(
                    "a[href*='/gp/bestsellers/'], a[href*='/Best-Sellers-']"
                ));
                return anchors.map(a => {
                    let host = a.closest('li, tr, span, p, div');
                    return {
                        href: a.getAttribute('href') || '',
                        text: (a.textContent || '').trim(),
                        host_text: host ? (host.textContent || '').trim().slice(0, 300) : ''
                    };
                }).filter(x => x.href);
            }
            """
        )
        if not bsr_links:
            print(f"  [extract] 未找到 BSR 链接")
            return None

        def depth(href: str) -> int:
            return len(href.strip("/").split("/"))

        best = max(
            enumerate(bsr_links),
            key=lambda iv: (depth(iv[1]["href"]), iv[0]),
        )[1]

        href = best["href"]
        if href.startswith("/"):
            href = self.cfg["base_url"] + href

        m = re.search(r"/bestsellers/[^/?#]+/(\d+)", href)
        node_id = m.group(1) if m else ""
        if not node_id:
            print(f"  [extract] href 中未解析出 node_id: {href}")
            return None

        host_text = best.get("host_text", "")
        path_m = re.search(r"in\s+([^(\n]+?)(?:\s*\(|$)", host_text)
        bsr_path = (path_m.group(1) if path_m else best.get("text", "")).strip()
        bsr_path = re.sub(r"\s*[#＃]?\d[\d,]*\s*", "", bsr_path).strip()
        bsr_path = re.sub(r"\s+", " ", bsr_path)[:200]

        return {
            "bsr_url": href.split("?")[0].split("#")[0],
            "bsr_node_id": node_id,
            "bsr_node_path": bsr_path,
        }

    # ---- Step 1: BSR 榜单 ----

    async def validate_bsr_page(self) -> bool:
        """飞行前校验: 确认 BSR 页面加载正确."""
        title = await self.page.title()
        print(f"  [check] Page title: {title}")
        items = await self.page.query_selector_all(
            "div#gridItemRoot, div[data-asin], div.p13n-sc-uncoverable-faceout"
        )
        ok = len(items) > 0
        print(f"  [{'OK' if ok else 'FAIL'}] {len(items)} product elements detected")
        return ok

    async def scrape_bsr_page(self) -> list[Product]:
        """从 BSR 品类页抓取 Top N 产品."""
        url = self.cfg["bsr_url"]
        print(f"\n[BSR] {url}")
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await self.sleep("BSR page")

        if await self.detect_block():
            print("[BSR] 命中反爬拦截，跳过本次抓取")
            return []

        await self.human_like_scroll(total_steps=4)
        try:
            await self.page.screenshot(path=str(self.output_dir / "bsr_page.png"))
        except Exception:
            pass

        await self.validate_bsr_page()

        items = await self.page.query_selector_all("div#gridItemRoot")
        if not items:
            items = await self.page.query_selector_all("div[data-asin]")

        print(f"[BSR] {len(items)} items found, extracting top {self.cfg['max_products']}...")
        products: list[Product] = []
        seen_asins: set = set()

        for i, item in enumerate(items):
            if len(products) >= self.cfg["max_products"]:
                break
            try:
                # ASIN
                asin_div = await item.query_selector("div[data-asin]")
                asin = await asin_div.get_attribute("data-asin") if asin_div else None
                if not asin:
                    asin = await item.get_attribute("data-asin")
                if not asin or asin in seen_asins:
                    continue
                seen_asins.add(asin)

                # BSR rank
                rank = len(products) + 1
                badge = await item.query_selector("span.zg-bdg-text")
                if badge:
                    m = re.search(r"(\d+)", (await badge.inner_text()).strip())
                    if m:
                        rank = int(m.group(1))

                # Title (from product image alt)
                img = await item.query_selector("img")
                title = (await img.get_attribute("alt")) if img else "N/A"
                if not title or title == "N/A":
                    link_el = await item.query_selector("a[href*='/dp/'] span")
                    title = (await link_el.inner_text()).strip() if link_el else "N/A"

                # Rating
                rating_el = await item.query_selector("span.a-icon-alt")
                rating_text = (await rating_el.inner_text()).strip() if rating_el else "0"
                rating = 0.0
                if m := re.search(r"([\d.]+)", rating_text):
                    rating = float(m.group(1))

                # Rating count
                count_el = await item.query_selector("span.a-size-small")
                count_text = (await count_el.inner_text()).strip() if count_el else "0"
                rating_count = int(count_text.replace(",", "")) if count_text.replace(",", "").isdigit() else 0

                # Price (类名含 hash 前后缀，需部分匹配)
                price_el = await item.query_selector("span[class*='p13n-sc-price']")
                if not price_el:
                    price_el = await item.query_selector("span.a-color-price")
                price = (await price_el.inner_text()).strip() if price_el else "N/A"

                w = bsr_weight(rank)
                p = Product(bsr_rank=rank, asin=asin, title=title,
                            rating=rating, rating_count=rating_count, price=price, weight=w)
                products.append(p)
                print(f"  [#{rank}] {asin} | {title[:60]}... | {rating}* ({rating_count}) | {price} | w={w}")

            except Exception as e:
                print(f"  [ERR BSR #{i+1}] {e}")
                continue

        self.products = products
        print(f"[BSR] {len(products)} products collected")
        return products

    # ---- Step 2: 产品页内嵌评论 ----

    async def scrape_reviews_for_product(self, product: Product) -> list[Review]:
        """从产品详情页采集内嵌评论 (无需登录)."""
        asin = product.asin
        url = f"{self.cfg['base_url']}/dp/{asin}/"
        print(f"\n[Reviews] {asin} | {product.title[:60]}...")

        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  [ERR] Page load: {e}")
            return []
        await self.sleep(f"product {asin}")

        if await self.detect_block():
            print(f"  [SKIP] {asin} blocked by anti-bot")
            return []

        await self.human_like_scroll(total_steps=3)
        for pct in [0.5, 0.75]:
            await self.page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {pct})")
            await asyncio.sleep(random.uniform(0.8, 1.5))

        all_collected: list[Review] = []

        # 第一页 (初始可见)
        cards = await self.page.query_selector_all("div[data-hook='review']")
        print(f"  Page 1: {len(cards)} cards")
        await self._collect_cards(cards, asin, product.weight, all_collected)

        # 翻页尝试 (产品页内翻页)
        for page_idx in range(self.cfg["max_review_pages"] - 1):
            # 尝试多种翻页按钮
            clicked = False
            for sel in [
                "li.a-last a",
                "a:has-text('Next')",
                "a[href*='reviewerType']",
                "span:has-text('See more reviews')",
                "a:has-text('See more reviews')",
            ]:
                btn = await self.page.query_selector(sel)
                if btn:
                    try:
                        await btn.click()
                        await asyncio.sleep(2.0)
                        clicked = True
                        break
                    except:
                        continue
            if not clicked:
                break

            cards = await self.page.query_selector_all("div[data-hook='review']")
            new_before = len(all_collected)
            await self._collect_cards(cards, asin, product.weight, all_collected)
            new_count = len(all_collected) - new_before
            print(f"  Page {page_idx + 2}: {len(cards)} cards, +{new_count} qualifying")
            if new_count == 0:
                break

        pos = len([r for r in all_collected if r.star >= 4])
        neg = len([r for r in all_collected if r.star <= 3])
        print(f"  [Reviews] {asin}: {len(all_collected)} total ({pos}P/{neg}N)")
        return all_collected

    async def _collect_cards(self, cards, asin: str, weight: float, collected: list[Review]):
        for card in cards:
            try:
                review = await self._parse_review_card(card, asin, weight)
                if review and review not in collected:
                    collected.append(review)
                    print(f"    [OK] {review.date} | {review.star}* | {review.title[:40]}...")
            except Exception as e:
                print(f"    [ERR] {e}")
                continue

    async def _parse_review_card(self, card, asin: str, weight: float) -> Review | None:
        """解析单条评论卡片 (兼容产品页 reviewTitle/reviewText)."""
        # Star
        star_el = await card.query_selector("[data-hook='review-star-rating'] span.a-icon-alt")
        star_text = (await star_el.inner_text()).strip() if star_el else ""
        m = re.search(r"([\d.]+)", star_text)
        star = int(float(m.group(1))) if m else 0

        # Title (产品页用 reviewTitle)
        title_el = await card.query_selector("[data-hook='reviewTitle']")
        title = (await title_el.inner_text()).strip() if title_el else ""

        # Body (产品页用 reviewText)
        body_el = await card.query_selector("[data-hook='reviewText']")
        body = (await body_el.inner_text()).strip() if body_el else ""

        # Date
        date_el = await card.query_selector("[data-hook='review-date']")
        date_text = (await date_el.inner_text()).strip() if date_el else ""
        date_str = parse_amazon_date(date_text)

        # Verified
        verified = await card.query_selector("span[data-hook='avp-badge']") is not None

        # Filters
        if star == 0:
            return None
        if word_count(body) < self.cfg["min_review_words"]:
            return None
        if not verified:
            return None
        if date_str is None or not is_within_months(date_str, self.cfg["max_review_age_months"]):
            return None

        return Review(asin=asin, star=star, date=date_str, title=title,
                       body=body, verified=True, weight=weight)

    # ---- Step 3: 输出 ----

    def save_markdown(self, products: list[Product]) -> Path:
        """产出结构化 Markdown."""
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.cfg['category'].replace(' ', '_')}_{now}.md"
        filepath = self.output_dir / filename

        lines = [
            f"# Amazon BSR 快照: {self.cfg['category']}",
            f"> 采集时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"> 市场: {self.cfg['market']}",
            f"> BSR 节点: {self.cfg['bsr_node_path']} (NodeID: {self.cfg['bsr_node_id']})",
            f"> 产品数: {len(products)}",
            "",
            "## BSR Top 10 产品",
            "",
            "| BSR# | ASIN | 产品名称 | 评分 | 评论数 | 价格 | weight |",
            "|------|------|----------|------|--------|------|--------|",
        ]
        for p in products:
            title = p.title.replace("|", "\\|")[:80]
            lines.append(f"| {p.bsr_rank} | {p.asin} | {title} | {p.rating} | {p.rating_count} | {p.price} | {p.weight} |")
        lines += ["", "## 评论快照", ""]

        total = 0
        for p in products:
            pos = [r for r in p.reviews if r.star >= 4]
            neg = [r for r in p.reviews if r.star <= 3]
            lines.append(f"### #{p.bsr_rank} {p.asin} — {p.title[:80]} | weight={p.weight}")
            lines.append(f"> 好评 {len(pos)} | 差评 {len(neg)}")
            lines.append("")
            if p.reviews:
                lines.append("| # | 星级 | 日期 | 标题 | 评论内容 | Verified | weight |")
                lines.append("|---|------|------|------|----------|----------|--------|")
                for idx, r in enumerate(p.reviews, 1):
                    title = r.title.replace("|", "\\|")[:50]
                    body = r.body.replace("|", "\\|").replace("\n", " ")[:200]
                    lines.append(f"| {idx} | {r.star} | {r.date} | {title} | {body} | Yes | {r.weight} |")
                lines.append("")
                total += len(p.reviews)
            else:
                lines.append("_(无合格评论)_")
                lines.append("")

        lines.append("---")
        lines.append(f"*总计: {len(products)} 产品, {total} 条合格评论*")

        content = "\n".join(lines)
        filepath.write_text(content, encoding="utf-8")
        print(f"\n[Output] Markdown -> {filepath}  ({len(products)} products, {total} reviews)")
        return filepath

    def save_json(self, products: list[Product]) -> Path:
        """保存原始 JSON (调试/中间存档)."""
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.cfg['category'].replace(' ', '_')}_{now}.json"
        filepath = self.output_dir / filename
        data = {
            "meta": {
                "category": self.cfg["category"],
                "market": self.cfg["market"],
                "bsr_node_id": self.cfg["bsr_node_id"],
                "collected_at": datetime.now().isoformat(),
            },
            "products": [
                {**{k: v for k, v in asdict(p).items() if k != "reviews"},
                 "reviews": [asdict(r) for r in p.reviews]}
                for p in products
            ],
        }
        filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[Output] JSON -> {filepath}")
        return filepath

    # ---- Main ----

    async def run(self):
        """主流程: [Discover] → BSR → 评论 → 输出 → [回填 catalog]."""
        print("=" * 50)
        print(f"  知微Agent · Amazon BSR Scraper")
        print(f"  品类: {self.cfg['category']}  |  市场: {self.cfg['market']}")
        if self.cfg.get("_discover_mode"):
            print(f"  模式: 🔍 动态发现 (search_keyword='{self.cfg.get('search_keyword', '')}')")
        print("=" * 50)

        await self.setup()

        try:
            # Step 0: 动态发现模式 → 先发现 BSR 节点
            if self.cfg.get("_discover_mode") and not self.cfg.get("bsr_url"):
                try:
                    await self.discover_bsr_node()
                except Exception as e:
                    print(f"\n[FATAL] BSR 节点发现失败: {e}")
                    print(
                        "         未发现节点意味着 Amazon 搜索无结果或反爬命中，"
                        "本次中止以避免污染 catalog。\n"
                        "         可稍后重试，或人工在 category_catalog.json 填入 BSR 节点。"
                    )
                    return

            # Step 1: BSR 榜单
            products = await self.scrape_bsr_page()
            if not products:
                print("[ERROR] No products found — aborting")
                return

            # Step 2: 逐产品评论采集
            for idx, product in enumerate(products):
                if idx > 0:
                    cd = self.cfg["product_cooldown"]
                    print(f"\n  [cooldown] {cd:.0f}s ...")
                    await asyncio.sleep(cd)

                product.reviews = await self.scrape_reviews_for_product(product)
                self.all_reviews.extend(product.reviews)
                self.save_json(products)  # 中间存档

            # Step 3: 最终输出
            self.save_markdown(products)
            self.save_json(products)

            # Step 4: 动态发现品类 → 写回 catalog（commit & push 由 Action 兜底）
            if self.cfg.get("_discover_mode"):
                append_to_catalog(
                    self.cfg["category"],
                    {
                        "search_keyword": self.cfg.get("search_keyword", ""),
                        "market": self.cfg.get("market", "amazon.com"),
                        "bsr_url": self.cfg.get("bsr_url", ""),
                        "bsr_node_id": self.cfg.get("bsr_node_id", ""),
                        "bsr_node_path": self.cfg.get("bsr_node_path", ""),
                    },
                    extra_aliases=self.cfg.get("_discover_existing_aliases") or [],
                )

            print("\n[DONE]")

        finally:
            await self.teardown()


# ============================================================
# ENTRY
# ============================================================

async def main():
    args = parse_args()
    cfg = build_config(args)
    await AmazonScraper(cfg).run()

if __name__ == "__main__":
    asyncio.run(main())
