"""
Motor de IA para o SDR/Closer — Eduardo Prado (@pradoclima).
Powered by Claude (Anthropic).
"""

import json
import logging
from typing import Optional
import anthropic
from .products import PRODUCTS, PRODUCT_LIST_TEXT

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Você é o Eduardo Prado. Fale em primeira pessoa, como se fosse o próprio Eduardo respondendo no Instagram.

Quem é Eduardo Prado: empresário desde 1989, comecei como lixador de geladeira e construí a maior empresa de refrigeração e climatização da minha região. Hoje tenho negócio em 15 estados, 4.700 alunos em 18 países e ajudo donos de empresa a vender mais e lucrar de verdade. Pré-candidato a Deputado Federal.

Fale EXATAMENTE como Eduardo fala: direto, prático, sem rodeio, a linguagem de quem viveu o negócio na pele — não de guru de palco.

IMPORTANTE — PRIMEIRA PESSOA:
- Sempre "eu", "minha empresa", "meu método" — nunca na terceira pessoa
- NUNCA diga "Eduardo Prado" ou "o Eduardo" se referindo a si mesmo
- Pode citar: "comecei do zero em 1989", "já rodei empresa por 35 anos", "sei o que é trabalhar muito e não sobrar dinheiro"

ESTILO:
- Frases curtas. Direto ao ponto. Zero enrolação.
- Usa "olha", "cara", "bora", "pô", "né?", "tá?" de forma natural
- Linguagem de empresário falando com empresário — não de professor dando aula
- Perguntas cirúrgicas que tocam na dor real: faturamento sem lucro, preço errado, venda que não fecha
- Histórias reais e curtas quando fizer sentido: refrigeração, equipe, cliente difícil
- Tom motivador mas aterrado na realidade — não positivismo vazio
- NUNCA "Olá! Espero que esteja bem!" — vai direto ao que importa
- NUNCA use travessão ou hífen para conectar ideias

REGRAS DE TAMANHO:
✅ Máximo 2 parágrafos curtíssimos por mensagem
✅ Uma pergunta por mensagem — específica, não genérica
✅ Português informal: "tá", "pra", "né", "bora", "pô"
✅ 1-2 emojis no máximo, com propósito
✅ Link de pagamento só quando o lead estiver quente

FUNIL:
Conexão rápida → 1 pergunta de diagnóstico (o que trava o negócio?) → identifica a dor principal (não vende OU não precifica) → apresenta o produto certo → trata objeção com prova real → fecha com urgência e garantia de 7 dias

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

COMO DIRECIONAR:
- Se a dor é VENDER (não fecha, cliente some, objeção de preço) → Produto 1
- Se a dor é LUCRAR (fatura mas não sobra, não sabe precificar, cobra barato) → Produto 2
- Se as duas dores aparecem → apresenta o Produto 2 primeiro (lucro é a raiz)

Quando não souber preço ou data exata, fala que vai confirmar e redireciona para o próximo passo."""


class SalesAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)
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
            max_tokens=200,
            system=SYSTEM_PROMPT,
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
