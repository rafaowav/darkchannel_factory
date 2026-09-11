# YouTube Factory 🎬

Pipeline automatizado de criação de vídeos para **YouTube** (documentários longos em inglês e português) e **Shopee Vídeo** (reels verticais com produtos reais), orquestrado por um **painel completo no Telegram** com fila persistente, SQLite e aprovação humana obrigatória.

---

## Arquitetura

```
bot_factory.py        Painel Telegram (aiogram 3): FSM, botões, comandos
job_manager.py        Fila asyncio + semáforos (1 render longo | 2 reels)
workers.py            Estágios: research/roteiro → render → review
database.py           SQLite local (data/youtube_factory.db) — jobs, batches,
                      products, assets, publications
models.py             Máquina de estados dos jobs e limites de concorrência
keyboards.py          Teclados inline (aprovação, seleção, painel)
utils/files.py        Pastas por job, slug seguro, logging rotativo, redação de segredos
services/             research · script · shopee · render · metadata
engine.py             FFmpeg, edge-tts, Gemini, Pexels (funções reaproveitadas)
shopee_client.py      API oficial de afiliados Shopee (shopee-afflib)
metadata_generator.py Título/descrição/tags/capítulos (Gemini) + thumbnail Pillow
```

- **Nada é publicado automaticamente** em YouTube/Shopee — o registro é manual via `/publicado`.
- Somente `TELEGRAM_ADMIN_CHAT_ID` pode usar comandos e callbacks.
- Ao reiniciar o processo, jobs retomáveis voltam à fila; jobs aguardando humano ficam parados esperando decisão.

---

## Instalação (Windows)

### 1. Python 3.10+ e FFmpeg

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
ffmpeg -version   # conferir PATH
```

### 2. Ambiente virtual

```powershell
cd youtube_factory
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Variáveis do `.env`

| Variável | Como obter |
|---|---|
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey) |
| `PEXELS_API_KEY` | [Pexels API](https://www.pexels.com/api/) |
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → /newbot |
| `TELEGRAM_ADMIN_CHAT_ID` | Seu ID (use [@userinfobot](https://t.me/userinfobot)) |
| `SHOPEE_PARTNER_ID` | Portal Shopee Afiliados → App ID da API |
| `SHOPEE_PARTNER_KEY` | Portal Shopee Afiliados → Chave secreta |

Segredos são lidos **somente** do `.env`; nunca aparecem em logs ou mensagens do bot (saídas técnicas passam por redação).

### 4. Iniciar

```powershell
python bot_factory.py
```

---

## Comandos do Telegram

| Comando | Função |
|---|---|
| `/start`, `/novo` | Menu principal com botões |
| `/global <tema>` | Documentário EN 8-12 min (1.300-1.800 palavras) |
| `/brasil <tema>` | Documentário PT 8-10 min (1.200-1.500 palavras) |
| `/shopee <busca>` | Reel vertical 9:16 com produto real da API |
| `/shopee_url <url>` | Reel a partir de URL do produto |
| `/lote_semana` | Lote semanal guiado por fases |
| `/status [job_id]` | Painel geral ou detalhe de um job |
| `/jobs` | Lista de jobs ativos (toque para abrir) |
| `/preview <job_id>` | Recebe o MP4 gerado |
| `/metadados <job_id>` | Título/descrição/tags do job |
| `/aprovar <job_id>` | Aprova roteiro OU revisão final |
| `/rejeitar <job_id> <motivo>` | Rejeita o job |
| `/regerar <job_id> <roteiro\|audio\|thumb\|video>` | Regeneração parcial |
| `/publicado <job_id> <url>` | Registra publicação manual |
| `/cancelar <job_id>` | Cancela job ativo |
| `/limpar_cache` | Limpa broll/assets (nunca toca output/jobs) |

---

## Fluxo de aprovação (obrigatório)

```
idea → researching → awaiting_script_approval → [APROVAÇÃO HUMANA DO ROTEIRO]
→ queued_render → rendering → rendered → review_required
→ [APROVAÇÃO HUMANA FINAL] → approved → scheduled → published (manual)
```

1. O worker gera briefing (`research.md`) + roteiro validado por contagem de palavras e envia o `.txt` ao admin.
2. Botões: **▶️ Renderizar** / **♻️ Regenerar roteiro** / **❌ Rejeitar**. Sem clique, nada renderiza.
3. Após render: vídeo, thumbnail e `review.md` chegam com botões **Preview / Metadados / Aprovar / Regenerar thumb / Regenerar vídeo / Rejeitar**.
4. A publicação é sempre manual; `/publicado <job> <url>` fecha o ciclo e alimenta as métricas do painel.

## Produtos Shopee: transparência

- Toda consulta salva **snapshot** (`product_snapshot.json`) com nome, preço, rating, vendas, imagens, link de afiliado, disponibilidade e **horário da consulta**.
- Produtos sem imagem, sem link ou indisponíveis são **rejeitados** antes de gerar roteiro.
- O roteiro usa somente os números retornados pela API — nada é inventado.
- ⚠️ A vinculação do produto/sacolinha no app da Shopee precisa ser conferida **manualmente** antes de postar.

## Operando um lote semanal

1. `/lote_semana` → escolha: Completo (5 Global + 5 Brasil + 10 Shopee), só Global, só Brasil, só Shopee ou Personalizado.
2. Envie temas/buscas em mensagens separadas (um por linha).
3. O lote (`LT-YYYYMMDD-XXX`) roda em fases: planejamento → pesquisa/seleção → roteiros → **aprovações individuais** → renderização em fila → revisão.
4. Nunca há disparo automático de renders: cada job pede aprovação própria.
5. Progresso: `/status` mostra fila, semáforos (1 longo simultâneo + 2 reels) e estado de cada job.

## Formatos de vídeo

| Formato | Duração | Uso |
|---|---|---|
| Longo (documentário) | 8-12 min | AdSense — monetização principal |
| Médio | 3-5 min | Engajamento/tráfego |
| Curto | 60-90s | Shorts/TikTok |
| Reel Shopee | 25-35s | Shopee Vídeo + link de afiliado |

Validação: `validate_script_length()` regenera roteiros abaixo do mínimo; FFmpeg usa a duração exata do áudio (mutagen), B-roll variado (múltiplos clipes com crossfade, sem loop único) e fades de 2s.

## Arquivos por job

```
output/jobs/<JOB-ID>_<slug>/
├── input.json               payload original
├── research.md              briefing (Global/Brasil)
├── product_snapshot.json    dados reais + horário (Shopee/Brasil)
├── script.txt               narração aprovada
├── narration.mp3            áudio TTS
├── subtitles.srt            legenda proporcional
├── video_<tipo>_<slug>_<timestamp>.mp4
├── thumbnail.jpg            1280x720
├── metadata.json            título/descrição/tags/categoria/agendamento
└── review.md                checklist humano
```

Arquivos finais **nunca** são apagados automaticamente. Logs rotativos em `logs/app.log` (5 MB × 5).

## Limitações e política

- Revisão humana obrigatória em duas etapas (roteiro e render final).
- Preços/ratings são snapshots: reconfirme antes de publicar.
- Publicação manual em YouTube e Shopee; nenhuma integração de upload automático.
- Use apenas mídia licenciada (Pexels) e respeite as políticas de afiliados da Shopee e do YouTube.

## Solução de problemas

| Problema | Solução |
|---|---|
| `ffmpeg não encontrado` | Instale e adicione ao PATH |
| Job travado em `failed` | `/regerar <id> roteiro` ou `video` |
| API Shopee 401/timeout | Confira credenciais; retry automático do lado da API |
| Espaço em disco | `/limpar_cache` (mantém output/jobs intacto) |
| Bot não responde | `TELEGRAM_BOT_TOKEN`/`TELEGRAM_ADMIN_CHAT_ID` no `.env` |
