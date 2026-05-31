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

# ===== Amazon BSR 顶级部门表（用于 Path C 目录树发现） =====
# 这些 URL 走 /Best-Sellers/zgbs/<slug>/ 路径，已验证不被 Amazon 反爬拦截。
# (display_name, slug) 顺序无关，发现时会按关键词命中度排序。
AMAZON_BSR_DEPTS: list[tuple[str, str]] = [
    ("Amazon Devices & Accessories", "amazon-devices"),
    ("Appliances", "appliances"),
    ("Arts, Crafts & Sewing", "arts-crafts"),
    ("Automotive", "automotive"),
    ("Baby", "baby-products"),
    ("Beauty & Personal Care", "beauty"),
    ("Books", "books"),
    ("Cell Phones & Accessories", "wireless"),
    ("Clothing, Shoes & Jewelry", "fashion"),
    ("Electronics", "electronics"),
    ("Grocery & Gourmet Food", "grocery"),
    ("Health & Household", "hpc"),
    ("Home & Kitchen", "home-garden"),
    ("Industrial & Scientific", "industrial"),
    ("Movies & TV", "movies-tv"),
    ("Musical Instruments", "musical-instruments"),
    ("Office Products", "office-products"),
    ("Patio, Lawn & Garden", "lawn-garden"),
    ("Pet Supplies", "pet-supplies"),
    ("Sports & Outdoors", "sporting-goods"),
    ("Tools & Home Improvement", "hi"),
    ("Toys & Games", "toys-and-games"),
    ("Video Games", "videogames"),
]

# 关键词 → 优先部门 种子库（覆盖跨境电商常见品类 ≥ 90%）
# 命中任一关键词即把该部门提到候选列表前面；未命中关键词则按部门字典序兜底
DEPT_KEYWORD_SEEDS: dict[str, list[str]] = {
    "Sports & Outdoors": [
        "yoga", "mat", "fitness", "exercise", "gym", "weight", "dumbbell", "barbell",
        "treadmill", "elliptical", "rowing", "bike", "cycling", "running", "hiking",
        "tent", "camping", "ski", "snowboard", "swim", "fishing", "golf", "tennis",
        "basketball", "soccer", "kayak", "paddle", "skate", "scooter",
    ],
    "Pet Supplies": [
        "pet", "dog", "cat", "puppy", "kitten", "fish", "aquarium", "bird",
        "litter", "fountain", "leash", "collar", "kennel", "crate", "feeder",
    ],
    "Home & Kitchen": [
        "kitchen", "blender", "cookware", "knife", "skillet", "pan", "pot",
        "vacuum", "robot vacuum", "lamp", "sofa", "bed", "mattress", "pillow",
        "rug", "curtain", "towel", "bedding", "iron", "humidifier", "dehumidifier",
        "air purifier", "fan", "espresso", "coffee maker", "kettle", "toaster",
    ],
    "Electronics": [
        "phone", "smartphone", "laptop", "tablet", "headphone", "earbud", "speaker",
        "bluetooth", "tv", "monitor", "camera", "drone", "smart watch", "smartwatch",
        "soundbar", "router", "modem", "ssd", "hdd", "usb", "charger", "power bank",
        "console", "gaming",
    ],
    "Beauty & Personal Care": [
        "beauty", "makeup", "skincare", "shampoo", "conditioner", "lipstick",
        "perfume", "cologne", "lotion", "razor", "trimmer", "hair dryer", "curling",
        "straightener", "nail",
    ],
    "Health & Household": [
        "vitamin", "supplement", "protein", "first aid", "thermometer", "scale",
        "massage", "massager", "bandage", "blood pressure", "glucose", "mask",
    ],
    "Tools & Home Improvement": [
        "drill", "saw", "tool", "hammer", "wrench", "ladder", "tape measure",
        "screwdriver", "sander", "compressor", "flashlight", "lock",
    ],
    "Toys & Games": [
        "toy", "lego", "puzzle", "doll", "board game", "card game", "plush",
        "action figure", "rc car", "kite",
    ],
    "Baby": [
        "baby", "infant", "toddler", "diaper", "stroller", "crib", "bottle",
        "pacifier", "highchair", "car seat", "monitor",
    ],
    "Office Products": [
        "desk", "chair", "stapler", "binder", "notebook", "printer", "pen",
        "marker", "calculator", "label",
    ],
    "Automotive": [
        "car", "auto", "tire", "wheel", "engine", "dash cam", "dashcam",
        "obd", "jump starter",
    ],
    "Patio, Lawn & Garden": [
        "garden", "patio", "lawn", "grill", "outdoor", "mower", "hose",
        "planter", "shovel", "trimmer",
    ],
    "Clothing, Shoes & Jewelry": [
        "shirt", "pants", "jacket", "dress", "shoes", "boot", "hat", "watch band",
        "earring", "necklace", "ring", "bracelet",
    ],
    "Arts, Crafts & Sewing": [
        "paint", "craft", "scissors", "yarn", "fabric", "sewing", "needle",
        "marker pen", "sketch",
    ],
    "Musical Instruments": [
        "guitar", "piano", "drum", "violin", "ukulele", "microphone", "midi",
    ],
    "Grocery & Gourmet Food": [
        "coffee bean", "tea bag", "snack", "chocolate", "candy", "spice",
    ],
}

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

    # ---- Path C: 穷举 Amazon BSR 目录树（绕开所有搜索引擎，最稳） ----

    @staticmethod
    def _tokens(text: str) -> set[str]:
        """归一化分词：小写 + 提取字母数字 token + 去掉无意义停用词。"""
        STOP = {"the", "a", "an", "of", "for", "and", "to", "with", "in", "on"}
        return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in STOP}

    @staticmethod
    def _singularize(token: str) -> str:
        """粗糙的复数→单数（仅用于匹配，不改写原文）：
            ies → y                   (puppies → puppy, ladies → lady)
            -sses/-xes/-zes → 去 es   (kisses → kiss, boxes → box)
            -shes/-ches → 去 es       (brushes → brush, watches → watch, smartwatches → smartwatch)
            其它 -s（非 -ss）→ 去 s   (mats → mat, noses → nose)

        注意 -ses 规则只匹配 -sses（双 s），避免把 'noses'(单数 nose) 错切成 'nos'。
        """
        if len(token) > 4 and token.endswith("ies"):
            return token[:-3] + "y"
        if len(token) > 4 and token.endswith(("sses", "xes", "zes", "shes", "ches")):
            return token[:-2]
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            return token[:-1]
        return token

    @staticmethod
    def _crunch(text: str) -> str:
        """去除所有空格/连字符/标点，仅保留字母数字（小写），用于跨越合成词差异比对。
        'smart watch' / 'Smartwatches' / 'smart-watch' / 'SmartWatch' → 全部归一为 'smartwatch'（单数化后）。
        """
        return re.sub(r"[^a-z0-9]+", "", (text or "").lower())

    def _score_category_name(self, keyword: str, name: str) -> int:
        """匹配评分（0-100）。优先级（高→低）：
            ① 合成词等价（crunch + 单数化后相同，如 'smart watch' ≡ 'Smartwatches'）→ 100
            ② 完全等价（token 集合相同）→ 100
            ③ 关键词 ⊊ 分类（分类比 kw 更具体）→ 100 - 15 × extra，下限 50
            ④ 分类 ⊊ 关键词（分类是父节点）→ 55
            ⑤ token 部分重叠 → 0-80（按 recall × precision）

        关键约束：
          'Yoga Mats' (100) > 'Yoga Mat Bags' (85) > 'Yoga' (55) > 'Mats' (40)
          'Smartwatches' ≡ 'smart watch' (100)  ← 解决合成词陷阱
        """
        if not name:
            return 0

        # ① 合成词等价检查（跨越分词差异）
        kw_crunch = self._singularize(self._crunch(keyword))
        name_crunch = self._singularize(self._crunch(name))
        if kw_crunch and name_crunch and kw_crunch == name_crunch:
            return 100

        kw_tokens = {self._singularize(t) for t in self._tokens(keyword)}
        name_tokens = {self._singularize(t) for t in self._tokens(name)}
        if not kw_tokens or not name_tokens:
            return 0

        # ② 完全等价（token 集合相同）
        if kw_tokens == name_tokens:
            return 100

        # ③ 关键词 ⊊ 分类
        if kw_tokens.issubset(name_tokens):
            extra = len(name_tokens) - len(kw_tokens)
            return max(50, 100 - 15 * extra)

        # ④ 分类 ⊊ 关键词
        if name_tokens.issubset(kw_tokens):
            return 55

        # ⑤ 部分重叠
        overlap = kw_tokens & name_tokens
        if not overlap:
            return 0
        recall = len(overlap) / len(kw_tokens)
        precision = len(overlap) / len(name_tokens)
        return int(80 * recall * (0.5 + 0.5 * precision))

    def _pick_candidate_depts(self, keyword: str, top_k: int = 3) -> list[tuple[str, str]]:
        """关键词 → 候选部门列表（按命中度倒序），最多 top_k 个。
        DEPT_KEYWORD_SEEDS 命中权重最高；未命中则取字典前几个作兜底。
        """
        kw_low = keyword.lower()
        scored: list[tuple[int, str, str]] = []
        for dept_name, slug in AMAZON_BSR_DEPTS:
            seeds = DEPT_KEYWORD_SEEDS.get(dept_name, [])
            hit = sum(1 for s in seeds if s in kw_low)
            if hit:
                scored.append((hit, dept_name, slug))
        scored.sort(key=lambda x: -x[0])
        if scored:
            picks = [(n, s) for _, n, s in scored[:top_k]]
        else:
            # 兜底：未命中任何种子，按通用程度尝试 Home & Kitchen / Electronics / Sports
            fallback_order = ["Home & Kitchen", "Electronics", "Sports & Outdoors"]
            slug_map = dict((n, s) for n, s in AMAZON_BSR_DEPTS)
            picks = [(n, slug_map[n]) for n in fallback_order if n in slug_map][:top_k]
        return picks

    async def _scrape_bsr_sidebar(self, url: str) -> list[dict]:
        """打开任意 BSR 页 URL，从侧边栏抽取当前层级的子分类。
        返回 [{name, href, node_id, slug}, ...]。
        """
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"[Discover/Tree]   加载失败 ({url[:80]}): {e}")
            return []
        await self.sleep("dept page")

        if await self.detect_block():
            print(f"[Discover/Tree]   被反爬 ({url[:80]})")
            return []

        try:
            cats = await self.page.evaluate(
                """
                () => {
                    const roots = document.querySelectorAll(
                        '#zg_browseRoot a, .zg_browseRoot a, '
                        + 'div[role="navigation"] a[href*="/zgbs/"], '
                        + 'div[role="group"] a[href*="/zgbs/"], '
                        + 'a[href*="/zgbs/"]'
                    );
                    const out = [];
                    const seen = new Set();
                    roots.forEach(a => {
                        const href = a.href || '';
                        const name = (a.textContent || '').trim();
                        if (!name || !href.includes('/zgbs/')) return;
                        if (seen.has(href)) return;
                        seen.add(href);
                        out.push({ name, href });
                    });
                    return out;
                }
                """
            )
        except Exception as e:
            print(f"[Discover/Tree]   侧边栏解析异常: {e}")
            return []

        parsed: list[dict] = []
        for c in cats or []:
            m = re.search(r"/zgbs/([a-z0-9\-]+)/(\d+)", c.get("href", ""))
            if not m:
                continue
            parsed.append({
                "name": c["name"],
                "href": c["href"],
                "node_id": m.group(2),
                "slug": m.group(1),
            })
        return parsed

    async def _discover_via_amazon_bsr_tree(self, keyword: str) -> dict | None:
        """Path C：BFS 递归下钻 Amazon BSR 目录树，找包含 keyword 的叶子分类。

        完全绕开搜索引擎：只走 /Best-Sellers/zgbs/<dept>/ 这条已验证不被拦截的路径。

        策略：
          1. 关键词 → 候选部门（启发式种子库 DEPT_KEYWORD_SEEDS）
          2. BFS 逐层下钻（最多 MAX_DEPTH=3 层，MAX_PAGES=25 页）
          3. 每层解析侧边栏子分类，评分匹配
          4. 有 partial match（score>0）→ 优先下钻该分支
          5. 全 0 命中时按 depth 自适应展开宽度：
             L0 全 0 → 展开全部子类（Amazon 侧边栏按字母排序，前 2 个完全可能漏到
                       'Wearable Technology' 这种 W 字母位置的关键节点）
             L1 全 0 → 展开前 3 个（已经在子部门里，预算更紧）
             L2+ 全 0 → 不展开（已经太深，零分继续下钻收益太低）

        时间预算：每页 ~3s → 总 worst-case 75s（25 页，会被早期 100 分命中终结）
        """
        MAX_DEPTH = 3
        MAX_PAGES = 25

        kw_clean = (keyword or "").strip()
        if not kw_clean:
            return None

        candidate_depts = self._pick_candidate_depts(kw_clean, top_k=3)
        print(f"[Discover/Tree] 关键词 {kw_clean!r} → 候选部门（按命中度）:")
        for n, s in candidate_depts:
            print(f"  - {n} (slug={s})")

        # BFS queue: (url, depth, breadcrumb_path)
        queue: list[tuple[str, int, str]] = []
        for dept_name, slug in candidate_depts:
            queue.append((
                f"https://www.amazon.com/Best-Sellers/zgbs/{slug}/",
                0,
                dept_name,
            ))

        visited: set[str] = set()
        best: dict | None = None
        best_score = 0
        pages_loaded = 0

        while queue and pages_loaded < MAX_PAGES and best_score < 100:
            url, depth, breadcrumb = queue.pop(0)
            if depth > MAX_DEPTH or url in visited:
                continue
            visited.add(url)

            cats = await self._scrape_bsr_sidebar(url)
            pages_loaded += 1
            # 去掉和已访问 URL 相同的条目（避免看到父级/自身链接）
            cats = [c for c in cats if c["href"] not in visited]

            # Amazon BSR 侧边栏在子目录下显示【相对父节点的简称】
            # 例如在 "Yoga" 下的 "Yoga Mats" 显示为 "Mats"。
            # 评分时同时尝试 raw name 和 "父节点名 + name" 的拼接，取最高分。
            parent_last = breadcrumb.split(">")[-1].strip()

            def _score(name: str) -> int:
                s1 = self._score_category_name(kw_clean, name)
                if parent_last:
                    qualified = f"{parent_last} {name}"
                    s2 = self._score_category_name(kw_clean, qualified)
                    return max(s1, s2)
                return s1

            print(f"[Discover/Tree] L{depth} [{breadcrumb}] → {len(cats)} 个子类目")
            for c in cats[:6]:
                s = _score(c["name"])
                tag = f" ★{s}" if s > 0 else ""
                print(f"[Discover/Tree]   - {c['name']}{tag}")
            if len(cats) > 6:
                print(f"[Discover/Tree]   ... 还有 {len(cats) - 6} 个")

            drill_candidates: list[tuple[int, dict, str]] = []
            for cat in cats:
                score = _score(cat["name"])
                # path 使用拼接后的合格名，方便后续 catalog 元信息可读
                display_name = cat["name"]
                if parent_last and self._score_category_name(kw_clean, f"{parent_last} {cat['name']}") > \
                        self._score_category_name(kw_clean, cat["name"]):
                    display_name = f"{parent_last} {cat['name']}"
                cat_path = f"{breadcrumb} > {display_name}"
                if score > best_score:
                    best_score = score
                    best = {
                        "name": display_name,
                        "href": cat["href"],
                        "node_id": cat["node_id"],
                        "slug": cat["slug"],
                        "path": cat_path,
                        "score": score,
                    }
                    print(f"[Discover/Tree]   ✨ 新最佳 score={score}: {display_name!r}")
                if score > 0:
                    drill_candidates.append((score, cat, cat_path))

            if best_score >= 100:
                break

            # 决定下一层下钻哪些分支
            #
            # 关键洞察：单靠 score 排序会被"低分干扰项"误导。
            # 例：keyword='smart watch' 在 L0 Electronics 会让 'Smart Home' 命中
            # 30 分（含 'smart'），但真正目标 'Wearable Technology' score=0，
            # 一旦 top-3 截断就漏掉。
            #
            # 因此 L0 一律执行"全广度兜底"：score>0 的优先入队，剩下的 score=0
            # 也全部入队（顶层部门一般 ≤25 项可控；MAX_PAGES=25 兜得住）。
            if depth < MAX_DEPTH:
                next_to_drill: list[tuple[dict, str]] = []
                drilled_hrefs: set[str] = set()

                if drill_candidates:
                    drill_candidates.sort(key=lambda x: -x[0])
                    for _, cat, path in drill_candidates[:3]:
                        next_to_drill.append((cat, path))
                        drilled_hrefs.add(cat["href"])

                # 自适应宽度兜底：
                #   L0：不管有没有 score>0 命中，剩余 zero-score 也全部入队
                #       （Amazon 侧边栏字母排序，关键节点可能在末尾如 'W' Wearable Technology）
                #   L1：仅当全 0 时展开前 3
                #   L2+：不补
                if depth == 0:
                    for cat in cats:
                        if cat["href"] not in drilled_hrefs:
                            next_to_drill.append((cat, f"{breadcrumb} > {cat['name']}"))
                            drilled_hrefs.add(cat["href"])
                elif depth == 1 and not drill_candidates:
                    for cat in cats[:3]:
                        if cat["href"] not in drilled_hrefs:
                            next_to_drill.append((cat, f"{breadcrumb} > {cat['name']}"))
                            drilled_hrefs.add(cat["href"])

                top_n = min(len(drill_candidates), 3)
                if len(next_to_drill) > top_n:
                    print(
                        f"[Discover/Tree]   ↪ 入队 {len(next_to_drill)} 个分支 "
                        f"(score>0 top: {top_n}, 宽度兜底: {len(next_to_drill) - top_n})"
                    )
                for cat, path in next_to_drill:
                    queue.append((cat["href"], depth + 1, path))

        if not best or best_score < 30:
            print(f"[Discover/Tree] ❌ {pages_loaded} 页扫描完毕，未找到分数 ≥ 30 的分类（最佳 {best_score}）")
            return None

        canonical_url = f"https://www.amazon.com/gp/bestsellers/{best['slug']}/{best['node_id']}"
        print(
            f"[Discover/Tree] ✅ 命中: '{best['name']}' (score={best['score']}) "
            f"path={best['path']!r}\n                  → {canonical_url}"
        )
        return {
            "bsr_url": canonical_url,
            "bsr_node_id": best["node_id"],
            "bsr_node_path": best["path"],
            "_se_engine": "AmazonBSRTree",
        }

    async def discover_bsr_node(self) -> dict:
        """从 search_keyword 出发，自动找出该品类在 Amazon 的 BSR 叶子节点。

        三路径接力（任一成功即返回 + 回填 self.cfg）：
          Path A: Amazon /s?k=xxx → 商品页 BSR 块（最准但 Action IP 经常被软拦截）
          Path C: Amazon BSR 目录树穷举（绕开所有反爬，自主可控，6-10s）
          Path B: 第三方搜索引擎 → 从 site:amazon.com 结果里 grep BSR URL
                  （3 大引擎已实测对 BSR URL 索引稀疏，仅作最后兜底）

        顺序设计：A 是最高质量但易拦，C 是稳定可控（首选 fallback），
        B 是"低概率高代价"的最后试探（保留是为了未来引擎索引改善时能受益）。
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
            print(f"[Discover] 🔄 切换 Path C (Amazon BSR 目录树穷举)")

        result_c = await self._discover_via_amazon_bsr_tree(keyword)
        if result_c and result_c.get("bsr_node_id"):
            self.cfg["bsr_url"] = result_c["bsr_url"]
            self.cfg["bsr_node_id"] = result_c["bsr_node_id"]
            self.cfg["bsr_node_path"] = result_c["bsr_node_path"]
            print(
                f"[Discover] ✅ Path C 命中 | node_id={result_c['bsr_node_id']} "
                f"path={result_c['bsr_node_path']!r}"
            )
            return {
                "bsr_url": self.cfg["bsr_url"],
                "bsr_node_id": self.cfg["bsr_node_id"],
                "bsr_node_path": self.cfg["bsr_node_path"],
            }

        print(f"\n[Discover] ⚠️ Path C 目录树未匹配")
        print(f"[Discover] 🔄 切换 Path B (第三方搜索引擎，最后兜底)")

        # Path B 加 path-score 校验：搜索引擎只能给一个 URL，命中的节点未必跟
        # keyword 真匹配（实测 brave 给 'smart watch' 返回的是 'Activity & Fitness Trackers'）。
        # 阈值同 Path A：50（兼容父节点 55）
        MIN_PATH_SCORE_B = 50
        result_b = self._discover_via_search_engine(keyword)
        if result_b and result_b.get("bsr_node_id"):
            bsr_path = await self._enrich_bsr_path_from_static_page(result_b["bsr_url"])
            final_path = bsr_path or result_b.get("bsr_node_path") or ""
            path_score = self._score_category_name(keyword, final_path)
            print(
                f"[Discover] Path B 拿到 node_id={result_b['bsr_node_id']} "
                f"path={final_path!r} → keyword 匹配度 {path_score}"
            )
            if path_score >= MIN_PATH_SCORE_B:
                self.cfg["bsr_url"] = result_b["bsr_url"]
                self.cfg["bsr_node_id"] = result_b["bsr_node_id"]
                self.cfg["bsr_node_path"] = final_path
                print(
                    f"[Discover] ✅ Path B 命中 | node_id={result_b['bsr_node_id']} "
                    f"path={final_path!r} url={result_b['bsr_url']}"
                )
                return {
                    "bsr_url": self.cfg["bsr_url"],
                    "bsr_node_id": self.cfg["bsr_node_id"],
                    "bsr_node_path": self.cfg["bsr_node_path"],
                }
            print(
                f"[Discover] ⚠️ Path B path-score {path_score} < {MIN_PATH_SCORE_B}，"
                f"放弃此节点（避免污染 catalog）"
            )

        raise RuntimeError(
            f"discover: 三路全部失败（A: {path_a_err}; C: 目录树未匹配; B: 无匹配 BSR URL）"
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

        # ASIN 候选会优先取前 3 个非赞助；每个商品页都要校验它的 BSR path
        # 与 keyword 是否匹配（搜索结果可能混入搭配/广告商品，BSR 在不相关类目下）。
        # 评分阈值 50：
        #   100 = 完全等价 / 85 = kw⊊name extra=1 / 55 = 父节点 / 50 是安全下限
        MIN_PATH_SCORE = 50
        last_err = None
        best_info = None  # 保留最高分候选，全部 < 阈值时作为相对最优兜底
        best_path_score = -1

        for asin in candidate_asins[:3]:
            try:
                info = await self._extract_bsr_from_product(asin)
                if not info or not info.get("bsr_node_id"):
                    print(f"[Discover] ASIN {asin}: 未提取到 BSR 节点")
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    continue

                path_score = self._score_category_name(keyword, info.get("bsr_node_path") or "")
                print(
                    f"[Discover] ASIN {asin}: path='{info['bsr_node_path']}' "
                    f"node_id={info['bsr_node_id']} → keyword 匹配度 {path_score}"
                )

                if path_score >= MIN_PATH_SCORE:
                    self.cfg["bsr_url"] = info["bsr_url"]
                    self.cfg["bsr_node_id"] = info["bsr_node_id"]
                    self.cfg["bsr_node_path"] = info["bsr_node_path"]
                    print(
                        f"[Discover] ✅ 节点确认 | node_id={info['bsr_node_id']} "
                        f"path='{info['bsr_node_path']}' url={info['bsr_url']}"
                    )
                    return info

                if path_score > best_path_score:
                    best_info, best_path_score = info, path_score
                print(f"[Discover] 分数 < {MIN_PATH_SCORE}，尝试下一个 ASIN")
            except Exception as e:
                last_err = e
                print(f"[Discover] ASIN {asin} 提取异常: {e}，尝试下一个")
            await asyncio.sleep(random.uniform(2.0, 4.0))

        raise RuntimeError(
            f"discover: Path A 候选 ASIN 的 BSR path 均与 keyword 不匹配 "
            f"(最高分 {best_path_score} < {MIN_PATH_SCORE}, "
            f"best path='{(best_info or {}).get('bsr_node_path', '')}', last_err={last_err})"
        )

    async def _extract_bsr_from_product(self, asin: str) -> dict | None:
        """打开商品详情页，**精确锁定**自身 BSR 信息块（不是页面其他位置的徽章/推荐分类链接），
        取叶子节点链接 + ID + 路径文本。

        Amazon 详情页 BSR 块固定格式：
            Best Sellers Rank: #6,123 in Electronics (See Top 100 in Electronics)
                               #45 in Smartwatches (See Top 100 in Smartwatches)
        策略：
          1. XPath 找含 "Best Sellers Rank" 文字的元素，向上找祖先容器
          2. 在该容器内按 DOM 顺序收集所有 /gp/bestsellers/ 链接
          3. Amazon 按"大类→叶子"排列，**最后一个**就是叶子
          4. 同时校验 host_text 含 "in <Category>" 句式（排除徽章型链接）

        ⚠️ 不再用"全页扫 + URL 深度排序"——所有 /gp/bestsellers/<dept>/<id> URL 段数一样，
            排序无效；商品页"相关推荐分类"徽章的 URL 会被误选（如 'Best Seller' badge）。
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
            r"""
            () => {
                // ① XPath 找含 "Best Sellers Rank" 文本节点的元素，向上找 6 级祖先作为锁定容器
                const xr = document.evaluate(
                    "//*[contains(translate(text(), 'BSR', 'bsr'), 'best sellers rank')]",
                    document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null
                );
                const containers = new Set();
                for (let i = 0; i < xr.snapshotLength; i++) {
                    let p = xr.snapshotItem(i);
                    for (let k = 0; k < 6 && p; k++) {
                        containers.add(p);
                        p = p.parentElement;
                    }
                }
                // 兜底：常见的 BSR 块容器
                ['#detailBullets_feature_div',
                 '#productDetails_detailBullets_sections1',
                 '#productDetails_db_sections',
                 '#prodDetails'].forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => containers.add(el));
                });
                if (containers.size === 0) return { links: [], hint: 'no_container' };

                // ② 在容器内按 DOM 顺序拿 /gp/bestsellers/ 链接
                const seen = new Set();
                const links = [];
                containers.forEach(c => {
                    const anchors = c.querySelectorAll(
                        "a[href*='/gp/bestsellers/'], a[href*='/Best-Sellers-']"
                    );
                    anchors.forEach(a => {
                        const href = a.getAttribute('href') || '';
                        if (!href || seen.has(href)) return;
                        seen.add(href);
                        const host = a.closest('li, tr, span, p, div');
                        const hostText = host ? (host.textContent || '').trim() : '';
                        // ③ 校验 host_text 含 "#N in X" 句式，排除徽章/推荐型
                        const isBsrEntry = /#[\d,]+\s+in\s+/i.test(hostText);
                        links.push({
                            href,
                            text: (a.textContent || '').trim(),
                            host_text: hostText.slice(0, 300),
                            is_bsr_entry: isBsrEntry,
                        });
                    });
                });
                return { links, hint: 'ok' };
            }
            """
        )

        candidates = (bsr_links or {}).get("links", [])
        hint = (bsr_links or {}).get("hint", "")
        if not candidates:
            print(f"  [extract] 未找到 BSR 链接 (hint={hint})")
            return None

        # 优先选"#X in Y"格式的真 BSR 入口；都没有再退到所有候选
        primary = [c for c in candidates if c.get("is_bsr_entry")]
        if not primary:
            print(f"  [extract] ⚠️ {len(candidates)} 个 BSR 链接均不含 '#N in X' 格式，可能在徽章区")
            primary = candidates

        # Amazon BSR 块按"大类→叶子"排列：取最后一个 = 叶子
        # 同一个链接（如 "See Top 100 in X"）可能重复，"See Top 100" 也是入口，所以这里仍取最后一个非空
        best = primary[-1]
        for cand in reversed(primary):
            if cand.get("href"):
                best = cand
                break

        href = best["href"]
        if href.startswith("/"):
            href = self.cfg["base_url"] + href

        m = re.search(r"/bestsellers/[^/?#]+/(\d+)", href)
        node_id = m.group(1) if m else ""
        if not node_id:
            print(f"  [extract] href 中未解析出 node_id: {href}")
            return None

        host_text = best.get("host_text", "")
        # 从 host_text 里抽 "#N in <Category Name>" 中的 <Category Name>（取最后一段）
        # 因为 host_text 可能跨多行包含 "#X in DEPT ... #Y in LEAF"
        path_matches = re.findall(r"#[\d,]+\s+in\s+([^(\n#]+?)(?:\s*\(|$|#)", host_text)
        if path_matches:
            bsr_path = path_matches[-1].strip()
        else:
            bsr_path = (best.get("text", "") or "").strip()
        bsr_path = re.sub(r"\s+", " ", bsr_path)[:200]

        print(f"  [extract] 候选 {len(candidates)} 个 (含 '#N in X' 格式 {len(primary)} 个) → 选最后: '{bsr_path}'")
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
