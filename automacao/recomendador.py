"""
Recomendador de coberturas do Blend.

Diferente do Guardian (que trava em R$ 50/mês), o Blend faz planejamento
personalizado: calcula capitais por cobertura tomando a renda do cliente como
referência. Multiplicadores praxe do mercado:
  - Seguro de vida (morte qualquer causa): 10x renda anual
  - Morte acidental: 5x renda anual
  - Doenças graves: 3x renda anual (cobre tratamento + afastamento)
  - Invalidez permanente total: 10x renda anual (substitui renda futura)
  - Invalidez por acidente (majorada): 8x renda anual

Sem teto de prêmio mensal — o cliente paga pelo capital que o perfil pede.
"""
from datetime import date


def calcular_idade(nascimento: str) -> int:
    try:
        d, m, a = nascimento.strip().split("/")
        nasc = date(int(a), int(m), int(d))
        hoje = date.today()
        return hoje.year - nasc.year - ((hoje.month, hoje.day) < (nasc.month, nasc.day))
    except Exception:
        return 40


# Multiplicador da renda anual para cada cobertura, sob critério padrão do mercado
# (renda anual = renda_mensal * 12)
_MULTIPLICADOR_ANOS_RENDA = {
    "vida":             10,   # Seguro de Vida (morte qualquer causa)
    "morte_acidental":   5,
    "doencas_graves":    3,
    "invalidez_perm":   10,
    "invalidez_acid":    8,
    "cirurgias":         2,
    "ref":               3,   # Renda por Acidente
    "rit":               2,   # Renda por Internação
}

# Fallback quando renda não informada: capital sugerido por categoria
_CAPITAL_FALLBACK = {
    "vida":             500_000,
    "morte_acidental":  300_000,
    "doencas_graves":   200_000,
    "invalidez_perm":   500_000,
    "invalidez_acid":   400_000,
}

# Capital mínimo razoável por categoria
_PISO_CAPITAL = {
    "vida":             100_000,
    "morte_acidental":  100_000,
    "doencas_graves":   100_000,
    "invalidez_perm":   100_000,
    "invalidez_acid":   100_000,
}

# Teto operacional praxe (Azos/MAG validam o ceiling em runtime)
_TETO_CAPITAL = {
    "vida":           3_000_000,
    "morte_acidental": 1_500_000,
    "doencas_graves":  800_000,
    "invalidez_perm": 1_000_000,
    "invalidez_acid": 1_000_000,
}


def _capital_por_renda(chave: str, renda_mensal: float, idade: int) -> int:
    """Capital sugerido com base em renda anual e ajuste de idade.

    A partir dos 50 anos, descontamos 4% por ano para refletir maturidade
    financeira (patrimônio acumulado).
    """
    if renda_mensal and renda_mensal > 0:
        anos = _MULTIPLICADOR_ANOS_RENDA.get(chave, 5)
        base = renda_mensal * 12 * anos
    else:
        base = _CAPITAL_FALLBACK.get(chave, 200_000)

    if idade > 50:
        base *= max(0.5, 1 - 0.04 * (idade - 50))

    base = max(_PISO_CAPITAL.get(chave, 50_000), base)
    base = min(_TETO_CAPITAL.get(chave, 5_000_000), base)
    return int(round(base / 10_000) * 10_000)


def recomendar(cliente: dict, coberturas_disponiveis: list[str],
               tipo_cobertura: str = "mix",
               apenas_padrao: bool = False) -> list[dict]:
    """
    Seleciona coberturas pelo perfil do cliente (sem teto de prêmio).

    tipo_cobertura define o foco:
      - "em_vida":    invalidez + doenças graves
      - "apos_morte": seguro de vida + morte acidental
      - "mix":        combinação das categorias acima
    """
    renda_mensal = float(cliente.get("renda_mensal") or 0)
    idade = calcular_idade(cliente.get("nascimento", "01/01/1985"))

    def disponivel(nome_parcial: str) -> str | None:
        alvo = nome_parcial.lower()
        for nome in coberturas_disponiveis:
            if alvo in nome.lower():
                return nome
        return None

    selecoes: list[dict] = []

    def add(nome_parcial: str, chave_taxa: str, motivo: str):
        nome = disponivel(nome_parcial)
        if not nome:
            return
        capital = _capital_por_renda(chave_taxa, renda_mensal, idade)
        selecoes.append({"nome": nome, "valor": capital, "motivo": motivo})

    referencia_anual = renda_mensal * 12 if renda_mensal > 0 else 0

    def _motivo(anos: int, descricao: str) -> str:
        if referencia_anual > 0:
            return f"{anos}× renda anual — {descricao}"
        return f"Capital padrão — {descricao}"

    quer_em_vida    = tipo_cobertura in ("em_vida", "mix")
    quer_apos_morte = tipo_cobertura in ("apos_morte", "mix")

    if quer_em_vida:
        add("Invalidez Total por Acidente", "invalidez_acid",
            _motivo(8, "Invalidez total por acidente"))
        add("Invalidez Permanente",         "invalidez_perm",
            _motivo(10, "Invalidez permanente (qualquer causa)"))
        add("Doenças Graves",               "doencas_graves",
            _motivo(3, "Cobre 30 doenças graves — tratamento + afastamento"))

    if quer_apos_morte:
        add("Seguro de vida",   "vida",
            _motivo(10, "Família mantém o padrão por 10 anos"))
        add("Morte acidental",  "morte_acidental",
            _motivo(5, "Indenização extra em morte acidental"))

    # Garante ao menos 1 cobertura
    if not selecoes:
        add("Seguro de vida",  "vida", _motivo(10, "Cobertura básica recomendada"))
        if not selecoes:
            add("Morte acidental", "morte_acidental", _motivo(5, "Cobertura básica"))

    return selecoes


def capital_recomendado_morte(cliente: dict) -> int:
    """Capital sugerido para Vida Inteira da MAG (10x renda anual)."""
    renda = float(cliente.get("renda_mensal") or 0)
    idade = calcular_idade(cliente.get("nascimento", "01/01/1985"))
    return _capital_por_renda("vida", renda, idade)
