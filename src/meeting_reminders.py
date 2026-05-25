"""
Lembretes de reunião com o closer (Guilherme).

Cadência:
  • 2 DIAS ANTES — pede confirmação ("esse horário está reservado para você").
  • DIA DA REUNIÃO, 09:00 BRT — saudação + horário + link.
  • 1 HORA ANTES — lembrete persuasivo com o link.
  • Se NÃO confirmou e é dia da reunião — até 3 mensagens de pressão
    espaçadas, com escassez ("sua vaga vai para quem está na fila").

A confirmação é detectada quando o lead responde com afirmativas após
o lembrete de 2 dias (ver `detect_confirmation_in_message`), ou via
endpoint POST /meeting/{user_id}/confirm.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .conversation_manager import ConversationManager
from .sales_agent import SalesAgent

logger = logging.getLogger(__name__)

# Brasília — UTC-3 (sem DST desde 2019).
BRT = timezone(timedelta(hours=-3))

# Confirmação positiva — palavras isoladas em mensagens curtas.
_CONFIRM_PATTERNS = [
    r"\bconfirm[ao]\b", r"\bconfirmad[ao]\b", r"\bconfirmei\b",
    r"\bestarei\b", r"\best[oa]u confirmad[ao]\b", r"\bvou estar\b",
    r"\bsim\b", r"\bok\b", r"\bbeleza\b", r"\btopo\b", r"\bfechado\b",
    r"\bcombinado\b", r"\bt[oa] aqui\b", r"\bt[oa] dentro\b", r"\bvou sim\b",
]
_CONFIRM_RE = re.compile("|".join(_CONFIRM_PATTERNS), re.IGNORECASE)

# Cancelamento — não confunde com confirmação ambígua.
_CANCEL_PATTERNS = [
    r"\bcancel", r"\bn[aã]o vou\b", r"\bn[aã]o consigo\b",
    r"\bdesmarc", r"\bremarc", r"\bvou faltar\b", r"\bn[aã]o estarei\b",
]
_CANCEL_RE = re.compile("|".join(_CANCEL_PATTERNS), re.IGNORECASE)


def detect_confirmation_in_message(text: str) -> Optional[str]:
    """Retorna 'confirmed', 'cancelled' ou None. Cancel vence empate."""
    if not text:
        return None
    if _CANCEL_RE.search(text):
        return "cancelled"
    if _CONFIRM_RE.search(text):
        return "confirmed"
    return None


def _now_brt() -> datetime:
    return datetime.now(BRT)


def _parse_meeting_time(value: str) -> Optional[datetime]:
    """Aceita ISO 8601. Se vier sem tz, assume BRT."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BRT)
    return dt.astimezone(BRT)


def _format_meeting_time(dt: datetime) -> str:
    """Formato em pt-BR: 'sexta, 27/05 às 15h00'."""
    weekdays = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    return f"{weekdays[dt.weekday()]}, {dt.strftime('%d/%m')} às {dt.strftime('%Hh%M')}"


class MeetingReminderService:
    def __init__(
        self,
        agent: SalesAgent,
        conv_manager: ConversationManager,
        reactivation_svc,  # ReactivationService — usado só para _send_manychat_dm
    ):
        self.agent = agent
        self.conv_manager = conv_manager
        self.reactivation_svc = reactivation_svc

    # ── API pública ──────────────────────────────────────────────────────

    def schedule(self, user_id: str, meeting_time_iso: str, meet_link: str = "") -> bool:
        """Registra a reunião. `meeting_time_iso` em ISO 8601 (com ou sem tz)."""
        dt = _parse_meeting_time(meeting_time_iso)
        if not dt:
            logger.warning(f"[MEETING] meeting_time inválido: {meeting_time_iso}")
            return False
        self.conv_manager.mark_meeting_scheduled(user_id, meet_link, dt.isoformat())
        logger.info(f"[MEETING] Agendado {user_id} para {dt.isoformat()}")
        return True

    def mark_confirmed(self, user_id: str) -> bool:
        return self.conv_manager.mark_meeting_confirmed(user_id)

    def mark_cancelled(self, user_id: str) -> bool:
        return self.conv_manager.mark_meeting_cancelled(user_id)

    def handle_user_reply(self, user_id: str, message: str) -> Optional[str]:
        """
        Chamado pelo webhook de DM. Se a mensagem confirma/cancela a reunião,
        atualiza o estado e retorna 'confirmed' ou 'cancelled'. None caso contrário.

        Só age se a reunião está agendada e o lembrete de 2 dias já foi enviado
        (evita falso positivo em conversas comuns que digam "ok").
        """
        conv = self.conv_manager.conversations.get(user_id)
        if not conv or not conv.get("meeting_scheduled"):
            return None
        reminders = conv.get("meeting_reminders", {})
        if not reminders.get("2d_sent_at"):
            return None
        if conv.get("meeting_confirmed") or conv.get("meeting_status") in ("cancelled", "completed", "no_show"):
            return None

        verdict = detect_confirmation_in_message(message)
        if verdict == "confirmed":
            self.mark_confirmed(user_id)
            logger.info(f"[MEETING] {user_id} confirmou pela conversa: '{message[:60]}'")
        elif verdict == "cancelled":
            self.mark_cancelled(user_id)
            logger.info(f"[MEETING] {user_id} cancelou pela conversa: '{message[:60]}'")
        return verdict

    # ── Loop principal ───────────────────────────────────────────────────

    def run_tick(self) -> dict:
        """
        Roda os 4 tipos de lembrete para todas as reuniões agendadas.
        Idempotente — só envia se a janela bater e o lembrete específico
        ainda não foi enviado.
        """
        now = _now_brt()
        results = {"2d": 0, "morning": 0, "1h": 0, "pressure": 0, "completed": 0}

        for user_id, conv in list(self.conv_manager.conversations.items()):
            if not conv.get("meeting_scheduled"):
                continue
            if conv.get("meeting_status") in ("cancelled", "completed", "no_show"):
                continue

            meeting_dt = _parse_meeting_time(conv.get("meeting_time", ""))
            if not meeting_dt:
                continue

            delta = meeting_dt - now
            hours_until = delta.total_seconds() / 3600
            reminders = conv.setdefault("meeting_reminders", {})

            # ── Reunião já passou: marca como completed (best-effort, sem tracking real) ──
            if hours_until < -0.5:
                self.conv_manager.mark_meeting_completed(user_id)
                results["completed"] += 1
                continue

            user_name = conv.get("user_name", "amigo")
            meet_link = conv.get("meeting_meet_link", "")

            # ── 2 dias antes: janela 47h45–48h15 ──
            if 47.75 <= hours_until <= 48.25 and not reminders.get("2d_sent_at"):
                msg = self._compose_2day(user_name, meeting_dt)
                if self._send(user_id, msg):
                    self.conv_manager.mark_meeting_reminder_sent(user_id, "2d_sent_at")
                    results["2d"] += 1
                continue

            # ── Dia da reunião, 09:00 BRT (janela 08:55–09:25) ──
            is_meeting_day = now.date() == meeting_dt.date()
            if is_meeting_day and 8 <= now.hour <= 9 and not reminders.get("morning_sent_at"):
                if (now.hour == 8 and now.minute >= 55) or (now.hour == 9 and now.minute <= 25):
                    msg = self._compose_morning(user_name, meeting_dt, meet_link, conv.get("meeting_confirmed", False))
                    if self._send(user_id, msg):
                        self.conv_manager.mark_meeting_reminder_sent(user_id, "morning_sent_at")
                        results["morning"] += 1
                    continue

            # ── 1h antes: janela 0h55–1h15 ──
            if 0.92 <= hours_until <= 1.25 and not reminders.get("1h_sent_at"):
                msg = self.agent.generate_meeting_reminder_1h(user_name, meeting_dt.strftime("%Hh%M"), meet_link)
                if self._send(user_id, msg):
                    self.conv_manager.mark_meeting_reminder_sent(user_id, "1h_sent_at")
                    results["1h"] += 1
                continue

            # ── Não confirmou + é o dia: pressão até 3x, espaçada 3h+ ──
            if is_meeting_day and not conv.get("meeting_confirmed"):
                attempt = reminders.get("pressure_count", 0)
                if attempt >= 3:
                    continue
                last_pressure = reminders.get("last_pressure_at")
                if last_pressure:
                    last_dt = datetime.fromisoformat(last_pressure)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=BRT)
                    if (now - last_dt) < timedelta(hours=3):
                        continue
                # Pelo menos 10h antes da reunião para não atropelar o 1h-reminder
                if hours_until < 1.5:
                    continue
                msg = self.agent.generate_meeting_pressure(
                    user_name, meeting_dt.strftime("%Hh%M"), meet_link, attempt=attempt + 1
                )
                if self._send(user_id, msg):
                    self.conv_manager.mark_meeting_pressure_sent(user_id)
                    results["pressure"] += 1

        return results

    # ── Composers ────────────────────────────────────────────────────────

    def _compose_2day(self, name: str, meeting_dt: datetime) -> str:
        """Script base — roteiro do dono: 'esse horário está reservado pra você'."""
        when = _format_meeting_time(meeting_dt)
        return (
            f"Bom dia, {name}. Esse horário ({when}) tá reservado pra você na agenda do Guilherme.\n\n"
            f"Notei que você ainda não confirmou. Me confirma aqui que vai estar? "
            f"É só 30 min e você sai com clareza do que faz sentido pra blindar renda, família e patrimônio."
        )

    def _compose_morning(self, name: str, meeting_dt: datetime, meet_link: str, confirmed: bool) -> str:
        """Manhã do dia da reunião. Se confirmou, tom de relembrar; se não, ainda pede confirmação."""
        hour = meeting_dt.strftime("%Hh%M")
        link_line = f"\n\nLink da reunião: {meet_link}" if meet_link else ""
        if confirmed:
            return (
                f"Bom dia, {name}, tudo bem? Passando aqui pra relembrar sobre nossa reunião "
                f"que vai acontecer às {hour} de hoje, conforme combinamos.{link_line}\n\n"
                f"Aguardo você!"
            )
        return (
            f"Bom dia, {name}. Nossa reunião com o Guilherme tá marcada pra hoje, {hour}.{link_line}\n\n"
            f"Você ainda não confirmou. Me dá um ok aqui que você vai estar?"
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _send(self, user_id: str, text: str) -> bool:
        ok = self.reactivation_svc._send_manychat_dm(user_id, text)
        if ok:
            self.conv_manager.add_message(user_id, "assistant", text)
        return ok
