"""
Recomendador / planejamento Blend.

Em vez de produtos isolados por seguradora, o planejamento gira em torno de
"linhas conceituais" (Morte qualquer causa, Morte acidental, Doenças Graves,
Cirurgias, Assistência Funeral etc.) — para cada linha, AZOS e MAG aparecem
lado a lado com prêmio estimado. O Life Planner edita o capital sugerido e
escolhe qual seguradora cobre cada linha para montar o blend final.

Catálogo calibrado pelo material "Montando um Blend v2" (Stoa, 2025/2026)
+ Manual de Subscrição AZOS Abril/26 v2 + Dicas Underwriting MAG Ago/23 v1.9:

  - Morte: AZOS Tradicional (TR1); MAG Term Life e Whole Life nivelados (canal PRIVATE).
  - Morte Acidental (MAC): AZOS oferece até R$1MM avulso (Manual Azos pg 4, 17);
    MAG embutido em pacotes DIT/SAF.
  - Invalidez: AZOS separa IPTA Majorada (acidente, até R$3MM, 12 eventos),
    IPTA Majorada Estendida (médico/dentista, até R$1MM) e IPT (qualquer
    causa, até R$1MM, 11 eventos). MAG IPA+IFPD majorada (768/769/2279).
  - DG: AZOS DG13 (13 doenças)/DG30 (30 doenças); MAG DG Plus 10d ou 28d.
    DG VITAL (MAG 3532) é RIDER de câncer-only, cap R$200k, NÃO comparável
    com DG13/DG30 (linha própria "doencas_graves_vital_cancer").
  - DIH: AZOS até R$1k/diária (R$500 se 61-65), 200 diárias/evento;
    MAG R$3k/diária sem UTI ou R$9k com Adicional UTI 200%.
  - Cirurgias: AZOS Cirurgias 2.0 (R$100k, 652 procs); MAG Cirurgias+Amparo.
  - Rupturas e Fraturas (REF): AZOS até R$300k. MAG sem cobertura.
  - SAF MAG: 3 tiers com capital fixo (Essencial 5.5k / Plus 10k / Premium 15k).

Taxas (R$/mês por R$1.000 de capital) calibradas a partir das tabelas do PDF
para perfil-base (homem 33a, não fumante) e do fator idade (+40% a cada 10
anos acima de 35) — substituídas pelos prêmios reais quando a pré-simulação
Playwright for executada.

Limites por idade × renda (clamp) e idade de corte vivem em
`automacao.auditor_catalogo`, fonte: Manual Azos pg 1, 14-20.
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
            "min": 50_000, "max": 5_000_000,
            "fonte": "calibrada",
            "fonte_pdf": "Manual Azos Abr/26 pg 4, 14, 16",
            "idade_corte_anos": None,  # vitalício enquanto pagar
            "obs": "Taxa ano 1 (~R$104,67/MM p/ 33a no PDF v2; R$199/1,2MM p/ 36a na cotação real). Reajusta com idade. Capital máximo absoluto R$5MM (sujeito a faixa renda/idade, ver auditor).",
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
            "produto": "Private Solutions · Term Life (modelo PRIVATE VD STOA)",
            "nome_no_portal": "TERM LIFE",
            "modalidade": "term_life",
            "modelo_preco": "taxa", "taxa": 0.18,
            "min": 100_000, "max": 10_000_000,
            "fonte": "estimada",
            "fonte_pdf": "Dicas Underwriting MAG Ago/23 pg 13 (Private Solutions Cobertura MORTE)",
            "idade_corte_anos": None,  # termina no fim do prazo
            "cotacao_real": False,
            "canal_mag": "PRIVATE VD STOA",
            "obs": "Disponível só no modelo 'PRIVATE VD STOA' do portal MAG (UI diferente, botão 'Editar Solução'). Fluxo de cotação dedicado em roadmap — por ora exibe prêmio estimado. R$183,06/MM p/ 33a, 20 anos (PDF v2). Capital acima R$3MM exige tele entrevista + exames; acima R$12MM exige comprovante de renda.",
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
            "produto": "Private Solutions · Whole Life Sucessão (3108-3113) — modelo PRIVATE VD STOA",
            "nome_no_portal": "WHOLE LIFE SUCESSAO",
            "susep": "15414.901244/2024",
            "modalidade": "whole_life",
            "modelo_preco": "taxa", "taxa": 0.55,
            "min": 1_000_000, "max": 25_000_000,
            "fonte": "estimada",
            "fonte_pdf": "Dicas Underwriting MAG Ago/23 pg 13",
            "idade_corte_anos": None,
            "cotacao_real": False,
            "canal_mag": "PRIVATE VD STOA",
            "aviso_privet": "Privet VD STOA exige prêmio mensal mínimo R$400,00 — capitais menores que R$1MM serão recusados ou exigirão outro modelo.",
            "obs": "Disponível só no modelo 'PRIVATE VD STOA' do portal MAG (UI diferente). Fluxo dedicado em roadmap — por ora exibe prêmio estimado. Capital R$1MM-25MM. Idade 25-70. Capital acima R$3MM exige tele/exames; >R$6MM idade>60 exige Ergométrico; >R$12MM exige comprovante de renda.",
        },
    },

    # ── MORTE ACIDENTAL ────────────────────────────────────────────────────
    {
        "id": "morte_acidental",
        "nome": "Morte Acidental (MAC)",
        "tipo": "morte",
        "descricao": "Indenização extra quando a morte é decorrente de acidente pessoal coberto. Acumula com Morte para capitais acima de R$3MM (limite do par soma).",
        "anos_renda": 5,
        "capital_min": 50_000, "capital_max": 1_000_000,
        "azos": {
            "disponivel": True,
            "produto": "Morte Acidental (MAC)",
            "nome_no_portal": "Morte acidental",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.05,
            "min": 50_000, "max": 1_000_000,
            "fonte": "calibrada",
            "fonte_pdf": "Manual Azos Abr/26 pg 4, 17",
            "idade_corte_anos": None,
            "obs": "Cobertura oficial AZOS (até R$1MM). Portal aceita no máx R$1MM. Capitais acima travam 'Ir para o Resumo'. Vitalício enquanto pagar.",
        },
        "mag": {
            "disponivel": True,
            "produto": "MAC (rider de IPA + IFPD / DIT / SAF)",
            "nome_no_portal": "MAC",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.06,
            "min": 50_000, "max": 1_000_000,
            "fonte": "estimada",
            "fonte_pdf": "Dicas Underwriting MAG Ago/23 pg 6 (Private Solutions cobertura MAC) + pg 7 (lista riders Vida Toda)",
            "idade_corte_anos": None,
            "cotacao_real": False,
            "canal_mag": "embutido em DIT (2398) ou Private Riders",
            "obs": "MAG oferece MAC como rider de DIT/IPA+IFPD/SAF (modelo Vida Toda VD STOA) ou Winsocial (como garantia básica quando MQC não cabe). Vide pg 6-7 das Dicas. Cobertura isolada não vendida no canal corretor — usar pacote.",
        },
    },

    # ── INVALIDEZ PERMANENTE TOTAL (IPT — qualquer causa) ────────────────
    {
        "id": "invalidez_permanente",
        "nome": "Invalidez Permanente Total (IPT) — qualquer causa",
        "tipo": "invalidez",
        "grupo_exclusivo": "invalidez_qualquer_causa",
        "grupo_titulo": "Invalidez por qualquer causa — escolha 1 modalidade por seguradora",
        "descricao": "Indenização por invalidez permanente total por qualquer causa (doença ou acidente). AZOS IPT (11 eventos, cancela aos 75a). MAG IPA Majorada + IFPD (Invalidez Funcional Permanente por Doença).",
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
            "fonte_pdf": "Manual Azos Abr/26 pg 4 (IPT — 11 eventos), pg 18 (capital por renda/idade)",
            "idade_corte_anos": 75,
            "obs": "11 eventos cobertos (Manual Abr/26 pg 5: visão, membros sup/inf, mãos, polegar, alienação mental, anquilose cotovelo/punhos, mudez, surdez). R$70,44/MM p/ 33a no PDF v2. Cancela aos 75 anos. Carência 60d exceto acidente.",
        },
        "mag": {
            "disponivel": True,
            "produto": "IPA com Majoração + IFPD (2279)",
            "nome_no_portal": "IPA COM MAJORAÇÃO + IFPD (2279)",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.076,
            "min": 100_000, "max": 1_700_000,
            "fonte": "calibrada",
            "fonte_pdf": "Dicas Underwriting MAG Ago/23 pg 8 (cap auto invalidez por idade)",
            "idade_corte_anos": None,
            "obs": "Inclui IFPD (Invalidez Funcional Permanente por Doença) — versão estendida usada na planilha calculadora Stoa. Cap auto: até 60a R$1,7MM; 61-65a R$1,2MM; 66-70a R$1MM. Idade 16-65 entrada, vitalício. Abdica do 769.",
        },
    },

    # ── IPTA MAJORADA (acidente) ─────────────────────────────────────────
    {
        "id": "invalidez_acidente",
        "nome": "Invalidez Permanente por Acidente — IPTA Majorada (12 eventos)",
        "tipo": "invalidez",
        "descricao": "AZOS IPTA Majorada cobre 12 eventos de invalidez por ACIDENTE (perda total visão/membros/mãos/pés + polegar + alienação mental + anquilose cotovelo/punho + mudez/surdez), até R$3MM. MAG embute majoração no IPA+IFPD.",
        "anos_renda": 8,
        "capital_min": 100_000, "capital_max": 3_000_000,
        "azos": {
            "disponivel": True,
            "produto": "IPTA Majorada",
            "nome_no_portal": "Invalidez Total por Acidente",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.07,
            "min": 100_000, "max": 3_000_000,
            "fonte": "calibrada",
            "fonte_pdf": "Manual Azos Abr/26 pg 4 (lista oficial), pg 17 (capital máx por renda/idade)",
            "idade_corte_anos": None,
            "obs": "12 eventos cobertos. Cap absoluto R$3MM (sujeito a faixa renda/idade: 7-10k → 2MM, 15-20k → 3MM, etc). Vitalício enquanto pagar. Sem carência exceto franquia regular.",
        },
        "mag": {
            "disponivel": False,
            "obs": "Majoração já inclusa no IPA+IFPD MAG — não isolável. Para acidente puro com majoração: rider 768 (IPA Majorada) + 769 (IFPD). 2279 já abdica do 769.",
        },
    },

    # ── IPTA MAJORADA ESTENDIDA (exclusiva médico/dentista) ───────────────
    {
        "id": "ipta_majorada_estendida",
        "nome": "IPTA Majorada Estendida (médico/dentista)",
        "tipo": "invalidez",
        "descricao": "AZOS exclusiva para médicos e dentistas. Antecipação do capital de IPTA Majorada para 3 eventos adicionais críticos pra carreira manual: perda de indicadores e imobilidade cervical/tóraco-lombo-sacro da coluna.",
        "restricao_profissao": ["médico", "medico", "dentista", "cirurgião", "cirurgiao"],
        "anos_renda": 5,
        "capital_min": 100_000, "capital_max": 1_000_000,
        "azos": {
            "disponivel": True,
            "produto": "IPTA Majorada Estendida",
            "nome_no_portal": "Invalidez Total por Acidente Estendida",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.085,
            "min": 100_000, "max": 1_000_000,
            "fonte": "estimada",
            "fonte_pdf": "Manual Azos Abr/26 pg 4 (IPTA Maj Estendida — exclusiva médicos/dentistas)",
            "idade_corte_anos": None,
            "obs": "EXCLUSIVA pra médicos e dentistas. 3 eventos: perda total uso dos indicadores, imobilidade segmento cervical da coluna, imobilidade segmento tóraco-lombo-sacro. Capital é ANTECIPAÇÃO do IPTA Majorada — paga uma vez, reduz o IPTA na mesma quantia. Cap absoluto R$1MM.",
        },
        "mag": {
            "disponivel": False,
            "obs": "MAG não tem equivalente isolado. IPA + IFPD MAG cobre cervical/tóraco como parte da Invalidez Funcional por Doença, mas com franquia maior.",
        },
    },

    # ──────────────────────────────────────────────────────────────────────
    # DOENÇAS GRAVES — 3 linhas:
    #   - Essencial (~13 doenças): AZOS DG13 + MAG Plus (10 doenças, rider 3501)
    #   - Completo (~28-30 doenças): AZOS DG30 + MAG Plus Premium (28 doenças)
    #     (NOTA: MAG DG VITAL NÃO entra aqui — é rider câncer-only de 200k)
    #   - Câncer Complementar (rider): MAG DG VITAL (200k, só câncer)
    # ──────────────────────────────────────────────────────────────────────

    # ── DG — ESSENCIAL 13 ──────────────────────────────────────────────────
    {
        "id": "doencas_graves_dg13",
        "nome": "Doenças Graves — Essencial (13 doenças)",
        "tipo": "doenca",
        "grupo_exclusivo": "doencas_graves",
        "grupo_titulo": "Doenças Graves — escolha 1 nível de cobertura",
        "descricao": "Cobertura essencial. AZOS DG13: câncer (30%/50%/100%), AVC, infarto, Alzheimer, perda visão, paralisia membros, Parkinson, esclerose múltipla, osteomielite, embolia pulmonar, coma trauma craniano, doença neurônio motor, hepatite aguda fulminante. Capital pago em vida no diagnóstico.",
        "anos_renda": 3,
        "capital_min": 100_000, "capital_max": 1_000_000,
        "azos": {
            "disponivel": True,
            "produto": "Doenças Graves 13 (DG13)",
            "nome_no_portal": "Doenças Graves",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.308,
            "min": 100_000, "max": 1_000_000,
            "fonte": "calibrada",
            "fonte_pdf": "Manual Azos Abr/26 pg 5 (lista 13 eventos), pg 19 (capital por renda/idade)",
            "idade_corte_anos": 75,
            "obs": "R$153,95/500k p/ 33a no PDF v2. 13 doenças. Vende avulso. Reenquadramento anual. Sem vínculo com MQC. Cap absoluto R$1MM (sujeito a faixa renda/idade). Cancela aos 75 anos. Carência 60d.",
        },
        "mag": {
            "disponivel": True,
            "produto": "Doenças Graves Plus (3501) — 10 doenças",
            "nome_no_portal": "DOENÇAS GRAVES PLUS (3501)",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.332,
            "min": 100_000, "max": 500_000,
            "fonte": "calibrada",
            "fonte_pdf": "Dicas Underwriting MAG Ago/23 pg 8 (DG Linha Vida Toda + DPS = R$500k)",
            "idade_corte_anos": None,
            "obs": "R$166,18/500k p/ 33a no PDF v2. 10 doenças. Reenquadramento a cada 5 anos (idade final 1 e 6). Câncer LMG sim. Sem vínculo MQC. Cap R$500k Vida Toda DPS; R$1MM Linha Private com tele/exames.",
        },
    },

    # ── DG — COMPLETO 30 ───────────────────────────────────────────────────
    {
        "id": "doencas_graves_dg30",
        "nome": "Doenças Graves — Completo (28-30 doenças)",
        "tipo": "doenca",
        "grupo_exclusivo": "doencas_graves",
        "descricao": "Cobertura ampliada com 28-30 doenças. AZOS DG30 adiciona às 13 anteriores: anemia aplásica, cirurgia de aorta/bypass/válvulas cardíacas, doenças hepáticas graves, lúpus sistêmico, perda de audição/fala, queimaduras graves, 7 tipos de transplante, tumor cerebral benigno. Versão mais completa, indicada quando cliente tem antecedentes ou idade > 40.",
        "anos_renda": 3,
        "capital_min": 100_000, "capital_max": 1_000_000,
        "azos": {
            "disponivel": True,
            "produto": "Doenças Graves 30 (DG30)",
            "nome_no_portal": "Doenças Graves",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.351,
            "min": 100_000, "max": 1_000_000,
            "fonte": "calibrada",
            "fonte_pdf": "Manual Azos Abr/26 pg 6 (lista 30 eventos), pg 19 (capital por renda/idade)",
            "idade_corte_anos": 75,
            "obs": "R$175,60/500k p/ 33a no PDF v2. 30 doenças. Versão mais completa AZOS. Reenquadramento anual. Cap absoluto R$1MM (sujeito faixa renda/idade). Cancela aos 75 anos.",
        },
        "mag": {
            "disponivel": True,
            "produto": "Doenças Graves Plus Premium (28 doenças, 28d) — rider 3501+",
            "nome_no_portal": "DOENÇAS GRAVES PLUS PREMIUM (28 doenças)",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.55,
            "min": 100_000, "max": 1_000_000,
            "fonte": "estimada",
            "fonte_pdf": "Dicas Underwriting MAG Ago/23 pg 8 (Linha Private DG R$1MM)",
            "idade_corte_anos": None,
            "cotacao_real": False,
            "canal_mag": "PRIVATE Solutions",
            "obs": "MAG DG Plus em versão Premium 28 doenças, disponível na Linha Private com tele/exames. Cap R$1MM. NÃO confundir com DG VITAL (rider câncer-only de 200k — linha 'doencas_graves_vital_cancer' separada). Reenquadramento a cada 5 anos.",
        },
    },

    # ── DG VITAL (rider câncer-only complementar) ──────────────────────────
    {
        "id": "doencas_graves_vital_cancer",
        "nome": "Câncer Complementar (rider DG VITAL)",
        "tipo": "doenca",
        "descricao": "Complemento RIDER do DG Plus/Modular. Cobre apenas câncer com qualidade superior (LMG, in situ) e capital máximo R$200k. NÃO substitui DG13/DG30 — é cobertura adicional pra reforçar câncer.",
        "anos_renda": 1,
        "capital_min": 50_000, "capital_max": 200_000,
        "capital_padrao": 200_000,
        "azos": {
            "disponivel": False,
            "obs": "AZOS não tem rider câncer-only equivalente. DG13/DG30 já cobrem câncer (30%/50%/100%) no capital principal.",
        },
        "mag": {
            "disponivel": True,
            "produto": "Doenças Graves Vital (3532) — rider câncer 200k",
            "nome_no_portal": "DOENÇAS GRAVES VITAL (3532)",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.45,
            "min": 50_000, "max": 200_000,
            "fonte": "estimada",
            "fonte_pdf": "input do produto (Manual MAG 2026 oficial pendente)",
            "idade_corte_anos": None,
            "cotacao_real": False,
            "canal_mag": "rider de DG Plus ou Modular (modelo dedicado)",
            "obs": "RIDER de DG Plus/Modular. Câncer ONLY. Capital máximo R$200k. Custo baixo. Estado read-only no combobox de VIDA TODA VD STOA pq exige DG Plus já contratado. Reenquadramento a cada 5 anos.",
        },
    },

    # ── CIRURGIAS 2.0 ──────────────────────────────────────────────────────
    {
        "id": "cirurgias",
        "nome": "Cirurgias 2.0",
        "tipo": "saude",
        "descricao": "Indenização por procedimentos cirúrgicos listados (TUSS). AZOS Cirurgias 2.0 cobre até R$100k. Indeniza 10/20/50/100% do CS conforme tabela do procedimento. MAG Cirurgias + Amparo adiciona valor mensal durante recuperação.",
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
            "fonte_pdf": "Manual Azos Abr/26 pg 4 (Cirurgias 2.0), pg 8 (10/20/50/100%), pg 14 (cap R$100k)",
            "idade_corte_anos": 70,
            "obs": "652 cirurgias cobertas. Capital até R$100k. Indeniza 10/20/50/100% do CS conforme procedimento. Carência 180d exceto acidente. Cancela aos 70 anos.",
        },
        "mag": {
            "disponivel": True,
            "produto": "Cirurgias + Amparo (3511)",
            "nome_no_portal": "CIRURGIAS + AMPARO (3511)",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.25,
            "min": 50_000, "max": 50_000,  # cap auto MAG R$50k (pg 8)
            "fonte": "estimada",
            "fonte_pdf": "Dicas Underwriting MAG Ago/23 pg 8 (Cirurgias automático R$50k)",
            "idade_corte_anos": None,
            "obs": "917 cirurgias + amparo financeiro durante recuperação. Cap automático R$50k (Vida Toda). Capital maior exige análise individual.",
        },
    },

    # ── RUPTURAS E FRATURAS (REF AZOS) ─────────────────────────────────────
    {
        "id": "quebra_ossos",
        "nome": "Rupturas e Fraturas (REF) — quebra de ossos",
        "tipo": "acidente",
        "modalidade": "tradicional",
        "descricao": "Indenização por fraturas ósseas e rupturas de tendões/ligamentos por acidente. Cobertura AZOS (REF, lançada em 2025). Até R$300k. Reintegração de capital a cada 12 meses. Indeniza 5%-100% por evento conforme tabela.",
        "anos_renda": 0,
        "capital_min": 5_000, "capital_max": 300_000,
        "capital_padrao": 50_000,
        "unidade": "Capital (R$)",
        "azos": {
            "disponivel": True,
            "produto": "Rupturas e Fraturas (REF)",
            "nome_no_portal": "Rupturas",
            "modalidade": "tradicional",
            "modelo_preco": "taxa", "taxa": 0.45,
            "min": 5_000, "max": 300_000,
            "fonte": "estimada",
            "fonte_pdf": "Manual Azos Abr/26 pg 4 (Rupturas e Fraturas), pg 8-9 (5%-100%), pg 14 (cap R$300k)",
            "idade_corte_anos": 75,
            "obs": "Cobertura AZOS. Fraturas ósseas + rupturas de tendões e ligamentos. Cap absoluto R$300k. Reintegração 12 meses. Sem carência. Cancela aos 75 anos.",
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
        "descricao": "Indenização diária enquanto internado. Em UTI/CTI a indenização equivale a 3 diárias contratadas. AZOS: 200 diárias por evento, 1000 total. MAG: 250 diárias por evento (Suporte 250) ou 150 (Suporte 150).",
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
            "fonte_pdf": "Manual Azos Abr/26 pg 7 (200 diárias/evento, 1000 vida, UTI×3), pg 19 (cap por renda)",
            "idade_corte_anos": 70,
            "obs": "R$51,30 p/ R$1k de diária (PDF v2). 200 diárias por evento, 1000 vida. Franquia 72h retroativa. UTI = 3 diárias. Cap absoluto R$1k/diária (R$500 se 61-65). Cancela aos 70 anos.",
        },
        "mag": {
            "disponivel": True,
            "produto": "Diária por Internação Hospitalar + Suporte 250 (3510)",
            "nome_no_portal": "DIÁRIA POR INTERNAÇÃO HOSPITALAR + SUPORTE 250 (3510)",
            "modalidade": "tradicional",
            "modelo_preco": "por_unidade", "taxa": 0.0647,
            "min": 100, "max": 3_000,
            "fonte": "calibrada",
            "fonte_pdf": "Dicas Underwriting MAG Ago/23 pg 8 (DIH R$3k sem UTI / R$9k com Adicional UTI 200%)",
            "idade_corte_anos": None,
            "obs": "R$64,68 p/ R$1k (PDF v2). 250 diárias/evento. Franquia 4 dias. UTI = 3 diárias. Cap auto R$3k/diária (sem UTI). Com Adicional UTI sobe pra R$9k/diária (200%). Alternativa Suporte 150 (3509) = 150 diárias.",
        },
    },

    # ── DIT / RIT (Renda por Incapacidade Temporária) ───────────────────────
    {
        "id": "renda_incapacidade",
        "nome": "Renda por Incapacidade Temporária (RIT / DIT)",
        "tipo": "renda",
        "descricao": "Indenização diária enquanto afastado por doença ou acidente. AZOS chama de RIT, MAG chama de DIT. AZOS RIT: 120 diárias pra LER/DORT/hérnia/diálise/cirrose, 730 demais. MAG DIT por grupo de risco profissional.",
        "anos_renda": 0,
        "capital_min": 100, "capital_max": 1_000,
        "capital_padrao": 300,
        "unidade": "R$/dia",
        "azos": {
            "disponivel": True,
            "produto": "RIT (Renda por Incapacidade Temporária)",
            "nome_no_portal": "Renda por Incapacidade",
            "modalidade": "tradicional",
            "modelo_preco": "por_unidade", "taxa": 0.272,
            "min": 100, "max": 1_000,
            "fonte": "calibrada",
            "fonte_pdf": "Manual Azos Abr/26 pg 7 (120 diárias LER/etc + 730 demais), pg 20 (cap: 1/30 salário, R$1k máx, R$500 se 61-65)",
            "idade_corte_anos": 70,
            "obs": "R$/dia de afastamento. Cap = min(1/30 do salário, R$1k até 60 anos, R$500 entre 61-65). Limite 730 dias eventos gerais; 120 dias LER/DORT/hérnia/diálise/cirrose. Hérnia de disco sim. Doenças por vetores sim. Cancela aos 70 anos. Vencedora da categoria no PDF v2.",
        },
        "mag": {
            "disponivel": True,
            "produto": "DIT + MAC + IPAM 10 dias (2398)",
            "nome_no_portal": "DIT + MAC + IPAM 10 DIAS (2398)",
            "modalidade": "tradicional",
            "modelo_preco": "por_unidade", "taxa": 0.30,
            "min": 100, "max": 1_333,  # R$40k/mês ÷ 30 = R$1.333/dia (Grupo Risco 0)
            "fonte": "estimada",
            "fonte_pdf": "Dicas Underwriting MAG Ago/23 pg 8 (DIT/DITA por grupo risco profissão)",
            "idade_corte_anos": None,
            "obs": "R$/dia. Combina DIT + MAC + IPAM. Franquia 10 dias. Cap por grupo de risco profissional: Grupo 0 R$40k/mês (R$1.333/dia), Grupo 1 R$30k (R$1k/dia), Grupos 2-3 R$20k (R$666/dia). Alternativas: DIT + MQC 10 DIAS (2399) ou versões 7 DIAS (2396/2397).",
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
            "fonte_pdf": "Manual Azos Abr/26 pg 4 (4 modalidades), pg 9 (Individual/Familiar/+Pais/+Sogros), pg 14 (cap fixo R$15k)",
            "idade_corte_anos": None,
            "obs": "Capital fixo R$15.000. Sem carência (titular). Pais/sogros: 120 dias carência. Traslado nacional e internacional sem limite km. Inclui urna, veículo, capela, cremação, flores, sepultamento, locação de jazigo. Titular: 18-65; com extensão Pais/Sogros titular até 55.",
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
            "fonte_pdf": "Catálogo MAG VD STOA + PDF Stoa v2 (SAF Essencial Familiar + Pais e Sogros)",
            "idade_corte_anos": None,
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
            "produto": "SAF Plus Familiar + Pais e Sogros (3065)",
            "nome_no_portal": "SAF PLUS FAMILIAR + PAIS E SOGROS (3065)",
            "modalidade": "pacote_fixo",
            "modelo_preco": "fixo",
            "premio_fixo": 42.00,
            "capital_fixo": 10_000,
            "min": 10_000, "max": 10_000,
            "fonte": "estimada",
            "fonte_pdf": "Catálogo MAG VD STOA + PDF Stoa v2 (SAF Plus)",
            "idade_corte_anos": None,
            "obs": "Capital fixo R$10.000 (PDF v2). Inclui titular + cônjuge + filhos + pais + sogros. Translado América Latina. Sem carência. Variações no portal: 3062 (Individual), 3063 (Familiar), 3064 (+Pais).",
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
            "produto": "SAF Premium Familiar + Pais e Sogros (3069)",
            "nome_no_portal": "SAF PREMIUM FAMILIAR + PAIS E SOGROS (3069)",
            "modalidade": "pacote_fixo",
            "modelo_preco": "fixo",
            "premio_fixo": 58.00,
            "capital_fixo": 15_000,
            "min": 15_000, "max": 15_000,
            "fonte": "estimada",
            "fonte_pdf": "Catálogo MAG VD STOA + PDF Stoa v2 (SAF Premium)",
            "idade_corte_anos": None,
            "obs": "Capital fixo R$15.000 (PDF v2). Translado internacional ilimitado. Funeral pet. Sem carência. Variações no portal: 3066 (Individual), 3067 (Familiar), 3068 (+Pais).",
        },
    },
]


def planejamento_grid(cliente: dict, tipo_cobertura: str = "mix",
                      modo_simplificado: str = "") -> dict:
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

    # Dentro de cada grupo_exclusivo só UMA linha pode estar ativa por
    # default (ex: grupo "morte" tem tradicional + term_life + whole_life —
    # contratar os 3 ao mesmo tempo é redundante). A "principal" é a 1ª linha
    # do grupo no catálogo, exceto:
    #   - grupo "doencas_graves": dg30 (completo) prefere idade >= 40
    #   - grupo "morte": whole_life prefere quando renda >= 30k (alta renda /
    #     foco em sucessão), senão tradicional
    principais_por_grupo: dict[str, str] = {}
    for L in _LINHAS_COMPARATIVAS:
        ge = L.get("grupo_exclusivo")
        if not ge or ge in principais_por_grupo:
            continue
        # Default: primeira linha do grupo no catálogo
        principais_por_grupo[ge] = L["id"]
    # Override por perfil: DG30 pra 40+, Whole Life pra renda alta
    if idade >= 40 and "doencas_graves" in principais_por_grupo:
        principais_por_grupo["doencas_graves"] = "doencas_graves_dg30"
    if renda >= 30_000 and "morte" in principais_por_grupo:
        # Renda alta tende a precisar de Whole Life pra sucessão patrimonial.
        # Term Life ainda fica como alternativa que o LP pode ativar manual.
        principais_por_grupo["morte"] = "morte_whole_life"
    # Modo MVP "vitalicio_sem_resgate" (Stoa, Eduardo): força Whole Life
    # e desabilita Term Life/Tradicional como principais. Reduz complexidade
    # de planejamento — recomendado pra maior parte da população.
    if modo_simplificado == "vitalicio_sem_resgate":
        if "morte" in principais_por_grupo:
            principais_por_grupo["morte"] = "morte_whole_life"

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

        # Dentro de grupo_exclusivo: só a "principal" fica ativa por default.
        # As outras linhas continuam visíveis no blend (LP pode trocar com 1
        # clique) mas não contam pro subtotal estimado.
        ge = L.get("grupo_exclusivo")
        if ge and principais_por_grupo.get(ge) != L["id"]:
            ativo_default = False

        # Linhas com restricao_profissao só ativam por default se a profissão
        # do cliente combina (ex: IPTA Estendida exclusiva médico/dentista).
        # O LP ainda pode ativar manualmente — só não conta no subtotal default.
        restr = L.get("restricao_profissao") or []
        if restr:
            import re as _re
            prof = str(cliente.get("profissao") or "").lower()
            pattern = "|".join(_re.escape(s) for s in restr)
            if not (prof and _re.search(pattern, prof)):
                ativo_default = False

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

    # Aviso global Privet se Whole Life é a principal e capital < R$1MM
    # (prêmio mensal mínimo Privet = R$400 → ajustar capital ou modelo)
    avisos_modelo = []
    if principais_por_grupo.get("morte") == "morte_whole_life":
        for L in linhas if False else _LINHAS_COMPARATIVAS:
            if L["id"] != "morte_whole_life":
                continue
            mag = L.get("mag") or {}
            if mag.get("aviso_privet"):
                avisos_modelo.append({
                    "nivel":    "aviso",
                    "linha_id": "morte_whole_life",
                    "mensagem": mag.get("aviso_privet"),
                })

    grid = {
        "cliente": {
            "nome":           cliente.get("nome", ""),
            "idade":          idade,
            "renda_mensal":   renda,
            "tipo_cobertura": tipo_cobertura,
            "profissao":      cliente.get("profissao", ""),
        },
        "modo_simplificado": modo_simplificado or None,
        "avisos_modelo": avisos_modelo,
        "linhas": linhas,
    }

    # ── Aplica clamps oficiais (renda/idade/profissão) e gera avisos do
    # auditor. Mantém capital_sugerido intocado — só ajusta capital_aplicado
    # por seguradora e marca avisos pro LP ver "por quê" no resumo.
    try:
        from automacao.auditor_catalogo import aplicar_clamps_no_grid, auditar_planejamento
        grid = aplicar_clamps_no_grid(cliente, grid)
        grid["avisos_auditor"] = auditar_planejamento(cliente, grid)
    except Exception as e:  # noqa: BLE001
        grid["avisos_auditor"] = [{
            "nivel": "aviso",
            "linha_id": "_global",
            "mensagem": f"auditor falhou: {e}",
        }]

    # ── Aplica template profissional (Polícia, Piloto, Médico, etc) ──────
    try:
        from automacao.profissoes import aplicar_template_na_grid
        grid = aplicar_template_na_grid(cliente, grid)
    except Exception as e:  # noqa: BLE001
        grid["template_profissao"] = {
            "id": None,
            "rotulo": "Falha na detecção",
            "erro": str(e)[:200],
        }
    return grid


def relatorio_catalogo() -> dict:
    """Wrapper para o endpoint /diagnostico/catalogo — roda o auditor estático
    contra _LINHAS_COMPARATIVAS e devolve o relatório."""
    from automacao.auditor_catalogo import auditar_catalogo
    return auditar_catalogo(_LINHAS_COMPARATIVAS)


def gerar_resumo_auditavel(cliente: dict, grid: dict) -> str:
    """Gera markdown explicando por que cada linha foi escolhida + as fontes
    PDF citadas. Vai pro UI do LP (Tela 4) e pode ser copiado pra apresentar
    ao cliente.

    Inclui:
      - Header com perfil do cliente + template profissional detectado
      - Lista de linhas ATIVAS com: seguradora escolhida, capital, prêmio,
        motivo do clamp (se houve) e fonte PDF
      - Avisos do auditor com fontes
      - Rodapé com as fontes oficiais usadas (Manual Azos + Dicas MAG)
    """
    import io
    out = io.StringIO()
    cli = grid.get("cliente") or {}
    tpl = grid.get("template_profissao") or {}
    avisos = grid.get("avisos_auditor") or []
    avisos_modelo = grid.get("avisos_modelo") or []
    modo = grid.get("modo_simplificado") or "completo"

    # ── Header ────────────────────────────────────────────────────────────
    nome = cli.get("nome") or "—"
    out.write(f"# Planejamento Blend Seguros — {nome}\n\n")
    out.write(f"**Idade:** {cli.get('idade','?')} anos   ")
    out.write(f"**Renda mensal:** R$ {float(cli.get('renda_mensal') or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    out.write(f"   **Profissão:** {cli.get('profissao') or '—'}\n\n")
    out.write(f"**Tipo de cobertura:** {cli.get('tipo_cobertura','mix')}   ")
    out.write(f"**Modo:** {modo}\n\n")
    if tpl.get("rotulo"):
        out.write(f"**Template profissional detectado:** {tpl.get('rotulo')}\n")
        if tpl.get("modelo_mag_obs"):
            out.write(f"> {tpl.get('modelo_mag_obs')}\n")
        if tpl.get("azos_obs"):
            out.write(f"> AZOS — {tpl.get('azos_obs')}\n")
        out.write("\n")

    # ── Avisos globais (modelo + auditor) ─────────────────────────────────
    if avisos_modelo or avisos:
        out.write("## ⚠ Avisos do Auditor\n\n")
        for a in avisos_modelo + avisos:
            nivel = a.get("nivel", "aviso").upper()
            out.write(f"- **[{nivel}]** _{a.get('linha_id','')}_: {a.get('mensagem','')}\n")
        out.write("\n")

    # ── Linhas escolhidas ─────────────────────────────────────────────────
    out.write("## Coberturas escolhidas\n\n")
    fontes_citadas: set[str] = set()
    for L in grid.get("linhas", []):
        if not L.get("ativo_default"):
            continue
        esc = L.get("escolhido_default")
        seg = L.get(esc) if esc else None
        if not seg or not seg.get("disponivel"):
            # Sem seguradora escolhida — pula
            continue
        produto = seg.get("produto") or L.get("nome")
        cap = seg.get("capital_aplicado") or L.get("capital_sugerido")
        premio = seg.get("premio_estimado")
        fonte_pdf = seg.get("fonte_pdf", "(fonte não declarada)")
        if fonte_pdf and fonte_pdf != "(fonte não declarada)":
            fontes_citadas.add(fonte_pdf)
        out.write(f"### {L.get('nome')}\n")
        out.write(f"- **Seguradora:** {esc.upper()} — {produto}\n")
        unidade = L.get("unidade") or "R$"
        if "R$/dia" in unidade:
            out.write(f"- **Capital:** R$ {cap:,}/dia\n".replace(",", "."))
        elif "Capital fixo" in unidade:
            out.write(f"- **Capital:** R$ {cap:,} (fixo)\n".replace(",", "."))
        else:
            out.write(f"- **Capital:** R$ {cap:,}\n".replace(",", "."))
        if premio:
            out.write(f"- **Prêmio estimado:** R$ {premio:.2f}/mês\n")
        if seg.get("clamp_motivo"):
            out.write(f"- **Clamp:** {seg['clamp_motivo']}\n")
        if seg.get("capital_original") and seg.get("capital_original") != cap:
            out.write(f"  - Capital original calculado: R$ {seg['capital_original']:,}\n".replace(",", "."))
        if seg.get("aviso_privet"):
            out.write(f"- **⚠ {seg['aviso_privet']}**\n")
        if seg.get("obs"):
            out.write(f"- _Obs:_ {seg['obs']}\n")
        out.write(f"- **Fonte:** {fonte_pdf}\n\n")

    # ── Linhas opcionais (visíveis mas inativas por default) ──────────────
    inativas = [L for L in grid.get("linhas", []) if not L.get("ativo_default")]
    if inativas:
        out.write("## Coberturas disponíveis (não ativas por padrão)\n\n")
        for L in inativas:
            linhas_seg = []
            for seg in ("azos", "mag"):
                info = L.get(seg) or {}
                if info.get("disponivel"):
                    cap = info.get("capital_aplicado") or "—"
                    pm  = info.get("premio_estimado")
                    pm_txt = f"R$ {pm:.2f}/mês" if pm else "—"
                    linhas_seg.append(f"{seg.upper()}: R$ {cap:,} ({pm_txt})".replace(",", "."))
                elif info.get("motivo_indisponivel"):
                    linhas_seg.append(f"{seg.upper()}: indisponível ({info['motivo_indisponivel'][:80]})")
            out.write(f"- **{L.get('nome')}** — {' | '.join(linhas_seg) or 'sem oferta'}\n")
        out.write("\n")

    # ── Fontes oficiais usadas ────────────────────────────────────────────
    if fontes_citadas:
        out.write("## Fontes oficiais\n\n")
        for f in sorted(fontes_citadas):
            out.write(f"- {f}\n")
    return out.getvalue()


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
