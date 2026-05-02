"""Automação MAG Seguros — Venda Digital."""
from __future__ import annotations
import os, re
from playwright.async_api import Page
from .base import novo_browser, fechar_browser, clicar_continuar, extrair_valor_monetario
from models import Cobertura, ResultadoFase1, ResultadoCotacao

URL_LOGIN = "https://canaldocorretor.mag.com.br/canal-do-corretor/login.htm"
CNPJ  = os.getenv("MAG_CNPJ", "")
SENHA = os.getenv("MAG_SENHA", "")

_SESSOES: dict[str, dict] = {}


async def fase1_coletar_coberturas(dados: dict, headless: bool = True) -> ResultadoFase1:
    pw, browser, ctx, page = await novo_browser(headless)
    session_id = "mag-" + str(id(page))
    try:
        print(f"[mag] iniciando fase1", flush=True)
        await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(2000)

        # Login com CNPJ
        await _login(page)
        print(f"[mag] login ok", flush=True)

        # Navega até simulação e preenche dados
        await _navegar_simulacao(page, dados)

        coberturas = await _extrair_coberturas(page)
        print(f"[mag] {len(coberturas)} coberturas extraídas", flush=True)

        _SESSOES[session_id] = {"pw": pw, "browser": browser, "ctx": ctx, "page": page, "dados": dados}
        return ResultadoFase1(seguradora="mag", ok=True, coberturas=coberturas, session_id=session_id)

    except Exception as e:
        print(f"[mag] ERRO fase1: {e}", flush=True)
        await fechar_browser(pw, browser)
        return ResultadoFase1(seguradora="mag", ok=False, erro=str(e))


async def _login(page: Page):
    # Redireciona para identidade.mag.com.br — campos name=Username e name=Password
    await page.wait_for_selector('input[name="Username"]', timeout=15_000)
    await page.fill('input[name="Username"]', CNPJ)
    await page.fill('input[name="Password"]', SENHA)

    for sel in ['button:has-text("ACESSAR")', 'button[type="submit"]', 'input[type="submit"]']:
        btn = page.locator(sel).first
        if await btn.count():
            await btn.click()
            break

    # Aguarda redirect para o portal do produtor
    await page.wait_for_url("**/plataformadosprodutores**", timeout=30_000)


async def _navegar_simulacao(page: Page, dados: dict):
    # Procura menu de simulação/cotação
    for txt in ["Simular", "Cotar", "Nova cotação", "Seguro de Vida"]:
        btn = page.locator(f'a:has-text("{txt}"), button:has-text("{txt}")').first
        if await btn.count():
            await btn.click()
            await page.wait_for_timeout(2000)
            break

    # Preenche campos de dados pessoais
    campos = [
        (['input[name="nome"]', 'input[placeholder*="nome" i]'],  dados.get("nome", "")),
        (['input[name="cpf"]', 'input[placeholder*="cpf" i]'],    dados.get("cpf", "")),
        (['input[name="nascimento"]', 'input[placeholder*="nasc" i]'], dados.get("nascimento", "")),
        (['input[name="email"]', 'input[type="email"]'],           dados.get("email", "")),
        (['input[name="telefone"]', 'input[placeholder*="fone" i]'], dados.get("telefone", "")),
    ]
    for sels, val in campos:
        if not val:
            continue
        for sel in sels:
            inp = page.locator(sel).first
            if await inp.count():
                await inp.fill(val)
                await page.wait_for_timeout(150)
                break

    await clicar_continuar(page)
    await page.wait_for_timeout(2000)


async def _extrair_coberturas(page: Page) -> list[Cobertura]:
    coberturas = []
    txt = await page.inner_text("body")

    # Tenta extrair coberturas de cards/seções
    try:
        titulos = await page.query_selector_all('h3, h4, [class*="cobertura"], [class*="coverage"]')
        for el in titulos:
            nome = (await el.inner_text()).strip()
            if not nome or len(nome) < 5:
                continue
            coberturas.append(Cobertura(
                id=f"mag_{re.sub(r'[^a-z0-9]', '_', nome.lower()[:30])}",
                nome=nome,
                descricao="",
                valor_min=50_000.0,
                valor_max=3_000_000.0,
                premio_referencia=0.0,
                seguradora="mag",
            ))
    except Exception as e:
        print(f"[mag] erro ao extrair coberturas: {e}", flush=True)

    return coberturas


async def fase2_finalizar(session_id: str, selecoes: list[dict]) -> list[ResultadoCotacao]:
    sessao = _SESSOES.get(session_id)
    if not sessao:
        return [ResultadoCotacao(
            seguradora="mag", cobertura_nome="", valor_capital=0,
            premio_mensal=0, erro="Sessão expirada"
        )]

    page: Page = sessao["page"]
    resultados = []

    try:
        for sel in selecoes:
            nome, valor = sel["nome"], float(sel["valor"])
            # Seleciona a cobertura por texto
            card = page.locator(f'*:has-text("{nome[:30]}")').first
            if await card.count():
                await card.scroll_into_view_if_needed()
                chk = page.locator(f'*:has-text("{nome[:30]}") input[type="checkbox"]').first
                if await chk.count() and not await chk.is_checked():
                    await chk.click()
                    await page.wait_for_timeout(400)

                # Define valor no input mais próximo
                inp = page.locator(f'*:has-text("{nome[:30]}") input[type="number"], '
                                   f'*:has-text("{nome[:30]}") input[type="tel"]').first
                if await inp.count():
                    try:
                        await inp.wait_for(state="enabled", timeout=3000)
                        await inp.click()
                    except Exception:
                        bbox = await inp.bounding_box()
                        if bbox:
                            await page.mouse.click(bbox["x"] + bbox["width"]/2, bbox["y"] + bbox["height"]/2)
                    await page.keyboard.press("Control+a")
                    await page.keyboard.type(str(int(valor)), delay=25)
                    await page.keyboard.press("Tab")
                    await page.wait_for_timeout(300)

        await clicar_continuar(page)
        await page.wait_for_timeout(3000)

        txt = await page.inner_text("body")
        m = re.search(r'R\$\s*([\d.]+),([\d]{2})', txt)
        premio = float(m.group(1).replace('.', '') + '.' + m.group(2)) if m else 0.0

        for sel in selecoes:
            resultados.append(ResultadoCotacao(
                seguradora="mag",
                cobertura_nome=sel["nome"],
                valor_capital=float(sel["valor"]),
                premio_mensal=premio / max(len(selecoes), 1),
                link_proposta=page.url,
            ))

    except Exception as e:
        print(f"[mag] ERRO fase2: {e}", flush=True)
        resultados.append(ResultadoCotacao(
            seguradora="mag", cobertura_nome="Erro", valor_capital=0,
            premio_mensal=0, erro=str(e)
        ))
    finally:
        sess = _SESSOES.pop(session_id, None)
        if sess:
            await fechar_browser(sess["pw"], sess["browser"])

    return resultados
