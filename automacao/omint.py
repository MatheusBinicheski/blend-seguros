"""Automação Omint — Cotador Athena (Quasar SPA)."""
from __future__ import annotations
import os, re
from playwright.async_api import Page
from .base import novo_browser, fechar_browser, clicar_continuar
from models import Cobertura, ResultadoFase1, ResultadoCotacao, SondagemPreco

URL_LOGIN   = "https://omint.seg.br/athena/#/login"
URL_COTACAO = "https://omint.seg.br/athena/#/proposta/cotacao/adicionar"
USUARIO = os.getenv("OMINT_USUARIO", "")
SENHA   = os.getenv("OMINT_SENHA",   "")

_SESSOES: dict[str, dict] = {}


async def fase1_coletar_coberturas(dados: dict, headless: bool = True) -> ResultadoFase1:
    # OMINT usa Quasar SPA que não renderiza corretamente em headless=True no Railway.
    # Railway tem display virtual (Xvfb) que suporta headless=False.
    pw, browser, ctx, page = await novo_browser(headless=False, extra_args=["--disable-gpu", "--disable-extensions"])
    session_id = "omint-" + str(id(page))
    try:
        print(f"[omint] iniciando fase1", flush=True)
        await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(10_000)  # Quasar SPA demora a renderizar

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
    await page.wait_for_selector('input[type="text"]', timeout=90_000)
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

    # Aguarda sair da página de login — polling até 30s
    for _ in range(60):
        await page.wait_for_timeout(500)
        if "login" not in page.url:
            break
    else:
        raise Exception(f"Login OMINT timeout 30s — url={page.url}")

    # Tutorial onboarding — botão "Avançar" bloqueado por overlay, force=True bypassa
    for _ in range(8):
        btn = page.locator('button:has-text("Avançar")').first
        if await btn.count():
            await btn.click(force=True)
            await page.wait_for_timeout(600)
        else:
            break


async def _navegar_simulacao(page: Page, dados: dict):
    await page.goto(URL_COTACAO, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_selector('input.q-field__native', timeout=30_000)
    await page.wait_for_timeout(2000)

    # Descarta modais de tutorial se aparecerem
    for _ in range(8):
        btn = page.locator('button:has-text("Avançar"), button:has-text("Fechar")').first
        if await btn.count() and await btn.is_visible():
            await btn.click(force=True)
            await page.wait_for_timeout(400)
        else:
            break

    # Nome civil — primeiro input text visível
    nome_inp = page.locator('input.q-field__native[type="text"]').first
    await nome_inp.wait_for(state="visible", timeout=10_000)
    await nome_inp.click()
    await nome_inp.press_sequentially(dados.get("nome", ""), delay=30)
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(600)

    # Fecha tooltip "Nome social" que pode aparecer após Tab
    for _ in range(3):
        btn_fechar = page.locator('button:has-text("Fechar")').first
        if await btn_fechar.count() and await btn_fechar.is_visible():
            await btn_fechar.click(force=True)
            await page.wait_for_timeout(300)
        else:
            break

    # Nascimento (ISO yyyy-mm-dd) — parseia apenas dígitos (robusto a separadores/whitespace)
    nasc_raw = dados.get("nascimento", "")
    digits = re.sub(r"\D", "", nasc_raw)
    if len(digits) == 8:
        nasc_iso = f"{digits[4:8]}-{digits[2:4]}-{digits[:2]}"
    elif re.match(r"^\d{4}-\d{2}-\d{2}$", nasc_raw):
        nasc_iso = nasc_raw
    else:
        raise Exception(f"OMINT data inválida: '{nasc_raw}' (esperado DD/MM/AAAA)")
    nasc_inp = page.locator('input[type="date"]').first
    await nasc_inp.wait_for(state="visible", timeout=10_000)
    await nasc_inp.click()
    await nasc_inp.fill(nasc_iso)
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(600)

    # Renda — máscara direita→esquerda (centavos): 5000 reais → digita "500000"
    renda_val = dados.get("renda_mensal", "5000")
    try:
        renda_int = int(float(str(renda_val).replace(",", ".").replace(" ", "")))
    except Exception:
        renda_int = 5000
    renda_digits = str(renda_int) + "00"

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
                print(f"[omint] renda preenchida: {await inp.input_value()}", flush=True)
                break
        except Exception:
            pass

    # Gênero (pode já estar pré-selecionado; re-clicar não faz mal)
    sexo = dados.get("sexo", "M")
    sexo_label = "Feminino" if sexo.upper() in ("F", "FEMININO") else "Masculino"
    await page.locator(f'button:has-text("{sexo_label}")').first.click(force=True)
    await page.wait_for_timeout(200)

    # Informar contatos → NÃO (esconde campos de email/tel que são required)
    # "SIM" vem pré-selecionado — precisa trocar para NÃO
    nao_btns = page.locator('button:has-text("NÃO")')
    cnt_nao = await nao_btns.count()
    for i in range(cnt_nao):
        btn = nao_btns.nth(i)
        try:
            label = await btn.evaluate("b => b.closest('.q-field, .row, div')?.querySelector('label, span.text-weight-medium')?.innerText || ''")
        except Exception:
            label = ""
        txt = (await btn.inner_text()).strip()
        if txt == "NÃO":
            await btn.click(force=True)
            await page.wait_for_timeout(200)
    print(f"[omint] Informar contatos: NÃO clicado", flush=True)

    # Corretor — só interage se ainda não tiver valor (já vem pré-carregado normalmente)
    corretor_sel = page.locator('.q-select').first
    corretor_txt = ""
    try:
        corretor_txt = await corretor_sel.inner_text()
    except Exception:
        pass
    if not corretor_txt.strip() or len(corretor_txt.strip()) < 3:
        await corretor_sel.click(force=True)
        await page.wait_for_timeout(1500)
        opts = await page.query_selector_all('.q-menu .q-item, .q-virtual-scroll .q-item, [role="option"]')
        if opts:
            await opts[0].click(force=True)
            print(f"[omint] Corretor selecionado manualmente", flush=True)
            await page.wait_for_timeout(500)
        else:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
    else:
        print(f"[omint] Corretor já definido: {corretor_txt.strip()[:40]}", flush=True)

    await page.wait_for_timeout(1000)

    # Continuar
    cont = page.locator('button[data-cy="btn-continuar"], button:has-text("Continuar")').first
    if await cont.count():
        dis = await cont.get_attribute("disabled")
        print(f"[omint] Continuar: disabled={dis!r}", flush=True)
    try:
        await cont.click(timeout=3000)
    except Exception:
        await cont.click(force=True)

    # Aguarda SPA navegar para produtos (até 15 s)
    for _ in range(30):
        await page.wait_for_timeout(500)
        if "produtos" in page.url:
            break
    print(f"[omint] pós-dados pessoais → {page.url}", flush=True)


async def _extrair_coberturas(page: Page) -> list[Cobertura]:
    coberturas = []
    try:
        # Aguarda a página de seleção de produtos renderizar (Quasar SPA)
        try:
            await page.wait_for_selector('text=Selecione o produto', timeout=15_000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)

        # Os produtos só aparecem após interagir com o seletor "Selecione o produto".
        # Usa seletores baseados em texto e atributos nativos (role, aria-*).
        achou = False
        for tentativa in range(4):
            try:
                if tentativa == 0:
                    # Clica pelo texto do label — seletor nativo por conteúdo de texto
                    await page.locator('text=Selecione o produto').first.click(force=True)
                elif tentativa == 1:
                    # Clica no primeiro elemento com role="combobox" — ARIA nativo
                    el = page.locator('[role="combobox"]').first
                    if await el.count():
                        await el.click(force=True)
                    else:
                        # Fallback: label cujo texto contenha "Selecione"
                        await page.locator('label:has-text("Selecione")').first.click(force=True)
                elif tentativa == 2:
                    # Teclado: Tab para focar no seletor e Enter/Space para abrir
                    await page.keyboard.press("Tab")
                    await page.wait_for_timeout(300)
                    await page.keyboard.press("Space")
                elif tentativa == 3:
                    # Dispara eventos nativos do DOM sem depender de seletor de classe
                    await page.evaluate("""() => {
                        const label = [...document.querySelectorAll('label, div, span')]
                            .find(el => el.textContent.trim().startsWith('Selecione o produto'));
                        if (label) {
                            label.dispatchEvent(new MouseEvent('mousedown', {bubbles:true,cancelable:true}));
                            label.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true,cancelable:true}));
                            label.dispatchEvent(new MouseEvent('click',     {bubbles:true,cancelable:true}));
                        }
                    }""")
            except Exception as exc:
                print(f"[omint] tentativa {tentativa} falhou: {exc}", flush=True)

            await page.wait_for_timeout(4000)
            chk = await page.inner_text("body")
            if any(l.strip().upper().startswith("OMINT ") for l in chk.splitlines()):
                achou = True
                print(f"[omint] produtos encontrados após tentativa {tentativa}", flush=True)
                break
            print(f"[omint] tentativa {tentativa} — produtos ainda não visíveis", flush=True)

        if not achou:
            await page.screenshot(path="/tmp/omint_fail.png")
            print("[omint] screenshot salvo em /tmp/omint_fail.png", flush=True)

        vistos: set[str] = set()
        txt = await page.inner_text("body")
        for linha in txt.splitlines():
            nome = linha.strip()
            if nome.upper().startswith("OMINT ") and len(nome) >= 8 and nome not in vistos:
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

        if not coberturas:
            print(f"[omint] fallback: nenhuma linha 'OMINT ' encontrada no inner_text", flush=True)
    except Exception as e:
        print(f"[omint] erro ao extrair coberturas: {e}", flush=True)
    return coberturas


async def sondar_preco_morte(session_id: str, capital: int = 100_000) -> SondagemPreco:
    """
    Sonda prêmio para OMINT IDEAL - SEGURO DE VIDA INDIVIDUAL em capital âncora.
    Reusa sessão fase1: marca checkbox da cobertura, fill capital, avança e captura preço.

    TODO: configurar comissão 25%/200% — descobrir onde está esse seletor na UI do Athena
    (provavelmente em /produtos ou tela posterior). Por ora usa comissão padrão.
    """
    sessao = _SESSOES.get(session_id)
    if not sessao:
        return SondagemPreco(
            linha_id="morte_qualquer_causa", cobertura_nome="OMINT IDEAL - SEGURO DE VIDA INDIVIDUAL",
            capital_sondado=capital, premio_mensal=0.0, preco_por_1000=0.0,
            erro="Sessão expirada",
        )
    page: Page = sessao["page"]
    try:
        nome_produto = "OMINT IDEAL - SEGURO DE VIDA INDIVIDUAL"
        # Reusa lógica da fase2: marca checkbox e preenche capital
        chk = page.locator(f'*:has-text("{nome_produto[:30]}") input[type="checkbox"]').first
        if await chk.count() and not await chk.is_checked():
            await chk.click()
            await page.wait_for_timeout(500)

        inp = page.locator(
            f'*:has-text("{nome_produto[:30]}") input[type="number"], '
            f'*:has-text("{nome_produto[:30]}") input[type="tel"]'
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
            await page.keyboard.type(str(int(capital)), delay=25)
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(700)

        # TODO: selecionar comissão 25%/200% — descobrir UI
        # Por enquanto, avança e captura o preço com comissão padrão

        await clicar_continuar(page)
        await page.wait_for_timeout(5000)

        # Salva screenshot/HTML pra debug
        try:
            await page.screenshot(path="/tmp/omint_sondagem.png", full_page=True)
            html = await page.content()
            with open("/tmp/omint_sondagem.html", "w") as f:
                f.write(html)
        except Exception:
            pass

        # Captura prêmio do texto da página com contexto
        txt = await page.inner_text("body")
        candidatos = []
        for m in re.finditer(r'R\$\s*([\d.]+),(\d{2})', txt):
            try:
                f = float(m.group(1).replace(".", "") + "." + m.group(2))
                if 5 <= f <= 5000:
                    idx = m.start()
                    ctx = txt[max(0, idx-50):idx+30].replace("\n", " ").strip()
                    candidatos.append((f, ctx))
            except Exception:
                pass
        print(f"[omint] sondagem R$ candidatos: {len(candidatos)} | url={page.url}", flush=True)
        for c in candidatos[:8]:
            print(f"  R$ {c[0]:.2f} | ctx: {c[1][:80]}", flush=True)

        premio_val: float | None = None
        # Heurística: menor valor razoável é provavelmente o prêmio
        if candidatos:
            premio_val = min(c[0] for c in candidatos)
            print(f"[omint] sondagem Morte: R$ {premio_val:.2f}/mês (menor entre {len(candidatos)})", flush=True)

        if premio_val is None or premio_val <= 0:
            return SondagemPreco(
                linha_id="morte_qualquer_causa", cobertura_nome=nome_produto,
                capital_sondado=capital, premio_mensal=0.0, preco_por_1000=0.0,
                erro=f"Prêmio não capturado | url={page.url}",
            )

        preco_1k = round(premio_val / (capital / 1000.0), 4)
        return SondagemPreco(
            linha_id="morte_qualquer_causa",
            cobertura_nome=nome_produto,
            capital_sondado=float(capital),
            premio_mensal=premio_val,
            preco_por_1000=preco_1k,
        )
    except Exception as e:
        return SondagemPreco(
            linha_id="morte_qualquer_causa",
            cobertura_nome="OMINT IDEAL - SEGURO DE VIDA INDIVIDUAL",
            capital_sondado=capital, premio_mensal=0.0, preco_por_1000=0.0,
            erro=str(e)[:120],
        )


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
