"""Protocolo de comunicación cliente-servidor para Trivia Multijugador.

Este módulo define la especificación, validación, serialización y
deserialización de mensajes JSON delimitados por saltos de línea (\\n) sobre TCP.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any


class MessageType(StrEnum):
    """Catálogo formal de tipos de mensajes del protocolo."""

    # Conexión y Diagnóstico
    WELCOME = "WELCOME"
    PING = "PING"
    PONG = "PONG"
    ERROR = "ERROR"

    # Salas (Lobby)
    CREATE_ROOM = "CREATE_ROOM"
    JOIN_ROOM = "JOIN_ROOM"
    ROOM_STATE = "ROOM_STATE"
    START_GAME = "START_GAME"

    # Partida y Preguntas
    QUESTION = "QUESTION"
    SUBMIT_ANSWER = "SUBMIT_ANSWER"
    ANSWER_RECEIVED = "ANSWER_RECEIVED"
    ROUND_RESULT = "ROUND_RESULT"

    # Resultados y Puntuación
    SCORE_UPDATE = "SCORE_UPDATE"
    GAME_OVER = "GAME_OVER"

    # Desconexión y Salida
    PLAYER_LEFT = "PLAYER_LEFT"

    @classmethod
    def has_value(cls, value: str) -> bool:
        """Verifica si un valor string corresponde a un MessageType válido."""
        return value in cls._value2member_map_


class ErrorCode(StrEnum):
    """Catálogo formal de códigos de error estructurados."""

    MALFORMED_JSON = "MALFORMED_JSON"
    INVALID_ENVELOPE = "INVALID_ENVELOPE"
    UNKNOWN_MESSAGE_TYPE = "UNKNOWN_MESSAGE_TYPE"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    ROOM_NOT_FOUND = "ROOM_NOT_FOUND"
    ROOM_FULL = "ROOM_FULL"
    GAME_ALREADY_STARTED = "GAME_ALREADY_STARTED"
    NOT_HOST = "NOT_HOST"
    ALREADY_ANSWERED = "ALREADY_ANSWERED"
    TIME_EXPIRED = "TIME_EXPIRED"
    INVALID_ROUND_STATE = "INVALID_ROUND_STATE"
    PLAYER_NOT_IN_ROOM = "PLAYER_NOT_IN_ROOM"
    INTERNAL_SERVER_ERROR = "INTERNAL_SERVER_ERROR"

    @classmethod
    def has_value(cls, value: str) -> bool:
        """Verifica si un valor string corresponde a un ErrorCode válido."""
        return value in cls._value2member_map_


class ProtocolError(Exception):
    """Excepción base para violaciones del protocolo de comunicación."""

    def __init__(self, message: str, code: ErrorCode | str = ErrorCode.INVALID_ENVELOPE) -> None:
        super().__init__(message)
        self.code = code if isinstance(code, str) else code.value


class MalformedMessageError(ProtocolError):
    """Excepción lanzada cuando una trama recibida no es un JSON válido."""

    def __init__(self, message: str = "Trama JSON malformada o corrupta.") -> None:
        super().__init__(message, code=ErrorCode.MALFORMED_JSON)


class InvalidEnvelopeError(ProtocolError):
    """Excepción lanzada cuando el envoltorio del mensaje no cumple con la especificación."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.INVALID_ENVELOPE)


@dataclass(slots=True)
class Message:
    """Representación fuertemente tipada de un mensaje del protocolo."""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        if isinstance(self.type, Enum):
            self.type = self.type.value
        validate_envelope(
            {
                "type": self.type,
                "request_id": self.request_id,
                "payload": self.payload,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        """Convierte la instancia a un diccionario serializable."""
        return {
            "type": self.type,
            "request_id": self.request_id,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        """Crea una instancia de Message a partir de un diccionario validado."""
        validate_envelope(data)
        return cls(
            type=data["type"],
            request_id=data["request_id"],
            payload=data["payload"],
        )

    def encode(self) -> bytes:
        """Serializa la instancia a bytes con delimitador \\n."""
        return encode_message(self)


def validate_envelope(data: Any) -> None:
    """Valida que los datos recibidos cumplan con el contrato de envoltorio.

    El envoltorio requiere:
    - data es un dict.
    - 'type' presente y es un string no vacío.
    - 'request_id' presente y es un string no vacío.
    - 'payload' presente y es un diccionario (objeto JSON).
    """
    if not isinstance(data, dict):
        raise InvalidEnvelopeError(
            f"El mensaje raíz debe ser un objeto JSON (dict), no {type(data).__name__}."
        )

    if "type" not in data:
        raise InvalidEnvelopeError("El campo 'type' es obligatorio en el envoltorio.")
    if not isinstance(data["type"], str) or not data["type"].strip():
        raise InvalidEnvelopeError("El campo 'type' debe ser un string no vacío.")

    if "request_id" not in data:
        raise InvalidEnvelopeError("El campo 'request_id' es obligatorio en el envoltorio.")
    if not isinstance(data["request_id"], str) or not data["request_id"].strip():
        raise InvalidEnvelopeError("El campo 'request_id' debe ser un string no vacío.")

    if "payload" not in data:
        raise InvalidEnvelopeError("El campo 'payload' es obligatorio en el envoltorio.")
    if not isinstance(data["payload"], dict):
        raise InvalidEnvelopeError(
            f"El campo 'payload' debe ser un diccionario JSON, no {type(data['payload']).__name__}."
        )


def encode_message(
    message: Message | dict[str, Any] | None = None,
    *,
    msg_type: str | MessageType | None = None,
    payload: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> bytes:
    """Serializa un mensaje a formato JSON codificado en UTF-8 y terminado en \\n.

    Puede invocarse pasando una instancia de `Message`, un `dict`, o mediante
    argumentos por palabra clave.

    Args:
        message: Instancia de Message o diccionario con type, request_id y payload.
        msg_type: Tipo de mensaje (si no se provee message).
        payload: Diccionario de datos asociados al mensaje.
        request_id: Identificador de solicitud opcional (se genera UUID si es None).

    Returns:
        bytes: Datos serializados en UTF-8 y terminados con b'\\n'.

    Raises:
        InvalidEnvelopeError: Si el mensaje no cumple las reglas del envoltorio.
    """
    if message is not None:
        if isinstance(message, Message):
            envelope = message.to_dict()
        elif isinstance(message, dict):
            # Clonamos para no mutar el diccionario recibido
            envelope = dict(message)
            if "request_id" not in envelope or not envelope["request_id"]:
                envelope["request_id"] = uuid.uuid4().hex
            if "payload" not in envelope or envelope["payload"] is None:
                envelope["payload"] = {}
        else:
            raise InvalidEnvelopeError(f"Tipo de mensaje no admitido: {type(message).__name__}")
    else:
        if msg_type is None:
            raise InvalidEnvelopeError("Debe suministrarse 'message' o 'msg_type'.")
        raw_type = msg_type.value if isinstance(msg_type, Enum) else msg_type
        envelope = {
            "type": raw_type,
            "request_id": request_id or uuid.uuid4().hex,
            "payload": payload if payload is not None else {},
        }

    validate_envelope(envelope)

    json_str = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return json_str.encode("utf-8") + b"\n"


def decode_messages(buffer: str | bytes) -> tuple[list[dict[str, Any]], str]:
    """Deserializa mensajes JSON delimitados por \\n desde un búfer acumulado.

    Maneja la fragmentación y concatenación del flujo TCP:
    - Si llegan múltiples mensajes juntos ('msg1\\nmsg2\\n'), deserializa todos.
    - Si el último mensaje está incompleto ('...msg3_parcial'), preserva el remanente
      en el segundo elemento de la tupla retornada para ser completado en recepciones futuras.
    - Omite líneas vacías causadas por saltos de línea consecutivos.

    Args:
        buffer: Cadena de texto o bytes acumulados recibidos del socket.

    Returns:
        tuple[list[dict[str, Any]], str]:
            - Lista de diccionarios validados representando los mensajes completos.
            - Remanente del búfer (string) que aún no ha recibido su delimitador '\\n'.

    Raises:
        MalformedMessageError: Si alguna de las líneas completas no es JSON sintácticamente válido.
        InvalidEnvelopeError: Si algún mensaje no respeta la estructura de envoltorio requerida.
    """
    if isinstance(buffer, bytes):
        buffer_str = buffer.decode("utf-8", errors="replace")
    else:
        buffer_str = buffer

    if not buffer_str:
        return [], ""

    if "\n" not in buffer_str:
        # No hay ningún mensaje completo aún
        return [], buffer_str

    parts = buffer_str.split("\n")
    # El último elemento es lo que queda después del último '\n' (completo o remanente incompleto)
    remaining_buffer = parts[-1]
    complete_lines = parts[:-1]

    messages: list[dict[str, Any]] = []
    for line in complete_lines:
        line = line.strip("\r").strip()
        if not line:
            continue

        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as err:
            raise MalformedMessageError(f"JSON inválido ({err.msg}): {line}") from err

        validate_envelope(parsed)
        messages.append(parsed)

    return messages, remaining_buffer


def decode_to_messages(buffer: str | bytes) -> tuple[list[Message], str]:
    """Variante tipada de `decode_messages` que retorna instancias de `Message`."""
    raw_messages, remaining = decode_messages(buffer)
    typed_messages = [Message.from_dict(m) for m in raw_messages]
    return typed_messages, remaining


class ProtocolHandler:
    """Gestiona el búfer de flujo TCP y el enmarcado de mensajes para una conexión."""

    def __init__(self) -> None:
        self._buffer: str = ""

    @property
    def buffer(self) -> str:
        """Contenido actual del búfer remanente no terminado."""
        return self._buffer

    def feed(self, data: bytes | str) -> list[dict[str, Any]]:
        """Incorpora datos recibidos y retorna los mensajes completos deserializados.

        Args:
            data: Fragmento de bytes o texto recibido desde el socket.

        Returns:
            list[dict[str, Any]]: Lista de mensajes JSON validados y completos.
        """
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="replace")
        else:
            text = data

        self._buffer += text
        messages, self._buffer = decode_messages(self._buffer)
        return messages

    def clear(self) -> None:
        """Limpia el búfer remanente."""
        self._buffer = ""


# --- Funciones Auxiliares de Construcción de Mensajes ---


def create_message(
    msg_type: str | MessageType,
    payload: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Crea un diccionario de mensaje válido y listo para serializar o enviar."""
    raw_type = msg_type.value if isinstance(msg_type, Enum) else str(msg_type)
    req_id = request_id or uuid.uuid4().hex
    data = {
        "type": raw_type,
        "request_id": req_id,
        "payload": payload if payload is not None else {},
    }
    validate_envelope(data)
    return data


def create_error_response(
    request_id: str,
    code: str | ErrorCode,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construye un mensaje de error estructurado en respuesta a una solicitud."""
    err_code = code.value if isinstance(code, Enum) else str(code)
    payload: dict[str, Any] = {
        "code": err_code,
        "message": message,
    }
    if details is not None:
        payload["details"] = details

    return create_message(
        msg_type=MessageType.ERROR,
        payload=payload,
        request_id=request_id,
    )


def create_ping(request_id: str | None = None, timestamp: float | None = None) -> dict[str, Any]:
    """Construye un mensaje PING de verificación de latencia / heartbeat."""
    import time

    return create_message(
        msg_type=MessageType.PING,
        payload={"timestamp": timestamp if timestamp is not None else time.time()},
        request_id=request_id,
    )


def create_pong(request_id: str, original_timestamp: float | int | None = None) -> dict[str, Any]:
    """Construye un mensaje PONG en respuesta a un PING."""
    payload: dict[str, Any] = {}
    if original_timestamp is not None:
        payload["original_timestamp"] = original_timestamp

    return create_message(
        msg_type=MessageType.PONG,
        payload=payload,
        request_id=request_id,
    )
