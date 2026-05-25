"""
Gerenciador de slots disponíveis para reunião com a assessoria.

O bot não acessa o Google Calendar real (Appointment Schedules são privadas
e não expostas via API pública sem OAuth). Em vez disso, mantemos uma grade
local de slots em `data/available_slots.json` — o bot oferece esses slots
ao lead, e quando o lead escolhe, reservamos internamente e disparamos
`meeting_svc.schedule(...)` (que aciona toda a cadência de lembretes).

Persistência: data/available_slots.json
Estrutura:
  {
    "slots": [
      {"iso": "2026-05-27T10:00:00-03:00",
       "status": "available" | "reserved" | "released",
       "reserved_by": null | "subscriber_id",
       "reserved_at": null | iso_ts}
    ]
  }
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

BRT = timezone(timedelta(hours=-3))
SLOTS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "available_slots.json")

# Grade default: seg-sex, 09h-11h e 14h-17h (inteiras), próximos 14 dias.
DEFAULT_GRID_HOURS = [9, 10, 11, 14, 15, 16, 17]
DEFAULT_GRID_DAYS_AHEAD = 14


class AgendaSlots:
    def __init__(self):
        self.slots: list[dict] = []
        self._load()

    # ── Persistência ─────────────────────────────────────────────────────

    def _load(self):
        os.makedirs(os.path.dirname(SLOTS_FILE), exist_ok=True)
        if os.path.exists(SLOTS_FILE):
            try:
                with open(SLOTS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.slots = data.get("slots", [])
                logger.info(f"[AGENDA] Carregados {len(self.slots)} slots.")
                return
            except Exception as e:
                logger.error(f"[AGENDA] Erro ao carregar: {e}")
        # Sem arquivo → popula grade default.
        self.ensure_default_grid()

    def _save(self):
        try:
            with open(SLOTS_FILE, "w", encoding="utf-8") as f:
                json.dump({"slots": self.slots}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[AGENDA] Erro ao salvar: {e}")

    # ── Construção da grade ──────────────────────────────────────────────

    def ensure_default_grid(self, days_ahead: int = DEFAULT_GRID_DAYS_AHEAD):
        """Popula slots seg-sex 9h-17h pelos próximos N dias se a agenda estiver vazia."""
        existing_isos = {s["iso"] for s in self.slots}
        added = 0
        today = datetime.now(BRT).replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(1, days_ahead + 1):  # começa AMANHÃ
            day = today + timedelta(days=offset)
            if day.weekday() >= 5:  # 5=sáb, 6=dom
                continue
            for h in DEFAULT_GRID_HOURS:
                slot_dt = day.replace(hour=h)
                iso = slot_dt.isoformat()
                if iso in existing_isos:
                    continue
                self.slots.append({
                    "iso": iso,
                    "status": "available",
                    "reserved_by": None,
                    "reserved_at": None,
                })
                added += 1
        if added > 0:
            self.slots.sort(key=lambda s: s["iso"])
            self._save()
            logger.info(f"[AGENDA] {added} slots default adicionados.")
        return added

    def add_slots(self, isos: list[str]) -> int:
        existing = {s["iso"] for s in self.slots}
        added = 0
        for iso in isos:
            dt = _parse_iso(iso)
            if not dt:
                continue
            canon = dt.isoformat()
            if canon in existing:
                continue
            self.slots.append({
                "iso": canon, "status": "available",
                "reserved_by": None, "reserved_at": None,
            })
            existing.add(canon)
            added += 1
        if added:
            self.slots.sort(key=lambda s: s["iso"])
            self._save()
        return added

    def remove_slot(self, iso: str) -> bool:
        dt = _parse_iso(iso)
        if not dt:
            return False
        canon = dt.isoformat()
        before = len(self.slots)
        self.slots = [s for s in self.slots if s["iso"] != canon]
        if len(self.slots) < before:
            self._save()
            return True
        return False

    # ── Consulta / oferta ────────────────────────────────────────────────

    def list_available(
        self,
        days_ahead: int = 14,
        period: Optional[str] = None,
        weekday: Optional[int] = None,
    ) -> list[dict]:
        """Slots disponíveis nas próximas N dias, filtrando por período/dia se passado.

        period: 'manha' (9h-12h) | 'tarde' (14h-18h) | None
        weekday: 0=seg .. 4=sex | None
        """
        now = datetime.now(BRT)
        cutoff = now + timedelta(days=days_ahead)
        out = []
        for s in self.slots:
            if s["status"] != "available":
                continue
            dt = _parse_iso(s["iso"])
            if not dt:
                continue
            if dt <= now or dt > cutoff:
                continue
            if period == "manha" and not (9 <= dt.hour < 12):
                continue
            if period == "tarde" and not (14 <= dt.hour < 18):
                continue
            if weekday is not None and dt.weekday() != weekday:
                continue
            out.append({**s, "dt": dt})
        out.sort(key=lambda s: s["iso"])
        return out

    def format_for_prompt(self, days_ahead: int = 10, limit: int = 30) -> str:
        """Renderiza a lista de slots para o Claude ver dentro do system context."""
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

    def reserve(self, iso: str, subscriber_id: str) -> Optional[dict]:
        """Marca o slot como reservado. Retorna o slot reservado ou None se indisponível."""
        dt = _parse_iso(iso)
        if not dt:
            return None
        canon = dt.isoformat()
        for s in self.slots:
            if s["iso"] != canon:
                continue
            if s["status"] != "available":
                return None
            s["status"] = "reserved"
            s["reserved_by"] = subscriber_id
            s["reserved_at"] = datetime.now(BRT).isoformat()
            self._save()
            return s
        return None

    def release(self, iso: str) -> bool:
        dt = _parse_iso(iso)
        if not dt:
            return False
        canon = dt.isoformat()
        for s in self.slots:
            if s["iso"] != canon:
                continue
            s["status"] = "released"
            self._save()
            return True
        return False


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
