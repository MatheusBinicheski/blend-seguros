from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

Seguradora = Literal["azos", "mag"]


@dataclass
class Cobertura:
    id: str
    nome: str
    descricao: str
    valor_min: float
    valor_max: float
    premio_referencia: float   # prêmio estimado no valor médio (R$/mês)
    seguradora: Seguradora = "azos"


@dataclass
class SondagemPreco:
    """Resultado de sondagem de preço para uma linha (ex: Morte) em um capital âncora."""
    linha_id: str                       # ex: "morte_qualquer_causa"
    cobertura_nome: str                 # nome usado pra fazer a sondagem
    capital_sondado: float              # capital usado (ex: 100000)
    premio_mensal: float                # prêmio capturado
    preco_por_1000: float               # premio_mensal / (capital/1000)
    erro: str | None = None


@dataclass
class ResultadoFase1:
    seguradora: Seguradora
    ok: bool
    coberturas: list[Cobertura] = field(default_factory=list)
    erro: str | None = None
    session_id: str | None = None  # mantém sessão aberta para fase 2
    sondagens: list[SondagemPreco] = field(default_factory=list)  # preços-âncora por linha


@dataclass
class SelecaoBlend:
    seguradora: Seguradora
    cobertura_id: str
    cobertura_nome: str
    valor_capital: float


@dataclass
class ResultadoCotacao:
    seguradora: Seguradora
    cobertura_nome: str
    valor_capital: float
    premio_mensal: float
    link_proposta: str | None = None
    numero_proposta: str | None = None
    erro: str | None = None


@dataclass
class ResultadoFase2:
    ok: bool
    cotacoes: list[ResultadoCotacao] = field(default_factory=list)
    erro: str | None = None
