"""Pruebas del proveedor local de preguntas (ISSUE #6).

Cubre:
- Carga del banco predeterminado y su independencia del directorio de trabajo.
- Validación de campos, tipos, IDs únicos, opciones y tiempos.
- Manejo de errores de archivo (inexistente o JSON corrupto) y de esquema.
- Selección aleatoria de N preguntas, ausencia de duplicados y exceso de demanda.
- Inyección de RNG para selecciones deterministas y uso de rutas alternativas.
- Garantía de que ``correct_option`` jamás aparece en el payload público ni en
  el frame serializado, y de que sigue disponible en la representación interna.
"""

from __future__ import annotations

import json
import random
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from src.common.protocol import MessageType, create_message, encode_message
from src.server.questions import (
    DEFAULT_BANK_PATH,
    LocalQuestionProvider,
    NotEnoughQuestionsError,
    Question,
    QuestionBankNotFoundError,
    QuestionFormatError,
    QuestionProvider,
    QuestionProviderError,
)

#: Campos exactos permitidos en el payload público de una pregunta.
PUBLIC_PAYLOAD_FIELDS = {
    "room_id",
    "round_number",
    "total_rounds",
    "question_id",
    "category",
    "difficulty",
    "prompt",
    "options",
    "time_limit_seconds",
}


class CountingRandom(random.Random):
    """Generador aleatorio que registra cuántas veces se invoca ``sample()``."""

    def __init__(self) -> None:
        super().__init__(0)
        self.sample_calls = 0

    def sample(self, population: Any, k: int) -> list[Any]:
        self.sample_calls += 1
        return super().sample(population, k)


def build_question(**overrides: Any) -> dict[str, Any]:
    """Devuelve una pregunta válida de base, sobrescribiendo los campos indicados."""
    question: dict[str, Any] = {
        "id": "Q-T01",
        "category": "ciencia",
        "difficulty": "easy",
        "prompt": "¿Cuánto es 2 + 2?",
        "options": [
            {"id": "A", "text": "3"},
            {"id": "B", "text": "4"},
            {"id": "C", "text": "5"},
            {"id": "D", "text": "6"},
        ],
        "correct_option": "B",
        "time_limit_seconds": 10,
    }
    question.update(overrides)
    return question


def write_bank(tmp_path: Path, questions: Any, *, schema_version: Any = 1) -> Path:
    """Escribe un banco temporal de pruebas y devuelve su ruta."""
    bank_path = tmp_path / "questions.json"
    payload = {"schema_version": schema_version, "questions": questions}
    bank_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return bank_path


def load_all(provider: LocalQuestionProvider) -> list[Question]:
    """Extrae todas las preguntas disponibles del proveedor."""
    return provider.get_questions(provider.available_count())


def payload_of(question: Question) -> dict[str, Any]:
    """Construye el payload público de ``question`` con valores de ejemplo."""
    return question.to_client_payload(
        room_id="TRV-1234",
        round_number=2,
        total_rounds=5,
    )


@pytest.fixture
def default_provider() -> LocalQuestionProvider:
    """Proveedor apuntando al banco real del repositorio."""
    return LocalQuestionProvider()


class TestDefaultBank:
    """Carga del banco predeterminado incluido en el repositorio."""

    def test_path_is_absolute_and_independent_of_working_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """La ruta no depende del directorio desde el que se ejecuta el programa."""
        monkeypatch.chdir(tmp_path)
        provider = LocalQuestionProvider()

        assert provider.bank_path.is_absolute()
        assert provider.bank_path == DEFAULT_BANK_PATH
        assert provider.bank_path.is_file()

    def test_bank_loads_at_least_twenty_questions(
        self, default_provider: LocalQuestionProvider
    ) -> None:
        """Requisito de la ISSUE #6: mínimo 20 preguntas válidas."""
        assert default_provider.available_count() >= 20

    def test_provider_is_a_question_provider(self, default_provider: LocalQuestionProvider) -> None:
        assert isinstance(default_provider, QuestionProvider)

    def test_question_ids_are_unique(self, default_provider: LocalQuestionProvider) -> None:
        ids = [question.question_id for question in load_all(default_provider)]
        assert len(ids) == len(set(ids))

    def test_every_question_has_exactly_four_options(
        self,
        default_provider: LocalQuestionProvider,
    ) -> None:
        assert all(len(question.options) == 4 for question in load_all(default_provider))

    def test_every_question_is_well_formed(self, default_provider: LocalQuestionProvider) -> None:
        allowed_difficulties = {"easy", "medium", "hard"}
        for question in load_all(default_provider):
            assert question.question_id.strip()
            assert question.category.strip()
            assert question.difficulty in allowed_difficulties
            assert question.prompt.strip()
            assert question.correct_option in {option.id for option in question.options}
            assert question.time_limit_seconds > 0
            assert all(option.text.strip() for option in question.options)


class TestBankValidation:
    """Validación de esquema y de cada campo obligatorio del banco."""

    def test_valid_bank_loads(self, tmp_path: Path) -> None:
        bank = write_bank(tmp_path, [build_question(), build_question(id="Q-T02")])
        provider = LocalQuestionProvider(path=bank)

        assert provider.bank_path == bank
        assert provider.available_count() == 2

    @pytest.mark.parametrize(
        "field",
        [
            "id",
            "category",
            "difficulty",
            "prompt",
            "options",
            "correct_option",
            "time_limit_seconds",
        ],
    )
    def test_missing_required_field_raises(self, tmp_path: Path, field: str) -> None:
        question = build_question()
        del question[field]
        bank = write_bank(tmp_path, [question])

        with pytest.raises(QuestionFormatError, match=field):
            LocalQuestionProvider(path=bank)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("id", 42),
            ("id", "   "),
            ("category", ""),
            ("difficulty", "impossible"),
            ("prompt", "   "),
            ("prompt", 123),
            ("options", {"id": "A"}),
            ("options", []),
            ("correct_option", "Z"),
            ("time_limit_seconds", 0),
            ("time_limit_seconds", -1),
            ("time_limit_seconds", True),
            ("time_limit_seconds", "10"),
        ],
    )
    def test_invalid_field_value_raises(self, tmp_path: Path, field: str, value: Any) -> None:
        bank = write_bank(tmp_path, [build_question(**{field: value})])

        with pytest.raises(QuestionFormatError, match=field):
            LocalQuestionProvider(path=bank)

    @pytest.mark.parametrize("missing", ["id", "text"])
    def test_option_requires_id_and_text(self, tmp_path: Path, missing: str) -> None:
        options = [
            {"id": "A", "text": "3"},
            {"id": "B", "text": "4"},
        ]
        del options[1][missing]
        bank = write_bank(tmp_path, [build_question(options=options)])

        with pytest.raises(QuestionFormatError, match=missing):
            LocalQuestionProvider(path=bank)

    def test_duplicate_option_id_raises(self, tmp_path: Path) -> None:
        options = [
            {"id": "A", "text": "3"},
            {"id": "A", "text": "4"},
        ]
        bank = write_bank(tmp_path, [build_question(options=options)])

        with pytest.raises(QuestionFormatError, match="duplicado"):
            LocalQuestionProvider(path=bank)

    def test_duplicate_question_id_raises(self, tmp_path: Path) -> None:
        bank = write_bank(tmp_path, [build_question(), build_question()])

        with pytest.raises(QuestionFormatError, match="duplicado"):
            LocalQuestionProvider(path=bank)

    def test_correct_option_must_exist_in_options(self, tmp_path: Path) -> None:
        bank = write_bank(tmp_path, [build_question(correct_option="X")])

        with pytest.raises(QuestionFormatError, match="correct_option"):
            LocalQuestionProvider(path=bank)

    def test_error_message_includes_question_index(self, tmp_path: Path) -> None:
        """El mensaje de error identifica cuál es la pregunta inválida."""
        bank = write_bank(tmp_path, [build_question(), build_question(prompt="   ")])

        with pytest.raises(QuestionFormatError) as exc_info:
            LocalQuestionProvider(path=bank)

        assert "#2" in str(exc_info.value)

    @pytest.mark.parametrize("schema_version", [None, 0, 2, "1"])
    def test_unsupported_schema_version_raises(self, tmp_path: Path, schema_version: Any) -> None:
        bank = write_bank(tmp_path, [build_question()], schema_version=schema_version)

        with pytest.raises(QuestionFormatError, match="esquema"):
            LocalQuestionProvider(path=bank)

    def test_questions_field_must_be_a_list(self, tmp_path: Path) -> None:
        bank = write_bank(tmp_path, {"id": "Q-T01"})

        with pytest.raises(QuestionFormatError, match="questions"):
            LocalQuestionProvider(path=bank)

    def test_empty_question_list_raises(self, tmp_path: Path) -> None:
        bank = write_bank(tmp_path, [])

        with pytest.raises(QuestionFormatError, match="no contiene preguntas"):
            LocalQuestionProvider(path=bank)

    def test_root_must_be_an_object(self, tmp_path: Path) -> None:
        bank = tmp_path / "questions.json"
        bank.write_text("[]", encoding="utf-8")

        with pytest.raises(QuestionFormatError, match="objeto JSON"):
            LocalQuestionProvider(path=bank)


class TestFileErrors:
    """Errores de lectura del archivo de preguntas."""

    def test_missing_file_raises_with_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "no-existe.json"

        with pytest.raises(QuestionBankNotFoundError) as exc_info:
            LocalQuestionProvider(path=missing)

        assert str(missing) in str(exc_info.value)

    def test_invalid_json_raises_format_error(self, tmp_path: Path) -> None:
        bank_path = tmp_path / "questions.json"
        bank_path.write_text('{"schema_version": 1, "questions": [', encoding="utf-8")

        with pytest.raises(QuestionFormatError, match="JSON"):
            LocalQuestionProvider(path=bank_path)

    def test_missing_default_bank_raises(self, tmp_path: Path) -> None:
        """Una ruta por defecto inexistente también informa el problema con claridad."""
        with pytest.raises(QuestionBankNotFoundError) as exc_info:
            LocalQuestionProvider(path=tmp_path / "data" / "questions.json")

        assert "questions.json" in str(exc_info.value)


class TestQuestionSelection:
    """Selección aleatoria de N preguntas por partida."""

    def test_returns_requested_count(self, default_provider: LocalQuestionProvider) -> None:
        selection = default_provider.get_questions(5)

        assert isinstance(selection, list)
        assert len(selection) == 5
        assert all(isinstance(question, Question) for question in selection)

    def test_selection_has_no_duplicate_ids(self, default_provider: LocalQuestionProvider) -> None:
        for _ in range(20):
            ids = [question.question_id for question in default_provider.get_questions(8)]
            assert len(ids) == len(set(ids))

    def test_full_bank_selection_contains_every_question(
        self,
        default_provider: LocalQuestionProvider,
    ) -> None:
        selection = load_all(default_provider)

        assert len(selection) == default_provider.available_count()
        assert {q.question_id for q in selection} == {
            q.question_id for q in load_all(default_provider)
        }

    def test_selection_can_be_retained_and_reused(
        self, default_provider: LocalQuestionProvider
    ) -> None:
        """La selección de una partida se conserva sin volver a sortearse."""
        total = default_provider.available_count()
        selection = default_provider.get_questions(4)
        snapshot = [question.question_id for question in selection]

        default_provider.get_questions(4)

        assert selection is not None
        assert [question.question_id for question in selection] == snapshot
        selection.clear()
        assert default_provider.available_count() == total

    def test_requesting_more_than_available_raises(
        self, default_provider: LocalQuestionProvider
    ) -> None:
        available = default_provider.available_count()

        with pytest.raises(NotEnoughQuestionsError) as exc_info:
            default_provider.get_questions(available + 1)

        message = str(exc_info.value)
        assert str(available + 1) in message
        assert str(available) in message

    @pytest.mark.parametrize("count", [0, -1, True, "3", 2.5])
    def test_invalid_count_raises(
        self, default_provider: LocalQuestionProvider, count: Any
    ) -> None:
        with pytest.raises(QuestionProviderError, match="entero"):
            default_provider.get_questions(count)

    def test_injected_rng_is_used(self, tmp_path: Path) -> None:
        rng = CountingRandom()
        bank = write_bank(tmp_path, [build_question(), build_question(id="Q-T02")])

        LocalQuestionProvider(path=bank, rng=rng).get_questions(1)

        assert rng.sample_calls == 1

    def test_same_seed_produces_same_selection(self, tmp_path: Path) -> None:
        questions = [build_question(id=f"Q-{index:03d}") for index in range(15)]
        bank = write_bank(tmp_path, questions)

        first = LocalQuestionProvider(path=bank, rng=random.Random(2026)).get_questions(5)
        second = LocalQuestionProvider(path=bank, rng=random.Random(2026)).get_questions(5)

        assert [q.question_id for q in first] == [q.question_id for q in second]

    def test_alternative_path_is_used(self, tmp_path: Path) -> None:
        bank = write_bank(tmp_path, [build_question(), build_question(id="Q-T02")])

        provider = LocalQuestionProvider(path=bank)

        assert provider.bank_path == bank
        assert provider.bank_path != DEFAULT_BANK_PATH
        assert provider.available_count() == 2


class TestAnswerIsNeverPublic:
    """Garantía crítica: la respuesta correcta no se filtra al cliente."""

    def test_internal_question_exposes_correct_option(
        self,
        default_provider: LocalQuestionProvider,
    ) -> None:
        question = load_all(default_provider)[0]

        assert isinstance(question.correct_option, str)
        assert question.correct_option in {option.id for option in question.options}

    def test_client_payload_has_exact_allowlist(
        self,
        default_provider: LocalQuestionProvider,
    ) -> None:
        payload = payload_of(load_all(default_provider)[0])

        assert set(payload) == PUBLIC_PAYLOAD_FIELDS

    def test_client_payload_never_contains_correct_option(
        self,
        default_provider: LocalQuestionProvider,
    ) -> None:
        for question in load_all(default_provider):
            payload = payload_of(question)

            assert "correct_option" not in payload
            assert "correct_option_id" not in payload
            assert "correct_option" not in json.dumps(payload, ensure_ascii=False)

    def test_question_message_frame_has_no_answer(
        self,
        default_provider: LocalQuestionProvider,
    ) -> None:
        """El frame NDJSON completo de QUESTION nunca contiene la respuesta."""
        payload = payload_of(load_all(default_provider)[0])
        message = create_message(MessageType.QUESTION, payload=payload, request_id="req-q-1")

        raw_frame = encode_message(message)

        assert raw_frame.endswith(b"\n")
        assert b"correct_option" not in raw_frame
        assert b"correct_option_id" not in raw_frame

    def test_payload_options_are_independent_copies(
        self,
        default_provider: LocalQuestionProvider,
    ) -> None:
        question = load_all(default_provider)[0]
        payload = payload_of(question)
        original_text = question.options[0].text

        payload["options"][0]["text"] = "MODIFICADO"

        assert question.options[0].text == original_text

    def test_payload_is_a_new_object_on_every_call(
        self,
        default_provider: LocalQuestionProvider,
    ) -> None:
        question = load_all(default_provider)[0]

        first = payload_of(question)
        second = payload_of(question)

        assert first is not second
        assert first["options"] is not second["options"]

    def test_question_is_immutable(self, default_provider: LocalQuestionProvider) -> None:
        question = load_all(default_provider)[0]

        with pytest.raises(FrozenInstanceError):
            question.correct_option = "A"  # type: ignore[misc]

    def test_question_has_no_generic_conversion_helpers(
        self,
        default_provider: LocalQuestionProvider,
    ) -> None:
        """La representación interna no expone convertidores que puedan filtrar campos."""
        question = load_all(default_provider)[0]

        assert not hasattr(question, "asdict")
        assert not hasattr(question, "to_dict")
        assert "correct_option" not in payload_of(question)
