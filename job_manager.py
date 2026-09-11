"""
job_manager.py – Fila persistente de jobs com workers e limites de concorrência.

- 1 worker de pesquisa/roteiro (leve, pode ser rápido)
- Semaphore global: 1 render LONGO simultâneo + 2 renders CURTOS Shopee
- Jobs voltam para a fila ao reiniciar o processo (persistidos no SQLite)
- Progresso é publicado via callback registrado pelo bot (notificações Telegram)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from database import get_db
from models import (
    MAX_CONCURRENT_LONG_RENDERS,
    MAX_CONCURRENT_SHORT_RENDERS,
    JobStatus,
    JobType,
)

logger = logging.getLogger(__name__)

# tipo de job → categoria de render
_RENDER_CATEGORY: Dict[str, str] = {
    JobType.GLOBAL_LONG.value: "long",
    JobType.BRASIL_LONG.value: "long",
    JobType.SHOPEE_SHORT.value: "short",
}


def next_job_id(prefix: str) -> str:
    """Gera GLB-YYYYMMDD-XXX / BR-YYYYMMDD-XXX / SP-YYYYMMDD-XXX sequencial."""
    db = get_db()
    today = datetime.now().strftime("%Y%m%d")
    like = f"{prefix}-{today}-%"
    with db._lock:  # noqa: SLF001 – acesso controlado p/ contagem atômica
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT id FROM jobs WHERE id LIKE ? ORDER BY id DESC LIMIT 1", (like,)
        ).fetchall()
    seq = 1
    if rows:
        try:
            seq = int(rows[0]["id"].rsplit("-", 1)[1]) + 1
        except ValueError:
            seq = 1
    return f"{prefix}-{today}-{seq:03d}"


def next_batch_id() -> str:
    """LT-YYYYMMDD-XXX."""
    db = get_db()
    today = datetime.now().strftime("%Y%m%d")
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT id FROM batches WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
            (f"LT-{today}-%",),
        ).fetchall()
    seq = 1
    if rows:
        try:
            seq = int(rows[0]["id"].rsplit("-", 1)[1]) + 1
        except ValueError:
            seq = 1
    return f"LT-{today}-{seq:03d}"


class JobManager:
    """Fila assíncrona + executor de estágios com controle de concorrência."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.long_sem = asyncio.Semaphore(MAX_CONCURRENT_LONG_RENDERS)
        self.short_sem = asyncio.Semaphore(MAX_CONCURRENT_SHORT_RENDERS)
        self._stages: Dict[str, Callable[[str], Awaitable[None]]] = {}
        self._progress_cb: Optional[Callable[[str, str], Awaitable[None]]] = None
        self._running = False
        self._tasks: List[asyncio.Task] = []

    # ---------------------------------------------------------------- setup

    def register_stage(self, status: str, handler: Callable[[str], Awaitable[None]]) -> None:
        """Associa um handler async a um status-alvo do job."""
        self._stages[status] = handler

    def set_progress_callback(
        self, cb: Callable[[str, str], Awaitable[None]]
    ) -> None:
        """cb(job_id, mensagem) — usado pelo bot para notificar progresso."""
        self._progress_cb = cb

    async def notify(self, job_id: str, message: str) -> None:
        if self._progress_cb:
            try:
                await self._progress_cb(job_id, message)
            except Exception as exc:
                logger.warning("Callback de progresso falhou: %s", exc)

    # ----------------------------------------------------------- enfileirar

    async def enqueue(self, job_id: str) -> None:
        await self.queue.put(job_id)
        logger.info("Job %s enfileirado (fila=%d).", job_id, self.queue.qsize())

    async def resume_pending(self) -> int:
        """Re-enfileira jobs retomáveis após restart (pula estados que esperam humano)."""
        from models import NON_RESUMABLE_STATUSES

        count = 0
        for job in get_db().active_jobs():
            if job.status in NON_RESUMABLE_STATUSES:
                continue
            target = _next_stage_for(job.status, job.type)
            if target and target in self._stages:
                await self.queue.put(job.id)
                count += 1
        if count:
            logger.info("%d jobs retomados da fila persistente.", count)
        return count

    # ------------------------------------------------------------ loop

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for i in range(4):  # pool de 4 consumidores; semáforos limitam renders
            self._tasks.append(asyncio.create_task(self._worker_loop(i)))
        logger.info("JobManager iniciado (4 consumidores; long=1, short=2).")

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _worker_loop(self, idx: int) -> None:
        while self._running:
            try:
                job_id = await self.queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._process(job_id)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Worker %d: erro não tratado em %s", idx, job_id)
            finally:
                self.queue.task_done()

    async def _process(self, job_id: str) -> None:
        db = get_db()
        job = db.get_job(job_id)
        if job is None or not job.is_active:
            return
        stage_status = _next_stage_for(job.status, job.type)
        if stage_status is None or stage_status not in self._stages:
            logger.debug("Nada a executar para %s (%s).", job_id, job.status)
            return

        handler = self._stages[stage_status]
        if stage_status == JobStatus.RENDERING.value:
            category = _RENDER_CATEGORY.get(job.type, "long")
            sem = self.long_sem if category == "long" else self.short_sem
            kind = "LONGO" if category == "long" else "curto"
            logger.info("Aguardando licença de render %s para %s…", kind, job_id)
            async with sem:
                await self._safe_run(handler, job_id, stage_status)
        else:
            await self._safe_run(handler, job_id, stage_status)

    async def _safe_run(
        self, handler: Callable[[str], Awaitable[None]], job_id: str, stage: str
    ) -> None:
        db = get_db()
        try:
            await handler(job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Estágio %s falhou para %s", stage, job_id)
            from utils.files import redact_secrets

            db.update_status(job_id, JobStatus.FAILED.value, error=redact_secrets(str(exc)))
            await self.notify(job_id, f"❌ Falhou na etapa *{stage}*. Veja /status {job_id}.")


def _next_stage_for(status: str, job_type: str) -> Optional[str]:
    """Mapeia o status atual do job para o próximo estágio executável."""
    mapping: Dict[str, str] = {
        JobStatus.IDEA.value: JobStatus.RESEARCHING.value,
        JobStatus.QUEUED_RENDER.value: JobStatus.RENDERING.value,
        JobStatus.RENDERED.value: JobStatus.REVIEW_REQUIRED.value,
    }
    return mapping.get(status)


_manager: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager
