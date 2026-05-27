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


def build_config(args) -> dict:
    """根据命令行参数 + catalog 构建最终 CONFIG。"""
    cfg = dict(DEFAULT_CONFIG)
    catalog = load_catalog()
    requested = args.category or os.getenv("SCRAPE_CATEGORY", "").strip() or cfg["category"]

    key, info = resolve_category(requested, catalog)
    if not info:
        print(f"[Catalog] 未找到品类 '{requested}'，使用 DEFAULT_CONFIG", file=sys.stderr)
        return cfg

    if "TODO_REPLACE" in str(info.get("bsr_url", "")) or "TODO_REPLACE" in str(info.get("bsr_node_id", "")):
        print(
            f"[Catalog] 品类 '{key}' 的 BSR 节点尚未填充（still TODO_REPLACE）。"
            f"\n请先在 category_catalog.json 里把 bsr_url 与 bsr_node_id 替换为真实值。",
            file=sys.stderr
        )
        sys.exit(2)

    cfg["category"] = key
    cfg["search_keyword"] = info.get("search_keyword", cfg["search_keyword"])
    cfg["market"] = info.get("market", cfg["market"])
    cfg["bsr_url"] = info["bsr_url"]
    cfg["bsr_node_id"] = info["bsr_node_id"]
    cfg["bsr_node_path"] = info.get("bsr_node_path", "")

    if args.max_products:
        cfg["max_products"] = args.max_products

    print(f"[Catalog] 加载品类: {key}  |  BSR: {cfg['bsr_url']}", file=sys.stderr)
    return cfg


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="知微 Amazon BSR Scraper")
    parser.add_argument("--category", "-c", default="",
                        help="目标品类名（任意大小写/中文/英文/别名，将自动归一化到 catalog 中的规范键）")
    parser.add_argument("--max-products", type=int, default=0, help="覆盖默认产品数")
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
    """快速检测是否命中 Amazon 反爬拦截页。"""
    if not title:
        return False
    t = title.lower()
    if any(x in t for x in ["robot check", "captcha", "sorry", "amazon.com"]) and "best seller" not in t:
        pass
    body_low = (body_text or "").lower()
    return (
        "enter the characters you see" in body_low
        or "to discuss automated access" in body_low
        or "type the characters you see" in body_low
        or t.startswith("amazon.com")
    )


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
        """主流程: BSR → 评论 → 输出."""
        print("=" * 50)
        print(f"  知微Agent · Amazon BSR Scraper")
        print(f"  品类: {self.cfg['category']}  |  市场: {self.cfg['market']}")
        print("=" * 50)

        await self.setup()

        try:
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
