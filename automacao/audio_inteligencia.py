"""Inteligência local pra transcrição + análise de áudio do Life Planner.

Fluxo:
1. `transcrever_local(audio_path)` → texto (faster-whisper PT).
2. `extrair_sinais(texto, cliente)` → lista de sinais detectados, cada um
   apontando ajustes a aplicar no `cliente` ou nas `linhas` do planejamento.
3. `aplicar_sinais(cliente, sinais)` → mutação do dict cliente (campos que
   alimentam os Blends de Ouro) + lista de linhas pra forçar ativas/capital
   acima do padrão.

Regras locais (sem LLM):
- profissões de risco: cirurgião, dentista, médico, motoboy, militar,
  bombeiro, piloto, motorista, construção, eletricista, mineiro, marinheiro,
  agricultor, segurança, soldador, pintor, andaime, paraquedista, MMA
- doenças preexistentes: hipertensão, diabetes, colesterol, ansiedade,
  depressão, asma, câncer (família), cardio, AVC, hepatite
- dependentes: filho/filha, esposa/marido, mãe/pai, idoso, criança
- patrimônio / sucessão: herança, sucessão, holding, empresa, sócio, imóveis,
  patrimônio, milhão, MM
- esportes / atividade: paraquedismo, mergulho, jiu-jitsu, MMA, futebol,
  ciclismo, corrida, maratona, trilha, escalada
- âncoras de capital: financiamento, hipoteca, escola particular, faculdade

Cada sinal tem campos:
  tipo     → "profissao" | "doenca" | "dependentes" | "patrimonio" | "atividade"
  rotulo   → string curta pra mostrar pro LP
  evidencia → trecho do texto que disparou
  ajustes_cliente → dict com chaves do cliente (med_continuo, tem_dependentes, ...)
  ajustes_linhas  → dict {linha_id: "forcar_ativa"|"capital_x2"|...}

A lista é exibida pro LP confirmar (não aplica direto sem revisão na UI).
"""
from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Optional


# ─── Catálogo de regras locais ─────────────────────────────────────────────

# Profissões de risco — mapeia padrão → ajustes
PROFISSOES_RISCO = [
    {
        # Tolera transcrição imprecisa do Whisper tiny: "cirurgiano", "cirurgião"
        "match": r"\b(cirurgi[ãa]o|cirurgi[ãa]n[oa]|cirurgia)\b",
        "rotulo": "Cirurgião — mãos são ferramenta de trabalho",
        "ajustes_cliente": {"profissao_hint": "Cirurgião"},
        # Cirurgião: fratura na mão = parar de operar. Quebra Ossos + RIT críticos.
        # IPA também precisa estar bem coberta.
        "ajustes_linhas": {
            "quebra_ossos":         "forcar_ativa",
            "renda_incapacidade":   "forcar_ativa_max",
            "invalidez_permanente": "forcar_ativa_max",
            "invalidez_acidente":   "forcar_ativa",
            "cirurgias":            "forcar_ativa",
        },
    },
    {
        "match": r"\b(dentista|odontolog)\b",
        "rotulo": "Dentista — uso intensivo das mãos",
        "ajustes_cliente": {"profissao_hint": "Dentista"},
        "ajustes_linhas": {
            "quebra_ossos":         "forcar_ativa",
            "renda_incapacidade":   "forcar_ativa_max",
            "invalidez_permanente": "forcar_ativa_max",
        },
    },
    {
        "match": r"\b(m[ée]dic[oa]|cl[íi]nic[oa])\b",
        "rotulo": "Médico — profissão diferenciada AZOS",
        "ajustes_cliente": {"profissao_hint": "Médico"},
        "ajustes_linhas": {
            "renda_incapacidade":   "forcar_ativa",
            "doencas_graves_dg30":  "forcar_ativa",
        },
    },
    {
        "match": r"\b(pilot[oa]\s+(de\s+)?avi[ãa]o|aviador|comandante)\b",
        "rotulo": "Piloto — exposição a risco aéreo elevado",
        "ajustes_cliente": {"profissao_hint": "Piloto"},
        "ajustes_linhas": {
            "morte_acidental":     "forcar_ativa_max",
            "invalidez_acidente":  "forcar_ativa_max",
            "morte_tradicional":   "forcar_ativa_max",
        },
    },
    {
        "match": r"\b(bombeir[oa]|policial|militar|seguran[çc]a\s+armad)\b",
        "rotulo": "Profissão de risco (bombeiro/policial/militar)",
        "ajustes_cliente": {"profissao_hint": "Segurança pública"},
        "ajustes_linhas": {
            "morte_acidental":     "forcar_ativa_max",
            "invalidez_acidente":  "forcar_ativa_max",
            "quebra_ossos":        "forcar_ativa",
        },
    },
    {
        "match": r"\b(motoboy|moto\s*taxi|entregador\s+(de\s+)?moto|motoqueiro)\b",
        "rotulo": "Motoboy — exposição rodoviária elevada",
        "ajustes_cliente": {"profissao_hint": "Motoboy"},
        "ajustes_linhas": {
            "morte_acidental":     "forcar_ativa_max",
            "invalidez_acidente":  "forcar_ativa_max",
            "quebra_ossos":        "forcar_ativa",
        },
    },
    {
        "match": r"\b(eletricista|soldador|pintor\s+industrial|andaim|alpinist|construtor|pedreiro|mestre\s+de\s+obras)\b",
        "rotulo": "Construção civil / trabalho em altura",
        "ajustes_cliente": {"profissao_hint": "Construção civil"},
        "ajustes_linhas": {
            "morte_acidental":      "forcar_ativa_max",
            "invalidez_acidente":   "forcar_ativa_max",
            "quebra_ossos":         "forcar_ativa",
            "internacao_hospitalar":"forcar_ativa",
        },
    },
    {
        "match": r"\b(engenheir[oa]|arquitet[oa]|advogad[oa])\b",
        "rotulo": "Profissão liberal (engenheiro/arquiteto/advogado)",
        "ajustes_cliente": {"profissao_hint": "Profissão liberal"},
        "ajustes_linhas": {"renda_incapacidade": "forcar_ativa"},
    },
    {
        "match": r"\b(empres[áa]ri[oa]|s[óo]cio|fundador|CEO|empreendedor)\b",
        "rotulo": "Empresário — foco em sucessão patrimonial",
        "ajustes_cliente": {"profissao_hint": "Empresário"},
        "ajustes_linhas": {
            "morte_whole_life":    "forcar_ativa",
            "doencas_graves_dg30": "forcar_ativa",
        },
    },
]

# Doenças preexistentes / risco saúde
DOENCAS = [
    {
        "match": r"\b(hipertens[ãa]o|press[ãa]o\s+alta)\b",
        "rotulo": "Hipertensão (uso contínuo de medicamento)",
        "ajustes_cliente": {"med_continuo": "sim", "doenca_hipertensao": True},
        "ajustes_linhas": {"doencas_graves_dg30": "forcar_ativa", "renda_incapacidade": "forcar_ativa"},
    },
    {
        "match": r"\b(diabet|insulin|glicose\s+alta)\b",
        "rotulo": "Diabetes",
        "ajustes_cliente": {"med_continuo": "sim", "doenca_diabetes": True},
        "ajustes_linhas": {"doencas_graves_dg30": "forcar_ativa", "internacao_hospitalar": "forcar_ativa"},
    },
    {
        "match": r"\b(colesterol\s+alto|dislipid)\b",
        "rotulo": "Colesterol alto",
        "ajustes_cliente": {"med_continuo": "sim"},
        "ajustes_linhas": {"doencas_graves_dg30": "forcar_ativa"},
    },
    {
        "match": r"\b(c[âa]ncer|tumor|oncolog|quimioterap)\b",
        "rotulo": "Câncer (histórico próprio ou familiar)",
        "ajustes_cliente": {"historico_cancer": True},
        "ajustes_linhas": {"doencas_graves_dg30": "forcar_ativa_max"},
    },
    {
        "match": r"\b(card[ií]ac|infarto|AVC|derrame|arritmi)\b",
        "rotulo": "Doença cardiovascular",
        "ajustes_cliente": {"med_continuo": "sim", "doenca_cardio": True},
        "ajustes_linhas": {
            "doencas_graves_dg30": "forcar_ativa_max",
            "internacao_hospitalar": "forcar_ativa",
        },
    },
    {
        "match": r"\b(asma|bronquite\s+cr[ôo]nica|DPOC)\b",
        "rotulo": "Asma / problema respiratório",
        "ajustes_cliente": {"med_continuo": "sim"},
        "ajustes_linhas": {"internacao_hospitalar": "forcar_ativa"},
    },
    {
        "match": r"\b(ansiedade|depress[ãa]o|burnout)\b",
        "rotulo": "Saúde mental (ansiedade/depressão)",
        "ajustes_cliente": {"med_continuo": "sim"},
        "ajustes_linhas": {"renda_incapacidade": "forcar_ativa"},
    },
]

# Dependentes e família
DEPENDENTES = [
    {
        "match": r"\b(\d+)\s+filh",
        "rotulo": "Tem filhos",
        "extrair_qtd": True,
        "ajustes_cliente": {"tem_dependentes": True},
        "ajustes_linhas": {
            "morte_tradicional":  "forcar_ativa_max",
            "renda_incapacidade": "forcar_ativa",
        },
    },
    {
        "match": r"\b(filh[oa]s?|crian[çc]a|bebê|gestante)\b",
        "rotulo": "Tem filhos / criança",
        "ajustes_cliente": {"tem_dependentes": True},
        "ajustes_linhas": {
            "morte_tradicional":  "forcar_ativa_max",
            "renda_incapacidade": "forcar_ativa",
        },
    },
    {
        "match": r"\b(esposa|marido|c[ôo]njuge|companheir[oa])\s+(n[ãa]o\s+trabalha|do\s+lar|dependente)\b",
        "rotulo": "Cônjuge dependente financeiro",
        "ajustes_cliente": {"tem_dependentes": True, "estado_civil": "casado"},
        "ajustes_linhas": {
            "morte_tradicional":   "forcar_ativa_max",
            "morte_whole_life":    "forcar_ativa",
        },
    },
    {
        "match": r"\b(cuida\s+(do|da|dos|das)\s+(pai|m[ãa]e|sogr|irm[ãa]o|av[óo]))\b",
        "rotulo": "Sustenta pais/sogros/parentes",
        "ajustes_cliente": {"tem_dependentes": True},
        "ajustes_linhas": {
            "saf_essencial":      "forcar_ativa",
            "funeral_azos":       "forcar_ativa",
        },
    },
    {
        "match": r"\b(solteir[oa]|sem\s+filh)\b",
        "rotulo": "Sem dependentes",
        "ajustes_cliente": {"tem_dependentes": False, "estado_civil": "solteiro"},
        "ajustes_linhas": {
            "morte_tradicional":     "reduzir_capital",
            "invalidez_permanente":  "forcar_ativa_max",
            "renda_incapacidade":    "forcar_ativa_max",
        },
    },
]

# Patrimônio / sucessão
PATRIMONIO = [
    {
        "match": r"\b(sucess[ãa]o|herdeir|holding|grupo\s+familiar|patrim[ôo]nio\s+(em\s+)?im[óo]vei|legado)\b",
        "rotulo": "Foco em sucessão patrimonial",
        "ajustes_cliente": {"foco_sucessao": True},
        "ajustes_linhas": {
            "morte_whole_life":    "forcar_ativa_max",
            "morte_tradicional":   "reduzir_capital",
        },
    },
    {
        "match": r"\b(\d+)\s*(milh[õo]es|MM)\b",
        "rotulo": "Patrimônio multi-milionário",
        "extrair_qtd": True,
        "ajustes_cliente": {"foco_sucessao": True},
        "ajustes_linhas": {"morte_whole_life": "forcar_ativa_max"},
    },
    {
        "match": r"\b(financiamento|hipoteca|im[óo]vel\s+financiad|cr[ée]dito\s+imobili[áa]rio)\b",
        "rotulo": "Tem financiamento ativo (cobrir saldo devedor)",
        "ajustes_cliente": {"tem_financiamento": True},
        "ajustes_linhas": {"morte_tradicional": "forcar_ativa_max"},
    },
    {
        "match": r"\b(escola\s+particular|faculdade\s+particular|forma[çc][ãa]o\s+filh)\b",
        "rotulo": "Educação privada dos filhos",
        "ajustes_cliente": {"tem_dependentes": True},
        "ajustes_linhas": {"morte_tradicional": "forcar_ativa_max"},
    },
]

# Atividade / esportes / lazer
ATIVIDADES = [
    {
        "match": r"\b(paraqued|salto\s+(em|de)\s+(alta|paraquedas)|skydiving)\b",
        "rotulo": "Paraquedismo (esporte radical)",
        "ajustes_cliente": {"esporte_radical": True},
        "ajustes_linhas": {
            "morte_acidental":    "forcar_ativa_max",
            "invalidez_acidente": "forcar_ativa_max",
        },
    },
    {
        "match": r"\b(mergulho|scuba|apneia)\b",
        "rotulo": "Mergulho (esporte de risco)",
        "ajustes_cliente": {"esporte_radical": True},
        "ajustes_linhas": {"morte_acidental": "forcar_ativa_max"},
    },
    {
        "match": r"\b(MMA|jiu[\s-]?jitsu|muay\s*thai|boxe|luta\s+livre)\b",
        "rotulo": "Luta / artes marciais",
        "ajustes_cliente": {"esporte_radical": True},
        "ajustes_linhas": {
            "quebra_ossos":        "forcar_ativa",
            "invalidez_acidente":  "forcar_ativa",
        },
    },
    {
        "match": r"\b(moto\s*velocidade|track\s*day|gymkhana|enduro)\b",
        "rotulo": "Moto velocidade",
        "ajustes_cliente": {"esporte_radical": True},
        "ajustes_linhas": {
            "morte_acidental":    "forcar_ativa_max",
            "invalidez_acidente": "forcar_ativa_max",
        },
    },
    {
        "match": r"\b(escalad|montanhism|trilha\s+t[ée]cnica|alpinismo)\b",
        "rotulo": "Escalada / montanhismo",
        "ajustes_cliente": {"esporte_radical": True},
        "ajustes_linhas": {
            "quebra_ossos":      "forcar_ativa",
            "morte_acidental":   "forcar_ativa",
        },
    },
]

# Estilo de vida
ESTILO_VIDA = [
    {
        "match": r"\b(fum[ao]|cigarr|tabag|nicotin)\b",
        "rotulo": "Fumante",
        "ajustes_cliente": {"fumante": "sim"},
        "ajustes_linhas": {"doencas_graves_dg30": "forcar_ativa"},
    },
    {
        "match": r"\b(viaja\s+muito|viajante|expatri|nomade?\s+digital)\b",
        "rotulo": "Viaja muito (exposição rodoviária/aérea)",
        "ajustes_cliente": {"viaja_muito": True},
        "ajustes_linhas": {"morte_acidental": "forcar_ativa"},
    },
]

REGRAS = (PROFISSOES_RISCO + DOENCAS + DEPENDENTES + PATRIMONIO +
          ATIVIDADES + ESTILO_VIDA)


# ─── Transcrição local (faster-whisper) ────────────────────────────────────

_MODEL = None


def _carregar_modelo():
    """Lazy-load do faster-whisper. Modelo configurável via WHISPER_MODEL."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            "faster-whisper não instalado. Adicione 'faster-whisper' ao requirements.txt"
        )
    model_size = os.getenv("WHISPER_MODEL", "tiny")  # tiny|base|small|medium
    compute_type = os.getenv("WHISPER_COMPUTE", "int8")  # int8|float16|float32
    print(f"[audio-IA] carregando faster-whisper modelo={model_size} compute={compute_type}...", flush=True)
    _MODEL = WhisperModel(model_size, compute_type=compute_type, device="cpu")
    print(f"[audio-IA] modelo carregado", flush=True)
    return _MODEL


def transcrever_local(audio_path: str | Path, idioma: str = "pt") -> str:
    """Transcreve áudio local pra texto em PT-BR.

    Retorna string contínua (segmentos concatenados). Erros levantam exceção.
    """
    path = Path(audio_path)
    if not path.exists():
        raise FileNotFoundError(f"áudio não encontrado: {path}")
    model = _carregar_modelo()
    segments, info = model.transcribe(str(path), language=idioma, beam_size=1)
    texto = " ".join(seg.text.strip() for seg in segments if seg.text)
    print(f"[audio-IA] transcrito {len(texto)} chars (idioma={info.language} dur={info.duration:.1f}s)", flush=True)
    return texto.strip()


# ─── Extração de sinais (sem LLM) ──────────────────────────────────────────

def extrair_sinais(texto: str) -> list[dict]:
    """Procura padrões REGRAS no texto e devolve lista de sinais detectados.

    Cada sinal:
      {
        tipo: "profissao" | "doenca" | "dependentes" | "patrimonio" | "atividade" | "estilo",
        rotulo: str,
        evidencia: str,   # trecho do texto que disparou
        ajustes_cliente: dict,
        ajustes_linhas:  dict,
      }
    """
    if not texto:
        return []
    texto_low = texto.lower()
    detectados: list[dict] = []
    vistos: set[str] = set()
    for regra in REGRAS:
        m = re.search(regra["match"], texto_low, re.IGNORECASE)
        if not m:
            continue
        rotulo = regra["rotulo"]
        if rotulo in vistos:
            continue
        vistos.add(rotulo)
        ini = max(0, m.start() - 30)
        fim = min(len(texto), m.end() + 30)
        evidencia = texto[ini:fim].strip()
        tipo = _classificar_tipo(regra)
        sinal = {
            "tipo":            tipo,
            "rotulo":          rotulo,
            "evidencia":       evidencia,
            "ajustes_cliente": dict(regra.get("ajustes_cliente") or {}),
            "ajustes_linhas":  dict(regra.get("ajustes_linhas")  or {}),
        }
        if regra.get("extrair_qtd"):
            try:
                sinal["quantidade"] = int(m.group(1))
            except (IndexError, ValueError):
                pass
        detectados.append(sinal)
    return detectados


def _classificar_tipo(regra: dict) -> str:
    """Identifica o tipo do sinal pela posição no catálogo."""
    if regra in PROFISSOES_RISCO: return "profissao"
    if regra in DOENCAS:          return "doenca"
    if regra in DEPENDENTES:      return "dependentes"
    if regra in PATRIMONIO:       return "patrimonio"
    if regra in ATIVIDADES:       return "atividade"
    if regra in ESTILO_VIDA:      return "estilo"
    return "outro"


def aplicar_sinais_no_cliente(cliente: dict, sinais: list[dict]) -> dict:
    """Mescla `ajustes_cliente` dos sinais no dict cliente (não-destrutivo).

    Retorna o cliente MODIFICADO. Campos do form preenchidos pelo LP vencem.
    """
    novo = dict(cliente)
    for sinal in sinais:
        for k, v in (sinal.get("ajustes_cliente") or {}).items():
            # Campos do form (não vazios) vencem sobre dedução do áudio
            if k in cliente and str(cliente[k] or "").strip():
                continue
            novo[k] = v
    return novo


def consolidar_ajustes_linhas(sinais: list[dict]) -> dict:
    """Agrega `ajustes_linhas` dos sinais em um único dict {linha_id: acao}.

    Quando 2 sinais pedem ações diferentes pra mesma linha, a mais forte vence:
    forcar_ativa_max > forcar_ativa > reduzir_capital.
    """
    forca = {"forcar_ativa_max": 3, "forcar_ativa": 2, "reduzir_capital": 1}
    out: dict[str, str] = {}
    for sinal in sinais:
        for linha_id, acao in (sinal.get("ajustes_linhas") or {}).items():
            atual = out.get(linha_id)
            if atual is None or forca.get(acao, 0) > forca.get(atual, 0):
                out[linha_id] = acao
    return out


# ─── Pipeline completo ─────────────────────────────────────────────────────

def analisar_audio(audio_path: str | Path, cliente: dict) -> dict:
    """Transcreve + extrai sinais + consolida ajustes em uma chamada.

    Retorna:
      {
        "texto": str,
        "sinais": [...],
        "cliente_enriquecido": dict,
        "ajustes_linhas":      dict,
        "erro":                str | None,
      }
    """
    out: dict = {
        "texto": "", "sinais": [], "cliente_enriquecido": dict(cliente),
        "ajustes_linhas": {}, "erro": None,
    }
    try:
        texto = transcrever_local(audio_path)
        out["texto"] = texto
        sinais = extrair_sinais(texto)
        out["sinais"] = sinais
        out["cliente_enriquecido"] = aplicar_sinais_no_cliente(cliente, sinais)
        out["ajustes_linhas"] = consolidar_ajustes_linhas(sinais)
    except Exception as e:
        out["erro"] = str(e)[:300]
        print(f"[audio-IA] ERRO: {out['erro']}", flush=True)
    return out
