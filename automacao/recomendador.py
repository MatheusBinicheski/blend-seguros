"""
Motor de recomendação de coberturas com orçamento fixo de R$50/mês.
Seleciona o maior capital possível para cada tipo de cobertura.
"""
from datetime import date


def calcular_idade(nascimento: str) -> int:
    """Nascimento: DD/MM/AAAA"""
    try:
        d, m, a = nascimento.strip().split("/")
        nasc = date(int(a), int(m), int(d))
        hoje = date.today()
        return hoje.year - nasc.year - ((hoje.month, hoje.day) < (nasc.month, nasc.day))
    except Exception:
        return 40


# Taxas mensais (R$/mês por R$1.000 de capital segurado) — calibradas com dados reais Azos.
# Conservadoras propositalmente: o retry de prêmio em main.py ajusta para cima se necessário.
# Aumentam ~40% a cada 10 anos de idade acima de 35.
_TAXAS = {
    "morte_acidental":  0.050,
    "vida":             0.400,   # dado real: ~R$0.37/mês/R$1k → usa 0.40 para folga
    "doencas_graves":   0.300,   # dado real: ~R$0.27/mês/R$1k → usa 0.30 para folga
    "invalidez_acid":   0.070,
    "invalidez_perm":   0.110,
    "cirurgias":        0.180,
    "ref":              0.140,
    "rit":              0.120,
}


def recomendar(cliente: dict, coberturas_disponiveis: list[str],
               tipo_cobertura: str = "mix",
               apenas_padrao: bool = False) -> list[dict]:
    """
    Seleciona coberturas maximizando o capital segurado para orçamento fixo de R$50/mês.

    tipo_cobertura:
      "em_vida"    → doenças graves, invalidez, internação, cirurgias
      "apos_morte" → seguro de vida, morte acidental
      "mix"        → R$25/mês em cada categoria

    apenas_padrao: mantido por compatibilidade (legado), ignorado quando tipo_cobertura é passado.
    """

    BUDGET = 50.0  # R$/mês — sempre fixo

    idade = calcular_idade(cliente.get("nascimento", "01/01/1984"))

    # Ajuste por idade: +40% a cada 10 anos acima de 35
    fator_idade = 1.0 + max(0.0, (idade - 35) / 10.0) * 0.4

    def disponivel(nome_parcial: str) -> str | None:
        for nome in coberturas_disponiveis:
            if nome_parcial.lower() in nome.lower():
                return nome
        return None

    selecoes = []

    def add(nome_parcial: str, valor: float, motivo: str):
        nome = disponivel(nome_parcial)
        if nome:
            selecoes.append({"nome": nome, "valor": int(round(valor, -3)), "motivo": motivo})

    def capital(chave_taxa: str, budget: float) -> int:
        """Capital = budget / taxa_efetiva × 1000, clampado a [50k, 5M]."""
        taxa = _TAXAS[chave_taxa] * fator_idade
        c = budget / taxa * 1_000
        return max(50_000, min(5_000_000, int(round(c, -3))))

    # Distribuição do orçamento por tipo
    if tipo_cobertura == "em_vida":
        budget_vida  = BUDGET
        budget_morte = 0.0
    elif tipo_cobertura == "apos_morte":
        budget_vida  = 0.0
        budget_morte = BUDGET
    else:  # mix
        budget_vida  = BUDGET / 2
        budget_morte = BUDGET / 2

    # ── COBERTURAS EM VIDA ──────────────────────────────────────────────────
    # Ordem de prioridade: menor taxa primeiro (menor mínimo real no Azos)
    # Invalidez Acidente (rate 0.070) tem mínimo ~R$50k–100k no Azos
    # DG30 (rate 0.300) tem mínimo real R$300.000 → prêmio mínimo ~R$90/mês → inviável para R$50
    if budget_vida > 0:
        # Prioridade 1: Invalidez Total por Acidente (taxa mais barata em vida)
        if disponivel("Invalidez Total por Acidente"):
            cap = capital("invalidez_acid", budget_vida)
            add("Invalidez Total por Acidente (Majorada)", cap,
                f"Máx. cobertura por R${budget_vida:.0f}/mês — invalidez por acidente")

        # Prioridade 2: Invalidez Permanente Total
        elif disponivel("Invalidez Permanente"):
            cap = capital("invalidez_perm", budget_vida)
            add("Invalidez Permanente Total", cap,
                f"Máx. cobertura por R${budget_vida:.0f}/mês — invalidez permanente")

        # Prioridade 3: Doenças Graves 30 (mínimo real R$300k — último recurso)
        elif disponivel("Doenças Graves"):
            cap = capital("doencas_graves", budget_vida)
            add("Doenças Graves 30", cap,
                f"Máx. cobertura por R${budget_vida:.0f}/mês — 30 doenças graves cobertas")

    # ── COBERTURAS APÓS A MORTE ─────────────────────────────────────────────
    # Morte Acidental (rate 0.050) tem mínimo muito mais baixo que Seguro de Vida (mínimo R$1.2M)
    if budget_morte > 0:
        # Prioridade 1: Morte Acidental (taxa mais barata, menor mínimo real)
        if disponivel("Morte acidental"):
            cap = capital("morte_acidental", budget_morte)
            add("Morte acidental", cap,
                f"Máx. cobertura acidental por R${budget_morte:.0f}/mês")

        # Prioridade 2: Seguro de Vida (mínimo real R$1.200.000 — último recurso)
        elif disponivel("Seguro de vida"):
            cap = capital("vida", budget_morte)
            add("Seguro de vida", cap,
                f"Máx. proteção familiar por R${budget_morte:.0f}/mês")

    # Fallback: se nada foi selecionado, usa Morte Acidental padrão
    if not selecoes:
        add("Morte acidental", 400_000, "Cobertura padrão — R$400.000")

    return selecoes
