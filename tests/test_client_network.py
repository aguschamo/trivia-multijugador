"""Pruebas del cliente con recepción desacoplada (ISSUE-03).

Cobertura de criterios de aceptación:
1. Conexión correcta al servidor.
2. Envío de mensajes codificados con el protocolo compartido.
3. Recepción y decodificación de mensajes.
4. Varios mensajes en una misma lectura TCP.
5. Mensajes fragmentados entre lecturas distintas.
6. El callback recibe cada mensaje completo una sola vez.
7. La recepción continúa mientras el hilo principal espera en input().
8. Detección de la desconexión del servidor.
9. El cierre libera recursos y finaliza el hilo receptor.
10. Errores de conexión sin estado inconsistente.

Los tests de enmarcado usan un socket "guionizado" para que cada recv()
devuelva exactamente los bytes que el test prescribe: aislando el framing de
la segmentación real de TCP, los casos quedan deterministas y reproducibles.
Los tests de integración usan un TCPServer real en 127.0.0.1 con puerto
dinámico (sin servidores externos ni conexiones a Internet).
"""

from __future__ import annotations

import socket
import threading
import time
from collections import deque
from collections.abc import Generator
from typing import Any

import pytest

from src.client.network import (
    DISCONNECT_LOCAL,
    DISCONNECT_REMOTE,
    NetworkReceiverThread,
    TriviaClient,
)
from src.common.protocol import MessageType, create_message, create_ping, encode_message
from src.server.network import TCPServer


def wait_until(condition: Any, timeout: float = 3.0, interval: float = 0.01) -> bool:
    """Espera activa hasta que ``condition`` sea verdadera (o agota el timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return bool(condition())


class ScriptedSocket:
    """Socket de prueba que devuelve una secuencia guionizada de lecturas.

    Permite simular con precisión el comportamiento del flujo TCP en pruebas:
    - una única recv() con varios mensajes pegados (sticky packets),
    - varias recv() con fragmentos de un mismo mensaje (fragmentación),
    - un b"" que representa el FIN del servidor.

    Cada elemento de la secuencia es bytes (devuelto por recv) o un
    threading.Event (recv espera a que se señale, simulando datos que aún
    no llegaron). Se asume que cada trozo mide menos que el búfer de lectura.
    """

    def __init__(self, reads: list[bytes | threading.Event]) -> None:
        self._reads = deque(reads)
        self.timeout: float | None = None

    def settimeout(self, value: float | None) -> None:
        """Implementa la interfaz mínima que el hilo receptor utiliza."""
        self.timeout = value

    def recv(self, bufsize: int) -> bytes:
        """Devuelve el siguiente trozo guionizado; sin más datos, simula FIN."""
        while self._reads:
            item = self._reads.popleft()
            if isinstance(item, threading.Event):
                item.wait(timeout=5.0)
                continue
            return item[:bufsize]
        return b""


class TestNetworkReceiverFraming:
    """Criterios 3, 4, 5 y 6: decodificación, agrupación, fragmentación y unicidad."""

    def test_single_message_reaches_callback_exactly_once(self) -> None:
        """Criterio 3 y 6: un mensaje completo se entrega una sola vez."""
        msg = create_message(MessageType.ROOM_STATE, payload={"room_id": "R1"}, request_id="req-1")
        sock = ScriptedSocket([encode_message(msg), b""])
        received: list[dict[str, Any]] = []

        thread = NetworkReceiverThread(sock, on_message=received.append)  # type: ignore[arg-type]
        thread.start()
        thread.join(timeout=3.0)

        assert not thread.is_alive()
        assert len(received) == 1
        assert received[0]["type"] == "ROOM_STATE"
        assert received[0]["request_id"] == "req-1"
        assert received[0]["payload"] == {"room_id": "R1"}
        assert thread.buffer == ""

    def test_multiple_messages_in_a_single_read(self) -> None:
        """Criterio 4: una misma recv() con tres mensajes pegados (sticky packets)."""
        msgs = [
            create_message(MessageType.PING, payload={"n": 1}, request_id="a"),
            create_message(MessageType.PONG, payload={"n": 2}, request_id="b"),
            create_message(MessageType.SCORE_UPDATE, payload={"n": 3}, request_id="c"),
        ]
        single_read = b"".join(encode_message(m) for m in msgs)
        sock = ScriptedSocket([single_read, b""])
        received: list[dict[str, Any]] = []

        thread = NetworkReceiverThread(sock, on_message=received.append)  # type: ignore[arg-type]
        thread.start()
        thread.join(timeout=3.0)

        assert not thread.is_alive()
        assert [m["type"] for m in received] == ["PING", "PONG", "SCORE_UPDATE"]
        assert [m["request_id"] for m in received] == ["a", "b", "c"]

    def test_fragmented_message_is_buffered_until_complete(self) -> None:
        """Criterio 5: el fragmento queda en el búfer y no se entrega a medias."""
        msg = create_message(MessageType.QUESTION, payload={"prompt": "2+2=?"}, request_id="q1")
        raw = encode_message(msg)
        split = len(raw) // 2
        gate = threading.Event()  # retiene la segunda lectura hasta que el test lo permita
        sock = ScriptedSocket([raw[:split], gate, raw[split:], b""])
        received: list[dict[str, Any]] = []

        thread = NetworkReceiverThread(sock, on_message=received.append)  # type: ignore[arg-type]
        thread.start()

        # Tras la primera lectura el mensaje está incompleto: vive en el búfer.
        assert wait_until(lambda: thread.buffer == raw[:split].decode("utf-8"))
        assert received == []  # nada entregado todavía

        # Al llegar el resto, se entrega exactamente un mensaje completo.
        gate.set()
        assert wait_until(lambda: len(received) == 1)
        thread.join(timeout=3.0)

        assert received[0] == msg
        assert thread.buffer == ""

    def test_multibyte_character_split_across_reads(self) -> None:
        """Criterio 5: un carácter UTF-8 partido entre lecturas no se corrompe."""
        prompt = "¿Cuánto es 2+2?"
        msg = create_message(MessageType.QUESTION, payload={"prompt": prompt}, request_id="q-utf8")
        raw = encode_message(msg)

        # Corta a la mitad del primer byte multibyte (el '¿' = 0xC2 0xBF).
        cut = raw.find(b"\xc2") + 1
        assert cut > 0
        sock = ScriptedSocket([raw[:cut], raw[cut:], b""])
        received: list[dict[str, Any]] = []

        thread = NetworkReceiverThread(sock, on_message=received.append)  # type: ignore[arg-type]
        thread.start()
        thread.join(timeout=3.0)

        assert len(received) == 1
        assert received[0]["payload"]["prompt"] == prompt

    def test_callback_invoked_once_per_message(self) -> None:
        """Criterio 6: N mensajes entregados => exactamente N invocaciones."""
        msgs = [
            create_message(MessageType.ROUND_RESULT, payload={"i": i}, request_id=str(i))
            for i in range(5)
        ]
        sock = ScriptedSocket([b"".join(encode_message(m) for m in msgs), b""])
        received: list[dict[str, Any]] = []

        thread = NetworkReceiverThread(sock, on_message=received.append)  # type: ignore[arg-type]
        thread.start()
        thread.join(timeout=3.0)

        # El hilo terminó: no quedan mensajes en vuelo que puedan duplicarse.
        assert not thread.is_alive()
        assert len(received) == 5
        assert [m["request_id"] for m in received] == ["0", "1", "2", "3", "4"]


class TestNetworkReceiverLifecycle:
    """Criterios 8 y 9: desconexión remota, cierre local y finalización del hilo."""

    def test_detects_remote_disconnect(self) -> None:
        """Criterio 8: recv() con b"" (FIN del servidor) notifica DISCONNECT_REMOTE."""
        local_sock, peer_sock = socket.socketpair()
        reasons: list[str] = []

        thread = NetworkReceiverThread(
            local_sock,
            on_message=lambda m: None,
            on_disconnect=reasons.append,
        )
        thread.start()
        peer_sock.close()  # el "servidor" cierra la conexión

        assert wait_until(lambda: not thread.is_alive())
        assert reasons == [DISCONNECT_REMOTE]
        local_sock.close()

    def test_local_stop_ends_thread_without_error(self) -> None:
        """Criterio 9: stop() + shutdown() finalizan el hilo como cierre local."""
        local_sock, peer_sock = socket.socketpair()
        reasons: list[str] = []

        thread = NetworkReceiverThread(
            local_sock,
            on_message=lambda m: None,
            on_disconnect=reasons.append,
        )
        thread.start()
        # El hilo queda bloqueado en recv() dentro de un socket real.
        assert wait_until(lambda: thread.is_running)

        thread.stop()
        # shutdown() interrumpe la lectura bloqueante sin generar excepciones.
        local_sock.shutdown(socket.SHUT_RDWR)
        thread.join(timeout=3.0)

        assert not thread.is_alive()
        assert reasons == [DISCONNECT_LOCAL]
        local_sock.close()
        peer_sock.close()


class TestReceptionDuringInput:
    """Criterio 7: la recepción no depende de que el usuario presione Enter."""

    def test_message_arrives_while_waiting_for_input(self, monkeypatch: Any) -> None:
        """Un broadcast llega mientras el "hilo principal" está bloqueado en input()."""
        server_sock, client_sock = socket.socketpair()
        received: list[dict[str, Any]] = []
        input_entered = threading.Event()
        input_release = threading.Event()
        input_finished = threading.Event()

        def fake_input(prompt: str = "") -> str:
            # Simula al usuario bloqueado en input("> ") sin depender de stdin.
            input_entered.set()
            input_release.wait(timeout=5.0)
            input_finished.set()
            return "/exit"

        monkeypatch.setattr("builtins.input", fake_input)

        thread = NetworkReceiverThread(client_sock, on_message=received.append)  # type: ignore[arg-type]
        thread.start()

        # Este hilo hace lo mismo que el bucle de main.py: bloquearse en input().
        user_thread = threading.Thread(target=lambda: input("> "), daemon=True)
        user_thread.start()
        assert input_entered.wait(timeout=3.0)

        # El "servidor" envía un broadcast mientras el usuario sigue en input().
        broadcast = create_message(
            MessageType.ROOM_STATE, payload={"room_id": "R9", "status": "WAITING"}
        )
        server_sock.sendall(encode_message(broadcast))

        # Se recibe (y muestra) sin que input() haya retornado.
        assert wait_until(lambda: len(received) == 1)
        assert not input_finished.is_set()
        assert received[0]["payload"]["room_id"] == "R9"

        input_release.set()
        user_thread.join(timeout=3.0)
        assert not user_thread.is_alive()

        thread.stop()
        client_sock.shutdown(socket.SHUT_RDWR)
        thread.join(timeout=3.0)
        server_sock.close()
        client_sock.close()


class TestTriviaClientIntegration:
    """Criterios 1, 2, 8, 9 y 10 contra un TCPServer real en loopback."""

    @pytest.fixture
    def running_server(self) -> Generator[TCPServer, None, None]:
        """TCPServer en puerto dinámico, garantizando su apagado al terminar."""
        server = TCPServer(host="127.0.0.1", port=0, accept_timeout=0.2)
        server.start(blocking=False)
        time.sleep(0.05)
        yield server
        server.stop()

    def test_connect_and_receive_welcome(self, running_server: TCPServer) -> None:
        """Criterios 1 y 3: conecta, arranca el hilo receptor y recibe WELCOME."""
        received: list[dict[str, Any]] = []
        client = TriviaClient(
            host=running_server.host,
            port=running_server.port,
            on_message=received.append,
        )
        try:
            client.connect()
            assert client.is_connected
            assert client.receiver is not None and client.receiver.is_alive()

            assert wait_until(lambda: any(m["type"] == "WELCOME" for m in received))
            welcome = next(m for m in received if m["type"] == "WELCOME")
            assert "client_id" in welcome["payload"]
        finally:
            client.close()

    def test_send_goes_through_shared_protocol(self, running_server: TCPServer) -> None:
        """Criterio 2: send() codifica con el protocolo compartido (llega válido)."""
        captured: dict[str, Any] = {}
        arrived = threading.Event()

        def on_msg(handler: Any, message: dict[str, Any]) -> None:
            captured.update(message)
            arrived.set()

        running_server.on_message_received = on_msg

        client = TriviaClient(host=running_server.host, port=running_server.port)
        try:
            client.connect()
            msg = create_message(
                MessageType.CREATE_ROOM,
                payload={"player_name": "Agus"},
                request_id="req-create-1",
            )
            assert client.send(msg) is True

            assert arrived.wait(timeout=3.0)
            assert captured["type"] == "CREATE_ROOM"
            assert captured["request_id"] == "req-create-1"
            assert captured["payload"] == {"player_name": "Agus"}
        finally:
            client.close()

    def test_ping_gets_pong_from_server(self, running_server: TCPServer) -> None:
        """Respalda el comando /ping: PING es soportado y el servidor responde PONG."""
        received: list[dict[str, Any]] = []
        client = TriviaClient(
            host=running_server.host,
            port=running_server.port,
            on_message=received.append,
        )
        try:
            client.connect()
            assert client.send(create_ping(request_id="ping-1", timestamp=42.0)) is True

            assert wait_until(lambda: any(m["type"] == "PONG" for m in received))
            pong = next(m for m in received if m["type"] == "PONG")
            assert pong["request_id"] == "ping-1"
            assert pong["payload"]["original_timestamp"] == 42.0
        finally:
            client.close()

    def test_server_disconnect_is_detected(self, running_server: TCPServer) -> None:
        """Criterio 8: al caerse el servidor se notifica DISCONNECT_REMOTE."""
        reasons: list[str] = []
        client = TriviaClient(
            host=running_server.host,
            port=running_server.port,
            on_disconnect=reasons.append,
        )
        try:
            client.connect()
            # Asegura que la conexión quedó establecida antes de cortarla.
            assert wait_until(lambda: running_server.get_connected_count() == 1)

            running_server.stop()

            assert wait_until(lambda: DISCONNECT_REMOTE in reasons)
            assert not client.is_connected
        finally:
            client.close()  # idempotente incluso tras la caída remota

    def test_close_stops_receiver_and_frees_resources(self, running_server: TCPServer) -> None:
        """Criterio 9: close() finaliza el hilo, libera el socket y avisa al servidor."""
        received: list[dict[str, Any]] = []
        client = TriviaClient(
            host=running_server.host,
            port=running_server.port,
            on_message=received.append,
        )
        client.connect()
        receiver = client.receiver
        assert receiver is not None and receiver.is_alive()
        assert wait_until(lambda: any(m["type"] == "WELCOME" for m in received))

        client.close()

        assert not client.is_connected
        assert client.socket is None
        assert client.receiver is None
        assert receiver is not None and not receiver.is_alive()

        # El servidor detecta la desconexión ordenada del cliente.
        assert wait_until(lambda: running_server.get_connected_count() == 0)

        client.close()  # segunda invocación sin efectos secundarios

    def test_send_after_close_returns_false(self, running_server: TCPServer) -> None:
        """Criterio 10: enviar sin conexión activa no lanza excepciones."""
        client = TriviaClient(host=running_server.host, port=running_server.port)
        client.connect()
        client.close()

        assert client.send(create_ping()) is False
        assert not client.is_connected

    def test_connect_error_leaves_consistent_state(self) -> None:
        """Criterio 10: un error de conexión no deja sockets ni hilos colgando."""
        # Puerto con un socket vinculado pero sin listen(): conexión rechazada.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        port = blocker.getsockname()[1]

        client = TriviaClient(host="127.0.0.1", port=port)
        try:
            with pytest.raises(OSError):
                client.connect()

            assert not client.is_connected
            assert client.socket is None
            assert client.receiver is None

            client.close()  # cerrar tras el fallo es inocuo
        finally:
            blocker.close()

        # Un cliente nuevo (o el mismo, ya limpio) puede conectar sin arrastrar
        # el estado del intento fallido.
        server = TCPServer(host="127.0.0.1", port=0, accept_timeout=0.2)
        server.start(blocking=False)
        try:
            retry = TriviaClient(host=server.host, port=server.port)
            retry.connect()
            assert retry.is_connected
            retry.close()
        finally:
            server.stop()
