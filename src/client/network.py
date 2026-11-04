"""Capa de red del cliente TCP para Trivia Multijugador.

Este módulo implementa la recepción desacoplada del cliente:

- ``NetworkReceiverThread``: hilo secundario (daemon) dedicado exclusivamente
  a leer del socket, reensamblar mensajes con ``decode_messages()`` y
  entregarlos a un callback.
- ``TriviaClient``: administra la conexión saliente, el envío codificado con
  ``encode_message()`` y el ciclo de vida del hilo receptor.

El envío y la recepción corren en hilos distintos: el hilo principal puede
bloquearse en ``input()`` sin que eso impida mostrar los mensajes que llegan
del servidor.
"""

from __future__ import annotations

import codecs
import logging
import socket
import threading
from collections.abc import Callable
from typing import Any

from src.common.protocol import Message, ProtocolError, decode_messages, encode_message

logger = logging.getLogger("TriviaClient")

# Motivos de finalización del hilo receptor.
DISCONNECT_REMOTE = "remote"  # El servidor cerró (FIN) o cortó la conexión.
DISCONNECT_LOCAL = "local"  # Cierre solicitado por el propio cliente.

MessageCallback = Callable[[dict[str, Any]], None]
DisconnectCallback = Callable[[str], None]


class NetworkReceiverThread(threading.Thread):
    """Hilo receptor de mensajes del servidor.

    Hereda de ``threading.Thread`` y se configura como daemon para que nunca
    impida la finalización del proceso. Su único trabajo es recibir: no envía
    ni comparte estado con el hilo principal más allá de los callbacks.
    """

    def __init__(
        self,
        sock: socket.socket,
        on_message: MessageCallback,
        on_disconnect: DisconnectCallback | None = None,
        recv_timeout: float = 0.5,
        bufsize: int = 4096,
    ) -> None:
        """Inicializa el hilo receptor.

        Args:
            sock: Socket TCP ya conectado con el servidor.
            on_message: Callback que recibe cada mensaje completo (dict validado).
            on_disconnect: Callback opcional que recibe el motivo de
                finalización (``DISCONNECT_REMOTE`` o ``DISCONNECT_LOCAL``).
            recv_timeout: Timeout de lectura; convierte ``recv()`` en una
                llamada periódica para poder consultar la petición de parada
                aunque el cierre del socket no interrumpa la lectura.
            bufsize: Tamaño máximo de bytes por lectura.
        """
        # daemon=True: si el proceso termina con el hilo aún bloqueado en
        # recv(), este no retrasa ni impide la finalización del proceso.
        super().__init__(daemon=True, name="NetworkReceiverThread")
        self.socket = sock
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self.recv_timeout = recv_timeout
        self.bufsize = bufsize

        # Evento de intención de parada (a diferencia de un simple bool, no
        # puede perderse si stop() se invoca antes de que run() comience).
        self._stop_requested = threading.Event()
        self._running = False
        self._buffer = ""

        # Decodificador UTF-8 incremental: un carácter multibyte puede quedar
        # partido entre dos recv(); decodificar cada trozo por separado
        # corrompería esos caracteres (el decodificador conserva los bytes
        # incompletos hasta completar el carácter).
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    @property
    def is_running(self) -> bool:
        """Indica si el bucle de recepción continúa activo."""
        return self._running

    @property
    def buffer(self) -> str:
        """Remanente del búfer sin delimitador ``\\n`` pendiente de completar."""
        return self._buffer

    def stop(self) -> None:
        """Pide al bucle que finalice; es seguro invocarlo antes de ``start()``."""
        self._stop_requested.set()

    def run(self) -> None:
        """Bucle de recepción: leer -> reensamblar -> despachar al callback."""
        # El motivo por defecto es el cierre local; solo se sobrescribe si la
        # conexión muere sin que nosotros lo hayamos pedido.
        reason = DISCONNECT_LOCAL
        self._running = True

        try:
            self.socket.settimeout(self.recv_timeout)
        except OSError:
            # El socket puede haberse cerrado desde otro hilo mientras se
            # iniciaba el hilo: el bucle ni siquiera debe comenzar.
            pass

        while not self._stop_requested.is_set():
            try:
                data = self.socket.recv(self.bufsize)
            except TimeoutError:
                # No hay datos disponibles: es el tick del timeout, no un error.
                continue
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as err:
                # Corte abrupto del par (RST). Solo es inesperado si todavía
                # no habíamos solicitado cerrar la conexión.
                if not self._stop_requested.is_set():
                    logger.warning(
                        "Conexión interrumpida de forma abrupta (%s).",
                        err.__class__.__name__,
                    )
                    reason = DISCONNECT_REMOTE
                break
            except OSError as err:
                if not self._stop_requested.is_set():
                    logger.warning("Error de socket durante la recepción: %s", err)
                    reason = DISCONNECT_REMOTE
                break

            # recv() devuelve b"": el servidor envió FIN (desconexión ordenada).
            # Si la parada fue pedida por nosotros, es el resultado esperado.
            if not data:
                if not self._stop_requested.is_set():
                    logger.info("El servidor cerró la conexión.")
                    reason = DISCONNECT_REMOTE
                break

            try:
                # Se acumula el texto decodificado sobre el remanente del búfer
                # para que decode_messages() decida qué mensajes ya están
                # completos y qué fragmento debe conservar para la próxima
                # lectura (framing: sticky packets y fragmentación).
                text = self._decoder.decode(data)
                messages, self._buffer = decode_messages(self._buffer + text)
            except ProtocolError as err:
                # Trama inválida: se descarta el búfer para no dejar al hilo
                # bloqueado re-procesando siempre la misma línea corrupta.
                logger.error("Trama inválida descartada: %s", err)
                self._buffer = ""
                continue

            for message in messages:
                try:
                    self.on_message(message)
                except Exception as err:
                    # Un error en el callback no debe terminar el hilo receptor.
                    logger.exception("Error invocando on_message: %s", err)

        self._running = False

        if self.on_disconnect is not None:
            try:
                self.on_disconnect(reason)
            except Exception as err:
                logger.exception("Error invocando on_disconnect: %s", err)


class TriviaClient:
    """Cliente TCP de Trivia Multijugador con recepción desacoplada.

    El hilo principal utiliza ``send()`` y ``close()``; el hilo receptor
    entrega los mensajes del servidor a través del callback ``on_message``.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5000,
        on_message: MessageCallback | None = None,
        on_disconnect: DisconnectCallback | None = None,
        recv_timeout: float = 0.5,
    ) -> None:
        """Inicializa el cliente (la conexión se establece con ``connect()``).

        Args:
            host: Dirección del servidor.
            port: Puerto del servidor.
            on_message: Callback invocado con cada mensaje recibido.
            on_disconnect: Callback invocado con el motivo de desconexión.
            recv_timeout: Timeout de lectura del hilo receptor.
        """
        self.host = host
        self.port = port
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self.recv_timeout = recv_timeout

        self.socket: socket.socket | None = None
        self.receiver: NetworkReceiverThread | None = None
        self._connected = False

        # Exclusión mutua sobre sendall(): garantiza que dos escrituras
        # simultáneas no se intercalen dentro del mismo flujo TCP.
        self._send_lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        """True mientras exista una conexión activa con el servidor."""
        return self._connected

    def connect(self) -> None:
        """Conecta con el servidor e inicia el hilo receptor.

        Raises:
            OSError: Si la conexión falla. El socket parcial se libera antes
                de relanzar la excepción para no dejar estado inconsistente.
        """
        if self._connected:
            logger.warning("El cliente ya se encuentra conectado.")
            return

        # Limpia restos de una conexión anterior (p. ej. una desconexión
        # remota que dejó el socket y el hilo receptor sin usar).
        self.close()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((self.host, self.port))
        except OSError:
            sock.close()
            raise

        self.socket = sock
        self._connected = True
        self._start_receiver()
        logger.info("Conectado a %s:%d.", self.host, self.port)

    def _start_receiver(self) -> None:
        """Crea e inicia el hilo receptor (la recepción corre en otro hilo)."""
        if self.socket is None:
            return
        self.receiver = NetworkReceiverThread(
            self.socket,
            on_message=self._dispatch_message,
            on_disconnect=self._handle_disconnect,
            recv_timeout=self.recv_timeout,
        )
        self.receiver.start()

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        """Entrega el mensaje al callback del usuario (corre en el hilo receptor)."""
        if self.on_message is not None:
            self.on_message(message)

    def _handle_disconnect(self, reason: str) -> None:
        """Marca al cliente como desconectado y notifica al usuario."""
        if reason == DISCONNECT_REMOTE:
            self._connected = False
        if self.on_disconnect is not None:
            self.on_disconnect(reason)

    def send(self, message: Message | dict[str, Any]) -> bool:
        """Codifica ``message`` con ``encode_message()`` y lo envía por el socket.

        Args:
            message: Instancia de Message o diccionario con el envoltorio
                (type/request_id/payload) del protocolo compartido.

        Returns:
            bool: True si los bytes fueron entregados al socket; False si no
            hay conexión activa o falla la escritura.

        Raises:
            ProtocolError: Si el mensaje no respeta el envoltorio. El socket
            no se toca, por lo que el estado del cliente queda intacto.
        """
        sock = self.socket
        if not self._connected or sock is None:
            logger.warning("send() invocado sin conexión activa.")
            return False

        # La validación ocurre antes de tocar el socket: un mensaje inválido
        # nunca deja al cliente a medias.
        raw = encode_message(message)

        try:
            with self._send_lock:
                sock.sendall(raw)
            return True
        except OSError as err:
            logger.warning("Error al enviar mensaje: %s", err)
            self._connected = False
            return False

    def close(self, join_timeout: float = 2.0) -> None:
        """Cierra la conexión de forma ordenada y detiene el hilo receptor.

        Secuencia de cierre:
        1. Marca al cliente como desconectado (los ``send()`` posteriores fallan).
        2. Pide al hilo receptor que salga de su bucle.
        3. ``shutdown(SHUT_RDWR)`` interrumpe la ``recv()`` bloqueante.
        4. Espera (``join``) a que el hilo finalice.
        5. Cierra el socket y olvida las referencias.

        Es idempotente: puede invocarse varias veces sin efectos secundarios.
        """
        sock = self.socket
        receiver = self.receiver
        if sock is None and receiver is None:
            return

        self._connected = False

        if receiver is not None:
            receiver.stop()

        if sock is not None:
            # shutdown() (y no close()) es lo que interrumpe de forma segura
            # una recv() bloqueante en otro hilo: cerrar el descriptor desde
            # un hilo distinto puede dejar la lectura en un estado indefinido.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        if (
            receiver is not None
            and receiver.is_alive()
            # join() desde el propio hilo receptor (p. ej. dentro del callback
            # on_disconnect) fallaría: se evita explícitamente.
            and receiver is not threading.current_thread()
        ):
            receiver.join(timeout=join_timeout)

        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

        self.socket = None
        self.receiver = None
        logger.info("Conexión cerrada y recursos liberados.")
