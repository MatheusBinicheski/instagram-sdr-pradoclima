"""
Slots disponíveis para reunião com a assessoria de seguros (Guilherme).

Fonte da verdade: agenda real do Guilherme via `CalendarManager`. Este módulo
é um cache leve (60s) em cima dela — assim o bot não bate no Apps Script a
cada mensagem do lead.

Quando o lead aceita um slot, `reserve(...)` chama `CalendarManager.create_meeting(...)`,
que cria o evento na agenda do Guilherme com Google Meet automático. A
próxima consulta de `freebusy` já vê esse horário como ocupado.

Se o `CalendarManager` não tiver canal real configurado (sem `GOOGLE_SCRIPT_URL`
nem `GOOGLE_SERVICE_ACCOUNT_JSON`), caímos numa grade estática seg-sex 9-17h
— o bot continua respondendo, mas pode oferecer horário ocupado.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

BRT = timezone(timedelta(hours=-3))

DEFAULT_GRID_HOURS = [9, 10, 11, 14, 15, 16, 17]
DEFAULT_DAYS_AHEAD = 7
CACHE_TTL_SECONDS = 60


class AgendaSlots:
    def __init__(self, calendar_manager=None):
        self.calendar = calendar_manager
        self._cache: list[datetime] = []
        self._cache_at: Optional[datetime] = None
        # Reservas locais recentes — evita oferecer o mesmo slot a outro lead
        # nos segundos entre `create_meeting` e o próximo refresh do cache.
        self._just_reserved: dict[str, dict] = {}  # iso → {user_id, meet_link, event_link, reserved_at}

    # ── Cache / consulta ─────────────────────────────────────────────────

    def _refresh_if_stale(self) -> None:
        now = datetime.now(BRT)
        fresh = self._cache_at and (now - self._cache_at).total_seconds() < CACHE_TTL_SECONDS
        if fresh:
            return

        free: Optional[list[datetime]] = None
        if self.calendar is not None:
            try:
                free = self.calendar.list_free_slots(
                    days_ahead=DEFAULT_DAYS_AHEAD,
                    grid_hours=DEFAULT_GRID_HOURS,
                )
            except Exception as e:
                logger.warning(f"[AGENDA] list_free_slots erro: {e}")
                free = None

        if free is None:
            # Sem canal real → grade estática. Não é ideal, mas o bot continua de pé.
            logger.warning("[AGENDA] Sem canal real — usando grade estática seg-sex 9-17h.")
            free = _static_grid(DEFAULT_DAYS_AHEAD, DEFAULT_GRID_HOURS)

        # Remove slots reservados localmente nos últimos 5 min (anti race condition).
        cutoff = now - timedelta(minutes=5)
        stale_keys = [iso for iso, meta in self._just_reserved.items()
                      if meta.get("reserved_at") and meta["reserved_at"] < cutoff]
        for k in stale_keys:
            self._just_reserved.pop(k, None)

        recent_iso = set(self._just_reserved.keys())
        free_filtered = [dt for dt in free if dt.isoformat() not in recent_iso]
        free_filtered.sort()

        self._cache = free_filtered
        self._cache_at = now
        logger.info(f"[AGENDA] Cache atualizado — {len(free_filtered)} slots livres.")

    def list_available(
        self,
        days_ahead: int = DEFAULT_DAYS_AHEAD,
        period: Optional[str] = None,
        weekday: Optional[int] = None,
    ) -> list[dict]:
        """Slots livres na agenda real, filtrados por período/dia se passado.

        period: 'manha' (9h-12h) | 'tarde' (14h-18h) | None
        weekday: 0=seg .. 4=sex | None
        """
        self._refresh_if_stale()
        now = datetime.now(BRT)
        cutoff = now + timedelta(days=days_ahead)
        out: list[dict] = []
        for dt in self._cache:
            if dt <= now or dt > cutoff:
                continue
            if period == "manha" and not (9 <= dt.hour < 12):
                continue
            if period == "tarde" and not (14 <= dt.hour < 18):
                continue
            if weekday is not None and dt.weekday() != weekday:
                continue
            out.append({"iso": dt.isoformat(), "dt": dt, "status": "available"})
        out.sort(key=lambda s: s["iso"])
        return out

    def format_for_prompt(self, days_ahead: int = DEFAULT_DAYS_AHEAD, limit: int = 30) -> str:
        avail = self.list_available(days_ahead=days_ahead)
        if not avail:
            return "AGENDA: nenhum horário disponível nos próximos dias."
        weekdays = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
        lines = []
        for s in avail[:limit]:
            dt = s["dt"]
            label = f"{weekdays[dt.weekday()]} {dt.strftime('%d/%m')} às {dt.strftime('%Hh%M')}"
            lines.append(f"  - ISO={dt.isoformat()} | {label}")
        return (
            "AGENDA DA MINHA ASSESSORIA — slots DISPONÍVEIS (use APENAS estes):\n"
            + "\n".join(lines)
        )

    # ── Reserva ──────────────────────────────────────────────────────────

    def reserve(
        self,
        iso: str,
        subscriber_id: str,
        user_name: str = "Lead",
        user_email: str = "",
    ) -> Optional[dict]:
        """Cria o evento real na agenda do Guilherme via CalendarManager.

        Retorna {iso, meet_link, event_link, event_id} em caso de sucesso,
        ou None se o slot for inválido / a criação falhar.
        """
        dt = _parse_iso(iso)
        if not dt:
            logger.warning(f"[AGENDA] ISO inválido: {iso}")
            return None
        canon = dt.isoformat()

        if canon in self._just_reserved:
            logger.warning(f"[AGENDA] Slot {canon} já reservado nessa janela — bloqueando duplicata.")
            return None

        # Confirma no cache fresco que o slot ainda está livre.
        self._refresh_if_stale()
        free_isos = {d.isoformat() for d in self._cache}
        if canon not in free_isos:
            logger.warning(f"[AGENDA] Slot {canon} não está mais disponível na agenda real.")
            return None

        if self.calendar is None:
            logger.error("[AGENDA] CalendarManager não configurado — impossível criar evento.")
            return None

        try:
            result = self.calendar.create_meeting(
                prospect_name=user_name,
                prospect_email=user_email,
                start_dt=dt,
                duration_minutes=30,
            )
        except Exception as e:
            logger.error(f"[AGENDA] create_meeting exceção: {e}")
            return None

        if not result.get("success"):
            logger.error(f"[AGENDA] create_meeting falhou: {result.get('error')}")
            return None

        mode = result.get("mode", "")
        if mode == "link_fallback":
            # Sem canal real — não bloqueia o slot na agenda do Guilherme.
            logger.warning(
                f"[AGENDA] Slot {canon} 'reservado' só localmente — "
                f"sem Apps Script/SA, evento NÃO foi criado no Calendar."
            )

        self._just_reserved[canon] = {
            "user_id": subscriber_id,
            "meet_link": result.get("meet_link", ""),
            "event_link": result.get("event_link", ""),
            "event_id": result.get("event_id", ""),
            "reserved_at": datetime.now(BRT),
        }
        # Invalida cache pra forçar refresh na próxima chamada (o evento já está na agenda).
        self._cache_at = None

        return {
            "iso": canon,
            "meet_link": result.get("meet_link", ""),
            "event_link": result.get("event_link", ""),
            "event_id": result.get("event_id", ""),
            "start_time_formatted": result.get("start_time_formatted", ""),
            "mode": mode,
        }


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BRT)
    return dt.astimezone(BRT)


def _static_grid(days_ahead: int, grid_hours: list[int]) -> list[datetime]:
    """Grade fallback usada apenas quando não há canal real pra agenda."""
    now = datetime.now(BRT)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    out: list[datetime] = []
    for offset in range(1, days_ahead + 1):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for h in grid_hours:
            slot = day.replace(hour=h)
            if slot > now:
                out.append(slot)
    return out
