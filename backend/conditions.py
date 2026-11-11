"""成色的中英文表述使用同一商品标签。未知值保持原义。"""


def normalize_condition(value: str) -> str:
    tag = value.strip().lower()
    return {"全新": "new", "全新机": "new", "二手": "used", "二手机": "used"}.get(tag, tag)
