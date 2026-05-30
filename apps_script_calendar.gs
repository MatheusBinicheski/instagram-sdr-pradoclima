/**
 * Apps Script relay — agenda do Guilherme (closer de seguros).
 *
 * Roda como grsouza93ip@gmail.com. Endpoints (op):
 *
 *   freebusy     → devolve intervalos ocupados em uma janela
 *   createEvent  → cria evento com Google Meet automático
 *   cancelEvent  → cancela evento por eventId (lead remarcou ou faltou)
 *
 * --- DEPLOY (passo a passo) ---
 *
 *  1) Entre em https://script.google.com (LOGADO como grsouza93ip@gmail.com).
 *  2) Novo projeto → cole este arquivo inteiro em Code.gs.
 *  3) Em "Services" (ícone +) ative: "Google Calendar API" (Calendar v3).
 *  4) Edite SHARED_SECRET abaixo — qualquer string aleatória longa.
 *  5) Deploy → New deployment:
 *        Type:           Web app
 *        Execute as:     Me (grsouza93ip@gmail.com)
 *        Who has access: Anyone
 *  6) Copie a "Web app URL" → cole no Railway como GOOGLE_SCRIPT_URL.
 *  7) Cole o MESMO valor de SHARED_SECRET no Railway como GOOGLE_SCRIPT_SECRET.
 *
 *  Pronto. O bot vai consultar a agenda real e criar eventos com Meet.
 */

const SHARED_SECRET = 'TROQUE_ESTE_SEGREDO_E_REPLIQUE_NO_RAILWAY';
const DEFAULT_CALENDAR = 'grsouza93ip@gmail.com';

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    if (body.secret !== SHARED_SECRET) {
      return _json({ success: false, error: 'unauthorized' });
    }
    const op = body.op || 'createEvent';
    if (op === 'freebusy')    return _json(handleFreebusy(body));
    if (op === 'createEvent') return _json(handleCreateEvent(body));
    if (op === 'cancelEvent') return _json(handleCancelEvent(body));
    if (op === 'ping')        return _json({ success: true, runningAs: Session.getActiveUser().getEmail() });
    return _json({ success: false, error: 'unknown op: ' + op });
  } catch (err) {
    return _json({ success: false, error: String(err) });
  }
}

function doGet(e) {
  // Permite checagem rápida de saúde via navegador.
  return _json({ success: true, hint: 'POST com {secret, op}' });
}

function handleFreebusy(body) {
  const calendarId = body.calendarId || DEFAULT_CALENDAR;
  const startISO = body.startISO;
  const endISO = body.endISO;
  if (!startISO || !endISO) {
    return { success: false, error: 'startISO and endISO required' };
  }
  const start = new Date(startISO);
  const end = new Date(endISO);
  const cal = CalendarApp.getCalendarById(calendarId);
  if (!cal) {
    return { success: false, error: 'calendar not accessible: ' + calendarId };
  }
  const events = cal.getEvents(start, end);
  const busy = events
    .filter(function (ev) {
      // Ignora eventos onde o Guilherme recusou.
      try {
        if (ev.getMyStatus && ev.getMyStatus() === CalendarApp.GuestStatus.NO) return false;
      } catch (e) {}
      return true;
    })
    .map(function (ev) {
      const isAllDay = ev.isAllDayEvent();
      return {
        id: ev.getId(),
        start: ev.getStartTime().toISOString(),
        end: ev.getEndTime().toISOString(),
        summary: ev.getTitle() || '',
        allDay: isAllDay,
      };
    });
  return { success: true, busy: busy, calendarId: calendarId };
}

function handleCreateEvent(body) {
  const calendarId = body.calendarId || DEFAULT_CALENDAR;
  const guestName = body.guestName || 'Lead';
  const guestEmail = body.guestEmail || '';
  const startISO = body.startISO;
  const endISO = body.endISO;
  const summary = body.summary || ('Reunião — Seguro de Vida — ' + guestName);
  const description = body.description || 'Reunião com a assessoria do Prado.';

  if (!startISO || !endISO) {
    return { success: false, error: 'startISO and endISO required' };
  }

  const requestId = Utilities.getUuid();
  const eventResource = {
    summary: summary,
    description: description,
    start: { dateTime: startISO },
    end: { dateTime: endISO },
    conferenceData: {
      createRequest: {
        requestId: requestId,
        conferenceSolutionKey: { type: 'hangoutsMeet' },
      },
    },
    reminders: {
      useDefault: false,
      overrides: [
        { method: 'email', minutes: 60 },
        { method: 'popup', minutes: 15 },
      ],
    },
  };
  if (guestEmail) {
    eventResource.attendees = [{ email: guestEmail, displayName: guestName }];
  }

  // Calendar advanced service — exige habilitar "Google Calendar API" em Services.
  const created = Calendar.Events.insert(eventResource, calendarId, {
    conferenceDataVersion: 1,
    sendUpdates: guestEmail ? 'all' : 'none',
  });

  let meetLink = '';
  const entryPoints = (created.conferenceData && created.conferenceData.entryPoints) || [];
  for (let i = 0; i < entryPoints.length; i++) {
    if (entryPoints[i].entryPointType === 'video') {
      meetLink = entryPoints[i].uri;
      break;
    }
  }

  const startDate = new Date(startISO);
  const tz = Session.getScriptTimeZone();
  const formatted = Utilities.formatDate(startDate, tz, "dd/MM/yyyy 'às' HH:mm");

  return {
    success: true,
    eventId: created.id,
    meetLink: meetLink,
    eventLink: created.htmlLink || '',
    startTimeFormatted: formatted,
  };
}

function handleCancelEvent(body) {
  const calendarId = body.calendarId || DEFAULT_CALENDAR;
  const eventId = body.eventId;
  const notify = body.notifyAttendees === false ? 'none' : 'all';
  if (!eventId) {
    return { success: false, error: 'eventId required' };
  }
  try {
    Calendar.Events.remove(calendarId, eventId, { sendUpdates: notify });
    return { success: true, eventId: eventId, cancelled: true };
  } catch (e) {
    return { success: false, error: String(e), eventId: eventId };
  }
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
