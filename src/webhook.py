"""
Webhook server para ManyChat — Eduardo Prado (@pradoclima).
ManyChat chama POST /webhook com dados do seguidor → retorna resposta gerada pelo Claude.
"""

import asyncio
import logging
import re
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from .sales_agent import SalesAgent
from .conversation_manager import ConversationManager
from .reactivation import ReactivationService
from .config import Config
from .products import PRODUCTS

logger = logging.getLogger(__name__)

app = FastAPI(title="Instagram SDR Bot — Eduardo Prado (@pradoclima)", version="1.0.0")

agent: SalesAgent = None
conv_manager: ConversationManager = None
reactivation_svc: ReactivationService = None


@app.on_event("startup")
async def startup():
    global agent, conv_manager, reactivation_svc
    if not Config.ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY não configurada! Adicione em Railway → Variables.")
    else:
        agent = SalesAgent(Config.ANTHROPIC_API_KEY)
        logger.info("Sales Agent (Eduardo Prado) inicializado.")
    conv_manager = ConversationManager()
    logger.info("ConversationManager inicializado.")

    if agent and conv_manager:
        reactivation_svc = ReactivationService(
            agent=agent,
            conv_manager=conv_manager,
            manychat_key=Config.MANYCHAT_API_KEY,
            instagram_token=Config.INSTAGRAM_ACCESS_TOKEN,
            instagram_account_id=Config.INSTAGRAM_ACCOUNT_ID,
        )
        asyncio.create_task(_reactivation_loop())
        asyncio.create_task(_purchase_followup_loop())
        logger.info("Schedulers de reativação e follow-up de compra iniciados.")


async def _reactivation_loop():
    """Roda a cada hora e reativa leads que pararam de responder."""
    while True:
        await asyncio.sleep(3600)
        if reactivation_svc:
            try:
                result = reactivation_svc.run_reactivation(hours_threshold=24)
                logger.info(f"[REATIVAÇÃO AUTO] {result}")
            except Exception as e:
                logger.error(f"[REATIVAÇÃO AUTO] Erro: {e}")


async def _purchase_followup_loop():
    """Roda a cada 2 horas e envia follow-up para quem recebeu link mas não comprou."""
    while True:
        await asyncio.sleep(7200)
        if agent and conv_manager and reactivation_svc:
            try:
                pending = conv_manager.get_purchase_followup_pending(hours_min=2, hours_max=72)
                for lead in pending:
                    user_id = lead["user_id"]
                    user_name = lead["user_name"]
                    product_id = lead.get("product_recommended", "")
                    attempt = lead.get("purchase_followup_count", 0) + 1
                    hours_since = lead.get("hours_since_link", 24)
                    product = PRODUCTS.get(product_id, {})
                    message = agent.generate_purchase_followup(
                        user_name=user_name,
                        product_name=product.get("name", "o produto"),
                        product_link=product.get("link", ""),
                        hours_since_link=hours_since,
                        attempt=attempt,
                    )
                    if reactivation_svc._send_manychat_dm(user_id, message):
                        conv_manager.add_message(user_id, "assistant", message)
                        conv_manager.mark_purchase_followup_sent(user_id)
                        if attempt >= 3:
                            conv_manager.mark_cold(user_id)
                        logger.info(f"[FOLLOW-UP COMPRA AUTO] Tentativa {attempt} → {user_name}")
            except Exception as e:
                logger.error(f"[FOLLOW-UP COMPRA AUTO] Erro: {e}")


# ─── Schema ─────────────────────────────────────────────────────────────────

class ManyChatPayload(BaseModel):
    subscriber_id: str
    first_name: Optional[str] = ""
    last_message: Optional[str] = ""
    attachment_type: Optional[str] = ""
    attachment_url: Optional[str] = ""


# ─── DMs ────────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def manychat_webhook(payload: ManyChatPayload):
    if not agent:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY não configurada.")

    def _clean(v: str) -> str:
        return re.sub(r"\{\{[^}]*\}\}", "", v or "").strip()

    user_id = _clean(payload.subscriber_id) or payload.subscriber_id
    user_name = _clean(payload.first_name) or "amigo"
    message = _clean(payload.last_message) or ""
    attachment_type = _clean(payload.attachment_type or "").lower()
    attachment_url = _clean(payload.attachment_url or "")

    if attachment_type:
        history_message = message or f"[enviou {attachment_type}]"
    else:
        history_message = message or "oi"

    logger.info(f"[DM] {user_name} ({user_id}) [{attachment_type or 'text'}]: '{(message or attachment_url)[:80]}'")

    try:
        conv_manager.get_or_create(user_id, user_name)
        history = conv_manager.get_history(user_id)
        conv_manager.add_message(user_id, "user", history_message)

        current_stage = agent.classify_message_stage(history_message, history)
        conv_manager.update_stage(user_id, current_stage)

        response_text = agent.generate_dm_response(
            user_name=user_name,
            user_message=message or history_message,
            conversation_history=history,
            stage=current_stage,
            attachment_type=attachment_type,
            attachment_url=attachment_url,
        )

        conv_manager.add_message(user_id, "assistant", response_text)

        link_sent = _detect_link_sent(response_text)
        if link_sent:
            conv_manager.mark_link_sent(user_id, link_sent)

        logger.info(f"[DM] Resposta ({current_stage}): '{response_text[:80]}'")
        return _build_response(response_text)

    except Exception as e:
        logger.error(f"[DM] Erro ao processar mensagem de {user_name} ({user_id}): {e}", exc_info=True)
        fallback = "Oi! Recebi sua mensagem. Me manda o que você precisa que eu te ajudo."
        return _build_response(fallback)


# ─── Comentários ────────────────────────────────────────────────────────────

@app.post("/webhook/comment")
async def manychat_comment_webhook(payload: ManyChatPayload):
    if not agent:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY não configurada.")

    user_name = payload.first_name or "amigo"
    comment = (payload.last_message or "").strip()

    logger.info(f"[COMENTÁRIO] @{user_name}: '{comment[:60]}'")

    reply = agent.generate_comment_reply(user_name=user_name, comment_text=comment or "...")

    if not reply.startswith(f"@{user_name}"):
        reply = f"@{user_name} {reply}"

    return JSONResponse({
        "version": "v2",
        "content": {
            "messages": [{"type": "text", "text": reply}],
            "actions": [],
            "quick_replies": [],
        }
    })


# ─── Reativação ─────────────────────────────────────────────────────────────

@app.post("/reactivation/run")
def run_reactivation(hours: int = 24):
    if not reactivation_svc:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")
    result = reactivation_svc.run_reactivation(hours_threshold=hours)
    return result


@app.get("/reactivation/pending")
def reactivation_pending(hours: int = 24):
    if not conv_manager:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")
    leads = conv_manager.get_cold_leads(hours=hours)
    return {
        "total": len(leads),
        "leads": [
            {
                "user_id": l["user_id"],
                "user_name": l["user_name"],
                "stage": l.get("stage"),
                "hours_silent": l.get("hours_since_last_user_message"),
                "reactivation_count": l.get("reactivation_count", 0),
            }
            for l in leads
        ],
    }


# ─── Utilitários ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "bot": "Instagram SDR Bot — Eduardo Prado (@pradoclima)"}


# ─── Webhook Greenn — confirmação de compra ──────────────────────────────────

class GreennPurchasePayload(BaseModel):
    email: Optional[str] = ""
    subscriber_id: Optional[str] = ""
    product_id: Optional[str] = ""
    transaction_id: Optional[str] = ""
    status: Optional[str] = "approved"


@app.post("/purchase/confirmed")
async def greenn_purchase_webhook(payload: GreennPurchasePayload):
    """
    Greenn chama este endpoint quando uma compra é confirmada.
    Configure em: Greenn → Produto → Webhooks → URL de notificação
    """
    if payload.status not in ("approved", "paid", "complete", "completed"):
        return {"message": "status ignorado", "status": payload.status}

    subscriber_id = payload.subscriber_id or ""
    email = payload.email or ""

    # Tenta encontrar o subscriber pelo ID direto ou pelo email
    found_id = None
    if subscriber_id and subscriber_id in conv_manager.conversations:
        found_id = subscriber_id
    elif email:
        found_id = conv_manager.find_by_email(email)

    if found_id:
        conv_manager.mark_purchased(found_id, product_id=payload.product_id, buyer_email=email)
        logger.info(f"[COMPRA] Venda confirmada para subscriber {found_id} (email: {email}, produto: {payload.product_id})")
        return {"message": "compra registrada", "subscriber_id": found_id}

    logger.warning(f"[COMPRA] Não encontrou subscriber para email={email} id={subscriber_id}")
    return {"message": "subscriber não encontrado — compra não vinculada", "email": email}


# ─── Follow-up de não-compradores ────────────────────────────────────────────

@app.post("/followup/non-buyers/run")
def run_non_buyer_followup():
    """
    Envia follow-up para quem recebeu o link mas não comprou.
    Rode manualmente ou configure um cron no Railway.
    """
    if not reactivation_svc or not agent:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")

    pending = conv_manager.get_purchase_followup_pending(hours_min=2, hours_max=72)
    results = {"total": len(pending), "sent": 0, "failed": 0}

    for lead in pending:
        user_id = lead["user_id"]
        user_name = lead["user_name"]
        product_id = lead.get("product_recommended", "")
        attempt = lead.get("purchase_followup_count", 0) + 1
        hours_since = lead.get("hours_since_link", 24)

        product = PRODUCTS.get(product_id, {})
        product_name = product.get("name", "o produto")
        product_link = product.get("link", "")

        try:
            message = agent.generate_purchase_followup(
                user_name=user_name,
                product_name=product_name,
                product_link=product_link,
                hours_since_link=hours_since,
                attempt=attempt,
            )
            success = reactivation_svc._send_manychat_dm(user_id, message)
            if success:
                conv_manager.add_message(user_id, "assistant", message)
                conv_manager.mark_purchase_followup_sent(user_id)
                if attempt >= 3:
                    conv_manager.mark_cold(user_id)
                results["sent"] += 1
                logger.info(f"[FOLLOW-UP COMPRA] Tentativa {attempt} → {user_name} ({hours_since}h desde o link)")
            else:
                results["failed"] += 1
        except Exception as e:
            logger.error(f"[FOLLOW-UP COMPRA] Erro para {user_name}: {e}")
            results["failed"] += 1

    return results


@app.get("/followup/non-buyers/pending")
def non_buyer_followup_pending():
    """Lista quem está pendente de follow-up de compra."""
    if not conv_manager:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")
    pending = conv_manager.get_purchase_followup_pending(hours_min=2, hours_max=72)
    return {
        "total": len(pending),
        "leads": [
            {
                "user_id": l["user_id"],
                "user_name": l["user_name"],
                "product": l.get("product_recommended"),
                "hours_since_link": l.get("hours_since_link"),
                "followup_count": l.get("purchase_followup_count", 0),
            }
            for l in pending
        ],
    }


@app.get("/debug/claude")
def debug_claude():
    """Testa conexão com Claude API e retorna erro detalhado se falhar."""
    if not agent:
        return {"status": "error", "detail": "ANTHROPIC_API_KEY não configurada — agent é None"}
    try:
        result = agent.client.messages.create(
            model=agent.model,
            max_tokens=10,
            messages=[{"role": "user", "content": "responda só: ok"}],
        )
        return {"status": "ok", "response": result.content[0].text.strip()}
    except Exception as e:
        return {"status": "error", "type": type(e).__name__, "detail": str(e)}


@app.get("/stats")
def stats():
    if not conv_manager:
        raise HTTPException(status_code=503, detail="Bot não inicializado")
    return conv_manager.get_stats()


@app.delete("/conversation/{subscriber_id}")
def reset_conversation(subscriber_id: str):
    if subscriber_id in conv_manager.conversations:
        del conv_manager.conversations[subscriber_id]
        conv_manager._save()
        return {"message": f"Conversa {subscriber_id} resetada."}
    raise HTTPException(status_code=404, detail="Conversa não encontrada")


def _build_response(text: str) -> JSONResponse:
    safe = re.sub(r"\{\{[^}]*\}\}", "", text).strip()
    parts = [p.strip() for p in safe.split("\n\n") if p.strip()]
    msgs = [{"type": "text", "text": p} for p in parts] or [{"type": "text", "text": safe}]
    return JSONResponse({
        "version": "v2",
        "content": {"type": "instagram", "messages": msgs, "actions": [], "quick_replies": []},
    })


def _detect_link_sent(text: str) -> Optional[str]:
    links = {
        "o_mapa_convencer": "payfast.greenn.com.br/66110",
        "a_arte_de_precificar": "payfast.greenn.com.br/65471",
    }
    for product_id, fragment in links.items():
        if fragment in text:
            return product_id
    return None
