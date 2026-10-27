"""Servicio orquestador de lógica de salas para el servidor de Trivia.

Este módulo implementa RoomService, desacoplando la capa de red (TCPServer /
ClientHandler) del modelo de dominio (RoomManager / Room / Player).
Se suscribe a los callbacks del servidor TCP sin alterar el código de transporte.
"""

from __future__ import annotations

import logging
from typing import Any

from src.common.protocol import (
    ErrorCode,
    MessageType,
    create_error_response,
    create_message,
)
from src.server.models import (
    DuplicatePlayerNameError,
    InvalidPlayerNameError,
    InvalidRoomConfigurationError,
    InvalidRoomStateError,
    Player,
    PlayerAlreadyInRoomError,
    Room,
    RoomCodeExhaustionError,
    RoomFullError,
    RoomManager,
    RoomNotFoundError,
)
from src.server.network import ClientHandler, TCPServer

logger = logging.getLogger("TriviaRoomService")


class RoomService:
    """Orquestador de salas y despachador de mensajes de nivel de aplicación.

    Conecta los eventos de red del TCPServer con el RoomManager:
    - Atiende CREATE_ROOM y responde con ROOM_STATE o ERROR.
    - Atiende JOIN_ROOM y difunde ROOM_STATE a todos los participantes de la sala.
    - Atiende desconexiones de clientes, retirando al jugador de la sala y notificando
      el estado actualizado o cerrando la sala si queda vacía.
    """

    def __init__(
        self,
        server: TCPServer,
        room_manager: RoomManager | None = None,
    ) -> None:
        """Inicializa el servicio de salas y suscribe sus manejadores al servidor TCP.

        Args:
            server: Instancia activa o configurada de TCPServer.
            room_manager: Administrador de salas opcional (se crea uno nuevo si es None).
        """
        self.server: TCPServer = server
        self.room_manager: RoomManager = room_manager or RoomManager()

        # Vinculación mediante hooks de callbacks del servidor TCP de Mia
        self.server.on_message_received = self.handle_message
        self.server.on_client_disconnected = self.handle_disconnect

    def handle_message(self, client: ClientHandler, message: dict[str, Any]) -> None:
        """Punto de entrada para el procesamiento de mensajes de aplicación.

        Args:
            client: Manejador del cliente emisor del mensaje.
            message: Diccionario del mensaje ya validado en enmarcado TCP.
        """
        msg_type = message.get("type")
        req_id = message.get("request_id", "")
        payload = message.get("payload")

        if not isinstance(payload, dict):
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.INVALID_PAYLOAD,
                    message="El payload del mensaje debe ser un objeto JSON (diccionario).",
                )
            )
            return

        if msg_type == MessageType.CREATE_ROOM.value:
            self._handle_create_room(client, req_id, payload)
        elif msg_type == MessageType.JOIN_ROOM.value:
            self._handle_join_room(client, req_id, payload)
        else:
            logger.debug(
                "[%s] Mensaje tipo '%s' recibido no corresponde a la gestión de salas (Issue 04).",
                client.client_id,
                msg_type,
            )

    def _handle_create_room(
        self,
        client: ClientHandler,
        req_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Procesa la solicitud CREATE_ROOM de un cliente."""
        player_name = payload.get("player_name")
        if not isinstance(player_name, str) or not player_name.strip():
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.INVALID_PAYLOAD,
                    message="El campo 'player_name' es obligatorio y debe ser una cadena no vacía.",
                )
            )
            return

        raw_room_name = payload.get("room_name")
        if raw_room_name is None:
            room_name = ""
        elif isinstance(raw_room_name, str):
            room_name = raw_room_name.strip()
        else:
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.INVALID_PAYLOAD,
                    message="El campo 'room_name' debe ser de tipo string.",
                )
            )
            return

        raw_max = payload.get("max_players", 4)
        if raw_max is None:
            max_players = 4
        elif isinstance(raw_max, bool) or not isinstance(raw_max, int) or raw_max < 2:
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.INVALID_PAYLOAD,
                    message="El campo 'max_players' debe ser un entero mayor o igual a 2.",
                )
            )
            return
        else:
            max_players = raw_max

        raw_rounds = payload.get("rounds_count", 5)
        if raw_rounds is None:
            rounds_count = 5
        elif isinstance(raw_rounds, bool) or not isinstance(raw_rounds, int) or raw_rounds < 1:
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.INVALID_PAYLOAD,
                    message="El campo 'rounds_count' debe ser un entero mayor o igual a 1.",
                )
            )
            return
        else:
            rounds_count = raw_rounds

        try:
            player = Player(
                player_id=client.client_id,
                name=player_name,
                is_host=True,
            )
            room = self.room_manager.create_room(
                host=player,
                room_name=room_name,
                max_players=max_players,
                rounds_count=rounds_count,
            )
        except PlayerAlreadyInRoomError as err:
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.INVALID_PAYLOAD,
                    message=str(err),
                )
            )
            return
        except RoomCodeExhaustionError as err:
            logger.error("No se pudo generar código de sala: %s", err)
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.INTERNAL_SERVER_ERROR,
                    message="Capacidad de salas colmada; no fue posible generar un código único.",
                )
            )
            return
        except (InvalidPlayerNameError, InvalidRoomConfigurationError) as err:
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.INVALID_PAYLOAD,
                    message=str(err),
                )
            )
            return

        logger.info(
            "[%s] Sala creada con éxito: %s (Host: %s)",
            client.client_id,
            room.room_id,
            player.name,
        )

        # Enviar respuesta formal ROOM_STATE al anfitrión que creó la sala
        room_state = create_message(
            msg_type=MessageType.ROOM_STATE,
            payload=room.to_dict(),
            request_id=req_id,
        )
        client.send_message(room_state)

    def _handle_join_room(
        self,
        client: ClientHandler,
        req_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Procesa la solicitud JOIN_ROOM de un cliente."""
        room_id = payload.get("room_id")
        if not isinstance(room_id, str) or not room_id.strip():
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.INVALID_PAYLOAD,
                    message="El campo 'room_id' es obligatorio y debe ser una cadena no vacía.",
                )
            )
            return

        player_name = payload.get("player_name")
        if not isinstance(player_name, str) or not player_name.strip():
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.INVALID_PAYLOAD,
                    message="El campo 'player_name' es obligatorio y debe ser una cadena no vacía.",
                )
            )
            return

        try:
            player = Player(
                player_id=client.client_id,
                name=player_name,
                is_host=False,
            )
            room = self.room_manager.join_room(room_id=room_id, player=player)
        except RoomNotFoundError as err:
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.ROOM_NOT_FOUND,
                    message=str(err),
                    details={"room_id": room_id},
                )
            )
            return
        except RoomFullError as err:
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.ROOM_FULL,
                    message=str(err),
                    details={"room_id": room_id},
                )
            )
            return
        except InvalidRoomStateError as err:
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.GAME_ALREADY_STARTED,
                    message=str(err),
                    details={"room_id": room_id},
                )
            )
            return
        except DuplicatePlayerNameError as err:
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.INVALID_PAYLOAD,
                    message=str(err),
                    details={"player_name": player_name},
                )
            )
            return
        except PlayerAlreadyInRoomError as err:
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.INVALID_PAYLOAD,
                    message=str(err),
                )
            )
            return
        except (InvalidPlayerNameError, InvalidRoomConfigurationError) as err:
            client.send_message(
                create_error_response(
                    request_id=req_id,
                    code=ErrorCode.INVALID_PAYLOAD,
                    message=str(err),
                )
            )
            return

        logger.info(
            "[%s] Jugador '%s' ingresó a la sala '%s'. Total: %d",
            client.client_id,
            player.name,
            room.room_id,
            room.current_players_count,
        )

        # Notificar el nuevo estado ROOM_STATE a todos los miembros de la sala
        self._broadcast_room_state(
            room=room,
            joining_client_id=client.client_id,
            joining_request_id=req_id,
        )

    def _broadcast_room_state(
        self,
        room: Room,
        joining_client_id: str | None = None,
        joining_request_id: str | None = None,
    ) -> None:
        """Difunde el mensaje formal ROOM_STATE a todos los miembros de una sala.

        Utiliza room.broadcast_lock para serializar el orden de emisión de estados,
        garantizando que eventos concurrentes no emitan notificaciones desordenadas.
        Las operaciones de red se ejecutan sin retener los cerrojos de datos de sala
        ni el cerrojo global del RoomManager.
        """
        with room.broadcast_lock:
            payload_data = room.to_dict()

            for player in room.get_players():
                target_handler = self.server.get_client(player.player_id)
                if target_handler is None or not target_handler.is_running:
                    continue

                if player.player_id == joining_client_id and joining_request_id:
                    msg = create_message(
                        msg_type=MessageType.ROOM_STATE,
                        payload=payload_data,
                        request_id=joining_request_id,
                    )
                else:
                    msg = create_message(
                        msg_type=MessageType.ROOM_STATE,
                        payload=payload_data,
                    )

                target_handler.send_message(msg)

    def handle_disconnect(self, client: ClientHandler) -> None:
        """Gestiona la desconexión de un cliente, asegurando la consistencia de las salas.

        Si el participante pertenecía a una sala activa, lo retira. Si la sala queda
        vacía, se elimina del registro; si aún contiene participantes, difunde la
        lista actualizada de participantes y nuevo anfitrión si correspondiera.
        """
        room, removed = self.room_manager.leave_room(client.client_id)
        if room is None or removed is None:
            return

        logger.info(
            "[%s] Jugador '%s' desconectado de la sala '%s'.",
            client.client_id,
            removed.name,
            room.room_id,
        )

        # Si aún quedan jugadores en la sala, notificarles el nuevo ROOM_STATE
        if not room.is_empty():
            self._broadcast_room_state(room)
