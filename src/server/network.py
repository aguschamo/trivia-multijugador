"""Servidor TCP multihilo y gestión de conexiones para Trivia Multijugador.

Este módulo implementa la capa de red del servidor:
- TCPServer: Administra el ciclo de vida del socket de escucha TCP, la aceptación
  de conexiones y la gestión concurrente de clientes con apagado ordenado.
- ClientHandler: Hilo independiente por cliente que gestiona la recepción continua
  mediante ProtocolHandler y el envío seguro de mensajes JSON delimitados por \\n.
"""

from __future__ import annotations

import logging
import socket
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from src.common.protocol import (
    Message,
    MessageType,
    ProtocolHandler,
    create_message,
    create_pong,
    encode_message,
)

logger = logging.getLogger("TriviaTCPServer")


class ClientHandler(threading.Thread):
    """Hilo dedicado a la atención individual de un socket de cliente conectado.

    Gestiona la lectura continua de bytes, el reensamblado de mensajes enmarcados
    con ProtocolHandler, el despacho de mensajes y la desconexión limpia.
    """

    def __init__(
        self,
        client_socket: socket.socket,
        address: tuple[str, int],
        server: TCPServer,
        client_id: str | None = None,
        recv_timeout: float = 1.0,
    ) -> None:
        """Inicializa el manejador del cliente.

        Args:
            client_socket: Socket TCP conectado al cliente.
            address: Tupla (ip, puerto) remota.
            server: Referencia al TCPServer propietario.
            client_id: Identificador único opcional (se genera UUID si es None).
            recv_timeout: Tiempo límite en segundos para operaciones de lectura.
        """
        super().__init__(daemon=True, name=f"ClientHandler-{address[0]}:{address[1]}")
        self.socket: socket.socket = client_socket
        self.address: tuple[str, int] = address
        self.server: TCPServer = server
        self.client_id: str = client_id or uuid.uuid4().hex[:8]
        self.recv_timeout: float = recv_timeout

        self.protocol_handler = ProtocolHandler()
        self._running: bool = False
        self._send_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        """Indica si el hilo del cliente continúa activo."""
        return self._running

    def run(self) -> None:
        """Bucle principal de ejecución del hilo de atención al cliente."""
        self._running = True
        self.socket.settimeout(self.recv_timeout)
        logger.info("[%s] Conexión establecida desde %s:%d", self.client_id, *self.address)

        # Enviar mensaje formal de bienvenida (WELCOME) según protocolo
        welcome_payload = {
            "client_id": self.client_id,
            "server_version": "0.1.0",
            "server_time": datetime.now(UTC).isoformat(),
        }
        self.send_message(create_message(MessageType.WELCOME, payload=welcome_payload))

        # Notificar al servidor que el cliente completó el handshake inicial
        self.server.notify_client_connected(self)

        try:
            while self._running and self.server.is_running:
                try:
                    data = self.socket.recv(4096)
                except TimeoutError:
                    # Timeout normal de lectura para comprobar si el servidor sigue corriendo
                    continue
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as err:
                    logger.info(
                        "[%s] Conexión cerrada abruptamente por el cliente (%s)",
                        self.client_id,
                        err.__class__.__name__,
                    )
                    break
                except OSError as err:
                    if self._running:
                        logger.warning(
                            "[%s] Error de socket durante lectura: %s", self.client_id, err
                        )
                    break

                # data vacía (b"") indica cierre ordenado por parte del cliente (FIN TCP)
                if not data:
                    logger.info("[%s] Cliente cerró la conexión ordenadamente.", self.client_id)
                    break

                try:
                    messages = self.protocol_handler.feed(data)
                except Exception as err:
                    logger.error("[%s] Error procesando trama recibida: %s", self.client_id, err)
                    continue

                for msg in messages:
                    self._handle_incoming_message(msg)

        finally:
            self._cleanup()

    def _handle_incoming_message(self, message: dict[str, Any]) -> None:
        """Procesa o delega un mensaje completo recibido del cliente."""
        msg_type = message.get("type")
        request_id = message.get("request_id", "")
        payload = message.get("payload", {})

        logger.debug("[%s] Mensaje recibido: %s (req_id=%s)", self.client_id, msg_type, request_id)

        # Manejo automático de PING a nivel de capa de transporte
        if msg_type == MessageType.PING.value:
            orig_ts = payload.get("timestamp")
            pong = create_pong(request_id=request_id, original_timestamp=orig_ts)
            self.send_message(pong)
            return

        # Delegar cualquier otro mensaje al servidor central
        self.server.notify_message_received(self, message)

    def send_message(self, message: Message | dict[str, Any]) -> bool:
        """Serializa y envía un mensaje al socket del cliente de forma sincronizada.

        Args:
            message: Instancia de Message o diccionario válido según el envoltorio.

        Returns:
            bool: True si el mensaje fue enviado exitosamente; False si falló.
        """
        if not self._running:
            return False

        try:
            raw_bytes = encode_message(message)
            with self._send_lock:
                self.socket.sendall(raw_bytes)
            return True
        except (BrokenPipeError, ConnectionResetError, OSError) as err:
            logger.warning("[%s] Error al enviar mensaje: %s", self.client_id, err)
            self.stop()
            return False

    def stop(self) -> None:
        """Detiene la ejecución del hilo y fuerza el cierre de la conexión."""
        if not self._running:
            return
        self._running = False
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass

    def _cleanup(self) -> None:
        """Limpia los recursos del socket y notifica la desconexión al servidor."""
        self._running = False
        try:
            self.socket.close()
        except OSError:
            pass
        self.server.notify_client_disconnected(self)
        logger.info("[%s] Desconexión completada y recursos liberados.", self.client_id)


class TCPServer:
    """Servidor TCP multihilo para Trivia Multijugador.

    Administra el socket de escucha, acepta nuevos clientes en un hilo dedicado
    y delega cada conexión a un ClientHandler independiente.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5000,
        accept_timeout: float = 0.5,
        on_client_connected: Callable[[ClientHandler], None] | None = None,
        on_client_disconnected: Callable[[ClientHandler], None] | None = None,
        on_message_received: Callable[[ClientHandler, dict[str, Any]], None] | None = None,
    ) -> None:
        """Inicializa los parámetros del servidor TCP.

        Args:
            host: Dirección IP o interfaz de escucha.
            port: Puerto TCP (utilizar 0 para asignar puerto dinámico del SO).
            accept_timeout: Timeout en segundos para la llamada a accept().
            on_client_connected: Callback al registrarse un nuevo cliente.
            on_client_disconnected: Callback al desconectarse un cliente.
            on_message_received: Callback al recibir un mensaje (distinto de PING).
        """
        self.host: str = host
        self.port: int = port
        self.accept_timeout: float = accept_timeout

        self.on_client_connected = on_client_connected
        self.on_client_disconnected = on_client_disconnected
        self.on_message_received = on_message_received

        self._server_socket: socket.socket | None = None
        self._running: bool = False
        self._accept_thread: threading.Thread | None = None

        self._clients: dict[str, ClientHandler] = {}
        self._clients_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        """Indica si el servidor se encuentra activo y escuchando."""
        return self._running

    def start(self, blocking: bool = False) -> None:
        """Inicializa el socket de escucha y comienza a recibir conexiones.

        Args:
            blocking: Si es True, el bucle de aceptación corre en el hilo actual;
                      si es False, se ejecuta en un hilo de fondo (recomendado para tests).
        """
        if self._running:
            logger.warning("El servidor ya se encuentra en ejecución.")
            return

        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))

        # Si se pasó port=0, obtener el puerto asignado por el sistema operativo
        if self.port == 0:
            self.port = self._server_socket.getsockname()[1]

        self._server_socket.listen(128)
        self._server_socket.settimeout(self.accept_timeout)
        self._running = True

        logger.info("TCPServer iniciado y escuchando en %s:%d", self.host, self.port)

        if blocking:
            self._accept_loop()
        else:
            self._accept_thread = threading.Thread(
                target=self._accept_loop,
                name="TCPServerAcceptThread",
                daemon=True,
            )
            self._accept_thread.start()

    def _accept_loop(self) -> None:
        """Bucle continuo de aceptación de nuevos sockets de clientes."""
        while self._running:
            try:
                if self._server_socket is None:
                    break
                client_sock, client_addr = self._server_socket.accept()
            except TimeoutError:
                # Comprobación de corte de ejecución
                continue
            except OSError:
                # Socket cerrado o apagado en progreso
                break

            handler = ClientHandler(client_sock, client_addr, server=self)
            with self._clients_lock:
                self._clients[handler.client_id] = handler

            handler.start()

    def stop(self) -> None:
        """Ejecuta un apagado ordenado (graceful shutdown) del servidor.

        Cierra el socket de escucha y detiene a todos los clientes activos.
        """
        if not self._running:
            return

        logger.info("Iniciando apagado ordenado (graceful shutdown) del servidor...")
        self._running = False

        # Cerrar el socket de escucha para interrumpir accept()
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None

        # Desconectar y detener a todos los clientes activos
        with self._clients_lock:
            active_clients = list(self._clients.values())

        for client in active_clients:
            client.stop()

        for client in active_clients:
            client.join(timeout=1.0)

        with self._clients_lock:
            self._clients.clear()

        # Esperar a que el hilo de aceptación termine
        if self._accept_thread and self._accept_thread.is_alive():
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None

        logger.info("TCPServer apagado exitosamente.")

    def broadcast(
        self,
        message: Message | dict[str, Any],
        exclude_client_id: str | None = None,
    ) -> int:
        """Envía un mensaje a todos los clientes actualmente conectados.

        Args:
            message: Mensaje a difundir.
            exclude_client_id: Identificador opcional del cliente a omitir.

        Returns:
            int: Cantidad de clientes a los que se envió el mensaje satisfactoriamente.
        """
        with self._clients_lock:
            recipients = [
                client
                for cid, client in self._clients.items()
                if cid != exclude_client_id and client.is_running
            ]

        delivered = 0
        for client in recipients:
            if client.send_message(message):
                delivered += 1
        return delivered

    def get_connected_count(self) -> int:
        """Retorna la cantidad actual de clientes conectados de forma segura."""
        with self._clients_lock:
            return len(self._clients)

    def get_clients(self) -> list[ClientHandler]:
        """Retorna una copia superficial de la lista de manejadores de clientes conectados."""
        with self._clients_lock:
            return list(self._clients.values())

    def get_client(self, client_id: str) -> ClientHandler | None:
        """Retorna el ClientHandler correspondiente a un client_id o None."""
        with self._clients_lock:
            return self._clients.get(client_id)

    # --- Notificaciones internas de eventos ---

    def notify_client_connected(self, client: ClientHandler) -> None:
        """Invocado por ClientHandler cuando se completa la inicialización."""
        if self.on_client_connected:
            try:
                self.on_client_connected(client)
            except Exception as err:
                logger.exception("Error en callback on_client_connected: %s", err)

    def notify_client_disconnected(self, client: ClientHandler) -> None:
        """Invocado por ClientHandler tras finalizar su ciclo de vida."""
        with self._clients_lock:
            self._clients.pop(client.client_id, None)

        if self.on_client_disconnected:
            try:
                self.on_client_disconnected(client)
            except Exception as err:
                logger.exception("Error en callback on_client_disconnected: %s", err)

    def notify_message_received(self, client: ClientHandler, message: dict[str, Any]) -> None:
        """Invocado por ClientHandler cuando recibe un mensaje del cliente."""
        if self.on_message_received:
            try:
                self.on_message_received(client, message)
            except Exception as err:
                logger.exception("Error en callback on_message_received: %s", err)


if __name__ == "__main__":
    import argparse
    import time

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="Servidor TCP multihilo de Trivia Multijugador")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Dirección IP de escucha (default: 127.0.0.1)"
    )
    parser.add_argument("--port", type=int, default=5000, help="Puerto de escucha (default: 5000)")
    args = parser.parse_args()

    server = TCPServer(host=args.host, port=args.port)
    server.start(blocking=False)
    print(f"[*] Servidor iniciado en {server.host}:{server.port}. Presione Ctrl+C para salir.")

    try:
        while server.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Señal de apagado recibida. Deteniendo servidor...")
    finally:
        server.stop()
        print("[*] Servidor finalizado.")
