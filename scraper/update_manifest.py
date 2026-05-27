"""
update_manifest.py · 更新 scraper/output/manifest.json

扫描 output/ 目录下所有 Markdown 快照，按品类分组，取每个品类最新的一份，
输出 manifest.json，供 Dify N2D 节点快速查找/判断新鲜度。

调用方式：
    python scraper/update_manifest.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "output"
CATALOG_PATH = Path(__file__).parent / "category_catalog.json"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
FRESHNESS_DAYS = 7

FILE_PATTERN = re.compile(r"^(?P<key>.+?)_(?P<ts>\d{8}_\d{6})\.md$")


def load_catalog() -> dict:
    if not CATALOG_PATH.exists():
        return {}
    try:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8")).get("categories", {})
    except Exception as e:
        print(f"[Manifest] catalog 加载失败: {e}", file=sys.stderr)
        return {}


def key_to_category(file_key: str, catalog: dict) -> str | None:
    """文件名前缀（如 Pet_Water_Fountain）→ catalog 中的规范名（Pet Water Fountain）。"""
    norm = file_key.replace("_", " ").strip().lower()
    for cat_key, info in catalog.items():
        if cat_key.lower() == norm:
            return cat_key
        for alias in info.get("aliases", []) or []:
            if str(alias).lower() == norm:
                return cat_key
    return None


def parse_timestamp(ts: str) -> str:
    """20260526_200537 → 2026-05-26T20:05:37Z（假设原时间戳为本地时间，标记为 UTC 仅用于比较）。"""
    try:
        dt = datetime.strptime(ts, "%Y%m%d_%H%M%S")
        return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def count_asins_in_md(filepath: Path) -> int:
    """从 Markdown 中粗略提取 ASIN 数（出现的 B0xxx 唯一计数）。"""
    try:
        text = filepath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return 0
    asins = set(re.findall(r"\bB0[A-Z0-9]{8}\b", text))
    return len(asins)


def build_manifest() -> dict:
    if not OUTPUT_DIR.exists():
        print(f"[Manifest] output/ 目录不存在: {OUTPUT_DIR}", file=sys.stderr)
        return {"categories": {}, "updated_at": "", "freshness_days": FRESHNESS_DAYS}

    catalog = load_catalog()
    grouped: dict[str, list[tuple[Path, str]]] = {}

    for md_path in OUTPUT_DIR.glob("*.md"):
        m = FILE_PATTERN.match(md_path.name)
        if not m:
            continue
        file_key = m.group("key")
        ts = m.group("ts")
        canonical = key_to_category(file_key, catalog) or file_key.replace("_", " ")
        grouped.setdefault(canonical, []).append((md_path, ts))

    categories: dict[str, dict] = {}
    for cat, files in grouped.items():
        files.sort(key=lambda x: x[1], reverse=True)
        latest_path, latest_ts = files[0]
        info = catalog.get(cat, {})
        categories[cat] = {
            "aliases": info.get("aliases", []),
            "latest_file": latest_path.name,
            "scraped_at": parse_timestamp(latest_ts),
            "asin_count": count_asins_in_md(latest_path),
            "market": info.get("market", "amazon.com"),
            "bsr_node_id": info.get("bsr_node_id", ""),
            "status": "ok",
            "history_files": [p.name for p, _ in files[:5]]
        }

    return {
        "categories": categories,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "freshness_days": FRESHNESS_DAYS
    }


def main() -> int:
    manifest = build_manifest()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    cats = manifest.get("categories", {})
    print(f"[Manifest] 已写入 {MANIFEST_PATH}")
    print(f"[Manifest] 收录 {len(cats)} 个品类:")
    for cat, info in cats.items():
        print(f"  - {cat}  ({info.get('latest_file','-')}, ASIN={info.get('asin_count',0)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
