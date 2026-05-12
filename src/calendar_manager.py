"""
Cria reuniões no Google Calendar. Três modos em ordem de prioridade:

  1. Apps Script relay (GOOGLE_SCRIPT_URL) — Meet automático, sem GCP
  2. Service Account (GOOGLE_SERVICE_ACCOUNT_JSON) — Meet automático, com GCP
  3. Zero-setup: gera link google.com/calendar que o prospect clica e
     Pedro recebe o convite automaticamente no email/Calendar.
     Funciona SEM nenhuma credencial configurada.
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

TIMEZONE   = "America/Sao_Paulo"
TZ         = ZoneInfo(TIMEZONE)
TZ_UTC     = ZoneInfo("UTC")
SCOPES     = ["https://www.googleapis.com/auth/calendar"]
PEDRO_EMAIL = "adm@plataformaglobalbrasilia.com.br"


class CalendarManager:
    def __init__(
        self,
        script_url: str = "",
        script_secret: str = "",
        service_account_json: str = "",
        calendar_id: str = PEDRO_EMAIL,
        booking_url: str = "",
    ):
        self.script_url    = script_url.strip()
        self.script_secret = script_secret
        self.calendar_id   = calendar_id
        self.booking_url   = booking_url
        self._service      = None

        if service_account_json and calendar_id and not self.script_url:
            try:
                from google.oauth2 import service_account
                from googleapiclient.discovery import build
                info  = json.loads(service_account_json)
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
            logger.info("[CALENDAR] Modo: link direto (zero-setup).")

    def is_configured(self) -> bool:
        return True  # sempre funciona — link direto como fallback

    def next_business_slot(self, hour: int = 10, minute: int = 0) -> datetime:
        now = datetime.now(TZ)
        dt  = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
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
    ) -> dict:
        """
        Cria reunião. Retorna sempre success=True com meet_link OU calendar_link.
        """
        if start_dt is None:
            start_dt = self.next_business_slot()
        end_dt = start_dt + timedelta(minutes=duration_minutes)

        if self.script_url:
            result = self._create_via_script(prospect_name, prospect_email, start_dt, end_dt)
            if result.get("success"):
                return result
            logger.warning("[CALENDAR] Apps Script falhou, usando link direto.")

        if self._service:
            result = self._create_via_service_account(prospect_name, prospect_email, start_dt, end_dt)
            if result.get("success"):
                return result
            logger.warning("[CALENDAR] Service Account falhou, usando link direto.")

        # ── Fallback zero-setup: link Google Calendar ─────────────────────────
        return self._create_calendar_link(prospect_name, prospect_email, start_dt, end_dt)

    # ── Apps Script relay ─────────────────────────────────────────────────────
    def _create_via_script(self, name, email, start_dt, end_dt) -> dict:
        payload = {
            "secret":      self.script_secret,
            "guestEmail":  email,
            "guestName":   name,
            "startTime":   start_dt.isoformat(),
            "endTime":     end_dt.isoformat(),
            "summary":     f"Reunião HUB Global Business — {name}",
            "description": "Reunião de descoberta sobre o HUB Global Business.\nhttps://hubglobalbusines.plataformaglobalbsb.com.br/",
        }
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                resp = client.post(self.script_url, json=payload)
                data = resp.json()
            if data.get("success"):
                logger.info(f"[CALENDAR] Apps Script OK para {email}")
                return {
                    "success": True,
                    "meet_link":            data.get("meetLink", ""),
                    "event_link":           data.get("eventLink", ""),
                    "start_time_formatted": data.get("startTimeFormatted", self._fmt(start_dt)),
                    "prospect_email":       email,
                }
            return {"success": False, "error": data.get("error")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Service Account ───────────────────────────────────────────────────────
    def _create_via_service_account(self, name, email, start_dt, end_dt) -> dict:
        rid = hashlib.md5(f"{email}-{start_dt.isoformat()}".encode()).hexdigest()[:12]
        body = {
            "summary":     f"Reunião HUB Global Business — {name}",
            "description": "https://hubglobalbusines.plataformaglobalbsb.com.br/",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE},
            "attendees": [{"email": email, "displayName": name}],
            "conferenceData": {"createRequest": {"requestId": rid, "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
            "reminders": {"useDefault": False, "overrides": [{"method": "email", "minutes": 60}, {"method": "popup", "minutes": 15}]},
        }
        try:
            ev  = self._service.events().insert(calendarId=self.calendar_id, body=body, conferenceDataVersion=1, sendUpdates="all").execute()
            eps = ev.get("conferenceData", {}).get("entryPoints", [])
            meet = next((e["uri"] for e in eps if e.get("entryPointType") == "video"), "")
            return {"success": True, "meet_link": meet, "event_link": ev.get("htmlLink", ""), "start_time_formatted": self._fmt(start_dt), "prospect_email": email}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Zero-setup: link direto ───────────────────────────────────────────────
    def _create_calendar_link(self, name, email, start_dt, end_dt) -> dict:
        """
        Gera link que o prospect clica para confirmar a reunião.
        Pedro (adm@plataformaglobalbrasilia.com.br) é adicionado como convidado
        e recebe o convite automaticamente no Gmail/Calendar.
        """
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=TZ)
        start_utc = start_dt.astimezone(TZ_UTC)
        end_utc   = end_dt.astimezone(TZ_UTC) if end_dt.tzinfo else (start_dt + timedelta(minutes=30)).astimezone(TZ_UTC)

        dates   = f"{start_utc.strftime('%Y%m%dT%H%M%SZ')}/{end_utc.strftime('%Y%m%dT%H%M%SZ')}"
        title   = quote(f"Reunião HUB Global Business — {name}")
        details = quote("Reunião de descoberta sobre o HUB Global Business.\nhttps://hubglobalbusines.plataformaglobalbsb.com.br/")

        link = (
            f"https://calendar.google.com/calendar/render?action=TEMPLATE"
            f"&text={title}&dates={dates}&details={details}"
            f"&add={PEDRO_EMAIL}&add={email}"
        )
        logger.info(f"[CALENDAR] Link direto gerado para {email}")
        return {
            "success":              True,
            "calendar_link":        link,
            "meet_link":            "",
            "event_link":           link,
            "start_time_formatted": self._fmt(start_dt),
            "prospect_email":       email,
            "mode":                 "link",
        }

    def _fmt(self, dt: datetime) -> str:
        local = dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)
        return local.strftime("%d/%m/%Y às %H:%M")
