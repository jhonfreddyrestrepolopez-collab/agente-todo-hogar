# agent/providers/whapi.py — Adaptador para Whapi.Cloud
# Generado por AgentKit

"""
Proveedor de WhatsApp usando Whapi.Cloud (https://whapi.cloud).

IMPORTANTE: Whapi.Cloud es un proveedor NO oficial (se conecta vía WhatsApp Web).
Meta puede banear el número del canal. Úsalo bajo tu propio riesgo.

Documentación API: https://support.whapi.cloud / https://gate.whapi.cloud
- Enviar texto:  POST {BASE_URL}/messages/text
  Headers: Authorization: Bearer <token>
  Body JSON: {"to": "<numero>", "body": "<texto>"}
- Webhook entrante: Whapi hace POST a tu /webhook con un JSON que contiene
  la lista "messages". No requiere verificación GET.
"""

import os
import logging
import httpx
from collections import deque
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


class ProveedorWhapi(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Whapi.Cloud."""

    def __init__(self):
        self.token = os.getenv("WHAPI_TOKEN")
        self.base_url = os.getenv("WHAPI_BASE_URL", "https://gate.whapi.cloud").rstrip("/")
        # IDs de los mensajes que envía el PROPIO bot. El número de WhatsApp es uno
        # solo, así que cuando el bot responde, Whapi reenvía ese mensaje como un
        # evento from_me=true. Registramos esos IDs para distinguir los ecos del
        # bot de los mensajes que el dueño escribe a mano (esos sí activan modo humano).
        self._ids_bot = deque()        # orden de llegada (para acotar tamaño)
        self._ids_bot_set = set()      # búsqueda rápida por id
        self._MAX_IDS_BOT = 1000

    def _registrar_envio_bot(self, mensaje_id: str) -> None:
        """Recuerda que el bot envió el mensaje con este id (para ignorar su eco)."""
        if not mensaje_id or mensaje_id in self._ids_bot_set:
            return
        self._ids_bot.append(mensaje_id)
        self._ids_bot_set.add(mensaje_id)
        while len(self._ids_bot) > self._MAX_IDS_BOT:
            viejo = self._ids_bot.popleft()
            self._ids_bot_set.discard(viejo)

    def fue_enviado_por_bot(self, mensaje_id: str) -> bool:
        """True si el mensaje (por id) lo envió el propio bot, no un humano."""
        return bool(mensaje_id) and mensaje_id in self._ids_bot_set

    @staticmethod
    def _extraer_id_respuesta(data: dict) -> str:
        """Extrae el id del mensaje recién enviado desde la respuesta de Whapi."""
        if not isinstance(data, dict):
            return ""
        # Whapi puede responder {"message": {"id": ...}} o {"messages": [{"id": ...}]}
        msg = data.get("message")
        if isinstance(msg, dict) and msg.get("id"):
            return msg["id"]
        msgs = data.get("messages")
        if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict):
            return msgs[0].get("id", "")
        return data.get("id", "")

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea el payload JSON de Whapi y normaliza los mensajes de texto."""
        body = await request.json()
        mensajes = []

        for msg in body.get("messages", []):
            # Solo procesamos mensajes de texto
            if msg.get("type") != "text":
                continue

            mensaje_id = msg.get("id", "")
            from_me = bool(msg.get("from_me", False))

            # Si el mensaje es saliente (from_me), hay que distinguir DOS casos:
            #   a) Lo envió el PROPIO bot -> es un eco, lo ignoramos por completo
            #      (si no, el bot se silenciaría a sí mismo al responder).
            #   b) Lo escribió el DUEÑO a mano desde el teléfono -> es_propio=True,
            #      y debe activar el modo humano en ese chat.
            # Detectamos el eco del bot por id conocido o por source == "api".
            if from_me:
                es_eco_bot = (
                    self.fue_enviado_por_bot(mensaje_id)
                    or msg.get("source") == "api"
                )
                if es_eco_bot:
                    continue

            # Identificamos la conversación por el cliente (la otra parte del chat).
            # Usamos chat_id porque es el mismo en ambos sentidos:
            #   - Entrante: chat_id = cliente, from = cliente
            #   - Saliente (from_me): chat_id = cliente, from = NUESTRO número
            # Si usáramos "from" en los mensajes propios, guardaríamos el modo
            # humano con nuestro número y nunca coincidiría con el del cliente.
            chat_id = msg.get("chat_id", "")
            telefono = chat_id.split("@")[0] if "@" in chat_id else chat_id
            if not telefono:
                telefono = msg.get("from", "")

            texto = msg.get("text", {}).get("body", "")

            mensajes.append(MensajeEntrante(
                telefono=telefono,
                texto=texto,
                mensaje_id=mensaje_id,
                es_propio=from_me,  # True solo si lo escribió un humano (ya filtramos ecos)
            ))

        return mensajes

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envía un mensaje de texto via Whapi.Cloud."""
        if not self.token:
            logger.warning("WHAPI_TOKEN no configurado en .env")
            return False

        url = f"{self.base_url}/messages/text"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "accept": "application/json",
        }
        payload = {
            "to": telefono,
            "body": mensaje,
        }

        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code not in (200, 201):
                logger.error(f"Error Whapi: {r.status_code} — {r.text}")
                return False
            # Registramos el id del mensaje enviado por el bot para ignorar su eco.
            try:
                self._registrar_envio_bot(self._extraer_id_respuesta(r.json()))
            except Exception:
                pass
            return True

    async def enviar_imagen(self, telefono: str, image_url: str, caption: str = "") -> bool:
        """Envía una imagen por Whapi.Cloud."""
        if not self.token:
            logger.warning("WHAPI_TOKEN no configurado en .env")
            return False

        url = f"{self.base_url}/messages/image"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "accept": "application/json",
        }
        payload = {
            "to": telefono,
            "media": image_url,
            "caption": caption,
        }

        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code not in (200, 201):
                logger.error(f"Error Whapi imagen: {r.status_code} — {r.text}")
                return False
            # Registramos el id de la imagen enviada por el bot para ignorar su eco.
            try:
                self._registrar_envio_bot(self._extraer_id_respuesta(r.json()))
            except Exception:
                pass
            return True
