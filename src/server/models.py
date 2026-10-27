"""Modelos de dominio y gestión de salas para Trivia Multijugador.

Este módulo implementa las entidades centrales de la lógica de salas:
- Player: Representación de un jugador participante.
- RoomStatus: Estados del ciclo de vida de una sala (WAITING, IN_GAME, FINISHED).
- Room: Gestión atómica y sincronizada de una sala de juego.
- RoomManager: Registro centralizado thread-safe de salas y mapeo de jugadores.
- Excepciones de dominio asociadas a violaciones de reglas de negocio.
"""

from __future__ import annotations

import dataclasses
import random
import string
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

# --- Excepciones de Dominio ---


class RoomError(Exception):
    """Excepción base para errores relacionados con el motor de salas."""


class InvalidRoomConfigurationError(RoomError):
    """La configuración de la sala no cumple con los límites requeridos."""


class RoomNotFoundError(RoomError):
    """La sala solicitada no existe o ya fue cerrada."""


class RoomFullError(RoomError):
    """La sala ha alcanzado su capacidad máxima de participantes."""


class InvalidRoomStateError(RoomError):
    """La sala no admite nuevos jugadores debido a su estado actual (ej. IN_GAME)."""


class DuplicatePlayerNameError(RoomError):
    """Ya existe un participante con el mismo apodo en la sala."""


class PlayerAlreadyInRoomError(RoomError):
    """El jugador ya se encuentra registrado en una sala activa."""


class InvalidPlayerNameError(RoomError):
    """El apodo suministrado es inválido (vacío o formato erróneo)."""


class RoomCodeExhaustionError(RoomError):
    """Se agotó el espacio de códigos únicos disponibles para nuevas salas."""


# --- Entidades de Dominio ---


class RoomStatus(StrEnum):
    """Estados posibles del ciclo de vida de una sala."""

    WAITING = "WAITING"
    IN_GAME = "IN_GAME"
    FINISHED = "FINISHED"


@dataclass(slots=True)
class Player:
    """Entidad que representa a un participante en una sala de Trivia.

    Atributos:
        player_id: Identificador único del jugador (alineado 1:1 con el client_id del socket).
        name: Apodo visible del jugador en la partida.
        is_host: True si el jugador es el anfitrión/creador de la sala; False en caso contrario.
        is_ready: Estado de preparación del participante en el lobby.
        score: Puntaje acumulado en la partida.
    """

    player_id: str
    name: str
    is_host: bool = False
    is_ready: bool = True
    score: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.player_id, str) or not self.player_id.strip():
            raise InvalidPlayerNameError("El ID del jugador debe ser un string no vacío.")
        if not isinstance(self.name, str):
            raise InvalidPlayerNameError("El apodo del jugador debe ser una cadena de texto.")
        clean_name = self.name.strip()
        if not clean_name:
            raise InvalidPlayerNameError("El apodo del jugador no puede estar vacío.")
        self.name = clean_name

    def to_dict(self) -> dict[str, Any]:
        """Serializa la entidad según el esquema formal de participantes en ROOM_STATE."""
        return {
            "player_id": self.player_id,
            "name": self.name,
            "is_host": self.is_host,
            "is_ready": self.is_ready,
        }


class Room:
    """Representa una sala de juego individual y administra sus participantes.

    Aplica sincronización de grano fino mediante un Lock interno para garantizar
    operaciones atómicas de ingreso, egreso y actualización de participantes.
    """

    def __init__(
        self,
        room_id: str,
        room_name: str,
        host_id: str,
        max_players: int = 4,
        rounds_count: int = 5,
    ) -> None:
        if isinstance(max_players, bool) or not isinstance(max_players, int) or max_players < 2:
            raise InvalidRoomConfigurationError(
                f"El valor de max_players debe ser un entero >= 2 (recibido: {max_players!r})."
            )
        if isinstance(rounds_count, bool) or not isinstance(rounds_count, int) or rounds_count < 1:
            raise InvalidRoomConfigurationError(
                f"El valor de rounds_count debe ser un entero >= 1 (recibido: {rounds_count!r})."
            )

        self.room_id: str = room_id.strip().upper()
        self.room_name: str = room_name.strip() or f"Sala {self.room_id}"
        self.host_id: str = host_id
        self.max_players: int = max_players
        self.rounds_count: int = rounds_count
        self.status: RoomStatus = RoomStatus.WAITING

        self._players: dict[str, Player] = {}
        self._lock = threading.Lock()
        self.broadcast_lock = threading.Lock()
        self.version: int = 1

    @property
    def current_players_count(self) -> int:
        """Cantidad actual de jugadores en la sala."""
        with self._lock:
            return len(self._players)

    def is_full(self) -> bool:
        """Indica si la sala ha alcanzado su capacidad máxima."""
        with self._lock:
            return len(self._players) >= self.max_players

    def is_empty(self) -> bool:
        """Indica si la sala no posee jugadores."""
        with self._lock:
            return len(self._players) == 0

    def has_player(self, player_id: str) -> bool:
        """Verifica si un jugador identificado por su ID está en la sala."""
        with self._lock:
            return player_id in self._players

    def has_player_name(self, name: str) -> bool:
        """Verifica de forma insensible a mayúsculas si ya existe un apodo en uso."""
        normalized = name.strip().casefold()
        with self._lock:
            return any(p.name.casefold() == normalized for p in self._players.values())

    def add_player(self, player: Player) -> None:
        """Añade un jugador a la sala de forma atómica y sincronizada.

        Raises:
            InvalidRoomStateError: Si la sala no está en estado WAITING.
            RoomFullError: Si la sala completó su cupo máximo.
            DuplicatePlayerNameError: Si ya existe un participante con el mismo apodo.
        """
        with self._lock:
            if self.status != RoomStatus.WAITING:
                raise InvalidRoomStateError(
                    f"La sala '{self.room_id}' no admite nuevos participantes "
                    f"(estado: {self.status.value})."
                )

            if len(self._players) >= self.max_players:
                raise RoomFullError(
                    f"La sala '{self.room_id}' ha alcanzado su cupo máximo "
                    f"({self.max_players} jugadores)."
                )

            target_name = player.name.casefold()
            for existing_player in self._players.values():
                if existing_player.name.casefold() == target_name:
                    raise DuplicatePlayerNameError(
                        f"Ya existe un participante con el apodo '{player.name}' en la sala."
                    )

            # Si el jugador es el anfitrión asignado a la sala, asegurar su flag
            if player.player_id == self.host_id:
                player.is_host = True
            elif not self.host_id:
                # Si por algún motivo no había host definido, asignar este
                self.host_id = player.player_id
                player.is_host = True

            self._players[player.player_id] = player
            self.version += 1

    def remove_player(self, player_id: str) -> Player | None:
        """Remueve a un jugador de la sala.

        Si el participante removido era el anfitrión, reasigna automáticamente
        el rol de host al siguiente participante más antiguo (si la sala no queda vacía).

        Returns:
            Player | None: Copia independiente del jugador removido o None.
        """
        with self._lock:
            removed = self._players.pop(player_id, None)
            if removed is None:
                return None

            self.version += 1

            # Si era el anfitrión, transferir el rol al primer jugador disponible
            if removed.player_id == self.host_id:
                removed.is_host = False
                if self._players:
                    new_host = next(iter(self._players.values()))
                    new_host.is_host = True
                    self.host_id = new_host.player_id
                else:
                    self.host_id = ""

            return dataclasses.replace(removed)

    def get_player(self, player_id: str) -> Player | None:
        """Retorna una copia independiente del jugador correspondiente o None."""
        with self._lock:
            player = self._players.get(player_id)
            return dataclasses.replace(player) if player is not None else None

    def get_players(self) -> list[Player]:
        """Retorna copias independientes de la lista de jugadores de la sala."""
        with self._lock:
            return [dataclasses.replace(p) for p in self._players.values()]

    def to_dict(self) -> dict[str, Any]:
        """Serializa la sala cumpliendo con el contrato del mensaje formal ROOM_STATE."""
        with self._lock:
            return {
                "room_id": self.room_id,
                "room_name": self.room_name,
                "host_id": self.host_id,
                "status": self.status.value,
                "players": [p.to_dict() for p in self._players.values()],
                "max_players": self.max_players,
            }


class RoomManager:
    """Registro centralizado y administrador del ciclo de vida de salas de juego.

    Mantiene la consistencia global del estado mediante un Lock principal:
    - Registro de salas activas por código único.
    - Índice bidireccional player_id -> room_id para prevenir que un usuario pertenezca
      simultáneamente a varias salas y agilizar la resolución de desconexiones en O(1).
    """

    def __init__(self, max_code_generation_attempts: int = 500) -> None:
        self._rooms: dict[str, Room] = {}
        self._player_to_room: dict[str, str] = {}
        self._lock = threading.Lock()
        self._max_code_attempts = max_code_generation_attempts

    @property
    def total_rooms_count(self) -> int:
        """Cantidad total de salas activas."""
        with self._lock:
            return len(self._rooms)

    def generate_unique_code(self) -> str:
        """Genera un código único en formato 'TRV-XXXX' no utilizado por salas activas.

        Raises:
            RoomCodeExhaustionError: Si no se encuentra un código disponible tras agotar reintentos.
        """
        with self._lock:
            return self._generate_unique_code_locked()

    def _generate_unique_code_locked(self) -> str:
        """Generador interno de códigos únicos. Debe invocarse sosteniendo self._lock."""
        # Se genera TRV- seguido de 4 dígitos (1000 a 9999)
        for _ in range(self._max_code_attempts):
            digits = "".join(random.choices(string.digits, k=4))
            candidate = f"TRV-{digits}"
            if candidate not in self._rooms:
                return candidate

        # Fallback alfanumérico si el espacio de 4 dígitos estuviera colmado
        for _ in range(self._max_code_attempts):
            chars = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
            candidate = f"TRV-{chars}"
            if candidate not in self._rooms:
                return candidate

        raise RoomCodeExhaustionError(
            "No fue posible generar un código único de sala; el espacio de códigos está saturado."
        )

    def create_room(
        self,
        host: Player,
        room_name: str = "",
        max_players: int = 4,
        rounds_count: int = 5,
    ) -> Room:
        """Crea y registra una nueva sala asignando al jugador provisto como anfitrión.

        Operación atómica que previene que un jugador cree una sala si ya se encuentra
        en otra y garantiza la unicidad del código.

        Raises:
            PlayerAlreadyInRoomError: Si el jugador ya pertenece a otra sala activa.
            RoomCodeExhaustionError: Si no hay códigos de sala disponibles.
        """
        with self._lock:
            if host.player_id in self._player_to_room:
                active_room = self._player_to_room[host.player_id]
                raise PlayerAlreadyInRoomError(
                    f"El jugador '{host.name}' ({host.player_id}) ya está "
                    f"en la sala '{active_room}'."
                )

            room_id = self._generate_unique_code_locked()
            host.is_host = True

            room = Room(
                room_id=room_id,
                room_name=room_name,
                host_id=host.player_id,
                max_players=max_players,
                rounds_count=rounds_count,
            )
            # add_player sobre una sala nueva recién creada no tiene contención
            room.add_player(host)

            self._rooms[room_id] = room
            self._player_to_room[host.player_id] = room_id
            return room

    def join_room(self, room_id: str, player: Player) -> Room:
        """Incorpora a un jugador a una sala existente de forma consistente.

        Primero valida que el jugador no esté en otra sala y que la sala exista,
        y luego delega la validación de cupo y nombre al cerrojo de la sala.

        Raises:
            PlayerAlreadyInRoomError: Si el jugador ya pertenece a otra sala activa.
            RoomNotFoundError: Si la sala no existe en el registro.
            RoomFullError: Si la sala completó su cupo máximo.
            DuplicatePlayerNameError: Si el apodo ya existe en la sala.
            InvalidRoomStateError: Si la sala no está en estado WAITING.
        """
        clean_code = room_id.strip().upper()

        with self._lock:
            if player.player_id in self._player_to_room:
                active_room = self._player_to_room[player.player_id]
                raise PlayerAlreadyInRoomError(
                    f"El jugador '{player.name}' ({player.player_id}) ya está "
                    f"en la sala '{active_room}'."
                )

            room = self._rooms.get(clean_code)
            if room is None:
                raise RoomNotFoundError(f"La sala '{clean_code}' no existe o ya ha sido cerrada.")

            # Se agrega al jugador en la sala (adquiere el lock de Room)
            room.add_player(player)
            # Si tuvo éxito, registramos en el índice bidireccional
            self._player_to_room[player.player_id] = clean_code
            return room

    def leave_room(self, player_id: str) -> tuple[Room | None, Player | None]:
        """Desvincula a un participante de su sala activa de forma consistente.

        Si la sala queda vacía tras el egreso, es destruida automáticamente del
        registro global para evitar fugas de memoria.

        Returns:
            tuple[Room | None, Player | None]: La sala afectada y el jugador removido,
            o (None, None) si el participante no estaba en ninguna sala.
        """
        with self._lock:
            room_id = self._player_to_room.pop(player_id, None)
            if room_id is None:
                return None, None

            room = self._rooms.get(room_id)
            if room is None:
                return None, None

            removed_player = room.remove_player(player_id)

            if room.is_empty():
                self._rooms.pop(room_id, None)

            return room, removed_player

    def get_room(self, room_id: str) -> Room | None:
        """Retorna la sala correspondiente al código provisto o None."""
        clean_code = room_id.strip().upper()
        with self._lock:
            return self._rooms.get(clean_code)

    def get_room_by_player(self, player_id: str) -> Room | None:
        """Retorna la sala activa a la que pertenece un jugador o None."""
        with self._lock:
            room_id = self._player_to_room.get(player_id)
            if room_id is None:
                return None
            return self._rooms.get(room_id)

    def get_all_rooms(self) -> list[Room]:
        """Retorna una lista con todas las salas activas en el sistema."""
        with self._lock:
            return list(self._rooms.values())
