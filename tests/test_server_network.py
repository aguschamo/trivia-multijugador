"""Pruebas de integración y concurrencia para el servidor TCP multihilo.

Verifica:
- Conexión concurrente de al menos 5 clientes simultáneos.
- Recepción garantizada del mensaje WELCOME.
- Intercambio de mensajes bidireccional (PING/PONG, broadcast).
- Detección de desconexiones normales y abruptas sin caída del servidor.
- Apagado ordenado (graceful shutdown) sin fugas de sockets ni hilos bloqueados.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Any

import pytest

from src.common.protocol import (
    MessageType,
    ProtocolHandler,
    create_message,
    create_ping,
    encode_message,
)
from src.server.network import TCPServer


class MockSocketClient:
    """Cliente de socket simple para simular conexiones de red en los tests."""

    def __init__(self, host: str, port: int, timeout: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.protocol_handler = ProtocolHandler()

    def connect(self) -> None:
        """Establece la conexión TCP con el servidor."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))

    def send(self, message: dict[str, Any]) -> None:
        """Serializa y envía un mensaje terminado en \\n."""
        assert self.sock is not None
        data = encode_message(message)
        self.sock.sendall(data)

    def receive_message(self, max_attempts: int = 20) -> dict[str, Any]:
        """Lee del socket hasta obtener al menos un mensaje completo."""
        assert self.sock is not None
        for _ in range(max_attempts):
            messages = self.protocol_handler.feed(b"")
            if messages:
                return messages.pop(0)

            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionResetError("Conexión cerrada por el servidor.")
            msgs = self.protocol_handler.feed(chunk)
            if msgs:
                return msgs.pop(0)
            time.sleep(0.05)
        raise TimeoutError("Tiempo de espera agotado esperando mensaje del servidor.")

    def close(self) -> None:
        """Cierra el socket ordenadamente."""
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def close_abruptly(self) -> None:
        """Fuerza un RST (reset TCP) para simular corte violento de conexión."""
        if self.sock:
            # Linger con timeout 0 envía RST en vez de FIN
            self.sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_LINGER,
                struct.pack("ii", 1, 0),
            )
            self.sock.close()
            self.sock = None


@pytest.fixture
def running_server() -> TCPServer:
    """Fixture que inicia un TCPServer en un puerto dinámico y asegura su cierre al terminar."""
    server = TCPServer(host="127.0.0.1", port=0, accept_timeout=0.2)
    server.start(blocking=False)
    # Esperar brevemente a que el socket esté listo
    time.sleep(0.05)
    yield server
    server.stop()


class TestTCPServerConcurrency:
    """Pruebas de concurrencia y clientes simultáneos."""

    def test_five_simultaneous_clients_connect_and_receive_welcome(
        self, running_server: TCPServer
    ) -> None:
        """Criterio de aceptación:

        El servidor acepta al menos 5 conexiones simultáneas de prueba y registra
        sus conexiones sin caerse.
        """
        num_clients = 5
        clients: list[MockSocketClient] = []
        welcome_messages: list[dict[str, Any]] = []

        try:
            # Conectar 5 clientes en paralelo
            for _ in range(num_clients):
                client = MockSocketClient(running_server.host, running_server.port)
                client.connect()
                clients.append(client)

            # Esperar a que el servidor registre a los 5 clientes
            max_wait = 3.0
            start_time = time.time()
            while running_server.get_connected_count() < num_clients:
                if time.time() - start_time > max_wait:
                    break
                time.sleep(0.05)

            assert running_server.get_connected_count() == num_clients

            # Cada uno debe recibir su mensaje de bienvenida WELCOME
            for client in clients:
                welcome = client.receive_message()
                assert welcome["type"] == MessageType.WELCOME.value
                assert "client_id" in welcome["payload"]
                assert welcome["payload"]["server_version"] == "0.1.0"
                welcome_messages.append(welcome)

            # Verificar que a cada cliente se le asignó un client_id único
            client_ids = [w["payload"]["client_id"] for w in welcome_messages]
            assert len(set(client_ids)) == num_clients

        finally:
            for client in clients:
                client.close()

            # Esperar a que el servidor detecte las desconexiones
            time.sleep(0.3)
            assert running_server.get_connected_count() == 0

    def test_ten_simultaneous_threaded_clients(self, running_server: TCPServer) -> None:
        """Prueba de estrés concurrente con 10 clientes conectándose en hilos independientes."""
        num_clients = 10
        barrier = threading.Barrier(num_clients)
        results: list[bool] = [False] * num_clients

        def worker(client_idx: int) -> None:
            c = MockSocketClient(running_server.host, running_server.port)
            try:
                c.connect()
                barrier.wait(timeout=5.0)  # Sincronizar conexión simultánea
                msg = c.receive_message()
                if msg["type"] == "WELCOME":
                    results[client_idx] = True
            except Exception:
                results[client_idx] = False
            finally:
                c.close()

        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True) for i in range(num_clients)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert all(results), "Todos los 10 clientes deben haber recibido el mensaje WELCOME"
        time.sleep(0.3)
        assert running_server.get_connected_count() == 0


class TestTCPServerMessageExchange:
    """Pruebas de intercambio de mensajes bidireccionales."""

    def test_ping_pong_response(self, running_server: TCPServer) -> None:
        """El servidor debe responder automáticamente a un PING con un PONG correlacionado."""
        client = MockSocketClient(running_server.host, running_server.port)
        try:
            client.connect()
            # Consumir WELCOME
            welcome = client.receive_message()
            assert welcome["type"] == "WELCOME"

            # Enviar PING
            ping_msg = create_ping(request_id="req-ping-123", timestamp=12345.67)
            client.send(ping_msg)

            # Recibir PONG
            pong = client.receive_message()
            assert pong["type"] == MessageType.PONG.value
            assert pong["request_id"] == "req-ping-123"
            assert pong["payload"]["original_timestamp"] == 12345.67
        finally:
            client.close()

    def test_server_message_received_callback(self, running_server: TCPServer) -> None:
        """El servidor invoca el callback on_message_received cuando llega un mensaje no-PING."""
        received_event = threading.Event()
        received_data: dict[str, Any] = {}

        def on_msg(client_handler: Any, message: dict[str, Any]) -> None:
            received_data.update(message)
            received_event.set()

        running_server.on_message_received = on_msg

        client = MockSocketClient(running_server.host, running_server.port)
        try:
            client.connect()
            client.receive_message()  # Consumir WELCOME

            # Enviar mensaje de crear sala
            create_msg = create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "Valen"},
                request_id="req-cr-1",
            )
            client.send(create_msg)

            assert received_event.wait(timeout=2.0)
            assert received_data["type"] == "CREATE_ROOM"
            assert received_data["payload"]["player_name"] == "Valen"
        finally:
            client.close()

    def test_broadcast_delivers_to_all_clients(self, running_server: TCPServer) -> None:
        """El método broadcast debe enviar el mensaje a todos los clientes conectados."""
        c1 = MockSocketClient(running_server.host, running_server.port)
        c2 = MockSocketClient(running_server.host, running_server.port)
        try:
            c1.connect()
            c2.connect()
            c1.receive_message()
            c2.receive_message()

            broadcast_msg = create_message(
                MessageType.ROOM_STATE,
                payload={"room_id": "R1", "status": "WAITING"},
            )
            delivered = running_server.broadcast(broadcast_msg)
            assert delivered == 2

            m1 = c1.receive_message()
            m2 = c2.receive_message()
            assert m1["type"] == "ROOM_STATE"
            assert m2["type"] == "ROOM_STATE"
        finally:
            c1.close()
            c2.close()


class TestTCPServerDisconnectionsAndRobustness:
    """Pruebas de tolerancia a desconexiones y resiliencia del servidor."""

    def test_server_survives_abrupt_client_disconnect(self, running_server: TCPServer) -> None:
        """Verificar que el servidor no se caiga ante desconexiones violentas (RST)."""
        client = MockSocketClient(running_server.host, running_server.port)
        client.connect()
        client.receive_message()
        assert running_server.get_connected_count() == 1

        # Cierre abrupto
        client.close_abruptly()

        # El servidor debe detectar la desconexión y seguir funcionando
        time.sleep(0.3)
        assert running_server.get_connected_count() == 0
        assert running_server.is_running

        # Un nuevo cliente debe poder conectarse sin problemas
        new_client = MockSocketClient(running_server.host, running_server.port)
        try:
            new_client.connect()
            welcome = new_client.receive_message()
            assert welcome["type"] == "WELCOME"
            assert running_server.get_connected_count() == 1
        finally:
            new_client.close()

    def test_callbacks_on_connect_and_disconnect(self, running_server: TCPServer) -> None:
        """Verificar invocación de callbacks on_client_connected y on_client_disconnected."""
        connected_events: list[str] = []
        disconnected_events: list[str] = []
        sync_lock = threading.Lock()

        def on_connect(handler: Any) -> None:
            with sync_lock:
                connected_events.append(handler.client_id)

        def on_disconnect(handler: Any) -> None:
            with sync_lock:
                disconnected_events.append(handler.client_id)

        running_server.on_client_connected = on_connect
        running_server.on_client_disconnected = on_disconnect

        client = MockSocketClient(running_server.host, running_server.port)
        client.connect()
        client.receive_message()

        time.sleep(0.1)
        with sync_lock:
            assert len(connected_events) == 1
            cid = connected_events[0]

        client.close()
        time.sleep(0.3)
        with sync_lock:
            assert len(disconnected_events) == 1
            assert disconnected_events[0] == cid

    def test_graceful_shutdown_closes_all_connections(self, running_server: TCPServer) -> None:
        """El método stop() debe desconectar a todos los clientes y detener el servidor."""
        client = MockSocketClient(running_server.host, running_server.port)
        client.connect()
        client.receive_message()
        assert running_server.get_connected_count() == 1

        # Detener el servidor
        running_server.stop()
        assert not running_server.is_running
        assert running_server.get_connected_count() == 0

        # El cliente debe experimentar el cierre del socket
        with pytest.raises((ConnectionResetError, TimeoutError, OSError)):
            client.receive_message(max_attempts=3)
        client.close()

        # Llamar a stop() de forma redundante debe ser idempotente
        running_server.stop()


class TestTCPServerEdgeCases:
    """Pruebas de casos borde y métodos auxiliares de TCPServer y ClientHandler."""

    def test_start_when_already_running(self, running_server: TCPServer) -> None:
        """Invocar start() en un servidor ya iniciado no debe lanzar excepciones."""
        assert running_server.is_running
        running_server.start()
        assert running_server.is_running

    def test_get_client_and_get_clients(self, running_server: TCPServer) -> None:
        """Verificar métodos inspectores get_client() y get_clients()."""
        client = MockSocketClient(running_server.host, running_server.port)
        client.connect()
        welcome = client.receive_message()
        cid = welcome["payload"]["client_id"]

        clients_list = running_server.get_clients()
        assert len(clients_list) == 1
        assert clients_list[0].client_id == cid

        handler = running_server.get_client(cid)
        assert handler is not None
        assert handler.client_id == cid

        non_existent = running_server.get_client("non-existent-id")
        assert non_existent is None
        client.close()

    def test_send_message_when_handler_stopped(self, running_server: TCPServer) -> None:
        """Intentar enviar mensaje en un handler detenido retorna False."""
        client = MockSocketClient(running_server.host, running_server.port)
        client.connect()
        welcome = client.receive_message()
        cid = welcome["payload"]["client_id"]

        handler = running_server.get_client(cid)
        assert handler is not None
        handler.stop()
        res = handler.send_message(create_ping())
        assert res is False
        client.close()

    def test_callback_exceptions_do_not_crash_server(self, running_server: TCPServer) -> None:
        """Excepciones lanzadas dentro de los callbacks no deben derribar el servidor."""

        def buggy_callback(*args: Any) -> None:
            raise RuntimeError("Error simulado en callback")

        running_server.on_client_connected = buggy_callback
        running_server.on_client_disconnected = buggy_callback
        running_server.on_message_received = buggy_callback

        client = MockSocketClient(running_server.host, running_server.port)
        client.connect()
        client.receive_message()

        # Enviar mensaje que activa on_message_received
        client.send(create_message("CUSTOM_ACTION", {}))
        time.sleep(0.1)

        # Desconectar para activar on_client_disconnected
        client.close()
        time.sleep(0.2)

        # El servidor debe seguir operativo
        assert running_server.is_running
