"""
Dify Knowledge Base Uploader
将采集产出的 Markdown 快照推送到 Dify 知识库.
"""

import os
import re
import requests
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================
DIFY_API_KEY = os.getenv("DIFY_DATASET_API_KEY", "dataset-U5F5fDvJGEXV231PP6uYpk07")
DIFY_BASE_URL = "https://api.dify.ai/v1"
DATASET_ID = "748a94e0-901e-445f-9d95-e1c9a9447148"
OUTPUT_DIR = Path(__file__).parent / "output"


def find_latest_md() -> Path | None:
    """找到最新的采集 Markdown 文件."""
    files = sorted(OUTPUT_DIR.glob("Pet_Water_Fountain_*.md"), reverse=True)
    return files[0] if files else None


def create_document(text: str, title: str) -> dict:
    """通过文本创建知识库文档."""
    url = f"{DIFY_BASE_URL}/datasets/{DATASET_ID}/document/create-by-text"
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "name": title,
        "text": text,
        "indexing_technique": "high_quality",
        "process_rule": {
            "mode": "automatic",
            "rules": {
                "pre_processing_rules": [
                    {"id": "remove_extra_spaces", "enabled": True},
                    {"id": "remove_urls_emails", "enabled": False},
                ],
                "segmentation": {
                    "separator": "\n\n",
                    "max_tokens": 500,
                },
            },
        },
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    return resp.json()


def update_document(document_id: str, text: str, title: str) -> dict:
    """更新已有文档内容."""
    url = f"{DIFY_BASE_URL}/datasets/{DATASET_ID}/documents/{document_id}/update-by-text"
    headers = {
        "Authorization": f"Bearer {DIFY_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "name": title,
        "text": text,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    return resp.json()


def list_documents(keyword: str = "") -> list:
    """列出知识库中的文档."""
    url = f"{DIFY_BASE_URL}/datasets/{DATASET_ID}/documents"
    params = {"page": 1, "limit": 50}
    if keyword:
        params["keyword"] = keyword
    headers = {"Authorization": f"Bearer {DIFY_API_KEY}"}
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    data = resp.json()
    return data.get("data", [])


def upload():
    """主流程: 找到最新 MD → 推送到 Dify."""
    md_path = find_latest_md()
    if not md_path:
        print("[ERROR] No markdown file found")
        return

    title = md_path.stem  # e.g. "Pet_Water_Fountain_20260524_122716"
    text = md_path.read_text(encoding="utf-8")

    # 去掉前端元数据行 (采集时间等)，保留表格内容
    # Dify 的分块策略对 Markdown 表格友好,保留原始格式
    print(f"[Upload] {title} ({len(text)} chars)")

    # 检查是否已存在同名片文档
    existing = list_documents(keyword=title)
    if existing:
        # 更新已有文档
        doc_id = existing[0]["id"]
        result = update_document(doc_id, text, title)
        print(f"[Update] doc={doc_id} → {result.get('document', {}).get('display_status', result)}")
    else:
        # 创建新文档
        result = create_document(text, title)
        doc = result.get("document", {})
        print(f"[Create] doc={doc.get('id')} batch={doc.get('batch')} → {doc.get('display_status', result)}")

    return result


if __name__ == "__main__":
    upload()
