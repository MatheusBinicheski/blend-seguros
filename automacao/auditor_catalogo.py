"""
Auditor de catálogo Blend Seguros.

Cruza o catálogo `_LINHAS_COMPARATIVAS` do recomendador contra as TABELAS
OFICIAIS extraídas dos manuais de subscrição AZOS e MAG. Detecta:

  - Coberturas marcadas como "indisponíveis" que na verdade existem na
    seguradora (ex: AZOS Morte Acidental, AZOS IPTA Majorada).
  - Pares de cobertura comparadas erroneamente (ex: DG VITAL × DG30).
  - Capital_max do catálogo divergente do limite por idade/renda do manual.
  - Linhas sem `fonte` declarada.
  - Idade do cliente ultrapassando o corte da cobertura.

Também faz CLAMP do capital sugerido para o limite real por (seguradora,
cobertura, idade, renda) — usado pelo `planejamento_grid` antes de mostrar
a sugestão ao Life Planner.

Fontes oficiais usadas:

  - Manual de Subscrição AZOS — Abril/2026 v2 (Excelsior Seguros, SUSEP
    15414.604991/2023-12). Páginas 4-20.
  - Dicas de Underwriting MAG — Agosto/2023 v1.9 (Mongeral Aegon). Páginas
    4-15. (Manual oficial 2026 da MAG ainda não publicado — substituir
    quando sair.)

Para mensagens de auditoria, sempre cite a página da fonte para o LP poder
abrir e conferir.
"""
from __future__ import annotations

from typing import Any, Iterable


# ─────────────────────────────────────────────────────────────────────────────
# FONTES (citações curtas que vão nos warnings)
# ─────────────────────────────────────────────────────────────────────────────
FONTE_AZOS = "Manual Azos Abr/26 v2"
FONTE_MAG  = "Dicas Underwriting MAG Ago/23 v1.9"


# ─────────────────────────────────────────────────────────────────────────────
# TABELA AZOS — coberturas disponíveis (Manual Abr/26 pg 4)
# ─────────────────────────────────────────────────────────────────────────────
AZOS_COBERTURAS_OFICIAIS = {
    "morte":                "Morte (até R$5MM)",
    "morte_acidental":      "Morte Acidental — MAC (até R$1MM)",
    "ipta_majorada":        "IPTA Majorada (até R$3MM, 12 eventos)",
    "ipta_majorada_estendida": "IPTA Majorada Estendida (até R$1MM, médico/dentista)",
    "ipt":                  "Invalidez Permanente Total — IPT (até R$1MM, 11 eventos)",
    "dg13":                 "Doenças Graves 13 (até R$1MM)",
    "dg30":                 "Doenças Graves 30 (até R$1MM)",
    "dih":                  "Diária Internação Hospitalar (até R$1k/diária, 200 diárias/evento)",
    "rit":                  "Renda Incapacidade Temporária (até R$1k/diária)",
    "rit_sr":               "RIT sem Retroativo (até R$1k/diária)",
    "cirurgias_2_0":        "Cirurgias 2.0 (até R$100k, 652 procedimentos)",
    "rupturas_fraturas":    "Rupturas e Fraturas — REF (até R$300k)",
    "funeral_individual":   "Funeral Individual (R$15k)",
    "funeral_individual_pais": "Funeral Individual + Pais (R$15k, pais 120d carência)",
    "funeral_familiar":     "Funeral Familiar (R$15k)",
    "funeral_familiar_pais_sogros": "Funeral Familiar + Pais e Sogros (R$15k, pais/sogros 120d)",
}

# Idade de corte por cobertura AZOS (Manual pg 1)
AZOS_IDADE_CORTE = {
    # cobertura → idade que cancela (None = vitalício enquanto pagar)
    "morte":                None,
    "morte_acidental":      None,
    "ipta_majorada":        None,
    "ipta_majorada_estendida": None,
    "ipt":                  75,
    "dg13":                 75,
    "dg30":                 75,
    "dih":                  70,
    "rit":                  70,
    "rit_sr":               70,
    "cirurgias_2_0":        70,
    "rupturas_fraturas":    75,
    "funeral_individual":   None,
    "funeral_individual_pais": None,
    "funeral_familiar":     None,
    "funeral_familiar_pais_sogros": None,
}

# Capital máximo absoluto (Manual pg 14) — limite teórico antes de renda/idade
AZOS_CAP_MAX_ABS = {
    "morte":                5_000_000,
    "morte_acidental":      1_000_000,
    "ipta_majorada":        3_000_000,
    "ipta_majorada_estendida": 1_000_000,
    "ipt":                  1_000_000,
    "dg13":                 1_000_000,
    "dg30":                 1_000_000,
    "dih":                  1_000,         # R$/diária
    "rit":                  1_000,         # R$/diária
    "rit_sr":               1_000,
    "cirurgias_2_0":        100_000,
    "rupturas_fraturas":    300_000,
    "funeral_individual":   15_000,
    "funeral_individual_pais": 15_000,
    "funeral_familiar":     15_000,
    "funeral_familiar_pais_sogros": 15_000,
}


# Tabela Azos: cap máximo Morte por (faixa_renda, faixa_idade) — Manual pg 16
# Cada chave é (renda_min, renda_max] em R$ mensal.
# Cada valor é dict {idade_max: cap_max} — usa o primeiro idade_max ≥ idade.
AZOS_TABELA_MORTE = [
    # (lim_inf_renda_excl, lim_sup_renda_incl, {faixa_idade_max: cap_max})
    (    0,   1_500, {50:   200_000, 60:   200_000, 65:   200_000}),
    (1_500,   3_000, {50:   300_000, 60:   300_000, 65:   300_000}),
    (3_000,   5_000, {50:   500_000, 60:   500_000, 65:   500_000}),
    (5_000,   7_000, {50: 1_000_000, 60: 1_000_000, 65:   750_000}),
    (7_000,  10_000, {50: 2_000_000, 60: 2_000_000, 65: 1_000_000}),
    (10_000, 15_000, {50: 2_000_000, 60: 2_000_000, 65: 1_000_000}),
    (15_000, 20_000, {50: 3_000_000, 60: 3_000_000, 65: 1_000_000}),
    (20_000, 30_000, {50: 4_000_000, 60: 3_000_000, 65: 1_000_000}),
    (30_000, 10**9,  {50: 5_000_000, 60: 4_000_000, 65: 1_000_000}),  # Prata/Ouro/Partner
]

# Manual pg 17
AZOS_TABELA_MORTE_ACIDENTAL = [
    (    0,   1_500, {60:   200_000, 65:   200_000}),
    (1_500,   3_000, {60:   300_000, 65:   300_000}),
    (3_000,   5_000, {60:   500_000, 65:   500_000}),
    (5_000,   7_000, {60: 1_000_000, 65: 1_000_000}),
    (7_000,  10_000, {60: 1_000_000, 65: 1_000_000}),
    (10_000, 15_000, {60: 1_000_000, 65: 1_000_000}),
    (15_000, 10**9,  {60: 1_000_000, 65: 1_000_000}),
]

# Manual pg 17
AZOS_TABELA_IPTA_MAJ = [
    (    0,   1_500, {60:   200_000, 65:   200_000}),
    (1_500,   3_000, {60:   300_000, 65:   300_000}),
    (3_000,   5_000, {60:   500_000, 65:   500_000}),
    (5_000,   7_000, {60: 1_000_000, 65: 1_000_000}),
    (7_000,  10_000, {60: 2_000_000, 65: 2_000_000}),
    (10_000, 15_000, {60: 2_000_000, 65: 2_000_000}),
    (15_000, 10**9,  {60: 3_000_000, 65: 2_000_000}),
]

# Manual pg 18
AZOS_TABELA_IPT = [
    (    0,   1_500, {60:   200_000, 65:   200_000}),
    (1_500,   3_000, {60:   300_000, 65:   300_000}),
    (3_000,   5_000, {60:   500_000, 65:   500_000}),
    (5_000,   7_000, {60: 1_000_000, 65:   500_000}),
    (7_000,  10_000, {60: 1_000_000, 65:   500_000}),
    (10_000, 15_000, {60: 1_000_000, 65:   500_000}),
    (15_000, 10**9,  {60: 1_000_000, 65:   500_000}),
]

# Manual pg 19 — DG13 e DG30 compartilham tabela
AZOS_TABELA_DG = [
    (    0,   1_500, {60:   200_000, 65:   100_000}),
    (1_500,   3_000, {60:   300_000, 65:   100_000}),
    (3_000,   5_000, {60:   500_000, 65:   100_000}),
    (5_000,   7_000, {60:   500_000, 65:   200_000}),
    (7_000,  10_000, {60:   750_000, 65:   200_000}),
    (10_000, 15_000, {60: 1_000_000, 65:   300_000}),
    (15_000, 10**9,  {60: 1_000_000, 65:   300_000}),
]

# Manual pg 19-20 — DIH (valor de diária por renda)
AZOS_TABELA_DIH = [
    (    0,   1_500, {60:   100, 65:   100}),
    (1_500,   3_000, {60:   150, 65:   150}),
    (3_000,   5_000, {60:   250, 65:   250}),
    (5_000,   7_000, {60:   500, 65:   500}),
    (7_000,  10_000, {60: 1_000, 65:   500}),
    (10_000, 15_000, {60: 1_000, 65:   500}),
    (15_000, 10**9,  {60: 1_000, 65:   500}),
]

# Manual pg 20 — RIT: 1/30 do salário, máximo R$1k (até 60) / R$500 (61-65)
AZOS_TABELA_RIT_TETO = {60: 1_000, 65: 500}


# ─────────────────────────────────────────────────────────────────────────────
# TABELA MAG — Dicas Underwriting Ago/23 (manual atual)
# ─────────────────────────────────────────────────────────────────────────────

# Pg 5: múltiplo de renda mensal por faixa etária (capital máximo recomendado)
MAG_MULTIPLO_RENDA_POR_IDADE = [
    # (idade_max, multiplo_renda_mensal)
    (30, 360),
    (40, 300),
    (50, 240),
    (60, 180),
    (65, 120),
    (90,  84),
]

# Pg 8: capital máximo Morte automático
MAG_CAP_MORTE_AUTO = [
    (60, 1_700_000),
    (65, 1_200_000),
    (70, 1_000_000),
    (80, 1_000_000),
    (85,   500_000),
]

# Pg 8: capital máximo INVALIDEZ automático (mesmo schema)
MAG_CAP_INVALIDEZ_AUTO = [
    (60, 1_700_000),
    (65, 1_200_000),
    (70, 1_000_000),
    (80, 1_000_000),
]

# Pg 8: DIT/DITA — capital por grupo de risco profissional
# Grupo 0 = risco baixo, Grupo 3 = risco alto
MAG_DIT_POR_GRUPO_RISCO = {
    0: 40_000,
    1: 30_000,
    2: 20_000,
    3: 20_000,
}

# Pg 8: Cirurgias automático
MAG_CAP_CIRURGIAS_AUTO = 50_000

# Pg 8: DIH (R$/dia)
MAG_CAP_DIH_SEM_UTI = 3_000   # sem adicional UTI
MAG_CAP_DIH_COM_UTI = 9_000   # adicional UTI 200%

# Pg 8: DG Plus
MAG_CAP_DG_PLUS_VIDATODA_DPS = 500_000
MAG_CAP_DG_PLUS_PRIVATE      = 1_000_000

# DG VITAL é rider de DG Plus/Modular, câncer-only, cap máx 200k
# (informação fornecida pelo usuário, não consta nas Dicas — manual 2026 vai
#  formalizar; manter como nota)
MAG_CAP_DG_VITAL_CANCER_RIDER = 200_000

# Atleta profissional / jogador futebol: máximo acumulado 10M (pg 12)
MAG_CAP_ATLETA_MAX = 10_000_000

# Administrador do lar / Estudante (pg 4):
MAG_CAP_ADM_LAR_VIDATODA = 200_000
MAG_CAP_ADM_LAR_PRIVATE  = 400_000

# Privet VD STOA — prêmio mensal mínimo (informação do usuário, formaliza
# depois de sair Manual 2026 oficial MAG)
MAG_PRIVET_PREMIO_MINIMO_MES = 400.00


# ─────────────────────────────────────────────────────────────────────────────
# LOOKUP HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _lookup_renda_idade(tabela: list, renda: float, idade: int) -> int | None:
    """Recebe tabela [(renda_inf_excl, renda_sup_incl, {idade_max: cap})...]
    e devolve cap aplicável."""
    for renda_inf, renda_sup, faixas_idade in tabela:
        if renda_inf < renda <= renda_sup:
            for idade_max in sorted(faixas_idade.keys()):
                if idade <= idade_max:
                    return faixas_idade[idade_max]
            return None  # idade acima da última faixa
    # renda ≤ 0 → considera primeira faixa (mais restritiva)
    if renda <= 0 and tabela:
        faixas_idade = tabela[0][2]
        for idade_max in sorted(faixas_idade.keys()):
            if idade <= idade_max:
                return faixas_idade[idade_max]
    return None


def _lookup_idade(tabela: list, idade: int) -> int | None:
    """Recebe tabela [(idade_max, valor)...] e devolve valor aplicável."""
    for idade_max, val in tabela:
        if idade <= idade_max:
            return val
    return None


def _grupo_risco_profissao(profissao: str) -> int:
    """Heurística simples baseada nas Dicas MAG. Mapeia para 0..3."""
    p = (profissao or "").lower()
    # Grupo 3 = risco alto (piloto teste, polícia, segurança armada,
    # bombeiro civil, manobrista de carga viva, mineração subterrânea)
    g3 = ("piloto teste", "policial militar", "policia militar", "segurança armada",
          "bombeiro civil", "vigilante armado", "mineiro", "soldador subaquático",
          "mergulhador", "estivador")
    if any(t in p for t in g3): return 3
    # Grupo 2 = risco médio-alto (motorista profissional carreta, motoboy,
    # operador de máquina pesada, eletricista alta tensão)
    g2 = ("caminhoneiro", "motoboy", "carreteiro", "operador de máquina",
          "eletricista de alta tensão", "ajudante de obra", "pedreiro",
          "construtor", "policial civil", "agente penitenciário")
    if any(t in p for t in g2): return 2
    # Grupo 1 = risco baixo-médio (motorista comum, vendedor externo,
    # técnico de campo)
    g1 = ("motorista", "vendedor externo", "técnico de campo", "frentista",
          "garçom", "cozinheiro", "açougueiro", "padeiro")
    if any(t in p for t in g1): return 1
    # Default 0 = administrativo / saúde não cirúrgica / TI / financeiro
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# CLAMPS PÚBLICOS
# ─────────────────────────────────────────────────────────────────────────────
def clamp_capital_azos(linha_id: str, idade: int, renda: float) -> tuple[int | None, str]:
    """Devolve (cap_max_permitido, motivo) para a cobertura AZOS.

    Retorno:
      - (int >0, motivo)  → clamp aplicável
      - (None, motivo)    → AZOS não oferece essa linha (sem clamp)
      - (0, motivo)       → AZOS não aceita pra esse perfil (idade fora corte, etc)
    """
    # Mapeamento linha_id (recomendador) → cobertura oficial Azos
    mapa = {
        "morte_tradicional":     ("morte",                AZOS_TABELA_MORTE),
        "morte_term_life":       (None, None),
        "morte_whole_life":      (None, None),
        "morte_acidental":       ("morte_acidental",      AZOS_TABELA_MORTE_ACIDENTAL),
        "invalidez_permanente":  ("ipt",                  AZOS_TABELA_IPT),
        "invalidez_acidente":    ("ipta_majorada",        AZOS_TABELA_IPTA_MAJ),
        "ipta_majorada_estendida": ("ipta_majorada_estendida", AZOS_TABELA_IPTA_MAJ),
        "doencas_graves_dg13":   ("dg13",                 AZOS_TABELA_DG),
        "doencas_graves_dg30":   ("dg30",                 AZOS_TABELA_DG),
        "doencas_graves_vital_cancer": (None, None),
        "cirurgias":             ("cirurgias_2_0",        None),
        "quebra_ossos":          ("rupturas_fraturas",    None),
        "internacao_hospitalar": ("dih",                  AZOS_TABELA_DIH),
        "renda_incapacidade":    ("rit",                  None),
        "funeral_azos":          ("funeral_familiar",     None),
        "saf_essencial":         (None, None),
        "saf_plus":              (None, None),
        "saf_premium":           (None, None),
    }
    cob, tabela = mapa.get(linha_id, (None, None))
    if cob is None:
        return None, "AZOS não oferece essa linha"
    # Idade de corte
    corte = AZOS_IDADE_CORTE.get(cob)
    if corte is not None and idade >= corte:
        return 0, f"AZOS cancela {cob} aos {corte} anos (cliente tem {idade}). {FONTE_AZOS} pg 1."
    # Público alvo 18-65a (mas Morte e MAC são vitalícios uma vez contratados)
    if idade > 65 and cob not in ("morte", "morte_acidental"):
        return 0, f"AZOS aceita até 65a 11m 29d para {cob}. {FONTE_AZOS} pg 1."
    if idade < 18:
        return 0, f"AZOS aceita a partir de 18a."
    # Lookup tabela renda/idade
    if tabela is not None:
        cap = _lookup_renda_idade(tabela, renda, idade)
        if cap is None:
            return AZOS_CAP_MAX_ABS.get(cob, 0), f"sem tabela renda/idade pra essa faixa — usando máximo absoluto"
        # RIT tem teto especial 1/30 do salário diário também
        if cob == "rit":
            teto_idade = AZOS_TABELA_RIT_TETO.get(60) if idade <= 60 else AZOS_TABELA_RIT_TETO.get(65)
            teto_salario = int((renda or 0) / 30) if renda else 1_000
            cap = min(cap or 10**9, teto_idade or 1_000, teto_salario or 1_000)
        return cap, f"limite renda/idade. {FONTE_AZOS} pg 16-20."
    # Sem tabela → cap máximo absoluto
    return AZOS_CAP_MAX_ABS.get(cob, 0), f"limite máximo absoluto. {FONTE_AZOS} pg 14."


def clamp_capital_mag(linha_id: str, idade: int, renda: float,
                      profissao: str = "", modelo_proposta: str = "vidatoda") -> tuple[int | None, str]:
    """Devolve (cap_max_permitido, motivo) para a cobertura MAG.

    Retorno:
      - (int >0, motivo)  → clamp aplicável
      - (None, motivo)    → capital fixo / não há clamp por renda/idade
      - (0, motivo)       → MAG não aceita pra esse perfil (idade fora, etc)

    modelo_proposta: "vidatoda" | "private" | "winsocial" | "pchv"
    """
    # Capitais Morte
    if linha_id in ("morte_tradicional", "morte_term_life", "morte_whole_life"):
        cap_idade = _lookup_idade(MAG_CAP_MORTE_AUTO, idade)
        if cap_idade is None:
            return 0, f"MAG aceita morte até 85a (cliente {idade}). {FONTE_MAG} pg 8."
        # Múltiplo de renda
        mult = _lookup_idade(MAG_MULTIPLO_RENDA_POR_IDADE, idade) or 84
        cap_renda = int((renda or 0) * mult) if renda > 0 else cap_idade
        cap = min(cap_idade, cap_renda)
        return cap, f"MAG: min(cap automático idade {cap_idade:,}, renda × {mult}). {FONTE_MAG} pg 5,8."
    if linha_id == "morte_acidental":
        # MAC MAG: rider em pacotes — acompanha limites de Morte/IPA
        cap_idade = _lookup_idade(MAG_CAP_MORTE_AUTO, idade)
        if cap_idade is None:
            return 0, f"MAG MAC até 85a. {FONTE_MAG} pg 8."
        return min(cap_idade, 1_000_000), "MAC MAG rider de pacote (limite alinhado com Morte/MAG Winsocial)."
    if linha_id in ("invalidez_permanente", "invalidez_acidente"):
        cap = _lookup_idade(MAG_CAP_INVALIDEZ_AUTO, idade)
        if cap is None:
            return 0, f"MAG: invalidez até 80a (cliente {idade}). {FONTE_MAG} pg 8."
        return cap, f"MAG: invalidez automática idade. {FONTE_MAG} pg 8."
    if linha_id == "ipta_majorada_estendida":
        return None, "MAG não tem equivalente isolado — sem clamp."
    if linha_id in ("doencas_graves_dg13", "doencas_graves_dg30"):
        if modelo_proposta == "private":
            return MAG_CAP_DG_PLUS_PRIVATE, f"DG Linha Private + tele/exames. {FONTE_MAG} pg 8."
        return MAG_CAP_DG_PLUS_VIDATODA_DPS, f"DG Linha Vida Toda + DPS. {FONTE_MAG} pg 8."
    if linha_id == "doencas_graves_vital_cancer":
        return MAG_CAP_DG_VITAL_CANCER_RIDER, "DG VITAL = rider câncer de DG Plus/Modular (cap 200k)."
    if linha_id == "cirurgias":
        return MAG_CAP_CIRURGIAS_AUTO, f"Cirurgias automáticas MAG. {FONTE_MAG} pg 8."
    if linha_id == "quebra_ossos":
        return None, "MAG sem cobertura específica de Rupturas e Fraturas — não clampável."
    if linha_id == "internacao_hospitalar":
        return MAG_CAP_DIH_SEM_UTI, f"DIH sem Adicional UTI. Com UTI 200%, sobe pra {MAG_CAP_DIH_COM_UTI:,}. {FONTE_MAG} pg 8."
    if linha_id == "renda_incapacidade":
        grupo = _grupo_risco_profissao(profissao)
        cap_mes = MAG_DIT_POR_GRUPO_RISCO.get(grupo, 20_000)
        cap_dia = int(cap_mes / 30)
        return cap_dia, f"DIT MAG grupo risco {grupo} ({profissao or '—'}): R${cap_mes:,}/mês = R${cap_dia}/dia. {FONTE_MAG} pg 8.".replace(",", ".")
    if linha_id in ("funeral_azos", "saf_essencial", "saf_plus", "saf_premium"):
        return None, "capital fixo do pacote — não clampável."
    return None, "linha MAG sem regra de clamp definida."


# ─────────────────────────────────────────────────────────────────────────────
# AUDITORIA DO CATÁLOGO ESTÁTICO
# ─────────────────────────────────────────────────────────────────────────────
def auditar_catalogo(linhas_comparativas: list[dict]) -> dict:
    """Roda na boot do FastAPI + endpoint /diagnostico/catalogo.

    Retorna dict com:
      - resumo: {linhas_total, linhas_com_fonte, erros, avisos}
      - issues: [{nivel, linha_id, mensagem, fonte_sugerida}]
      - cobertura_oficial_azos: lista de coberturas Azos que TÊM catálogo
        cobrindo e as que NÃO TÊM (gap)
    """
    issues: list[dict] = []

    # Coberturas Azos oficialmente disponíveis que deveriam estar no catálogo
    cobertas_no_catalogo = set()
    for L in linhas_comparativas:
        azos = L.get("azos") or {}
        # heuristica: qualquer linha com azos.disponivel=True conta
        if azos.get("disponivel"):
            cobertas_no_catalogo.add(L["id"])

    # Validações por linha
    for L in linhas_comparativas:
        for seg in ("azos", "mag"):
            info = L.get(seg) or {}
            if not info.get("disponivel"):
                continue
            if not info.get("fonte"):
                issues.append({
                    "nivel": "aviso",
                    "linha_id": L["id"],
                    "seguradora": seg,
                    "mensagem": f"sem campo 'fonte' declarado",
                    "fonte_sugerida": FONTE_AZOS if seg == "azos" else FONTE_MAG,
                })

        # Pares incompatíveis específicos
        if L["id"] == "doencas_graves_dg30":
            mag = L.get("mag") or {}
            nome_mag = (mag.get("produto") or "").lower()
            if "vital" in nome_mag and mag.get("disponivel"):
                issues.append({
                    "nivel": "erro",
                    "linha_id": L["id"],
                    "seguradora": "mag",
                    "mensagem": (
                        "MAG DG VITAL pareado com AZOS DG30 — produtos incompatíveis. "
                        "DG VITAL é rider câncer-only (cap 200k) de DG Plus/Modular, "
                        "não cobertura de 30 doenças. Despareiar."
                    ),
                    "fonte_sugerida": "input do produto (manual MAG 2026 oficial pendente)",
                })

        # Linha morte_acidental marcada como AZOS indisponível ainda?
        if L["id"] == "morte_acidental":
            azos = L.get("azos") or {}
            if not azos.get("disponivel"):
                issues.append({
                    "nivel": "erro",
                    "linha_id": L["id"],
                    "seguradora": "azos",
                    "mensagem": "AZOS oferece Morte Acidental (até R$1MM).",
                    "fonte_sugerida": f"{FONTE_AZOS} pg 4, 17",
                })

        # IPTA Maj separada das demais invalidezes
        if L["id"] == "invalidez_acidente":
            azos = L.get("azos") or {}
            if not azos.get("disponivel"):
                issues.append({
                    "nivel": "erro",
                    "linha_id": L["id"],
                    "seguradora": "azos",
                    "mensagem": "AZOS oferece IPTA Majorada (até R$3MM, 12 eventos).",
                    "fonte_sugerida": f"{FONTE_AZOS} pg 4, 17",
                })

    # Coberturas Azos oficiais SEM linha no catálogo (gaps a criar)
    LINHAS_ESPERADAS_AZOS = {
        "morte_tradicional",
        "morte_acidental",
        "invalidez_permanente",     # IPT
        "invalidez_acidente",       # IPTA Maj
        "ipta_majorada_estendida",  # IPTA Maj Estendida (médico/dentista)
        "doencas_graves_dg13",
        "doencas_graves_dg30",
        "internacao_hospitalar",    # DIH
        "renda_incapacidade",       # RIT/RIT-SR
        "cirurgias",                # Cirurgias 2.0
        "quebra_ossos",             # Rupturas e Fraturas
        "funeral_azos",
    }
    faltando = LINHAS_ESPERADAS_AZOS - cobertas_no_catalogo
    for linha_id in sorted(faltando):
        issues.append({
            "nivel": "erro",
            "linha_id": linha_id,
            "seguradora": "azos",
            "mensagem": f"Cobertura Azos oficial sem entrada no catálogo Blend",
            "fonte_sugerida": f"{FONTE_AZOS} pg 4 (lista oficial)",
        })

    erros  = sum(1 for i in issues if i["nivel"] == "erro")
    avisos = sum(1 for i in issues if i["nivel"] == "aviso")
    return {
        "resumo": {
            "linhas_total":     len(linhas_comparativas),
            "linhas_no_catalogo_azos": len(cobertas_no_catalogo),
            "erros":            erros,
            "avisos":           avisos,
        },
        "issues": issues,
        "fontes_oficiais": {
            "azos": f"{FONTE_AZOS} (Excelsior Seguros, SUSEP 15414.604991/2023-12)",
            "mag":  f"{FONTE_MAG} (Mongeral Aegon) — manual 2026 oficial pendente",
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# AUDITORIA INLINE DO PLANEJAMENTO (cliente + linhas escolhidas)
# ─────────────────────────────────────────────────────────────────────────────
def auditar_planejamento(cliente: dict, grid: dict) -> list[dict]:
    """Roda no /planejamento depois de gerar a grid. Devolve lista de
    warnings que vão pro frontend mostrar (ex: 'capital morte acima do
    limite Azos pra essa renda — vai exigir tele/exames')."""
    avisos: list[dict] = []
    idade = grid["cliente"].get("idade") or 40
    renda = float(grid["cliente"].get("renda_mensal") or 0)
    profissao = str(cliente.get("profissao") or "")

    # Adm do lar / estudante
    p = profissao.lower()
    if any(t in p for t in ("administrador do lar", "do lar", "estudante", "dona de casa")):
        avisos.append({
            "nivel": "aviso",
            "linha_id": "morte_tradicional",
            "mensagem": (
                f"Cliente como '{profissao}' tem limite MAG MORTE+INVALIDEZ "
                f"de R$200k (Vida Toda) ou R$400k (Privet). {FONTE_MAG} pg 4."
            ),
        })

    # Polícia, piloto, militar → roteamento profissão
    if any(t in p for t in ("polícia", "policia", "policial")):
        avisos.append({
            "nivel": "aviso",
            "linha_id": "_global",
            "mensagem": (
                "Profissão Polícia: MAG só aceita MQC (morte) com Classe 4. "
                "Recomendado priorizar AZOS para o blend. "
                f"{FONTE_MAG} pg 6."
            ),
        })
    if any(t in p for t in ("piloto", "comissário", "comissaria")):
        avisos.append({
            "nivel": "aviso",
            "linha_id": "_global",
            "mensagem": (
                "Profissão Piloto: usar canal MAG PCHV (helicóptero) ou regular "
                "(asa fixa). Capitais limitados conforme tipo de aeronave. "
                f"{FONTE_MAG} pg 6-7."
            ),
        })

    # Por linha: capital sugerido acima do limite real
    for L in grid.get("linhas", []):
        linha_id = L["id"]
        if not L.get("ativo_default"):
            continue
        for seg in ("azos", "mag"):
            info = L.get(seg) or {}
            if not info.get("disponivel"):
                continue
            cap_aplicado = int(info.get("capital_aplicado") or 0)
            if cap_aplicado <= 0:
                continue
            if seg == "azos":
                cap_max, motivo = clamp_capital_azos(linha_id, idade, renda)
            else:
                cap_max, motivo = clamp_capital_mag(linha_id, idade, renda, profissao)
            if cap_max is None:
                continue  # sem clamp = nada a auditar
            if cap_max == 0:
                avisos.append({
                    "nivel": "erro",
                    "linha_id": linha_id,
                    "seguradora": seg,
                    "mensagem": f"{seg.upper()} não aceita {L['nome']} pra esse perfil: {motivo}",
                })
                continue
            if cap_aplicado > cap_max:
                avisos.append({
                    "nivel": "aviso",
                    "linha_id": linha_id,
                    "seguradora": seg,
                    "mensagem": (
                        f"{seg.upper()} {L['nome']}: capital R$ {cap_aplicado:,} acima "
                        f"do limite automático (R$ {cap_max:,}). {motivo}"
                    ).replace(",", "."),
                })
    return avisos


def aplicar_clamps_no_grid(cliente: dict, grid: dict) -> dict:
    """Reaperta o capital_aplicado de cada seg pra ficar dentro do clamp
    real por idade/renda/profissão. Não toca capital_sugerido — só o
    capital_aplicado por seguradora.

    Recalcula `premio_estimado` quando o capital muda.

    Devolve o próprio grid mutado, por conveniência."""
    from automacao.recomendador import _premio_linha  # evita import circular no boot
    idade = grid["cliente"].get("idade") or 40
    renda = float(grid["cliente"].get("renda_mensal") or 0)
    profissao = str(cliente.get("profissao") or "")
    for L in grid.get("linhas", []):
        linha_id = L["id"]
        for seg in ("azos", "mag"):
            info = L.get(seg)
            if not info or not info.get("disponivel"):
                continue
            cap_aplicado = int(info.get("capital_aplicado") or 0)
            if cap_aplicado <= 0:
                continue
            if seg == "azos":
                cap_max, motivo = clamp_capital_azos(linha_id, idade, renda)
            else:
                cap_max, motivo = clamp_capital_mag(linha_id, idade, renda, profissao)
            # cap_max None = sem regra de clamp (capital fixo / não aplicável)
            if cap_max is None:
                continue
            if cap_max == 0:
                info["disponivel"] = False
                info["motivo_indisponivel"] = motivo
                info["premio_estimado"] = None
                continue
            if cap_aplicado > cap_max:
                info["capital_original"]   = cap_aplicado
                info["capital_aplicado"]   = int(cap_max)
                info["clamp_motivo"]       = motivo
                # Recalcula prêmio com capital clampado
                info["premio_estimado"]    = _premio_linha(info, int(cap_max), idade)
    return grid
