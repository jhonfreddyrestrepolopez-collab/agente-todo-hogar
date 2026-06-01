# agent/memory.py — Memoria de conversaciones con SQLite
# Generado por AgentKit

"""
Sistema de memoria del agente. Guarda el historial de conversaciones
por número de teléfono usando SQLite (local) o PostgreSQL (producción).
"""

import os
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, select, delete, Integer
from dotenv import load_dotenv

load_dotenv()

# Configuración de base de datos
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

# Si es PostgreSQL en producción, ajustar el esquema de URL
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Modelo de mensaje en la base de datos."""
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" o "assistant"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChatEstado(Base):
    """
    Estado por chat. Aquí guardamos el "modo humano" (human_takeover):
    cuando el dueño del negocio responde manualmente, el bot se silencia
    en ese chat hasta `human_takeover_until` (fecha en UTC).
    """
    __tablename__ = "chat_estado"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    human_takeover_until: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class MensajeBot(Base):
    """
    IDs de los mensajes que envió el PROPIO bot. Whapi reenvía por webhook los
    mensajes salientes (from_me=true), así que registramos aquí los que envía el
    bot para distinguir su eco de los mensajes que el dueño escribe a mano.
    Se persiste en la BD para que sobreviva reinicios del contenedor.
    """
    __tablename__ = "mensajes_bot"

    mensaje_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# Horas que dura el modo humano desde el último mensaje del dueño.
HORAS_MODO_HUMANO = 24


async def activar_modo_humano(telefono: str, horas: int = HORAS_MODO_HUMANO):
    """
    Activa (o extiende) el modo humano para un chat: el bot quedará en silencio
    hasta dentro de `horas` horas a partir de AHORA. Llamar cada vez que el dueño
    envía un mensaje reinicia el contador a 24 horas desde ese último mensaje.
    """
    nueva_fecha = datetime.utcnow() + timedelta(hours=horas)
    async with async_session() as session:
        estado = await session.get(ChatEstado, telefono)
        if estado is None:
            estado = ChatEstado(telefono=telefono, human_takeover_until=nueva_fecha)
            session.add(estado)
        else:
            estado.human_takeover_until = nueva_fecha
        await session.commit()
    return nueva_fecha


async def modo_humano_activo(telefono: str) -> bool:
    """
    Devuelve True si el chat está en modo humano (el bot debe permanecer en
    silencio). Si ya pasaron las 24 horas desde el último mensaje del dueño,
    retorna False automáticamente (reactivación del bot sin tarea programada).
    """
    async with async_session() as session:
        estado = await session.get(ChatEstado, telefono)
        if estado is None or estado.human_takeover_until is None:
            return False
        return datetime.utcnow() < estado.human_takeover_until


# Días que conservamos los IDs de mensajes del bot (solo se necesitan unos
# segundos, hasta que llega el eco por webhook; 2 días es un margen amplio).
DIAS_RETENER_IDS_BOT = 2


async def registrar_mensaje_bot(mensaje_id: str):
    """Persiste en la BD el id de un mensaje enviado por el bot (para ignorar su eco)."""
    if not mensaje_id:
        return
    async with async_session() as session:
        if await session.get(MensajeBot, mensaje_id) is None:
            session.add(MensajeBot(mensaje_id=mensaje_id, timestamp=datetime.utcnow()))
        # Limpieza: borramos ids viejos para que la tabla no crezca indefinidamente.
        limite = datetime.utcnow() - timedelta(days=DIAS_RETENER_IDS_BOT)
        await session.execute(delete(MensajeBot).where(MensajeBot.timestamp < limite))
        await session.commit()


async def fue_mensaje_de_bot(mensaje_id: str) -> bool:
    """True si el id corresponde a un mensaje enviado por el bot (consulta a la BD)."""
    if not mensaje_id:
        return False
    async with async_session() as session:
        return await session.get(MensajeBot, mensaje_id) is not None


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de conversación."""
    async with async_session() as session:
        mensaje = Mensaje(
            telefono=telefono,
            role=role,
            content=content,
            timestamp=datetime.utcnow()
        )
        session.add(mensaje)
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Recupera los últimos N mensajes de una conversación.

    Args:
        telefono: Número de teléfono del cliente
        limite: Máximo de mensajes a recuperar (default: 20)

    Returns:
        Lista de diccionarios con role y content
    """
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()

        # Invertir para orden cronológico (los más recientes están primero)
        mensajes = list(mensajes)
        mensajes.reverse()

        return [
            {"role": msg.role, "content": msg.content}
            for msg in mensajes
        ]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversación."""
    async with async_session() as session:
        query = select(Mensaje).where(Mensaje.telefono == telefono)
        result = await session.execute(query)
        mensajes = result.scalars().all()
        for msg in mensajes:
            await session.delete(msg)
        await session.commit()
