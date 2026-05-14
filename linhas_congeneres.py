"""
Mapeamento de linhas congêneres entre seguradoras.

Cada linha representa uma "categoria conceitual" (ex: Morte por Qualquer Causa) e
indica qual cobertura específica de cada seguradora atende essa categoria, junto
com características chave (vitalícia, prêmio, resgate) e restrições por perfil.

Filtro atual: APENAS Morte por Qualquer Causa que sejam vitalícias, com prêmio
crescente (NÃO nivelado) e SEM possibilidade de resgate.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class CoberturaSeguradora:
    """Como uma seguradora oferece uma linha congênere."""
    seguradora: str
    nome_oficial: str                  # nome SUSEP exato
    nome_no_sistema: str               # nome como aparece na lista que extraímos
    descricao_curta: str               # 1 linha p/ UI
    fonte_cg_url: str                  # link p/ Condições Gerais oficiais
    susep: str | None = None           # processo SUSEP
    vitalicia: bool = True
    premio_crescente: bool = True      # True = sobe com idade; False = nivelado
    tem_resgate: bool = False
    capital_min: int = 50_000
    capital_max: int = 3_000_000
    idade_min: int = 18
    idade_max: int = 65                # idade máxima de aceitação (entrada)
    idade_saida: int | None = None     # idade em que a cobertura termina; None = sem limite
    restricoes_profissao: list[str] = field(default_factory=list)
    capital_reduzido_para: dict[str, int] = field(default_factory=dict)
    observacoes: str = ""
    # Configurações específicas pra automação Playwright sondar o preço
    comissao_config: str | None = None   # ex: "25%/200%" para OMINT IDEAL
    capital_ancora_padrao: int = 100_000  # capital usado na sondagem de preço


@dataclass
class LinhaCongenere:
    """Uma linha conceitual unificada entre as 3 seguradoras."""
    id: str
    nome: str                          # display name p/ UI (ex: "Morte por Qualquer Causa")
    descricao: str                     # explica o que é essa cobertura
    tipo: str                          # morte | invalidez | doenca_grave | etc
    criterios_filtro: list[str]        # lista de critérios que aplicamos (p/ tooltip "por que essas?")
    coberturas: dict[str, CoberturaSeguradora]  # chave = nome da seguradora


# ──────────────────────────────────────────────────────────────────────────────
# LINHA 1: Morte por Qualquer Causa — vitalícia, prêmio crescente, sem resgate
# ──────────────────────────────────────────────────────────────────────────────
MORTE_QUALQUER_CAUSA = LinhaCongenere(
    id="morte_qualquer_causa",
    nome="Morte por Qualquer Causa",
    descricao=(
        "Indenização aos beneficiários no falecimento do segurado, por qualquer causa "
        "(natural ou acidental). Filtramos só as opções vitalícias, com prêmio que sobe "
        "com a idade (sem nivelamento) e sem componente de resgate — modelo puro de risco."
    ),
    tipo="morte",
    criterios_filtro=[
        "Vitalícia (sem idade de saída)",
        "Prêmio crescente por faixa etária (sem nivelamento)",
        "Sem valor de resgate (sem provisão matemática acumulada)",
    ],
    coberturas={
        "azos": CoberturaSeguradora(
            seguradora="azos",
            nome_oficial="Morte (M)",
            nome_no_sistema="Seguro de vida",
            descricao_curta="Vitalícia renovável anualmente; reajuste etário + IPCA.",
            fonte_cg_url="https://files.azos.com.br/f/especialista-outubro-2025.pdf",
            susep="15414.604991/2023-12",
            vitalicia=True,
            premio_crescente=True,
            tem_resgate=False,
            capital_min=50_000,
            capital_max=5_000_000,
            idade_min=18,
            idade_max=65,
            idade_saida=None,  # info conflitante; CG nao limita explicitamente; canal corretor renova
            restricoes_profissao=[
                "Militares das forças armadas",
                "Pilotos profissionais",
                "Mergulhadores profissionais",
                "Trabalhadores de plataforma de petróleo",
            ],
            capital_reduzido_para={
                "militar": 100_000,
                "piloto": 100_000,
                "mergulhador": 100_000,
                "plataformista": 100_000,
            },
            observacoes=(
                "Canal corretor: capital até R$ 3 mi (R$ 5 mi para parceiros Azos+ desde out/2025). "
                "Acima de 60 anos: capital máximo R$ 500 mil em algumas tabelas. "
                "Carência: 0 dias para morte natural ou acidental; 2 anos para suicídio. "
                "Vigência individual de 5 anos com renovação automática."
            ),
            capital_ancora_padrao=100_000,
        ),
        "mag": CoberturaSeguradora(
            seguradora="mag",
            nome_oficial="Vida Inteira (CG 3082/3083)",
            nome_no_sistema="VIDA INTEIRA",
            descricao_curta="Vitalícia até a morte; reajuste por idade conforme tabela de fatores 1,0106→1,4964.",
            fonte_cg_url="https://magportaisinststgprd.blob.core.windows.net/magseguros/2025/03/3082-e-3083-Condicoes-Gerais-Vida-Inteira.pdf",
            susep="15414.604647/2025-87",
            vitalicia=True,
            premio_crescente=True,
            tem_resgate=False,
            capital_min=10_000,
            capital_max=3_000_000,
            idade_min=16,
            idade_max=85,
            idade_saida=None,
            restricoes_profissao=[],
            observacoes=(
                "Regime de repartição simples (item 1.1.1 da CG): não permite resgate ou "
                "devolução de prêmios. Vigência vitalícia (item 9.1 da CG). Reajuste por idade "
                "via tabela de fatores até 99 anos (item 13.3.2 da CG). "
                "Sem carência para morte por qualquer causa; 2 anos para suicídio."
            ),
            capital_ancora_padrao=100_000,
        ),
        "omint": CoberturaSeguradora(
            seguradora="omint",
            nome_oficial="OMINT IDEAL — Seguro de Vida Individual",
            nome_no_sistema="OMINT IDEAL - SEGURO DE VIDA INDIVIDUAL",
            descricao_curta="Termo renovável anualmente, sem resgate (regime de repartição simples).",
            fonte_cg_url="https://www.omint.com.br/wp-content/themes/OmintPortal360/assets/pdfs/CONDICOES_GERAIS_IDEAL_15414900334201747-2025.pdf",
            susep="15414.900334/2017-47",
            vitalicia=False,  # nao confirmado vitalicio publicamente; tratamos como termo renovavel
            premio_crescente=True,
            tem_resgate=False,
            capital_min=20_000,
            capital_max=1_000_000,
            idade_min=18,
            idade_max=70,
            idade_saida=None,
            restricoes_profissao=[
                "Avaliação caso-a-caso via DPSA (Declaração Pessoal de Saúde e Atividade)",
            ],
            observacoes=(
                "Produto de entrada da OMINT, sem componente de capitalização. Renovação anual "
                "com reajuste por faixa etária. Comissão configurada na cotação: 25% recorrente "
                "+ 200% no primeiro ano."
            ),
            comissao_config="25_200",
            capital_ancora_padrao=100_000,
        ),
    },
)


# ──────────────────────────────────────────────────────────────────────────────
# Lista de TODAS as linhas conceituais ativas
# ──────────────────────────────────────────────────────────────────────────────
LINHAS_ATIVAS: list[LinhaCongenere] = [
    MORTE_QUALQUER_CAUSA,
]


def por_id(linha_id: str) -> LinhaCongenere | None:
    for l in LINHAS_ATIVAS:
        if l.id == linha_id:
            return l
    return None


def cobertura_no_sistema(seguradora: str, linha_id: str) -> CoberturaSeguradora | None:
    """Retorna a configuração da cobertura na seguradora para uma linha conceitual."""
    linha = por_id(linha_id)
    if not linha:
        return None
    return linha.coberturas.get(seguradora)
