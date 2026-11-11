"""集中管理项目路径与本机配置，避免依赖启动命令的当前目录。"""
import json
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
ENV_FILE = CONFIG_DIR / ".env"
CATALOG_FILE = CONFIG_DIR / "catalog.json"
SCENARIO_DIR = CONFIG_DIR / "scenarios"
DEFAULTS_FILE = CONFIG_DIR / "defaults.json"


def requirement_defaults(now):
    settings = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    numeric = ("storage_gb", "delivery_days", "followup_days", "check_interval_seconds")
    if (set(settings) != set(numeric) | {"region", "condition", "notification_channel"}
            or any(type(settings.get(k)) is not int or settings[k] <= 0 for k in numeric)
            or not isinstance(settings.get("region"), str) or not settings["region"].strip()
            or settings.get("condition") not in {"new", "used"}
            or settings.get("notification_channel") != "page"):
        raise ValueError("INVALID_DEFAULT_CONFIGURATION: 请检查 config/defaults.json")
    return {"region": settings["region"], "condition": settings["condition"],
            "storage_gb": settings["storage_gb"],
            "desired_delivery_at": now + settings["delivery_days"] * 86400,
            "followup_until": now + settings["followup_days"] * 86400,
            "check_interval_seconds": settings["check_interval_seconds"],
            "notification_channel": settings["notification_channel"], "followup_authorized": False}


def load_environment():
    # 进程环境变量优先，真实密钥只从忽略的本机 .env 读取。
    load_dotenv(ENV_FILE)


def project_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path
