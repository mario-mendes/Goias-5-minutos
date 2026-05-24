#!/usr/bin/env python3
"""
podcast_generator.py
Goiás Econômico em 5 Minutos — Gerador autônomo para servidor (GitHub Actions)

Fluxo:
  1. Baixa episódio anterior do Google Drive (deduplicação)
  2. Chama Claude API com web_search para pesquisar notícias e gerar roteiro
  3. Gera MP3 via ElevenLabs + ffmpeg
  4. Sobe os 3 arquivos (.txt, .md, .mp3) para o Google Drive

Variáveis de ambiente obrigatórias:
  ANTHROPIC_API_KEY          — chave da API Anthropic
  ELEVENLABS_API_KEY         — chave da API ElevenLabs
  GOOGLE_SERVICE_ACCOUNT_JSON — conteúdo do JSON da conta de serviço GCP
  GOOGLE_DRIVE_FOLDER_ID     — ID da pasta no Google Drive
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

# ── Verificação antecipada de dependências ────────────────────────────────────
try:
    import anthropic
except ImportError:
    sys.exit("[ERRO] pip install anthropic")

try:
    from elevenlabs import ElevenLabs
except ImportError:
    sys.exit("[ERRO] pip install elevenlabs")

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
except ImportError:
    sys.exit("[ERRO] pip install google-auth google-api-python-client")


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURAÇÃO
# ══════════════════════════════════════════════════════════════════════════════

ANTHROPIC_MODEL     = "claude-sonnet-4-6"
DRIVE_FOLDER_ID     = os.environ["GOOGLE_DRIVE_FOLDER_ID"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
ELEVENLABS_API_KEY  = os.environ["ELEVENLABS_API_KEY"]

# IDs de voz ElevenLabs (mesmos do gerador_mp3_goias.py)
VOICE_M = "pNInz6obpgDQGcFmaJgB"   # Adam  — âncora masculino
VOICE_F = "21m00Tcm4TlvDq8ikWAM"   # Rachel — repórter feminina

# Parâmetros de síntese
EL_MODEL       = "eleven_multilingual_v2"
EL_STABILITY   = 0.55
EL_SIMILARITY  = 0.80
EL_SPEED       = 0.95
VOLUME_BOOST_F = 3      # dB extra na voz feminina
PAUSA_MS       = 800
PAUSA_CURTA_MS = 300

# Vinhetas — repo deve ter pasta vinhetas/ com os arquivos MP3
REPO_ROOT = Path(__file__).parent
VINHETAS  = [REPO_ROOT / "vinhetas" / f"Vinheta_Goias_Economico_{i}.mp3"
             for i in range(1, 5)]


# ══════════════════════════════════════════════════════════════════════════════
#  GOOGLE DRIVE
# ══════════════════════════════════════════════════════════════════════════════

def get_drive_service():
    info  = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def drive_find(svc, folder_id: str, name: str) -> dict | None:
    q = f"name='{name}' and '{folder_id}' in parents and trashed=false"
    try:
        r = svc.files().list(q=q, fields="files(id,name)", pageSize=5).execute()
        files = r.get("files", [])
        return files[0] if files else None
    except HttpError as e:
        if e.resp.status == 404:
            sys.exit(
                f"\n[ERRO] Pasta do Google Drive não encontrada (HTTP 404).\n"
                f"  Folder ID configurado: {folder_id}\n\n"
                f"  Causas prováveis:\n"
                f"  1. A pasta NÃO foi compartilhada com a conta de serviço.\n"
                f"     → Abra o Drive, clique com botão direito na pasta,\n"
                f"       'Compartilhar' e adicione o e-mail da conta de serviço\n"
                f"       (termina em @...iam.gserviceaccount.com) como Editor.\n"
                f"  2. O GOOGLE_DRIVE_FOLDER_ID está errado.\n"
                f"     → Confira o ID na URL da pasta: drive.google.com/drive/folders/<ID>\n"
            )
        raise


def drive_download_text(svc, file_id: str) -> str:
    req = svc.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl  = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue().decode("utf-8")


def drive_upload(svc, folder_id: str, name: str, data: bytes, mime: str) -> str:
    media    = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=True)
    existing = drive_find(svc, folder_id, name)
    if existing:
        f = svc.files().update(
            fileId=existing["id"], media_body=media
        ).execute()
    else:
        meta = {"name": name, "parents": [folder_id]}
        f = svc.files().create(
            body=meta, media_body=media, fields="id"
        ).execute()
    return f["id"]


# ══════════════════════════════════════════════════════════════════════════════
#  PROMPT PARA O CLAUDE
# ══════════════════════════════════════════════════════════════════════════════

PROMPT_TEMPLATE = """\
Você é produtor do podcast diário "Goiás Econômico em 5 Minutos" — análise técnica \
de política e economia do estado de Goiás, com ênfase de quem trabalha com orçamento \
público (LOA, LDO, LRF, ICMS, renúncia de receita, resultado primário, empenho, \
investimentos, folha de pessoal, agro, CPIs etc.).

HOJE: {hoje_br}
DATA ISO: {hoje}

══════════════════════════════════════════
EPISÓDIO ANTERIOR (para DEDUPLICAÇÃO):
══════════════════════════════════════════
{episodio_anterior}

INSTRUÇÃO DE DEDUPLICAÇÃO:
Extraia mentalmente quais temas foram cobertos ontem. Qualquer notícia REPETIDA deve \
ser DESCARTADA. Se ainda relevante, trate como desdobramento explícito \
("Em desdobramento ao que noticiamos ontem...").

══════════════════════════════════════════
PASSO 1 — PESQUISAR NOTÍCIAS DAS ÚLTIMAS 24H
══════════════════════════════════════════
Use a ferramenta web_search para realizar AO MENOS 5 buscas com queries como:
- "Goiás economia fiscal {hoje_br} 2026"
- "Goiás ICMS arrecadação {hoje_br} 2026"
- "Assembleia Legislativa Goiás sessão votação {hoje_br} 2026"
- "Goiás investimento obra decreto {hoje_br} 2026"
- "Goiás agronegócio exportação {hoje_br} 2026"
- "Secretaria Economia Goiás nota decreto {hoje_br} 2026"
- "Goiás orçamento LOA empenho resultado {hoje_br} 2026"

Fontes prioritárias: O Popular, Jornal Opção, Portal da Alego \
(portal.al.go.leg.br), Portal 6, Mais Goiás, Goinfra, STG News, \
Secretaria da Economia (goias.gov.br/economia), Agência Cora Coralina, \
G1 Goiás, Diário de Aparecida.

FILTRO DE FRESCOR: só use notícias das últimas 24h que NÃO estejam nos temas \
cobertos ontem. Se sobrar menos de 3 pautas frescas, busque impactos federais em \
Goiás (transferências, FUNDEB, câmbio, commodities, emendas).

══════════════════════════════════════════
PASSO 2 — GERAR OS DOIS ARQUIVOS
══════════════════════════════════════════

Após pesquisar, gere EXATAMENTE dois blocos de conteúdo nas tags abaixo.

─── ARQUIVO TTS (.txt) ───────────────────
Roteiro com marcadores para síntese de voz. Regras obrigatórias:

• PRIMEIRA fala DEVE ser:
    M: Bom dia, Economia! Aqui é o Goiás Econômico em cinco minutos.
• ÚLTIMAS falas DEVEM ser:
    M: Este foi o Goiás Econômico em cinco minutos.
    F: Um abraço a todos os colegas da Secretaria da Economia de Goiás. Boa semana de trabalho!
    [PAUSA CURTA]
    [VINHETA_OUT]
• Números SEMPRE por extenso (ex: "cinquenta e três bilhões de reais")
• Alternar M: e F: naturalmente — nunca duas falas da mesma voz sem necessidade
• Termos fiscais com *asteriscos* (ex: *resultado primário*, *ICMS*, *empenho*)
• Duração alvo: 650-780 palavras faladas (M+F juntos)

Marcadores TTS disponíveis:
  [VINHETA_IN]   → acorde de abertura
  [VINHETA_OUT]  → acorde de fechamento
  [PAUSA]        → silêncio 800ms
  [PAUSA CURTA]  → silêncio 300ms
  M: texto       → voz masculina âncora
  F: texto       → voz feminina repórter
  *palavra*      → ênfase

─── ARQUIVO MARKDOWN (.md) ──────────────
Versão legível com: título, data, subtítulos por bloco, tabelas onde couber, \
e seção "Fontes" com links reais.

══════════════════════════════════════════
FORMATO DE SAÍDA — OBRIGATÓRIO
══════════════════════════════════════════

Retorne EXATAMENTE neste formato (sem texto fora das tags):

<TTS_FILE>
[conteúdo completo do .txt com todos os marcadores TTS]
</TTS_FILE>

<MD_FILE>
[conteúdo completo do .md em markdown]
</MD_FILE>
"""


# ══════════════════════════════════════════════════════════════════════════════
#  GERAÇÃO DO ROTEIRO VIA CLAUDE API
# ══════════════════════════════════════════════════════════════════════════════

def gerar_roteiro(episodio_anterior: str, hoje: str) -> tuple[str, str]:
    """Chama Claude com web_search e retorna (txt_content, md_content)."""
    hoje_br = date.fromisoformat(hoje).strftime("%d/%m/%Y")
    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = PROMPT_TEMPLATE.format(
        hoje=hoje,
        hoje_br=hoje_br,
        episodio_anterior=episodio_anterior or "(sem episódio anterior — primeiro episódio)",
    )

    messages = [{"role": "user", "content": prompt}]
    response = None

    print("[CLA] Chamando Claude API com web_search...")

    for turno in range(25):  # limite de segurança
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=8000,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 12,
            }],
            messages=messages,
        )

        # Adiciona a resposta do assistente ao histórico
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            print(f"[CLA] Concluído em {turno + 1} turno(s).")
            break

        if response.stop_reason == "tool_use":
            # Mapeia tool_use_id → resultados (vindos dos blocos web_search_tool_result)
            result_map: dict[str, list] = {}
            for block in response.content:
                btype = getattr(block, "type", "")
                if btype == "web_search_tool_result":
                    tid = getattr(block, "tool_use_id", None)
                    if tid:
                        result_map[tid] = getattr(block, "content", [])

            # Monta tool_result para cada tool_use
            tool_results = []
            for block in response.content:
                if getattr(block, "type", "") == "tool_use":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_map.get(block.id, []),
                    })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            continue

        # Qualquer outro stop_reason (ex: max_tokens) — sai do loop
        print(f"[CLA] Stop reason inesperado: {response.stop_reason}")
        break

    if response is None:
        raise RuntimeError("Claude não retornou resposta.")

    # Extrai texto final do assistente
    final_text = "".join(
        getattr(b, "text", "") for b in response.content
    )

    # Parse das tags de saída
    txt_m = re.search(r"<TTS_FILE>\s*(.*?)\s*</TTS_FILE>", final_text, re.DOTALL)
    md_m  = re.search(r"<MD_FILE>\s*(.*?)\s*</MD_FILE>",  final_text, re.DOTALL)

    if not txt_m or not md_m:
        raise RuntimeError(
            "Claude não retornou no formato esperado (<TTS_FILE> / <MD_FILE>).\n"
            f"Resposta (primeiros 3000 chars):\n{final_text[:3000]}"
        )

    return txt_m.group(1).strip(), md_m.group(1).strip()


# ══════════════════════════════════════════════════════════════════════════════
#  GERAÇÃO DE MP3 (lógica de gerador_mp3_goias.py integrada)
# ══════════════════════════════════════════════════════════════════════════════

def _check_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _limpar_marcadores(texto: str) -> str:
    return re.sub(r"\*([^*]+)\*", r"\1", texto).strip()


def _sintetizar(client_el: ElevenLabs, texto: str, voice_id: str, destino: Path) -> bool:
    texto = _limpar_marcadores(texto)
    if not texto:
        return False
    resposta = client_el.text_to_speech.convert(
        voice_id=voice_id,
        text=texto,
        model_id=EL_MODEL,
        voice_settings={
            "stability":        EL_STABILITY,
            "similarity_boost": EL_SIMILARITY,
            "speed":            EL_SPEED,
        },
        output_format="mp3_44100_128",
    )
    audio = resposta if isinstance(resposta, (bytes, bytearray)) else b"".join(resposta)
    destino.write_bytes(audio)
    return True


def _gerar_silencio(ms: int, destino: Path) -> None:
    # Gera silêncio diretamente em MP3 (libmp3lame) — evita conflito de codec/extensão
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", str(ms / 1000),
        "-acodec", "libmp3lame", "-b:a", "128k",
        str(destino),
    ], capture_output=True, check=True)


def _aplicar_volume(arquivo: Path, db: float) -> None:
    if db == 0:
        return
    tmp = arquivo.with_suffix(".vol_tmp.mp3")
    subprocess.run([
        "ffmpeg", "-y", "-i", str(arquivo),
        "-filter:a", f"volume={db}dB",
        "-acodec", "libmp3lame", "-b:a", "128k", str(tmp),
    ], capture_output=True, check=True)
    tmp.replace(arquivo)


def _concatenar(partes: list[Path], saida: Path) -> None:
    n      = len(partes)
    inputs = [arg for p in partes for arg in ("-i", str(p))]
    fc     = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
    subprocess.run(
        ["ffmpeg", "-y"] + inputs + [
            "-filter_complex", fc, "-map", "[out]",
            "-acodec", "libmp3lame", "-b:a", "128k",
            "-id3v2_version", "3", str(saida),
        ],
        capture_output=True, check=True,
    )


def _parsear_roteiro(txt: str) -> list[dict]:
    segs = []
    for linha in txt.splitlines():
        linha = linha.rstrip()
        if not linha:
            continue
        if linha.startswith("M:"):
            segs.append({"tipo": "M", "conteudo": linha[2:].strip()})
        elif linha.startswith("F:"):
            segs.append({"tipo": "F", "conteudo": linha[2:].strip()})
        elif linha == "[PAUSA]":
            segs.append({"tipo": "PAUSA", "conteudo": ""})
        elif linha == "[PAUSA CURTA]":
            segs.append({"tipo": "PAUSA_CURTA", "conteudo": ""})
        elif "[VINHETA" in linha:
            segs.append({"tipo": "VINHETA", "conteudo": linha})
    return segs


def _selecionar_vinheta(hoje: date) -> Path | None:
    idx = hoje.toordinal() % len(VINHETAS)
    v   = VINHETAS[idx]
    return v if v.exists() else None


def _duracao_mp3(caminho: Path) -> float:
    try:
        r = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(caminho),
        ], capture_output=True, text=True, check=True)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def gerar_mp3(txt_content: str, hoje: date, saida: Path) -> float:
    """Gera o MP3 a partir do roteiro TTS. Retorna duração em segundos."""
    if not _check_ffmpeg():
        raise RuntimeError("ffmpeg não encontrado no PATH.")

    client_el = ElevenLabs(api_key=ELEVENLABS_API_KEY)
    segs      = _parsear_roteiro(txt_content)
    falas     = [s for s in segs if s["tipo"] in ("M", "F")]
    vinheta   = _selecionar_vinheta(hoje)

    if vinheta:
        print(f"[EL ] Vinheta: {vinheta.name}")
    else:
        print("[EL ] Vinheta não encontrada — usando silêncio de 1s")

    print(f"[EL ] {len(falas)} falas — iniciando síntese...")

    partes:   list[Path] = []
    contador: int        = 0

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)

        for i, seg in enumerate(segs):
            destino = tmp / f"{i:04d}.mp3"
            tipo    = seg["tipo"]

            if tipo == "PAUSA":
                _gerar_silencio(PAUSA_MS, destino)
                partes.append(destino)

            elif tipo == "PAUSA_CURTA":
                _gerar_silencio(PAUSA_CURTA_MS, destino)
                partes.append(destino)

            elif tipo == "VINHETA":
                if vinheta:
                    subprocess.run([
                        "ffmpeg", "-y", "-i", str(vinheta),
                        "-ar", "44100", "-ac", "2",
                        "-acodec", "libmp3lame", "-b:a", "128k", str(destino),
                    ], capture_output=True, check=True)
                else:
                    _gerar_silencio(1000, destino)
                partes.append(destino)

            elif tipo in ("M", "F"):
                contador += 1
                voice_id = VOICE_M if tipo == "M" else VOICE_F
                label    = "M (âncora)" if tipo == "M" else "F (repórt)"
                trecho   = seg["conteudo"]
                trecho   = (trecho[:65] + "...") if len(trecho) > 65 else trecho
                print(f"  [{contador:02d}/{len(falas)}] {label}: {trecho}")

                try:
                    _sintetizar(client_el, seg["conteudo"], voice_id, destino)
                    if tipo == "F" and VOLUME_BOOST_F != 0:
                        _aplicar_volume(destino, VOLUME_BOOST_F)
                    partes.append(destino)
                except Exception as e:
                    print(f"    [AVISO] Fala {contador} falhou: {e} — pulando")

                time.sleep(0.4)  # rate limit

        print("[EL ] Concatenando segmentos...")
        _concatenar(partes, saida)

    return _duracao_mp3(saida)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    hoje  = date.today()
    ontem = hoje - timedelta(days=1)

    hoje_str  = hoje.isoformat()
    ontem_str = ontem.isoformat()

    nome_txt = f"texto_tts_goias_economico_5min_{hoje_str}.txt"
    nome_md  = f"goias_economico_5min_{hoje_str}.md"
    nome_mp3 = f"goias_economico_5min_{hoje_str}.mp3"

    print(f"\n{'='*60}")
    print(f"  Goiás Econômico em 5 Minutos — {hoje_str}")
    print(f"{'='*60}\n")

    # ── 1. Google Drive ────────────────────────────────────────────────────────
    print("[DRV] Conectando ao Google Drive...")
    drive = get_drive_service()

    # ── 2. Episódio anterior (deduplicação) ───────────────────────────────────
    prev_name = f"goias_economico_5min_{ontem_str}.md"
    prev_file = drive_find(drive, DRIVE_FOLDER_ID, prev_name)

    if prev_file:
        print(f"[DRV] Episódio anterior encontrado: {prev_name}")
        episodio_anterior = drive_download_text(drive, prev_file["id"])
    else:
        print(f"[DRV] Episódio anterior não encontrado ({prev_name}) — sem deduplicação")
        episodio_anterior = ""

    # ── 3. Gerar roteiro via Claude ────────────────────────────────────────────
    txt_content, md_content = gerar_roteiro(episodio_anterior, hoje_str)

    falas_count = sum(
        1 for linha in txt_content.splitlines()
        if linha.startswith(("M:", "F:"))
    )
    palavras = len(" ".join(
        linha[2:] for linha in txt_content.splitlines()
        if linha.startswith(("M:", "F:"))
    ).split())
    print(f"[OK ] Roteiro: {falas_count} falas | ~{palavras} palavras faladas")

    # ── 4. Gerar MP3 ──────────────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as work_dir:
        mp3_path = Path(work_dir) / nome_mp3
        duracao  = gerar_mp3(txt_content, hoje, mp3_path)
        tamanho  = mp3_path.stat().st_size
        print(f"[OK ] MP3: {int(duracao // 60)}min {int(duracao % 60)}s | {tamanho // 1024} KB")

        # ── 5. Upload para Google Drive ────────────────────────────────────────
        print("\n[DRV] Enviando arquivos para o Google Drive...")

        drive_upload(drive, DRIVE_FOLDER_ID, nome_txt,
                     txt_content.encode("utf-8"), "text/plain")
        print(f"[DRV] ✅ {nome_txt}")

        drive_upload(drive, DRIVE_FOLDER_ID, nome_md,
                     md_content.encode("utf-8"), "text/markdown")
        print(f"[DRV] ✅ {nome_md}")

        drive_upload(drive, DRIVE_FOLDER_ID, nome_mp3,
                     mp3_path.read_bytes(), "audio/mpeg")
        print(f"[DRV] ✅ {nome_mp3}")

    print(f"\n{'='*60}")
    print(f"  CONCLUÍDO ✅ — {hoje_str}")
    print(f"  Duração MP3 : {int(duracao // 60)}min {int(duracao % 60)}s")
    print(f"  Falas       : {falas_count}  |  Palavras: ~{palavras}")
    print(f"  Drive folder: {DRIVE_FOLDER_ID}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
