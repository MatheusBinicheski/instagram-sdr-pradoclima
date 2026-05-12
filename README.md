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

### 3. Configure a variável de ambiente

No painel do Railway → seu projeto → **Variables** → **New Variable**:

| Chave | Valor |
|-------|-------|
| `ANTHROPIC_API_KEY` | sua chave em console.anthropic.com |

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
