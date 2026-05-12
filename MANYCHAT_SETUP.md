# Configuração ManyChat — SDR Bot

## Visão geral do fluxo

```
Seguidor manda DM/comentário
        ↓
    ManyChat recebe
        ↓
ManyChat chama seu webhook (POST /webhook)
        ↓
Claude AI gera resposta personalizada
        ↓
ManyChat envia a resposta para o seguidor
```

---

## PASSO 1 — Suba o servidor

### Opção A: Railway (recomendado, gratuito para começar)

1. Acesse [railway.app](https://railway.app) e crie conta
2. Clique em **New Project → Deploy from GitHub Repo**
3. Suba os arquivos do projeto para um repositório GitHub
4. Em **Variables**, adicione:
   ```
   ANTHROPIC_API_KEY = sua_chave_aqui
   ```
5. Railway vai gerar uma URL tipo: `https://seu-bot.up.railway.app`

### Opção B: Render (gratuito)

1. Acesse [render.com](https://render.com)
2. New → Web Service → conecte o GitHub
3. **Start Command:** `uvicorn src.webhook:app --host 0.0.0.0 --port $PORT`
4. Adicione a variável `ANTHROPIC_API_KEY`

### Opção C: Local com ngrok (para testes)

```bash
# Terminal 1 — inicia o servidor
python main.py

# Terminal 2 — expõe na internet
ngrok http 8000
```
ngrok gera uma URL tipo `https://xxxx.ngrok.io` — use essa no ManyChat.

---

## PASSO 2 — Crie os Campos Customizados no ManyChat

Vá em **Audience → Custom Fields → New Field**

| Nome do campo | Tipo   |
|---------------|--------|
| `sdr_stage`   | Text   |

---

## PASSO 3 — Configure o Flow de DMs

### 3.1 — Crie o Flow

**Automation → New Flow → "SDR Bot — DMs"**

### 3.2 — Triggers (escolha os que quiser)

- **Instagram DM Received** (qualquer mensagem)
- **Instagram Story Reply** (quando respondem o story)
- **Keyword** (ex: palavras como "info", "quero", "valor", "evento")

### 3.3 — Adicione o bloco "Dynamic Content"

1. No editor do flow, clique em **"+"** → **External Request**
2. Configure assim:

```
Método:  POST
URL:     https://sua-url.railway.app/webhook
Headers: Content-Type: application/json

Body (JSON):
{
  "subscriber_id": "{{messenger user id}}",
  "first_name":    "{{first name}}",
  "last_name":     "{{last name}}",
  "last_message":  "{{last input text}}",
  "sdr_stage":     "{{sdr_stage}}"
}

Response Type: Dynamic Content (v2)
```

3. Em **"On Success"**: o ManyChat já usa a resposta do webhook automaticamente
4. Em **"On Error"**: adicione uma mensagem de fallback:
   > "Oi! Vi sua mensagem e vou te responder em breve. 😊"

---

## PASSO 4 — Configure o Flow de Comentários

**Automation → New Flow → "SDR Bot — Comentários"**

### Trigger
- **Instagram Comment** → em qualquer post (ou posts específicos)

### Bloco External Request

```
Método:  POST
URL:     https://sua-url.railway.app/webhook/comment

Body (JSON):
{
  "subscriber_id": "{{messenger user id}}",
  "first_name":    "{{first name}}",
  "last_message":  "{{last input text}}"
}

Response Type: Dynamic Content (v2)
```

---

## PASSO 5 — Teste o webhook

Use o Postman ou o próprio ManyChat para testar:

```bash
curl -X POST https://sua-url.railway.app/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "subscriber_id": "teste123",
    "first_name": "João",
    "last_message": "oi, vi o post sobre prosperidade",
    "sdr_stage": "conexao"
  }'
```

Resposta esperada:
```json
{
  "version": "v2",
  "content": {
    "messages": [{"type": "text", "text": "Oi João! ..."}],
    "actions": [{"action": "set_field", "field_name": "sdr_stage", "value": "qualificando"}],
    "quick_replies": [...]
  }
}
```

---

## Funil de Vendas (estágios automáticos)

| Estágio        | Descrição                                       |
|----------------|-------------------------------------------------|
| `conexao`      | Primeiro contato — cria rapport                 |
| `qualificando` | Descobre dor/desejo do lead                     |
| `apresentando` | Apresenta o produto certo para o perfil         |
| `objecao`      | Trata objeções com empatia                      |
| `fechando`     | Lead quente — envia link de pagamento           |
| `frio`         | Sem interesse — bot recua                       |

O bot avança os estágios automaticamente e o campo `sdr_stage` no ManyChat é atualizado a cada resposta.

---

## Monitoramento

- **Stats:** `GET https://sua-url/stats`
- **Logs:** arquivo `logs/bot.log`
- **Resetar conversa:** `DELETE https://sua-url/conversation/{subscriber_id}`
