"""Suite de pruebas unitarias para el módulo protocol.py.

Verifica la serialización, deserialización, validación del envoltorio y
el manejo robusto de fragmentación y concatenación (sticky packets) sobre sockets TCP.
"""

from __future__ import annotations

import json

import pytest

from src.common.protocol import (
    ErrorCode,
    InvalidEnvelopeError,
    MalformedMessageError,
    Message,
    MessageType,
    create_error_response,
    create_message,
    create_ping,
    create_pong,
    decode_messages,
    decode_to_messages,
    encode_message,
)


class TestMessageEncoding:
    """Pruebas para la serialización de mensajes (encode_message)."""

    def test_encode_message_from_dict(self) -> None:
        raw = {
            "type": MessageType.PING.value,
            "request_id": "req-001",
            "payload": {"timestamp": 12345.67},
        }
        encoded = encode_message(raw)
        assert isinstance(encoded, bytes)
        assert encoded.endswith(b"\n")

        # Verificar que se deserialice exactamente igual
        text = encoded.decode("utf-8")
        data = json.loads(text.strip())
        assert data["type"] == "PING"
        assert data["request_id"] == "req-001"
        assert data["payload"] == {"timestamp": 12345.67}

    def test_encode_message_from_message_instance(self) -> None:
        msg = Message(
            type=MessageType.WELCOME.value,
            payload={"client_id": "c-123", "server_version": "1.0.0"},
            request_id="req-welcome",
        )
        encoded = encode_message(msg)
        assert encoded.endswith(b"\n")
        decoded = json.loads(encoded.decode("utf-8").strip())
        assert decoded["type"] == "WELCOME"
        assert decoded["request_id"] == "req-welcome"
        assert decoded["payload"]["client_id"] == "c-123"

    def test_encode_message_with_kwargs(self) -> None:
        encoded = encode_message(
            msg_type=MessageType.JOIN_ROOM,
            payload={"room_id": "ROOM-99", "player_name": "Carlos"},
            request_id="req-join-01",
        )
        assert encoded.endswith(b"\n")
        data = json.loads(encoded.decode("utf-8").strip())
        assert data["type"] == "JOIN_ROOM"
        assert data["request_id"] == "req-join-01"
        assert data["payload"]["player_name"] == "Carlos"

    def test_encode_message_auto_generates_request_id_if_omitted(self) -> None:
        encoded = encode_message(msg_type="PING", payload={})
        data = json.loads(encoded.decode("utf-8").strip())
        assert "request_id" in data
        assert isinstance(data["request_id"], str)
        assert len(data["request_id"]) > 0

    def test_encode_message_from_dict_without_request_id_or_payload(self) -> None:
        encoded = encode_message({"type": "PING"})
        data = json.loads(encoded.decode("utf-8").strip())
        assert "request_id" in data
        assert data["payload"] == {}

    def test_encode_message_invalid_type_raises_error(self) -> None:
        with pytest.raises(InvalidEnvelopeError, match="Tipo de mensaje no admitido"):
            encode_message(12345)  # type: ignore[arg-type]

    def test_encode_message_no_arguments_raises_error(self) -> None:
        with pytest.raises(InvalidEnvelopeError, match="Debe suministrarse 'message' o 'msg_type'"):
            encode_message()

    def test_encode_message_handles_utf8_special_characters(self) -> None:
        payload = {
            "pregunta": "¿Cuál es la capital del país? España / Perú / México",
            "caracteres": "áéíóú ñ Ñ ç 🎮 🏆",
        }
        encoded = encode_message(msg_type=MessageType.QUESTION, payload=payload)
        text = encoded.decode("utf-8")
        data = json.loads(text.strip())
        assert data["payload"]["pregunta"] == payload["pregunta"]
        assert data["payload"]["caracteres"] == payload["caracteres"]


class TestMessageFramingAndDecoding:
    """Pruebas de enmarcado: concatenación (sticky packets) y fragmentación."""

    def test_sticky_packets_two_concatenated_messages(self) -> None:
        """Criterio de aceptación:

        Dos mensajes pegados en un mismo buffer (msg1\\nmsg2\\n) son decodificados
        correctamente sin corrupción de datos.
        """
        msg1_bytes = encode_message(
            msg_type=MessageType.PING,
            payload={"ping_id": 1},
            request_id="req-1",
        )
        msg2_bytes = encode_message(
            msg_type=MessageType.PONG,
            payload={"pong_id": 2},
            request_id="req-2",
        )

        concatenated_buffer = (msg1_bytes + msg2_bytes).decode("utf-8")

        messages, remaining = decode_messages(concatenated_buffer)

        assert len(messages) == 2
        assert remaining == ""

        assert messages[0]["type"] == "PING"
        assert messages[0]["request_id"] == "req-1"
        assert messages[0]["payload"] == {"ping_id": 1}

        assert messages[1]["type"] == "PONG"
        assert messages[1]["request_id"] == "req-2"
        assert messages[1]["payload"] == {"pong_id": 2}

    def test_sticky_packets_multiple_messages_with_bytes_input(self) -> None:
        """Verifica que decode_messages admita bytes directamente."""
        m1 = encode_message(msg_type="START_GAME", payload={"room_id": "R1"}, request_id="1")
        m2 = encode_message(msg_type="SCORE_UPDATE", payload={"scores": []}, request_id="2")
        m3 = encode_message(msg_type="GAME_OVER", payload={"winner": "Alice"}, request_id="3")

        buffer_bytes = m1 + m2 + m3
        messages, remaining = decode_messages(buffer_bytes)

        assert len(messages) == 3
        assert remaining == ""
        assert [m["type"] for m in messages] == ["START_GAME", "SCORE_UPDATE", "GAME_OVER"]

    def test_packet_fragmentation_incomplete_message(self) -> None:
        """Si un mensaje no termina con \\n, debe quedar retenido en el buffer remanente."""
        raw_message = '{"type":"PING","request_id":"req-1","payload":{}}'
        chunk1 = raw_message[:15]  # '{"type":"PING",'
        chunk2 = raw_message[15:] + "\n"

        # Primer chunk: nada completo aún
        messages1, remaining1 = decode_messages(chunk1)
        assert len(messages1) == 0
        assert remaining1 == chunk1

        # Segundo chunk: se combina con el remanente
        combined = remaining1 + chunk2
        messages2, remaining2 = decode_messages(combined)
        assert len(messages2) == 1
        assert remaining2 == ""
        assert messages2[0]["type"] == "PING"
        assert messages2[0]["request_id"] == "req-1"

    def test_concatenated_with_trailing_fragment(self) -> None:
        """Mensajes completos seguidos de un fragmento sin terminar."""
        m1 = encode_message(msg_type="CREATE_ROOM", payload={"room": "A"}, request_id="1")
        m2 = encode_message(msg_type="JOIN_ROOM", payload={"room": "A"}, request_id="2")
        trailing_fragment = '{"type":"SUBMIT_ANSWER","request_id":"3","pay'

        buffer = (m1 + m2).decode("utf-8") + trailing_fragment

        messages, remaining = decode_messages(buffer)
        assert len(messages) == 2
        assert [m["type"] for m in messages] == ["CREATE_ROOM", "JOIN_ROOM"]
        assert remaining == trailing_fragment

        # Cuando llega el resto del fragmento
        second_arrival = 'load":{"answer":"B"}}\n'
        next_buffer = remaining + second_arrival
        messages_next, remaining_next = decode_messages(next_buffer)
        assert len(messages_next) == 1
        assert remaining_next == ""
        assert messages_next[0]["type"] == "SUBMIT_ANSWER"
        assert messages_next[0]["payload"]["answer"] == "B"

    def test_empty_buffer_and_whitespace(self) -> None:
        assert decode_messages("") == ([], "")
        assert decode_messages("\n\n") == ([], "")
        assert decode_messages("   \n   \n") == ([], "")

    def test_windows_crlf_compatibility(self) -> None:
        raw_crlf = '{"type":"PING","request_id":"1","payload":{}}\r\n'
        messages, remaining = decode_messages(raw_crlf)
        assert len(messages) == 1
        assert remaining == ""
        assert messages[0]["type"] == "PING"


class TestEnvelopeValidation:
    """Pruebas de validación del esquema de envoltorio."""

    def test_missing_type_raises_error(self) -> None:
        bad_msg = '{"request_id":"1","payload":{}}\n'
        with pytest.raises(InvalidEnvelopeError, match="campo 'type' es obligatorio"):
            decode_messages(bad_msg)

    def test_empty_type_raises_error(self) -> None:
        bad_msg = '{"type":"  ","request_id":"1","payload":{}}\n'
        with pytest.raises(InvalidEnvelopeError, match="campo 'type' debe ser un string no vacío"):
            decode_messages(bad_msg)

    def test_missing_request_id_raises_error(self) -> None:
        bad_msg = '{"type":"PING","payload":{}}\n'
        with pytest.raises(InvalidEnvelopeError, match="campo 'request_id' es obligatorio"):
            decode_messages(bad_msg)

    def test_empty_request_id_raises_error(self) -> None:
        bad_msg = '{"type":"PING","request_id":"   ","payload":{}}\n'
        with pytest.raises(
            InvalidEnvelopeError, match="campo 'request_id' debe ser un string no vacío"
        ):
            decode_messages(bad_msg)

    def test_missing_payload_raises_error(self) -> None:
        bad_msg = '{"type":"PING","request_id":"1"}\n'
        with pytest.raises(InvalidEnvelopeError, match="campo 'payload' es obligatorio"):
            decode_messages(bad_msg)

    def test_payload_not_a_dict_raises_error(self) -> None:
        bad_msg = '{"type":"PING","request_id":"1","payload":["item1"]}\n'
        with pytest.raises(InvalidEnvelopeError, match="debe ser un diccionario"):
            decode_messages(bad_msg)

    def test_non_dict_root_raises_error(self) -> None:
        bad_msg = '["not", "a", "dict"]\n'
        with pytest.raises(InvalidEnvelopeError, match="debe ser un objeto JSON"):
            decode_messages(bad_msg)

    def test_malformed_json_syntax_raises_error(self) -> None:
        corrupted = '{"type":"PING", incomplete_json\n'
        with pytest.raises(MalformedMessageError):
            decode_messages(corrupted)


class TestMessageDataclassAndHelpers:
    """Pruebas para Message dataclass y funciones auxiliares del catálogo."""

    def test_message_dataclass_methods(self) -> None:
        msg = Message(
            type=MessageType.QUESTION,
            payload={"prompt": "¿Cuánto es 2+2?"},
            request_id="test-req",
        )
        assert msg.type == "QUESTION"
        dict_repr = msg.to_dict()
        assert dict_repr["type"] == "QUESTION"

        restored = Message.from_dict(dict_repr)
        assert restored.type == msg.type
        assert restored.request_id == msg.request_id
        assert restored.payload == msg.payload

        encoded = msg.encode()
        assert encoded.endswith(b"\n")

    def test_decode_to_messages_typed(self) -> None:
        raw = encode_message(msg_type="PING", payload={}, request_id="abc")
        typed_list, rem = decode_to_messages(raw)
        assert len(typed_list) == 1
        assert rem == ""
        assert isinstance(typed_list[0], Message)
        assert typed_list[0].type == "PING"
        assert typed_list[0].request_id == "abc"

    def test_create_message_helper(self) -> None:
        m = create_message(MessageType.ROUND_RESULT, {"scores": [10, 20]})
        assert m["type"] == "ROUND_RESULT"
        assert m["payload"] == {"scores": [10, 20]}
        assert "request_id" in m

    def test_create_error_response_helper(self) -> None:
        err = create_error_response(
            request_id="req-original",
            code=ErrorCode.ROOM_NOT_FOUND,
            message="La sala no existe",
            details={"room_id": "ROOM-404"},
        )
        assert err["type"] == "ERROR"
        assert err["request_id"] == "req-original"
        assert err["payload"]["code"] == "ROOM_NOT_FOUND"
        assert err["payload"]["message"] == "La sala no existe"
        assert err["payload"]["details"] == {"room_id": "ROOM-404"}

    def test_create_ping_and_pong_helpers(self) -> None:
        ping = create_ping(request_id="ping-1", timestamp=100.5)
        assert ping["type"] == "PING"
        assert ping["payload"]["timestamp"] == 100.5

        pong = create_pong(request_id="ping-1", original_timestamp=100.5)
        assert pong["type"] == "PONG"
        assert pong["request_id"] == "ping-1"
        assert pong["payload"]["original_timestamp"] == 100.5

    def test_catalog_enums_membership(self) -> None:
        # Verificar que todos los tipos requeridos existen en MessageType
        expected_types = {
            "WELCOME",
            "PING",
            "PONG",
            "ERROR",
            "CREATE_ROOM",
            "JOIN_ROOM",
            "ROOM_STATE",
            "START_GAME",
            "QUESTION",
            "SUBMIT_ANSWER",
            "ANSWER_RECEIVED",
            "ROUND_RESULT",
            "SCORE_UPDATE",
            "GAME_OVER",
            "PLAYER_LEFT",
        }
        for item in expected_types:
            assert MessageType.has_value(item), f"Falta tipo en MessageType: {item}"

        # Verificar códigos de error críticos
        expected_errors = {
            "MALFORMED_JSON",
            "INVALID_ENVELOPE",
            "UNKNOWN_MESSAGE_TYPE",
            "INVALID_PAYLOAD",
            "ROOM_NOT_FOUND",
            "ROOM_FULL",
            "GAME_ALREADY_STARTED",
            "NOT_HOST",
            "ALREADY_ANSWERED",
            "TIME_EXPIRED",
            "INVALID_ROUND_STATE",
            "PLAYER_NOT_IN_ROOM",
            "INTERNAL_SERVER_ERROR",
        }
        for err in expected_errors:
            assert ErrorCode.has_value(err), f"Falta código en ErrorCode: {err}"
