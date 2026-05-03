"""Automação Omint — Cotador Athena (Quasar SPA)."""
from __future__ import annotations
import os, re
from playwright.async_api import Page
from .base import novo_browser, fechar_browser, clicar_continuar
from models import Cobertura, ResultadoFase1, ResultadoCotacao

URL_LOGIN   = "https://omint.seg.br/athena/#/login"
URL_COTACAO = "https://omint.seg.br/athena/#/proposta/cotacao/adicionar"
USUARIO = os.getenv("OMINT_USUARIO", "")
SENHA   = os.getenv("OMINT_SENHA",   "")

_SESSOES: dict[str, dict] = {}


async def fase1_coletar_coberturas(dados: dict, headless: bool = True) -> ResultadoFase1:
    pw, browser, ctx, page = await novo_browser(headless)
    session_id = "omint-" + str(id(page))
    try:
        print(f"[omint] iniciando fase1", flush=True)
        await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(2000)

        await _login(page)
        print(f"[omint] login ok → {page.url}", flush=True)

        await _navegar_simulacao(page, dados)

        coberturas = await _extrair_coberturas(page)
        print(f"[omint] {len(coberturas)} coberturas extraídas", flush=True)

        _SESSOES[session_id] = {"pw": pw, "browser": browser, "ctx": ctx, "page": page, "dados": dados}
        return ResultadoFase1(seguradora="omint", ok=True, coberturas=coberturas, session_id=session_id)

    except Exception as e:
        print(f"[omint] ERRO fase1: {e}", flush=True)
        await fechar_browser(pw, browser)
        return ResultadoFase1(seguradora="omint", ok=False, erro=str(e))


async def _login(page: Page):
    await page.wait_for_selector('input[type="text"]', timeout=10_000)
    # Preenche usuário e faz Tab para disparo blur/Vue reactivity
    await page.locator('input[type="text"]').first.click()
    await page.locator('input[type="text"]').first.fill(USUARIO)
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(300)
    # Preenche senha e submete com Enter (evita clicar no botão que o Vue pode desabilitar)
    await page.locator('input[type="password"]').first.click()
    await page.locator('input[type="password"]').first.fill(SENHA)
    await page.wait_for_timeout(300)
    await page.keyboard.press("Enter")

    # Aguarda sair da página de login — polling até 15s (headless pode ser mais lento)
    for _ in range(30):
        await page.wait_for_timeout(500)
        if "login" not in page.url:
            break
    else:
        raise Exception("Login OMINT falhou — verifique credenciais")

    # Tutorial onboarding — botão "Avançar" bloqueado por overlay, force=True bypassa
    for _ in range(8):
        btn = page.locator('button:has-text("Avançar")').first
        if await btn.count():
            await btn.click(force=True)
            await page.wait_for_timeout(600)
        else:
            break


async def _navegar_simulacao(page: Page, dados: dict):
    # Navega diretamente para a página de cotação
    await page.goto(URL_COTACAO, wait_until="domcontentloaded", timeout=30_000)
    # Aguarda o primeiro input do form (q-field__native) aparecer
    await page.wait_for_selector('input.q-field__native', timeout=10_000)
    await page.wait_for_timeout(3000)  # aguarda inicialização + carregamento do dropdown Corretor

    # Descarta modais de tutorial que reaparecem na navegação (force=True bypassa overlay)
    for _ in range(10):
        btn = page.locator('button:has-text("Avançar")').first
        if await btn.count() and await btn.is_visible():
            await btn.click(force=True)
            await page.wait_for_timeout(400)
        else:
            break

    # Fecha tooltip "Nome civil" se aparecer
    fechar = page.locator('button:has-text("Fechar")').first
    if await fechar.count() and await fechar.is_visible():
        await fechar.click(force=True)
        await page.wait_for_timeout(400)

    # Nome civil — primeiro input.q-field__native visível (não o search field)
    nome_inp = page.locator('input.q-field__native[type="text"]').first
    await nome_inp.wait_for(state="visible", timeout=10_000)
    await nome_inp.click()
    await nome_inp.press_sequentially(dados.get("nome", ""), delay=30)
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(800)

    # Nascimento — aguarda o input type="date"
    nasc_raw = dados.get("nascimento", "")
    if "/" in nasc_raw:
        d, m, y = nasc_raw.split("/")
        nasc_iso = f"{y}-{m}-{d}"
    else:
        nasc_iso = nasc_raw
    nasc_inp = page.locator('input[type="date"]').first
    await nasc_inp.wait_for(state="visible", timeout=10_000)
    await nasc_inp.click()
    await nasc_inp.fill(nasc_iso)
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(800)

    # Renda — máscara monetária direita→esquerda; NÃO limpa antes, só clica e digita
    # Para R$ 5.000,00 (5000 reais) é preciso digitar "500000" (valor em centavos)
    renda_val = dados.get("renda_mensal", "5000")
    try:
        renda_int = int(float(str(renda_val).replace(",", ".").replace(" ", "")))
    except Exception:
        renda_int = 5000
    renda_digits = str(renda_int) + "00"  # ex.: 5000 → "500000"

    all_native = page.locator('input.q-field__native')
    cnt = await all_native.count()
    for i in range(cnt):
        inp = all_native.nth(i)
        try:
            tp = await inp.get_attribute("type") or "text"
            if tp in ("date", "email", "tel", "search", "password"):
                continue
            v = await inp.input_value()
            if "R$" in v or "\xa0" in v:
                await inp.click()
                await inp.press_sequentially(renda_digits, delay=50)
                await page.keyboard.press("Tab")
                await page.wait_for_timeout(300)
                break
        except Exception:
            pass

    # Gênero
    sexo = dados.get("sexo", "M")
    sexo_label = "Feminino" if sexo.upper() in ("F", "FEMININO") else "Masculino"
    await page.locator(f'button:has-text("{sexo_label}")').first.click(force=True)
    await page.wait_for_timeout(200)

    # Profissional saúde → NÃO (nth(0))
    await page.locator('button:has-text("NÃO")').nth(0).click(force=True)
    await page.wait_for_timeout(200)

    # Fumante → Não Fumante
    await page.locator('button:has-text("Não Fumante")').first.click(force=True)
    await page.wait_for_timeout(200)

    # Informar contatos → NÃO (nth(1))
    try:
        await page.locator('button:has-text("NÃO")').nth(1).click(force=True)
        await page.wait_for_timeout(200)
    except Exception:
        pass

    # Corretor — q-select obrigatório; sem ele o Quasar isActionable permanece false
    corretor_sel = page.locator('.q-select').first
    await corretor_sel.click(force=True)
    await page.wait_for_timeout(1500)
    opts = await page.query_selector_all('.q-menu .q-item, .q-virtual-scroll .q-item, [role="option"]')
    if opts:
        await opts[0].click(force=True)
        print(f"[omint] Corretor selecionado", flush=True)
        await page.wait_for_timeout(500)
    else:
        print(f"[omint] AVISO: dropdown Corretor vazio, prosseguindo sem seleção", flush=True)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)

    # Log estado do botão para diagnóstico
    cont = page.locator('button[data-cy="btn-continuar"], button:has-text("Continuar")').first
    if await cont.count():
        cls = await cont.get_attribute("class") or ""
        dis = await cont.get_attribute("disabled")
        print(f"[omint] Continuar: disabled={dis!r} actionable={'q-btn--actionable' in cls}", flush=True)

    # Tenta clique normal primeiro; se não responder usa force
    try:
        await cont.click(timeout=2000)
    except Exception:
        await cont.click(force=True)
    await page.wait_for_timeout(3000)
    print(f"[omint] pós-dados pessoais → {page.url}", flush=True)


async def _extrair_coberturas(page: Page) -> list[Cobertura]:
    coberturas = []
    try:
        try:
            await page.wait_for_selector(
                'h3, h4, [class*="cobertura"], [class*="produto"], [class*="card"]',
                timeout=8_000
            )
        except Exception:
            pass

        vistos: set[str] = set()
        titulos = await page.query_selector_all(
            'h3, h4, [class*="cobertura"], [class*="plano"], [class*="produto"], td:first-child'
        )
        for el in titulos:
            nome = (await el.inner_text()).strip()
            if not nome or len(nome) < 5 or nome in vistos:
                continue
            vistos.add(nome)
            coberturas.append(Cobertura(
                id=f"omint_{re.sub(r'[^a-z0-9]', '_', nome.lower()[:30])}",
                nome=nome,
                descricao="",
                valor_min=50_000.0,
                valor_max=3_000_000.0,
                premio_referencia=0.0,
                seguradora="omint",
            ))
    except Exception as e:
        print(f"[omint] erro ao extrair coberturas: {e}", flush=True)
    return coberturas


async def fase2_finalizar(session_id: str, selecoes: list[dict]) -> list[ResultadoCotacao]:
    sessao = _SESSOES.get(session_id)
    if not sessao:
        return [ResultadoCotacao(
            seguradora="omint", cobertura_nome="", valor_capital=0,
            premio_mensal=0, erro="Sessão expirada"
        )]

    page: Page = sessao["page"]
    resultados = []

    try:
        for sel in selecoes:
            nome, valor = sel["nome"], float(sel["valor"])
            chk = page.locator(f'*:has-text("{nome[:30]}") input[type="checkbox"]').first
            if await chk.count() and not await chk.is_checked():
                await chk.click()
                await page.wait_for_timeout(400)

            inp = page.locator(
                f'*:has-text("{nome[:30]}") input[type="number"], '
                f'*:has-text("{nome[:30]}") input[type="tel"]'
            ).first
            if await inp.count():
                try:
                    await inp.wait_for(state="enabled", timeout=3000)
                    await inp.click()
                except Exception:
                    bbox = await inp.bounding_box()
                    if bbox:
                        await page.mouse.click(bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2)
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
                seguradora="omint",
                cobertura_nome=sel["nome"],
                valor_capital=float(sel["valor"]),
                premio_mensal=premio / max(len(selecoes), 1),
                link_proposta=page.url,
            ))

    except Exception as e:
        print(f"[omint] ERRO fase2: {e}", flush=True)
        resultados.append(ResultadoCotacao(
            seguradora="omint", cobertura_nome="Erro", valor_capital=0,
            premio_mensal=0, erro=str(e)
        ))
    finally:
        sess = _SESSOES.pop(session_id, None)
        if sess:
            await fechar_browser(sess["pw"], sess["browser"])

    return resultados
