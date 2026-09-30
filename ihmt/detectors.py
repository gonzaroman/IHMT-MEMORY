"""Dynamic detection of material type and knowledge domain.

The detector answers two questions about an incoming document:

* **What is it?** (:class:`~ihmt.models.DataType`) — which decides the splitting
  strategy.
* **Where does it belong?** (the *domain*, a dotted slug such as
  ``software.java``, ``medicine.clinical`` or ``cooking``) — which decides the
  branch of the tree it lands in.

Detection is lexical and language-aware (English + Spanish out of the box).
Both answers can always be overridden explicitly at ingest time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .chunkers.code import LANGUAGE_BY_EXTENSION
from .models import DataType
from .textutils import normalize, slugify

#: Term lexicons keyed by material type. Terms are accent-free and lowercase.
TYPE_LEXICON: Dict[DataType, frozenset] = {
    DataType.CLINICAL: frozenset(
        """
        paciente pacientes diagnostico diagnostica sintomas sintoma tratamiento anamnesis exploracion
        antecedentes alergias medicacion dosis mg posologia receta-medica evolucion pronostico
        consulta ingreso alta clinica historia-clinica tension arterial glucemia analitica
        patient diagnosis symptoms treatment prescription dosage clinical medical chart vitals
        allergies comorbidity follow-up prognosis referral triage admission discharge
        """.split()
    ),
    DataType.PROCESS: frozenset(
        """
        ingredientes ingrediente receta pasos paso preparacion mezclar cocinar hornear anadir batir
        reposar servir raciones coccion utensilios materiales procedimiento protocolo instrucciones
        ingredients recipe steps step preparation mix cook bake stir whisk simmer serve servings
        procedure protocol instructions checklist runbook requirements assemble install configure
        """.split()
    ),
    DataType.PERSONAL: frozenset(
        """
        hoy ayer manana diario entrada sesion recordar recuerdo me-siento cansado contento familia
        amigos vacaciones cumpleanos casa mudanza trabajo jefe novia novio quede quedamos
        today yesterday tomorrow diary journal entry felt remember reminder weekend holiday
        birthday family friends moved apartment girlfriend boyfriend chatted call meetup
        """.split()
    ),
    DataType.NARRATIVE: frozenset(
        """
        capitulo escena personaje protagonista relato novela cuento historia habia-una-vez susurro
        miro dijo penso caminaba noche silencio ciudad puerta ventana lluvia
        chapter scene character protagonist novel tale story whispered glanced murmured dusk
        shadows corridor stranger horizon silence
        """.split()
    ),
}

#: Topical lexicons refining the domain slug independently of the type.
TOPIC_LEXICON: Dict[str, frozenset] = {
    "medicine": frozenset(
        "paciente diagnostico tratamiento clinica sintomas medico hospital patient diagnosis clinical "
        "medication doctor nurse hospital".split()
    ),
    "cooking": frozenset(
        "receta ingredientes cocinar horno sarten sabor plato cocina recipe ingredients cook oven pan "
        "flavour dish kitchen bake".split()
    ),
    "education": frozenset(
        "alumno alumnos leccion tema examen apuntes curso profesor aprendizaje ejercicio student lesson "
        "exam notes course teacher learning exercise syllabus".split()
    ),
    "literature": frozenset(
        "novela capitulo personaje narrador poema verso prosa relato chapter character narrator poem "
        "verse prose fiction".split()
    ),
    "finance": frozenset(
        "factura presupuesto ingresos gastos impuestos nomina inversion invoice budget income expenses "
        "taxes payroll investment".split()
    ),
    "travel": frozenset(
        "viaje vuelo hotel maleta playa vacaciones itinerario trip flight hotel luggage beach holiday "
        "itinerary".split()
    ),
    "software": frozenset(
        "api endpoint servidor despliegue repositorio funcion clase bug refactor deploy server "
        "repository function class database".split()
    ),
}

_CODE_SIGNATURES: List[Tuple[re.Pattern[str], float]] = [
    (re.compile(r"^\s*(public|private|protected)\s+.*\{", re.MULTILINE), 3.0),
    (re.compile(r"^\s*(def|class)\s+\w+.*:", re.MULTILINE), 3.0),
    (re.compile(r"^\s*(import|from|#include|package|using)\s+[\w.<\"/]+", re.MULTILINE), 2.0),
    (re.compile(r"^\s*(function|const|let|var)\s+\w+\s*[=(]", re.MULTILINE), 2.0),
    (re.compile(r"[;{}]\s*$", re.MULTILINE), 0.4),
    (re.compile(r"\breturn\b|\bif\s*\(|\bfor\s*\(|=>"), 0.5),
]

_DIALOGUE_RE = re.compile(r"^\s*[—–\-\"“«]", re.MULTILINE)
_CHAT_RE = re.compile(r"^\s*(?:\[\d{1,2}:\d{2}\]\s*)?[A-ZÀ-Ü][\wÀ-ÿ ]{1,20}:\s", re.MULTILINE)
_DATE_ENTRY_RE = re.compile(r"^\s*#{0,4}\s*\[?\d{4}-\d{1,2}-\d{1,2}|^\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}", re.MULTILINE)
_NUMBERED_STEPS_RE = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*•])\s+\S", re.MULTILINE)


@dataclass
class Detection:
    """Outcome of :meth:`DomainDetector.detect`."""

    data_type: DataType
    domain: str
    confidence: float = 0.0
    language: Optional[str] = None
    scores: Dict[str, float] = field(default_factory=dict)
    signals: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "data_type": self.data_type.value,
            "domain": self.domain,
            "confidence": round(self.confidence, 3),
            "language": self.language,
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
            "signals": self.signals,
        }


class DomainDetector:
    """Classifies incoming material by type and domain.

    The detector never fails: an unrecognizable document is classified as
    :attr:`~ihmt.models.DataType.GENERIC` in the ``general`` domain, with a low
    confidence that the caller may act on.
    """

    def detect(
        self,
        text: str,
        *,
        source: str = "",
        data_type: Optional[DataType] = None,
        domain: Optional[str] = None,
    ) -> Detection:
        """Classify ``text``.

        Args:
            text: Raw document content.
            source: Optional filename; its extension is a strong signal.
            data_type: Explicit override for the material type.
            domain: Explicit override for the domain slug.

        Returns:
            A :class:`Detection`. Explicit overrides are honoured verbatim and
            reported with full confidence.
        """
        sample = text[:20000]
        language = self._language_from_source(source)
        signals: List[str] = []
        scores: Dict[str, float] = {}

        if language:
            signals.append(f"extension:{language}")
        code_score = self._code_score(sample) + (6.0 if language else 0.0)
        scores[DataType.CODE.value] = code_score

        words = [w for w in re.findall(r"[\wÀ-ÿ'-]+", normalize(sample)) if w]
        density = max(len(words), 1) / 1000.0
        for kind, lexicon in TYPE_LEXICON.items():
            hits = sum(1 for word in words if word in lexicon)
            scores[kind.value] = hits / density

        scores[DataType.PERSONAL.value] += 2.0 * len(_CHAT_RE.findall(sample)) / max(1, sample.count("\n") / 10 or 1)
        scores[DataType.PERSONAL.value] += 3.0 if _DATE_ENTRY_RE.search(sample) else 0.0
        scores[DataType.NARRATIVE.value] += min(4.0, len(_DIALOGUE_RE.findall(sample)) * 0.5)
        scores[DataType.PROCESS.value] += min(5.0, len(_NUMBERED_STEPS_RE.findall(sample)) * 0.6)

        if data_type is not None:
            detected = data_type
            confidence = 1.0
            signals.append("type:explicit")
        else:
            best_key, best_value = max(scores.items(), key=lambda kv: kv[1])
            runner_up = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0.0
            detected = DataType.coerce(best_key) if best_value >= 2.0 else DataType.GENERIC
            confidence = self._confidence(best_value, runner_up)
            signals.append(f"top:{best_key}={best_value:.1f}")

        resolved_domain = domain or self._domain_for(detected, words, language, source)
        return Detection(
            data_type=detected,
            domain=slugify(resolved_domain),
            confidence=confidence,
            language=language,
            scores=scores,
            signals=signals,
        )

    # ------------------------------------------------------------------ parts
    @staticmethod
    def _language_from_source(source: str) -> Optional[str]:
        if not source:
            return None
        return LANGUAGE_BY_EXTENSION.get(Path(source).suffix.lower())

    @staticmethod
    def _code_score(text: str) -> float:
        """Weighted count of syntactic signatures, normalized per 100 lines."""
        lines = max(1, text.count("\n"))
        total = 0.0
        for pattern, weight in _CODE_SIGNATURES:
            total += weight * len(pattern.findall(text))
        return total * 100.0 / lines

    @staticmethod
    def _confidence(best: float, runner_up: float) -> float:
        """Map an absolute score and its margin into ``[0, 1]``."""
        if best <= 0:
            return 0.0
        margin = (best - runner_up) / best
        strength = min(1.0, best / 12.0)
        return round(min(1.0, 0.45 * strength + 0.55 * margin), 3)

    def _domain_for(
        self,
        data_type: DataType,
        words: List[str],
        language: Optional[str],
        source: str,
    ) -> str:
        """Derive the dotted domain slug for a classified document."""
        if data_type is DataType.CODE:
            return f"software.{language}" if language else "software"

        topic_hits = {
            topic: sum(1 for word in words if word in lexicon)
            for topic, lexicon in TOPIC_LEXICON.items()
        }
        topic, hits = max(topic_hits.items(), key=lambda kv: kv[1]) if topic_hits else ("", 0)

        if data_type is DataType.CLINICAL:
            return "medicine.clinical"
        if data_type is DataType.PROCESS:
            return f"process.{topic}" if hits >= 3 and topic != "software" else "process"
        if data_type is DataType.PERSONAL:
            # A journal usually touches work, family and travel at once; only a
            # strongly dominant topic earns a sub-domain of its own.
            return f"personal.{topic}" if hits >= 12 else "personal"
        if data_type is DataType.NARRATIVE:
            return "literature.narrative"
        if hits >= 3:
            return topic
        stem = Path(source).stem if source else ""
        return slugify(stem, fallback="general") if stem else "general"
