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
from .meeting_reminders import MeetingReminderService
from .agenda_slots import AgendaSlots
from .calendar_manager import CalendarManager
from .config import Config
from .products import PRODUCTS
from .seguros_vida_kb import SEGURO_VIDA_KEYWORDS

logger = logging.getLogger(__name__)

app = FastAPI(title="Instagram SDR Bot — Eduardo Prado (@pradoclima)", version="1.0.0")

agent: SalesAgent = None
conv_manager: ConversationManager = None
reactivation_svc: ReactivationService = None
meeting_svc: MeetingReminderService = None
agenda: AgendaSlots = None
calendar_mgr: CalendarManager = None

# Debounce: acumula mensagens rápidas e processa como bloco único
DEBOUNCE_DELAY = 4  # segundos de silêncio antes de processar
_debounce_tasks: dict[str, asyncio.Task] = {}
_message_buffers: dict[str, list[dict]] = {}  # user_id → lista de payloads


@app.on_event("startup")
async def startup():
    global agent, conv_manager, reactivation_svc, agenda, calendar_mgr
    if not Config.ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY não configurada! Adicione em Railway → Variables.")
    else:
        agent = SalesAgent(Config.ANTHROPIC_API_KEY)
        logger.info("Sales Agent (Eduardo Prado) inicializado.")
    conv_manager = ConversationManager()
    logger.info("ConversationManager inicializado.")

    calendar_mgr = CalendarManager(
        script_url=Config.GOOGLE_SCRIPT_URL,
        script_secret=Config.GOOGLE_SCRIPT_SECRET,
        service_account_json=Config.GOOGLE_SERVICE_ACCOUNT_JSON,
        calendar_id=Config.GUILHERME_CALENDAR_ID,
    )
    if not calendar_mgr.has_real_calendar():
        logger.warning(
            "[STARTUP] Sem GOOGLE_SCRIPT_URL nem GOOGLE_SERVICE_ACCOUNT_JSON — "
            "o bot vai usar grade estática e NÃO criará eventos reais."
        )

    agenda = AgendaSlots(calendar_manager=calendar_mgr)
    initial = len(agenda.list_available(days_ahead=7))
    logger.info(f"AgendaSlots inicializada — {initial} slots livres na próxima semana.")

    if agent and conv_manager:
        reactivation_svc = ReactivationService(
            agent=agent,
            conv_manager=conv_manager,
            manychat_key=Config.MANYCHAT_API_KEY,
            instagram_token=Config.INSTAGRAM_ACCESS_TOKEN,
            instagram_account_id=Config.INSTAGRAM_ACCOUNT_ID,
        )
        global meeting_svc
        meeting_svc = MeetingReminderService(
            agent=agent,
            conv_manager=conv_manager,
            reactivation_svc=reactivation_svc,
        )
        asyncio.create_task(_reactivation_loop())
        asyncio.create_task(_purchase_followup_loop())
        asyncio.create_task(_meeting_reminders_loop())
        logger.info("Schedulers de reativação, follow-up de compra e lembretes de reunião iniciados.")


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
        conv = conv_manager.get_or_create(user_id, user_name)
        history = conv_manager.get_history(user_id)
        conv_manager.add_message(user_id, "user", history_message)

        # Se a pessoa já tem reunião agendada e respondeu confirmando/cancelando, registra.
        if meeting_svc:
            try:
                meeting_verdict = meeting_svc.handle_user_reply(user_id, combined_message or history_message)
                if meeting_verdict:
                    logger.info(f"[MEETING] {user_name} ({user_id}) → {meeting_verdict}")
            except Exception as mv_err:
                logger.warning(f"[MEETING] Falha ao avaliar confirmação: {mv_err}")

        # Stage + keyword detection
        triggered_product = last["triggered_product"]
        product_from_keyword = last["product_from_keyword"]

        # Trava VIDA persistente — vale para o resto da conversa, não só pra essa mensagem.
        # Uma vez travado, o lead só recebe fluxo de seguro de vida (agenda),
        # independente do que ele falar depois (negócio, vendas, família, etc.).
        vida_locked = bool(
            triggered_product == "seguro_vida"
            or conv.get("vida_locked")
            or conv.get("product_recommended") == "seguro_vida"
            or conv.get("meeting_scheduled")
        )
        if vida_locked:
            if triggered_product and triggered_product != "seguro_vida":
                logger.info(
                    f"[VIDA-LOCK] Conv travada em vida — ignora triggered_product={triggered_product} "
                    f"({user_name} / {user_id})"
                )
            if product_from_keyword and product_from_keyword != "seguro_vida":
                logger.info(
                    f"[VIDA-LOCK] Conv travada em vida — ignora keyword={product_from_keyword} "
                    f"({user_name} / {user_id})"
                )
                product_from_keyword = None
            triggered_product = "seguro_vida"
            if not conv.get("vida_locked"):
                conv["vida_locked"] = True
                try:
                    conv_manager._save()
                except Exception:
                    pass

        # Produto já recomendado em mensagens anteriores (trava anti-troca-de-produto)
        already_recommended = conv.get("product_recommended") if conv.get("link_sent") else None
        locked_product = None
        if not triggered_product and already_recommended and conv.get("status") != "vendido":
            locked_product = already_recommended

        if product_from_keyword:
            keyword_label = {
                "o_mapa_convencer": "mcc20",
                "a_arte_de_precificar": "arte20",
                "estrategias_vendas_digital": "metodo26",
                "blindar_mente_filho": "familia26",
            }.get(product_from_keyword, product_from_keyword)
            conv_manager.mark_keyword_triggered(user_id, keyword_label, product_from_keyword)
        if triggered_product:
            conv_manager.mark_link_sent(user_id, triggered_product)

        current_stage = conv_manager.get_stage(user_id)
        if triggered_product or locked_product:
            current_stage = "fechando"
        elif current_stage == "conexao" and len(history) >= 2:
            current_stage = "qualificando"
        elif current_stage == "qualificando" and len(history) >= 6:
            current_stage = "apresentando"
        conv_manager.update_stage(user_id, current_stage)

        extra_context = ""

        # Detecta se conversa é sobre seguro de vida → injeta agenda
        seguros_now = _is_seguros_context(combined_message, history, conv) or vida_locked
        if seguros_now and agenda:
            # Limit baixo de propósito: Claude só precisa de 8-10 slots pra oferecer 3.
            # Lista maior aumenta latência e custo sem benefício real.
            agenda_block = agenda.format_for_prompt(days_ahead=7, limit=10)
            extra_context += agenda_block + "\n\n"
            # Marca a tag vida26_active no ManyChat (uma única vez por conversa)
            if not conv.get("vida26_active_tagged") and reactivation_svc:
                asyncio.create_task(_apply_vida26_tag(user_id, "vida26_active"))
                conv["vida26_active_tagged"] = True
                conv_manager._save()

        if vida_locked:
            # Não dispara o "manda AGORA o link" — vida exige qualificação + email +
            # whatsapp + marcador [BOOK: ...]. O SEGURO_VIDA_PROMPT_BLOCK no system
            # prompt já contém o roteiro.
            extra_context += (
                "TRAVA VIDA (OBRIGATÓRIA): Este lead veio da campanha de seguro de vida "
                "(tag vida26_active). Seu único objetivo é levar ele pra reunião de 30 min "
                "com a assessoria do Guilherme. "
                "Se ele falar de negócio, vendas, marketing, empresa, família, escola ou qualquer "
                "outro tema, escuta com empatia mas SEMPRE redireciona pra proteção (renda, padrão "
                "de vida da família, sucessão). "
                "PROIBIDO oferecer Método 26, Família 26, Mapa de Convencer, Arte de Precificar "
                "ou qualquer outro produto. PROIBIDO mandar link de curso. "
                "PROIBIDO mudar de assunto pra outro produto. "
                "Termina sempre AGENDANDO a reunião.\n\n"
            )
        elif triggered_product:
            p = PRODUCTS[triggered_product]
            extra_context = (
                f"AÇÃO OBRIGATÓRIA: O lead digitou uma palavra-chave de produto. "
                f"Mande AGORA o link do '{p['name']}': {p['link']} "
                f"Seja direto, mande o link já na primeira frase. "
                f"Depois faça UMA pergunta para continuar a conversa. "
                f"NÃO mencione, NÃO ofereça e NÃO mande link de NENHUM outro produto."
            )
        elif locked_product:
            p = PRODUCTS[locked_product]
            extra_context = (
                f"TRAVA DE PRODUTO: O lead JÁ recebeu o link do produto '{p['name']}' "
                f"({p['link']}) em mensagem anterior. NÃO mude de produto. "
                f"NÃO ofereça nem mencione outro produto. NÃO mande link de outro produto. "
                f"Continue focado em fechar ESTE produto: trate objeção, tire dúvida, "
                f"reforce a garantia de 7 dias, ou reenvie o MESMO link se for o caso. "
                f"Frases como 'quero essa condição', 'topei', 'me manda', 'manda aí' "
                f"significam interesse NESTE produto, fecha aqui."
            )

        try:
            # Timeout aumentado: o webhook do ManyChat já retornou vazio antes desse
            # ponto (debounce assíncrono). A latência aqui só afeta o tempo até a DM
            # final chegar — 15s é confortável e evita fallback estático no modo seguros.
            response_text = await asyncio.wait_for(
                agent.generate_dm_response_async(
                    user_name=user_name,
                    user_message=combined_message or history_message,
                    conversation_history=history,
                    stage=current_stage,
                    attachment_type=attachment_type,
                    attachment_url=attachment_url,
                    extra_context=extra_context,
                    force_product="seguro_vida" if vida_locked else None,
                ),
                timeout=15,
            )
        except asyncio.TimeoutError:
            response_text = STAGE_FALLBACKS.get(current_stage, STAGE_FALLBACKS["conexao"])
            logger.warning(f"[DEBOUNCE] Timeout Claude para {user_name} — fallback '{current_stage}'")
        except Exception as claude_err:
            response_text = STAGE_FALLBACKS.get(current_stage, STAGE_FALLBACKS["conexao"])
            logger.error(f"[DEBOUNCE] Erro Claude para {user_name}: {claude_err}")

        # Pós-geração: se há produto travado e o Claude vazou link de OUTRO produto, corrige
        if locked_product:
            detected_in_response = _detect_link_sent(response_text)
            if detected_in_response and detected_in_response != locked_product:
                p = PRODUCTS[locked_product]
                response_text = (
                    f"{user_name}, segue o link de novo, qualquer dúvida me chama: {p['link']}\n\n"
                    f"O que tá te travando pra fechar agora? Preço, tempo ou outra coisa?"
                )
                logger.warning(
                    f"[LOCK] Claude tentou trocar de produto ({locked_product} → {detected_in_response}) "
                    f"para {user_name} ({user_id}). Resposta substituída pela versão segura."
                )

        conv_manager.add_message(user_id, "assistant", response_text)
        link_sent = _detect_link_sent(response_text)
        if link_sent:
            conv_manager.mark_link_sent(user_id, link_sent)

        # Envia via API (não via webhook response — o webhook já retornou vazio)
        safe = re.sub(r"\{\{[^}]*\}\}", "", response_text).strip()
        safe = _sanitize_persona(safe, user_id, user_name)
        # Detecta marcador [BOOK: ISO] gerado pelo Claude e faz a reserva real
        safe = _process_booking_marker(safe, user_id, user_name)
        if user_id:
            safe = _inject_tracking(safe, user_id)

        import httpx as _httpx
        bubbles = _split_into_bubbles(safe, max_chars=180)
        if any(len(b) > 220 for b in bubbles):
            logger.warning(
                f"[BUBBLES] Algum balão saiu acima do limite ({[len(b) for b in bubbles]}) "
                f"para {user_name} ({user_id}). Mensagem pode estar pesada."
            )
        payload = {
            "subscriber_id": user_id,
            "data": {"version": "v2", "content": {
                "type": "instagram",
                "messages": [{"type": "text", "text": b} for b in bubbles],
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


async def _meeting_reminders_loop():
    """Roda a cada 5 minutos e dispara lembretes de reunião (2d / manhã / 1h / pressão)."""
    while True:
        await asyncio.sleep(300)
        if meeting_svc:
            try:
                result = meeting_svc.run_tick()
                if any(v > 0 for v in result.values()):
                    logger.info(f"[MEETING REMINDERS] {result}")
            except Exception as e:
                logger.error(f"[MEETING REMINDERS] Erro: {e}", exc_info=True)


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
                    # Follow-up de seguro de vida = checar se a reunião foi marcada, tom diferente
                    if product_id == "seguro_vida":
                        continue
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
    "open_checkout_metodo26": "estrategias_vendas_digital",
    "metodo26": "estrategias_vendas_digital",
    "método26": "estrategias_vendas_digital",
    "open_checkout_familia26": "blindar_mente_filho",
    "familia26": "blindar_mente_filho",
    "família26": "blindar_mente_filho",
    # Campanha seguro de vida — lead vem travado pra agenda do Guilherme
    "vida26_active": "seguro_vida",
    "vida26": "seguro_vida",
    "open_checkout_vida26": "seguro_vida",
    "vida_active": "seguro_vida",
    "seguro_vida_active": "seguro_vida",
}

KEYWORD_TO_PRODUCT = [
    (re.compile(r"\barte\s*20\b", re.IGNORECASE), "a_arte_de_precificar"),
    (re.compile(r"\bmcc\s*20\b", re.IGNORECASE), "o_mapa_convencer"),
    (re.compile(r"\bm[eé]todo\s*26\b", re.IGNORECASE), "estrategias_vendas_digital"),
    (re.compile(r"\bm[eé]todo\b", re.IGNORECASE), "estrategias_vendas_digital"),
    # Família 26 — captura variações comuns de digitação (familia26, família 26, famila26, etc.)
    (re.compile(r"\bfam[ií]?l[ií]?a\s*26\b", re.IGNORECASE), "blindar_mente_filho"),
    (re.compile(r"\bfam[ií]?l[ií]?a\b", re.IGNORECASE), "blindar_mente_filho"),
]


def _detect_keyword_product(text: str) -> Optional[str]:
    for pattern, product_id in KEYWORD_TO_PRODUCT:
        if pattern.search(text):
            return product_id
    return None


def _detect_tag_product(tag: str) -> Optional[str]:
    """Tag exata OU substring de campanha → produto correspondente."""
    if not tag:
        return None
    exact = TAG_TO_PRODUCT.get(tag)
    if exact:
        return exact
    # Vida vence as outras — checa primeiro pra leads da campanha de seguro de vida
    # que possam ter outras tags acumuladas.
    if "vida26" in tag or "vida_active" in tag:
        return "seguro_vida"
    if "metodo26" in tag or "método26" in tag:
        return "estrategias_vendas_digital"
    if "familia26" in tag or "família26" in tag:
        return "blindar_mente_filho"
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
    product_from_tag = _detect_tag_product(tag)
    product_from_keyword = _detect_keyword_product(message)

    # Trava VIDA: tag da campanha de seguro de vida vence qualquer keyword de outro produto.
    # Lead que veio por essa campanha NÃO recebe Método 26 / Família 26 / MCC / Arte —
    # mesmo se mencionar "vendas" ou "família" no meio da conversa.
    if product_from_tag == "seguro_vida":
        if product_from_keyword and product_from_keyword != "seguro_vida":
            logger.info(
                f"[VIDA-LOCK] Tag vida26 ignora keyword product={product_from_keyword} "
                f"para {user_name} ({user_id})"
            )
        product_from_keyword = None
        triggered_product = "seguro_vida"
    else:
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


# ─── Agenda de slots ────────────────────────────────────────────────────────
# A agenda agora é apenas leitura — fonte da verdade é o Google Calendar do
# Guilherme (grsouza93ip@gmail.com). Para bloquear um horário, basta criar um
# evento direto no Calendar dele que o bot deixa de oferecê-lo.

@app.get("/agenda/available")
def agenda_list_available(days: int = 7, period: Optional[str] = None):
    if not agenda:
        raise HTTPException(status_code=503, detail="Agenda não inicializada.")
    slots = agenda.list_available(days_ahead=days, period=period)
    return {
        "total": len(slots),
        "slots": [{"iso": s["iso"], "status": s["status"]} for s in slots],
    }


@app.get("/agenda/health")
def agenda_health():
    """Verifica se o bot tem acesso real à agenda do Guilherme."""
    if not calendar_mgr:
        raise HTTPException(status_code=503, detail="CalendarManager não inicializado.")
    return {
        "has_real_calendar": calendar_mgr.has_real_calendar(),
        "calendar_id": calendar_mgr.calendar_id,
        "mode": (
            "apps_script" if calendar_mgr.script_url
            else "service_account" if calendar_mgr._service
            else "link_fallback"
        ),
    }


class AgendaBookPayload(BaseModel):
    iso: str                       # ISO 8601 do slot (ex.: "2026-06-04T10:00:00-03:00")
    user_name: str = "Lead"
    user_email: Optional[str] = "" # se vier, o lead vira attendee e recebe convite
    whatsapp: Optional[str] = ""   # vai pra descrição do evento
    qualification: Optional[str] = ""  # pré-qualificação pro closer
    subscriber_id: Optional[str] = ""  # ManyChat subscriber_id — se vier, dispara cadência de lembretes


@app.post("/agenda/book")
def agenda_book(payload: AgendaBookPayload):
    """Cria o evento real na agenda do Guilherme. Útil pra testes e bookings manuais."""
    if not agenda:
        raise HTTPException(status_code=503, detail="Agenda não inicializada.")
    subscriber = payload.subscriber_id or f"manual_{int(__import__('time').time())}"
    result = agenda.reserve(
        iso=payload.iso,
        subscriber_id=subscriber,
        user_name=payload.user_name,
        user_email=payload.user_email or "",
        whatsapp=payload.whatsapp or "",
        qualification=payload.qualification or "",
    )
    if not result:
        raise HTTPException(status_code=409, detail="Slot indisponível ou criação do evento falhou.")
    if payload.subscriber_id and meeting_svc and conv_manager:
        if payload.subscriber_id not in conv_manager.conversations:
            conv_manager.get_or_create(payload.subscriber_id, payload.user_name)
        meeting_svc.schedule(payload.subscriber_id, result["iso"], meet_link=result.get("meet_link", ""))
        conv = conv_manager.conversations.get(payload.subscriber_id)
        if conv is not None:
            conv["meeting_event_id"] = result.get("event_id", "")
            if payload.user_email:
                conv["lead_email"] = payload.user_email
            if payload.whatsapp:
                conv["lead_whatsapp"] = payload.whatsapp
            if payload.qualification:
                conv["lead_qualification"] = payload.qualification
            conv_manager._save()
    return result


class AgendaCancelPayload(BaseModel):
    event_id: Optional[str] = ""        # cancela diretamente pelo eventId
    subscriber_id: Optional[str] = ""   # OU pelo subscriber (busca event_id no conv)
    notify_attendees: bool = True       # avisa o lead via email (se ele era attendee)


@app.post("/agenda/cancel")
def agenda_cancel(payload: AgendaCancelPayload):
    """Cancela um evento na agenda do Guilherme.

    Aceita event_id direto (pra cleanup/testes) OU subscriber_id (busca o
    evento na conversa). Quando vier subscriber_id, também marca como cancelado
    no meeting_svc — corta a cadência de lembretes."""
    if not calendar_mgr:
        raise HTTPException(status_code=503, detail="CalendarManager não inicializado.")

    event_id = (payload.event_id or "").strip()
    sub_id = (payload.subscriber_id or "").strip()

    if not event_id and sub_id and conv_manager:
        conv = conv_manager.conversations.get(sub_id) or {}
        event_id = (conv.get("meeting_event_id") or "").strip()

    if not event_id:
        raise HTTPException(status_code=400, detail="event_id ou subscriber_id com reunião agendada são obrigatórios.")

    result = calendar_mgr.cancel_meeting(event_id, notify_attendees=payload.notify_attendees)
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=f"Falha ao cancelar evento: {result.get('error')}")

    if sub_id and meeting_svc and conv_manager and sub_id in conv_manager.conversations:
        meeting_svc.mark_cancelled(sub_id)
        conv = conv_manager.conversations.get(sub_id)
        if conv is not None:
            conv["meeting_event_id"] = ""
            conv_manager._save()

    return {"cancelled": True, "event_id": event_id, "subscriber_id": sub_id or None}


# ─── Reuniões com a assessoria ──────────────────────────────────────────────

class MeetingSchedulePayload(BaseModel):
    subscriber_id: str
    meeting_time: str  # ISO 8601, com ou sem timezone (assume BRT se sem)
    meet_link: Optional[str] = ""


@app.post("/meeting/schedule")
def meeting_schedule(payload: MeetingSchedulePayload):
    """Registra uma reunião marcada pelo lead na agenda da assessoria.

    Use este endpoint via Zapier/n8n/manual quando souber que o lead
    agendou (ex.: webhook do Google Calendar)."""
    if not meeting_svc or not conv_manager:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")
    if payload.subscriber_id not in conv_manager.conversations:
        # cria conversa vazia para permitir agendar mesmo sem histórico prévio
        conv_manager.get_or_create(payload.subscriber_id, payload.subscriber_id)
    ok = meeting_svc.schedule(payload.subscriber_id, payload.meeting_time, payload.meet_link or "")
    if not ok:
        raise HTTPException(status_code=400, detail="meeting_time inválido (use ISO 8601).")
    return {"message": "reunião registrada", "subscriber_id": payload.subscriber_id}


@app.post("/meeting/{subscriber_id}/confirm")
def meeting_confirm(subscriber_id: str):
    if not meeting_svc:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")
    ok = meeting_svc.mark_confirmed(subscriber_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    return {"message": "reunião confirmada", "subscriber_id": subscriber_id}


@app.post("/meeting/{subscriber_id}/cancel")
def meeting_cancel(subscriber_id: str):
    """Cancela a reunião — corta lembretes E apaga o evento na agenda do Guilherme."""
    if not meeting_svc or not conv_manager:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")
    ok = meeting_svc.mark_cancelled(subscriber_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Conversa não encontrada.")
    # Apaga o evento na agenda do Guilherme (se houver event_id salvo).
    cancel_result = None
    conv = conv_manager.conversations.get(subscriber_id) or {}
    event_id = (conv.get("meeting_event_id") or "").strip()
    if event_id and calendar_mgr:
        cancel_result = calendar_mgr.cancel_meeting(event_id, notify_attendees=True)
        if cancel_result.get("success"):
            conv["meeting_event_id"] = ""
            conv_manager._save()
            logger.info(f"[MEETING] Evento {event_id} apagado da agenda do Guilherme.")
        else:
            logger.warning(f"[MEETING] Falha ao apagar evento {event_id}: {cancel_result.get('error')}")
    return {
        "message": "reunião cancelada",
        "subscriber_id": subscriber_id,
        "calendar_cancelled": bool(cancel_result and cancel_result.get("success")),
    }


@app.post("/meeting/tick")
def meeting_tick():
    """Roda manualmente uma rodada do scheduler de lembretes (debug/cron externo)."""
    if not meeting_svc:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")
    return meeting_svc.run_tick()


@app.get("/meeting/upcoming")
def meeting_upcoming():
    if not conv_manager:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")
    upcoming = []
    for c in conv_manager.conversations.values():
        if c.get("meeting_scheduled") and c.get("meeting_status") not in ("cancelled", "completed", "no_show"):
            upcoming.append({
                "user_id": c.get("user_id"),
                "user_name": c.get("user_name"),
                "meeting_time": c.get("meeting_time"),
                "meeting_meet_link": c.get("meeting_meet_link"),
                "meeting_confirmed": c.get("meeting_confirmed", False),
                "meeting_status": c.get("meeting_status", "scheduled"),
                "reminders": c.get("meeting_reminders", {}),
            })
    upcoming.sort(key=lambda x: x.get("meeting_time") or "")
    return {"total": len(upcoming), "meetings": upcoming}


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
    "metodo26": "estrategias_vendas_digital",
    "familia26": "blindar_mente_filho",
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


# Regex por product_id, agrupando os patterns de KEYWORD_TO_PRODUCT
PRODUCT_REGEXES: dict[str, list] = {}
for _pattern, _pid in KEYWORD_TO_PRODUCT:
    PRODUCT_REGEXES.setdefault(_pid, []).append(_pattern)


def _historico_menciona_produto(history: list, product_id: str) -> Optional[str]:
    """
    Verifica se o histórico do lead (mensagens do usuário) menciona alguma
    palavra-chave do produto. Retorna o trecho que casou ou None.
    """
    patterns = PRODUCT_REGEXES.get(product_id, [])
    if not patterns:
        return None
    for msg in history:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "") or ""
        for p in patterns:
            m = p.search(content)
            if m:
                return content[:120]
    return None


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


# ─── Remarketing por histórico — varre todas conversas, não só keywords_triggered ─

@app.get("/remarketing/{slug}/preview-historico")
def remarketing_preview_historico(slug: str):
    """
    Lista todos que JÁ MENCIONARAM o termo do produto no histórico, mesmo que
    nunca tenham sido marcados em keywords_triggered (útil para leads anteriores
    ao deploy da palavra-chave). slug: mcc20 | arte20 | metodo26 | familia26
    """
    slug = slug.lower()
    if slug not in PRODUCT_SLUG:
        raise HTTPException(status_code=404, detail=f"Use: {list(PRODUCT_SLUG.keys())}")
    if not conv_manager:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")

    product_id = PRODUCT_SLUG[slug]
    product = PRODUCTS[product_id]

    leads = []
    for conv in conv_manager.conversations.values():
        if conv.get("status") == "vendido":
            continue
        match = _historico_menciona_produto(conv.get("history", []), product_id)
        if match:
            leads.append({
                "user_id": conv["user_id"],
                "user_name": conv.get("user_name"),
                "stage": conv.get("stage"),
                "status": conv.get("status"),
                "product_recommended": conv.get("product_recommended"),
                "link_sent": conv.get("link_sent", False),
                "trecho_que_casou": match,
            })

    return {
        "produto": product["name"],
        "link": product["link"],
        "slug": slug,
        "total": len(leads),
        "leads": leads,
    }


@app.post("/remarketing/{slug}/send-historico")
def remarketing_send_historico(slug: str):
    """
    Envia remarketing com o link correto do produto para todos que mencionaram o
    termo no histórico (mesmo que keywords_triggered não esteja marcado).
    Marca retroativamente keywords_triggered e product_recommended.
    slug: mcc20 | arte20 | metodo26 | familia26
    """
    if not agent or not reactivation_svc:
        raise HTTPException(status_code=503, detail="Bot não inicializado.")

    slug = slug.lower()
    if slug not in PRODUCT_SLUG:
        raise HTTPException(status_code=404, detail=f"Use: {list(PRODUCT_SLUG.keys())}")

    product_id = PRODUCT_SLUG[slug]
    product = PRODUCTS[product_id]

    leads = []
    for conv in conv_manager.conversations.values():
        if conv.get("status") == "vendido":
            continue
        match = _historico_menciona_produto(conv.get("history", []), product_id)
        if match:
            leads.append(conv)

    results = {
        "produto": product["name"],
        "link_base": product["link"],
        "slug": slug,
        "total_encontrados": len(leads),
        "sent": 0,
        "failed": 0,
        "leads": [],
    }

    for lead in leads:
        user_id = lead["user_id"]
        user_name = (lead.get("user_name") or "amigo").strip() or "amigo"
        tracked_link = f"{product['link'].split('?')[0]}?ref={user_id}"

        try:
            message = agent.generate_remarketing_message(
                user_name=user_name,
                product_name=product["name"],
                product_link=tracked_link,
            )
            if reactivation_svc._send_manychat_dm(user_id, message):
                conv_manager.add_message(user_id, "assistant", message)
                conv_manager.mark_keyword_triggered(user_id, slug, product_id)
                conv_manager.mark_link_sent(user_id, product_id)
                results["sent"] += 1
                results["leads"].append({"user_id": user_id, "user_name": user_name, "status": "enviado"})
                logger.info(f"[REMARKETING HIST {slug.upper()}] → {user_name} ({user_id})")
            else:
                results["failed"] += 1
                results["leads"].append({"user_id": user_id, "user_name": user_name, "status": "falha_envio"})
        except Exception as e:
            logger.error(f"[REMARKETING HIST {slug.upper()}] Erro {user_name}: {e}")
            results["failed"] += 1
            results["leads"].append({"user_id": user_id, "user_name": user_name, "status": f"erro: {e}"})

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


_SEGURO_KEYS_LOWER = {k.lower() for k in SEGURO_VIDA_KEYWORDS}


def _is_seguros_context(message: str, history: list[dict], conv: dict) -> bool:
    """Decide se a conversa atual é sobre seguro de vida, pra injetar a agenda
    no contexto do Claude. Heurística simples: palavra-chave na mensagem
    atual OU já houve reunião agendada OU já houve menção em histórico recente."""
    if conv.get("vida_locked"):
        return True
    msg = (message or "").lower()
    if any(k in msg for k in _SEGURO_KEYS_LOWER):
        return True
    if conv.get("meeting_scheduled") or conv.get("product_recommended") == "seguro_vida":
        return True
    for turn in history[-6:]:
        if any(k in turn.get("content", "").lower() for k in _SEGURO_KEYS_LOWER):
            return True
    return False


# Marcador de reserva — Claude inclui [BOOK: ...] quando o lead confirma slot.
# Formato novo (preferido): ISO=... | EMAIL=... | WHATSAPP=... | QUAL=...
# Formato legado (compat): apenas a ISO crua, sem chaves.
_BOOK_MARKER_RE = re.compile(r"^[ \t]*\[BOOK:\s*([^\]]+)\][ \t]*\n?", re.IGNORECASE | re.MULTILINE)

_KEY_ALIASES = {
    "iso": "iso", "datetime": "iso", "data": "iso",
    "email": "email", "e-mail": "email", "mail": "email",
    "whatsapp": "whatsapp", "wpp": "whatsapp", "telefone": "whatsapp",
    "phone": "whatsapp", "tel": "whatsapp", "fone": "whatsapp",
    "qual": "qual", "qualificacao": "qual", "qualificação": "qual",
    "pre-qual": "qual", "summary": "qual", "resumo": "qual",
}


def _parse_book_payload(content: str) -> dict:
    """Parseia o conteúdo do [BOOK: ...]. Tolera ordem variável, espaços e o
    formato legado (apenas ISO crua)."""
    content = content.strip()
    if "=" not in content:
        # Formato legado: só a ISO.
        return {"iso": content}

    out: dict = {}
    parts = re.split(r"\s*\|\s*", content)
    for p in parts:
        if "=" not in p:
            continue
        key, value = p.split("=", 1)
        norm_key = _KEY_ALIASES.get(key.strip().lower())
        if not norm_key:
            continue
        out[norm_key] = value.strip()
    return out


def _process_booking_marker(text: str, user_id: str, user_name: str) -> str:
    """Detecta '[BOOK: ...]' na resposta do Claude e dispara a reserva real.
    Remove a linha do marcador antes de enviar pro lead."""
    m = _BOOK_MARKER_RE.search(text)
    if not m:
        return text
    payload = _parse_book_payload(m.group(1))
    cleaned = _BOOK_MARKER_RE.sub("", text).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    iso = payload.get("iso", "").strip()
    if not iso:
        logger.warning(f"[BOOK] Marcador sem ISO — {user_name} ({user_id}). Conteúdo: {m.group(1)[:120]}")
        return cleaned

    if not agenda or not meeting_svc:
        logger.warning(f"[BOOK] Marcador encontrado mas agenda/meeting_svc não estão prontos.")
        return cleaned

    slot = agenda.reserve(
        iso=iso,
        subscriber_id=user_id,
        user_name=user_name or "Lead",
        user_email=payload.get("email", ""),
        whatsapp=payload.get("whatsapp", ""),
        qualification=payload.get("qual", ""),
    )
    if not slot:
        logger.warning(f"[BOOK] Slot {iso} indisponível ou criação falhou — {user_name} ({user_id}).")
        return cleaned + (
            "\n\n(Acabei de ver aqui e esse horário foi pego agora. "
            "Me dá um instante que eu confiro outras opções da semana.)"
        )

    meet_link = slot.get("meet_link") or ""
    event_id = slot.get("event_id") or ""
    meeting_svc.schedule(user_id, slot["iso"], meet_link=meet_link)
    # Guarda contato + event_id no conv pra cancel/lembretes posteriores.
    if conv_manager:
        conv_manager.mark_link_sent(user_id, "seguro_vida")
        conv = conv_manager.conversations.get(user_id)
        if conv is not None:
            conv["meeting_event_id"] = event_id
            if payload.get("email"):
                conv["lead_email"] = payload["email"]
            if payload.get("whatsapp"):
                conv["lead_whatsapp"] = payload["whatsapp"]
            if payload.get("qual"):
                conv["lead_qualification"] = payload["qual"]
            conv_manager._save()
    logger.info(
        f"[BOOK] Reservado {slot['iso']} para {user_name} ({user_id}) — "
        f"event_id={event_id} mode={slot.get('mode')} "
        f"email={'sim' if payload.get('email') else 'nao'} "
        f"wpp={'sim' if payload.get('whatsapp') else 'nao'} "
        f"qual={'sim' if payload.get('qual') else 'nao'}"
    )
    # Fecha o ciclo da tag: remove vida26_active e adiciona vida26_done
    # → o Condition no flow do ManyChat sai do loop e vai pra End Flow.
    if reactivation_svc:
        asyncio.create_task(_apply_vida26_done(user_id))
    return cleaned


async def _apply_vida26_tag(user_id: str, tag_name: str):
    """Helper async — evita bloquear o webhook em IO de tag."""
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: reactivation_svc.add_manychat_tag(user_id, tag_name)
        )
    except Exception as e:
        logger.warning(f"[TAG] Falha ao adicionar {tag_name} em {user_id}: {e}")


async def _apply_vida26_done(user_id: str):
    """Marca o lead como vida26_done e remove vida26_active — sai do loop."""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: reactivation_svc.add_manychat_tag(user_id, "vida26_done"))
        await loop.run_in_executor(None, lambda: reactivation_svc.remove_manychat_tag(user_id, "vida26_active"))
    except Exception as e:
        logger.warning(f"[TAG] Falha ao fechar vida26 para {user_id}: {e}")


# Regras de sanitização de persona: o lead JAMAIS pode ver o nome real do
# especialista de seguros que faz a reunião. Se o Claude escapar das
# instruções, esta sanitização é a última linha de defesa.
#
# Atenção: o lead pode se chamar Guilherme. NÃO podemos atropelar o nome
# dele. Por isso a regra solta de \bGuilherme\b é aplicada APENAS quando
# o user_name não é Guilherme. As demais (nome completo, MDRT, AMAN,
# ex-oficial) sempre rodam.
_PERSONA_ALWAYS = [
    (re.compile(r"\bGuilherme\s+Rodrigues\b", re.IGNORECASE), "minha assessoria"),
    (re.compile(r"\bMDRT\b",                 re.IGNORECASE),  "top 1% mundial em seguros"),
    (re.compile(r"\bAMAN\b"),                                  ""),
    (re.compile(r"\bex[- ]oficial( do Ex[ée]rcito)?\b", re.IGNORECASE), ""),
]
_PERSONA_WHEN_NOT_LEAD_NAME = [
    (re.compile(r"\bo\s+Guilherme\b", re.IGNORECASE), "minha assessoria"),
    (re.compile(r"\bGuilherme\b",     re.IGNORECASE), "minha equipe"),
]


def _sanitize_persona(text: str, user_id: str = "", user_name: str = "") -> str:
    """Última linha de defesa: troca menções ao nome real do especialista
    por linguagem genérica. Preserva o nome do lead se ele se chamar
    'Guilherme' — qualquer ocorrência nesse caso é considerada saudação."""
    cleaned = text
    leaked: list[str] = []

    for pattern, replacement in _PERSONA_ALWAYS:
        if pattern.search(cleaned):
            leaked.append(pattern.pattern)
            cleaned = pattern.sub(replacement, cleaned)

    # Aplica as regras de "Guilherme solto" só se o lead NÃO se chama Guilherme
    first_name = (user_name or "").strip().split()[0].lower() if user_name else ""
    if first_name != "guilherme":
        for pattern, replacement in _PERSONA_WHEN_NOT_LEAD_NAME:
            if pattern.search(cleaned):
                leaked.append(pattern.pattern)
                cleaned = pattern.sub(replacement, cleaned)

    if leaked:
        logger.warning(
            f"[PERSONA] Sanitização aplicada para {user_name} ({user_id}): "
            f"padrões vazados {leaked}"
        )
    # remove eventuais espaços duplicados que a sub criou
    cleaned = re.sub(r"  +", " ", cleaned).strip()
    return cleaned


def _split_into_bubbles(text: str, max_chars: int = 180) -> list[str]:
    """Quebra a resposta do Claude em balões curtos pra DM, simulando conversa real.

    Estratégia em camadas:
    1. Quebra por \\n\\n (separação que o Claude usa pra parágrafos).
    2. Cada bloco que ainda passa do limite é quebrado por sentença (. ? !).
    3. Como último recurso, junta sentenças curtas adjacentes pra não estourar
       em balões de 1 palavra.

    Garante que LINKs (URLs) e marcadores não são partidos no meio.
    """
    text = (text or "").strip()
    if not text:
        return [""]

    raw_blocks = [p.strip() for p in text.split("\n\n") if p.strip()]
    bubbles: list[str] = []
    for block in raw_blocks:
        if len(block) <= max_chars:
            bubbles.append(block)
            continue
        # Bloco maior que o limite: quebra por sentença.
        sentences = re.split(r"(?<=[.!?])\s+", block)
        current = ""
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            # Se a sentença sozinha estoura, ainda envia inteira (não vai partir URL).
            if len(s) > max_chars:
                if current:
                    bubbles.append(current.strip())
                    current = ""
                bubbles.append(s)
                continue
            # Tenta juntar com o balão corrente se ainda couber.
            tentative = f"{current} {s}".strip() if current else s
            if len(tentative) <= max_chars:
                current = tentative
            else:
                if current:
                    bubbles.append(current.strip())
                current = s
        if current:
            bubbles.append(current.strip())

    return bubbles or [text]


def _inject_tracking(text: str, subscriber_id: str) -> str:
    """Adiciona ?ref=SUBSCRIBER_ID em qualquer link Greenn gerado pelo Claude.

    Pula links que não são de checkout (ex.: agenda do Google Calendar do seguro de vida)."""
    for pid, product in PRODUCTS.items():
        if product.get("tipo") == "reuniao_closer":
            continue
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
        "estrategias_vendas_digital": "pages.eduprado.com.br/estrategias-de-vendas-no-digital",
        "blindar_mente_filho": "payfast.greenn.com.br/xg846k8",
        "seguro_vida": "calendar.google.com/calendar/u/0/appointments/schedules/AcZssZ1GjM29rB7AL",
    }
    for product_id, fragment in links.items():
        if fragment in text:
            return product_id
    return None
