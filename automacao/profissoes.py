"""
Roteamento por profissão Blend Seguros.

Detecta a profissão do cliente (string livre) e aplica TEMPLATE com:
  - Linhas vetadas (acidente em altura, MAG só MQC, etc) — viram
    disponivel=False com motivo
  - Modelo MAG sugerido (VIDA TODA VD STOA, PCHV VD STOA, etc)
  - Limites de capital quando profissão restringe

Templates baseados nas Dicas de Underwriting MAG Ago/23 pg 4, 6-7 e nas
regras gerais Azos pg 1 (público alvo 18-65a 11m 29d).

Quando o cliente é "Administrador do Lar", "Estudante", "Aposentado",
"Pensionista" ou "Do Lar":
  - MAG Vida Toda: MORTE+INVALIDEZ máx R$200k
  - MAG Private: MORTE+INVALIDEZ máx R$400k
  - DIT MAG vedada (não tem renda comprovável)

Quando o cliente é Polícia/Aprendiz de piloto/Piloto de teste/
Busca e resgate/Combate a incêndios/Piloto monomotor:
  - MAG: somente MQC (morte), Classe 4
  - Outras linhas vetadas

Quando o cliente é piloto de helicóptero (asa rotativa) comercial regular:
  - MAG canal PCHV VD STOA
  - Somente Morte (MQC) e DG

Quando o cliente é piloto de linha aérea regular (asa fixa, GOL/TAM/AZUL):
  - MAG canal regular
  - MORTE até R$1MM, IPTA/IPA até R$1MM, IFPD até R$1MM, DG até R$1MM
  - DIT até R$20k/mês (piloto) ou R$3k/mês (tripulação)
  - DIH até R$9k/dia
"""
from __future__ import annotations

import re
from typing import Iterable


# ─────────────────────────────────────────────────────────────────────────────
# DETECÇÃO DE PROFISSÃO (regex sobre string livre digitada pelo LP)
# ─────────────────────────────────────────────────────────────────────────────
# Templates em ordem de prioridade (primeiro match vence).
TEMPLATES_PROFISSAO = [
    # ── Pilotos de teste, instrutores, polícia, bombeiros — só MQC ─────
    {
        "id": "monomotor_polo_resgate",
        "rotulo": "Polícia / Bombeiro / Piloto monomotor (MQC Classe 4)",
        "regex": (
            r"piloto\s+teste|piloto\s+monomotor|aprendiz\s+piloto|"
            r"polici(a|al)\s+militar|policia\s+militar|"
            r"bombeir(o|a)\s+civil|bombeir(o|a)\s+militar|"
            r"busca\s+e\s+resgate|combate\s+a?\s+inc[êe]ndio"
        ),
        "modelo_mag": None,  # nenhum modelo Vida Toda padrão; só MQC Classe 4
        "modelo_mag_obs": "MAG: somente MQC (Morte Qualquer Causa) com agravamento Classe 4. Demais coberturas vetadas.",
        "linhas_permitidas_mag": {"morte_tradicional", "morte_whole_life", "morte_term_life"},
        "linhas_vetadas_mag": "todas-exceto-morte",
        "azos_observacao": "AZOS preferencial pra invalidez/DG/cirurgias/DIH/RIT (não tem restrição classe).",
    },

    # ── Piloto helicóptero (asa rotativa) — PCHV ───────────────────────
    {
        "id": "piloto_helicoptero",
        "rotulo": "Piloto helicóptero (asa rotativa) — canal PCHV",
        "regex": r"piloto.*helic[óo]pter|piloto.*asa\s+rotativa|helic[óo]pter.*piloto",
        "modelo_mag": "PCHV VD STOA",
        "modelo_mag_obs": "MAG canal PCHV (Pilotos e Tripulantes de Helicóptero) — Morte e DG apenas. Classe 2.",
        "linhas_permitidas_mag": {
            "morte_tradicional", "morte_whole_life", "morte_term_life",
            "doencas_graves_dg13", "doencas_graves_dg30", "doencas_graves_vital_cancer",
        },
        "linhas_vetadas_mag_motivo": "Canal PCHV cobre apenas Morte (MQC) e Doenças Graves. {FONTE_MAG} pg 7.",
        "azos_observacao": "AZOS sem restrição específica pra heli — pode atender invalidez/cirurgias/DIH/RIT.",
    },

    # ── Piloto de linha aérea regular (asa fixa) ───────────────────────
    {
        "id": "piloto_asa_fixa",
        "rotulo": "Piloto de linha aérea regular (GOL/TAM/AZUL/LATAM)",
        "regex": (
            r"piloto.*linha\s+a[ée]rea|piloto.*comercial|piloto.*asa\s+fixa|"
            r"piloto.*civil|comandante.*avi[aã]o|"
            r"copiloto.*linha|tripula(nte|c[aã]o).*GOL|tripula(nte|c[aã]o).*Azul|"
            r"tripula(nte|c[aã]o).*LATAM|tripula(nte|c[aã]o).*TAM"
        ),
        "modelo_mag": "VIDA TODA VD STOA",
        "modelo_mag_obs": "MAG canal regular asa fixa: MORTE R$1MM, IPTA/IPA/IFPD R$1MM cada, DG R$1MM, DIT R$20k/mês piloto (R$3k tripulação), DIH R$9k/dia. Prêmio padrão.",
        "limites_extra_mag": {
            "morte_tradicional":    1_000_000,
            "morte_whole_life":     1_000_000,
            "morte_term_life":      1_000_000,
            "invalidez_permanente": 1_000_000,
            "invalidez_acidente":   1_000_000,
            "doencas_graves_dg13":  1_000_000,
            "doencas_graves_dg30":  1_000_000,
            # DIT é R$/dia: R$20k/mês ÷ 30 ≈ R$666/dia (piloto)
            "renda_incapacidade":   666,
            "internacao_hospitalar": 9_000,
        },
        "azos_observacao": "AZOS aceita piloto comercial sem restrição extra.",
    },

    # ── Administrador do Lar / Estudante / Aposentado ───────────────────
    {
        "id": "adm_lar_estudante",
        "rotulo": "Administrador do Lar / Estudante / Aposentado",
        "regex": (
            r"administrador.*lar|do\s+lar|dona\s+de\s+casa|"
            r"estudante|aposentad[oa]|pensionista|desempregad[oa]"
        ),
        "modelo_mag": "VIDA TODA VD STOA",
        "modelo_mag_obs": "MAG Vida Toda: MORTE+INVALIDEZ máx R$200k. Privet: R$400k. DIT vedada (sem renda comprovável). {FONTE_MAG} pg 4, 13.",
        "limites_extra_mag": {
            "morte_tradicional":    200_000,
            "morte_whole_life":     400_000,  # Private
            "morte_term_life":      400_000,
            "invalidez_permanente": 200_000,
            "invalidez_acidente":   200_000,
        },
        "linhas_vetadas_mag_dt": {"renda_incapacidade": "Vedada contratação de DIT — Administrador do Lar/Estudante/Aposentado não tem renda do trabalho. Dicas MAG pg 13."},
    },

    # ── Atleta profissional / jogador de futebol ───────────────────────
    {
        "id": "atleta_profissional",
        "rotulo": "Atleta profissional / Jogador de futebol",
        "regex": (
            r"atleta\s+profissional|jogador.*futebol|jogador.*basquete|"
            r"futebolista|jogador.*v[oô]lei|lutador.*MMA"
        ),
        "modelo_mag": "VIDA TODA VD STOA",
        "modelo_mag_obs": "MAG Atleta: Morte (MQC/MAC) e IPA/IPTA. Capital máx acumulado R$10MM. R$2-10MM exige Formulário Financeiro + Tele Entrevista Médica + exames completos. Beneficiário NÃO pode ser clube (na linha PRIVATE). Dicas MAG pg 12.",
        "limites_extra_mag": {
            "morte_tradicional":    10_000_000,
            "morte_whole_life":     10_000_000,
            "morte_term_life":      10_000_000,
            "invalidez_permanente": 10_000_000,
            "invalidez_acidente":   10_000_000,
        },
    },

    # ── Oficial do Exército ────────────────────────────────────────────
    {
        "id": "oficial_exercito",
        "rotulo": "Oficial do Exército / Marinha / Aeronáutica",
        "regex": (
            r"oficial.*ex[ée]rcito|oficial.*marinha|oficial.*aerona[uú]tica|"
            r"capit[aã]o.*ex[ée]rcito|major.*ex[ée]rcito|coronel.*ex[ée]rcito|"
            r"tenente\s+coronel|general"
        ),
        "modelo_mag": "VIDA TODA VD STOA",
        "modelo_mag_obs": "Oficial: usar AZOS como principal (sem agravamento) + MAG complementar pra DG/Cirurgias/SAF. Recomendação tática Stoa.",
        "azos_observacao": "AZOS é o principal pro Oficial (público alvo OK, sem restrição).",
    },

    # ── Médico / Dentista — habilita IPTA Estendida ────────────────────
    {
        "id": "medico_dentista",
        "rotulo": "Médico / Dentista — IPTA Majorada Estendida liberada",
        "regex": (
            r"m[ée]dic[oa]|dentista|cirurgi[aã]o\s+dentista|odont[oó]log[oa]|"
            r"cirurgi[aã]o(?!\s*dentista)"
        ),
        "modelo_mag": "VIDA TODA VD STOA",
        "modelo_mag_obs": "Médicos/dentistas têm IPTA Majorada Estendida AZOS (cervical, tóraco-lombo-sacro, indicadores) — proteção crítica pra cirurgia manual.",
        "azos_observacao": "AZOS oferece IPTA Maj Estendida exclusiva — ativar a linha 'ipta_majorada_estendida' por padrão.",
        "ativar_extra": {"ipta_majorada_estendida": True},
    },
]


def detectar_template(profissao: str) -> dict | None:
    """Retorna o primeiro template que casa com a profissão, ou None."""
    if not profissao or not isinstance(profissao, str):
        return None
    p = profissao.strip().lower()
    if not p:
        return None
    for tpl in TEMPLATES_PROFISSAO:
        if re.search(tpl["regex"], p, re.IGNORECASE):
            return tpl
    return None


# ─────────────────────────────────────────────────────────────────────────────
# APLICAÇÃO DO TEMPLATE NA GRID
# ─────────────────────────────────────────────────────────────────────────────
def aplicar_template_na_grid(cliente: dict, grid: dict) -> dict:
    """Recebe a grid e aplica restrições do template profissional.

    Mutações:
      - linhas vetadas MAG → mag.disponivel=False + motivo_indisponivel
      - limites extras (clamp) → capital_aplicado teto + clamp_motivo
      - linhas ativadas extras → ativo_default=True
      - grid['template_profissao'] = {id, rotulo, modelo_mag_obs, ...}
    """
    profissao = str(cliente.get("profissao") or "")
    tpl = detectar_template(profissao)
    if not tpl:
        grid["template_profissao"] = {
            "id": None,
            "rotulo": "Profissão genérica (sem template específico)",
            "modelo_mag": "VIDA TODA VD STOA",
            "modelo_mag_obs": "Cliente em perfil padrão. MAG canal VIDA TODA VD STOA.",
        }
        return grid

    # Importa _premio_linha pra recalcular prêmios após clamp
    from automacao.recomendador import _premio_linha
    idade = grid["cliente"].get("idade") or 40

    # Anota o template detectado no grid
    grid["template_profissao"] = {
        "id":             tpl["id"],
        "rotulo":         tpl["rotulo"],
        "modelo_mag":     tpl.get("modelo_mag"),
        "modelo_mag_obs": tpl.get("modelo_mag_obs"),
        "azos_obs":       tpl.get("azos_observacao"),
    }

    # Linhas permitidas MAG (whitelist)
    permitidas = tpl.get("linhas_permitidas_mag")
    vetadas    = tpl.get("linhas_vetadas_mag")
    if permitidas or vetadas == "todas-exceto-morte":
        for L in grid.get("linhas", []):
            mag = L.get("mag")
            if not mag or not mag.get("disponivel"):
                continue
            if permitidas and L["id"] not in permitidas:
                mag["disponivel"] = False
                mag["motivo_indisponivel"] = (
                    f"Profissão '{tpl['rotulo']}' restringe MAG. "
                    f"Linhas permitidas: {sorted(permitidas)}."
                )
                mag["premio_estimado"] = None
            elif vetadas == "todas-exceto-morte" and L.get("tipo") != "morte":
                mag["disponivel"] = False
                mag["motivo_indisponivel"] = (
                    f"Profissão '{tpl['rotulo']}': MAG só aceita MQC (morte) com Classe 4."
                )
                mag["premio_estimado"] = None

    # Vetos individuais por id
    for linha_id, motivo in (tpl.get("linhas_vetadas_mag_dt") or {}).items():
        for L in grid.get("linhas", []):
            if L["id"] != linha_id:
                continue
            mag = L.get("mag")
            if not mag:
                continue
            mag["disponivel"] = False
            mag["motivo_indisponivel"] = motivo
            mag["premio_estimado"] = None

    # Limites extras (clamp adicional por profissão)
    for linha_id, cap_max in (tpl.get("limites_extra_mag") or {}).items():
        for L in grid.get("linhas", []):
            if L["id"] != linha_id:
                continue
            mag = L.get("mag")
            if not mag or not mag.get("disponivel"):
                continue
            cap_atual = int(mag.get("capital_aplicado") or 0)
            if cap_atual > cap_max:
                mag["capital_original"] = cap_atual
                mag["capital_aplicado"] = int(cap_max)
                mag["clamp_motivo"] = f"Profissão '{tpl['rotulo']}' limita MAG a R$ {cap_max:,}".replace(",", ".")
                mag["premio_estimado"] = _premio_linha(mag, int(cap_max), idade)

    # Ativar linhas extras (ex: IPTA Estendida pra médico/dentista)
    for linha_id, ativo in (tpl.get("ativar_extra") or {}).items():
        for L in grid.get("linhas", []):
            if L["id"] != linha_id:
                continue
            if ativo:
                L["ativo_default"] = True
                L["template_ativou"] = tpl["id"]

    return grid
