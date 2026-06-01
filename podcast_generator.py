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

import html.parser
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import json
import xml.etree.ElementTree as ET
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

ANTHROPIC_MODEL    = "claude-sonnet-4-6"
# Chaves lidas sob demanda dentro de cada função (evita falha em modos que não precisam delas)
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")

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
#  O POPULAR — CAPTURA VIA RSS + PRÉ-PAYWALL
# ══════════════════════════════════════════════════════════════════════════════

# Feeds RSS d'O Popular (tentados em ordem; falhas são ignoradas silenciosamente)
RSS_OPOPULAR = [
    "https://www.opopular.com.br/rss",
    "https://www.opopular.com.br/economia/rss",
    "https://www.opopular.com.br/politica/rss",
    "https://www.opopular.com.br/cidades/rss",
]

# Quantos caracteres do artigo tentar extrair antes do paywall
PAYWALL_TRECHO_CHARS = 300


class _TextExtractor(html.parser.HTMLParser):
    """HTMLParser minimalista: extrai texto visível, pula scripts/estilos."""
    _SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer",
                  "aside", "iframe", "form"}
    _BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "figcaption"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            chunk = data.strip()
            if chunk:
                self._parts.append(chunk)

    def get_text(self) -> str:
        return re.sub(r" {2,}", " ", " ".join(self._parts)).strip()


def _get(url: str, timeout: int = 10) -> str:
    """HTTP GET simples; retorna '' em qualquer erro."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PodcastBot/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except Exception:
        return ""


def _pre_paywall(url: str) -> str:
    """
    Baixa a página e extrai os primeiros PAYWALL_TRECHO_CHARS de texto visível.
    Heurística: pega o texto até encontrar palavras típicas de paywall.
    """
    raw = _get(url, timeout=8)
    if not raw:
        return ""
    ext = _TextExtractor()
    ext.feed(raw)
    texto = ext.get_text()
    # Corta no primeiro sinal de paywall
    paywall_re = re.compile(
        r"(assine|assinar|seja assinante|conteúdo exclusivo|faça login|"
        r"acesso restrito|para continuar lendo|leia mais com)",
        re.IGNORECASE,
    )
    m = paywall_re.search(texto)
    corte = m.start() if m else len(texto)
    return texto[:min(corte, PAYWALL_TRECHO_CHARS)].strip()


def buscar_opopular(max_artigos: int = 10) -> str:
    """
    Busca manchetes recentes d'O Popular via RSS e tenta extrair
    o início de cada artigo antes do paywall.
    Retorna bloco de texto formatado para injeção no prompt do Claude.
    """
    vistos: set[str] = set()
    artigos: list[dict] = []

    for rss_url in RSS_OPOPULAR:
        xml_raw = _get(rss_url, timeout=12)
        if not xml_raw:
            continue
        try:
            root = ET.fromstring(xml_raw)
        except ET.ParseError:
            continue
        for item in root.iter("item"):
            titulo = (item.findtext("title") or "").strip()
            link   = (item.findtext("link")  or "").strip()
            desc   = (item.findtext("description") or "").strip()
            desc   = re.sub(r"<[^>]+>", " ", desc)
            desc   = re.sub(r"\s+", " ", desc).strip()
            if not titulo or link in vistos:
                continue
            vistos.add(link)
            artigos.append({"titulo": titulo, "link": link, "desc": desc})
            if len(artigos) >= max_artigos:
                break
        if len(artigos) >= max_artigos:
            break

    if not artigos:
        print("[OPO] RSS indisponível — O Popular será buscado via web_search.")
        return "(RSS d'O Popular indisponível — use web_search para buscar notícias recentes do site.)"

    print(f"[OPO] {len(artigos)} manchetes obtidas via RSS.")

    linhas: list[str] = ["### Manchetes recentes — O Popular (via RSS)\n"]
    for a in artigos:
        linhas.append(f"**{a['titulo']}**")
        trecho = _pre_paywall(a["link"]) if a["link"] else ""
        if trecho and len(trecho) > len(a["desc"]) + 60:
            linhas.append(trecho)
        elif a["desc"]:
            linhas.append(a["desc"][:500])
        if a["link"]:
            linhas.append(f"Fonte: {a['link']}")
        linhas.append("")

    return "\n".join(linhas)


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
MANCHETES D'O POPULAR — FONTE PRIORITÁRIA
══════════════════════════════════════════
O Popular é o maior jornal de Goiás. As manchetes abaixo foram coletadas via RSS \
e devem ser tratadas como fonte primária. Regras:

1. Para cada manchete relevante (economia, fiscal, política, agro), USE DIRETAMENTE \
   o conteúdo já fornecido — NÃO faça web_search adicional para essas pautas.
2. Só use web_search para temas importantes NÃO cobertos pelo RSS do O Popular.
3. Máximo {max_buscas} buscas web no total — use-as com critério.

{manchetes_opopular}

══════════════════════════════════════════
PASSO 1 — COMPLEMENTAR COM WEB_SEARCH (somente se necessário)
══════════════════════════════════════════
Se após analisar o RSS do O Popular ainda faltarem pautas para completar o episódio, \
use web_search com foco em:
- "Assembleia Legislativa Goiás sessão {hoje_br} 2026"
- "Secretaria Economia Goiás decreto {hoje_br} 2026"
- Impactos federais em Goiás (FPE, FUNDEB, câmbio, commodities, emendas)

Fontes aceitas: Jornal Opção, Portal da Alego, Portal 6, Mais Goiás, \
Goinfra, STG News, goias.gov.br/economia, Agência Cora Coralina, G1 Goiás.

FILTRO DE FRESCOR: só use notícias das últimas 24h que NÃO estejam nos temas \
cobertos ontem.

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
• Números: use escala abreviada legível para TTS (ex: "3,7 bilhões de reais", \
"450 milhões", "12,5 mil vagas", "1,2 trilhão"). NUNCA escreva dígitos com \
pontos de milhar (ex: PROIBIDO "1.234.567.890"). Percentuais podem ficar \
como "3,5%" ou "três vírgula cinco por cento".
• Alternar M: e F: naturalmente — nunca duas falas da mesma voz sem necessidade
• Termos fiscais com *asteriscos* (ex: *resultado primário*, *ICMS*, *empenho*)
• ⚠️ LIMITE DE PALAVRAS — REGRA MAIS IMPORTANTE:
  Conte as palavras de CADA fala M: e F: enquanto escreve.
  PARE quando o total acumulado atingir 720 palavras.
  O programa dura EXATAMENTE 5 minutos — 720 palavras ≈ 5min no TTS.
  Acima de 780 palavras o episódio ultrapassa 5 minutos e será REJEITADO.
  Abaixo de 620 palavras também é inaceitável.
  NUNCA use "blocos" numerados nem sub-seções — cada pauta tem no máximo \
  2 falas (uma M e uma F) antes de ir para o próximo tema.

Marcadores TTS disponíveis:
  [VINHETA_IN]   → acorde de abertura
  [VINHETA_OUT]  → acorde de fechamento
  [PAUSA]        → silêncio 800ms
  [PAUSA CURTA]  → silêncio 300ms
  M: texto       → voz masculina âncora
  F: texto       → voz feminina repórter
  *palavra*      → ênfase

─── ARQUIVO MARKDOWN (.md) ──────────────
Versão legível com: título (# nível 1), data, e UMA seção ## por pauta \
(ex: "## ICMS bate recorde em abril"). Use no máximo nível ##, \
sem ### ou ####. Tabelas onde couber. Seção "## Fontes" com links reais ao final.

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

def gerar_roteiro(episodio_anterior: str, hoje: str,
                  manchetes_opopular: str = "") -> tuple[str, str]:
    """Chama Claude com web_search e retorna (txt_content, md_content)."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("Variável de ambiente ANTHROPIC_API_KEY não definida.")
    hoje_br = date.fromisoformat(hoje).strftime("%d/%m/%Y")
    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = PROMPT_TEMPLATE.format(
        hoje=hoje,
        hoje_br=hoje_br,
        episodio_anterior=episodio_anterior or "(sem episódio anterior)",
        manchetes_opopular=manchetes_opopular or "(não disponível — use web_search)",
        max_buscas=4,
    )

    messages = [{"role": "user", "content": prompt}]
    response = None

    print("[CLA] Chamando Claude API com web_search...")

    for turno in range(25):
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=5000,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 4,
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

def _normalizar_numeros_tts(texto: str) -> str:
    """
    Converte números grandes para escala legível antes da síntese de voz.

    Exemplos:
      1.234.567.890   → "1,2 bilhão"
      R$ 3.700.000    → "R$ 3,7 milhões"
      450.000.000     → "450 milhões"
      12.500          → "12,5 mil"
      3,7%            → inalterado (percentuais não são convertidos)
      2026            → inalterado (anos de 4 dígitos não são convertidos)
    """

    def _dec(v: float) -> str:
        """Formata com uma casa decimal apenas se necessário."""
        arred = round(v, 1)
        if arred == int(arred):
            return str(int(arred))
        return f"{arred:.1f}".replace(".", ",")

    def _escala(valor: float) -> str | None:
        if valor >= 1_000_000_000_000:
            v = valor / 1_000_000_000_000
            nome = "trilhão" if round(v, 1) < 2 else "trilhões"
            return f"{_dec(v)} {nome}"
        if valor >= 1_000_000_000:
            v = valor / 1_000_000_000
            nome = "bilhão" if round(v, 1) < 2 else "bilhões"
            return f"{_dec(v)} {nome}"
        if valor >= 1_000_000:
            v = valor / 1_000_000
            nome = "milhão" if round(v, 1) < 2 else "milhões"
            return f"{_dec(v)} {nome}"
        if valor >= 10_000:
            return f"{_dec(valor / 1_000)} mil"
        return None  # número pequeno — não converter

    def _substituir(m: re.Match) -> str:
        pref    = (m.group("pref") or "").strip()   # "R$" se presente
        num_str = m.group("num")
        suf     = (m.group("suf")  or "").strip()   # "%" se presente

        # Percentuais: não alterar
        if suf == "%":
            return m.group(0)

        # Normaliza para float (formato BR: ponto=milhar, vírgula=decimal)
        limpo = num_str.replace(".", "")
        # Vírgula como separador decimal (ex: 1.234,56)
        if "," in limpo:
            partes = limpo.split(",")
            if len(partes) == 2 and len(partes[1]) <= 2:
                limpo = limpo.replace(",", ".")
            else:
                limpo = limpo.replace(",", "")

        try:
            valor = float(limpo)
        except ValueError:
            return m.group(0)

        resultado = _escala(valor)
        if resultado is None:
            return m.group(0)

        if pref in ("R$", "R $"):
            return f"R$ {resultado}"
        return resultado

    # Captura números com separadores de milhar no padrão brasileiro
    # Ex: 1.234.567  |  R$ 1.234.567,89  |  1234567 (sem pontos, 7+ dígitos)
    pattern = re.compile(
        r"(?P<pref>R\s*\$\s*)?"
        r"(?P<num>"
        r"\d{1,3}(?:\.\d{3})+"           # formato BR com pontos: 1.234.567
        r"(?:,\d{1,2})?"                  # centavos opcionais
        r"|\d{7,}"                        # número bruto sem pontos com 7+ dígitos
        r")"
        r"(?P<suf>\s*%)?",
        re.IGNORECASE,
    )
    return pattern.sub(_substituir, texto)


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
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("Variável de ambiente ELEVENLABS_API_KEY não definida.")
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
                conteudo_norm = _normalizar_numeros_tts(seg["conteudo"])
                ok = False
                for tentativa in range(1, 4):  # até 3 tentativas
                    try:
                        _sintetizar(client_el, conteudo_norm, voice_id, destino)
                        if tipo == "F" and VOLUME_BOOST_F != 0:
                            _aplicar_volume(destino, VOLUME_BOOST_F)
                        partes.append(destino)
                        ok = True
                        break
                    except Exception as e:
                        print(f"    [AVISO] Fala {contador} tentativa {tentativa}/3: {e}")
                        if tentativa < 3:
                            time.sleep(3 * tentativa)  # backoff: 3s, 6s
                if not ok:
                    print(f"    [ERRO] Fala {contador} falhou após 3 tentativas — pulando")
                time.sleep(0.8)  # intervalo entre falas (era 0.4s)

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

    # Extrai apenas headers ## de nível 2 (pautas principais, não sub-seções)
    # Ignora cabeçalhos de metadados (edição, data, fontes, dias da semana)
    _ignorar = re.compile(
        r"(Edição|Fontes|Sábado|Domingo|Segunda|Terça|Quarta|Quinta|Sexta"
        r"|nº\s*\d|^\d{1,2}\s*/)", re.IGNORECASE
    )
    pautas = []
    for linha in md_content.splitlines():
        if not re.match(r"^## ", linha):   # só ## exato, ignora ### e ####
            continue
        titulo = re.sub(r"^## ", "", linha).strip()
        if _ignorar.search(titulo):
            continue
        # Remove prefixos de bloco: "🔵 Bloco 1 — ", "1. ", emojis iniciais
        titulo = re.sub(r"^[🔵🟢🔴⚫⚪🟡🟠🟣🎙️\s]*Bloco\s*\d+\s*[–—-]\s*", "", titulo)
        titulo = re.sub(r"^\d+\.\s*", "", titulo).strip()
        if titulo and len(titulo) > 6:
            pautas.append(titulo)
        if len(pautas) == 4:
            break

    itens = "\n".join(f"▪️ {p}" for p in pautas) if pautas else "▪️ Confira no áudio"

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
    """
    Dispara o webhook do Make.com, que por sua vez chama o Whapi.cloud.
    O Make.com usa IPs não bloqueados pelo Cloudflare.
    """
    webhook_url = os.environ["MAKE_WEBHOOK_URL"]

    payload = json.dumps({"texto": texto, "audio_url": audio_url}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            corpo = resp.read().decode("utf-8", errors="replace")
            print(f"[MAKE] Webhook disparado com sucesso: {resp.status} — {corpo[:200]}")
    except urllib.error.HTTPError as e:
        corpo = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {e.reason} — {corpo}") from e


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
        texto_anterior = prev_md.read_text(encoding="utf-8")
        # Limita a 1500 chars para não estourar o rate limit de tokens
        episodio_anterior = texto_anterior[:1500] + ("..." if len(texto_anterior) > 1500 else "")
    else:
        print(f"[DED] ℹ️  Episódio anterior não encontrado ({prev_md.name}) — sem deduplicação")
        episodio_anterior = ""

    # ── 2. Buscar manchetes d'O Popular ───────────────────────────────────────
    manchetes_opopular = buscar_opopular(max_artigos=3)

    # ── 3. Gerar roteiro via Claude (com re-tentativa se muito longo) ──────────
    for tentativa_roteiro in range(1, 3):
        txt_content, md_content = gerar_roteiro(episodio_anterior, hoje_str,
                                                manchetes_opopular)
        _palavras_check = len(" ".join(
            l[2:] for l in txt_content.splitlines() if l.startswith(("M:", "F:"))
        ).split())
        if _palavras_check <= 800:
            break
        print(f"[AVISO] Roteiro longo ({_palavras_check} palavras) na tentativa "
              f"{tentativa_roteiro}/2 — aguardando 120s para evitar rate limit...")
        time.sleep(120)
        print("[CLA] Regenerando com instrução reforçada...")
        # Injeta aviso no contexto para a próxima tentativa
        manchetes_opopular = (
            f"⚠️ ATENÇÃO: sua tentativa anterior teve {_palavras_check} palavras "
            f"(máximo 780). Gere um roteiro MAIS CURTO desta vez. "
            f"Corte detalhes, mantenha só os fatos essenciais.\n\n"
            + manchetes_opopular
        )

    falas = sum(1 for l in txt_content.splitlines() if l.startswith(("M:", "F:")))
    palavras = len(" ".join(
        l[2:] for l in txt_content.splitlines() if l.startswith(("M:", "F:"))
    ).split())
    print(f"[OK ] Roteiro: {falas} falas | ~{palavras} palavras")
    if palavras < 620:
        print(f"[AVISO] ⚠️  Roteiro curto ({palavras} palavras). "
              "Pode indicar falta de pauta ou Claude ignorou o limite mínimo.")
    elif palavras > 800:
        print(f"[AVISO] ⚠️  Roteiro longo ({palavras} palavras > 800). "
              "O episódio vai ultrapassar 5 minutos.")

    # ── 4. Salvar .txt e .md no repo ──────────────────────────────────────────
    (EPISODIOS_DIR / nome_txt).write_text(txt_content, encoding="utf-8")
    (EPISODIOS_DIR / nome_md).write_text(md_content,  encoding="utf-8")
    print(f"[OK ] Textos salvos em episodios/")

    # ── 5. Gerar MP3 ──────────────────────────────────────────────────────────
    mp3_path = EPISODIOS_DIR / nome_mp3
    duracao  = gerar_mp3(txt_content, hoje, mp3_path)
    tamanho  = mp3_path.stat().st_size
    print(f"[OK ] MP3: {int(duracao // 60)}min {int(duracao % 60)}s | {tamanho // 1024} KB")

    # ── 6. WhatsApp ────────────────────────────────────────────────────────────
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
    nome_mp3  = f"goias_economico_5min_{hoje_str}.mp3"
    repo      = os.environ.get("GITHUB_REPOSITORY", "mario-mendes/Goias-5-minutos")

    # Tenta ler o arquivo de metadados; se não existir, reconstrói os valores
    meta_path = EPISODIOS_DIR / f".meta_{hoje_str}.env"
    if meta_path.exists():
        meta: dict[str, str] = {}
        for linha in meta_path.read_text(encoding="utf-8").splitlines():
            if "=" in linha:
                k, v = linha.split("=", 1)
                meta[k.strip()] = v.strip()
        audio_url = meta.get("RELEASE_MP3_URL",
                             f"https://github.com/{repo}/releases/download/ep-{hoje_str}/{nome_mp3}")
        dur_match = re.match(r"(\d+)m(\d+)s", meta.get("DURACAO", "0m0s"))
        duracao_s = (int(dur_match.group(1)) * 60 + int(dur_match.group(2))) if dur_match else 0.0
    else:
        # Modo apenas_whatsapp: reconstrói URL do Release a partir do padrão conhecido
        print(f"[WPP] .meta não encontrado — reconstruindo URL do Release")
        audio_url = f"https://github.com/{repo}/releases/download/ep-{hoje_str}/{nome_mp3}"
        duracao_s = 0.0

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
