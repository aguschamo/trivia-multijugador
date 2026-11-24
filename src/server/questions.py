"""Proveedor local de preguntas para Trivia Multijugador (ISSUE #6).

Este módulo implementa un banco de preguntas estático cargado desde un archivo
JSON, con una separación estricta entre:

- La **representación interna** de una pregunta (``Question``), que conserva
  ``correct_option`` y permanece exclusivamente en el servidor.
- La **representación pública** (``Question.to_client_payload()``), construida
  mediante una allowlist explícita de campos, que es la única vía de salida
  hacia los clientes y nunca incluye la respuesta correcta.

El banco se carga y valida una única vez al construir el proveedor; los datos
son inmutables, por lo que pueden leerse desde varios hilos sin sincronización
adicional.
"""

from __future__ import annotations

import json
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Ruta por defecto del banco, resuelta desde la ubicación de este módulo (y no
#: desde el directorio de trabajo desde el que se ejecuta el programa).
DEFAULT_BANK_PATH = Path(__file__).resolve().parents[2] / "data" / "questions.json"

#: Versión de esquema soportada por este proveedor.
SUPPORTED_SCHEMA_VERSION = 1

#: Dificultades admitidas según la especificación del protocolo.
SUPPORTED_DIFFICULTIES = frozenset({"easy", "medium", "hard"})

#: Campos obligatorios de cada pregunta dentro del banco JSON.
REQUIRED_QUESTION_FIELDS = (
    "id",
    "category",
    "difficulty",
    "prompt",
    "options",
    "correct_option",
    "time_limit_seconds",
)

#: Campos obligatorios de cada opción dentro de una pregunta.
REQUIRED_OPTION_FIELDS = ("id", "text")


class QuestionProviderError(Exception):
    """Excepción base para fallas de carga, validación o selección de preguntas."""


class QuestionBankNotFoundError(QuestionProviderError):
    """El archivo del banco de preguntas no existe o no pudo leerse."""


class QuestionFormatError(QuestionProviderError):
    """El banco no cumple con el esquema esperado (JSON corrupto o campo inválido)."""


class NotEnoughQuestionsError(QuestionProviderError):
    """Se solicitaron más preguntas de las disponibles en el banco."""


@dataclass(frozen=True, slots=True)
class QuestionOption:
    """Una opción de respuesta: identificador corto y texto visible."""

    id: str
    text: str


@dataclass(frozen=True, slots=True)
class Question:
    """Representación interna de una pregunta (incluye la respuesta correcta).

    Los datos son inmutables tras su construcción: la respuesta correcta vive
    exclusivamente en el servidor y solo puede transmitirse mediante el payload
    devuelto por :meth:`to_client_payload`.
    """

    question_id: str
    category: str
    difficulty: str
    prompt: str
    options: tuple[QuestionOption, ...]
    correct_option: str
    time_limit_seconds: int

    def to_client_payload(
        self,
        *,
        room_id: str,
        round_number: int,
        total_rounds: int,
    ) -> dict[str, Any]:
        """Construye el payload público esperado por el mensaje ``QUESTION``.

        Los campos se enumeran uno a uno (allowlist): no se utilizan ``asdict``,
        ``__dict__`` ni ninguna conversión genérica de la dataclass, de modo que
        ``correct_option`` no puede filtrarse por accidente. Las opciones se
        copian en diccionarios nuevos para que el llamador pueda manipular el
        payload sin afectar el estado interno.

        El nombre del campo de la respuesta en el futuro ``ROUND_RESULT`` será
        ``correct_option_id``; esa construcción queda fuera del alcance de este
        módulo.
        """
        return {
            "room_id": room_id,
            "round_number": round_number,
            "total_rounds": total_rounds,
            "question_id": self.question_id,
            "category": self.category,
            "difficulty": self.difficulty,
            "prompt": self.prompt,
            "options": [{"id": option.id, "text": option.text} for option in self.options],
            "time_limit_seconds": self.time_limit_seconds,
        }


class QuestionProvider(ABC):
    """Interfaz común de los proveedores de preguntas.

    Permite reemplazar el banco local por otra fuente (por ejemplo, un servicio
    externo con estrategia de fallback) sin cambiar a los consumidores.
    """

    @abstractmethod
    def get_questions(self, count: int) -> list[Question]:
        """Devuelve una selección aleatoria sin repetición de ``count`` preguntas."""

    @abstractmethod
    def available_count(self) -> int:
        """Devuelve la cantidad de preguntas disponibles en la fuente."""


class LocalQuestionProvider(QuestionProvider):
    """Proveedor que carga y valida un banco de preguntas desde un archivo JSON.

    La carga ocurre una sola vez, al construir la instancia: si el archivo no
    existe o no respeta el esquema, se lanza una excepción de
    :class:`QuestionProviderError` con un mensaje que identifica el problema
    (ruta, número de pregunta y campo), de forma que el servidor no arranca con
    un banco inválido.

    Args:
        path: Ruta del banco JSON. Si es ``None`` se usa :data:`DEFAULT_BANK_PATH`.
        rng: Generador aleatorio inyectable para tests deterministas. Si es
            ``None`` se crea una instancia independiente de :class:`random.Random`.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self._bank_path: Path = Path(path) if path is not None else DEFAULT_BANK_PATH
        self._rng: random.Random = rng if rng is not None else random.Random()
        self._questions: tuple[Question, ...] = self._load()

    @property
    def bank_path(self) -> Path:
        """Ruta del banco efectivamente cargado."""
        return self._bank_path

    def available_count(self) -> int:
        """Cantidad de preguntas válidas cargadas en memoria."""
        return len(self._questions)

    def __len__(self) -> int:
        return len(self._questions)

    def get_questions(self, count: int) -> list[Question]:
        """Selecciona al azar ``count`` preguntas sin repetición dentro de la selección.

        El resultado es una **lista nueva**: el llamador puede conservarla y
        reutilizarla en una misma partida sin que una llamada posterior vuelva a
        sortearla ni modifique lo ya seleccionado.

        Raises:
            QuestionProviderError: Si ``count`` no es un entero >= 1.
            NotEnoughQuestionsError: Si ``count`` supera la cantidad disponible.
        """
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise QuestionProviderError(
                f"La cantidad de preguntas debe ser un entero >= 1 (recibido: {count!r})."
            )

        available = len(self._questions)
        if count > available:
            raise NotEnoughQuestionsError(
                f"Se solicitaron {count} preguntas, pero solo hay {available} "
                f"disponibles en {self._bank_path}."
            )

        # random.sample() garantiza ausencia de duplicados dentro de la selección
        # y devuelve una lista nueva e independiente del banco interno.
        return list(self._rng.sample(self._questions, count))

    def _load(self) -> tuple[Question, ...]:
        """Lee, parsea y valida el banco completo. Invocado una sola vez."""
        try:
            raw_text = self._bank_path.read_text(encoding="utf-8")
        except FileNotFoundError as err:
            raise QuestionBankNotFoundError(
                f"No se encontró el archivo de preguntas: {self._bank_path}"
            ) from err
        except OSError as err:
            raise QuestionBankNotFoundError(
                f"No se pudo leer el archivo de preguntas {self._bank_path}: {err}"
            ) from err

        try:
            root = json.loads(raw_text)
        except json.JSONDecodeError as err:
            raise QuestionFormatError(
                f"JSON inválido en {self._bank_path} "
                f"(línea {err.lineno}, columna {err.colno}): {err.msg}"
            ) from err

        if not isinstance(root, dict):
            raise QuestionFormatError(
                f"El banco {self._bank_path} debe ser un objeto JSON, no {type(root).__name__}."
            )

        schema_version = root.get("schema_version")
        if schema_version != SUPPORTED_SCHEMA_VERSION:
            raise QuestionFormatError(
                f"Versión de esquema no soportada en {self._bank_path}: "
                f"{schema_version!r} (se esperaba {SUPPORTED_SCHEMA_VERSION})."
            )

        questions_raw = root.get("questions")
        if not isinstance(questions_raw, list):
            raise QuestionFormatError(
                f"El campo 'questions' de {self._bank_path} debe ser una lista, "
                f"no {type(questions_raw).__name__}."
            )
        if not questions_raw:
            raise QuestionFormatError(f"El banco {self._bank_path} no contiene preguntas.")

        parsed: list[Question] = []
        seen_ids: set[str] = set()
        for index, raw_question in enumerate(questions_raw, start=1):
            question = _parse_question(raw_question, index)
            if question.question_id in seen_ids:
                raise QuestionFormatError(
                    f"Pregunta #{index}: id duplicado {question.question_id!r} "
                    f"en {self._bank_path}."
                )
            seen_ids.add(question.question_id)
            parsed.append(question)

        return tuple(parsed)


def _require_non_empty_str(value: Any, field: str, context: str) -> str:
    """Valida que ``value`` sea un string no vacío y devuelve su versión recortada."""
    if not isinstance(value, str) or not value.strip():
        raise QuestionFormatError(
            f"{context}: el campo '{field}' debe ser una cadena no vacía (recibido: {value!r})."
        )
    return value.strip()


def _parse_options(raw_options: Any, context: str) -> tuple[QuestionOption, ...]:
    """Valida la lista de opciones de una pregunta."""
    if not isinstance(raw_options, list):
        raise QuestionFormatError(
            f"{context}: el campo 'options' debe ser una lista, no {type(raw_options).__name__}."
        )
    if len(raw_options) < 2:
        raise QuestionFormatError(
            f"{context}: el campo 'options' debe incluir al menos 2 opciones "
            f"(recibido: {len(raw_options)})."
        )

    options: list[QuestionOption] = []
    seen_ids: set[str] = set()
    for position, raw_option in enumerate(raw_options, start=1):
        option_context = f"{context}, opción #{position}"
        if not isinstance(raw_option, dict):
            raise QuestionFormatError(
                f"{option_context}: debe ser un objeto JSON con "
                f"{', '.join(REQUIRED_OPTION_FIELDS)}."
            )

        missing = [field for field in REQUIRED_OPTION_FIELDS if field not in raw_option]
        if missing:
            raise QuestionFormatError(
                f"{option_context}: faltan campos obligatorios: {', '.join(missing)}."
            )

        option_id = _require_non_empty_str(raw_option["id"], "id", option_context)
        if option_id in seen_ids:
            raise QuestionFormatError(
                f"{context}: id de opción duplicado {option_id!r} en 'options'."
            )
        seen_ids.add(option_id)

        option_text = _require_non_empty_str(raw_option["text"], "text", option_context)
        options.append(QuestionOption(id=option_id, text=option_text))

    return tuple(options)


def _parse_question(raw_question: Any, index: int) -> Question:
    """Valida una entrada individual del banco y la convierte en :class:`Question`."""
    context = f"Pregunta #{index}"

    if not isinstance(raw_question, dict):
        raise QuestionFormatError(
            f"{context}: debe ser un objeto JSON, no {type(raw_question).__name__}."
        )

    missing = [field for field in REQUIRED_QUESTION_FIELDS if field not in raw_question]
    if missing:
        raise QuestionFormatError(f"{context}: faltan campos obligatorios: {', '.join(missing)}.")

    question_id = _require_non_empty_str(raw_question["id"], "id", context)
    category = _require_non_empty_str(raw_question["category"], "category", context)

    difficulty = _require_non_empty_str(raw_question["difficulty"], "difficulty", context)
    if difficulty not in SUPPORTED_DIFFICULTIES:
        raise QuestionFormatError(
            f"{context}: el campo 'difficulty' debe ser uno de "
            f"{sorted(SUPPORTED_DIFFICULTIES)} (recibido: {difficulty!r})."
        )

    prompt = _require_non_empty_str(raw_question["prompt"], "prompt", context)
    options = _parse_options(raw_question["options"], context)

    correct_option = _require_non_empty_str(
        raw_question["correct_option"], "correct_option", context
    )
    option_ids = {option.id for option in options}
    if correct_option not in option_ids:
        raise QuestionFormatError(
            f"{context}: 'correct_option' ({correct_option!r}) no corresponde a ninguna "
            f"opción válida ({', '.join(sorted(option_ids))})."
        )

    time_limit = raw_question["time_limit_seconds"]
    if isinstance(time_limit, bool) or not isinstance(time_limit, int) or time_limit <= 0:
        raise QuestionFormatError(
            f"{context}: el campo 'time_limit_seconds' debe ser un entero positivo "
            f"(recibido: {time_limit!r})."
        )

    return Question(
        question_id=question_id,
        category=category,
        difficulty=difficulty,
        prompt=prompt,
        options=options,
        correct_option=correct_option,
        time_limit_seconds=time_limit,
    )
