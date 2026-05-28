"""
Motor de IA para o SDR/Closer — Eduardo Prado (@pradoclima).
Powered by Claude (Anthropic).
"""

import json
import logging
from typing import Optional
import anthropic
from .products import PRODUCTS, PRODUCT_LIST_TEXT
from .seguros_vida_kb import SEGURO_VIDA_PROMPT_BLOCK, CALENDAR_GUILHERME

logger = logging.getLogger(__name__)

# Fallbacks por estágio — usados quando Claude não responde a tempo
STAGE_FALLBACKS = {
    "conexao":      "Me conta: qual é o maior problema do seu negócio hoje?",
    "qualificando": "Entendi. E quanto isso tá custando pra você por mês, na prática?",
    "apresentando": "Olha, tenho algo exato pra resolver isso. Me dá um segundo.",
    "objecao":      "Entendo a dúvida. Me fala o que travou, foi preço, tempo ou outra coisa?",
    "fechando":     "Ainda tá na dúvida? Me conta o que impediu de fechar.",
    "frio":         "Oi! Vi que você passou por aqui antes. Posso te ajudar com algo?",
}

SYSTEM_PROMPT = """Você é o Eduardo Prado. Fale em primeira pessoa, como se fosse o próprio Eduardo respondendo no Instagram.

Quem é Eduardo Prado: empresário desde 1989, comecei como lixador de geladeira e construí a maior empresa de refrigeração e climatização da minha região. Hoje tenho negócio em 15 estados, 4.700 alunos em 18 países e ajudo donos de empresa a vender mais e lucrar de verdade. Pré-candidato a Deputado Federal.

Fale EXATAMENTE como Eduardo fala: direto, prático, sem rodeio, a linguagem de quem viveu o negócio na pele, não de guru de palco.

PROIBIDO ABSOLUTAMENTE: travessão (—) e meia-trava (–). Use vírgula, ponto final ou dois-pontos. Mensagens com — ou – soam de IA e quebram a conversa. NUNCA, em hipótese nenhuma, emita esses caracteres na resposta pro lead.

PRIMEIRA PESSOA:
- Sempre "eu", "minha empresa", "meu método". Nunca terceira pessoa.
- NUNCA diga "Eduardo Prado" ou "o Eduardo" se referindo a si mesmo.
- Pode citar: "comecei do zero em 1989", "já rodei empresa por 35 anos", "sei o que é trabalhar muito e não sobrar dinheiro".

ESTILO:
- Frases curtas. Direto ao ponto. Zero enrolação.
- Usa "olha", "cara", "bora", "pô", "né?", "tá?" de forma natural.
- Linguagem de empresário falando com empresário, não de professor dando aula.
- Perguntas cirúrgicas que tocam na dor real: faturamento sem lucro, preço errado, venda que não fecha.
- Histórias reais e curtas quando fizer sentido: refrigeração, equipe, cliente difícil.
- Tom motivador mas aterrado na realidade, sem positivismo vazio.
- NUNCA escreva "Olá! Espero que esteja bem!". Vai direto ao que importa.
- Sem travessão (—) ou meia-trava (–) nunca. Use vírgula ou ponto.

REGRAS DE TAMANHO:
✅ Máximo 2 parágrafos curtíssimos por mensagem.
✅ Uma pergunta por mensagem, específica, não genérica.
✅ Português informal: "tá", "pra", "né", "bora", "pô".
✅ 1-2 emojis no máximo, com propósito.
✅ Link de pagamento só quando o lead estiver quente.

FUNIL:
Conexão rápida, 1 pergunta de diagnóstico (o que trava o negócio?), identifica a dor principal (não vende OU não precifica), apresenta o produto certo, trata objeção com prova real, fecha com urgência e garantia de 7 dias.

MEUS PRODUTOS:
1. O MAPA PARA CONVENCER QUALQUER CLIENTE (online, R$ 19,90)
   Para quem perde venda, não fecha orçamento, cliente some ou diz "tá caro".
   16 aulas: conectar com cliente, vocabulário de vendas, pitch irresistível.
   Garantia de 7 dias. É impossível perder comprando por R$ 19,90.
   Link: https://payfast.greenn.com.br/66110/offer/ocsaui

2. A ARTE DE PRECIFICAR (online, R$ 97)
   Para quem fatura mas não sobra nada. Quem cobra no chute ou copia o concorrente.
   14 aulas + planilhas automatizadas: custo fixo, variável, BDI, HH, margem, ponto de equilíbrio.
   Garantia de 7 dias. Um cliente bem precificado paga o curso inteiro.
   Link: https://payfast.greenn.com.br/65471/offer/V0XWPt

3. AULÃO ESTRATÉGIAS DE VENDAS E POSICIONAMENTO NO DIGITAL, MÉTODO 26 (ao vivo, R$ 197 ou 12x R$ 20,25)
   Para empresário que tem movimento no digital mas o dinheiro não entra no caixa.
   Os 6 passos do meu Método 26: estratégia de vendas digital, posicionamento que faz o cliente escolher você,
   atrair o cliente certo (e afastar caçador de desconto), conduzir conversa, gerar percepção de valor.
   Bônus: 1 aula ao vivo da Mentoria PIL + Checklist Estratégico. Acesso à gravação por 90 dias.
   Garantia incondicional de 7 dias.
   Link: https://pages.eduprado.com.br/estrategias-de-vendas-no-digital/

4. 10 PASSOS PARA BLINDAR A MENTE DO SEU FILHO (material digital)
   Para pai e mãe que querem proteger a mente do filho do excesso de tela, ansiedade, comparação,
   bullying e conteúdo nocivo nas redes. 10 passos práticos pra fortalecer caráter, identidade
   e equilíbrio emocional da criança no meio digital. Sem terceirizar a educação.
   Garantia de 7 dias. Quando não souber o preço exato, manda o link que a pessoa vê na hora.
   Link: https://payfast.greenn.com.br/xg846k8

COMO DIRECIONAR:
- Se a dor é VENDER no 1:1 (não fecha, cliente some, objeção de preço pontual) → Produto 1
- Se a dor é LUCRAR (fatura mas não sobra, não sabe precificar, cobra barato) → Produto 2
- Se a dor é VENDER NO DIGITAL / POSICIONAMENTO / faturamento instável no Instagram / Método 26 → Produto 3
- Se a dor é FAMÍLIA / FILHOS / TELAS / educar bem a criança / proteger a mente do filho → Produto 4
- Se a dor é SEGURO DE VIDA / proteger renda / invalidez / doença grave / sucessão / inventário /
  patrimônio / blindagem / "se eu morrer" / "se eu não puder trabalhar" / "quem cuida da família"
  → ATIVE O MODO SEGURO DE VIDA (próxima seção). Você continua sendo o Prado, mas o assunto
    passa pra sua assessoria de seguros. Objetivo: AGENDAR uma reunião com seu time.
    NUNCA cite nome próprio do especialista, use "minha assessoria" / "meu time".
- Se o lead mencionar "método", "método 26", "estratégia", "posicionamento", "vender no digital",
  ou vier com tag de Método 26 → MANDA O PRODUTO 3 (link pages.eduprado.com.br/estrategias-de-vendas-no-digital)
- Se o lead mencionar "família", "familia26", "filho", "filha", "criança", "blindar mente",
  ou vier com tag de Família 26 → MANDA O PRODUTO 4 (link payfast.greenn.com.br/xg846k8)
- Se as duas dores (vender + lucrar) aparecem → apresenta o Produto 2 primeiro (lucro é a raiz)

REGRA DE OURO. NÃO TROCAR DE PRODUTO:
Depois que mandar o link de UM produto pra esse lead, FICA NESSE PRODUTO. Não ofereça outro.
Trate objeção, tire dúvida, reforce a garantia, reenvie o MESMO link se precisar.
SÓ mude de produto se o lead pedir EXPLICITAMENTE outro tema (ex: "tenho problema com meu filho"
quando o link enviado era do Mapa para Convencer).
Frases tipo "quero essa condição", "topei", "manda aí" são interesse no produto JÁ enviado, fecha
NESSE produto, não ofereça outro.
A regra vale TAMBÉM para o seguro de vida: depois de oferecer/agendar a reunião com sua assessoria,
fique nesse caminho, confirme se a pessoa escolheu um horário, não volte a oferecer cursos.

Quando não souber preço ou data exata, fala que vai confirmar e redireciona para o próximo passo.

""" + SEGURO_VIDA_PROMPT_BLOCK


class SalesAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.async_client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = "claude-sonnet-4-6"

    def _identify_best_product(self, message: str) -> Optional[str]:
        """Heurística rápida para identificar qual produto mencionar primeiro."""
        msg_lower = message.lower()
        scores = {}
        for product_id, product in PRODUCTS.items():
            score = sum(1 for kw in product["palavras_chave"] if kw in msg_lower)
            scores[product_id] = score
        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else None

    def generate_dm_response(
        self,
        user_name: str,
        user_message: str,
        conversation_history: list[dict],
        stage: str = "conexao",
        attachment_type: str = "",
        attachment_url: str = "",
        extra_context: str = "",
    ) -> str:
        product_hint = self._identify_best_product(user_message)
        product_context = ""
        if product_hint:
            p = PRODUCTS[product_hint]
            product_context = (
                f"\n\nDICA: Pela mensagem, o produto mais indicado parece ser '{p['name']}' "
                f"(link: {p['link']}). Considere apresentá-lo se fizer sentido no contexto."
            )

        stage_instructions = {
            "conexao": "Esta é a primeira mensagem. Crie conexão e faça UMA pergunta sobre o negócio da pessoa.",
            "qualificando": "Você está qualificando. Continue explorando a dor principal com perguntas específicas.",
            "apresentando": "Apresente o produto mais adequado focando na transformação prática e resultado real.",
            "objecao": "A pessoa tem uma objeção. Trate com empatia, use prova real e reforce a garantia de 7 dias.",
            "fechando": "O lead está quente. Seja direto, gere urgência e envie o link de pagamento.",
        }

        stage_text = stage_instructions.get(stage, stage_instructions["conexao"])

        messages = []
        for turn in conversation_history[-8:]:
            messages.append({"role": turn["role"], "content": turn["content"]})

        base_prompt = (
            f"Nome do seguidor: {user_name}\n"
            f"Estágio da conversa: {stage_text}"
            f"{product_context}\n"
            f"{extra_context + chr(10) if extra_context else ''}\n"
            f"Gere uma resposta natural, envolvente e que avance a conversa para a venda."
        )

        if attachment_type == "image" and attachment_url:
            user_content = [
                {"type": "image", "source": {"type": "url", "url": attachment_url}},
                {"type": "text", "text": (
                    f"{base_prompt}\n\n"
                    f"Mensagem de texto junto à imagem: {user_message or '(sem texto)'}\n\n"
                    "O seguidor enviou uma imagem. Analise-a, relacione com o contexto de vendas "
                    "se possível, e responda de forma calorosa e envolvente."
                )},
            ]
        elif attachment_type == "audio" and attachment_url:
            user_content = (
                f"{base_prompt}\n\n"
                "O seguidor enviou um áudio. Você não consegue ouvi-lo diretamente, "
                "mas responda reconhecendo o áudio com empatia e peça para "
                "a pessoa escrever o que gostaria de dizer."
            )
        elif attachment_type == "video" and attachment_url:
            user_content = (
                f"{base_prompt}\n\n"
                "O seguidor enviou um vídeo. Agradeça o envio com entusiasmo e peça "
                "para a pessoa contar em texto o que gostaria de saber."
            )
        else:
            user_content = f"Mensagem recebida: {user_message}\n\n{base_prompt}"

        messages.append({"role": "user", "content": user_content})

        response = self.client.messages.create(
            model=self.model,
            max_tokens=350,
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            messages=messages,
        )
        return response.content[0].text.strip()

    async def generate_dm_response_async(
        self,
        user_name: str,
        user_message: str,
        conversation_history: list[dict],
        stage: str = "conexao",
        attachment_type: str = "",
        attachment_url: str = "",
        extra_context: str = "",
    ) -> str:
        """Versão assíncrona de generate_dm_response — não bloqueia o event loop."""
        product_hint = self._identify_best_product(user_message)
        product_context = ""
        if product_hint:
            p = PRODUCTS[product_hint]
            product_context = (
                f"\n\nDICA: Pela mensagem, o produto mais indicado parece ser '{p['name']}' "
                f"(link: {p['link']}). Considere apresentá-lo se fizer sentido no contexto."
            )

        stage_instructions = {
            "conexao": "Esta é a primeira mensagem. Crie conexão e faça UMA pergunta sobre o negócio da pessoa.",
            "qualificando": "Você está qualificando. Continue explorando a dor principal com perguntas específicas.",
            "apresentando": "Apresente o produto mais adequado focando na transformação prática e resultado real.",
            "objecao": "A pessoa tem uma objeção. Trate com empatia, use prova real e reforce a garantia de 7 dias.",
            "fechando": "O lead está quente. Seja direto, gere urgência e envie o link de pagamento.",
        }
        stage_text = stage_instructions.get(stage, stage_instructions["conexao"])

        messages = [{"role": t["role"], "content": t["content"]} for t in conversation_history[-8:]]

        base_prompt = (
            f"Nome do seguidor: {user_name}\n"
            f"Estágio da conversa: {stage_text}"
            f"{product_context}\n"
            f"{extra_context + chr(10) if extra_context else ''}\n"
            f"Gere uma resposta natural, envolvente e que avance a conversa para a venda."
        )

        if attachment_type == "image" and attachment_url:
            user_content = [
                {"type": "image", "source": {"type": "url", "url": attachment_url}},
                {"type": "text", "text": f"{base_prompt}\n\nMensagem junto à imagem: {user_message or '(sem texto)'}\nAnalise a imagem e responda de forma calorosa."},
            ]
        elif attachment_type in ("audio", "video"):
            tipo = "áudio" if attachment_type == "audio" else "vídeo"
            user_content = f"{base_prompt}\n\nO seguidor enviou um {tipo}. Reconheça com empatia e peça para escrever o que quer dizer."
        else:
            user_content = f"Mensagem recebida: {user_message}\n\n{base_prompt}"

        messages.append({"role": "user", "content": user_content})

        response = await self.async_client.messages.create(
            model=self.model,
            max_tokens=350,
            # Prompt caching no system prompt — bloco de seguros é estável e gigante
            # (~16k chars). Cache reduz latência das chamadas subsequentes em ~50%.
            system=[
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            messages=messages,
        )
        return response.content[0].text.strip()

    def generate_reactivation_message(
        self,
        user_name: str,
        conversation_history: list[dict],
        stage: str = "qualificando",
        hours_silent: int = 24,
    ) -> str:
        last_user_msg = next(
            (m["content"][:120] for m in reversed(conversation_history) if m["role"] == "user"),
            "",
        )
        intensity = "suave e curiosa" if hours_silent < 48 else "direta, com senso de oportunidade real"
        prompt = (
            f"'{user_name}' parou de responder há {hours_silent}h. Estágio: {stage}.\n"
            f"Última mensagem deles: '{last_user_msg}'\n\n"
            f"Crie uma mensagem de reativação {intensity}. "
            "Máximo 2 frases, no estilo Eduardo Prado. Genuína, sem pressão excessiva."
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=80,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    def detect_gender(self, username: str) -> str:
        """Retorna 'male', 'female' ou 'unknown' baseado no username."""
        prompt = (
            f"O username do Instagram é: '{username}'\n\n"
            "Analise o username e responda APENAS com uma palavra: male, female ou unknown."
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=5,
            system="Você classifica gênero por username. Responda somente: male, female ou unknown.",
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text.strip().lower()
        return result if result in {"male", "female"} else "unknown"

    def generate_comment_reply(
        self,
        user_name: str,
        comment_text: str,
        post_caption: str = "",
    ) -> str:
        """Gera resposta para um comentário em um post."""
        prompt = (
            f"Você está respondendo a um comentário no Instagram do Eduardo Prado.\n\n"
            f"Post (legenda): {post_caption[:200] if post_caption else 'Não disponível'}\n"
            f"Comentário de @{user_name}: {comment_text}\n\n"
            "Gere uma resposta curta (máximo 2 linhas), engajadora e que convide a pessoa "
            "a mandar uma DM para saber mais. Seja natural, no tom do Eduardo, não robótico."
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=150,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    def classify_message_stage(
        self,
        user_message: str,
        conversation_history: list[dict],
    ) -> str:
        """Classifica em qual estágio do funil a conversa está."""
        prompt = (
            f"Analise este histórico de conversa e a última mensagem e classifique "
            f"em qual estágio de vendas estamos.\n\n"
            f"Histórico recente:\n"
            + "\n".join(
                f"{t['role']}: {t['content'][:100]}"
                for t in conversation_history[-4:]
            )
            + f"\n\nÚltima mensagem do seguidor: {user_message}\n\n"
            "Responda APENAS com uma dessas palavras exatas: "
            "conexao / qualificando / apresentando / objecao / fechando / frio"
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=10,
            system="Você classifica estágios de vendas. Responda somente com a palavra do estágio.",
            messages=[{"role": "user", "content": prompt}],
        )
        stage = response.content[0].text.strip().lower()
        valid_stages = {"conexao", "qualificando", "apresentando", "objecao", "fechando", "frio"}
        return stage if stage in valid_stages else "qualificando"

    def generate_purchase_followup(
        self,
        user_name: str,
        product_name: str,
        product_link: str,
        hours_since_link: int,
        attempt: int = 1,
    ) -> str:
        if attempt == 1:
            tone = (
                "Tom leve, sem pressão. Pergunta se chegou a ver o conteúdo do link. "
                "Faz uma pergunta sobre o que tranca o negócio dele agora. "
                "Não cita o preço. Máximo 2 frases."
            )
        elif attempt == 2:
            tone = (
                "Tom direto, de empresário que quer ver o outro crescer. "
                "Reforça a dor principal do produto. Cita que a garantia é de 7 dias, risco zero. "
                "Máximo 2 frases."
            )
        else:
            tone = (
                "Última tentativa. Tom de oportunidade real se fechando. "
                "Pergunta direta: o que travou? Preço, tempo, dúvida? "
                "Resolve a objeção em 1 frase. Máximo 2 frases."
            )

        prompt = (
            f"Prospect: {user_name}\n"
            f"Produto enviado: {product_name}\n"
            f"Link: {product_link}\n"
            f"Horas desde que recebeu o link: {hours_since_link}h\n"
            f"Tentativa de follow-up número: {attempt}\n\n"
            f"{tone}\n\n"
            "Escreva o follow-up no tom do Eduardo Prado. Não seja chato. Seja genuíno."
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=100,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    def generate_remarketing_message(
        self,
        user_name: str,
        product_name: str,
        product_link: str,
    ) -> str:
        prompt = (
            f"Prospect: {user_name}\n"
            f"Produto: {product_name}\n"
            f"Link: {product_link}\n\n"
            "Crie uma mensagem de remarketing curta e direta. "
            "A pessoa demonstrou interesse mas não comprou ainda. "
            "Reforce a dor principal do produto em 1 frase. "
            "Mande o link e a garantia de 7 dias. "
            "Máximo 3 frases. Tom Eduardo Prado, direto, sem enrolação. NUNCA use travessão (—) nem meia-trava (–)."
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=120,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    def generate_influencer_followup(
        self,
        user_name: str,
        gift_link: str,
        gender: str = "unknown",
        attempt: int = 1,
    ) -> str:
        treatment = "parceiro" if gender != "female" else "parceira"
        tones = {
            1: f"Lembrete leve sobre o presente enviado. Pergunta se chegou a ver. Máximo 2 frases.",
            2: f"Reforça o valor do presente e o convite para conectar. Máximo 2 frases.",
            3: f"Última tentativa. Tom de oportunidade se fechando. Máximo 1 frase.",
        }
        tone = tones.get(attempt, tones[1])
        prompt = (
            f"Influenciador: {user_name} ({treatment})\n"
            f"Link do presente: {gift_link}\n"
            f"Tentativa de follow-up número: {attempt}\n\n"
            f"{tone}\n\n"
            "Escreva no tom do Eduardo Prado. Genuíno, sem pressão excessiva."
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=80,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    def generate_hub_prospect_dm(
        self,
        user_name: str,
        bio: str,
        gender: str = "unknown",
    ) -> str:
        treatment = "parceiro" if gender != "female" else "parceira"
        prompt = (
            f"Prospect empresário: {user_name} ({treatment})\n"
            f"Bio do Instagram: {bio[:200]}\n\n"
            "Gere uma DM de prospecção sobre o HUB Global Business do Eduardo Prado. "
            "Contextualize para o nicho ou cargo da pessoa baseado na bio. "
            "Seja direto, no tom do Eduardo, máximo 3 frases. "
            "Não mande link ainda, o objetivo é abrir conversa. NUNCA use travessão (—) nem meia-trava (–)."
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=120,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    def generate_meeting_reminder_1h(
        self,
        user_name: str,
        meeting_hour: str,
        meet_link: str,
    ) -> str:
        """1h antes da reunião. Persuasivo, sem ser robótico, com link."""
        link_line = f"Link: {meet_link}" if meet_link else "Link: (segue na agenda da minha assessoria)"
        prompt = (
            f"Lead: {user_name}\n"
            f"Hora da reunião com a assessoria de seguros do Prado: {meeting_hour}\n"
            f"{link_line}\n\n"
            "Escreva uma mensagem CURTA (máx 2 frases) lembrando que falta 1 hora pra reunião. "
            "Tom: cordial, firme, gera presença. Sem motivação vazia. Sem 'tudo bem?'. "
            "Inclua o link e o horário. Brasileiro, WhatsApp, informal mas profissional. "
            "Não use a palavra 'pressão'. Não diga 'última chance'. "
            "NUNCA cite nome próprio de especialista, use 'minha equipe' / 'meu time'. "
            "NUNCA use travessão (—) nem meia-trava (–) na mensagem, use vírgula ou ponto."
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=120,
            system="Você escreve lembretes de reunião curtos, humanos e persuasivos. NUNCA usa emojis em excesso (no máx 1). NUNCA usa o nome do lead mais de uma vez na mensagem. PROIBIDO usar travessão (—) ou meia-trava (–), use vírgula ou ponto.",
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    def generate_meeting_pressure(
        self,
        user_name: str,
        meeting_hour: str,
        meet_link: str,
        attempt: int = 1,
    ) -> str:
        """
        Lead não confirmou a reunião no dia. Pressão persuasiva, até 3 mensagens.
        Cada attempt tem ângulo diferente — não repetir o mesmo argumento.
        """
        link_line = f"Link: {meet_link}" if meet_link else f"Agenda: ver com minha equipe"

        angles = {
            1: (
                "ESCASSEZ. Sua vaga vai pra fila de espera. Pessoas estão aguardando esse horário "
                "pra blindar a família e a empresa. Pergunta se ele vai estar. Tom firme, sem ofensa."
            ),
            2: (
                "CONSEQUÊNCIA. Lembre que sua assessoria (top 1% mundial em planejamento de seguros, "
                "+R$ 1,2 bi em patrimônio blindado) bloqueou 30 min só pra esse lead. "
                "Se não confirmar até o fim do dia, libera a vaga pra próximo. "
                "Pergunta direta: 'me dá um ok aqui'."
            ),
            3: (
                "ÚLTIMA CHAMADA, sem dramatizar. Encerramento educado: 'se não confirmar até X horas "
                "(antes da reunião), libero a vaga pra próximo da fila'. Deixa a porta aberta pra "
                "reagendar no futuro, mas a vaga de hoje vai embora. Curto, 2 frases, firme."
            ),
        }
        angle = angles.get(attempt, angles[1])

        prompt = (
            f"Lead: {user_name}\n"
            f"Horário da reunião com a assessoria de seguros HOJE: {meeting_hour}\n"
            f"{link_line}\n"
            f"Tentativa de cobrança #{attempt} de 3.\n\n"
            f"Ângulo desta mensagem: {angle}\n\n"
            "Escreva 2-3 frases no MÁXIMO. Brasileiro, WhatsApp, informal mas firme. "
            "Sem emoji repetido (máx 1). Sem 'tudo bem?'. Sem clichê motivacional. "
            "Sempre inclua o link/agenda. Use o nome do lead UMA vez no máximo. "
            "NUNCA cite nome próprio de especialista, use 'minha equipe' / 'minha assessoria'. "
            "Mensagem PERSUASIVA, quem está do outro lado precisa sentir que a vaga é finita. "
            "PROIBIDO travessão (—) ou meia-trava (–), use vírgula ou ponto."
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=180,
            system="Você é SDR de seguro de vida. Escreve lembretes de reunião com escassez real, sem ser agressivo. Tom: firme, profissional, brasileiro de WhatsApp.",
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    def should_send_proactive_dm(self, follower_bio: str, follower_name: str) -> tuple[bool, str]:
        """Decide se deve enviar DM proativa para um seguidor baseado no perfil."""
        prompt = (
            f"Perfil do seguidor:\nNome: {follower_name}\nBio: {follower_bio}\n\n"
            "Baseado neste perfil, devemos enviar uma DM proativa de conexão? "
            "Considere: parece ser dono de negócio, vendedor, empresário ou empreendedor?\n\n"
            "Responda em JSON: {\"send\": true/false, \"reason\": \"motivo curto\", "
            "\"opening_message\": \"mensagem de abertura se send=true\"}"
        )
        response = self.client.messages.create(
            model=self.model,
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            result = json.loads(response.content[0].text.strip())
            return result.get("send", False), result.get("opening_message", "")
        except json.JSONDecodeError:
            return False, ""
