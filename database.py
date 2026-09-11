"""
database.py – Camada de persistência SQLite do youtube_factory.

Banco local em data/youtube_factory.db com WAL, thread-safe via lock,
API de alto nível para jobs, batches, products, assets e publications.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from models import (
    ACTIVE_STATUSES,
    VALID_TRANSITIONS,
    Batch,
    Job,
    JobStatus,
    utcnow_iso,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "youtube_factory.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    status      TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    batch_id    TEXT,
    folder      TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_batch  ON jobs(batch_id);

CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'planning',
    total_jobs  INTEGER NOT NULL DEFAULT 0,
    done_jobs   INTEGER NOT NULL DEFAULT 0,
    failed_jobs INTEGER NOT NULL DEFAULT 0,
    phase       TEXT NOT NULL DEFAULT 'planning',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL,
    item_id       TEXT NOT NULL,
    shop_id       TEXT DEFAULT '',
    name          TEXT NOT NULL,
    price         REAL DEFAULT 0,
    original_price REAL DEFAULT 0,
    discount      REAL DEFAULT 0,
    rating        REAL DEFAULT 0,
    sold          INTEGER DEFAULT 0,
    images_json   TEXT DEFAULT '[]',
    product_url   TEXT DEFAULT '',
    affiliate_url TEXT DEFAULT '',
    available     INTEGER DEFAULT 1,
    checked_at    TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS assets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    path       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS publications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL,
    platform   TEXT NOT NULL DEFAULT 'youtube',
    url        TEXT NOT NULL,
    published_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
"""


class Database:
    """Acesso serializado ao SQLite (lock + check_same_thread=False)."""

    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info("SQLite pronto em %s", path)

    # ------------------------------------------------------------------ jobs

    def create_job(
        self,
        job_id: str,
        job_type: str,
        title: str,
        payload: Optional[Dict[str, Any]] = None,
        batch_id: Optional[str] = None,
        folder: Optional[str] = None,
    ) -> Job:
        now = utcnow_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id,type,status,title,payload,batch_id,folder,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    job_id, job_type, JobStatus.IDEA.value, title,
                    json.dumps(payload or {}, ensure_ascii=False),
                    batch_id, folder, now, now,
                ),
            )
            self._conn.commit()
        return Job(
            id=job_id, type=job_type, status=JobStatus.IDEA.value, title=title,
            payload=payload or {}, batch_id=batch_id, folder=folder,
            created_at=now, updated_at=now,
        )

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def update_status(
        self, job_id: str, new_status: str, error: Optional[str] = None
    ) -> bool:
        """Atualiza status validando a transição; retorna False se inválida."""
        job = self.get_job(job_id)
        if job is None:
            logger.warning("update_status: job %s não existe", job_id)
            return False
        try:
            cur, nxt = JobStatus(job.status), JobStatus(new_status)
        except ValueError:
            logger.error("update_status: status desconhecido %r", new_status)
            return False
        if nxt not in VALID_TRANSITIONS.get(cur, []):
            logger.warning(
                "Transição de status inválida em %s: %s → %s (ignorada)",
                job_id, cur.value, nxt.value,
            )
            return False
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                (nxt.value, error, utcnow_iso(), job_id),
            )
            self._conn.commit()
        return True

    def force_status(self, job_id: str, new_status: str) -> None:
        """Atualização direta (usar apenas em recuperação/manual)."""
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
                (new_status, utcnow_iso(), job_id),
            )
            self._conn.commit()

    def update_payload(self, job_id: str, **keys: Any) -> None:
        """Merge raso de chaves no payload JSON do job."""
        job = self.get_job(job_id)
        if job is None:
            return
        job.payload.update(keys)
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET payload=?, updated_at=? WHERE id=?",
                (json.dumps(job.payload, ensure_ascii=False), utcnow_iso(), job_id),
            )
            self._conn.commit()

    def set_folder(self, job_id: str, folder: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET folder=?, updated_at=? WHERE id=?",
                (folder, utcnow_iso(), job_id),
            )
            self._conn.commit()

    def list_jobs(
        self, statuses: Optional[List[str]] = None, limit: int = 50
    ) -> List[Job]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            sql = f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY created_at DESC LIMIT ?"
            params: tuple = tuple(statuses) + (limit,)
        else:
            sql = "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_job(r) for r in rows]

    def active_jobs(self) -> List[Job]:
        return self.list_jobs(ACTIVE_STATUSES, limit=200)

    def count_by_status(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) c FROM jobs GROUP BY status"
            ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"], type=row["type"], status=row["status"],
            title=row["title"],
            payload=json.loads(row["payload"] or "{}"),
            batch_id=row["batch_id"], folder=row["folder"], error=row["error"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    # --------------------------------------------------------------- batches

    def create_batch(self, batch_id: str, kind: str, total_jobs: int) -> Batch:
        now = utcnow_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO batches (id,kind,status,total_jobs,phase,created_at)"
                " VALUES (?,?,'planning',?,'planning',?)",
                (batch_id, kind, total_jobs, now),
            )
            self._conn.commit()
        return Batch(id=batch_id, kind=kind, status="planning",
                     total_jobs=total_jobs, created_at=now)

    def get_batch(self, batch_id: str) -> Optional[Batch]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
        if not row:
            return None
        return Batch(
            id=row["id"], kind=row["kind"], status=row["status"],
            total_jobs=row["total_jobs"], done_jobs=row["done_jobs"],
            failed_jobs=row["failed_jobs"], phase=row["phase"],
            created_at=row["created_at"],
        )

    def update_batch(
        self,
        batch_id: str,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        done_delta: int = 0,
        failed_delta: int = 0,
    ) -> None:
        sets, params = [], []
        if status:
            sets.append("status=?"); params.append(status)
        if phase:
            sets.append("phase=?"); params.append(phase)
        if done_delta:
            sets.append("done_jobs=done_jobs+?"); params.append(done_delta)
        if failed_delta:
            sets.append("failed_jobs=failed_jobs+?"); params.append(failed_delta)
        if not sets:
            return
        params.append(batch_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE batches SET {', '.join(sets)} WHERE id=?", params
            )
            self._conn.commit()

    # -------------------------------------------------------------- products

    def save_product_snapshot(self, job_id: str, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO products (job_id,item_id,shop_id,name,price,"
                "original_price,discount,rating,sold,images_json,product_url,"
                "affiliate_url,available,checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    str(snapshot.get("item_id", "")),
                    str(snapshot.get("shop_id", "")),
                    snapshot.get("name", ""),
                    float(snapshot.get("price", 0) or 0),
                    float(snapshot.get("price_original", 0) or 0),
                    float(snapshot.get("discount", 0) or 0),
                    float(snapshot.get("rating", 0) or 0),
                    int(snapshot.get("sold", 0) or 0),
                    json.dumps(snapshot.get("images", [])),
                    snapshot.get("product_url", ""),
                    snapshot.get("affiliate_url", ""),
                    1 if snapshot.get("available", True) else 0,
                    snapshot.get("checked_at") or utcnow_iso(),
                ),
            )
            self._conn.commit()

    def list_products(self, job_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM products WHERE job_id=? ORDER BY id", (job_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["images"] = json.loads(d.pop("images_json") or "[]")
            d["available"] = bool(d["available"])
            out.append(d)
        return out

    # ---------------------------------------------------------------- assets

    def register_asset(self, job_id: str, kind: str, path: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO assets (job_id,kind,path,created_at) VALUES (?,?,?,?)",
                (job_id, kind, path, utcnow_iso()),
            )
            self._conn.commit()

    def list_assets(self, job_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM assets WHERE job_id=? ORDER BY id", (job_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- publications

    def record_publication(self, job_id: str, url: str, platform: str = "youtube") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO publications (job_id,platform,url,published_at)"
                " VALUES (?,?,?,?)",
                (job_id, platform, url, utcnow_iso()),
            )
            self._conn.commit()

    def get_publication(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM publications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_db: Optional[Database] = None


def get_db() -> Database:
    """Singleton do banco (criado sob demanda)."""
    global _db
    if _db is None:
        _db = Database()
    return _db
