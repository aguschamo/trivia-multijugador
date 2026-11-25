# Estado compartido y concurrencia

Cada conexión tiene un hilo `ClientHandler` y su propio búfer de protocolo. El
servicio comparte un `RoomManager` entre esos hilos. Las salas administradas
solo admiten altas y bajas a través del administrador; sus métodos públicos
directos rechazan esas operaciones para mantener el índice sincronizado. Una
sala independiente sí admite `add_player` y `remove_player` directamente.

## Datos y locks

| Lock | Datos o función | Motivo |
| --- | --- | --- |
| `RoomManager._lock` | `_rooms`, `_player_to_room` y generación de códigos | Mantiene atómicas las operaciones globales de creación, ingreso y egreso. |
| `Room._lock` | Jugadores, anfitrión, estado, versión, respuestas y puntajes | Agrupa validaciones y modificaciones de una sala en secciones críticas. |
| `Room.broadcast_lock` | Orden de las difusiones de una sala y respuesta inicial | Serializa la toma de instantáneas y los envíos de esas operaciones. |
| `TCPServer._clients_lock` | Registro de conexiones | Protege altas, bajas y consultas al diccionario de clientes. |
| `ClientHandler._send_lock` | Envíos de un socket | Impide que las tramas concurrentes de ese cliente se mezclen. |
| `TriviaClient._send_lock` | Envíos del socket del cliente | Evita mezclar escrituras concurrentes del lado cliente. |

El orden de datos es **administrador → sala**. La reserva inicial de difusión
se toma al crear la sala, antes de publicarla, con el lock del administrador;
el servicio la libera después de enviar la respuesta inicial. Las difusiones
adquieren primero `broadcast_lock` y luego, por separado, el lock de sala para
la instantánea, el de clientes para buscar destinatarios y el de envío para
cada socket. No llaman al administrador mientras conservan el lock de difusión.
Los callbacks de desconexión se ejecutan después de liberar el lock de clientes.
No se usan `RLock` ni se adquiere recursivamente un lock de datos.

La red no se ejecuta con los locks de datos de sala o administrador tomados.
Sí se mantiene `broadcast_lock` durante los envíos para conservar su orden;
un cliente lento puede demorar las difusiones de esa sala. `sendall` mantiene
su lock de envío por la duración de la escritura. Las mutaciones de sala pueden
ocurrir entre difusiones: una instantánea puede omitir estados intermedios o
repetir uno, y el lock no garantiza entrega a clientes desconectados. El flujo
de creación libera la reserva en `finally`, incluso si el envío lanza una
excepción. El fallo habitual de `ClientHandler.send_message` libera primero el
lock de envío y luego cierra el socket; la notificación de desconexión ocurre
desde la limpieza del hilo, tras liberar el lock de clientes. Un callback ajeno
que invoque sincrónicamente una difusión de la misma sala desde `send_message`
sería reentrante y podría bloquearse; el transporte actual no lo hace.

## Propiedad y límites

La sala conserva una copia de cada `Player` sin modificar el original; sus
consultas devuelven copias. El indicador `is_host` se determina con el ID de
anfitrión interno, ignorando el valor recibido. En una sala administrada no
vacía hay un único anfitrión; si se retira, se transfiere al primer participante.
La configuración e identificadores de sala son de solo lectura, y el estado
se consulta y modifica con el lock. El estado y los destinatarios de una
difusión se obtienen en una sola instantánea; luego pueden cambiar, pero la
instantánea no cambia. Los handlers de red obtenidos del registro siguen siendo
objetos vivos y pueden desconectarse antes del envío.

## Versión de composición

`Room.version` es un contador local de cambios de composición: comienza en 1 y
sube una vez por cada ingreso o salida exitosa, incluida la transferencia de
anfitrión causada por una salida. No cambia por intentos fallidos, estado,
respuestas ni puntajes. No forma parte de `ROOM_STATE` ni se usa para ordenar
difusiones. Se conserva ese comportamiento existente, por lo que no debe
utilizarse para detectar todos los cambios del estado visible o completo.

`record_answer(question_id, player_id, selected_option_id)` registra una
opción por jugador y pregunta. Cuando un jugador abandona la sala, se eliminan
sus respuestas; si vuelve, puede responder de nuevo. Los diccionarios devueltos
por `get_answers` y `get_scores` son copias. `update_score` aplica un delta
entero bajo el mismo lock sin definir reglas de juego. Todavía no se validan
pregunta activa, plazos, corrección ni reglas de puntaje.

Las preguntas locales son inmutables; el proveedor aún no está conectado al
ciclo de salas. Si se comparte en el futuro, debe revisarse el uso concurrente
del generador aleatorio inyectado. Los flags de ciclo de vida de la red y la
gestión de arranque y apagado pertenecen a la capa de transporte y no se han
rediseñado en esta issue.
