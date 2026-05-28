# Instagram SDR Bot
**Pedro Rehn × Erasmo Cirqueira — Powered by Claude AI**

Webhook para ManyChat que age como SDR/Closer inteligente via Instagram DMs e comentários.

---

## Deploy no Railway (passo a passo)

### 1. Suba para o GitHub

No terminal dentro da pasta do projeto:

```bash
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/SEU_USUARIO/SEU_REPO.git
git push -u origin main
```

### 2. Conecte ao Railway

1. Acesse [railway.app](https://railway.app) → **New Project**
2. Escolha **Deploy from GitHub repo**
3. Selecione o repositório do bot
4. Railway detecta automaticamente o `Procfile` e sobe o servidor

### 3. Configure as variáveis de ambiente

No painel do Railway → seu projeto → **Variables** → **New Variable**:

| Chave | Valor |
|-------|-------|
| `ANTHROPIC_API_KEY` | sua chave em console.anthropic.com |
| `GOOGLE_SCRIPT_URL` | URL do Apps Script — veja "Setup da agenda" abaixo |
| `GOOGLE_SCRIPT_SECRET` | mesmo `SHARED_SECRET` do Apps Script |
| `GUILHERME_CALENDAR_ID` | `grsouza93ip@gmail.com` (default já correto) |

---

## Setup da agenda (Apps Script)

O bot consulta a agenda real do Guilherme (`grsouza93ip@gmail.com`) e cria
eventos lá quando o lead confirma a reunião. Para isso, o Guilherme precisa
fazer um deploy de 5 minutos no Google Apps Script dele.

### Passo a passo (peça pro Guilherme fazer):

1. Entre em **https://script.google.com** logado como `grsouza93ip@gmail.com`.
2. Clique em **New project**.
3. Apague o conteúdo de `Code.gs` e cole o arquivo `apps_script_calendar.gs` deste repo.
4. Edite a constante `SHARED_SECRET` no topo — coloque uma string aleatória longa (ex.: `openssl rand -hex 24`).
5. No menu lateral, clique no `+` ao lado de **Services** → adicione **Google Calendar API** (deixe identifier `Calendar`).
6. **Deploy → New deployment**:
   - Type: **Web app**
   - Execute as: **Me (grsouza93ip@gmail.com)**
   - Who has access: **Anyone**
   - Clica em Deploy e autoriza o acesso ao Calendar quando pedir.
7. Copie a **Web app URL** que aparece após o deploy.
8. No Railway, defina:
   - `GOOGLE_SCRIPT_URL` = a URL copiada
   - `GOOGLE_SCRIPT_SECRET` = o mesmo valor de `SHARED_SECRET`

### Confirmar que está funcionando

Depois do deploy, bata no endpoint de saúde:

```bash
curl https://SEU-PROJETO.up.railway.app/agenda/health
```

Resposta esperada:
```json
{ "has_real_calendar": true, "calendar_id": "grsouza93ip@gmail.com", "mode": "apps_script" }
```

E pra ver os horários livres que o bot vê:
```bash
curl https://SEU-PROJETO.up.railway.app/agenda/available
```

> Se `mode: "link_fallback"` aparecer, o bot **não** está acessando a agenda real
> e vai oferecer slots de uma grade estática — confira o `GOOGLE_SCRIPT_URL`.

### 4. Copie a URL pública

Railway → seu projeto → **Settings → Networking → Public Domain**

Vai ser algo como: `https://instagram-sdr-bot-production.up.railway.app`

Essa URL vai no ManyChat.

---

## Configuração no ManyChat

### Campos customizados necessários

**Audience → Custom Fields → New Field:**

| Campo | Tipo |
|-------|------|
| `sdr_stage` | Text |

---

### Flow de DMs

**Automation → New Flow → "SDR Bot DMs"**

**Trigger:** Instagram DM Received (ou Keywords específicas)

**Bloco:** External Request
```
Método: POST
URL:    https://SEU-PROJETO.up.railway.app/webhook

Body (JSON):
{
  "subscriber_id": "{{messenger user id}}",
  "first_name":    "{{first name}}",
  "last_message":  "{{last input text}}",
  "sdr_stage":     "{{sdr_stage}}"
}

Response Type: Dynamic Content (v2)
```

**On Error:** mensagem de fallback → "Oi! Vi sua mensagem e respondo em breve 😊"

---

### Flow de Comentários

**Automation → New Flow → "SDR Bot Comentários"**

**Trigger:** Instagram Comment (qualquer post ou posts selecionados)

**Bloco:** External Request
```
Método: POST
URL:    https://SEU-PROJETO.up.railway.app/webhook/comment

Body (JSON):
{
  "subscriber_id": "{{messenger user id}}",
  "first_name":    "{{first name}}",
  "last_message":  "{{last input text}}"
}

Response Type: Dynamic Content (v2)
```

---

## Produtos configurados

| Produto | Link |
|---------|------|
| Família Inquebráel — Brasília | https://payfast.greenn.com.br/4gtruwa |
| Imersão Rota Prosperidade — Rio Verde | https://payfast.greenn.com.br/dmqav8z |
| Imersão Rota Prosperidade — Goiânia | https://payfast.greenn.com.br/7zsz478 |
| Método IP (Online) | https://metodoip.com.br |

---

## Funil automático

```
CONEXÃO → QUALIFICAÇÃO → APRESENTAÇÃO → OBJEÇÃO → FECHAMENTO
```

O campo `sdr_stage` no ManyChat é atualizado a cada resposta. O bot avança o lead pelo funil automaticamente.

---

## Endpoints da API

| Método | Rota | Uso |
|--------|------|-----|
| `POST` | `/webhook` | DMs (ManyChat Dynamic Content) |
| `POST` | `/webhook/comment` | Comentários |
| `GET` | `/health` | Status do servidor |
| `GET` | `/stats` | Estatísticas de conversas |
| `DELETE` | `/conversation/{id}` | Resetar conversa |

---

## Testar o webhook

```bash
curl -X POST https://SEU-PROJETO.up.railway.app/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "subscriber_id": "teste001",
    "first_name": "João",
    "last_message": "oi, vi o post sobre prosperidade e quero saber mais",
    "sdr_stage": "conexao"
  }'
```

---

## Estrutura do projeto

```
├── src/
│   ├── webhook.py            ← Servidor FastAPI (endpoints ManyChat)
│   ├── sales_agent.py        ← Claude AI como SDR/Closer
│   ├── conversation_manager.py ← Histórico e estágios por usuário
│   ├── products.py           ← Catálogo de produtos e links
│   └── config.py             ← Variáveis de ambiente
├── data/                     ← Conversas salvas (gerado em runtime)
├── logs/                     ← Logs (gerado em runtime)
├── main.py                   ← Entry point local
├── Procfile                  ← Railway/Heroku start command
├── railway.toml              ← Config Railway
└── requirements.txt
```
