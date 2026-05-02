"""Automação AZOS — portal corretores."""
from __future__ import annotations
import os, re, asyncio
from playwright.async_api import Page
from .base import novo_browser, fechar_browser, clicar_continuar, extrair_valor_monetario, resolver_captcha
from models import Cobertura, ResultadoFase1, ResultadoCotacao

URL_LOGIN = "https://corretores.azos.com.br/login"
URL_SIM   = "https://contratacao.azos.com.br/simulacao/dados-pessoais"
EMAIL = os.getenv("AZOS_EMAIL", "")
SENHA = os.getenv("AZOS_SENHA", "")

# Sessões abertas aguardando fase 2
_SESSOES: dict[str, dict] = {}


# ─── FASE 1: login + dados pessoais + coleta de coberturas ───────────────────

async def fase1_coletar_coberturas(dados: dict, headless: bool = True) -> ResultadoFase1:
    """
    Faz login, preenche dados do cliente e extrai todas as coberturas disponíveis.
    Mantém a sessão aberta (salva em _SESSOES[session_id]) para a fase 2.
    """
    pw, browser, ctx, page = await novo_browser(headless)
    session_id = dados.get("session_id", "azos-" + str(id(page)))
    try:
        print(f"[azos] iniciando fase1 session={session_id}", flush=True)

        # Login — aguarda form estar pronto antes de preencher
        await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_selector('input[name="email"]', timeout=15_000)
        await page.fill('input[name="email"]', EMAIL)
        await page.fill('input[name="password"]', SENHA)
        await resolver_captcha(page)
        await page.click('button[type="submit"]')
        await page.wait_for_timeout(1000)
        await page.wait_for_url("**/dashboard**", timeout=30_000)
        print(f"[azos] login ok", flush=True)

        # Navegação para simulação
        await page.goto(URL_SIM, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1500)

        # Seleciona "Novo cliente" (é um elemento de texto, não um <button>)
        await page.locator('text="Novo cliente"').click()
        await page.wait_for_timeout(800)

        # Preenche dados pessoais
        await _preencher_dados_pessoais(page, dados)

        # Avança para coberturas
        await page.locator('button:has-text("Continuar")').click()
        await page.wait_for_timeout(4000)
        await page.wait_for_load_state("domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(1500)

        # Aguarda React renderizar os cards de cobertura
        try:
            await page.wait_for_selector('button[role="switch"]', timeout=10_000)
        except Exception:
            pass

        coberturas = await _extrair_coberturas(page)
        print(f"[azos] {len(coberturas)} coberturas extraídas | url={page.url}", flush=True)

        # Guarda sessão para fase 2
        _SESSOES[session_id] = {
            "pw": pw, "browser": browser, "ctx": ctx, "page": page,
            "dados": dados,
        }

        return ResultadoFase1(
            seguradora="azos",
            ok=True,
            coberturas=coberturas,
            session_id=session_id,
        )

    except Exception as e:
        print(f"[azos] ERRO fase1: {e}", flush=True)
        await fechar_browser(pw, browser)
        return ResultadoFase1(seguradora="azos", ok=False, erro=str(e))


async def _preencher_dados_pessoais(page: Page, d: dict):
    campos = {
        'input[name="name"], input[placeholder*="nome" i]':    d.get("nome", ""),
        'input[name="birthdate"], input[placeholder*="nasc" i]': d.get("nascimento", ""),
        'input[name="cpf"]':  d.get("cpf", ""),
        'input[name="phone"]': d.get("telefone", ""),
        'input[name="email"]': d.get("email", ""),
    }
    for sel, val in campos.items():
        if not val:
            continue
        try:
            inp = page.locator(sel).first
            if await inp.count():
                await inp.scroll_into_view_if_needed()
                await inp.click()
                await inp.fill(val)
                await page.wait_for_timeout(200)
        except Exception:
            pass

    # Selects (sexo, estado civil, profissão, fumante, renda)
    await _preencher_selects(page, d)
    await page.wait_for_timeout(500)


async def _preencher_selects(page: Page, d: dict):
    mapeamentos = [
        ('select[name="gender"], [aria-label*="sexo" i]',       d.get("sexo", "M")),
        ('select[name="marital"], [aria-label*="estado civil" i]', d.get("estado_civil", "")),
        ('input[name="income"], input[placeholder*="renda" i]', d.get("renda_mensal", "5000")),
    ]
    for sel, val in mapeamentos:
        if not val:
            continue
        try:
            el = page.locator(sel).first
            if not await el.count():
                continue
            tag = await el.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                await el.select_option(label=val)
            else:
                await el.fill(val)
            await page.wait_for_timeout(150)
        except Exception:
            pass


async def _extrair_coberturas(page: Page) -> list[Cobertura]:
    coberturas = []
    try:
        cards = await page.query_selector_all('*:has(button[role="switch"])')
        for card in cards:
            try:
                h = await card.query_selector("h2, h3, h4, h5")
                if not h:
                    continue
                nome = (await h.inner_text()).strip()
                if not nome or len(nome) < 3:
                    continue

                # Min/max do slider
                slider = await card.query_selector('[role="slider"]')
                v_min, v_max = 50_000.0, 5_000_000.0
                if slider:
                    try:
                        v_min = float(await slider.get_attribute("aria-valuemin") or v_min)
                        v_max = float(await slider.get_attribute("aria-valuemax") or v_max)
                    except Exception:
                        pass

                # Prêmio de referência: ativa o switch e lê o prêmio
                premio_ref = 0.0
                sw = await card.query_selector('button[role="switch"]')
                if sw:
                    checked = await sw.get_attribute("aria-checked")
                    if checked != "true":
                        await sw.scroll_into_view_if_needed()
                        await sw.click()
                        await page.wait_for_timeout(800)
                    txt = await page.inner_text("body")
                    val = extrair_valor_monetario(txt)
                    if val and 5 < val < 2000:
                        premio_ref = val
                    # Desativa para não acumular
                    if checked != "true":
                        await sw.click()
                        await page.wait_for_timeout(400)

                coberturas.append(Cobertura(
                    id=f"azos_{re.sub(r'[^a-z0-9]', '_', nome.lower()[:30])}",
                    nome=nome,
                    descricao="",
                    valor_min=v_min,
                    valor_max=v_max,
                    premio_referencia=premio_ref,
                    seguradora="azos",
                ))
            except Exception:
                pass
    except Exception as e:
        print(f"[azos] erro ao extrair coberturas: {e}", flush=True)

    return coberturas


# ─── FASE 2: seleciona coberturas do blend e finaliza cotação ────────────────

async def fase2_finalizar(session_id: str, selecoes: list[dict]) -> list[ResultadoCotacao]:
    """
    Recebe a lista de coberturas selecionadas pelo corretor e finaliza a cotação.
    selecoes = [{"nome": str, "valor": float}, ...]
    """
    sessao = _SESSOES.get(session_id)
    if not sessao:
        return [ResultadoCotacao(
            seguradora="azos", cobertura_nome="", valor_capital=0,
            premio_mensal=0, erro="Sessão expirada — reinicie a coleta"
        )]

    page: Page = sessao["page"]
    resultados: list[ResultadoCotacao] = []

    try:
        # Garante que está na página de coberturas
        if "coberturas" not in page.url:
            await page.goto(
                "https://contratacao.azos.com.br/simulacao/coberturas",
                wait_until="domcontentloaded", timeout=20_000
            )
            await page.wait_for_timeout(1500)

        # Desativa todos os switches antes de selecionar
        await page.evaluate("""() => {
            document.querySelectorAll('button[role="switch"][aria-checked="true"]')
                .forEach(s => s.click());
        }""")
        await page.wait_for_timeout(600)

        # Seleciona cada cobertura com o valor escolhido
        for sel in selecoes:
            await _selecionar_cobertura(page, sel["nome"], float(sel["valor"]))

        await page.wait_for_timeout(1000)
        await clicar_continuar(page)
        await page.wait_for_timeout(3000)

        # Preenche DPS e demais etapas até o resultado
        premio = await _aguardar_resultado(page)

        for sel in selecoes:
            resultados.append(ResultadoCotacao(
                seguradora="azos",
                cobertura_nome=sel["nome"],
                valor_capital=float(sel["valor"]),
                premio_mensal=premio / len(selecoes) if premio else 0,
                link_proposta=page.url,
            ))

    except Exception as e:
        print(f"[azos] ERRO fase2: {e}", flush=True)
        resultados.append(ResultadoCotacao(
            seguradora="azos", cobertura_nome="Erro", valor_capital=0,
            premio_mensal=0, erro=str(e)
        ))
    finally:
        sess = _SESSOES.pop(session_id, None)
        if sess:
            await fechar_browser(sess["pw"], sess["browser"])

    return resultados


async def _selecionar_cobertura(page: Page, nome: str, valor: float):
    nome_curto = nome[:30]
    switch = page.locator(f'*:has(h3:has-text("{nome_curto}")) button[role="switch"]').first
    if not await switch.count():
        return

    await switch.scroll_into_view_if_needed()
    if await switch.get_attribute("aria-checked") != "true":
        await switch.click()
        await page.wait_for_timeout(1000)

    if valor <= 0:
        return

    # Localiza o input de valor no mesmo card
    for sel in [
        f'*:has(h3:has-text("{nome_curto}")) input[type="tel"]',
        f'*:has(h3:has-text("{nome_curto}")) input[type="number"]',
        f'*:has(h3:has-text("{nome_curto}")) [role="slider"]',
    ]:
        inp = page.locator(sel).first
        if not await inp.count():
            continue

        await inp.scroll_into_view_if_needed()

        try:
            await inp.wait_for(state="enabled", timeout=4000)
            await inp.click()
        except Exception:
            bbox = await inp.bounding_box()
            if bbox:
                await page.mouse.click(bbox["x"] + bbox["width"]/2, bbox["y"] + bbox["height"]/2)

        await page.wait_for_timeout(150)
        await page.keyboard.press("Control+a")
        await page.keyboard.type(str(int(valor)), delay=25)
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(400)
        print(f"[azos] {nome_curto[:20]} = {int(valor)}", flush=True)
        break


async def _aguardar_resultado(page: Page) -> float | None:
    """Avança pelas etapas de DPS/saúde/endereço até chegar no resultado final."""
    import re as _re
    for _ in range(40):
        await page.wait_for_timeout(1500)
        url = page.url
        txt = (await page.inner_text("body")).lower()

        if any(k in url for k in ["resultado", "proposta", "checkout", "pagamento", "sucesso"]):
            break

        # Responde "Não" em perguntas de saúde
        nao_btn = page.locator('button:has-text("Não"), label:has-text("Não")').first
        if await nao_btn.count():
            await nao_btn.click()
            await page.wait_for_timeout(400)
            continue

        avancou = await clicar_continuar(page)
        if not avancou:
            break

    txt = await page.inner_text("body")
    m = _re.search(r'R\$\s*([\d.]+),([\d]{2})', txt)
    if m:
        return float(m.group(1).replace('.', '') + '.' + m.group(2))
    return None
