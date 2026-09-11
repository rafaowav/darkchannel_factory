"""
keyboards.py – Teclados inline (aiogram 3) do painel Telegram.

Todos os callback_data usam prefixos curtos e sempre incluem o job_id,
exigindo verificação de admin no handler.
"""

from __future__ import annotations

from typing import List, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def kb(rows: List[List[tuple]]) -> InlineKeyboardMarkup:
    """Constrói markup a partir de [[(texto, callback_data), ...], ...]."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=cb) for t, cb in row]
        for row in rows
    ])


# ---------------------------------------------------------------------------
# Menu inicial /novo
# ---------------------------------------------------------------------------

def main_menu() -> InlineKeyboardMarkup:
    return kb([
        [("🌍 Global longo", "new:global_long")],
        [("🇧🇷 Brasil longo", "new:brasil_long")],
        [("🛍️ Shopee Produto", "new:shopee_short")],
        [("📦 Lote semanal", "batch:start")],
        [("📊 Painel", "nav:painel")],
    ])


# ---------------------------------------------------------------------------
# Fluxo GLOBAL/BRASIL
# ---------------------------------------------------------------------------

def visual_query_suggestions(suggestions: List[str]) -> InlineKeyboardMarkup:
    rows = [[(s, f"vq:{s}")] for s in suggestions[:4]]
    rows.append([("✍️ Escrever outra…", "vq:manual")])
    return kb(rows)


def product_source_choices() -> InlineKeyboardMarkup:
    return kb([
        [("🔎 Buscar Shopee", "psrc:search")],
        [("📚 Usar catálogo local", "psrc:catalog")],
        [("🔗 Colar URLs", "psrc:urls")],
    ])


def product_picker(product_ids: List[int], selected: List[int], min_sel: int = 2, max_sel: int = 4) -> InlineKeyboardMarkup:
    rows = []
    for i, pid in enumerate(product_ids):
        mark = "✅" if pid in selected else "⬜"
        rows.append([(f"{mark} Produto {i + 1} (id={pid})", f"ppick:{pid}")])
    ready = min_sel <= len(selected) <= max_sel
    rows.append([("➡️ Confirmar seleção" if ready else f"Selecione {min_sel}-{max_sel} produtos",
                  "pdone" if ready else "noop")])
    return kb(rows)


def duration_choices() -> InlineKeyboardMarkup:
    return kb([
        [("30s", "dur:30"), ("60s", "dur:60"), ("90s", "dur:90")],
    ])


# ---------------------------------------------------------------------------
# Aprovação de roteiro
# ---------------------------------------------------------------------------

def script_approval(job_id: str) -> InlineKeyboardMarkup:
    return kb([
        [("▶️ Renderizar", f"ok:{job_id}"), ("♻️ Regenerar roteiro", f"regen:{job_id}:script")],
        [("❌ Rejeitar", f"rej:{job_id}")],
    ])


# ---------------------------------------------------------------------------
# Revisão final do vídeo
# ---------------------------------------------------------------------------

def review_actions(job_id: str) -> InlineKeyboardMarkup:
    return kb([
        [("🎬 Preview", f"prev:{job_id}"), ("📝 Metadados", f"meta:{job_id}")],
        [("✅ Aprovar", f"ok:{job_id}"), ("♻️ Regenerar thumb", f"regen:{job_id}:thumb")],
        [("♻️ Regenerar vídeo", f"regen:{job_id}:video"), ("❌ Rejeitar", f"rej:{job_id}")],
    ])


# ---------------------------------------------------------------------------
# Lote semanal
# ---------------------------------------------------------------------------

def batch_kinds() -> InlineKeyboardMarkup:
    return kb([
        [("📦 Completo (5G+5B+10S)", "bkind:full")],
        [("🌍 Apenas 5 Global", "bkind:global")],
        [("🇧🇷 Apenas 5 Brasil", "bkind:brasil")],
        [("🛍️ Apenas 10 Shopee", "bkind:shopee")],
        [("🎛️ Personalizado", "bkind:custom")],
    ])


def batch_phase_approve(batch_id: str, phase: str) -> InlineKeyboardMarkup:
    return kb([
        [("▶️ Avançar fase", f"bok:{batch_id}:{phase}")],
        [("⏸️ Pausar lote", f"bpause:{batch_id}")],
    ])


# ---------------------------------------------------------------------------
# Painel / jobs
# ---------------------------------------------------------------------------

def jobs_list(jobs: List[tuple]) -> InlineKeyboardMarkup:
    """jobs: [(job_id, resumo_status)] — paginação simples de 10."""
    rows = [[(f"{jid} — {st}", f"jopen:{jid}")] for jid, st in jobs[:10]]
    return kb(rows) if rows else kb([ [("Nenhum job ativo", "noop")] ])


def painel_nav() -> InlineKeyboardMarkup:
    return kb([
        [("🆕 Novo job", "nav:novo"), ("📋 Meus jobs", "nav:jobs")],
        [("🔄 Atualizar", "nav:painel")],
    ])


def cancel_confirm(job_id: str) -> InlineKeyboardMarkup:
    return kb([
        [("⚠️ Confirmar cancelamento", f"cancel:{job_id}")],
        [("Voltar", f"jopen:{job_id}")],
    ])
