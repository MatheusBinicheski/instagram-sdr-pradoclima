import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    MANYCHAT_API_KEY: str = os.getenv("MANYCHAT_API_KEY", "")
    INSTAGRAM_ACCESS_TOKEN: str = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
    INSTAGRAM_ACCOUNT_ID: str = os.getenv("INSTAGRAM_ACCOUNT_ID", "")
    PORT: int = int(os.getenv("PORT", 8000))

    # Agenda do Guilherme (closer de seguros) — Apps Script relay
    GOOGLE_SCRIPT_URL: str = os.getenv("GOOGLE_SCRIPT_URL", "")
    GOOGLE_SCRIPT_SECRET: str = os.getenv("GOOGLE_SCRIPT_SECRET", "")
    # Fallback alternativo — Service Account JSON em string única
    GOOGLE_SERVICE_ACCOUNT_JSON: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    GUILHERME_CALENDAR_ID: str = os.getenv("GUILHERME_CALENDAR_ID", "grsouza93ip@gmail.com")
