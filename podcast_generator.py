#!/usr/bin/env python3
"""
podcast_generator.py
Goiás Econômico em 5 Minutos — Gerador autônomo para servidor (GitHub Actions)

Fluxo:
  1. Lê episódio anterior de episodios/ no próprio repo (deduplicação local, sem API)
  2. Chama Claude API com web_search para pesquisar notícias e gerar roteiro
  3. Gera MP3 via ElevenLabs + ffmpeg
  4. Salva .txt, .md e .mp3 em episodios/
     (o workflow commita os textos e publica o MP3 como GitHub Release)
  5. Envia resumo em texto + áudio para o grupo do WhatsApp via Z-API

Variáveis de ambiente obrigatórias:
  ANTHROPIC_API_KEY    — chave da API Anthropic
  ELEVENLABS_API_KEY   — chave da API ElevenLabs

Variáveis opcionais (WhatsApp):
  ZAPI_INSTANCE_ID     — ID da instância Z-API
  ZAPI_TOKEN           — Token da instância Z-API
  ZAPI_CLIENT_TOKEN    — Client-Token de segurança Z-API
  WHATSAPP_GROUP_ID    — ID do grupo (formato: XXXXXXXXXXX@g.us)
  GITHUB_REPOSITORY    — preenchido automaticamente pelo GitHub Actions
"""

import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import json
from datetime import date, timedelta
from pathlib import Path

# ── Verificação de dependências ───────────────────────────────────────────────
try:
    import anthropic
except ImportError:
    sys.exit("[ERRO] pip install anthropic")

try:
    from elevenlabs import ElevenLabs
except ImportError:
    sys.exit("[ERRO] pip install elevenlabs")


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURAÇÃO
# ══════════════════════════════════════════════════════════════════════════════

ANTHROPIC_MODEL   = "claude-sonnet-4-6"
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]

REPO_ROOT      = Path(__file__).parent
EPISODIOS_DIR  = REPO_ROOT / "episodios"

# IDs de voz ElevenLabs
VOICE_M = "pNInz6obpgDQGcFmaJgB"   # Adam  — âncora masculino
VOICE_F = "21m00Tcm4TlvDq8ikWAM"   # Rachel — repórter feminina

# Parâmetros de síntese
EL_MODEL       = "eleven_multilingual_v2"
EL_STABILITY   = 0.55
EL_SIMILARITY  = 0.80
EL_SPEED       = 0.95
VOLUME_BOOST_F = 3
PAUSA_MS       = 800
PAUSA_CURTA_MS = 300

# Vinhetas (pasta vinhetas/ no repo)
VINHETAS = [REPO_ROOT / "vinhetas" / f"Vinheta_Goias_Economico_{i}.mp3"
            for i in range(1, 5)]


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
        episodio_anterior=episodio_anterior or "(sem episódio anterior)",
    )

    messages = [{"role": "user", "content": prompt}]
    response = None

    print("[CLA] Chamando Claude API com web_search...")

    for turno in range(25):
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

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            print(f"[CLA] Concluído em {turno + 1} turno(s).")
            break

        if response.stop_reason == "tool_use":
            result_map: dict[str, list] = {}
            for block in response.content:
                if getattr(block, "type", "") == "web_search_tool_result":
                    tid = getattr(block, "tool_use_id", None)
                    if tid:
                        result_map[tid] = getattr(block, "content", [])

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

        print(f"[CLA] Stop reason inesperado: {response.stop_reason}")
        break

    if response is None:
        raise RuntimeError("Claude não retornou resposta.")

    final_text = "".join(getattr(b, "text", "") for b in response.content)

    txt_m = re.search(r"<TTS_FILE>\s*(.*?)\s*</TTS_FILE>", final_text, re.DOTALL)
    md_m  = re.search(r"<MD_FILE>\s*(.*?)\s*</MD_FILE>",  final_text, re.DOTALL)

    if not txt_m or not md_m:
        raise RuntimeError(
            "Claude não retornou no formato esperado.\n"
            f"Resposta (primeiros 3000 chars):\n{final_text[:3000]}"
        )

    return txt_m.group(1).strip(), md_m.group(1).strip()


# ══════════════════════════════════════════════════════════════════════════════
#  GERAÇÃO DE MP3
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
    if not _check_ffmpeg():
        raise RuntimeError("ffmpeg não encontrado no PATH.")

    client_el = ElevenLabs(api_key=ELEVENLABS_API_KEY)
    segs      = _parsear_roteiro(txt_content)
    falas     = [s for s in segs if s["tipo"] in ("M", "F")]
    vinheta   = _selecionar_vinheta(hoje)

    status_vin = vinheta.name if vinheta else "não encontrada — usando silêncio"
    print(f"[EL ] Vinheta: {status_vin}")
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
                label    = "M" if tipo == "M" else "F"
                trecho   = seg["conteudo"]
                trecho   = (trecho[:65] + "...") if len(trecho) > 65 else trecho
                print(f"  [{contador:02d}/{len(falas)}] {label}: {trecho}")
                try:
                    _sintetizar(client_el, seg["conteudo"], voice_id, destino)
                    if tipo == "F" and VOLUME_BOOST_F != 0:
                        _aplicar_volume(destino, VOLUME_BOOST_F)
                    partes.append(destino)
                except Exception as e:
                    print(f"    [AVISO] Fala {contador}: {e} — pulando")
                time.sleep(0.4)

        print("[EL ] Concatenando segmentos...")
        _concatenar(partes, saida)

    return _duracao_mp3(saida)


# ══════════════════════════════════════════════════════════════════════════════
#  WHATSAPP — Z-API
# ══════════════════════════════════════════════════════════════════════════════

def _md_para_whatsapp(md_content: str, hoje_str: str, duracao_s: float) -> str:
    """Gera mensagem curta de WhatsApp — apenas teaser, convida a ouvir o áudio."""
    hoje    = date.fromisoformat(hoje_str)
    hoje_br = hoje.strftime("%d/%m/%Y")
    dia_sem = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"][hoje.weekday()]
    dur_min = int(duracao_s // 60)
    dur_seg = int(duracao_s % 60)

    # Extrai títulos dos blocos de pauta (## ou ###) — máx 4
    pautas = []
    for linha in md_content.splitlines():
        if re.match(r"^#{2,3} ", linha) and not re.match(
            r"^## (Fontes|Sábado|Domingo|Segunda|Terça|Quarta|Quinta|Sexta)", linha
        ):
            titulo = re.sub(r"^#{2,3} ", "", linha).strip()
            if titulo and len(titulo) > 4:
                pautas.append(titulo)
        if len(pautas) == 4:
            break

    itens = "\n".join(f"▪️ {p}" for p in pautas)

    return (
        f"🎙️ *Goiás Econômico em 5 Minutos*\n"
        f"📅 {dia_sem}, {hoje_br}\n"
        f"\n"
        f"A edição de hoje em {dur_min}min{f' {dur_seg}s' if dur_seg else ''}:\n"
        f"\n"
        f"{itens}\n"
        f"\n"
        f"Ouça o áudio abaixo 👇"
    )


def enviar_whatsapp(texto: str, audio_url: str) -> None:
    instance_id  = os.environ["ZAPI_INSTANCE_ID"]
    token        = os.environ["ZAPI_TOKEN"]
    client_token = os.environ["ZAPI_CLIENT_TOKEN"]
    group_id     = os.environ["WHATSAPP_GROUP_ID"]

    base_url = f"https://api.z-api.io/instances/{instance_id}/token/{token}"
    headers  = {"Content-Type": "application/json", "Client-Token": client_token}

    def _post(endpoint: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            f"{base_url}/{endpoint}", data=data, headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    print("[WPP] Enviando resumo em texto...")
    r = _post("send-text", {"phone": group_id, "message": texto})
    print(f"[WPP] ✅ Texto (zaapId: {r.get('zaapId', '?')})")

    time.sleep(3)

    print("[WPP] Enviando áudio...")
    r = _post("send-audio", {"phone": group_id, "audio": audio_url, "ptt": True})
    print(f"[WPP] ✅ Áudio (zaapId: {r.get('zaapId', '?')})")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    hoje      = date.today()
    ontem     = hoje - timedelta(days=1)
    hoje_str  = hoje.isoformat()
    ontem_str = ontem.isoformat()

    nome_txt = f"texto_tts_goias_economico_5min_{hoje_str}.txt"
    nome_md  = f"goias_economico_5min_{hoje_str}.md"
    nome_mp3 = f"goias_economico_5min_{hoje_str}.mp3"

    EPISODIOS_DIR.mkdir(exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Goiás Econômico em 5 Minutos — {hoje_str}")
    print(f"{'='*60}\n")

    # ── 1. Episódio anterior (leitura local — sem API) ─────────────────────────
    prev_md = EPISODIOS_DIR / f"goias_economico_5min_{ontem_str}.md"
    if prev_md.exists():
        print(f"[DED] ✅ Episódio anterior encontrado: {prev_md.name}")
        episodio_anterior = prev_md.read_text(encoding="utf-8")
    else:
        print(f"[DED] ℹ️  Episódio anterior não encontrado ({prev_md.name}) — sem deduplicação")
        episodio_anterior = ""

    # ── 2. Gerar roteiro via Claude ────────────────────────────────────────────
    txt_content, md_content = gerar_roteiro(episodio_anterior, hoje_str)

    falas = sum(1 for l in txt_content.splitlines() if l.startswith(("M:", "F:")))
    palavras = len(" ".join(
        l[2:] for l in txt_content.splitlines() if l.startswith(("M:", "F:"))
    ).split())
    print(f"[OK ] Roteiro: {falas} falas | ~{palavras} palavras")

    # ── 3. Salvar .txt e .md no repo ──────────────────────────────────────────
    (EPISODIOS_DIR / nome_txt).write_text(txt_content, encoding="utf-8")
    (EPISODIOS_DIR / nome_md).write_text(md_content,  encoding="utf-8")
    print(f"[OK ] Textos salvos em episodios/")

    # ── 4. Gerar MP3 ──────────────────────────────────────────────────────────
    mp3_path = EPISODIOS_DIR / nome_mp3
    duracao  = gerar_mp3(txt_content, hoje, mp3_path)
    tamanho  = mp3_path.stat().st_size
    print(f"[OK ] MP3: {int(duracao // 60)}min {int(duracao % 60)}s | {tamanho // 1024} KB")

    # ── 5. WhatsApp ────────────────────────────────────────────────────────────
    # A URL do áudio é o GitHub Release criado pelo workflow após este script.
    # O workflow lê RELEASE_MP3_URL do arquivo .env gerado abaixo.
    repo    = os.environ.get("GITHUB_REPOSITORY", "mario-mendes/Goias-5-minutos")
    mp3_url = (f"https://github.com/{repo}/releases/download/"
               f"ep-{hoje_str}/{nome_mp3}")

    # Salva metadados para o workflow usar no step de WhatsApp e sumário
    meta = (EPISODIOS_DIR / f".meta_{hoje_str}.env")
    meta.write_text(
        f"RELEASE_MP3_URL={mp3_url}\n"
        f"DURACAO={int(duracao // 60)}m{int(duracao % 60)}s\n"
        f"FALAS={falas}\n"
        f"PALAVRAS={palavras}\n",
        encoding="utf-8",
    )

    print(f"\n{'='*60}")
    print(f"  GERAÇÃO CONCLUÍDA ✅ — {hoje_str}")
    print(f"  MP3        : {int(duracao // 60)}min {int(duracao % 60)}s | {tamanho // 1024} KB")
    print(f"  Falas      : {falas} | Palavras: ~{palavras}")
    print(f"  Release URL: {mp3_url}")
    print(f"{'='*60}\n")


def whatsapp_main() -> None:
    """Lê metadados do episódio de hoje e envia ao WhatsApp (usado pelo workflow após o Release)."""
    hoje_str  = date.today().isoformat()

    meta_path = EPISODIOS_DIR / f".meta_{hoje_str}.env"
    if not meta_path.exists():
        raise FileNotFoundError(f"Metadados não encontrados: {meta_path}")

    meta: dict[str, str] = {}
    for linha in meta_path.read_text(encoding="utf-8").splitlines():
        if "=" in linha:
            k, v = linha.split("=", 1)
            meta[k.strip()] = v.strip()

    audio_url = meta["RELEASE_MP3_URL"]

    dur_match = re.match(r"(\d+)m(\d+)s", meta.get("DURACAO", "0m0s"))
    duracao_s = (int(dur_match.group(1)) * 60 + int(dur_match.group(2))) if dur_match else 0.0

    md_path = EPISODIOS_DIR / f"goias_economico_5min_{hoje_str}.md"
    if not md_path.exists():
        raise FileNotFoundError(f"Markdown não encontrado: {md_path}")

    md_content = md_path.read_text(encoding="utf-8")
    texto      = _md_para_whatsapp(md_content, hoje_str, duracao_s)

    enviar_whatsapp(texto, audio_url)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--whatsapp":
        whatsapp_main()
    else:
        main()
