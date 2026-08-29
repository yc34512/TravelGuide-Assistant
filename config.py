"""全局配置。所有频率/数量限制默认取保守值，可通过 .env 覆盖。"""
from pathlib import Path

from dotenv import load_dotenv
import os

load_dotenv()

PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
REPORT_DIR = DATA_DIR / "reports"
DEBUG_DIR = DATA_DIR / "debug"
BROWSER_PROFILE_DIR = PROJECT_ROOT / "browser_profile"

for _d in (DATA_DIR, RAW_DIR, REPORT_DIR, DEBUG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# —— 频控（合规第一道防线：模拟正常浏览节奏，默认保守，调小会增加风控风险）——
REQUEST_DELAY_MIN = float(os.getenv("REQUEST_DELAY_MIN", "2.5"))
REQUEST_DELAY_MAX = float(os.getenv("REQUEST_DELAY_MAX", "5.0"))
MAX_VIDEOS_PER_RUN = int(os.getenv("MAX_VIDEOS_PER_RUN", "10"))
MAX_COMMENTS_PER_VIDEO = int(os.getenv("MAX_COMMENTS_PER_VIDEO", "50"))
MAX_SEARCH_SCROLLS = int(os.getenv("MAX_SEARCH_SCROLLS", "8"))
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

# —— LLM（OpenAI 兼容接口，任选 DeepSeek / 智谱 / 通义 等）——
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
