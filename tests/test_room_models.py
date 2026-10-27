"""Pruebas unitarias para el modelo de salas y jugadores (models.py).

Verifica la lógica de negocio de Player, Room y RoomManager:
- Normalización y validación de apodos.
- Creación de salas y asignación del anfitrión (host).
- Incorporación de participantes y rechazo de apodos duplicados.
- Límite de cupo (RoomFullError) y restricciones por estado (InvalidRoomStateError).
- Reasignación de anfitrión al retirarse el creador.
- Prevención de pertenencia simultánea a múltiples salas.
- Generación de códigos únicos y limpieza de salas vacías.
"""

from __future__ import annotations

import unittest

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
    RoomStatus,
)


class TestPlayerModel(unittest.TestCase):
    """Pruebas para la entidad Player."""

    def test_player_creation_and_stripping(self) -> None:
        p = Player(player_id="c-1", name="  Constanza  ")
        self.assertEqual(p.player_id, "c-1")
        self.assertEqual(p.name, "Constanza")
        self.assertFalse(p.is_host)
        self.assertTrue(p.is_ready)
        self.assertEqual(p.score, 0)

    def test_player_empty_name_raises_error(self) -> None:
        with self.assertRaises(InvalidPlayerNameError):
            Player(player_id="c-1", name="")

        with self.assertRaises(InvalidPlayerNameError):
            Player(player_id="c-1", name="   ")

    def test_player_invalid_types_raise_error(self) -> None:
        with self.assertRaises(InvalidPlayerNameError):
            Player(player_id="c-1", name=123)  # type: ignore[arg-type]

        with self.assertRaises(InvalidPlayerNameError):
            Player(player_id="c-1", name=None)  # type: ignore[arg-type]

        with self.assertRaises(InvalidPlayerNameError):
            Player(player_id="", name="Valid")

        with self.assertRaises(InvalidPlayerNameError):
            Player(player_id=123, name="Valid")  # type: ignore[arg-type]

    def test_player_to_dict_matches_protocol(self) -> None:
        p = Player(player_id="c-1", name="Alice", is_host=True, is_ready=False)
        data = p.to_dict()
        self.assertEqual(
            data,
            {
                "player_id": "c-1",
                "name": "Alice",
                "is_host": True,
                "is_ready": False,
            },
        )


class TestRoomModel(unittest.TestCase):
    """Pruebas para la entidad Room y su ciclo de vida individual."""

    def test_room_creation_defaults(self) -> None:
        room = Room(room_id="trv-1001", room_name="", host_id="p-1")
        self.assertEqual(room.room_id, "TRV-1001")
        self.assertEqual(room.room_name, "Sala TRV-1001")
        self.assertEqual(room.host_id, "p-1")
        self.assertEqual(room.max_players, 4)
        self.assertEqual(room.rounds_count, 5)
        self.assertEqual(room.status, RoomStatus.WAITING)
        self.assertTrue(room.is_empty())
        self.assertFalse(room.is_full())

    def test_room_invalid_configurations_raise_error(self) -> None:
        with self.assertRaises(InvalidRoomConfigurationError):
            Room(room_id="TRV-1", room_name="", host_id="p", max_players=1)

        with self.assertRaises(InvalidRoomConfigurationError):
            Room(room_id="TRV-1", room_name="", host_id="p", max_players=True)  # type: ignore[arg-type]

        with self.assertRaises(InvalidRoomConfigurationError):
            Room(room_id="TRV-1", room_name="", host_id="p", rounds_count=0)

        with self.assertRaises(InvalidRoomConfigurationError):
            Room(room_id="TRV-1", room_name="", host_id="p", rounds_count=False)  # type: ignore[arg-type]

    def test_get_player_and_get_players_return_copies(self) -> None:
        room = Room(room_id="TRV-001", room_name="Test", host_id="p-1")
        p1 = Player(player_id="p-1", name="OriginalName")
        room.add_player(p1)

        # Mutar el objeto retornado por get_player no debe alterar el estado interno
        copy_p = room.get_player("p-1")
        self.assertIsNotNone(copy_p)
        assert copy_p is not None
        copy_p.name = "MutatedName"
        self.assertEqual(room.get_player("p-1").name, "OriginalName")  # type: ignore[union-attr]

        # Mutar elementos retornados por get_players tampoco debe alterar el estado
        list_players = room.get_players()
        list_players[0].name = "MutatedInList"
        self.assertEqual(room.get_player("p-1").name, "OriginalName")  # type: ignore[union-attr]

    def test_add_player_assigns_host(self) -> None:
        room = Room(room_id="TRV-001", room_name="Test", host_id="p-host")
        host = Player(player_id="p-host", name="HostUser")
        room.add_player(host)

        self.assertEqual(room.current_players_count, 1)
        self.assertTrue(host.is_host)
        self.assertTrue(room.has_player("p-host"))
        self.assertTrue(room.has_player_name("hostuser"))

    def test_add_second_player_success(self) -> None:
        room = Room(room_id="TRV-001", room_name="Test", host_id="p-1")
        p1 = Player(player_id="p-1", name="Alice", is_host=True)
        p2 = Player(player_id="p-2", name="Bob", is_host=False)

        room.add_player(p1)
        room.add_player(p2)

        self.assertEqual(room.current_players_count, 2)
        self.assertFalse(p2.is_host)
        players = room.get_players()
        self.assertEqual(len(players), 2)
        self.assertEqual(players[0].name, "Alice")
        self.assertEqual(players[1].name, "Bob")

    def test_add_duplicate_player_name_case_insensitive_raises_error(self) -> None:
        room = Room(room_id="TRV-001", room_name="Test", host_id="p-1")
        p1 = Player(player_id="p-1", name="Constanza")
        room.add_player(p1)

        # Mismo nombre en minúsculas con otro ID
        p2 = Player(player_id="p-2", name="constanza")
        with self.assertRaises(DuplicatePlayerNameError):
            room.add_player(p2)

        # Mismo nombre con espacios
        p3 = Player(player_id="p-3", name="  Constanza  ")
        with self.assertRaises(DuplicatePlayerNameError):
            room.add_player(p3)

    def test_room_full_raises_error(self) -> None:
        room = Room(room_id="TRV-001", room_name="Test", host_id="p-1", max_players=2)
        p1 = Player(player_id="p-1", name="P1")
        p2 = Player(player_id="p-2", name="P2")
        p3 = Player(player_id="p-3", name="P3")

        room.add_player(p1)
        room.add_player(p2)
        self.assertTrue(room.is_full())

        with self.assertRaises(RoomFullError):
            room.add_player(p3)

    def test_add_player_when_game_started_raises_error(self) -> None:
        room = Room(room_id="TRV-001", room_name="Test", host_id="p-1")
        p1 = Player(player_id="p-1", name="P1")
        room.add_player(p1)

        room.status = RoomStatus.IN_GAME

        p2 = Player(player_id="p-2", name="P2")
        with self.assertRaises(InvalidRoomStateError):
            room.add_player(p2)

    def test_remove_non_host_player(self) -> None:
        room = Room(room_id="TRV-001", room_name="Test", host_id="p-1")
        p1 = Player(player_id="p-1", name="Host")
        p2 = Player(player_id="p-2", name="Guest")
        room.add_player(p1)
        room.add_player(p2)

        removed = room.remove_player("p-2")
        self.assertIsNotNone(removed)
        self.assertEqual(removed.name, "Guest")
        self.assertEqual(room.current_players_count, 1)
        self.assertEqual(room.host_id, "p-1")

    def test_remove_host_reassigns_to_next_player(self) -> None:
        room = Room(room_id="TRV-001", room_name="Test", host_id="p-1")
        p1 = Player(player_id="p-1", name="Host")
        p2 = Player(player_id="p-2", name="NextHost")
        room.add_player(p1)
        room.add_player(p2)

        removed = room.remove_player("p-1")
        self.assertEqual(removed.player_id, "p-1")
        self.assertFalse(removed.is_host)

        # p2 ahora debe ser el host
        self.assertEqual(room.host_id, "p-2")
        self.assertTrue(p2.is_host)
        self.assertEqual(room.current_players_count, 1)

    def test_remove_last_player_clears_host(self) -> None:
        room = Room(room_id="TRV-001", room_name="Test", host_id="p-1")
        p1 = Player(player_id="p-1", name="Host")
        room.add_player(p1)

        room.remove_player("p-1")
        self.assertTrue(room.is_empty())
        self.assertEqual(room.host_id, "")

    def test_room_to_dict_matches_protocol(self) -> None:
        room = Room(room_id="TRV-9999", room_name="Mi Sala", host_id="p-1", max_players=4)
        p1 = Player(player_id="p-1", name="Constanza", is_host=True)
        room.add_player(p1)

        payload = room.to_dict()
        self.assertEqual(payload["room_id"], "TRV-9999")
        self.assertEqual(payload["room_name"], "Mi Sala")
        self.assertEqual(payload["host_id"], "p-1")
        self.assertEqual(payload["status"], "WAITING")
        self.assertEqual(payload["max_players"], 4)
        self.assertEqual(len(payload["players"]), 1)
        self.assertEqual(payload["players"][0]["name"], "Constanza")
        self.assertTrue(payload["players"][0]["is_host"])


class TestRoomManager(unittest.TestCase):
    """Pruebas para el administrador RoomManager."""

    def setUp(self) -> None:
        self.manager = RoomManager()

    def test_create_room_generates_code_and_registers(self) -> None:
        host = Player(player_id="c-host", name="Host1")
        room = self.manager.create_room(host, room_name="Sala A", max_players=3)

        self.assertTrue(room.room_id.startswith("TRV-"))
        self.assertEqual(len(room.room_id), 8)  # "TRV-" + 4 dígitos
        self.assertEqual(self.manager.total_rooms_count, 1)

        retrieved = self.manager.get_room(room.room_id)
        self.assertIs(retrieved, room)

        by_player = self.manager.get_room_by_player("c-host")
        self.assertIs(by_player, room)

    def test_player_cannot_create_two_rooms_simultaneously(self) -> None:
        host = Player(player_id="c-1", name="Player1")
        self.manager.create_room(host)

        # Intento de crear otra sala con la misma conexión/ID
        with self.assertRaises(PlayerAlreadyInRoomError):
            self.manager.create_room(host)

    def test_player_cannot_join_another_room_simultaneously(self) -> None:
        h1 = Player(player_id="c-1", name="Host1")
        h2 = Player(player_id="c-2", name="Host2")
        r1 = self.manager.create_room(h1)
        r2 = self.manager.create_room(h2)

        # Un jugador libre ingresa a r1
        guest = Player(player_id="c-3", name="Guest")
        self.manager.join_room(r1.room_id, guest)

        # El mismo jugador intenta ingresar a r2 sin haber salido de r1
        with self.assertRaises(PlayerAlreadyInRoomError):
            self.manager.join_room(r2.room_id, guest)

    def test_join_nonexistent_room_raises_error(self) -> None:
        guest = Player(player_id="c-2", name="Guest")
        with self.assertRaises(RoomNotFoundError):
            self.manager.join_room("TRV-4040", guest)

    def test_leave_room_and_cleanup_empty_room(self) -> None:
        host = Player(player_id="c-1", name="Host")
        room = self.manager.create_room(host)
        code = room.room_id

        # Salir de la sala
        affected_room, removed = self.manager.leave_room("c-1")
        self.assertIs(affected_room, room)
        self.assertEqual(removed.player_id, "c-1")

        # La sala quedó vacía, debe ser destruida del registro
        self.assertIsNone(self.manager.get_room(code))
        self.assertIsNone(self.manager.get_room_by_player("c-1"))
        self.assertEqual(self.manager.total_rooms_count, 0)

    def test_leave_room_when_not_in_room_returns_none(self) -> None:
        affected, removed = self.manager.leave_room("c-ghost")
        self.assertIsNone(affected)
        self.assertIsNone(removed)

    def test_code_exhaustion_raises_error(self) -> None:
        manager = RoomManager(max_code_generation_attempts=0)
        with self.assertRaises(RoomCodeExhaustionError):
            manager.create_room(Player(player_id="p-exhaust", name="PExhaust"))


if __name__ == "__main__":
    unittest.main()
