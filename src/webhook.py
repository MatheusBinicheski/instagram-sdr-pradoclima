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
from .sales_agent import STAGE_FALLBACKS

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

# Debounce: acumula mensagens rápidas e processa como bloco único
DEBOUNCE_DELAY = 4  # segundos de silêncio antes de processar
_debounce_tasks: dict[str, asyncio.Task] = {}
_message_buffers: dict[str, list[dict]] = {}  # user_id → lista de payloads


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


async def _process_debounced(user_id: str):
    """
    Processa todas as mensagens acumuladas no buffer após DEBOUNCE_DELAY segundos.
    Chamado como task — cancela e reagenda a cada nova mensagem do mesmo usuário.
    """
    await asyncio.sleep(DEBOUNCE_DELAY)

    buffer = _message_buffers.pop(user_id, [])
    if not buffer:
        return

    # Usa o payload mais recente para contexto; combina textos
    last = buffer[-1]
    user_name = last["user_name"]
    attachment_type = last["attachment_type"]
    attachment_url = last["attachment_url"]

    texts = [b["message"] for b in buffer if b["message"]]
    combined_message = " / ".join(texts) if texts else last["history_message"]
    history_message = combined_message or last["history_message"]

    logger.info(f"[DEBOUNCE] Processando {len(buffer)} msg(s) de {user_name} ({user_id}): '{combined_message[:80]}'")

    try:
        conv_manager.get_or_create(user_id, user_name)
        history = conv_manager.get_history(user_id)
        conv_manager.add_message(user_id, "user", history_message)

        # Stage + keyword detection
        triggered_product = last["triggered_product"]
        product_from_keyword = last["product_from_keyword"]

        if product_from_keyword:
            keyword_label = "mcc20" if product_from_keyword == "o_mapa_convencer" else "arte20"
            conv_manager.mark_keyword_triggered(user_id, keyword_label, product_from_keyword)
        if triggered_product:
            conv_manager.mark_link_sent(user_id, triggered_product)

        current_stage = conv_manager.get_stage(user_id)
        if triggered_product:
            current_stage = "fechando"
        elif current_stage == "conexao" and len(history) >= 2:
            current_stage = "qualificando"
        elif current_stage == "qualificando" and len(history) >= 6:
            current_stage = "apresentando"
        conv_manager.update_stage(user_id, current_stage)

        extra_context = ""
        if triggered_product:
            p = PRODUCTS[triggered_product]
            extra_context = (
                f"AÇÃO OBRIGATÓRIA: O lead digitou uma palavra-chave de produto. "
                f"Mande AGORA o link do '{p['name']}': {p['link']} "
                f"Seja direto, mande o link já na primeira frase. "
                f"Depois faça UMA pergunta para continuar a conversa."
            )

        try:
            response_text = await asyncio.wait_for(
                agent.generate_dm_response_async(
                    user_name=user_name,
                    user_message=combined_message or history_message,
                    conversation_history=history,
                    stage=current_stage,
                    attachment_type=attachment_type,
                    attachment_url=attachment_url,
                    extra_context=extra_context,
                ),
                timeout=4.5,
            )
        except asyncio.TimeoutError:
            response_text = STAGE_FALLBACKS.get(current_stage, STAGE_FALLBACKS["conexao"])
            logger.warning(f"[DEBOUNCE] Timeout Claude para {user_name} — fallback '{current_stage}'")
        except Exception as claude_err:
            response_text = STAGE_FALLBACKS.get(current_stage, STAGE_FALLBACKS["conexao"])
            logger.error(f"[DEBOUNCE] Erro Claude para {user_name}: {claude_err}")

        conv_manager.add_message(user_id, "assistant", response_text)
        link_sent = _detect_link_sent(response_text)
        if link_sent:
            conv_manager.mark_link_sent(user_id, link_sent)

        # Envia via API (não via webhook response — o webhook já retornou vazio)
        safe = re.sub(r"\{\{[^}]*\}\}", "", response_text).strip()
        if user_id:
            safe = _inject_tracking(safe, user_id)

        import httpx as _httpx
        parts = [p.strip() for p in safe.split("\n\n") if p.strip()] or [safe]
        full_text = "\n\n".join(parts)
        payload = {
            "subscriber_id": user_id,
            "data": {"version": "v2", "content": {
                "type": "instagram",
                "messages": [{"type": "text", "text": full_text}],
                "actions": [], "quick_replies": [],
            }},
        }
        async with _httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.manychat.com/fb/sending/sendContent",
                headers={"Authorization": f"Bearer {Config.MANYCHAT_API_KEY}", "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code >= 400:
                logger.error(f"[DEBOUNCE] ManyChat send erro {resp.status_code}: {resp.text[:200]}")
            else:
                logger.info(f"[DEBOUNCE] Resposta enviada via API para {user_name}")

        asyncio.create_task(_tag_interagiu(user_id))

    except Exception as e:
        logger.error(f"[DEBOUNCE] Erro crítico para {user_name} ({user_id}): {e}", exc_info=True)


async def _tag_interagiu(user_id: str):
    """Adiciona tag bot_interagiu no ManyChat sem bloquear o webhook."""
    try:
        if reactivation_svc:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: reactivation_svc.add_manychat_tag(user_id, "bot_interagiu")
            )
    except Exception as e:
        logger.warning(f"[TAG] Erro ao adicionar tag para {user_id}: {e}")


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

TAG_TO_PRODUCT = {
    "open_checkout_mcc20": "o_mapa_convencer",
    "open_checkout_arte20": "a_arte_de_precificar",
}

KEYWORD_TO_PRODUCT = [
    (re.compile(r"\barte\s*20\b", re.IGNORECASE), "a_arte_de_precificar"),
    (re.compile(r"\bmcc\s*20\b", re.IGNORECASE), "o_mapa_convencer"),
]


def _detect_keyword_product(text: str) -> Optional[str]:
    for pattern, product_id in KEYWORD_TO_PRODUCT:
        if pattern.search(text):
            return product_id
    return None


class ManyChatPayload(BaseModel):
    subscriber_id: str
    first_name: Optional[str] = ""
    last_message: Optional[str] = ""
    attachment_type: Optional[str] = ""
    attachment_url: Optional[str] = ""
    tag: Optional[str] = ""  # tag enviada pelo ManyChat: open_checkout_mcc20 | open_checkout_arte20


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

    tag = _clean(payload.tag or "").lower()
    product_from_tag = TAG_TO_PRODUCT.get(tag)
    product_from_keyword = _detect_keyword_product(message)
    triggered_product = product_from_keyword or product_from_tag

    logger.info(f"[DM] {user_name} ({user_id}) [{attachment_type or 'text'}]: '{(message or attachment_url)[:80]}'")

    # Acumula no buffer de debounce
    if user_id not in _message_buffers:
        _message_buffers[user_id] = []
    _message_buffers[user_id].append({
        "user_name": user_name,
        "message": message,
        "history_message": history_message,
        "attachment_type": attachment_type,
        "attachment_url": attachment_url,
        "triggered_product": triggered_product,
        "product_from_keyword": product_from_keyword,
    })

    # Cancela task anterior e agenda nova (reinicia o timer)
    existing = _debounce_tasks.get(user_id)
    if existing and not existing.done():
        existing.cancel()
    _debounce_tasks[user_id] = asyncio.create_task(_process_debounced(user_id))

    # Retorna vazio imediatamente — a resposta real chega via API após o delay
    return JSONResponse({
        "version": "v2",
        "content": {"type": "instagram", "messages": [], "actions": [], "quick_replies": []},
    })


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
    return {"status": "ok", "bot": "Instagram SDR Bot — Eduardo Prado (@pradoclima)", "v": "5a6e430"}


# ─── Webhook Greenn — confirmação de compra ──────────────────────────────────

class GreennPurchasePayload(BaseModel):
    # Campos padrão da Greenn
    email: Optional[str] = ""
    name: Optional[str] = ""
    product_id: Optional[str] = ""
    transaction_id: Optional[str] = ""
    status: Optional[str] = "approved"
    # Campo injetado pelo link rastreado: ?ref=SUBSCRIBER_ID
    ref: Optional[str] = ""
    # Fallback: subscriber_id direto
    subscriber_id: Optional[str] = ""


@app.post("/purchase/confirmed")
async def greenn_purchase_webhook(payload: GreennPurchasePayload):
    """
    Greenn chama este endpoint quando uma compra é confirmada.
    Configure em: Greenn → Produto → Webhooks → URL de notificação

    O subscriber_id chega via parâmetro ?ref= injetado no link pelo bot.
    Fallback: busca por email caso ref não esteja presente.
    """
    if payload.status not in ("approved", "paid", "complete", "completed"):
        return {"message": "status ignorado", "status": payload.status}

    # Prioridade 1: ref rastreado (subscriber_id injetado no link)
    found_id = None
    if payload.ref and payload.ref in conv_manager.conversations:
        found_id = payload.ref
    # Prioridade 2: subscriber_id direto
    elif payload.subscriber_id and payload.subscriber_id in conv_manager.conversations:
        found_id = payload.subscriber_id
    # Prioridade 3: email
    elif payload.email:
        found_id = conv_manager.find_by_email(payload.email)

    if found_id:
        conv_manager.mark_purchased(
            found_id,
            product_id=payload.product_id,
            buyer_email=payload.email,
        )
        if reactivation_svc:
            reactivation_svc.remove_manychat_tag(found_id, "bot_interagiu")
            reactivation_svc.add_manychat_tag(found_id, "bot_comprou")
        logger.info(f"[COMPRA] ✅ Venda confirmada → subscriber {found_id} | ref={payload.ref} | email={payload.email} | produto={payload.product_id}")
        return {"message": "compra registrada", "subscriber_id": found_id}

    logger.warning(f"[COMPRA] ⚠️ Não encontrou subscriber | ref={payload.ref} | email={payload.email}")
    return {"message": "subscriber não encontrado", "ref": payload.ref, "email": payload.email}


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


# ─── Remarketing ─────────────────────────────────────────────────────────────

PRODUCT_SLUG = {
    "mcc20": "o_mapa_convencer",
    "arte20": "a_arte_de_precificar",
}


class ManualRemarketingPayload(BaseModel):
    subscriber_ids: list[str]
    product: Optional[str] = "mcc20"  # mcc20 | arte20


@app.post("/send-remarketing")
def remarketing_manual_send(payload: ManualRemarketingPayload):
    """Dispara remarketing para uma lista manual de subscriber_ids do ManyChat."""
    if not agent or not reactivation_svc:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")

    product_id = PRODUCT_SLUG.get(payload.product.lower(), "o_mapa_convencer")
    product = PRODUCTS[product_id]

    ids = [sid.strip() for sid in payload.subscriber_ids if sid.strip()]
    results = {"produto": product["name"], "total": len(ids), "sent": 0, "failed": 0, "erros": []}

    for user_id in ids:
        tracked_link = f"{product['link'].split('?')[0]}?ref={user_id}"
        try:
            message = agent.generate_remarketing_message(
                user_name="amigo",
                product_name=product["name"],
                product_link=tracked_link,
            )
            import httpx as _httpx
            _payload = {
                "subscriber_id": user_id,
                "data": {
                    "version": "v2",
                    "content": {
                        "type": "instagram",
                        "messages": [{"type": "text", "text": message}],
                        "actions": [],
                        "quick_replies": [],
                    },
                },
            }
            with _httpx.Client(timeout=15) as _client:
                _resp = _client.post(
                    "https://api.manychat.com/fb/sending/sendContent",
                    headers={"Authorization": f"Bearer {Config.MANYCHAT_API_KEY}", "Content-Type": "application/json"},
                    json=_payload,
                )
            if _resp.status_code < 400:
                conv_manager.get_or_create(user_id, user_id)
                conv_manager.add_message(user_id, "assistant", message)
                conv_manager.mark_link_sent(user_id, product_id)
                results["sent"] += 1
                logger.info(f"[REMARKETING MANUAL] → {user_id}")
            else:
                results["failed"] += 1
                results["erros"].append({"id": user_id, "status": _resp.status_code, "body": _resp.text[:200]})
        except Exception as e:
            logger.error(f"[REMARKETING MANUAL] Erro para {user_id}: {e}")
            results["failed"] += 1
            results["erros"].append({"id": user_id, "erro": str(e)})

    return results


def _get_remarketing_leads(slug: str) -> tuple[dict, list]:
    """Retorna (product, leads) para o slug. 'broadcast' = todos os contatos exceto bloqueados."""
    if slug == "broadcast":
        product = PRODUCTS["o_mapa_convencer"]
        leads = [
            c for c in conv_manager.conversations.values()
            if c.get("status") != "vendido"
            and c.get("user_name", "").lower().strip() not in REMARKETING_BLOCKLIST
        ]
    elif slug in PRODUCT_SLUG:
        product = PRODUCTS[PRODUCT_SLUG[slug]]
        leads = conv_manager.get_non_buyers_by_keyword(slug)
    else:
        raise HTTPException(status_code=404, detail=f"Use: mcc20, arte20 ou broadcast")
    return product, leads


@app.get("/remarketing/{slug}/preview")
def remarketing_preview(slug: str):
    """Lista quem vai receber. slug: mcc20 | arte20 | broadcast | manychat"""
    slug = slug.lower()

    if slug == "manychat":
        if not reactivation_svc:
            raise HTTPException(status_code=503, detail="Bot não inicializado.")
        all_subs = reactivation_svc.get_all_subscribers()
        filtered = _filter_blocklist(all_subs)
        return {
            "total_manychat": len(all_subs),
            "total_para_envio": len(filtered),
            "bloqueados": list(REMARKETING_BLOCKLIST),
            "leads": [{"id": s.get("id"), "nome": f"{s.get('first_name','')} {s.get('last_name','')}".strip()} for s in filtered],
        }

    product, leads = _get_remarketing_leads(slug)
    return {
        "produto": product["name"],
        "total": len(leads),
        "bloqueados": list(REMARKETING_BLOCKLIST) if slug in ("broadcast", "manychat") else [],
        "leads": [{"user_id": l["user_id"], "user_name": l["user_name"]} for l in leads],
    }


@app.post("/remarketing/{slug}/send")
def remarketing_send(slug: str):
    """Dispara remarketing. slug: mcc20 | arte20 | broadcast | manychat"""
    if not agent or not reactivation_svc:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")

    slug = slug.lower()
    product = PRODUCTS["o_mapa_convencer"]

    if slug == "manychat":
        all_subs = reactivation_svc.get_all_subscribers()
        filtered = _filter_blocklist(all_subs)
        results = {"total_manychat": len(all_subs), "sent": 0, "failed": 0, "bloqueados": len(all_subs) - len(filtered)}

        for s in filtered:
            user_id = str(s.get("id", ""))
            user_name = s.get("first_name", "amigo").strip() or "amigo"
            tracked_link = f"{product['link'].split('?')[0]}?ref={user_id}"
            try:
                message = agent.generate_remarketing_message(user_name=user_name, product_name=product["name"], product_link=tracked_link)
                if reactivation_svc._send_manychat_dm(user_id, message):
                    results["sent"] += 1
                    logger.info(f"[REMARKETING MANYCHAT] → {user_name} ({user_id})")
                else:
                    results["failed"] += 1
            except Exception as e:
                logger.error(f"[REMARKETING MANYCHAT] Erro para {user_name}: {e}")
                results["failed"] += 1
        return results

    product, leads = _get_remarketing_leads(slug)
    results = {"produto": product["name"], "total": len(leads), "sent": 0, "failed": 0}

    for lead in leads:
        user_id = lead["user_id"]
        user_name = lead["user_name"]
        tracked_link = f"{product['link'].split('?')[0]}?ref={user_id}"
        try:
            message = agent.generate_remarketing_message(user_name=user_name, product_name=product["name"], product_link=tracked_link)
            if reactivation_svc._send_manychat_dm(user_id, message):
                conv_manager.add_message(user_id, "assistant", message)
                results["sent"] += 1
                logger.info(f"[REMARKETING {slug.upper()}] → {user_name} ({user_id})")
            else:
                results["failed"] += 1
        except Exception as e:
            logger.error(f"[REMARKETING {slug.upper()}] Erro para {user_name}: {e}")
            results["failed"] += 1

    return results


# ─── Remarketing geral (todos os contatos menos excluídos) ───────────────────

REMARKETING_BLOCKLIST = {"paulo roberto", "lindival neto", "lilian queiroz"}


def _filter_blocklist(subscribers: list) -> list:
    result = []
    for s in subscribers:
        full = f"{s.get('first_name','').strip()} {s.get('last_name','').strip()}".strip().lower()
        first = s.get("first_name", "").strip().lower()
        if full in REMARKETING_BLOCKLIST or first in REMARKETING_BLOCKLIST:
            logger.info(f"[BLOCKLIST] Ignorando: {full}")
            continue
        result.append(s)
    return result


@app.get("/remarketing/broadcast/preview")
def remarketing_broadcast_preview():
    """Lista todos que vão receber o remarketing geral (exceto bloqueados)."""
    leads = [
        c for c in conv_manager.conversations.values()
        if c.get("status") != "vendido"
        and c.get("user_name", "").lower().strip() not in REMARKETING_BLOCKLIST
    ]
    return {
        "produto": PRODUCTS["o_mapa_convencer"]["name"],
        "total": len(leads),
        "bloqueados": list(REMARKETING_BLOCKLIST),
        "leads": [{"user_id": l["user_id"], "user_name": l["user_name"]} for l in leads],
    }


@app.post("/remarketing/broadcast/send")
def remarketing_broadcast_send():
    """Envia remarketing do Mapa para Convencer para todos os contatos, exceto os 3 bloqueados."""
    if not agent or not reactivation_svc:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")

    product = PRODUCTS["o_mapa_convencer"]
    leads = [
        c for c in conv_manager.conversations.values()
        if c.get("status") != "vendido"
        and c.get("user_name", "").lower().strip() not in REMARKETING_BLOCKLIST
    ]
    results = {"produto": product["name"], "total": len(leads), "sent": 0, "failed": 0, "bloqueados": list(REMARKETING_BLOCKLIST)}

    for lead in leads:
        user_id = lead["user_id"]
        user_name = lead["user_name"]
        tracked_link = f"{product['link'].split('?')[0]}?ref={user_id}"

        try:
            message = agent.generate_remarketing_message(
                user_name=user_name,
                product_name=product["name"],
                product_link=tracked_link,
            )
            if reactivation_svc._send_manychat_dm(user_id, message):
                conv_manager.add_message(user_id, "assistant", message)
                results["sent"] += 1
                logger.info(f"[REMARKETING GERAL] → {user_name} ({user_id})")
            else:
                results["failed"] += 1
        except Exception as e:
            logger.error(f"[REMARKETING GERAL] Erro para {user_name}: {e}")
            results["failed"] += 1

    return results


@app.get("/debug/manychat")
def debug_manychat():
    """Testa endpoints do ManyChat com a key real configurada no Railway."""
    if not Config.MANYCHAT_API_KEY:
        return {"status": "error", "detail": "MANYCHAT_API_KEY não configurada"}
    import httpx
    headers = {"Authorization": f"Bearer {Config.MANYCHAT_API_KEY}"}
    results = {}
    endpoints = [
        # Confirmados
        ("GET",  "fb/page/getInfo",                  {}),
        # Subscribers — prefixo fb
        ("GET",  "fb/subscriber/getAll",              {"count": 5}),
        ("GET",  "fb/subscriber/search",              {"name": "a"}),
        ("POST", "fb/subscriber/search",              {"name": "a"}),
        ("GET",  "fb/subscriber/findByName",          {"name": "a"}),
        ("POST", "fb/subscriber/findByName",          {"name": "a"}),
        # Subscribers — prefixo ig (Instagram)
        ("GET",  "ig/subscriber/getAll",              {"count": 5}),
        ("GET",  "ig/subscriber/search",              {"name": "a"}),
        ("POST", "ig/subscriber/search",              {"name": "a"}),
        # Conversas / histórico
        ("GET",  "fb/subscriber/getConversations",    {}),
        ("GET",  "ig/conversations",                  {}),
        ("GET",  "ig/subscriber/getConversations",    {}),
        # Tags / segments
        ("GET",  "fb/tag/getList",                    {}),
        ("GET",  "ig/tag/getList",                    {}),
    ]
    with httpx.Client(timeout=10) as client:
        for method, path, params in endpoints:
            key = f"{method} {path}"
            try:
                if method == "GET":
                    r = client.get(f"https://api.manychat.com/{path}", headers=headers, params=params)
                else:
                    r = client.post(f"https://api.manychat.com/{path}", headers=headers, json=params)
                results[key] = {"status": r.status_code, "body": r.text[:300]}
            except Exception as e:
                results[key] = {"error": str(e)}
    return results


@app.get("/subscriber/search")
def subscriber_search(name: str):
    """Busca subscriber no ManyChat pelo nome. Ex: /subscriber/search?name=Fabricio"""
    import httpx
    headers = {"Authorization": f"Bearer {Config.MANYCHAT_API_KEY}"}
    with httpx.Client(timeout=10) as client:
        r = client.get(
            "https://api.manychat.com/fb/subscriber/findByName",
            headers=headers,
            params={"name": name},
        )
    return {"status": r.status_code, "data": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:500]}


@app.get("/debug/send/{subscriber_id}")
def debug_send(subscriber_id: str):
    """Testa variações do payload de envio do ManyChat para um subscriber_id."""
    import httpx
    headers = {"Authorization": f"Bearer {Config.MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    text = "Teste de envio — pode ignorar."
    results = {}

    variants = {
        "ig_with_type": {
            "url": "https://api.manychat.com/ig/sending/sendContent",
            "payload": {"subscriber_id": subscriber_id, "data": {"version": "v2", "content": {"type": "instagram", "messages": [{"type": "text", "text": text}], "actions": [], "quick_replies": []}}},
        },
        "ig_without_type": {
            "url": "https://api.manychat.com/ig/sending/sendContent",
            "payload": {"subscriber_id": subscriber_id, "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text}], "actions": [], "quick_replies": []}}},
        },
        "fb_with_type": {
            "url": "https://api.manychat.com/fb/sending/sendContent",
            "payload": {"subscriber_id": subscriber_id, "data": {"version": "v2", "content": {"type": "instagram", "messages": [{"type": "text", "text": text}], "actions": [], "quick_replies": []}}},
        },
        "fb_without_type": {
            "url": "https://api.manychat.com/fb/sending/sendContent",
            "payload": {"subscriber_id": subscriber_id, "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text}], "actions": [], "quick_replies": []}}},
        },
    }

    with httpx.Client(timeout=10) as client:
        for name, v in variants.items():
            try:
                r = client.post(v["url"], headers=headers, json=v["payload"])
                results[name] = {"status": r.status_code, "body": r.text[:300]}
            except Exception as e:
                results[name] = {"error": str(e)}

    return results


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


@app.post("/recover/remarketing")
def recover_remarketing(product: str = "mcc20", days_ago: int = 1):
    """
    Varre ManyChat via findByName (prefixos A-Z+0-9), filtra por ig_last_interaction
    na data correta e dispara remarketing para quem não tem tag bot_comprou.

    GET /recover/remarketing?product=mcc20&days_ago=1  → ontem
    """
    if not agent or not reactivation_svc or not Config.MANYCHAT_API_KEY:
        raise HTTPException(status_code=503, detail="Bot não inicializado ou MANYCHAT_API_KEY ausente.")

    from datetime import datetime, timedelta
    import httpx as _httpx

    target_date = (datetime.now() - timedelta(days=days_ago)).date()
    headers = {"Authorization": f"Bearer {Config.MANYCHAT_API_KEY}"}

    # Nomes mais comuns no Brasil + prefixos de 2 letras para cobertura ampla
    prefixes = [
        "jo", "ma", "an", "pe", "ca", "lu", "fe", "ro", "pa", "ri",
        "vi", "le", "br", "ed", "ra", "al", "gu", "da", "ne", "re",
        "th", "wa", "cl", "si", "fa", "mo", "di", "ti", "cr", "sa",
        "fl", "me", "he", "de", "na", "la", "mi", "go", "ta", "gi",
        "se", "co", "em", "ia", "so", "te", "ba", "ch", "fi", "he",
        "João", "Maria", "José", "Ana", "Pedro", "Carlos", "Luiz",
        "Paulo", "Marcos", "Lucas", "Gabriel", "Rafael", "Felipe",
        "Rodrigo", "Bruno", "Eduardo", "Ricardo", "Gustavo", "Daniel",
        "Matheus", "André", "Fernanda", "Juliana", "Camila", "Amanda",
        "Patricia", "Renata", "Fabricio", "Roberto", "Diego", "Thiago",
        "Marcelo", "Alexandre", "Anderson", "Leandro", "Sergio", "Flavio",
    ]
    seen_ids: set = set()
    candidates = []

    with _httpx.Client(timeout=15) as client:
        for prefix in prefixes:
            try:
                r = client.get(
                    "https://api.manychat.com/fb/subscriber/findByName",
                    headers=headers,
                    params={"name": prefix},
                )
                if r.status_code != 200:
                    continue
                for sub in r.json().get("data", []):
                    sid = str(sub.get("id", ""))
                    if not sid or sid in seen_ids:
                        continue
                    seen_ids.add(sid)

                    # Filtra por data de interação no Instagram
                    ig_last = sub.get("ig_last_interaction") or sub.get("last_interaction")
                    if not ig_last:
                        continue
                    try:
                        sub_date = datetime.fromisoformat(ig_last.replace("Z", "+00:00")).date()
                    except Exception:
                        continue
                    if sub_date != target_date:
                        continue

                    # Pula quem já comprou
                    tags = {t["name"] for t in sub.get("tags", [])}
                    if "bot_comprou" in tags:
                        continue

                    candidates.append({
                        "id": sid,
                        "name": sub.get("name") or sub.get("first_name") or "amigo",
                        "ig_username": sub.get("ig_username", ""),
                        "tags": list(tags),
                    })
            except Exception as e:
                logger.warning(f"[RECOVER] Erro no prefixo '{prefix}': {e}")

    if not candidates:
        return {"data_alvo": str(target_date), "encontrados": 0, "mensagem": "Nenhum subscriber encontrado para essa data."}

    product_id = PRODUCT_SLUG.get(product.lower(), "o_mapa_convencer")
    prod = PRODUCTS[product_id]
    results = {"data_alvo": str(target_date), "encontrados": len(candidates), "sent": 0, "failed": 0, "leads": []}

    for c in candidates:
        user_id = c["id"]
        user_name = c["name"]
        tracked_link = f"{prod['link'].split('?')[0]}?ref={user_id}"
        try:
            message = agent.generate_remarketing_message(
                user_name=user_name,
                product_name=prod["name"],
                product_link=tracked_link,
            )
            payload = {
                "subscriber_id": user_id,
                "data": {"version": "v2", "content": {
                    "type": "instagram",
                    "messages": [{"type": "text", "text": message}],
                    "actions": [], "quick_replies": [],
                }},
            }
            with _httpx.Client(timeout=15) as client:
                resp = client.post(
                    "https://api.manychat.com/fb/sending/sendContent",
                    headers={"Authorization": f"Bearer {Config.MANYCHAT_API_KEY}", "Content-Type": "application/json"},
                    json=payload,
                )
            if resp.status_code < 400:
                conv_manager.get_or_create(user_id, user_name)
                conv_manager.add_message(user_id, "assistant", message)
                conv_manager.mark_link_sent(user_id, product_id)
                results["sent"] += 1
                results["leads"].append({"id": user_id, "nome": user_name, "ig": c["ig_username"], "status": "enviado"})
                logger.info(f"[RECOVER] Remarketing → @{c['ig_username']} ({user_id})")
            else:
                results["failed"] += 1
                results["leads"].append({"id": user_id, "nome": user_name, "ig": c["ig_username"], "status": f"falha_{resp.status_code}"})
        except Exception as e:
            results["failed"] += 1
            results["leads"].append({"id": user_id, "nome": user_name, "status": f"erro: {e}"})

    return results


@app.get("/report/non-buyers")
def report_non_buyers(hours: int = 24):
    """
    Lista quem interagiu nas últimas N horas e não comprou.
    Fonte 1: conversations.json (quando existe)
    Fonte 2: tag bot_interagiu no ManyChat (persiste mesmo após redeploy)
    """
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(hours=hours)
    results = []

    # Fonte 1: dados locais
    for conv in conv_manager.conversations.values():
        if conv.get("status") == "vendido":
            continue
        last = conv.get("last_user_message_at") or conv.get("created_at")
        if not last:
            continue
        try:
            if datetime.fromisoformat(last) >= cutoff:
                results.append({
                    "user_id": conv["user_id"],
                    "user_name": conv.get("user_name"),
                    "stage": conv.get("stage"),
                    "produto_recebido": conv.get("product_recommended"),
                    "link_enviado": conv.get("link_sent", False),
                    "fonte": "local",
                })
        except Exception:
            pass

    # Fonte 2: tag bot_interagiu no ManyChat (busca por nome genérico para listar)
    manychat_ids = set(r["user_id"] for r in results)
    if reactivation_svc and Config.MANYCHAT_API_KEY:
        import httpx
        headers = {"Authorization": f"Bearer {Config.MANYCHAT_API_KEY}"}
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(
                    "https://api.manychat.com/fb/subscriber/findByName",
                    headers=headers,
                    params={"name": " "},
                )
                if r.status_code == 200:
                    for sub in r.json().get("data", []):
                        tags = [t["name"] for t in sub.get("tags", [])]
                        if "bot_interagiu" in tags and str(sub["id"]) not in manychat_ids:
                            results.append({
                                "user_id": str(sub["id"]),
                                "user_name": sub.get("name") or sub.get("first_name"),
                                "ig_username": sub.get("ig_username"),
                                "stage": "desconhecido",
                                "produto_recebido": None,
                                "link_enviado": None,
                                "fonte": "manychat_tag",
                            })
        except Exception as e:
            logger.warning(f"[REPORT] Erro ao buscar ManyChat tags: {e}")

    return {
        "periodo_horas": hours,
        "total_nao_compradores": len(results),
        "leads": results,
    }


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


def _inject_tracking(text: str, subscriber_id: str) -> str:
    """Adiciona ?ref=SUBSCRIBER_ID em qualquer link Greenn gerado pelo Claude."""
    for product in PRODUCTS.values():
        base = product["link"].split("?")[0]
        if base in text:
            text = text.replace(base, f"{base}?ref={subscriber_id}")
    return text


def _build_response(text: str, subscriber_id: str = "") -> JSONResponse:
    safe = re.sub(r"\{\{[^}]*\}\}", "", text).strip()
    if subscriber_id:
        safe = _inject_tracking(safe, subscriber_id)
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
