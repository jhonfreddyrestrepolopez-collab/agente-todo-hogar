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
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


class ProveedorWhapi(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Whapi.Cloud."""

    def __init__(self):
        self.token = os.getenv("WHAPI_TOKEN")
        self.base_url = os.getenv("WHAPI_BASE_URL", "https://gate.whapi.cloud").rstrip("/")

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea el payload JSON de Whapi y normaliza los mensajes de texto."""
        body = await request.json()
        mensajes = []

        for msg in body.get("messages", []):
            # Solo procesamos mensajes de texto
            if msg.get("type") != "text":
                continue

            # El número del remitente: campo "from" (solo dígitos).
            # Si no viene, lo extraemos del chat_id (ej: "573001112233@s.whatsapp.net")
            telefono = msg.get("from", "")
            if not telefono:
                chat_id = msg.get("chat_id", "")
                telefono = chat_id.split("@")[0] if "@" in chat_id else chat_id

            texto = msg.get("text", {}).get("body", "")

            mensajes.append(MensajeEntrante(
                telefono=telefono,
                texto=texto,
                mensaje_id=msg.get("id", ""),
                es_propio=bool(msg.get("from_me", False)),  # ignoramos los nuestros
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
            return r.status_code in (200, 201)




            async def enviar_imagen(self, telefono: str, image_url: str, caption: str = "") -> bool:
            """Envía una imagen por Whapi.Cloud"""

    if not self.token:
        logger.warning("WHAPI_TOKEN no configurado")
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
        "caption": caption
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)

        if r.status_code not in (200, 201):
            logger.error(f"Error Whapi imagen: {r.status_code} — {r.text}")

        return r.status_code in (200, 201)
async def enviar_imagen(self, telefono: str, image_url: str, caption: str = "") -> bool:
    """Envía una imagen por Whapi.Cloud"""

    if not self.token:
        logger.warning("WHAPI_TOKEN no configurado")
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
        "caption": caption
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)

        if r.status_code not in (200, 201):
            logger.error(f"Error Whapi imagen: {r.status_code} — {r.text}")

        return r.status_code in (200, 201)
