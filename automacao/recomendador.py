"""
Recomendador / planejamento Blend.

Em vez de produtos isolados por seguradora, o planejamento gira em torno de
"linhas conceituais" (Morte qualquer causa, Morte acidental, Doenças Graves,
Cirurgias, Assistência Funeral etc.) — para cada linha, AZOS e MAG aparecem
lado a lado com prêmio estimado. O Life Planner edita o capital sugerido e
escolhe qual seguradora cobre cada linha para montar o blend final.

Catálogo calibrado pelo material "Montando um Blend v2" (Stoa, 2025/2026):
  - Morte: AZOS só tem Tradicional (TR1); MAG tem Term Life e Whole Life nivelados.
  - DG: AZOS DG13/DG30; MAG Plus (10d) / Premium (28d).
  - DIH: AZOS R$51,30/1k diária; MAG R$64,68/1k.
  - SAF MAG: 3 tiers com capital fixo (Essencial 5.5k / Plus 10k / Premium 15k).

Taxas (R$/mês por R$1.000 de capital) calibradas a partir das tabelas do PDF
para perfil-base (homem 33a, não fumante) e do fator idade (+40% a cada 10
anos acima de 35) — substituídas pelos prêmios reais quando a pré-simulação
Playwright for executada.
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


def _capital_por_anos_renda(anos: int, renda_mensal: float, idade: int,
                            piso: int = 50_000, teto: int = 3_000_000) -> int:
    if renda_mensal and renda_mensal > 0:
        base = renda_mensal * 12 * anos
    else:
        base = piso * 4  # fallback razoável quando renda não vem
    if idade > 50:
        base *= max(0.5, 1 - 0.04 * (idade - 50))
    base = max(piso, min(teto, base))
    return int(round(base / 10_000) * 10_000)


def _fator_idade(idade: int) -> float:
    """+40% a cada 10 anos acima de 35; +0% nos 35 ou abaixo."""
    return 1.0 + max(0.0, (idade - 35) / 10.0) * 0.4


def _premio_por_taxa(taxa: float, capital: int, idade: int) -> float:
    return round((capital / 1000.0) * taxa * _fator_idade(idade), 2)


def _premio_linha(seg_info: dict, capital: int, idade: int) -> float | None:
    """Calcula prêmio mensal estimado da linha em uma seguradora.

    modelo_preco:
      - "fixo"        → usa premio_fixo (independe do capital)
      - "taxa"        → R$/mês por R$1k de capital × fator idade
      - "por_unidade" → R$/mês por R$1 de capital (usado em DIH e RIT,
                         onde "capital" é R$/dia ou R$/mês de renda)
    """
    if not seg_info or not seg_info.get("disponivel"):
        return None
    modelo = seg_info.get("modelo_preco", "taxa")
    if modelo == "fixo":
        return float(seg_info.get("premio_fixo") or 0)
    if modelo == "taxa":
        taxa = float(seg_info.get("taxa") or 0)
        return _premio_por_taxa(taxa, capital, idade)
    if modelo == "por_unidade":
        taxa = float(seg_info.get("taxa") or 0)
        return round(capital * taxa * _fator_idade(idade), 2)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Catálogo de LINHAS comparáveis entre AZOS e MAG
#
# Para cada linha:
#   anos_renda    → multiplicador da renda anual para capital sugerido
#   capital_min/max → limites usados pelo input do LP (pode ser sobrescrito por seguradora)
#   modalidade da seguradora:
#       "tradicional" → TR1, prêmio reajusta com idade
#       "term_life"   → prêmio nivelado por prazo (10/15/20/30 anos), termina vigência
#       "whole_life"  → vitalício com prêmio nivelado (fixo a vida toda)
#       "pacote_fixo" → produto com capital + prêmio fixos (ex: SAF MAG)
#   azos/mag.modelo_preco:
#       "taxa"        → taxa R$/R$1k/mês (calibrada com observações reais)
#       "fixo"        → produto com capital/prêmio fixo
#       "por_unidade" → taxa direta sobre o capital (DIH/RIT — R$/dia ou R$/mês)
#   azos/mag.capital_fixo → quando presente, força o capital aplicado para esse valor
#       (UI esconde o slider/input). Usado em SAF MAG e Funeral AZOS.
#   azos/mag.fonte → "calibrada" (observada em cotação real ou PDF v2) ou "estimada"
# ──────────────────────────────────────────────────────────────────────────────
_LINHAS_COMPARATIVAS: list[dict] = [
    # ──────────────────────────────────────────────────────────────────────
    # MORTE — 3 modalidades. AZOS só tem Tradicional (TR1); Whole Life e
    # Term Life nivelados são MAG (também há no mercado: Icatu, Omint,
    # Centauro, MetLife, Prudential — mas só MAG está cotável aqui).
    # ──────────────────────────────────────────────────────────────────────

    # ── MORTE — TRADICIONAL (TR1, reajuste etário) ──────────────────────
    {
        "id": "morte_tradicional",
        "nome": "Morte — Tradicional (TR1, reajuste etário)",
        "tipo": "morte",
        "modalidade": "tradicional",
        "grupo_exclusivo": "morte",
        "grupo_titulo": "Cobertura de Morte — escolha 1 modalidade por seguradora",
        "descricao": "Vitalício com renovação anual e reajuste por idade. Custo inicial menor, mas sobe com o tempo. Hoje a melhor tabela de TR1 é da AZOS.",
        "anos_renda": 10,
        "capital_min": 50_000, "capital_max": 3_000_000,
        "azos": {
            "disponivel": True,
            "produto": "Especialista · Morte (M)",
            "nome_no_portal": "Seguro de vida",
            "susep": "15414.604991/2023-12",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.17,
            "min": 50_000, "max": 3_000_000,
            "fonte": "calibrada",
            "obs": "Taxa ano 1 (~R$104,67/MM p/ 33a no PDF v2; R$199/1,2MM p/ 36a na cotação real). Reajusta com idade.",
        },
        "mag": {
            "disponivel": False,
            "obs": "MAG tem Vida Inteira tradicional, mas no canal corretor prioriza Whole Life e Term Life nivelados.",
        },
    },

    # ── MORTE — TERM LIFE (prêmio nivelado por prazo) ─────────────────────
    {
        "id": "morte_term_life",
        "nome": "Morte — Term Life (nivelado por prazo 10/15/20/30 anos)",
        "tipo": "morte",
        "modalidade": "term_life",
        "grupo_exclusivo": "morte",
        "descricao": "Prêmio fixo por prazo definido. Termina a vigência ao fim do prazo. Maior previsibilidade para o cliente. MAG permite saldamento e mudança para vitalício.",
        "anos_renda": 10,
        "capital_min": 100_000, "capital_max": 10_000_000,
        "azos": {
            "disponivel": False,
            "obs": "AZOS não oferece Term Life nivelado — só Tradicional (TR1, reajuste etário).",
        },
        "mag": {
            "disponivel": True,
            "produto": "Private Solutions · Term Life",
            "nome_no_portal": "TERM LIFE",
            "modalidade": "term_life",
            "modelo_preco": "taxa", "taxa": 0.18,
            "min": 100_000, "max": 10_000_000,
            "fonte": "calibrada",
            "obs": "R$183,06/MM p/ homem 33a, 20 anos (PDF v2). Muito competitivo p/ sexo feminino. Saldamento mesmo sendo Term Life. Permite mudança p/ vitalício.",
        },
    },

    # ── MORTE — WHOLE LIFE (vitalício prêmio nivelado) ────────────────────
    {
        "id": "morte_whole_life",
        "nome": "Morte — Whole Life (vitalício com prêmio nivelado)",
        "tipo": "morte",
        "modalidade": "whole_life",
        "grupo_exclusivo": "morte",
        "descricao": "Vitalício com prêmio fixo a vida toda. Possibilidade de quitação e formação de reserva. Ideal para sucessão patrimonial e clientes de alta renda.",
        "anos_renda": 10,
        "capital_min": 1_000_000, "capital_max": 25_000_000,
        "azos": {
            "disponivel": False,
            "obs": "AZOS não oferece Whole Life nivelado. Para vitalício na AZOS só Tradicional (TR1).",
        },
        "mag": {
            "disponivel": True,
            "produto": "Private Solutions · Whole Life Sucessão (3108-3113)",
            "nome_no_portal": "WHOLE LIFE SUCESSAO",
            "susep": "15414.901244/2024",
            "modalidade": "whole_life",
            "modelo_preco": "taxa", "taxa": 0.55,
            "min": 1_000_000, "max": 25_000_000,
            "fonte": "estimada",
            "obs": "Vitalício, prêmio nivelado fixo. Capital R$1MM-25MM. Idade 25-70.",
        },
    },

    # ── MORTE ACIDENTAL ────────────────────────────────────────────────────
    {
        "id": "morte_acidental",
        "nome": "Morte Acidental",
        "tipo": "morte",
        "descricao": "Indenização extra quando a morte é decorrente de acidente pessoal coberto.",
        "anos_renda": 5,
        "capital_min": 50_000, "capital_max": 1_500_000,
        "azos": {
            "disponivel": True,
            "produto": "Morte Acidental (MAC)",
            "nome_no_portal": "Morte acidental",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.05,
            "min": 50_000, "max": 1_500_000,
            "fonte": "calibrada",
        },
        "mag": {
            "disponivel": False,
            "obs": "Componente embutido nos pacotes SAF MAG — não comparável isoladamente.",
        },
    },

    # ── INVALIDEZ PERMANENTE TOTAL (IPT — qualquer causa) ────────────────
    {
        "id": "invalidez_permanente",
        "nome": "Invalidez Permanente Total (IPT)",
        "tipo": "invalidez",
        "descricao": "Indenização em caso de invalidez permanente total por qualquer causa (doença ou acidente). AZOS e MAG são as poucas que vendem avulso.",
        "anos_renda": 10,
        "capital_min": 100_000, "capital_max": 1_000_000,
        "azos": {
            "disponivel": True,
            "produto": "Invalidez Permanente Total (IPT)",
            "nome_no_portal": "Invalidez Permanente",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.070,
            "min": 100_000, "max": 1_000_000,
            "fonte": "calibrada",
            "obs": "R$70,44/MM p/ 33a no PDF v2. AZOS vende avulso. Abrangência OK. IPTA Majorada disponível separada.",
        },
        "mag": {
            "disponivel": True,
            "produto": "Invalidez (Private Solutions)",
            "nome_no_portal": "INVALIDEZ",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.076,
            "min": 100_000, "max": 1_000_000,
            "fonte": "calibrada",
            "obs": "R$76,05/MM p/ 33a no PDF v2. MAG vende avulso. Boa abrangência. Majoração já inclusa. Idade 16-65, vitalício. Abdica do 769.",
        },
    },

    # ── INVALIDEZ TOTAL POR ACIDENTE (IPTA Majorada) ─────────────────────
    {
        "id": "invalidez_acidente",
        "nome": "Invalidez Total por Acidente (IPTA Majorada)",
        "tipo": "invalidez",
        "descricao": "Indenização majorada quando a invalidez total é por acidente pessoal. AZOS oferece a versão Majorada.",
        "anos_renda": 8,
        "capital_min": 100_000, "capital_max": 1_000_000,
        "azos": {
            "disponivel": True,
            "produto": "IPTA Majorada",
            "nome_no_portal": "Invalidez Total por Acidente",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.07,
            "min": 100_000, "max": 1_000_000,
            "fonte": "calibrada",
        },
        "mag": {
            "disponivel": False,
            "obs": "Majoração já inclusa na cobertura de Invalidez MAG — não isolável.",
        },
    },

    # ──────────────────────────────────────────────────────────────────────
    # DOENÇAS GRAVES — 2 linhas (grupo_exclusivo "doencas_graves"):
    #   - Básico (~13 doenças): AZOS DG13 + MAG Plus (10d)
    #   - Completo (~30 doenças): AZOS DG30 + MAG Premium (28d)
    # ──────────────────────────────────────────────────────────────────────

    # ── DG — BÁSICO 13 ─────────────────────────────────────────────────────
    {
        "id": "doencas_graves_dg13",
        "nome": "Doenças Graves — Básico (~13 doenças)",
        "tipo": "doenca",
        "grupo_exclusivo": "doencas_graves",
        "grupo_titulo": "Doenças Graves — escolha o nível de cobertura por seguradora",
        "descricao": "Cobertura básica (~13 doenças: câncer, AVC, infarto, transplantes etc). Capital pago em vida no diagnóstico.",
        "anos_renda": 3,
        "capital_min": 100_000, "capital_max": 800_000,
        "azos": {
            "disponivel": True,
            "produto": "Doenças Graves 13 (DG13)",
            "nome_no_portal": "Doenças Graves",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.308,
            "min": 100_000, "max": 800_000,
            "fonte": "calibrada",
            "obs": "R$153,95/500k p/ 33a no PDF v2. 13 doenças. Vende avulso. Reenquadramento anual. Sem vínculo com MQC, até R$1MM.",
        },
        "mag": {
            "disponivel": True,
            "produto": "Doenças Graves Plus (10 doenças)",
            "nome_no_portal": "DOENÇAS GRAVES PLUS",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.332,
            "min": 100_000, "max": 500_000,
            "fonte": "calibrada",
            "obs": "R$166,18/500k p/ 33a no PDF v2. 10 doenças. Reenquadramento a cada 5 anos (idade final 1 e 6). Câncer LMG sim. Sem vínculo MQC.",
        },
    },

    # ── DG — COMPLETO 30 ───────────────────────────────────────────────────
    {
        "id": "doencas_graves_dg30",
        "nome": "Doenças Graves — Completo (28-30 doenças)",
        "tipo": "doenca",
        "grupo_exclusivo": "doencas_graves",
        "descricao": "Cobertura ampliada com 28-30 doenças. Versão mais completa, indicada quando o cliente tem antecedentes ou idade > 40.",
        "anos_renda": 3,
        "capital_min": 100_000, "capital_max": 800_000,
        "azos": {
            "disponivel": True,
            "produto": "Doenças Graves 30 (DG30)",
            "nome_no_portal": "Doenças Graves",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.351,
            "min": 100_000, "max": 800_000,
            "fonte": "calibrada",
            "obs": "R$175,60/500k p/ 33a no PDF v2. 30 doenças. Versão mais completa AZOS. Reenquadramento anual.",
        },
        "mag": {
            "disponivel": True,
            "produto": "Doenças Graves Premium (28 doenças)",
            "nome_no_portal": "DOENÇAS GRAVES PREMIUM",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.590,
            "min": 100_000, "max": 500_000,
            "fonte": "calibrada",
            "obs": "R$295,17/500k p/ 33a no PDF v2. 28 doenças. Reenquadramento a cada 5 anos. Sem vínculo com MQC, até R$1MM. Câncer LMG sim (30%/50%/100%).",
        },
    },

    # ── CIRURGIAS ──────────────────────────────────────────────────────────
    {
        "id": "cirurgias",
        "nome": "Cirurgias",
        "tipo": "saude",
        "descricao": "Indenização por procedimentos cirúrgicos listados (TUSS). AZOS Cirurgia 2.0 cobre até R$100k (limite maior do mercado AZOS).",
        "anos_renda": 2,
        "capital_min": 50_000, "capital_max": 200_000,
        "azos": {
            "disponivel": True,
            "produto": "Cirurgias 2.0 (C2.0)",
            "nome_no_portal": "Cirurgia",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.18,
            "min": 50_000, "max": 100_000,
            "fonte": "estimada",
            "obs": "652 cirurgias cobertas. Capital até R$100k (Cirurgia 2.0 dobra o limite). Indeniza 10/20/50/100% do CS.",
        },
        "mag": {
            "disponivel": True,
            "produto": "Cirurgias + Amparo (3511)",
            "nome_no_portal": "CIRURGIAS + AMPARO (3511)",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.25,
            "min": 50_000, "max": 200_000,
            "fonte": "estimada",
            "obs": "917 cirurgias + amparo financeiro durante recuperação.",
        },
    },

    # ── QUEBRA DE OSSOS (REF AZOS) ─────────────────────────────────────────
    {
        "id": "quebra_ossos",
        "nome": "Quebra de Ossos (Rupturas e Fraturas)",
        "tipo": "acidente",
        "modalidade": "tradicional",
        "descricao": "Indenização por fraturas ósseas e rupturas de tendões/ligamentos por acidente. Cobertura nova AZOS (REF, lançada em 2025).",
        "anos_renda": 0,
        "capital_min": 5_000, "capital_max": 50_000,
        "capital_padrao": 15_000,
        "unidade": "Capital (R$)",
        "azos": {
            "disponivel": True,
            "produto": "Rupturas e Fraturas (REF)",
            "nome_no_portal": "Rupturas",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 1.20,
            "min": 5_000, "max": 50_000,
            "fonte": "estimada",
            "obs": "Cobertura AZOS (out/2025). Fraturas ósseas, rupturas de tendões e ligamentos.",
        },
        "mag": {
            "disponivel": False,
            "obs": "MAG não tem cobertura específica de Rupturas e Fraturas. No mercado: Prudential Cirurgias Ampliadas (33 ossos) e Omint Quebra de Ossos (51 ossos).",
        },
    },

    # ── DIH (Diária de Internação Hospitalar) ──────────────────────────────
    {
        "id": "internacao_hospitalar",
        "nome": "Diária de Internação Hospitalar (DIH)",
        "tipo": "hospitalar",
        "descricao": "Indenização diária enquanto internado. Triplica em UTI. AZOS vende avulso (raro no mercado).",
        "anos_renda": 0,
        "capital_min": 100, "capital_max": 1_000,
        "capital_padrao": 300,
        "unidade": "R$/dia",
        "azos": {
            "disponivel": True,
            "produto": "DIH AZOS",
            "nome_no_portal": "Internação",
            "modalidade": "tradicional",
            "modelo_preco": "por_unidade", "taxa": 0.0513,
            "min": 100, "max": 1_000,
            "fonte": "calibrada",
            "obs": "R$51,30 p/ R$1k de diária (PDF v2). 200 diárias por evento. Franquia 72h. Triplica em UTI. Vende avulso. Reajuste anual.",
        },
        "mag": {
            "disponivel": True,
            "produto": "DIH MAG (150/200/250 diárias)",
            "nome_no_portal": "DIH",
            "modalidade": "tradicional",
            "modelo_preco": "por_unidade", "taxa": 0.0647,
            "min": 100, "max": 1_000,
            "fonte": "calibrada",
            "obs": "R$64,68 p/ R$1k (PDF v2). Não vende avulso (vinculado à Morte). Franquia 4 dias. Triplica em UTI. 150, 200 ou 250 diárias por evento por ano.",
        },
    },

    # ── DIT / RIT (Renda por Incapacidade Temporária) ───────────────────────
    {
        "id": "renda_incapacidade",
        "nome": "Renda por Incapacidade Temporária (RIT / DIT)",
        "tipo": "renda",
        "descricao": "Renda mensal enquanto afastado por doença ou acidente. AZOS chama de RIT, MAG/mercado chama de DIT. AZOS é a vencedora desta categoria no PDF v2.",
        "anos_renda": 0,
        "capital_min": 1_000, "capital_max": 30_000,
        "capital_padrao_por_renda": 0.6,
        "unidade": "R$/mês de renda",
        "azos": {
            "disponivel": True,
            "produto": "RIT (Renda por Incapacidade Temporária)",
            "nome_no_portal": "Renda por Incapacidade",
            "modalidade": "tradicional",
            "modelo_preco": "por_unidade", "taxa": 0.025,
            "min": 1_000, "max": 30_000,
            "fonte": "estimada",
            "obs": "Limite 730 dias (mercado: 365). Hérnia de disco sim. Doenças por vetores sim. Cobertura mesmo inadimplente sim. Abrangência global. Vencedora da categoria no PDF v2.",
        },
        "mag": {
            "disponivel": True,
            "produto": "DIT (DIT+MAC+IPAM 10 dias ou DIT+MQC 10 dias)",
            "nome_no_portal": "DIT",
            "modalidade": "tradicional",
            "modelo_preco": "por_unidade", "taxa": 0.030,
            "min": 1_000, "max": 30_000,
            "fonte": "estimada",
            "obs": "Franquia 10 dias doenças, 7 dias reduzida. Limite 365. Hérnia/LMG sim. Sem cobertura mesmo inadimplente.",
        },
    },

    # ──────────────────────────────────────────────────────────────────────
    # FUNERAL / SAF — capital fixo por tier.
    # AZOS Funeral Individual ou Familiar = R$15.000.
    # MAG SAF: 3 tiers em grupo_exclusivo "saf"
    #   - Essencial R$5.500 / Plus R$10.000 / Premium R$15.000
    # ──────────────────────────────────────────────────────────────────────

    # ── FUNERAL AZOS (capital fixo) ────────────────────────────────────────
    {
        "id": "funeral_azos",
        "nome": "Assistência Funeral AZOS (capital fixo R$15.000)",
        "tipo": "assistencia",
        "modalidade": "pacote_fixo",
        "descricao": "Cobertura funerária AZOS — Individual (titular) ou Familiar (titular + cônjuge + filhos). Capital fixo R$15.000. Sem carência.",
        "anos_renda": 0,
        "capital_min": 15_000, "capital_max": 15_000,
        "capital_padrao": 15_000,
        "unidade": "Capital fixo R$",
        "azos": {
            "disponivel": True,
            "produto": "Assistência Funeral (Individual ou Familiar)",
            "nome_no_portal": "Funeral",
            "modalidade": "pacote_fixo",
            "modelo_preco": "fixo", "premio_fixo": 14.90,
            "capital_fixo": 15_000,
            "min": 15_000, "max": 15_000,
            "fonte": "estimada",
            "obs": "Capital fixo R$15.000. Sem carência. Reembolso de despesas até o limite.",
        },
        "mag": {
            "disponivel": False,
            "obs": "Equivalente MAG: SAF Premium (R$15.000) — também capital fixo.",
        },
    },

    # ── SAF ESSENCIAL MAG (R$5.500) ────────────────────────────────────────
    {
        "id": "saf_essencial",
        "nome": "SAF Essencial MAG (R$5.500, familiar + pais e sogros)",
        "tipo": "assistencia",
        "modalidade": "pacote_fixo",
        "grupo_exclusivo": "saf",
        "grupo_titulo": "SAF MAG — escolha 1 tier (capital fixo por tier)",
        "descricao": "Pacote MAG com capital fixo R$5.500. Titular + cônjuge + filhos + pais e sogros. Cross-sell após 1ª venda MAG.",
        "anos_renda": 0,
        "capital_min": 5_500, "capital_max": 5_500,
        "capital_padrao": 5_500,
        "unidade": "Capital fixo R$",
        "azos": {
            "disponivel": False,
            "obs": "Equivalente AZOS: Assistência Funeral Familiar (R$15.000).",
        },
        "mag": {
            "disponivel": True,
            "produto": "SAF Essencial Familiar + Pais e Sogros (3061)",
            "nome_no_portal": "SAF ESSENCIAL FAMILIAR + PAIS E SOGROS (3061)",
            "modalidade": "pacote_fixo",
            "modelo_preco": "fixo",
            "premio_fixo": 28.41,
            "capital_fixo": 5_500,
            "min": 5_500, "max": 5_500,
            "fonte": "calibrada",
            "obs": "Capital fixo R$5.500 (PDF v2). Idade entrada 16-95. Sem carência. Translado nacional.",
        },
    },

    # ── SAF PLUS MAG (R$10.000) ────────────────────────────────────────────
    {
        "id": "saf_plus",
        "nome": "SAF Plus MAG (R$10.000, familiar)",
        "tipo": "assistencia",
        "modalidade": "pacote_fixo",
        "grupo_exclusivo": "saf",
        "descricao": "Pacote MAG com capital fixo R$10.000. Translado América Latina.",
        "anos_renda": 0,
        "capital_min": 10_000, "capital_max": 10_000,
        "capital_padrao": 10_000,
        "unidade": "Capital fixo R$",
        "azos": {
            "disponivel": False,
            "obs": "Equivalente AZOS: Assistência Funeral Familiar (R$15.000).",
        },
        "mag": {
            "disponivel": True,
            "produto": "SAF Plus",
            "nome_no_portal": "SAF PLUS",
            "modalidade": "pacote_fixo",
            "modelo_preco": "fixo",
            "premio_fixo": 42.00,
            "capital_fixo": 10_000,
            "min": 10_000, "max": 10_000,
            "fonte": "estimada",
            "obs": "Capital fixo R$10.000 (PDF v2). Translado América Latina. Sem carência.",
        },
    },

    # ── SAF PREMIUM MAG (R$15.000) ─────────────────────────────────────────
    {
        "id": "saf_premium",
        "nome": "SAF Premium MAG (R$15.000, familiar + pet)",
        "tipo": "assistencia",
        "modalidade": "pacote_fixo",
        "grupo_exclusivo": "saf",
        "descricao": "Pacote MAG com capital fixo R$15.000. Translado internacional ilimitado + funeral pet.",
        "anos_renda": 0,
        "capital_min": 15_000, "capital_max": 15_000,
        "capital_padrao": 15_000,
        "unidade": "Capital fixo R$",
        "azos": {
            "disponivel": False,
            "obs": "Equivalente AZOS: Assistência Funeral Familiar (R$15.000) — mas sem pet/internacional.",
        },
        "mag": {
            "disponivel": True,
            "produto": "SAF Premium",
            "nome_no_portal": "SAF PREMIUM",
            "modalidade": "pacote_fixo",
            "modelo_preco": "fixo",
            "premio_fixo": 58.00,
            "capital_fixo": 15_000,
            "min": 15_000, "max": 15_000,
            "fonte": "estimada",
            "obs": "Capital fixo R$15.000 (PDF v2). Translado internacional ilimitado. Funeral pet. Sem carência.",
        },
    },
]


def planejamento_grid(cliente: dict, tipo_cobertura: str = "mix") -> dict:
    """
    Devolve o grid comparativo AZOS × MAG por linha conceitual.

    Para cada linha o frontend mostra:
      - Capital sugerido (editável, dentro de min/max)
      - Prêmio estimado AZOS (calibrado por taxa × capital × fator idade)
      - Prêmio estimado MAG (idem; ou prêmio fixo se for pacote)
      - Escolha do LP: qual seguradora vai cobrir essa linha (default: a mais
        barata; ou só uma quando a outra não atende)
    """
    renda = float(cliente.get("renda_mensal") or 0)
    idade = calcular_idade(cliente.get("nascimento", "01/01/1985"))

    quer_em_vida    = tipo_cobertura in ("em_vida", "mix")
    quer_apos_morte = tipo_cobertura in ("apos_morte", "mix")

    def _e_morte(tp):       return tp == "morte"
    def _e_em_vida(tp):     return tp in ("invalidez", "doenca", "saude",
                                          "hospitalar", "renda")
    def _e_assistencia(tp): return tp == "assistencia"

    linhas = []
    for L in _LINHAS_COMPARATIVAS:
        # Capital sugerido
        if L.get("anos_renda", 0) > 0:
            cap = _capital_por_anos_renda(
                L["anos_renda"], renda, idade,
                piso=L["capital_min"], teto=L["capital_max"],
            )
        elif L.get("capital_padrao_por_renda") and renda > 0:
            cap = int(round(renda * L["capital_padrao_por_renda"] / 100) * 100)
            cap = max(L["capital_min"], min(L["capital_max"], cap))
        elif L.get("capital_padrao"):
            cap = int(L["capital_padrao"])
        else:
            mid = (L["capital_min"] + L["capital_max"]) // 2
            cap = mid

        azos = dict(L.get("azos") or {})
        mag  = dict(L.get("mag")  or {})

        # Clampa o capital aos limites de cada seguradora individualmente
        cap_azos = cap
        cap_mag  = cap
        if azos.get("disponivel"):
            cap_azos = max(int(azos.get("min") or L["capital_min"]),
                           min(int(azos.get("max") or L["capital_max"]), cap))
            if azos.get("capital_fixo"):
                cap_azos = int(azos["capital_fixo"])
            azos["capital_aplicado"] = cap_azos
            azos["premio_estimado"]  = _premio_linha(azos, cap_azos, idade)
        if mag.get("disponivel"):
            cap_mag = max(int(mag.get("min") or L["capital_min"]),
                          min(int(mag.get("max") or L["capital_max"]), cap))
            if mag.get("capital_fixo"):
                cap_mag = int(mag["capital_fixo"])
            mag["capital_aplicado"] = cap_mag
            mag["premio_estimado"]  = _premio_linha(mag, cap_mag, idade)

        # Default escolhido:
        #   - se só uma das duas está disponível, é ela
        #   - se ambas estão disponíveis com capitais COMPATÍVEIS (mesma ordem
        #     de grandeza), prefere a mais barata
        #   - se uma tem capital fixo muito menor que a outra (ex: MAG SAF R$ 5.500
        #     vs AZOS R$ 1.200.000), são produtos diferentes — prefere a que
        #     cobre o capital sugerido pela renda (AZOS), e o LP decide
        p_a = azos.get("premio_estimado") if azos.get("disponivel") else None
        p_m = mag.get("premio_estimado")  if mag.get("disponivel")  else None
        if p_a is None and p_m is None:
            escolhido = None
        elif p_a is None:
            escolhido = "mag"
        elif p_m is None:
            escolhido = "azos"
        else:
            # Compatibilidade: razão entre capitais não passa de 4x
            cap_aplicado_azos = int(azos.get("capital_aplicado") or 0)
            cap_aplicado_mag  = int(mag.get("capital_aplicado")  or 0)
            razao = max(cap_aplicado_azos, cap_aplicado_mag) / max(
                1, min(cap_aplicado_azos, cap_aplicado_mag)
            )
            if razao > 4:
                # Produtos não comparáveis — prefere o que cobre o capital sugerido
                escolhido = "azos" if cap_aplicado_azos >= cap_aplicado_mag else "mag"
            else:
                escolhido = "azos" if p_a <= p_m else "mag"

        # Flag "ativo" segundo o tipo_cobertura escolhido
        tipo = L.get("tipo")
        if tipo == "morte":
            ativo_default = quer_apos_morte
        elif tipo in ("invalidez", "doenca", "saude", "hospitalar", "renda"):
            ativo_default = quer_em_vida
        else:  # assistencia / funeral
            ativo_default = True

        linhas.append({
            "id":             L["id"],
            "nome":           L["nome"],
            "tipo":           tipo,
            "modalidade":     L.get("modalidade"),
            "grupo_exclusivo": L.get("grupo_exclusivo"),
            "grupo_titulo":   L.get("grupo_titulo"),
            "descricao":      L["descricao"],
            "unidade":        L.get("unidade") or "Capital (R$)",
            "capital_sugerido": cap,
            "capital_min":    L["capital_min"],
            "capital_max":    L["capital_max"],
            "ativo_default":  ativo_default,
            "escolhido_default": escolhido,
            "azos":           azos,
            "mag":            mag,
        })

    return {
        "cliente": {
            "nome":           cliente.get("nome", ""),
            "idade":          idade,
            "renda_mensal":   renda,
            "tipo_cobertura": tipo_cobertura,
        },
        "linhas": linhas,
    }


# Mantido por compat com código que ainda usa este import
def capital_recomendado_morte(cliente: dict) -> int:
    """Capital sugerido para SAF MAG (capital fixo do produto)."""
    return 5_500


def recomendar(cliente: dict, coberturas_disponiveis: list[str],
               tipo_cobertura: str = "mix") -> list[dict]:
    """Compat: devolve seleção AZOS no formato antigo (lista de {nome, valor, motivo})
    a partir do planejamento_grid."""
    grid = planejamento_grid(cliente, tipo_cobertura)
    selecoes = []
    for L in grid["linhas"]:
        if not L["ativo_default"]: continue
        a = L["azos"] or {}
        if not a.get("disponivel"): continue
        alvo = (a.get("nome_no_portal") or "").lower()
        match = next((n for n in coberturas_disponiveis if alvo and alvo in n.lower()), None)
        if not match: continue
        selecoes.append({
            "nome":   match,
            "valor":  int(a.get("capital_aplicado") or L["capital_sugerido"]),
            "motivo": f"{L['nome']}",
        })
    return selecoes


# ──────────────────────────────────────────────────────────────────────────────
# BLEND DE OURO — presets de planejamento por perfil de cliente
#
# Baseado no material "Montando um Blend v2" (Stoa/Vida Stoa), p.20-22.
# Cada preset define qual(is) seguradora(s) cobre(m) cada linha do catálogo.
# A função `blends_de_ouro(cliente)` devolve apenas os presets que CASAM com
# o perfil do cliente (auto-match por idade, IMC, fumante, profissão e
# dependentes), em ordem de prioridade.
#
# IDs do catálogo após refator Fase A:
#   Morte:       morte_tradicional, morte_term_life, morte_whole_life
#   Acidental:   morte_acidental
#   Invalidez:   invalidez_permanente, invalidez_acidente
#   DG:          doencas_graves_dg13, doencas_graves_dg30  (grupo "doencas_graves")
#   Cirurgias:   cirurgias, quebra_ossos
#   Hospitalar:  internacao_hospitalar (DIH)
#   Renda:       renda_incapacidade (RIT/DIT)
#   Funeral:     funeral_azos
#   SAF MAG:     saf_essencial, saf_plus, saf_premium  (grupo "saf")
# ──────────────────────────────────────────────────────────────────────────────

# Configuração por linha do catálogo. Cada valor indica quais seguradoras
# entram no blend daquela linha para o preset:
#   None      → linha não entra no blend (fica desligada)
#   "azos"    → só AZOS
#   "mag"     → só MAG

_BLENDS_OURO_DEFS: list[dict] = [
    # ── Perfil 1: Cliente jovem saudável (Até 50 · IMC bom · Não fumante) ──
    {
        "id": "ate50_saudavel",
        "nome": "Até 50 · Saudável",
        "descricao": "Cliente jovem, IMC bom, não fumante. Tradicional AZOS na morte + invalidez/DG/cirurgias AZOS + SAF Essencial MAG.",
        "perfil": "Até 50 anos · IMC normal · Não fumante",
        "condicoes": {"idade_max": 50, "imc_max": 30, "fumante": False},
        "linhas": {
            "morte_tradicional":     "azos",
            "morte_term_life":       None,
            "morte_whole_life":      None,
            "morte_acidental":       "azos",
            "invalidez_permanente":  "mag",
            "invalidez_acidente":    "azos",
            "doencas_graves_dg13":   None,
            "doencas_graves_dg30":   "azos",
            "cirurgias":             "mag",
            "quebra_ossos":          "azos",
            "internacao_hospitalar": "azos",
            "renda_incapacidade":    "azos",
            "funeral_azos":          None,
            "saf_essencial":         "mag",
            "saf_plus":              None,
            "saf_premium":           None,
        },
    },

    # ── Perfil 2: Acima 50 — Whole Life para previsibilidade ──
    {
        "id": "acima50_saudavel",
        "nome": "Acima de 50 · Saudável",
        "descricao": "Cliente maduro saudável. Whole Life MAG na base (vitalício nivelado, sem reajuste etário) + DG MAG Premium + cirurgias MAG.",
        "perfil": "Acima de 50 anos · IMC normal · Não fumante",
        "condicoes": {"idade_min": 50, "idade_max": 64, "imc_max": 30, "fumante": False},
        "linhas": {
            "morte_tradicional":     None,
            "morte_term_life":       None,
            "morte_whole_life":      "mag",
            "morte_acidental":       "azos",
            "invalidez_permanente":  "mag",
            "invalidez_acidente":    "azos",
            "doencas_graves_dg13":   None,
            "doencas_graves_dg30":   "mag",
            "cirurgias":             "mag",
            "quebra_ossos":          None,
            "internacao_hospitalar": "azos",
            "renda_incapacidade":    None,
            "funeral_azos":          None,
            "saf_essencial":         "mag",
            "saf_plus":              None,
            "saf_premium":           None,
        },
    },

    # ── Perfil 3: Fumante / IMC alto — risco maior ──
    {
        "id": "fumante_imc_alto",
        "nome": "Fumante / IMC alto",
        "descricao": "Perfil de risco elevado. MAG cobre tudo que dá (Whole Life MAG aceita mais que AZOS) + DG13 (Plus) para conter custo + SAF Essencial.",
        "perfil": "Qualquer idade · IMC alto OU fumante",
        "condicoes": {"_qualquer": ["fumante:sim", "imc_min:30"]},
        "linhas": {
            "morte_tradicional":     None,
            "morte_term_life":       None,
            "morte_whole_life":      "mag",
            "morte_acidental":       "azos",
            "invalidez_permanente":  "mag",
            "invalidez_acidente":    None,
            "doencas_graves_dg13":   "mag",
            "doencas_graves_dg30":   None,
            "cirurgias":             "mag",
            "quebra_ossos":          "azos",
            "internacao_hospitalar": "azos",
            "renda_incapacidade":    None,
            "funeral_azos":          None,
            "saf_essencial":         "mag",
            "saf_plus":              None,
            "saf_premium":           None,
        },
    },

    # ── Perfil 4: Sucessão empresarial/familiar — alta renda ──
    {
        "id": "sucessao",
        "nome": "Sucessão Empresarial/Familiar",
        "descricao": "Foco em capital de morte para sucessão. Whole Life MAG com capital alto (R$ 5MM+) + complementos AZOS.",
        "perfil": "Sucessão · Saudável · IMC normal · Não fumante",
        "condicoes": {"idade_max": 70, "imc_max": 30, "fumante": False, "renda_min": 30_000},
        "linhas": {
            "morte_tradicional":     "azos",
            "morte_term_life":       None,
            "morte_whole_life":      "mag",
            "morte_acidental":       "azos",
            "invalidez_permanente":  "mag",
            "invalidez_acidente":    "azos",
            "doencas_graves_dg13":   None,
            "doencas_graves_dg30":   "mag",
            "cirurgias":             "mag",
            "quebra_ossos":          None,
            "internacao_hospitalar": "azos",
            "renda_incapacidade":    None,
            "funeral_azos":          None,
            "saf_essencial":         None,
            "saf_plus":              None,
            "saf_premium":           "mag",
        },
    },

    # ── Perfil 5: Até 65 hipertenso/diabético controlado ──
    {
        "id": "doente_cronico",
        "nome": "Até 65 · Hipertenso/Diabético",
        "descricao": "Condição crônica controlada. Tradicional AZOS + invalidez por acidente + DG13 (DG30 pode ter restrição).",
        "perfil": "Até 65 anos · Hipertenso ou diabético · IMC bom · Não fumante",
        "condicoes": {"idade_max": 65, "med_continuo": True, "fumante": False},
        "linhas": {
            "morte_tradicional":     "azos",
            "morte_term_life":       None,
            "morte_whole_life":      None,
            "morte_acidental":       "azos",
            "invalidez_permanente":  None,
            "invalidez_acidente":    "azos",
            "doencas_graves_dg13":   "azos",
            "doencas_graves_dg30":   None,
            "cirurgias":             "azos",
            "quebra_ossos":          None,
            "internacao_hospitalar": "azos",
            "renda_incapacidade":    None,
            "funeral_azos":          None,
            "saf_essencial":         "mag",
            "saf_plus":              None,
            "saf_premium":           None,
        },
    },

    # ── Perfil 6: Solteiro sem filhos — foco em "em vida" ──
    {
        "id": "solteiro_sem_filhos",
        "nome": "Solteiro sem filhos",
        "descricao": "Sem dependentes financeiros. Foco total em proteção 'em vida' (invalidez, DG, DIH, cirurgias). Capital de morte mínimo.",
        "perfil": "Até 65 · Solteiro · Sem filhos · Saudável",
        "condicoes": {"idade_max": 65, "fumante": False, "sem_dependentes": True},
        "linhas": {
            "morte_tradicional":     None,
            "morte_term_life":       None,
            "morte_whole_life":      None,
            "morte_acidental":       "azos",
            "invalidez_permanente":  "mag",
            "invalidez_acidente":    "azos",
            "doencas_graves_dg13":   None,
            "doencas_graves_dg30":   "azos",
            "cirurgias":             "mag",
            "quebra_ossos":          "azos",
            "internacao_hospitalar": "azos",
            "renda_incapacidade":    "azos",
            "funeral_azos":          "azos",
            "saf_essencial":         None,
            "saf_plus":              None,
            "saf_premium":           None,
        },
    },

    # ── Perfil 7: Acima 65 sem saúde — proteção mínima ──
    {
        "id": "idoso_sem_saude",
        "nome": "Acima de 65 · Sem saúde",
        "descricao": "Idade avançada com saúde comprometida. MAG cobre quase tudo (aceita melhor que AZOS nesse perfil) + SAF Essencial.",
        "perfil": "Acima 65 · Sem saúde · IMC e tabagismo irrelevantes",
        "condicoes": {"idade_min": 65},
        "linhas": {
            "morte_tradicional":     "azos",
            "morte_term_life":       None,
            "morte_whole_life":      None,
            "morte_acidental":       "azos",
            "invalidez_permanente":  "mag",
            "invalidez_acidente":    None,
            "doencas_graves_dg13":   None,
            "doencas_graves_dg30":   None,
            "cirurgias":             "mag",
            "quebra_ossos":          None,
            "internacao_hospitalar": "azos",
            "renda_incapacidade":    None,
            "funeral_azos":          "azos",
            "saf_essencial":         "mag",
            "saf_plus":              None,
            "saf_premium":           None,
        },
    },

    # ── Perfil 8: Profissões diferenciadas (médicos/dentistas) ──
    {
        "id": "profissoes_diferenciadas",
        "nome": "Profissões diferenciadas",
        "descricao": "Médicos e dentistas têm IPTA Médicos e DG Top na AZOS (taxas reduzidas). Blend completo na AZOS + SAF MAG.",
        "perfil": "Até 65 · Médico, Dentista, Engenheiro ou Advogado",
        "condicoes": {"idade_max": 65, "profissao_match": r"m[eé]dico|dentista|engenheir|advogad"},
        "linhas": {
            "morte_tradicional":     "azos",
            "morte_term_life":       None,
            "morte_whole_life":      None,
            "morte_acidental":       "azos",
            "invalidez_permanente":  "azos",
            "invalidez_acidente":    "azos",
            "doencas_graves_dg13":   None,
            "doencas_graves_dg30":   "azos",
            "cirurgias":             "azos",
            "quebra_ossos":          "azos",
            "internacao_hospitalar": "azos",
            "renda_incapacidade":    "azos",
            "funeral_azos":          None,
            "saf_essencial":         "mag",
            "saf_plus":              None,
            "saf_premium":           None,
        },
    },

    # ── Perfil 9: Orçamento curto — cobertura mínima essencial ──
    {
        "id": "orcamento_curto",
        "nome": "Orçamento curto",
        "descricao": "Cobertura mínima essencial: Tradicional AZOS (custo inicial baixo) + invalidez por acidente + Quebra de Ossos.",
        "perfil": "Até 65 · Orçamento limitado",
        "condicoes": {"idade_max": 65, "renda_max": 8_000},
        "linhas": {
            "morte_tradicional":     "azos",
            "morte_term_life":       None,
            "morte_whole_life":      None,
            "morte_acidental":       "azos",
            "invalidez_permanente":  None,
            "invalidez_acidente":    "azos",
            "doencas_graves_dg13":   None,
            "doencas_graves_dg30":   None,
            "cirurgias":             None,
            "quebra_ossos":          "azos",
            "internacao_hospitalar": None,
            "renda_incapacidade":    None,
            "funeral_azos":          None,
            "saf_essencial":         None,
            "saf_plus":              None,
            "saf_premium":           None,
        },
    },
]


def _calcular_imc(cliente: dict) -> float:
    try:
        altura_cm = float(cliente.get("altura") or 0)
        peso_kg   = float(cliente.get("peso")   or 0)
        if altura_cm <= 0 or peso_kg <= 0:
            return 0.0
        return round(peso_kg / ((altura_cm / 100.0) ** 2), 1)
    except Exception:
        return 0.0


def _eh_fumante(cliente: dict) -> bool:
    v = cliente.get("fumante", "")
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("sim", "true", "1", "yes")


def _avalia_condicao(blend: dict, cliente: dict) -> tuple[bool, list[str]]:
    """Retorna (combina, razões). Razões explicam por que casou ou não."""
    import re as _re
    cond = blend.get("condicoes", {}) or {}
    razoes = []
    idade = calcular_idade(cliente.get("nascimento", "01/01/1985"))
    imc   = _calcular_imc(cliente)
    fumante = _eh_fumante(cliente)
    profissao = str(cliente.get("profissao", "")).lower()
    renda  = float(cliente.get("renda_mensal") or 0)
    med_continuo = str(cliente.get("med_continuo", "nao")).lower() == "sim"
    sem_deps = not bool(cliente.get("tem_dependentes")) and \
               str(cliente.get("estado_civil","solteiro")).lower() == "solteiro"

    ok = True
    if "idade_max" in cond:
        if idade > cond["idade_max"]: ok = False
        else: razoes.append(f"≤{cond['idade_max']} anos")
    if "idade_min" in cond:
        if idade < cond["idade_min"]: ok = False
        else: razoes.append(f"≥{cond['idade_min']} anos")
    if "imc_max" in cond:
        if imc > cond["imc_max"]: ok = False
        elif imc > 0: razoes.append(f"IMC {imc} (≤{cond['imc_max']})")
    if "imc_min" in cond:
        if imc < cond["imc_min"]: ok = False
        elif imc > 0: razoes.append(f"IMC {imc} (≥{cond['imc_min']})")
    if "fumante" in cond:
        if fumante != cond["fumante"]: ok = False
        else: razoes.append("fumante" if fumante else "não fumante")
    if "profissao_match" in cond:
        if not _re.search(cond["profissao_match"], profissao): ok = False
        else: razoes.append(f"profissão {profissao}")
    if "renda_min" in cond:
        if renda < cond["renda_min"]: ok = False
        else: razoes.append(f"renda ≥ R$ {cond['renda_min']:,}".replace(",","."))
    if "renda_max" in cond:
        if renda > cond["renda_max"]: ok = False
        else: razoes.append(f"renda ≤ R$ {cond['renda_max']:,}".replace(",","."))
    if cond.get("med_continuo") is True and not med_continuo:
        ok = False
    elif cond.get("med_continuo"):
        razoes.append("uso contínuo de medicamento")
    if cond.get("sem_dependentes") is True and not sem_deps:
        ok = False
    elif cond.get("sem_dependentes"):
        razoes.append("sem dependentes")

    # Condição especial: "_qualquer" — basta UM dos itens bater (OR)
    if "_qualquer" in cond:
        any_ok = False
        for item in cond["_qualquer"]:
            chave, _, val = item.partition(":")
            if chave == "fumante" and fumante and val == "sim": any_ok = True
            elif chave == "imc_min" and imc >= float(val or 0): any_ok = True
        if not any_ok: ok = False
        else: razoes.append("perfil de risco (fumante ou IMC alto)")

    return ok, razoes


def blends_de_ouro(cliente: dict) -> list[dict]:
    """
    Devolve os Blends de Ouro aplicáveis ao perfil do cliente, em ordem de
    prioridade (perfis mais específicos primeiro). Cada item tem:
      - id, nome, descricao, perfil (display)
      - linhas: {linha_id: 'azos'|'mag'|None}
      - aplicavel (bool)
      - razoes ([str] explicação de por que combina)
    """
    out = []
    for blend in _BLENDS_OURO_DEFS:
        aplicavel, razoes = _avalia_condicao(blend, cliente)
        out.append({
            "id":         blend["id"],
            "nome":       blend["nome"],
            "descricao":  blend["descricao"],
            "perfil":     blend["perfil"],
            "linhas":     blend["linhas"],
            "aplicavel":  aplicavel,
            "razoes":     razoes,
        })
    # Aplicáveis primeiro
    out.sort(key=lambda b: (0 if b["aplicavel"] else 1, b["id"]))
    return out
