"""
Recomendador / planejamento Blend.

Em vez de produtos isolados por seguradora, o planejamento gira em torno de
"linhas conceituais" (Morte qualquer causa, Morte acidental, Doenças Graves,
Cirurgias, Assistência Funeral etc.) — para cada linha, AZOS e MAG aparecem
lado a lado com prêmio estimado. O Life Planner edita o capital sugerido e
escolhe qual seguradora cobre cada linha para montar o blend final.

Taxas (R$/mês por R$1.000 de capital) calibradas a partir de cotações reais
e de ajuste por idade (~+40% a cada 10 anos acima de 35) — depois substituídas
pelos prêmios reais quando a pré-simulação Playwright for executada.
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
#   azos/mag.fonte → "calibrada" (observada em cotação real) ou "estimada"
# ──────────────────────────────────────────────────────────────────────────────
_LINHAS_COMPARATIVAS: list[dict] = [
    # ──────────────────────────────────────────────────────────────────────────
    # MORTE — 3 linhas distintas para o LP escolher a modalidade ideal:
    #   (1) Whole Life — vitalício nivelado (Private MAG é o produto líder)
    #   (2) Term Life — nivelado por prazo (Vida Segura AZOS / Private TL MAG)
    #   (3) Tradicional — Especialista AZOS (reajusta com idade, mais barato hoje)
    # ──────────────────────────────────────────────────────────────────────────

    # ── 1) MORTE — WHOLE LIFE (vitalício prêmio nivelado) ─────────────────
    {
        "id": "morte_whole_life",
        "nome": "Morte — Whole Life (vitalício nivelado)",
        "tipo": "morte",
        "modalidade": "whole_life",
        "descricao": "Vitalício com prêmio fixo a vida toda. Ideal para sucessão patrimonial e clientes de alta renda.",
        "anos_renda": 10,
        "capital_min": 100_000, "capital_max": 25_000_000,
        "azos": {
            "disponivel": False,
            "modalidade": "whole_life",
            "obs": "AZOS Especialista é Tradicional (reajusta com idade) — sem Whole Life nivelado puro.",
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
            "obs": "Vitalício, prêmio nivelado fixo. Capital de R$1MM a R$25MM. Idade 25-70.",
        },
    },

    # ── 2) MORTE — TERM LIFE NIVELADO (prêmio fixo por 10/15/20/30 anos) ──
    {
        "id": "morte_term_life",
        "nome": "Morte — Term Life (nivelado 20 anos)",
        "tipo": "morte",
        "modalidade": "term_life",
        "descricao": "Prêmio fixo por prazo definido (ex: 20 anos), termina a vigência ao fim do prazo. Custo até 60% menor que Tradicional.",
        "anos_renda": 10,
        "capital_min": 100_000, "capital_max": 10_000_000,
        "azos": {
            "disponivel": True,
            "produto": "Vida Segura (Term Life)",
            "nome_no_portal": "Vida Segura",
            "modalidade": "term_life",
            "modelo_preco": "taxa", "taxa": 0.16,
            "min": 60_000, "max": 3_000_000,
            "fonte": "estimada",
            "obs": "Prêmio nivelado por 20 anos; mínimo R$60k. VS5 mais caro. Aceita troca p/ vitalício.",
        },
        "mag": {
            "disponivel": True,
            "produto": "Private Solutions · Term Life",
            "nome_no_portal": "TERM LIFE",
            "modalidade": "term_life",
            "modelo_preco": "taxa", "taxa": 0.19,
            "min": 100_000, "max": 10_000_000,
            "fonte": "estimada",
            "obs": "Term Life com saldamento. Muito competitivo p/ sexo feminino. Permite mudança p/ vitalício.",
        },
    },

    # ── 3) MORTE — TRADICIONAL (Especialista AZOS, reajusta com idade) ────
    {
        "id": "morte_tradicional",
        "nome": "Morte — Tradicional (reajuste etário)",
        "tipo": "morte",
        "modalidade": "tradicional",
        "descricao": "Vitalício com renovação anual e reajuste por idade. Custo inicial menor, mas sobe com o tempo. Maior comissão recorrente.",
        "anos_renda": 10,
        "capital_min": 50_000, "capital_max": 3_000_000,
        "azos": {
            "disponivel": True,
            "produto": "Especialista · Morte (M)",
            "nome_no_portal": "Seguro de vida",
            "susep": "15414.604991/2023-12",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.40,
            "min": 50_000, "max": 3_000_000,
            "fonte": "calibrada",
            "obs": "Vitalício renovável; reajuste etário + IPCA. Hoje tem a melhor tabela de reajuste do mercado.",
        },
        "mag": {
            "disponivel": False,
            "obs": "MAG não tem tradicional puro no portal — oferece Whole Life ou Term Life.",
        },
    },

    # ── 2) MORTE ACIDENTAL ──────────────────────────────────────────────────
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
            "obs": "Componente embutido nos pacotes SAF — não comparável isoladamente.",
        },
    },

    # ── 3) INVALIDEZ PERMANENTE TOTAL (qualquer causa) ──────────────────────
    {
        "id": "invalidez_permanente",
        "nome": "Invalidez Permanente Total (IPT)",
        "tipo": "invalidez",
        "descricao": "Indenização em caso de invalidez permanente total por qualquer causa.",
        "anos_renda": 10,
        "capital_min": 100_000, "capital_max": 1_000_000,
        "azos": {
            "disponivel": True,
            "produto": "Invalidez Permanente Total (IPT)",
            "nome_no_portal": "Invalidez Permanente",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.11,
            "min": 100_000, "max": 1_000_000,
            "fonte": "calibrada",
        },
        "mag": {
            "disponivel": False,
            "obs": "MAG não oferece IPT isolada no canal corretor — coberta dentro dos pacotes SAF.",
        },
    },

    # ── 4) INVALIDEZ TOTAL POR ACIDENTE (Majorada) ──────────────────────────
    {
        "id": "invalidez_acidente",
        "nome": "Invalidez Total por Acidente (Majorada)",
        "tipo": "invalidez",
        "descricao": "Indenização majorada quando a invalidez total é por acidente pessoal.",
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
            "obs": "Componente do pacote SAF — não isolável.",
        },
    },

    # ── 5) DOENÇAS GRAVES (30 doenças vs MAG Plus 27) ───────────────────────
    {
        "id": "doencas_graves",
        "nome": "Doenças Graves",
        "tipo": "doenca",
        "descricao": "Capital pago em vida no diagnóstico de doença grave (câncer, AVC, infarto etc).",
        "anos_renda": 3,
        "capital_min": 100_000, "capital_max": 800_000,
        "azos": {
            "disponivel": True,
            "produto": "Doenças Graves 30 (DG30)",
            "nome_no_portal": "Doenças Graves",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.30,
            "min": 100_000, "max": 800_000,
            "fonte": "calibrada",
            "obs": "Cobre 30 doenças graves — versão mais completa da Azos.",
        },
        "mag": {
            "disponivel": True,
            "produto": "Doenças Graves Plus (3501)",
            "nome_no_portal": "DOENÇAS GRAVES PLUS (3501)",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.50,
            "min": 100_000, "max": 500_000,
            "fonte": "estimada",
            "obs": "27 doenças (câncer, AVC, Parkinson, transplantes) — calibrar com pré-simulação.",
        },
    },

    # ── 6) CIRURGIAS ─────────────────────────────────────────────────────────
    {
        "id": "cirurgias",
        "nome": "Cirurgias",
        "tipo": "saude",
        "descricao": "Indenização por procedimentos cirúrgicos listados na apólice (código TUSS).",
        "anos_renda": 2,
        "capital_min": 50_000, "capital_max": 200_000,
        "azos": {
            "disponivel": True,
            "produto": "Cirurgias 2.0 (C2.0)",
            "nome_no_portal": "Cirurgia",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.18,
            "min": 50_000, "max": 200_000,
            "fonte": "estimada",
        },
        "mag": {
            "disponivel": True,
            "produto": "Cirurgias + Amparo (3511)",
            "nome_no_portal": "CIRURGIAS + AMPARO (3511)",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.25,
            "min": 50_000, "max": 200_000,
            "fonte": "estimada",
            "obs": "Cirurgias + amparo financeiro durante a recuperação.",
        },
    },

    # ── 7) DIÁRIA DE INTERNAÇÃO HOSPITALAR ─────────────────────────────────
    {
        "id": "internacao_hospitalar",
        "nome": "Diária de Internação Hospitalar (DIH)",
        "tipo": "hospitalar",
        "descricao": "Indenização diária enquanto o segurado estiver internado em hospital.",
        "anos_renda": 0,
        "capital_min": 100, "capital_max": 1_000,
        "capital_padrao": 300,
        "unidade": "R$/dia",
        "azos": {
            "disponivel": True,
            "produto": "DIH",
            "nome_no_portal": "Internação",
            "modalidade": "tradicional",
            "modelo_preco": "por_unidade", "taxa": 0.08,  # R$/mês por cada R$1 de diária
            "min": 100, "max": 1_000,
            "fonte": "estimada",
            "obs": "Limite típico 30-60 diárias por evento.",
        },
        "mag": {
            "disponivel": False,
            "obs": "MAG não oferece DIH isolada no portal corretor.",
        },
    },

    # ── 8) RENDA POR INCAPACIDADE TEMPORÁRIA ───────────────────────────────
    {
        "id": "renda_incapacidade",
        "nome": "Renda por Incapacidade Temporária (RIT)",
        "tipo": "renda",
        "descricao": "Renda mensal enquanto o segurado estiver afastado por doença ou acidente.",
        "anos_renda": 0,
        "capital_min": 1_000, "capital_max": 30_000,
        "capital_padrao_por_renda": 0.6,  # 60% da renda mensal
        "unidade": "R$/mês de renda",
        "azos": {
            "disponivel": True,
            "produto": "RIT (Renda por Incapacidade Temporária)",
            "nome_no_portal": "Renda por Incapacidade",
            "modalidade": "tradicional",
            "modelo_preco": "por_unidade", "taxa": 0.025,  # R$/mês por R$1 de renda
            "min": 1_000, "max": 30_000,
            "fonte": "estimada",
            "obs": "Carência típica 30 dias; pagamento até 12 meses por evento.",
        },
        "mag": {
            "disponivel": False,
            "obs": "Não disponível isoladamente no portal.",
        },
    },

    # ── 9) ASSISTÊNCIA FUNERAL ──────────────────────────────────────────────
    {
        "id": "funeral",
        "nome": "Assistência Funeral",
        "tipo": "assistencia",
        "modalidade": "pacote_fixo",
        "descricao": "Cobertura dos custos de funeral até o limite contratado (titular + dependentes).",
        "anos_renda": 0,
        "capital_min": 5_000, "capital_max": 30_000,
        "unidade": "Limite R$",
        "azos": {
            "disponivel": True,
            "produto": "Assistência Funeral Familiar (AFF)",
            "nome_no_portal": "Funeral",
            "modalidade": "pacote_fixo",
            "modelo_preco": "fixo", "premio_fixo": 14.90,
            "min": 5_000, "max": 30_000,
            "fonte": "estimada",
            "obs": "Pacote por valor fixo, cobre titular + cônjuge + filhos.",
        },
        "mag": {
            "disponivel": True,
            "modalidade": "pacote_fixo",
            "obs": "Componente embutido nos pacotes SAF MAG — sem prêmio adicional.",
            "modelo_preco": "fixo", "premio_fixo": 0.0,
        },
    },

    # ── 10) SAF FAMILIAR (Pacote MAG: titular + cônjuge + pais e sogros) ──
    {
        "id": "saf_familiar",
        "nome": "SAF Familiar (Pacote MAG)",
        "tipo": "assistencia",
        "modalidade": "pacote_fixo",
        "descricao": "Pacote MAG que cobre o titular + cônjuge + pais + sogros com capital fixo. Inclui assistência funeral. Vendido apenas pela MAG.",
        "anos_renda": 0,
        "capital_min": 5_500, "capital_max": 5_500,
        "unidade": "Limite R$",
        "azos": {
            "disponivel": False,
            "obs": "Equivalente: AFF (Assistência Funeral Familiar) vendido separadamente.",
        },
        "mag": {
            "disponivel": True,
            "produto": "SAF Essencial Familiar + Pais e Sogros (3061)",
            "nome_no_portal": "SAF ESSENCIAL FAMILIAR + PAIS E SOGROS (3061)",
            "modalidade": "pacote_fixo",
            "modelo_preco": "fixo",
            "premio_fixo": 28.41, "capital_fixo": 5_500,
            "min": 5_500, "max": 5_500,
            "fonte": "calibrada",
            "obs": "Pacote familiar com capital fixo R$5.500. Cross-sell após primeira venda MAG.",
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
