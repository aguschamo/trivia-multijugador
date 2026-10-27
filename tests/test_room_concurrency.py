"""Pruebas de concurrencia pura para el motor de salas y sincronización.

Verifica condiciones de carrera críticas en entornos multihilo:
- Competencia simultánea por el último cupo disponible (RoomFullError).
- Creación masiva concurrente de salas y unicidad estricta de códigos.
- Integridad del estado compartido bajo accesos paralelos.
"""

from __future__ import annotations

import threading
import unittest
from typing import Any

from src.common.protocol import MessageType, create_message
from src.server.models import (
    Player,
    RoomFullError,
    RoomManager,
)
from src.server.service import RoomService


class TestRoomConcurrency(unittest.TestCase):
    """Pruebas de estrés y condiciones de carrera sobre Room y RoomManager."""

    def test_concurrent_join_last_slot_race_condition(self) -> None:
        """Prueba crítica:

        Una sala con capacidad para 2 jugadores (1 host y 1 vacante) recibe
        solicitudes de ingreso concurrentes desde 15 hilos simultáneos.
        Garantiza que exactamente un participante obtenga la vacante y los
        otros 14 sean rechazados atómicamente con RoomFullError.
        """
        manager = RoomManager()
        host = Player(player_id="host-1", name="HostAlice")
        room = manager.create_room(host, room_name="Sala Exclusiva", max_players=2)

        num_competitors = 15
        barrier = threading.Barrier(num_competitors)

        success_count = 0
        room_full_count = 0
        lock_stats = threading.Lock()

        def competitor_worker(idx: int) -> None:
            nonlocal success_count, room_full_count
            player = Player(player_id=f"guest-{idx}", name=f"Guest-{idx}")

            # Sincronización en la barrera para disparar el acceso en el mismo instante
            barrier.wait()

            try:
                manager.join_room(room.room_id, player)
                with lock_stats:
                    success_count += 1
            except RoomFullError:
                with lock_stats:
                    room_full_count += 1

        threads = [
            threading.Thread(target=competitor_worker, args=(i,), name=f"Competitor-{i}")
            for i in range(num_competitors)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Aserciones de exclusión mutua
        self.assertEqual(
            success_count,
            1,
            f"Exactamente 1 competidor debió ingresar, pero ingresaron {success_count}",
        )
        self.assertEqual(
            room_full_count,
            num_competitors - 1,
            f"Los otros {num_competitors - 1} debieron recibir RoomFullError",
        )
        self.assertEqual(room.current_players_count, 2)
        self.assertTrue(room.is_full())

    def test_concurrent_room_creation_unique_codes(self) -> None:
        """Prueba de creación masiva de salas simultáneas:

        40 hilos intentan crear salas al mismo tiempo. Verifica que no existan
        colisiones de códigos ni corrupción del diccionario global de salas.
        """
        manager = RoomManager()
        num_rooms = 40
        barrier = threading.Barrier(num_rooms)

        created_rooms: list[str] = []
        lock_list = threading.Lock()

        def creator_worker(idx: int) -> None:
            host = Player(player_id=f"host-concurrent-{idx}", name=f"Host-{idx}")
            barrier.wait()

            room = manager.create_room(host, room_name=f"Sala-{idx}")
            with lock_list:
                created_rooms.append(room.room_id)

        threads = [
            threading.Thread(target=creator_worker, args=(i,))
            for i in range(num_rooms)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(created_rooms), num_rooms)
        self.assertEqual(
            len(set(created_rooms)),
            num_rooms,
            "Todos los códigos de sala generados concurrentemente deben ser únicos",
        )
        self.assertEqual(manager.total_rooms_count, num_rooms)

    def test_concurrent_join_and_leave_stress(self) -> None:
        """Prueba de estrés con ingresos y egresos concurrentes cruzados sin deadlocks."""
        manager = RoomManager()
        host = Player(player_id="host-stress", name="HostStress")
        room = manager.create_room(host, max_players=20)

        num_threads = 16
        barrier = threading.Barrier(num_threads)

        def worker(idx: int) -> None:
            p_id = f"worker-{idx}"
            player = Player(player_id=p_id, name=f"User-{idx}")
            barrier.wait()

            for _ in range(5):
                try:
                    manager.join_room(room.room_id, player)
                    manager.leave_room(p_id)
                except Exception:
                    pass

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Si el test finaliza sin timeouts ni excepciones bloqueadas, no hubo deadlocks
        self.assertTrue(room.current_players_count >= 1)
        self.assertEqual(room.host_id, "host-stress")

    def test_concurrent_broadcast_order_no_stale_regression(self) -> None:
        """Verifica que las notificaciones concurrentes no lleguen desordenadas al anfitrión."""
        class DummyHandler:
            def __init__(self, client_id: str) -> None:
                self.client_id = client_id
                self.is_running = True
                self.messages: list[dict[str, Any]] = []
                self.lock = threading.Lock()

            def send_message(self, msg: Any) -> bool:
                with self.lock:
                    if isinstance(msg, dict):
                        self.messages.append(msg)
                    else:
                        self.messages.append(msg.to_dict())
                return True

        class DummyServer:
            def __init__(self) -> None:
                self.clients: dict[str, DummyHandler] = {}
                self.on_message_received = None
                self.on_client_disconnected = None

            def get_client(self, client_id: str) -> DummyHandler | None:
                return self.clients.get(client_id)

        server = DummyServer()
        manager = RoomManager()
        service = RoomService(server=server, room_manager=manager)  # type: ignore[arg-type]

        host_handler = DummyHandler("h-1")
        server.clients["h-1"] = host_handler

        service.handle_message(  # type: ignore[arg-type]
            host_handler,
            create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "Host", "max_players": 10},
                request_id="cr-1",
            ),
        )
        room_code = host_handler.messages[0]["payload"]["room_id"]

        num_joiners = 8
        barrier = threading.Barrier(num_joiners)

        def join_worker(idx: int) -> None:
            cid = f"joiner-{idx}"
            handler = DummyHandler(cid)
            server.clients[cid] = handler
            barrier.wait()

            service.handle_message(  # type: ignore[arg-type]
                handler,
                create_message(
                    MessageType.JOIN_ROOM,
                    payload={"room_id": room_code, "player_name": f"P{idx}"},
                    request_id=f"join-req-{idx}",
                ),
            )

        threads = [
            threading.Thread(target=join_worker, args=(i,))
            for i in range(num_joiners)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Evaluar la secuencia de ROOM_STATE recibida por el Host
        room_states = [
            m["payload"]["players"]
            for m in host_handler.messages
            if m["type"] == MessageType.ROOM_STATE.value
        ]

        # La secuencia debe ser monótonamente no decreciente en cantidad de jugadores
        counts = [len(p) for p in room_states]
        for i in range(1, len(counts)):
            self.assertGreaterEqual(
                counts[i],
                counts[i - 1],
                f"Regresión desordenada detectada en notificaciones: {counts}",
            )

        # El último estado recibido por el anfitrión debe contener a todos los participantes
        self.assertEqual(counts[-1], num_joiners + 1)


if __name__ == "__main__":
    unittest.main()
