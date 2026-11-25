"""Pruebas de integración para la gestión de salas (Issue 04).

Verifica la orquestación entre la capa de red y el modelo de salas:
- Handshake y creación de sala (CREATE_ROOM -> ROOM_STATE).
- Ingreso de un segundo jugador (JOIN_ROOM -> ROOM_STATE a todos los miembros).
- Respuestas estructuradas de error (ROOM_NOT_FOUND, ROOM_FULL, INVALID_PAYLOAD).
- Detección y notificación de desconexiones sin caída del servidor.
- Limpieza automática de salas vacías.
- Pruebas directas de despacho (RoomService) y sobre sockets TCP reales.
"""

from __future__ import annotations

import socket
import time
import unittest
from typing import Any

from src.common.protocol import (
    MessageType,
    ProtocolHandler,
    create_message,
    encode_message,
)
from src.server.models import RoomManager
from src.server.network import TCPServer
from src.server.service import RoomService


def can_bind_and_connect_sockets() -> bool:
    """Verifica si el entorno actual permite abrir y conectar sockets TCP en localhost."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c.settimeout(0.5)
        c.connect(("127.0.0.1", port))
        conn, _ = s.accept()
        conn.close()
        c.close()
        s.close()
        return True
    except (PermissionError, OSError):
        return False


CAN_USE_TCP_SOCKETS = can_bind_and_connect_sockets()


class MockClientHandlerShim:
    """Simulador de ClientHandler para verificar el despacho de RoomService."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self.is_running = True
        self.sent_messages: list[dict[str, Any]] = []

    def send_message(self, message: Any) -> bool:
        if isinstance(message, dict):
            self.sent_messages.append(message)
        else:
            self.sent_messages.append(message.to_dict())
        return True


class MockServerShim:
    """Simulador de TCPServer para verificar RoomService de forma determinística."""

    def __init__(self) -> None:
        self.clients: dict[str, MockClientHandlerShim] = {}
        self.on_message_received = None
        self.on_client_disconnected = None

    def get_client(self, client_id: str) -> MockClientHandlerShim | None:
        return self.clients.get(client_id)

    def register(self, client: MockClientHandlerShim) -> None:
        self.clients[client.client_id] = client


class TestRoomServiceDispatch(unittest.TestCase):
    """Pruebas de integración de la lógica de despacho de RoomService."""

    def setUp(self) -> None:
        self.room_manager = RoomManager()
        self.server_shim = MockServerShim()
        # RoomService se vincula al server shim
        self.service = RoomService(
            server=self.server_shim,  # type: ignore[arg-type]
            room_manager=self.room_manager,
        )

        self.c1 = MockClientHandlerShim(client_id="c-001")
        self.c2 = MockClientHandlerShim(client_id="c-002")
        self.c3 = MockClientHandlerShim(client_id="c-003")

        self.server_shim.register(self.c1)
        self.server_shim.register(self.c2)
        self.server_shim.register(self.c3)

    def test_create_room_dispatch(self) -> None:
        create_msg = create_message(
            MessageType.CREATE_ROOM,
            payload={"player_name": "Constanza", "room_name": "Sala Oficial"},
            request_id="req-cr-1",
        )
        self.service.handle_message(self.c1, create_msg)  # type: ignore[arg-type]

        self.assertEqual(len(self.c1.sent_messages), 1)
        res = self.c1.sent_messages[0]
        self.assertEqual(res["type"], MessageType.ROOM_STATE.value)
        self.assertEqual(res["request_id"], "req-cr-1")

        payload = res["payload"]
        self.assertTrue(payload["room_id"].startswith("TRV-"))
        self.assertEqual(payload["room_name"], "Sala Oficial")
        self.assertEqual(payload["host_id"], "c-001")
        self.assertEqual(len(payload["players"]), 1)
        self.assertEqual(payload["players"][0]["name"], "Constanza")
        self.assertTrue(payload["players"][0]["is_host"])

    def test_join_room_both_notified_dispatch(self) -> None:
        # C1 crea sala
        self.service.handle_message(  # type: ignore[arg-type]
            self.c1,
            create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "HostPlayer"},
                request_id="r1",
            ),
        )
        room_code = self.c1.sent_messages[0]["payload"]["room_id"]

        # C2 se une
        self.service.handle_message(  # type: ignore[arg-type]
            self.c2,
            create_message(
                MessageType.JOIN_ROOM,
                payload={"room_id": room_code, "player_name": "GuestPlayer"},
                request_id="req-join-c2",
            ),
        )

        # C2 debe recibir ROOM_STATE respondiendo a su solicitud
        self.assertEqual(len(self.c2.sent_messages), 1)
        c2_msg = self.c2.sent_messages[0]
        self.assertEqual(c2_msg["type"], MessageType.ROOM_STATE.value)
        self.assertEqual(c2_msg["request_id"], "req-join-c2")
        self.assertEqual(len(c2_msg["payload"]["players"]), 2)

        # C1 también debe recibir la actualización por broadcast
        self.assertEqual(len(self.c1.sent_messages), 2)
        c1_broadcast = self.c1.sent_messages[1]
        self.assertEqual(c1_broadcast["type"], MessageType.ROOM_STATE.value)
        self.assertEqual(len(c1_broadcast["payload"]["players"]), 2)

        names = [p["name"] for p in c1_broadcast["payload"]["players"]]
        self.assertIn("HostPlayer", names)
        self.assertIn("GuestPlayer", names)

    def test_join_nonexistent_room_returns_error(self) -> None:
        self.service.handle_message(  # type: ignore[arg-type]
            self.c1,
            create_message(
                MessageType.JOIN_ROOM,
                payload={"room_id": "TRV-4040", "player_name": "Perdido"},
                request_id="req-404",
            ),
        )
        self.assertEqual(len(self.c1.sent_messages), 1)
        err = self.c1.sent_messages[0]
        self.assertEqual(err["type"], MessageType.ERROR.value)
        self.assertEqual(err["request_id"], "req-404")
        self.assertEqual(err["payload"]["code"], "ROOM_NOT_FOUND")

    def test_join_duplicate_nickname_returns_error(self) -> None:
        self.service.handle_message(  # type: ignore[arg-type]
            self.c1,
            create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "Mia"},
            ),
        )
        room_code = self.c1.sent_messages[0]["payload"]["room_id"]

        self.service.handle_message(  # type: ignore[arg-type]
            self.c2,
            create_message(
                MessageType.JOIN_ROOM,
                payload={"room_id": room_code, "player_name": "  mia  "},
                request_id="dup-req",
            ),
        )
        self.assertEqual(len(self.c2.sent_messages), 1)
        err = self.c2.sent_messages[0]
        self.assertEqual(err["type"], MessageType.ERROR.value)
        self.assertEqual(err["request_id"], "dup-req")
        self.assertEqual(err["payload"]["code"], "INVALID_PAYLOAD")

    def test_join_full_room_returns_error(self) -> None:
        self.service.handle_message(  # type: ignore[arg-type]
            self.c1,
            create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "P1", "max_players": 2},
            ),
        )
        room_code = self.c1.sent_messages[0]["payload"]["room_id"]

        self.service.handle_message(  # type: ignore[arg-type]
            self.c2,
            create_message(
                MessageType.JOIN_ROOM,
                payload={"room_id": room_code, "player_name": "P2"},
            ),
        )
        self.assertEqual(self.c2.sent_messages[0]["type"], MessageType.ROOM_STATE.value)

        # C3 intenta unirse a sala llena
        self.service.handle_message(  # type: ignore[arg-type]
            self.c3,
            create_message(
                MessageType.JOIN_ROOM,
                payload={"room_id": room_code, "player_name": "P3"},
                request_id="full-req",
            ),
        )
        self.assertEqual(len(self.c3.sent_messages), 1)
        err = self.c3.sent_messages[0]
        self.assertEqual(err["type"], MessageType.ERROR.value)
        self.assertEqual(err["request_id"], "full-req")
        self.assertEqual(err["payload"]["code"], "ROOM_FULL")

    def test_client_cannot_create_or_join_multiple_rooms(self) -> None:
        self.service.handle_message(  # type: ignore[arg-type]
            self.c1,
            create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "P1"},
            ),
        )
        self.assertEqual(self.c1.sent_messages[0]["type"], MessageType.ROOM_STATE.value)

        # Intentar crear otra sala con la misma conexión
        self.service.handle_message(  # type: ignore[arg-type]
            self.c1,
            create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "P1-bis"},
                request_id="bad-repeat",
            ),
        )
        self.assertEqual(len(self.c1.sent_messages), 2)
        err = self.c1.sent_messages[1]
        self.assertEqual(err["type"], MessageType.ERROR.value)
        self.assertEqual(err["request_id"], "bad-repeat")
        self.assertEqual(err["payload"]["code"], "INVALID_PAYLOAD")

    def test_invalid_payload_fields(self) -> None:
        # payload no es dict
        self.service.handle_message(  # type: ignore[arg-type]
            self.c1,
            {"type": "CREATE_ROOM", "request_id": "inv-1", "payload": "not-a-dict"},
        )
        self.assertEqual(self.c1.sent_messages[0]["payload"]["code"], "INVALID_PAYLOAD")

        # player_name vacío
        self.service.handle_message(  # type: ignore[arg-type]
            self.c1,
            create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "  "},
                request_id="inv-2",
            ),
        )
        self.assertEqual(self.c1.sent_messages[1]["payload"]["code"], "INVALID_PAYLOAD")

        # max_players es booleano True
        self.service.handle_message(  # type: ignore[arg-type]
            self.c1,
            create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "Valen", "max_players": True},
                request_id="inv-3",
            ),
        )
        self.assertEqual(self.c1.sent_messages[2]["payload"]["code"], "INVALID_PAYLOAD")

        # rounds_count es booleano True
        self.service.handle_message(  # type: ignore[arg-type]
            self.c1,
            create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "Valen", "rounds_count": True},
                request_id="inv-4",
            ),
        )
        self.assertEqual(self.c1.sent_messages[3]["payload"]["code"], "INVALID_PAYLOAD")

        # room_name es numérico (tipo erróneo)
        self.service.handle_message(  # type: ignore[arg-type]
            self.c1,
            create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "Valen", "room_name": 12345},
                request_id="inv-5",
            ),
        )
        self.assertEqual(self.c1.sent_messages[4]["payload"]["code"], "INVALID_PAYLOAD")

        # room_name es None (debe permitirse como opcional)
        self.service.handle_message(  # type: ignore[arg-type]
            self.c1,
            create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "Valen", "room_name": None},
                request_id="ok-none-name",
            ),
        )
        self.assertEqual(self.c1.sent_messages[5]["type"], MessageType.ROOM_STATE.value)

    def test_disconnect_reassigns_host_and_cleans_up(self) -> None:
        self.service.handle_message(  # type: ignore[arg-type]
            self.c1,
            create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "Host1"},
            ),
        )
        room_code = self.c1.sent_messages[0]["payload"]["room_id"]

        self.service.handle_message(  # type: ignore[arg-type]
            self.c2,
            create_message(
                MessageType.JOIN_ROOM,
                payload={"room_id": room_code, "player_name": "Guest2"},
            ),
        )

        # Desconectar C1 (Host)
        self.service.handle_disconnect(self.c1)  # type: ignore[arg-type]

        # C2 debe recibir ROOM_STATE donde él es ahora el host
        c2_last_msg = self.c2.sent_messages[-1]
        self.assertEqual(c2_last_msg["type"], MessageType.ROOM_STATE.value)
        players = c2_last_msg["payload"]["players"]
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0]["player_id"], "c-002")
        self.assertTrue(players[0]["is_host"])
        self.assertEqual(c2_last_msg["payload"]["host_id"], "c-002")

        # Desconectar C2 -> la sala queda vacía y se destruye
        self.service.handle_disconnect(self.c2)  # type: ignore[arg-type]
        self.assertIsNone(self.room_manager.get_room(room_code))
        self.assertEqual(self.room_manager.total_rooms_count, 0)


@unittest.skipIf(not CAN_USE_TCP_SOCKETS, "Sockets TCP bloqueados por el entorno sandbox")
class TestServerRoomsTCPIntegration(unittest.TestCase):
    """Pruebas de red real sobre sockets TCP (se ejecutan fuera del sandbox restringido)."""

    def setUp(self) -> None:
        self.room_manager = RoomManager()
        self.server = TCPServer(host="127.0.0.1", port=0, accept_timeout=0.2)
        self.service = RoomService(server=self.server, room_manager=self.room_manager)
        self.server.start(blocking=False)
        time.sleep(0.05)

    def tearDown(self) -> None:
        self.server.stop()

    def test_create_and_join_room_real_tcp(self) -> None:
        # MockTCPClient local
        class RealClient:
            def __init__(self, host: str, port: int) -> None:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(3.0)
                self.sock.connect((host, port))
                self.handler = ProtocolHandler()

            def send(self, msg: dict[str, Any]) -> None:
                self.sock.sendall(encode_message(msg))

            def recv_msg(self) -> dict[str, Any]:
                while True:
                    msgs = self.handler.feed(b"")
                    if msgs:
                        return msgs.pop(0)
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        raise ConnectionResetError()
                    msgs = self.handler.feed(chunk)
                    if msgs:
                        return msgs.pop(0)

            def close(self) -> None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.sock.close()

        c1 = RealClient(self.server.host, self.server.port)
        c2 = RealClient(self.server.host, self.server.port)
        try:
            c1.recv_msg()  # WELCOME
            c2.recv_msg()  # WELCOME

            c1.send(create_message(MessageType.CREATE_ROOM, {"player_name": "H"}))
            r = c1.recv_msg()
            code = r["payload"]["room_id"]

            c2.send(create_message(MessageType.JOIN_ROOM, {"room_id": code, "player_name": "G"}))
            m2 = c2.recv_msg()
            self.assertEqual(len(m2["payload"]["players"]), 2)
        finally:
            c1.close()
            c2.close()


if __name__ == "__main__":
    unittest.main()
