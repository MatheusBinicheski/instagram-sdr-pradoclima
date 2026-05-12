"""
Gerencia o estado e histórico de cada conversa de venda.
Persiste em JSON local para sobreviver a reinicializações.
"""

import json
import os
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "conversations.json")


class ConversationManager:
    def __init__(self):
        self.conversations: dict = {}
        self._load()

    def _load(self):
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    self.conversations = json.load(f)
                logger.info(f"Carregadas {len(self.conversations)} conversas do arquivo.")
            except Exception as e:
                logger.error(f"Erro ao carregar conversas: {e}")
                self.conversations = {}

    def _save(self):
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(self.conversations, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            logger.error(f"Erro ao salvar conversas: {e}")

    def get_or_create(self, user_id: str, user_name: str) -> dict:
        if user_id not in self.conversations:
            self.conversations[user_id] = {
                "user_id": user_id,
                "user_name": user_name,
                "stage": "conexao",
                "history": [],
                "product_recommended": None,
                "link_sent": False,
                "created_at": datetime.now().isoformat(),
                "last_interaction": datetime.now().isoformat(),
                "last_user_message_at": None,
                "last_reactivation_at": None,
                "reactivation_count": 0,
                "total_messages": 0,
                "status": "ativo",
            }
            self._save()
        return self.conversations[user_id]

    def add_message(self, user_id: str, role: str, content: str):
        """role: 'user' ou 'assistant'"""
        conv = self.conversations.get(user_id)
        if not conv:
            return
        conv["history"].append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        })
        conv["last_interaction"] = datetime.now().isoformat()
        if role == "user":
            conv["last_user_message_at"] = datetime.now().isoformat()
        conv["total_messages"] += 1
        if len(conv["history"]) > 40:  # mantém só as últimas 40 mensagens
            conv["history"] = conv["history"][-40:]
        self._save()

    def update_stage(self, user_id: str, stage: str):
        if user_id in self.conversations:
            self.conversations[user_id]["stage"] = stage
            self._save()

    def mark_link_sent(self, user_id: str, product_id: str):
        if user_id in self.conversations:
            self.conversations[user_id]["link_sent"] = True
            self.conversations[user_id]["product_recommended"] = product_id
            self._save()

    def get_cold_leads(self, hours: int = 24) -> list[dict]:
        """Retorna leads que pararam de responder há mais de `hours` horas."""
        result = []
        cutoff = datetime.now() - timedelta(hours=hours)
        reactivation_cooldown = datetime.now() - timedelta(hours=72)

        for conv in self.conversations.values():
            if conv.get("status") in ("vendido", "frio"):
                continue
            last_user_msg = conv.get("last_user_message_at")
            if not last_user_msg:
                continue
            last_user_dt = datetime.fromisoformat(last_user_msg)
            if last_user_dt > cutoff:
                continue
            history = conv.get("history", [])
            if not history or history[-1]["role"] != "assistant":
                continue
            last_react = conv.get("last_reactivation_at")
            if last_react and datetime.fromisoformat(last_react) > reactivation_cooldown:
                continue
            hours_since = int((datetime.now() - last_user_dt).total_seconds() / 3600)
            result.append({**conv, "hours_since_last_user_message": hours_since})

        return result

    def mark_influencer(self, user_id: str, gender: str = "unknown", gift_link: str = ""):
        if user_id in self.conversations:
            self.conversations[user_id]["is_influencer"] = True
            self.conversations[user_id]["influencer_gender"] = gender
            self.conversations[user_id]["influencer_gift_link"] = gift_link
            self._save()

    def mark_reactivation_sent(self, user_id: str):
        if user_id in self.conversations:
            self.conversations[user_id]["last_reactivation_at"] = datetime.now().isoformat()
            self.conversations[user_id]["reactivation_count"] = (
                self.conversations[user_id].get("reactivation_count", 0) + 1
            )
            self._save()

    def mark_sold(self, user_id: str):
        if user_id in self.conversations:
            self.conversations[user_id]["status"] = "vendido"
            self.conversations[user_id]["stage"] = "fechado"
            self.conversations[user_id]["sold_at"] = datetime.now().isoformat()
            self._save()

    def mark_cold(self, user_id: str):
        if user_id in self.conversations:
            self.conversations[user_id]["status"] = "frio"
            self._save()

    def mark_hub_prospect(self, user_id: str, bio: str = "", username: str = ""):
        if user_id in self.conversations:
            self.conversations[user_id]["is_hub_prospect"] = True
            self.conversations[user_id]["hub_bio"] = bio
            self._save()

    def store_prospect_email(self, user_id: str, email: str):
        if user_id in self.conversations:
            self.conversations[user_id]["prospect_email"] = email
            self._save()

    def mark_meeting_scheduled(self, user_id: str, meet_link: str, event_time: str):
        if user_id in self.conversations:
            self.conversations[user_id]["meeting_scheduled"] = True
            self.conversations[user_id]["meeting_meet_link"] = meet_link
            self.conversations[user_id]["meeting_time"] = event_time
            self.conversations[user_id]["stage"] = "reuniao_agendada"
            self._save()

    def get_hub_prospects_contacted(self) -> set:
        return {uid for uid, c in self.conversations.items() if c.get("is_hub_prospect")}

    def get_history(self, user_id: str) -> list[dict]:
        conv = self.conversations.get(user_id, {})
        return [
            {"role": m["role"], "content": m["content"]}
            for m in conv.get("history", [])
        ]

    def get_stage(self, user_id: str) -> str:
        return self.conversations.get(user_id, {}).get("stage", "conexao")

    def should_respond(self, user_id: str, cooldown_minutes: int = 5) -> bool:
        """Evita responder a mesma pessoa duas vezes em rápida sucessão."""
        conv = self.conversations.get(user_id)
        if not conv:
            return True
        last = conv.get("last_interaction")
        if not last:
            return True
        last_dt = datetime.fromisoformat(last)
        return datetime.now() - last_dt > timedelta(minutes=cooldown_minutes)

    def get_stats(self) -> dict:
        total = len(self.conversations)
        by_stage = {}
        by_status = {}
        for conv in self.conversations.values():
            stage = conv.get("stage", "desconhecido")
            status = conv.get("status", "ativo")
            by_stage[stage] = by_stage.get(stage, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1
        return {
            "total_conversas": total,
            "por_estagio": by_stage,
            "por_status": by_status,
        }
