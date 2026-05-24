"""
Amazon BSR + Reviews Scraper for 知微Agent.
零依赖自动化: 无需登录/Cookie, 从产品页内嵌评论采集。
品类: Pet Water Fountain | 市场: amazon.com
产出: 含 weight 和 date 字段的结构化 Markdown 快照
"""

import asyncio
import random
import re
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import dataclass, field, asdict
from playwright.async_api import async_playwright, Page


# ============================================================
# CONFIG —— 品类配置 (换品类只改这里)
# ============================================================
CONFIG = {
    # ---- 品类 ----
    "category": "Pet Water Fountain",
    "search_keyword": "pet water fountain",
    "market": "amazon.com",
    "base_url": "https://www.amazon.com",
    # ---- BSR 节点 (需实测验证) ----
    "bsr_url": "https://www.amazon.com/gp/bestsellers/pet-supplies/2975263011",
    "bsr_node_id": "2975263011",
    "bsr_node_path": "Pet Supplies > Cat Fountains",
    # ---- 采集参数 ----
    "max_products": 10,
    "max_review_pages": 3,       # 产品页翻页尝试次数
    "min_review_words": 50,
    "max_review_age_months": 18,
    # ---- 节奏控制 (反反爬) ----
    "headless": os.getenv("CI", "").lower() == "true" or False,  # CI 环境自动无头
    "delay_min": 2.0,            # 最短随机延迟
    "delay_max": 5.0,            # 最长随机延迟
    "product_cooldown": 8.0,     # 产品间冷却
    # ---- 输出 ----
    "output_dir": str(Path(__file__).parent / "output"),
}


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
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.cfg["headless"],
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        self.context = await self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        await self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => false });"
        )
        self.page: Page = await self.context.new_page()
        print("[setup] Browser ready")

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
        await self.page.screenshot(path=str(self.output_dir / "bsr_page.png"))

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

        # 滚动加载懒渲染评论
        for pct in [0.5, 0.75]:
            await self.page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {pct})")
            await asyncio.sleep(1.0)

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
    await AmazonScraper(CONFIG).run()

if __name__ == "__main__":
    asyncio.run(main())
