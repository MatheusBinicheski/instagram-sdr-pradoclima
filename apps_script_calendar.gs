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

  // Calendar.Events.list (advanced service) — devolve API event IDs reais,
  // diferente do CalendarApp.getEvents() que devolve iCalUID. Usamos o id
  // aqui pra que /agenda/cancel-by-iso consiga deletar pelo Events.remove.
  let response;
  try {
    response = Calendar.Events.list(calendarId, {
      timeMin: new Date(startISO).toISOString(),
      timeMax: new Date(endISO).toISOString(),
      singleEvents: true,
      orderBy: 'startTime',
      showDeleted: false,
      maxResults: 250,
    });
  } catch (e) {
    return { success: false, error: 'list failed: ' + String(e) };
  }

  const items = response.items || [];
  const busy = items
    .filter(function (ev) {
      // Ignora eventos onde o owner recusou.
      if (!ev.attendees) return true;
      for (let i = 0; i < ev.attendees.length; i++) {
        const att = ev.attendees[i];
        if (att.self && att.responseStatus === 'declined') return false;
      }
      return true;
    })
    .map(function (ev) {
      const isAllDay = !!(ev.start && ev.start.date);
      const startTime = (ev.start && (ev.start.dateTime || ev.start.date)) || '';
      const endTime = (ev.end && (ev.end.dateTime || ev.end.date)) || '';
      return {
        id: ev.id,
        start: startTime ? new Date(startTime).toISOString() : '',
        end: endTime ? new Date(endTime).toISOString() : '',
        summary: ev.summary || '',
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
  let eventId = body.eventId;
  const notify = body.notifyAttendees === false ? 'none' : 'all';
  if (!eventId) {
    return { success: false, error: 'eventId required' };
  }

  // Defensivo: se chegou iCalUID (formato "abc@google.com"), traduz pro API event ID.
  if (eventId.indexOf('@') > -1) {
    try {
      const list = Calendar.Events.list(calendarId, {
        iCalUID: eventId,
        showDeleted: false,
        maxResults: 1,
      });
      if (list.items && list.items.length > 0) {
        eventId = list.items[0].id;
      } else {
        return { success: false, error: 'event not found by iCalUID', eventId: eventId };
      }
    } catch (e) {
      return { success: false, error: 'iCalUID lookup failed: ' + String(e), eventId: eventId };
    }
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
