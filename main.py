"""
Instagram SDR Bot — ManyChat Webhook Server
Powered by Claude (Anthropic)

Local:    python main.py
Railway:  automático via Procfile
"""

import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        *(
            [logging.FileHandler("logs/bot.log", encoding="utf-8")]
            if os.path.isdir("logs")
            else []
        ),
    ],
)

# Importa o app FastAPI — usado pelo Procfile via uvicorn diretamente
from src.webhook import app  # noqa: F401, E402

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("src.webhook:app", host="0.0.0.0", port=port, reload=False)
