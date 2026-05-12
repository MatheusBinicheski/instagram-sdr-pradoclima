#!/usr/bin/env python3
"""
deploy_hub_calendar.py
======================
Cria e publica o HUB Calendar Relay no Google Apps Script automaticamente.
Abre o navegador UMA vez para autorizar — depois é tudo automático.

Uso:
    pip install google-auth-oauthlib google-auth-httplib2 requests
    python deploy_hub_calendar.py
"""

import json
import os
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

# ─── Credenciais OAuth embutidas (Google Apps Script API — client público) ────
# Se preferir usar suas próprias credenciais, crie um client OAuth "Desktop"
# no Google Cloud Console e salve como client_secrets.json nesta pasta.
FALLBACK_CLIENT_ID     = ""
FALLBACK_CLIENT_SECRET = ""

SCOPES = [
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
    "https://www.googleapis.com/auth/drive.file",
]

CREDS_FILE = os.path.join(os.path.dirname(__file__), ".hub_calendar_creds.json")

SECRET     = "hub-relay-2026-X9k"
CALENDAR_ID = "adm@plataformaglobalbrasilia.com.br"

SCRIPT_SOURCE = r"""
const CALENDAR_ID = 'adm@plataformaglobalbrasilia.com.br';
const SECRET      = 'hub-relay-2026-X9k';

function doPost(e) {
  try {
    var p = JSON.parse(e.postData.contents);
    if (p.secret !== SECRET) return json({success:false,error:'Unauthorized'});
    var s = new Date(p.startTime);
    var f = new Date(p.endTime || new Date(s.getTime()+30*60000));
    var ev = Calendar.Events.insert({
      summary:     p.summary || 'Reunião HUB Global Business — '+(p.guestName||''),
      description: p.description || 'Reunião de descoberta sobre o HUB.\nhttps://hubglobalbusines.plataformaglobalbsb.com.br/',
      start:{dateTime:s.toISOString(),timeZone:'America/Sao_Paulo'},
      end:  {dateTime:f.toISOString(),timeZone:'America/Sao_Paulo'},
      attendees:[{email:p.guestEmail,displayName:p.guestName||''}],
      conferenceData:{createRequest:{requestId:'hub-'+Date.now(),conferenceSolutionKey:{type:'hangoutsMeet'}}},
      reminders:{useDefault:false,overrides:[{method:'email',minutes:60},{method:'popup',minutes:15}]}
    }, CALENDAR_ID, {conferenceDataVersion:1});
    var meet='';
    var eps=(ev.conferenceData||{}).entryPoints||[];
    for(var i=0;i<eps.length;i++){if(eps[i].entryPointType==='video'){meet=eps[i].uri;break;}}
    var fmt=Utilities.formatDate(s,'America/Sao_Paulo',"dd/MM/yyyy 'às' HH:mm");
    return json({success:true,eventId:ev.id,eventLink:ev.htmlLink,meetLink:meet,startTimeFormatted:fmt});
  } catch(err) { return json({success:false,error:err.message}); }
}
function json(d){return ContentService.createTextOutput(JSON.stringify(d)).setMimeType(ContentService.MimeType.JSON);}
function testeLocal(){
  var fake={postData:{contents:JSON.stringify({secret:SECRET,guestEmail:'teste@teste.com',guestName:'Teste',startTime:new Date(Date.now()+86400000).toISOString()})}};
  Logger.log(doPost(fake).getContent());
}
"""

MANIFEST = json.dumps({
    "timeZone": "America/Sao_Paulo",
    "dependencies": {
        "enabledAdvancedServices": [{
            "userSymbol": "Calendar",
            "serviceId": "calendar",
            "version": "v3"
        }]
    },
    "exceptionLogging": "STACKDRIVER",
    "runtimeVersion": "V8",
    "webapp": {
        "executeAs": "USER_DEPLOYING",
        "access": "ANYONE_ANONYMOUS"
    }
})


# ─── Auth ─────────────────────────────────────────────────────────────────────

def load_client_secrets():
    """Tenta carregar client_secrets.json local, senão usa embutido."""
    path = os.path.join(os.path.dirname(__file__), "client_secrets.json")
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        installed = data.get("installed") or data.get("web") or {}
        return installed.get("client_id"), installed.get("client_secret")
    if FALLBACK_CLIENT_ID:
        return FALLBACK_CLIENT_ID, FALLBACK_CLIENT_SECRET
    print("\n❌ Credenciais OAuth não encontradas.")
    print("   Crie um client OAuth no Google Cloud Console:")
    print("   1. https://console.cloud.google.com → APIs e Serviços → Credenciais")
    print("   2. + Criar credenciais → ID do cliente OAuth")
    print("   3. Tipo: App para computador")
    print("   4. Baixe o JSON e salve como 'client_secrets.json' nesta pasta")
    print("   5. Execute o script novamente\n")
    sys.exit(1)


def get_token_via_oauth(client_id, client_secret):
    """OAuth2 instalado: abre o navegador, captura o código no callback local."""
    redirect_uri  = "http://localhost:8888/callback"
    auth_url      = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urlencode({
            "client_id":     client_id,
            "redirect_uri":  redirect_uri,
            "response_type": "code",
            "scope":         " ".join(SCOPES),
            "access_type":   "offline",
            "prompt":        "consent",
        })
    )

    auth_code = {"value": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/callback":
                params = parse_qs(parsed.query)
                auth_code["value"] = params.get("code", [None])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<h2>Autorizado! Pode fechar esta aba.</h2>")
        def log_message(self, *args):
            pass

    print(f"\n🌐 Abrindo navegador para autorização Google...")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", 8888), Handler)
    server.timeout = 120
    while not auth_code["value"]:
        server.handle_request()

    # Troca código por tokens
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "code":          auth_code["value"],
        "client_id":     client_id,
        "client_secret": client_secret,
        "redirect_uri":  redirect_uri,
        "grant_type":    "authorization_code",
    })
    resp.raise_for_status()
    tokens = resp.json()
    tokens["client_id"]     = client_id
    tokens["client_secret"] = client_secret
    with open(CREDS_FILE, "w") as f:
        json.dump(tokens, f)
    print("✅ Autorizado e credenciais salvas.")
    return tokens["access_token"]


def refresh_access_token(creds):
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type":    "refresh_token",
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_access_token():
    # 1. Tenta gcloud CLI
    import subprocess
    try:
        res = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True, timeout=10
        )
        if res.returncode == 0 and res.stdout.strip():
            print("✅ Token via gcloud CLI.")
            return res.stdout.strip()
    except Exception:
        pass

    # 2. Credenciais salvas
    if os.path.exists(CREDS_FILE):
        with open(CREDS_FILE) as f:
            creds = json.load(f)
        if "refresh_token" in creds:
            try:
                token = refresh_access_token(creds)
                print("✅ Token via credenciais salvas.")
                return token
            except Exception:
                pass

    # 3. OAuth flow completo
    client_id, client_secret = load_client_secrets()
    return get_token_via_oauth(client_id, client_secret)


# ─── Apps Script API ──────────────────────────────────────────────────────────

def api(method, url, token, **kwargs):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = getattr(requests, method)(url, headers=headers, **kwargs)
    if not resp.ok:
        print(f"❌ Erro {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
    return resp.json()


def create_project(token):
    print("📁 Criando projeto Apps Script...")
    data = api("post", "https://script.googleapis.com/v1/projects", token,
               json={"title": "HUB Calendar Relay"})
    return data["scriptId"]


def upload_code(token, script_id):
    print("📝 Uploading código...")
    api("put", f"https://script.googleapis.com/v1/projects/{script_id}/content", token,
        json={"files": [
            {"name": "Code",        "type": "SERVER_JS", "source": SCRIPT_SOURCE},
            {"name": "appsscript",  "type": "JSON",      "source": MANIFEST},
        ]})


def create_version(token, script_id):
    print("🏷️  Criando versão...")
    data = api("post", f"https://script.googleapis.com/v1/projects/{script_id}/versions", token,
               json={"description": "v1"})
    return data["versionNumber"]


def create_deployment(token, script_id, version):
    print("🚀 Publicando como Web App...")
    data = api("post", f"https://script.googleapis.com/v1/projects/{script_id}/deployments", token,
               json={
                   "versionNumber": version,
                   "manifestFileName": "appsscript",
                   "description": "HUB Calendar Relay — produção",
               })
    return data.get("deploymentId", "")


def get_web_url(token, script_id, deployment_id):
    data = api("get", f"https://script.googleapis.com/v1/projects/{script_id}/deployments/{deployment_id}", token)
    eps = data.get("entryPoints", [])
    for ep in eps:
        if ep.get("entryPointType") == "WEB_APP":
            return ep.get("webApp", {}).get("url", "")
    # Fallback: construir URL manualmente
    return f"https://script.google.com/macros/s/{deployment_id}/exec"


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*55)
    print("  HUB Calendar Relay — Deploy Automático")
    print("="*55)

    token       = get_access_token()
    script_id   = create_project(token)
    upload_code(token, script_id)
    version     = create_version(token, script_id)
    dep_id      = create_deployment(token, script_id, version)

    # Aguarda propagação
    print("⏳ Aguardando propagação do deploy...")
    time.sleep(3)

    web_url = get_web_url(token, script_id, dep_id)

    print("\n" + "="*55)
    print("  ✅ SUCESSO! Cole no Railway:")
    print("="*55)
    print(f"\n  GOOGLE_SCRIPT_URL    = {web_url}")
    print(f"  GOOGLE_SCRIPT_SECRET = {SECRET}")
    print(f"\n  Script ID (backup): {script_id}")
    print(f"  Link: https://script.google.com/d/{script_id}/edit\n")


if __name__ == "__main__":
    main()
