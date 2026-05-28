"""
Integração com a agenda do Guilherme (closer de seguros — grsouza93ip@gmail.com).

Três modos em ordem de prioridade:

  1. Apps Script relay (GOOGLE_SCRIPT_URL) — Meet automático, sem GCP.
     O script em apps_script_calendar.gs roda como o próprio Guilherme,
     então enxerga a agenda real (incluindo bookings da Appointment Schedule)
     e cria eventos no calendário dele.
  2. Service Account (GOOGLE_SERVICE_ACCOUNT_JSON) — Meet automático, com GCP.
  3. Link direto Google Calendar (zero-setup) — fallback de último caso, só
     funciona se algum dos dois acima falhar. NÃO consulta agenda real.
"""

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

TIMEZONE = "America/Sao_Paulo"
TZ = ZoneInfo(TIMEZONE)
TZ_UTC = ZoneInfo("UTC")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Closer de seguros — agenda real consultada pelo bot.
GUILHERME_EMAIL = "grsouza93ip@gmail.com"


class CalendarManager:
    def __init__(
        self,
        script_url: str = "",
        script_secret: str = "",
        service_account_json: str = "",
        calendar_id: str = GUILHERME_EMAIL,
        booking_url: str = "",
    ):
        self.script_url = script_url.strip()
        self.script_secret = script_secret
        self.calendar_id = calendar_id or GUILHERME_EMAIL
        self.booking_url = booking_url
        self._service = None

        if service_account_json and calendar_id and not self.script_url:
            try:
                from google.oauth2 import service_account
                from googleapiclient.discovery import build
                info = json.loads(service_account_json)
                creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
                self._service = build("calendar", "v3", credentials=creds)
                logger.info("[CALENDAR] Modo: Service Account.")
            except Exception as e:
                logger.error(f"[CALENDAR] Falha Service Account: {e}")

        if self.script_url:
            logger.info("[CALENDAR] Modo: Apps Script relay.")
        elif self._service:
            logger.info("[CALENDAR] Modo: Service Account.")
        else:
            logger.info("[CALENDAR] Modo: link direto (zero-setup). Agenda real NÃO será consultada.")

    def has_real_calendar(self) -> bool:
        """True quando dá pra consultar/criar eventos na agenda de verdade."""
        return bool(self.script_url) or bool(self._service)

    def is_configured(self) -> bool:
        return True  # sempre devolve algo — link direto como último fallback

    # ── Consulta de ocupação ─────────────────────────────────────────────────
    def list_busy(self, start_dt: datetime, end_dt: datetime) -> Optional[list[dict]]:
        """Retorna lista de intervalos ocupados [{'start': iso, 'end': iso, 'allDay': bool}].

        Devolve None se não há canal pra consultar a agenda real (sem Apps Script
        nem Service Account). Devolve [] quando a janela está totalmente livre.
        """
        if self.script_url:
            return self._busy_via_script(start_dt, end_dt)
        if self._service:
            return self._busy_via_service_account(start_dt, end_dt)
        return None

    def list_free_slots(
        self,
        days_ahead: int = 7,
        grid_hours: Optional[list[int]] = None,
        skip_weekend: bool = True,
    ) -> Optional[list[datetime]]:
        """Devolve datetimes livres na janela `days_ahead`, dentro da grade
        seg-sex `grid_hours` (slots de 1h). Filtra contra `list_busy`.

        Retorna None quando não há canal pra consultar a agenda (chamador
        decide se quer cair em fallback de grade fixa)."""
        if grid_hours is None:
            grid_hours = [9, 10, 11, 14, 15, 16, 17]

        now = datetime.now(TZ)
        start_window = now.replace(minute=0, second=0, microsecond=0)
        end_window = (now + timedelta(days=days_ahead + 1)).replace(hour=23, minute=59, second=59, microsecond=0)

        busy = self.list_busy(start_window, end_window)
        if busy is None:
            return None

        # Normaliza intervalos ocupados pra (start_aware, end_aware).
        busy_intervals: list[tuple[datetime, datetime]] = []
        for b in busy:
            try:
                bs = _parse_dt(b.get("start"))
                be = _parse_dt(b.get("end"))
                if bs and be:
                    busy_intervals.append((bs, be))
            except Exception:
                continue

        # Gera a grade candidata e filtra colisões.
        free: list[datetime] = []
        cursor = start_window.replace(hour=0)
        for offset in range(1, days_ahead + 1):
            day = (cursor + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
            if skip_weekend and day.weekday() >= 5:
                continue
            for h in grid_hours:
                slot_start = day.replace(hour=h)
                if slot_start <= now:
                    continue
                slot_end = slot_start + timedelta(hours=1)
                if not _overlaps_any(slot_start, slot_end, busy_intervals):
                    free.append(slot_start)
        return free

    def _busy_via_script(self, start_dt: datetime, end_dt: datetime) -> Optional[list[dict]]:
        payload = {
            "secret": self.script_secret,
            "op": "freebusy",
            "calendarId": self.calendar_id,
            "startISO": start_dt.isoformat(),
            "endISO": end_dt.isoformat(),
        }
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                resp = client.post(self.script_url, json=payload)
                data = resp.json()
            if data.get("success"):
                return data.get("busy", [])
            logger.warning(f"[CALENDAR] freebusy via script falhou: {data.get('error')}")
            return None
        except Exception as e:
            logger.warning(f"[CALENDAR] freebusy via script erro: {e}")
            return None

    def _busy_via_service_account(self, start_dt: datetime, end_dt: datetime) -> Optional[list[dict]]:
        try:
            body = {
                "timeMin": start_dt.astimezone(TZ_UTC).isoformat().replace("+00:00", "Z"),
                "timeMax": end_dt.astimezone(TZ_UTC).isoformat().replace("+00:00", "Z"),
                "items": [{"id": self.calendar_id}],
            }
            res = self._service.freebusy().query(body=body).execute()
            cal = res.get("calendars", {}).get(self.calendar_id, {})
            return [{"start": b["start"], "end": b["end"]} for b in cal.get("busy", [])]
        except Exception as e:
            logger.warning(f"[CALENDAR] freebusy via SA erro: {e}")
            return None

    # ── Criação de evento ────────────────────────────────────────────────────
    def next_business_slot(self, hour: int = 10, minute: int = 0) -> datetime:
        now = datetime.now(TZ)
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        while dt.weekday() >= 5:
            dt += timedelta(days=1)
        return dt

    def create_meeting(
        self,
        prospect_name: str,
        prospect_email: str,
        start_dt: Optional[datetime] = None,
        duration_minutes: int = 30,
        whatsapp: str = "",
        qualification: str = "",
        subscriber_id: str = "",
    ) -> dict:
        """Cria reunião. Retorna success=True com meet_link/event_link.

        `qualification` é o resumo do bot pra descrição do evento — o closer
        chega na reunião sabendo o contexto do lead.
        """
        if start_dt is None:
            start_dt = self.next_business_slot()
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=TZ)
        end_dt = start_dt + timedelta(minutes=duration_minutes)

        description = _build_event_description(
            name=prospect_name,
            email=prospect_email,
            whatsapp=whatsapp,
            subscriber_id=subscriber_id,
            qualification=qualification,
        )

        if self.script_url:
            result = self._create_via_script(prospect_name, prospect_email, start_dt, end_dt, description)
            if result.get("success"):
                return result
            logger.warning(f"[CALENDAR] Apps Script falhou: {result.get('error')}")

        if self._service:
            result = self._create_via_service_account(prospect_name, prospect_email, start_dt, end_dt, description)
            if result.get("success"):
                return result
            logger.warning(f"[CALENDAR] Service Account falhou: {result.get('error')}")

        return self._create_calendar_link(prospect_name, prospect_email, start_dt, end_dt)

    def cancel_meeting(self, event_id: str, notify_attendees: bool = True) -> dict:
        """Cancela evento na agenda do Guilherme."""
        if not event_id:
            return {"success": False, "error": "event_id required"}
        if self.script_url:
            return self._cancel_via_script(event_id, notify_attendees)
        if self._service:
            return self._cancel_via_service_account(event_id, notify_attendees)
        return {"success": False, "error": "no real calendar channel — cannot cancel"}

    def _cancel_via_script(self, event_id: str, notify_attendees: bool) -> dict:
        payload = {
            "secret": self.script_secret,
            "op": "cancelEvent",
            "calendarId": self.calendar_id,
            "eventId": event_id,
            "notifyAttendees": bool(notify_attendees),
        }
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                resp = client.post(self.script_url, json=payload)
                data = resp.json()
            return data
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _cancel_via_service_account(self, event_id: str, notify_attendees: bool) -> dict:
        try:
            self._service.events().delete(
                calendarId=self.calendar_id,
                eventId=event_id,
                sendUpdates="all" if notify_attendees else "none",
            ).execute()
            return {"success": True, "eventId": event_id, "cancelled": True}
        except Exception as e:
            return {"success": False, "error": str(e), "eventId": event_id}

    def _create_via_script(self, name, email, start_dt, end_dt, description) -> dict:
        payload = {
            "secret": self.script_secret,
            "op": "createEvent",
            "calendarId": self.calendar_id,
            "guestEmail": email,
            "guestName": name,
            "startISO": start_dt.isoformat(),
            "endISO": end_dt.isoformat(),
            "summary": f"Reunião de Seguro de Vida com {name}",
            "description": description,
        }
        try:
            with httpx.Client(timeout=25, follow_redirects=True) as client:
                resp = client.post(self.script_url, json=payload)
                data = resp.json()
            if data.get("success"):
                logger.info(f"[CALENDAR] Apps Script OK — evento {data.get('eventId')}")
                return {
                    "success": True,
                    "event_id": data.get("eventId", ""),
                    "meet_link": data.get("meetLink", ""),
                    "event_link": data.get("eventLink", ""),
                    "start_time_formatted": data.get("startTimeFormatted", self._fmt(start_dt)),
                    "prospect_email": email,
                    "mode": "apps_script",
                }
            return {"success": False, "error": data.get("error")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _create_via_service_account(self, name, email, start_dt, end_dt, description) -> dict:
        rid = hashlib.md5(f"{email}-{start_dt.isoformat()}".encode()).hexdigest()[:12]
        body = {
            "summary": f"Reunião de Seguro de Vida com {name}",
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
            "conferenceData": {"createRequest": {"requestId": rid, "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
            "reminders": {"useDefault": False, "overrides": [{"method": "email", "minutes": 60}, {"method": "popup", "minutes": 15}]},
        }
        if email:
            body["attendees"] = [{"email": email, "displayName": name}]
        try:
            ev = self._service.events().insert(
                calendarId=self.calendar_id,
                body=body,
                conferenceDataVersion=1,
                sendUpdates="all" if email else "none",
            ).execute()
            eps = ev.get("conferenceData", {}).get("entryPoints", [])
            meet = next((e["uri"] for e in eps if e.get("entryPointType") == "video"), "")
            return {
                "success": True,
                "event_id": ev.get("id", ""),
                "meet_link": meet,
                "event_link": ev.get("htmlLink", ""),
                "start_time_formatted": self._fmt(start_dt),
                "prospect_email": email,
                "mode": "service_account",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _create_calendar_link(self, name, email, start_dt, end_dt) -> dict:
        """Fallback: link Google Calendar pré-preenchido. NÃO cria evento real."""
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=TZ)
        start_utc = start_dt.astimezone(TZ_UTC)
        end_utc = end_dt.astimezone(TZ_UTC) if end_dt.tzinfo else (start_dt + timedelta(minutes=30)).astimezone(TZ_UTC)

        dates = f"{start_utc.strftime('%Y%m%dT%H%M%SZ')}/{end_utc.strftime('%Y%m%dT%H%M%SZ')}"
        title = quote(f"Reunião de Seguro de Vida com {name}")
        details = quote("Reunião de descoberta com a assessoria do Prado.")

        link = (
            f"https://calendar.google.com/calendar/render?action=TEMPLATE"
            f"&text={title}&dates={dates}&details={details}"
            f"&add={GUILHERME_EMAIL}"
        )
        if email:
            link += f"&add={email}"
        logger.warning(f"[CALENDAR] Fallback link direto gerado — agenda real NÃO foi tocada.")
        return {
            "success": True,
            "event_id": "",
            "calendar_link": link,
            "meet_link": "",
            "event_link": link,
            "start_time_formatted": self._fmt(start_dt),
            "prospect_email": email,
            "mode": "link_fallback",
        }

    def _fmt(self, dt: datetime) -> str:
        local = dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)
        return local.strftime("%d/%m/%Y às %H:%M")


# ── helpers ──────────────────────────────────────────────────────────────────
def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _overlaps_any(start: datetime, end: datetime, intervals: list[tuple[datetime, datetime]]) -> bool:
    for bs, be in intervals:
        if start < be and bs < end:
            return True
    return False


def _build_event_description(
    name: str,
    email: str = "",
    whatsapp: str = "",
    subscriber_id: str = "",
    qualification: str = "",
) -> str:
    """Monta a descrição do evento com tudo que o closer precisa pra chegar pronto."""
    lines = ["Reunião agendada via bot SDR do @pradoclima.", ""]
    lines.append("CONTATO DO LEAD")
    lines.append(f"  Nome:     {name or '(não informado)'}")
    lines.append(f"  Email:    {email or '(não informado)'}")
    lines.append(f"  WhatsApp: {whatsapp or '(não informado)'}")
    if subscriber_id:
        lines.append(f"  Instagram (ManyChat ID): {subscriber_id}")
    lines.append("")
    lines.append("PRÉ-QUALIFICAÇÃO DO BOT")
    if qualification:
        lines.append(qualification.strip())
    else:
        lines.append("(sem síntese — explore na call)")
    lines.append("")
    lines.append("---")
    lines.append("Reunião de descoberta sobre seguro de vida / sucessão patrimonial.")
    return "\n".join(lines)
