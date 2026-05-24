# Zhiwei Scraper · 知微Agent 数据采集模块

Amazon BSR 榜单 + 产品评论自动采集，产出含 `weight` 和 `date` 字段的结构化 Markdown，供 [Dify Chatflow](https://dify.ai) 知识库注入。

## 架构位置

```
zhiwei-scraper (本仓库)
    ↓ 产出 Markdown 快照
Dify 知识库 L2 注入
    ↓ N2a 知识库检索
知微Agent 主 Chatflow (P&E 分析)
```

## 运行

```bash
# 安装
pip install playwright greenlet
python -m playwright install chromium

# 采集
cd scraper
python amazon_bsr_scraper.py

# 输出
output/Pet_Water_Fountain_<timestamp>.md   # 上传 Dify 知识库
output/Pet_Water_Fountain_<timestamp>.json # 原始备份
```

## 定时采集

GitHub Actions 每日 UTC 18:00（北京时间凌晨 2 点）自动运行，输出提交到 `scraper/output/`。

## 换品类

修改 `scraper/amazon_bsr_scraper.py` 顶部 `CONFIG` 字典：

```python
CONFIG = {
    "category": "New Category",
    "search_keyword": "new keyword",
    "bsr_url": "https://www.amazon.com/gp/bestsellers/...",
    "bsr_node_id": "1234567890",
    ...
}
```

详见 `scraper/数据采集模块_技术说明.md`。
