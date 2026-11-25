"""Contratos concurrentes del estado de salas."""

from queue import Queue
from threading import Barrier, Event, Thread
from time import monotonic

import pytest

from src.common.protocol import MessageType, create_message
from src.server.models import (
    DuplicateAnswerError,
    DuplicatePlayerNameError,
    Player,
    PlayerAlreadyInRoomError,
    PlayerNotFoundError,
    Room,
    RoomError,
    RoomFullError,
    RoomManager,
    RoomStatus,
)
from src.server.service import RoomService


def run_concurrently(count, operation):
    barrier = Barrier(count, timeout=5)
    results = Queue()

    def worker(index):
        try:
            barrier.wait()
            results.put((True, operation(index)))
        except BaseException as error:
            results.put((False, error))

    threads = [Thread(target=worker, args=(i,), daemon=True) for i in range(count)]
    for thread in threads:
        thread.start()
    deadline = monotonic() + 10
    for thread in threads:
        thread.join(timeout=max(0, deadline - monotonic()))
    assert all(not thread.is_alive() for thread in threads), "Hay hilos bloqueados"
    collected = [results.get_nowait() for _ in threads]
    errors = [value for ok, value in collected if not ok]
    assert not errors, f"Errores inesperados: {errors}"
    return [value for _, value in collected]


def test_duplicate_id_and_managed_room_mutation():
    room = Room("TRV-1", "", "host")
    room.add_player(Player("host", "Host"))
    with pytest.raises(PlayerAlreadyInRoomError):
        room.add_player(Player("host", "Another"))
    manager = RoomManager()
    managed = manager.create_room(Player("a", "Alice"))
    with pytest.raises(RoomError):
        managed.add_player(Player("b", "Bob"))
    with pytest.raises(RoomError):
        managed.remove_player("a")
    assert manager.get_room_by_player("a") is managed
    assert managed.has_player("a")


def test_managed_room_owns_host_flags_without_changing_original_players():
    manager = RoomManager()
    host = Player("host", "Host", is_host=False)
    guest = Player("guest", "Guest", is_host=True)
    room = manager.create_room(host)
    manager.join_room(room.room_id, guest)

    assert host.is_host is False
    assert guest.is_host is True
    assert [(p.player_id, p.is_host) for p in room.get_players()] == [
        ("host", True), ("guest", False)
    ]
    assert manager.get_room_by_player("guest") is room
    manager.leave_room("host")
    assert room.host_id == "guest"
    assert [(p.player_id, p.is_host) for p in room.get_players()] == [("guest", True)]


def test_room_version_tracks_membership_changes_only():
    room = Room("TRV-1", "", "host")
    assert room.version == 1
    room.add_player(Player("host", "Host"))
    assert room.version == 2
    room.record_answer("q1", "host", "A")
    room.update_score("host", 1)
    room.status = RoomStatus.IN_GAME
    assert room.version == 2
    assert room.remove_player("missing") is None
    assert room.version == 2
    room.remove_player("host")
    assert room.version == 3


def test_concurrent_last_place():
    manager = RoomManager()
    room = manager.create_room(Player("host", "Host"), max_players=2)

    def join(i):
        try:
            manager.join_room(room.room_id, Player(str(i), f"Player {i}"))
            return True
        except RoomFullError:
            return False

    assert sum(run_concurrently(20, join)) == 1
    assert room.current_players_count == 2
    for player in room.get_players():
        assert manager.get_room_by_player(player.player_id) is room


def test_concurrent_duplicate_names():
    room = Room("TRV-1", "", "host", max_players=20)
    room.add_player(Player("host", "Host"))

    def join(i):
        try:
            room.add_player(Player(str(i), "Same" if i % 2 else "same"))
            return True
        except DuplicatePlayerNameError:
            return False

    assert sum(run_concurrently(12, join)) == 1
    assert room.current_players_count == 2


def test_same_player_cannot_join_two_rooms_concurrently():
    manager = RoomManager()
    rooms = [manager.create_room(Player(f"h{i}", f"Host {i}")) for i in range(2)]

    def join(i):
        try:
            manager.join_room(rooms[i].room_id, Player("guest", "Guest"))
            return True
        except PlayerAlreadyInRoomError:
            return False

    assert sum(run_concurrently(2, join)) == 1
    assert sum(room.has_player("guest") for room in rooms) == 1
    assert manager.get_room_by_player("guest").has_player("guest")


def test_concurrent_answers_and_scores():
    room = Room("TRV-1", "", "host")
    room.add_player(Player("host", "Host"))

    def answer(_):
        try:
            room.record_answer("q1", "host", "A")
            return True
        except DuplicateAnswerError:
            return False

    assert sum(run_concurrently(16, answer)) == 1
    room.record_answer("q2", "host", "B")
    with pytest.raises(PlayerNotFoundError):
        room.record_answer("q1", "missing", "A")
    answers = room.get_answers("q1")
    answers.clear()
    assert room.get_answers("q1") == {"host": "A"}
    def increment_many(_):
        for _ in range(100):
            room.update_score("host", 1)

    run_concurrently(16, increment_many)
    assert room.get_scores() == {"host": 1600}
    with pytest.raises(ValueError):
        room.update_score("host", True)
    with pytest.raises(PlayerNotFoundError):
        room.update_score("missing", 1)
    room.remove_player("host")
    assert room.get_answers("q1") == {}
    assert room.get_answers("q2") == {}


def test_different_players_answer_simultaneously():
    room = Room("TRV-1", "", "host", max_players=20)
    for i in range(12):
        room.add_player(Player(str(i), f"Player {i}"))
    run_concurrently(12, lambda i: room.record_answer("q1", str(i), "A"))
    assert room.get_answers("q1") == {str(i): "A" for i in range(12)}
    scores = room.get_scores()
    scores.clear()
    assert len(room.get_scores()) == 12
    with pytest.raises(ValueError):
        room.record_answer("", "0", "A")


def test_concurrent_join_leave_keeps_indices_consistent():
    manager = RoomManager()
    room = manager.create_room(Player("host", "Host"), max_players=20)

    def worker(i):
        player_id = f"p{i}"
        for _ in range(10):
            manager.join_room(room.room_id, Player(player_id, player_id))
            assert manager.get_room_by_player(player_id) is room
            manager.leave_room(player_id)
        return player_id

    ids = run_concurrently(12, worker)
    assert room.current_players_count == 1
    assert all(manager.get_room_by_player(player_id) is None for player_id in ids)


def test_creation_reply_precedes_concurrent_broadcast():
    entered, release, joined, attempting = Event(), Event(), Event(), Event()

    class ObservableLock:
        def __init__(self, lock):
            self.lock = lock

        def __enter__(self):
            attempting.set()
            self.lock.acquire()
            return self

        def __exit__(self, *args):
            self.lock.release()

        def acquire(self, *args, **kwargs):
            return self.lock.acquire(*args, **kwargs)

        def release(self):
            self.lock.release()

    class Manager(RoomManager):
        def create_room(self, *args, **kwargs):
            room = super().create_room(*args, **kwargs)
            room.broadcast_lock = ObservableLock(room.broadcast_lock)
            return room

        def join_room(self, room_id, player):
            result = super().join_room(room_id, player)
            joined.set()
            return result

    class Handler:
        def __init__(self, client_id):
            self.client_id = client_id
            self.is_running = True
            self.messages = []

        def send_message(self, message):
            if self.client_id == "host" and message["request_id"] == "create":
                entered.set()
                assert release.wait(5)
            self.messages.append(message)
            return True

    class Server:
        def __init__(self):
            self.clients = {name: Handler(name) for name in ("host", "guest")}

        def get_client(self, client_id):
            return self.clients.get(client_id)

    server = Server()
    manager = Manager()
    service = RoomService(server, manager)
    errors = Queue()

    def invoke(handler, message):
        try:
            service.handle_message(handler, message)
        except BaseException as error:
            errors.put(error)

    creator = Thread(target=invoke, args=(server.clients["host"], create_message(
        MessageType.CREATE_ROOM, {"player_name": "Host"}, "create")), daemon=True)
    joiner = None
    creator.start()
    try:
        assert entered.wait(5)
        room = manager.get_room_by_player("host")
        joiner = Thread(target=invoke, args=(server.clients["guest"], create_message(
            MessageType.JOIN_ROOM, {"room_id": room.room_id, "player_name": "Guest"}, "join")),
            daemon=True)
        joiner.start()
        assert joined.wait(5)
        assert attempting.wait(5), "La difusión no intentó adquirir el lock"
        # La reserva debe existir mientras la respuesta inicial está detenida.
        acquired = room.broadcast_lock.acquire(blocking=False)
        if acquired:
            room.broadcast_lock.release()
        assert not acquired
        assert server.clients["host"].messages == []
    finally:
        release.set()
        creator.join(5)
        if joiner is not None:
            joiner.join(5)
    assert not creator.is_alive() and joiner is not None and not joiner.is_alive()
    assert errors.empty(), list(errors.queue)
    assert server.clients["host"].messages[0]["request_id"] == "create"
    assert len(server.clients["host"].messages[-1]["payload"]["players"]) == 2


def test_creation_send_exception_releases_broadcast_lock():
    class Handler:
        client_id = "host"

        def send_message(self, message):
            raise RuntimeError("falló el envío")

    class Server:
        pass

    manager = RoomManager()
    service = RoomService(Server(), manager)
    with pytest.raises(RuntimeError, match="falló el envío"):
        service.handle_message(Handler(), create_message(
            MessageType.CREATE_ROOM, {"player_name": "Host"}, "create"))
    room = manager.get_room_by_player("host")
    acquired = room.broadcast_lock.acquire(timeout=1)
    assert acquired, "El lock de difusión quedó retenido después del error"
    room.broadcast_lock.release()


def test_broadcast_uses_same_snapshot_for_payload_and_recipients():
    manager = RoomManager()
    room = manager.create_room(Player("host", "Host"))

    class Handler:
        is_running = True

        def __init__(self):
            self.messages = []

        def send_message(self, message):
            self.messages.append(message)
            return True

    class Server:
        def __init__(self):
            self.clients = {"host": Handler(), "guest": Handler()}
            self.joined = False

        def get_client(self, client_id):
            if not self.joined:
                self.joined = True
                manager.join_room(room.room_id, Player("guest", "Guest"))
            return self.clients[client_id]

    server = Server()
    service = RoomService(server, manager)
    service._broadcast_room_state(room)
    assert len(server.clients["host"].messages[0]["payload"]["players"]) == 1
    assert server.clients["guest"].messages == []
    assert room.current_players_count == 2
