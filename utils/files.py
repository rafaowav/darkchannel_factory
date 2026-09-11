"""
utils/files.py – Pastas por job, nomes seguros e logging rotativo.

Cada job ganha output/jobs/<job_id>_<slug>/ com todos os artefatos do
pipeline (input.json, research.md, script.txt, vídeo, thumbnail, metadata).
Arquivos finais nunca são apagados automaticamente.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
JOBS_ROOT = BASE_DIR / "output" / "jobs"
LOGS_DIR = BASE_DIR / "logs"

_SAFE_RE = re.compile(r"[^a-z0-9]+")


def safe_slug(text: str, max_length: int = 40) -> str:
    """Slug seguro para pastas/arquivos (sem acentos, espaços ou símbolos)."""
    if not text or not text.strip():
        return "job"
    slug = unicodedata.normalize("NFKD", text.lower())
    slug = "".join(c for c in slug if not unicodedata.combining(c))
    slug = _SAFE_RE.sub("_", slug).strip("_")
    return slug[:max_length] or "job"


def ensure_free_space(min_mb: int = 1500) -> None:
    """
    Verifica espaço livre em disco antes de renderizações pesadas.

    Raises:
        OSError: se houver menos de ``min_mb`` livres.
    """
    usage = shutil.disk_usage(str(BASE_DIR))
    free_mb = usage.free // (1024 * 1024)
    if free_mb < min_mb:
        raise OSError(
            f"Espaço em disco insuficiente: {free_mb} MB livres "
            f"(mínimo {min_mb} MB)."
        )


class JobFolders:
    """Diretório padronizado de um job."""

    def __init__(self, job_id: str, title: str) -> None:
        self.job_id = job_id
        self.path = JOBS_ROOT / f"{job_id}_{safe_slug(title)}"
        self.path.mkdir(parents=True, exist_ok=True)

    # artefatos nomeados -------------------------------------------------
    @property
    def input_json(self) -> Path:
        return self.path / "input.json"

    @property
    def research_md(self) -> Path:
        return self.path / "research.md"

    @property
    def product_snapshot_json(self) -> Path:
        return self.path / "product_snapshot.json"

    @property
    def script_txt(self) -> Path:
        return self.path / "script.txt"

    @property
    def narration_mp3(self) -> Path:
        return self.path / "narration.mp3"

    @property
    def subtitles_srt(self) -> Path:
        return self.path / "subtitles.srt"

    @property
    def thumbnail_jpg(self) -> Path:
        return self.path / "thumbnail.jpg"

    @property
    def metadata_json(self) -> Path:
        return self.path / "metadata.json"

    @property
    def review_md(self) -> Path:
        return self.path / "review.md"

    def video_path(self, final_name: str) -> Path:
        """Caminho do vídeo final (nome descritivo com timestamp)."""
        return self.path / final_name

    # helpers -------------------------------------------------------------
    def write_input(self, payload: Dict[str, Any]) -> Path:
        return self.write_text(self.input_json, json.dumps(payload, ensure_ascii=False, indent=2))

    def write_product_snapshot(self, snapshots: Any) -> Path:
        return self.write_text(
            self.product_snapshot_json,
            json.dumps(snapshots, ensure_ascii=False, indent=2),
        )

    def write_script(self, script: str) -> Path:
        return self.write_text(self.script_txt, script)

    def write_review(self, content: str) -> Path:
        return self.write_text(self.review_md, content)

    def write_metadata(self, metadata: Dict[str, Any]) -> Path:
        return self.write_text(
            self.metadata_json, json.dumps(metadata, ensure_ascii=False, indent=2)
        )

    @staticmethod
    def write_text(path: Path, content: str) -> Path:
        path.write_text(content, encoding="utf-8")
        return path


def setup_logging(level: int = logging.INFO) -> None:
    """Logging rotativo em logs/app.log (5 MB x 5) + console."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        LOGS_DIR / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)


def redact_secrets(text: str) -> str:
    """Remove possíveis segredos de mensagens destinadas ao Telegram."""
    text = re.sub(r"(key|token|secret|credential)\S*", "[redigido]", text, flags=re.I)
    text = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "[redigido]", text)
    return text[:600]
