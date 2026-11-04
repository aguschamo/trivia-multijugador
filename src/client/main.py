"""Punto de entrada de la aplicación de consola de Trivia Multijugador.

Ejecución:
    python -m src.client.main [--host HOST] [--port PORT]

Comandos del bucle interactivo:
    /ping  Envía un PING: es la única acción de diagnóstico que el servidor
           soporta a nivel de transporte (responde con PONG automáticamente).
    /exit  Cierra la conexión y finaliza el cliente ordenadamente.
    <JSON> Cualquier otro texto se interpreta como un mensaje JSON del
           protocolo (objeto con type/request_id/payload) y se envía al
           servidor codificado con encode_message(). No se inventan tipos
           de mensaje: solo se reenvía lo que el usuario escribe respetando
           el envoltorio compartido.
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from src.client.network import DISCONNECT_REMOTE, TriviaClient
from src.common.protocol import ProtocolError, create_ping


def on_message_received(message: dict[str, Any]) -> None:
    """Imprime cada mensaje recibido (se ejecuta en el hilo receptor).

    Limitación de presentación: la terminal estándar no redibuja el prompt
    ``> `` mientras ``input()`` está activo, así que el mensaje se imprime
    en una línea nueva y el prompt vuelve a aparecer cuando el usuario
    presiona Enter. Esto evita pisar la línea de entrada que el usuario
    puede estar escribiendo (solución sin dependencias externas).
    """
    payload = json.dumps(message.get("payload", {}), ensure_ascii=False)
    print(f"\n[Servidor] {message.get('type')}: {payload}", flush=True)


def on_disconnected(reason: str) -> None:
    """Avisa por consola cuando la caída no fue un cierre propio."""
    if reason == DISCONNECT_REMOTE:
        print("\n[*] La conexión con el servidor fue interrumpida.", flush=True)


def send_text(line: str, client: TriviaClient) -> None:
    """Interpreta la línea ingresada como mensaje JSON del protocolo y lo envía.

    El texto que no sea un objeto JSON no se envía: el servidor descartaría
    cualquier cosa que no sea un envoltorio válido del protocolo.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        print("[!] Ingrese un mensaje JSON del protocolo, /ping o /exit.")
        return

    if not isinstance(data, dict):
        print('[!] El mensaje debe ser un objeto JSON: {"type": ..., "payload": {}}.')
        return

    try:
        client.send(data)
    except ProtocolError as err:
        print(f"[!] Mensaje inválido según el protocolo: {err}")


def run(host: str, port: int) -> int:
    """Ejecuta el bucle interactivo del cliente.

    Returns:
        int: 0 si la salida fue ordenada, 1 si falló la conexión inicial.
    """
    client = TriviaClient(
        host=host,
        port=port,
        on_message=on_message_received,
        on_disconnect=on_disconnected,
    )

    try:
        client.connect()
    except OSError as err:
        print(f"[!] No se pudo conectar a {host}:{port}: {err}")
        return 1

    print(f"[*] Conectado a {host}:{port}. Comandos: /ping, /exit.")

    try:
        # Mientras haya conexión, el hilo principal se dedica a leer input().
        # La recepción ocurre en paralelo: un broadcast del servidor se
        # muestra apenas llega, sin esperar a que el usuario presione Enter.
        while client.is_connected:
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                # Ctrl+C o cierre de stdin se tratan como salida ordenada.
                break

            line = line.strip()
            if not line:
                continue
            if line == "/exit":
                break
            if line == "/ping":
                # create_ping() produce MessageType.PING, la única acción de
                # diagnóstico que el servidor implementa (responde PONG).
                client.send(create_ping())
            else:
                send_text(line, client)
    except KeyboardInterrupt:
        print()
    finally:
        # Cierre garantizado: detiene el hilo receptor y libera el socket.
        client.close()
        print("[*] Cliente finalizado.")

    return 0


def main() -> None:
    """Parsea los argumentos de línea de comandos y arranca el cliente."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Cliente CLI de Trivia Multijugador")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Dirección del servidor (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=5000, help="Puerto del servidor (default: 5000)"
    )
    args = parser.parse_args()

    raise SystemExit(run(args.host, args.port))


if __name__ == "__main__":
    main()
