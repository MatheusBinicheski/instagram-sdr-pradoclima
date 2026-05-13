"""
Serviço de reativação de leads frios e campanha de presente para influenciadores.

Reativação: roda a cada hora, envia DM via ManyChat API para quem parou de responder.
Influenciadores: varre seguidores via Instagram Graph API, filtra 100k+ e envia presente.
"""

import logging
import httpx
from typing import Optional

from .conversation_manager import ConversationManager
from .sales_agent import SalesAgent

logger = logging.getLogger(__name__)

MANYCHAT_API = "https://api.manychat.com"
INSTAGRAM_API = "https://graph.facebook.com/v21.0"
INFLUENCER_GIFT_LINK = "https://payfast.greenn.com.br/gqgsk8v"
INFLUENCER_MIN_FOLLOWERS = 100_000

HUB_PROSPECT_KEYWORDS = [
    "ceo", "fundador", "founder", "empresário", "empresaria", "dono", "dona",
    "sócio", "socia", "diretor", "diretora", "gestor", "gestora", "owner",
    "presidente", "empreendedor", "empreendedora", "empresa", "negócio",
    "negocio", "business", "startup", "consultoria", "imobiliária", "imobiliaria",
    "construtora", "incorporadora", "franquia", "holding", "grupo",
]
HUB_PROSPECT_MIN_FOLLOWERS = 1_000


class ReactivationService:
    def __init__(
        self,
        agent: SalesAgent,
        conv_manager: ConversationManager,
        manychat_key: str,
        instagram_token: str = "",
        instagram_account_id: str = "",
    ):
        self.agent = agent
        self.conv_manager = conv_manager
        self.manychat_key = manychat_key
        self.instagram_token = instagram_token
        self.instagram_account_id = instagram_account_id

    def get_all_subscribers(self) -> list[dict]:
        """Busca todos os subscribers ativos no ManyChat via API (paginado)."""
        if not self.manychat_key:
            logger.warning("MANYCHAT_API_KEY não configurada.")
            return []

        headers = {"Authorization": f"Bearer {self.manychat_key}"}
        subscribers = []
        page = 1

        with httpx.Client(timeout=30) as client:
            while True:
                try:
                    resp = client.get(
                        f"{MANYCHAT_API}/fb/subscriber/getAll",
                        headers=headers,
                        params={"status": "active", "page": page, "count": 100},
                    )
                    data = resp.json()
                    batch = data.get("data", [])
                    if not batch:
                        break
                    subscribers.extend(batch)
                    logger.info(f"[MANYCHAT] Página {page}: {len(batch)} subscribers")
                    if len(batch) < 100:
                        break
                    page += 1
                except Exception as e:
                    logger.error(f"[MANYCHAT] Erro ao buscar subscribers página {page}: {e}")
                    break

        logger.info(f"[MANYCHAT] Total de subscribers carregados: {len(subscribers)}")
        return subscribers

    def add_manychat_tag(self, subscriber_id: str, tag_name: str) -> bool:
        """Adiciona uma tag a um subscriber no ManyChat via API."""
        if not self.manychat_key:
            return False
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    f"{MANYCHAT_API}/fb/subscriber/addTag",
                    headers={"Authorization": f"Bearer {self.manychat_key}", "Content-Type": "application/json"},
                    json={"subscriber_id": subscriber_id, "tag_name": tag_name},
                )
                if resp.status_code < 400:
                    logger.info(f"[MANYCHAT TAG] +{tag_name} → {subscriber_id}")
                    return True
                logger.warning(f"[MANYCHAT TAG] Falha {resp.status_code} ao adicionar {tag_name} em {subscriber_id}: {resp.text[:150]}")
                return False
        except Exception as e:
            logger.error(f"[MANYCHAT TAG] Erro: {e}")
            return False

    def remove_manychat_tag(self, subscriber_id: str, tag_name: str) -> bool:
        """Remove uma tag de um subscriber no ManyChat via API."""
        if not self.manychat_key:
            return False
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    f"{MANYCHAT_API}/fb/subscriber/removeTag",
                    headers={"Authorization": f"Bearer {self.manychat_key}", "Content-Type": "application/json"},
                    json={"subscriber_id": subscriber_id, "tag_name": tag_name},
                )
                return resp.status_code < 400
        except Exception as e:
            logger.error(f"[MANYCHAT TAG] Erro ao remover: {e}")
            return False

    def _send_manychat_dm(self, subscriber_id: str, text: str) -> bool:
        if not self.manychat_key:
            logger.warning("MANYCHAT_API_KEY não configurada. DM não enviada.")
            return False
        try:
            payload = {
                "subscriber_id": subscriber_id,
                "data": {
                    "version": "v2",
                    "content": {
                        "type": "instagram",
                        "messages": [{"type": "text", "text": text}],
                        "actions": [],
                        "quick_replies": [],
                    },
                },
            }
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    f"{MANYCHAT_API}/fb/sending/sendContent",
                    headers={
                        "Authorization": f"Bearer {self.manychat_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if resp.status_code >= 400:
                    logger.error(f"[MANYCHAT] Erro HTTP {resp.status_code} para {subscriber_id}: {resp.text[:300]}")
                    return False
                logger.info(f"[MANYCHAT] DM enviada para {subscriber_id}")
                return True
        except Exception as e:
            logger.error(f"[MANYCHAT] Erro ao enviar para {subscriber_id}: {e}")
            return False

    def run_reactivation(self, hours_threshold: int = 24) -> dict:
        """Busca leads frios e envia mensagem de reativação personalizada."""
        cold_leads = self.conv_manager.get_cold_leads(hours=hours_threshold)
        results = {"total_cold": len(cold_leads), "sent": 0, "failed": 0, "skipped": 0}

        for lead in cold_leads:
            user_id = lead["user_id"]
            user_name = lead["user_name"]

            if lead.get("reactivation_count", 0) >= 3:
                results["skipped"] += 1
                continue

            history = self.conv_manager.get_history(user_id)

            # Influenciadores recebem follow-up específico
            if lead.get("is_influencer"):
                attempt = lead.get("reactivation_count", 0) + 1
                message = self.agent.generate_influencer_followup(
                    user_name=user_name,
                    gift_link=lead.get("influencer_gift_link", INFLUENCER_GIFT_LINK),
                    gender=lead.get("influencer_gender", "unknown"),
                    attempt=attempt,
                )
            else:
                message = self.agent.generate_reactivation_message(
                    user_name=user_name,
                    conversation_history=history,
                    stage=lead.get("stage", "qualificando"),
                    hours_silent=lead["hours_since_last_user_message"],
                )

            success = self._send_manychat_dm(user_id, message)
            if success:
                self.conv_manager.add_message(user_id, "assistant", message)
                self.conv_manager.mark_reactivation_sent(user_id)
                results["sent"] += 1
                logger.info(f"[REATIVAÇÃO] Enviado para {user_name} ({user_id})")
            else:
                results["failed"] += 1

        return results

    def run_influencer_scan(self, posts_limit: int = 20) -> dict:
        """
        Varre comentadores dos posts recentes do Pedro via Instagram Graph API.
        Para cada comentador com 100k+ seguidores, envia o link do presente via ManyChat.

        Usa instagram_basic + instagram_manage_comments (sem precisar de permissão restrita).
        Business Discovery verifica follower_count — funciona apenas para contas business/creator.
        """
        if not self.instagram_token or not self.instagram_account_id:
            logger.warning("[INFLUENCER] INSTAGRAM_ACCESS_TOKEN ou INSTAGRAM_ACCOUNT_ID não configurados.")
            return {"error": "Instagram API não configurada."}

        results = {"posts_scanned": 0, "commenters_checked": 0, "influencers_found": 0, "gifts_sent": 0, "errors": 0}
        seen_usernames: set = set()

        with httpx.Client(timeout=30) as client:
            # 1. Busca posts recentes
            media_url = f"{INSTAGRAM_API}/{self.instagram_account_id}/media"
            media_params = {"fields": "id", "limit": posts_limit, "access_token": self.instagram_token}

            try:
                resp = client.get(media_url, params=media_params)
                resp.raise_for_status()
                posts = resp.json().get("data", [])
            except Exception as e:
                logger.error(f"[INFLUENCER] Erro ao buscar posts: {e}")
                return {**results, "errors": 1}

            # 2. Para cada post, varre comentadores
            for post in posts:
                results["posts_scanned"] += 1
                comments_url = f"{INSTAGRAM_API}/{post['id']}/comments"
                comments_params = {"fields": "id,username", "limit": 50, "access_token": self.instagram_token}

                while comments_url:
                    try:
                        resp = client.get(comments_url, params=comments_params)
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as e:
                        logger.error(f"[INFLUENCER] Erro ao buscar comentários do post {post['id']}: {e}")
                        results["errors"] += 1
                        break

                    for comment in data.get("data", []):
                        username = comment.get("username", "")
                        if not username or username in seen_usernames:
                            continue
                        seen_usernames.add(username)
                        results["commenters_checked"] += 1

                        follower_count = self._get_follower_count_by_username(client, username)
                        if follower_count is None or follower_count < INFLUENCER_MIN_FOLLOWERS:
                            continue

                        results["influencers_found"] += 1
                        gender = self.agent.detect_gender(username)
                        message = self.agent.generate_influencer_gift_message(
                            user_name=username,
                            gift_link=INFLUENCER_GIFT_LINK,
                            gender=gender,
                        )

                        ig_id = comment.get("id", "").split("_")[0]
                        if self._send_manychat_dm(ig_id, message):
                            results["gifts_sent"] += 1
                            # Registra no conv_manager para follow-up automático
                            self.conv_manager.get_or_create(ig_id, username)
                            self.conv_manager.add_message(ig_id, "assistant", message)
                            self.conv_manager.update_stage(ig_id, "apresentando")
                            self.conv_manager.mark_influencer(ig_id, gender=gender, gift_link=INFLUENCER_GIFT_LINK)
                            logger.info(f"[INFLUENCER] Presente enviado para @{username} ({follower_count:,} seg, {gender})")

                    paging = data.get("paging", {})
                    comments_url = paging.get("next", "")
                    comments_params = {}

        logger.info(f"[INFLUENCER] Scan concluído: {results}")
        return results

    def send_gift_to_ids(self, subscriber_ids: list[str]) -> dict:
        """Envia presente manualmente para uma lista de subscriber_ids do ManyChat."""
        results = {"sent": 0, "failed": 0}
        for sub_id in subscriber_ids:
            gender = self.agent.detect_gender(sub_id)
            message = self.agent.generate_influencer_gift_message(
                user_name=sub_id,
                gift_link=INFLUENCER_GIFT_LINK,
                gender=gender,
            )
            if self._send_manychat_dm(sub_id, message):
                self.conv_manager.get_or_create(sub_id, sub_id)
                self.conv_manager.add_message(sub_id, "assistant", message)
                self.conv_manager.update_stage(sub_id, "apresentando")
                self.conv_manager.mark_influencer(sub_id, gender=gender, gift_link=INFLUENCER_GIFT_LINK)
                results["sent"] += 1
            else:
                results["failed"] += 1
        return results

    def run_hub_prospect_scan(self, posts_limit: int = 20) -> dict:
        """
        Varre comentadores dos posts recentes, identifica empresários via bio/keywords
        e envia DM de prospecção sobre o HUB Global Business.
        Filtra: bio com keywords de empresário OU 1k+ seguidores.
        Evita recontatar quem já recebeu DM do HUB.
        """
        if not self.instagram_token or not self.instagram_account_id:
            return {"error": "Instagram API não configurada."}

        already_contacted = self.conv_manager.get_hub_prospects_contacted()
        results = {
            "posts_scanned": 0, "commenters_checked": 0,
            "prospects_found": 0, "dms_sent": 0, "errors": 0,
        }
        seen_usernames: set = set()

        with httpx.Client(timeout=30) as client:
            try:
                resp = client.get(
                    f"{INSTAGRAM_API}/{self.instagram_account_id}/media",
                    params={"fields": "id", "limit": posts_limit, "access_token": self.instagram_token},
                )
                resp.raise_for_status()
                posts = resp.json().get("data", [])
            except Exception as e:
                return {**results, "errors": 1, "error": str(e)}

            for post in posts:
                results["posts_scanned"] += 1
                comments_url = f"{INSTAGRAM_API}/{post['id']}/comments"
                comments_params = {"fields": "id,username", "limit": 50, "access_token": self.instagram_token}

                while comments_url:
                    try:
                        resp = client.get(comments_url, params=comments_params)
                        resp.raise_for_status()
                        data = resp.json()
                    except Exception as e:
                        results["errors"] += 1
                        break

                    for comment in data.get("data", []):
                        username = comment.get("username", "")
                        if not username or username in seen_usernames:
                            continue
                        seen_usernames.add(username)

                        ig_id = comment.get("id", "").split("_")[0]
                        if ig_id in already_contacted:
                            continue

                        results["commenters_checked"] += 1
                        profile = self._get_business_profile(client, username)
                        bio = profile.get("biography", "").lower()
                        followers = profile.get("followers_count", 0)

                        is_business = any(kw in bio for kw in HUB_PROSPECT_KEYWORDS)
                        has_followers = followers >= HUB_PROSPECT_MIN_FOLLOWERS

                        if not (is_business or has_followers):
                            continue

                        results["prospects_found"] += 1
                        gender = self.agent.detect_gender(username)
                        message = self.agent.generate_hub_prospect_dm(
                            user_name=username,
                            bio=profile.get("biography", ""),
                            gender=gender,
                        )

                        if self._send_manychat_dm(ig_id, message):
                            results["dms_sent"] += 1
                            self.conv_manager.get_or_create(ig_id, username)
                            self.conv_manager.add_message(ig_id, "assistant", message)
                            self.conv_manager.update_stage(ig_id, "conexao")
                            self.conv_manager.mark_hub_prospect(ig_id, bio=profile.get("biography", ""), username=username)
                            logger.info(f"[HUB PROSPECT] DM → @{username} (seg: {followers}, kw: {is_business})")

                    paging = data.get("paging", {})
                    comments_url = paging.get("next", "")
                    comments_params = {}

        logger.info(f"[HUB PROSPECT] Scan concluído: {results}")
        return results

    def _get_business_profile(self, client: httpx.Client, username: str) -> dict:
        """Busca bio e follower_count via Business Discovery."""
        try:
            resp = client.get(
                f"{INSTAGRAM_API}/{self.instagram_account_id}",
                params={
                    "fields": "business_discovery.fields(biography,followers_count,username)",
                    "username": username,
                    "access_token": self.instagram_token,
                },
                timeout=10,
            )
            return resp.json().get("business_discovery", {})
        except Exception:
            return {}

    def _get_follower_count_by_username(self, client: httpx.Client, username: str) -> Optional[int]:
        """Busca contagem de seguidores pelo username via Business Discovery."""
        try:
            resp = client.get(
                f"{INSTAGRAM_API}/{self.instagram_account_id}",
                params={
                    "fields": f"business_discovery.fields(followers_count,username)",
                    "username": username,
                    "access_token": self.instagram_token,
                },
                timeout=10,
            )
            data = resp.json()
            return data.get("business_discovery", {}).get("followers_count")
        except Exception:
            return None
