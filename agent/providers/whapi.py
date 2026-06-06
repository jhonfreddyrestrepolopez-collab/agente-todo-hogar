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
import time
import logging
import httpx
from collections import deque
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante
from agent.memory import registrar_mensaje_bot, fue_mensaje_de_bot

logger = logging.getLogger("agentkit")

# Valores de "source" que indican que un HUMANO escribió desde un dispositivo
# (no la API, no un sistema automático). Whapi marca con estos los mensajes
# tecleados a mano. El modo humano SOLO se activa con uno de estos.
SOURCES_HUMANOS = {"mobile", "web", "desktop", "ios", "android", "phone"}

# Segundos durante los cuales un mensaje que el bot acaba de enviar se reconoce
# como "eco propio" por coincidencia de contenido (además del id y source=api).
VENTANA_ECO_SEGUNDOS = 180


class ProveedorWhapi(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Whapi.Cloud."""

    def __init__(self):
        self.token = os.getenv("WHAPI_TOKEN")
        self.base_url = os.getenv("WHAPI_BASE_URL", "https://gate.whapi.cloud").rstrip("/")
        # IDs de los mensajes que envía el PROPIO bot. El número de WhatsApp es uno
        # solo, así que cuando el bot responde, Whapi reenvía ese mensaje como un
        # evento from_me=true. Registramos esos IDs para distinguir los ecos del
        # bot de los mensajes que el dueño escribe a mano (esos sí activan modo humano).
        # Se PERSISTEN en la BD (tabla mensajes_bot) para sobrevivir reinicios del
        # contenedor; este set/deque es solo un caché en memoria de acceso rápido.
        self._ids_bot = deque()        # orden de llegada (para acotar tamaño)
        self._ids_bot_set = set()      # búsqueda rápida por id
        self._MAX_IDS_BOT = 1000
        # Contenido recién enviado por el bot: (telefono, texto, timestamp). Sirve
        # para reconocer el eco del propio bot aunque el id no coincida.
        self._envios_recientes = deque()
        # Buffer de diagnóstico de los últimos mensajes from_me (sin texto).
        self._ultimos_from_me = deque(maxlen=30)

    async def _registrar_envio_bot(self, mensaje_id: str) -> None:
        """Recuerda que el bot envió el mensaje con este id (caché + BD persistente)."""
        if not mensaje_id:
            return
        # Caché en memoria (rápido).
        if mensaje_id not in self._ids_bot_set:
            self._ids_bot.append(mensaje_id)
            self._ids_bot_set.add(mensaje_id)
            while len(self._ids_bot) > self._MAX_IDS_BOT:
                viejo = self._ids_bot.popleft()
                self._ids_bot_set.discard(viejo)
        # Persistencia en BD (sobrevive reinicios).
        await registrar_mensaje_bot(mensaje_id)

    def _registrar_envio_contenido(self, telefono: str, texto: str) -> None:
        """Guarda (telefono, texto, ahora) de un mensaje que acaba de enviar el bot."""
        self._envios_recientes.append((telefono, (texto or "").strip(), time.monotonic()))
        # Acotar tamaño.
        while len(self._envios_recientes) > 200:
            self._envios_recientes.popleft()

    def _es_eco_por_contenido(self, telefono: str, texto: str) -> bool:
        """True si el bot envió hace poco un mensaje igual (mismo destino y texto)."""
        ahora = time.monotonic()
        objetivo = (texto or "").strip()
        encontrado = False
        vigentes = deque()
        for tel, txt, ts in self._envios_recientes:
            if ahora - ts <= VENTANA_ECO_SEGUNDOS:
                vigentes.append((tel, txt, ts))  # conservar los recientes
                if tel == telefono and txt == objetivo:
                    encontrado = True
        self._envios_recientes = vigentes
        return encontrado

    def ultimos_from_me(self) -> list[dict]:
        """Metadatos (sin texto) de los últimos from_me, para diagnóstico."""
        return list(self._ultimos_from_me)

    async def fue_enviado_por_bot(self, mensaje_id: str) -> bool:
        """True si el mensaje (por id) lo envió el propio bot, no un humano."""
        if not mensaje_id:
            return False
        # Primero el caché en memoria; si no está (p.ej. tras un reinicio), la BD.
        if mensaje_id in self._ids_bot_set:
            return True
        return await fue_mensaje_de_bot(mensaje_id)

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
            source = (msg.get("source") or "").lower()

            # Identificamos la conversación por el cliente (la otra parte del chat).
            # chat_id es el mismo en ambos sentidos; en mensajes propios "from" es
            # NUESTRO número, por eso no lo usamos como clave.
            chat_id = msg.get("chat_id", "")
            telefono = chat_id.split("@")[0] if "@" in chat_id else chat_id
            if not telefono:
                telefono = msg.get("from", "")

            texto = msg.get("text", {}).get("body", "")

            # ── Mensajes ENTRANTES (del cliente): se procesan normal. ──
            if not from_me:
                mensajes.append(MensajeEntrante(
                    telefono=telefono, texto=texto,
                    mensaje_id=mensaje_id, es_propio=False,
                ))
                continue

            # ── Mensajes SALIENTES (from_me): clasificar con EVIDENCIA POSITIVA. ──
            # El modo humano SOLO se activa si hay prueba clara de que un humano
            # escribió desde un dispositivo. Todo lo demás (eco del bot, API,
            # saludos automáticos de WhatsApp Business, sistema) se IGNORA.
            es_eco_bot = (
                await self.fue_enviado_por_bot(mensaje_id)
                or source == "api"
                or self._es_eco_por_contenido(telefono, texto)
            )
            es_humano = (not es_eco_bot) and (source in SOURCES_HUMANOS)

            # Registrar diagnóstico (sin texto).
            self._ultimos_from_me.append({
                "source": source or "(vacío)",
                "id_conocido_del_bot": await self.fue_enviado_por_bot(mensaje_id),
                "eco_por_contenido": self._es_eco_por_contenido(telefono, texto),
                "clasificacion": "humano" if es_humano else ("eco_bot" if es_eco_bot else "ignorado"),
            })

            if es_humano:
                logger.info(f"Mensaje MANUAL de humano detectado (source={source}) en chat {telefono}")
                mensajes.append(MensajeEntrante(
                    telefono=telefono, texto=texto,
                    mensaje_id=mensaje_id, es_propio=True,
                ))
            else:
                # Ni cliente ni humano operador: no activa modo humano ni se responde.
                logger.info(
                    f"from_me IGNORADO en chat {telefono} "
                    f"(source={source or 'vacío'}, eco_bot={es_eco_bot}): no activa modo humano"
                )

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
            # Registramos el id Y el contenido del mensaje del bot para ignorar su eco.
            self._registrar_envio_contenido(telefono, mensaje)
            try:
                await self._registrar_envio_bot(self._extraer_id_respuesta(r.json()))
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

        # Log de diagnóstico: la URL exacta que se intenta enviar.
        logger.info(f"Whapi enviar_imagen -> {telefono} | URL: {image_url}")

        async with httpx.AsyncClient() as client:
            r = await client.post(url, json=payload, headers=headers)

        # Registramos SIEMPRE la respuesta completa de Whapi (status + cuerpo) para
        # poder diagnosticar fallos de entrega; el cuerpo se recorta a 500 chars.
        cuerpo = r.text
        logger.info(f"Whapi imagen respuesta [{r.status_code}] URL={image_url}: {cuerpo[:500]}")

        if r.status_code not in (200, 201):
            logger.error(f"Error Whapi imagen ({r.status_code}) URL={image_url}: {cuerpo}")
            return False

        # Exigimos una CONFIRMACIÓN REAL de Whapi: "sent": true o un id de mensaje.
        # Antes cualquier HTTP 200 contaba como enviado, aunque Whapi respondiera
        # "sent": false o un cuerpo vacío (por eso se decía "enviado" sin entregar).
        try:
            data = r.json()
        except Exception:
            data = {}
        mensaje_id = self._extraer_id_respuesta(data)
        confirmado = (data.get("sent") is True) or bool(mensaje_id)
        if not confirmado:
            logger.error(
                f"Whapi NO confirmó el envío de la imagen (sent!=true y sin id) "
                f"URL={image_url}: {cuerpo}"
            )
            return False

        # Registramos el id Y el caption de la imagen del bot para ignorar su eco.
        self._registrar_envio_contenido(telefono, caption)
        try:
            await self._registrar_envio_bot(mensaje_id)
        except Exception:
            pass
        return True
