"""将用户地区表述统一为模拟商品的地区标签。"""


def normalize_region(value: str) -> str:
    tag = value.strip().upper()
    aliases = {
        "中国": "CN", "中国大陆": "CN", "大陆": "CN", "国行": "CN",
        "CHINA": "CN", "MAINLAND CHINA": "CN",
        "不限": "ANY", "不限制": "ANY", "地区不限": "ANY", "*": "ANY",
    }
    return aliases.get(tag, tag)
