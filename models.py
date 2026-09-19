"""
models.py – Enums, dataclasses e constantes de domínio do youtube_factory.

Define os estados de ciclo de vida dos jobs, tipos de conteúdo e limites de
concorrência usados pelo job_manager e pelos workers.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


class JobType(str, enum.Enum):
    """Tipo de conteúdo gerado por um job."""

    GLOBAL_LONG = "global_long"
    BRASIL_LONG = "brasil_long"
    SHOPEE_SHORT = "shopee_short"
    TIKTOK_SHORT = "tiktok_short"


class JobStatus(str, enum.Enum):
    """Máquina de estados persistente de um job."""

    IDEA = "idea"
    RESEARCHING = "researching"
    SCRIPT_READY = "script_ready"
    AWAITING_SCRIPT_APPROVAL = "awaiting_script_approval"
    QUEUED_RENDER = "queued_render"
    RENDERING = "rendering"
    RENDERED = "rendered"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Transições válidas (para validação defensiva no update_status)
VALID_TRANSITIONS: Dict[JobStatus, List[JobStatus]] = {
    JobStatus.IDEA: [JobStatus.RESEARCHING, JobStatus.CANCELLED, JobStatus.FAILED],
    JobStatus.RESEARCHING: [
        JobStatus.SCRIPT_READY,
        JobStatus.AWAITING_SCRIPT_APPROVAL,
        JobStatus.CANCELLED,
        JobStatus.FAILED,
    ],
    JobStatus.SCRIPT_READY: [
        JobStatus.AWAITING_SCRIPT_APPROVAL,
        JobStatus.RESEARCHING,
        JobStatus.CANCELLED,
        JobStatus.FAILED,
    ],
    JobStatus.AWAITING_SCRIPT_APPROVAL: [
        JobStatus.QUEUED_RENDER,
        JobStatus.RESEARCHING,   # regenerar roteiro
        JobStatus.REJECTED,
        JobStatus.CANCELLED,
        JobStatus.FAILED,
    ],
    JobStatus.QUEUED_RENDER: [
        JobStatus.RENDERING,
        JobStatus.CANCELLED,
        JobStatus.FAILED,
    ],
    JobStatus.RENDERING: [
        JobStatus.RENDERED,
        JobStatus.CANCELLED,
        JobStatus.FAILED,
    ],
    JobStatus.RENDERED: [
        JobStatus.REVIEW_REQUIRED,
        JobStatus.FAILED,
    ],
    JobStatus.REVIEW_REQUIRED: [
        JobStatus.APPROVED,
        JobStatus.REJECTED,
        JobStatus.QUEUED_RENDER,  # regenerar vídeo/thumb
        JobStatus.RESEARCHING,    # regenerar roteiro
        JobStatus.CANCELLED,
    ],
    JobStatus.APPROVED: [
        JobStatus.SCHEDULED,
        JobStatus.PUBLISHED,
        JobStatus.CANCELLED,
    ],
    JobStatus.SCHEDULED: [JobStatus.PUBLISHED, JobStatus.CANCELLED],
    JobStatus.PUBLISHED: [],
    JobStatus.REJECTED: [JobStatus.RESEARCHING, JobStatus.CANCELLED],
    JobStatus.FAILED: [
        JobStatus.IDEA,               # reprocessar do zero (ex.: cota Gemini renovada)
        JobStatus.RESEARCHING,
        JobStatus.QUEUED_RENDER,
        JobStatus.CANCELLED,
    ],
    JobStatus.CANCELLED: [],
}

# Estágios que podem ser re-enfileirados automaticamente após restart
RESUMABLE_STATUSES: List[str] = [
    JobStatus.IDEA.value,
    JobStatus.QUEUED_RENDER.value,
    JobStatus.RENDERED.value,
]

# Status que ainda ocupam a fila (não terminais)
ACTIVE_STATUSES: List[str] = [
    JobStatus.IDEA.value,
    JobStatus.RESEARCHING.value,
    JobStatus.SCRIPT_READY.value,
    JobStatus.AWAITING_SCRIPT_APPROVAL.value,
    JobStatus.QUEUED_RENDER.value,
    JobStatus.RENDERING.value,
    JobStatus.RENDERED.value,
    JobStatus.REVIEW_REQUIRED.value,
    JobStatus.APPROVED.value,
    JobStatus.SCHEDULED.value,
]

# Status que NÃO devem ser retomados automaticamente no restart
# (aguardam decisão humana ou já foram cancelados/finalizados)
NON_RESUMABLE_STATUSES: List[str] = [
    JobStatus.AWAITING_SCRIPT_APPROVAL.value,
    JobStatus.REVIEW_REQUIRED.value,
    JobStatus.APPROVED.value,
    JobStatus.SCHEDULED.value,
    JobStatus.PUBLISHED.value,
    JobStatus.REJECTED.value,
    JobStatus.CANCELLED.value,
]

# Limites de concorrência da fila de renderização
MAX_CONCURRENT_LONG_RENDERS: int = 1
MAX_CONCURRENT_SHORT_RENDERS: int = 2

# Limites de palavras por tipo de roteiro
SCRIPT_LIMITS: Dict[JobType, tuple] = {
    JobType.GLOBAL_LONG: (1300, 1800),
    JobType.BRASIL_LONG: (1200, 1500),
    JobType.SHOPEE_SHORT: (60, 90),
    JobType.TIKTOK_SHORT: (25, 120),
}


@dataclass
class Job:
    """Representação em memória de um registro da tabela jobs."""

    id: str
    type: str
    status: str
    title: str
    payload: Dict[str, Any] = field(default_factory=dict)
    batch_id: Optional[str] = None
    folder: Optional[str] = None
    error: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def job_type(self) -> JobType:
        return JobType(self.type)

    @property
    def job_status(self) -> JobStatus:
        return JobStatus(self.status)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


@dataclass
class Batch:
    """Representação em memória de um lote semanal."""

    id: str
    kind: str
    status: str
    total_jobs: int = 0
    done_jobs: int = 0
    failed_jobs: int = 0
    phase: str = "planning"
    created_at: str = ""


def utcnow_iso() -> str:
    """Timestamp UTC ISO-8601 com segundos."""
    return datetime.utcnow().replace(microsecond=0).isoformat()
