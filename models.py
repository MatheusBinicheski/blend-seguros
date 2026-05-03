from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

Seguradora = Literal["azos", "mag", "omint"]


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
class ResultadoFase1:
    seguradora: Seguradora
    ok: bool
    coberturas: list[Cobertura] = field(default_factory=list)
    erro: str | None = None
    session_id: str | None = None  # mantém sessão aberta para fase 2


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
