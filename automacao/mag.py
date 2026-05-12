"""Automação MAG Seguros — Venda Digital (contratacao/simulacao)."""
from __future__ import annotations
import asyncio, os, re
from playwright.async_api import Page
from .base import novo_browser, fechar_browser, resolver_captcha
from models import Cobertura, ResultadoFase1, ResultadoCotacao

URL_SIMULACAO = "https://digital.mag.com.br/contratacao/simulacao"
CNPJ  = os.getenv("MAG_CNPJ", "")
SENHA = os.getenv("MAG_SENHA", "")

_SESSOES: dict[str, dict] = {}


async def _abrir_react_select(page: Page, inp_id: str):
    """
    Abre um React-Select clicando no Control div (3 níveis acima do input via XPath).
    React Select v2/v3 escuta mousedown no Control, não no input em si.
    """
    if inp_id:
        inp = page.locator(f'#{inp_id}')
    else:
        inp = page.locator('input[aria-autocomplete="list"]').first

    try:
        await inp.scroll_into_view_if_needed(timeout=5_000)
    except Exception:
        pass

    # Clica no Control (3 níveis acima): input → input-container → value-container → control
    opened = False
    for xpath in ('xpath=../../..', 'xpath=../../../..', 'xpath=../..'):
        try:
            ctrl = inp.locator(xpath)
            await ctrl.click(timeout=2_000)
            opened = True
            break
        except Exception:
            continue

    if not opened:
        try:
            await inp.click(force=True)
        except Exception:
            pass

    await page.wait_for_timeout(700)


async def _escolher_react_select(page: Page, inp_id: str, texto: str, teclado: bool = False):
    """
    Preenche e seleciona uma opção num React-Select.
    Usa [role="option"] e input[role="combobox"] — sem seletores de classe CSS.
    """
    try:
        await page.wait_for_selector('[aria-busy="true"]', state='hidden', timeout=10_000)
    except Exception:
        pass
    await page.wait_for_timeout(300)
    await _abrir_react_select(page, inp_id)

    # Localiza o input pelo ID ou por atributo semântico
    inp = page.locator(f'#{inp_id}') if inp_id else page.locator(
        'input[role="combobox"], input[aria-haspopup="listbox"], input[aria-autocomplete="list"]'
    ).first

    if teclado:
        # Para React Select assíncrono (busca via API ao digitar):
        # press_sequentially foca o input E dispara os eventos de teclado certos
        try:
            await inp.press_sequentially(texto[:15], delay=80)
        except Exception:
            # Fallback se o input não aceitar diretamente (opacity:0)
            try:
                await inp.click(force=True)
            except Exception:
                pass
            await page.keyboard.type(texto[:15], delay=80)
    else:
        try:
            await inp.fill("")
            await inp.type(texto[:12], delay=50)
        except Exception:
            await page.keyboard.type(texto[:12], delay=50)

    # Aguarda a API de busca responder (2.5s é suficiente para o backend MAG)
    await page.wait_for_timeout(2500)

    palavras = texto.split()
    if not palavras:
        return

    search_words = " ".join(palavras[:2]) if len(palavras) > 1 else palavras[0]

    # Estratégia 1: [role="option"] — React Select v3+ (AZOS, OMINT)
    opt_exato = page.locator('[role="option"]').filter(
        has_text=re.compile(r'^\s*' + re.escape(texto) + r'\s*$', re.IGNORECASE)
    )
    if await opt_exato.count():
        await opt_exato.first.click()
        await page.wait_for_timeout(400)
        return

    opts = page.locator('[role="option"]').filter(
        has_text=re.compile(re.escape(search_words), re.IGNORECASE)
    )
    if await opts.count():
        await opts.first.click()
        await page.wait_for_timeout(400)
        return

    # Estratégia 1.5: [id*="--option-"] — React Select v1/v2 (MAG)
    # As opções têm IDs no padrão "react-select-X--option-N" ou "{comp-id}--option-N".
    rs_opts = page.locator('[id*="--option-"]')
    rs_count = await rs_opts.count()
    if rs_count == 0:
        # Alternativa: procura divs dentro do listbox (role="listbox")
        listbox = page.locator('[role="listbox"]')
        if await listbox.count():
            rs_opts = listbox.locator('div')
            rs_count = await rs_opts.count()

    if rs_count:
        exact_rs = rs_opts.filter(
            has_text=re.compile(r'^\s*' + re.escape(texto) + r'\s*$', re.IGNORECASE)
        )
        if await exact_rs.count():
            await exact_rs.first.click()
            await page.wait_for_timeout(400)
            return
        word_rs = rs_opts.filter(
            has_text=re.compile(re.escape(search_words), re.IGNORECASE)
        )
        if await word_rs.count():
            await word_rs.first.click()
            await page.wait_for_timeout(400)
            return

    # Estratégia 2: click por texto visível (fallback amplo)
    try:
        await page.get_by_text(texto, exact=False).first.click(timeout=2_000)
        await page.wait_for_timeout(400)
        return
    except Exception:
        pass

    # Estratégia 3: ArrowDown + Enter (fallback teclado)
    await page.keyboard.press("ArrowDown")
    await page.wait_for_timeout(500)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(400)


async def _login(page: Page):
    print("[mag] abrindo identidade…", flush=True)
    await page.goto("https://digital.mag.com.br/simulador",
                    wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(3000)

    await page.wait_for_selector('#Cpf', timeout=15_000)
    await page.locator('#Cpf').fill(CNPJ)
    await page.locator('input[type="password"]').first.fill(SENHA)
    await resolver_captcha(page)
    await page.wait_for_timeout(1000)
    await page.evaluate("const b=document.getElementById('btnAuth'); if(b) b.removeAttribute('disabled');")
    await page.wait_for_timeout(300)
    await page.locator('#btnAuth').first.click()

    for _ in range(30):
        await page.wait_for_timeout(1000)
        if not any(x in page.url for x in ("identidade", "auth-callback", "login")):
            break

    if "/parceria" in page.url:
        lbl = page.locator('.area__partnership label.radio-list__label').first
        if await lbl.count():
            await lbl.click(force=True)
        else:
            await page.locator('input[name*="partnerships"]').first.click(force=True)
        await page.wait_for_timeout(500)
        hier = page.locator('.area__hierarchy label.radio-list__label').first
        if await hier.count():
            await hier.click(force=True)
            await page.wait_for_timeout(300)
        try:
            await page.locator('button:has-text("Selecionar")').last.click(timeout=5000)
        except Exception:
            await page.evaluate(
                "() => { const b=[...document.querySelectorAll('button')]"
                ".find(b=>b.textContent.includes('Selecionar')); if(b)b.click(); }"
            )
        for _ in range(15):
            await page.wait_for_timeout(700)
            if "/parceria" not in page.url:
                break
    print(f"[mag] login ok → {page.url}", flush=True)


async def _preencher_dados(page: Page, dados: dict):
    await page.goto(URL_SIMULACAO, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(3000)

    await _escolher_react_select(
        page, "quoter_form__your-data__proposal_model_id__input", "VIDA TODA VD STOA"
    )

    nome_inp = page.locator('#quoter_form__your-data__input_name')
    await nome_inp.click()
    await nome_inp.fill(dados.get("nome", ""))
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(400)
    await page.evaluate("""() => {
        const b = [...document.querySelectorAll('button')]
            .find(b => /não tem|nao tem/i.test(b.textContent||'') && b.offsetParent);
        if (b) b.click();
    }""")
    await page.wait_for_timeout(300)

    cpf = re.sub(r"\D", "", dados.get("cpf", ""))
    cpf_inp = page.locator('#quoter_form__your-data__document')
    await cpf_inp.click()
    await cpf_inp.press_sequentially(cpf, delay=30)
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(300)

    nasc_raw = re.sub(r"\D", "", dados.get("nascimento", ""))
    nasc_inp = page.locator('#quoter_form__your-data__birthday')
    await nasc_inp.click()
    await nasc_inp.press_sequentially(nasc_raw, delay=40)
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(300)

    sexo = dados.get("sexo", "M")
    gender_id = ("quoter_form__your-data__gender__1"
                 if sexo.upper() in ("M", "MASCULINO")
                 else "quoter_form__your-data__gender__2")
    await page.locator(f'label.radio-list__label[for="{gender_id}"]').click()
    await page.wait_for_timeout(200)

    await _escolher_react_select(
        page, "quoter_form__your-data__state_id__input",
        dados.get("estado", "Paulo"), teclado=True
    )

    await _escolher_react_select(
        page, "quoter_form__your-data__occupation__input",
        dados.get("ocupacao", "Profissional Liberal")
    )

    await page.wait_for_timeout(600)
    works_inp = page.locator('#quoter_form__your-data__works_as__input')
    if await works_inp.is_visible():
        profissao = dados.get("profissao", "") or "Advogado"
        await _escolher_react_select(
            page, "quoter_form__your-data__works_as__input",
            profissao
        )

    renda_raw = re.sub(r'\D', '', str(dados.get("renda_mensal", "5000")))
    renda_inp = page.locator('#quoter_form__your-data__currency')
    await renda_inp.click(click_count=3)
    await renda_inp.press_sequentially(str(int(renda_raw) * 100), delay=30)
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(300)

    await page.locator(
        'label.radio-list__label[for="quoter_form__your-data__has_companion__0"]'
    ).click()
    await page.wait_for_timeout(300)


async def _editar_solucao(page: Page) -> bool:
    btn = page.locator('button:has-text("EDITAR SOLUÇÃO"), button:has-text("Editar Solução")').first
    if not await btn.count():
        return False
    await btn.click()
    await page.wait_for_timeout(5000)
    return True


async def _benefit_ids_for(page: Page, cod: str) -> list:
    """IDs de inputs do produto que contenham 'benefit' (case-insensitive)."""
    return await page.evaluate(f"""() => {{
        return [...document.querySelectorAll('input[id*="product_{cod}"]')]
            .filter(el => el.id.toLowerCase().includes('benefit'))
            .map(el => el.id);
    }}""")


async def _preencher_benefit_id(page: Page, eid: str, centavos: str):
    loc = page.locator(f'#{eid}')
    try:
        await loc.scroll_into_view_if_needed(timeout=8000)
    except Exception:
        pass
    await loc.focus()
    await page.keyboard.press("Control+a")
    await page.keyboard.type(centavos, delay=25)
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(300)
    val = await loc.input_value()
    print(f"  benefício [{eid}] = {val}", flush=True)


async def _adicionar_produto(page: Page, nome: str, capital):
    """
    capital: int (mesmo para todos) ou dict {"default": N, "fieldkey": M}
    """
    def get_centavos(eid: str) -> str:
        if isinstance(capital, dict):
            eid_lower = eid.lower()
            for key, val in capital.items():
                if key != "default" and key in eid_lower:
                    return str(val * 100)
            return str(capital.get("default", 0) * 100)
        return str(capital * 100)

    print(f"[mag] adicionando: {nome}", flush=True)

    # O combobox de produto é o último input[aria-autocomplete="list"] na página
    # (os primeiros 3 são dos dados do cliente que ficam no formulário).
    combo = page.locator('input[aria-autocomplete="list"]').last
    try:
        await combo.scroll_into_view_if_needed(timeout=5_000)
    except Exception:
        pass
    await combo.click(force=True)
    await page.wait_for_timeout(300)
    await combo.type(nome[:20], delay=60)
    await page.wait_for_timeout(2500)

    # Opções do dropdown via ARIA role (sem classe CSS)
    opts_all = page.locator('[role="option"]')
    opcoes = await opts_all.all_inner_texts()
    codigo = None

    opt_exato = opts_all.filter(has_text=re.compile(re.escape(nome[:15]), re.IGNORECASE))
    if await opt_exato.count():
        opt_text = await opt_exato.first.inner_text()
        m = re.search(r'\((\d+)\)', opt_text)
        if m:
            codigo = m.group(1)
        await opt_exato.first.click()
    elif opcoes:
        opt_text = opcoes[0]
        m = re.search(r'\((\d+)\)', opt_text)
        if m:
            codigo = m.group(1)
        await opts_all.first.click()
    else:
        await page.keyboard.press("Escape")
        print(f"  ⚠️ nenhuma opção para: {nome}", flush=True)
        return
    await page.wait_for_timeout(3000)

    if not codigo:
        return

    ids1 = await _benefit_ids_for(page, codigo)
    for eid in ids1:
        await _preencher_benefit_id(page, eid, get_centavos(eid))

    if ids1:
        await page.wait_for_timeout(800)
        ids2 = await _benefit_ids_for(page, codigo)
        for eid in [e for e in ids2 if e not in ids1]:
            await _preencher_benefit_id(page, eid, get_centavos(eid))

    await page.wait_for_timeout(300)


async def _confirmar_solucao(page: Page) -> float:
    try:
        await page.wait_for_selector('.loading', state='hidden', timeout=10_000)
    except Exception:
        pass

    total_txt = await page.evaluate("""() => {
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            if (/CONTRIBUI/i.test(node.textContent)) {
                return node.parentElement?.closest('[class]')?.textContent || node.textContent;
            }
        }
        return '';
    }""")
    print(f"[mag] total pré-confirmar: {total_txt.strip()}", flush=True)

    btn = page.locator('button:has-text("CONFIRMAR SOLUÇÃO")').first
    await btn.wait_for(state='visible', timeout=10_000)
    await btn.click()
    await page.wait_for_timeout(3000)

    for _ in range(5):
        modal_ok = page.locator('button:has-text("OK"), button:has-text("Ok"), button:has-text("Fechar")')
        if await modal_ok.count():
            await modal_ok.first.click()
            await page.wait_for_timeout(1500)
        else:
            break
    await page.wait_for_timeout(3000)

    m = re.search(r'R\$\s*([\d.]+),([\d]{2})', total_txt)
    return float(m.group(1).replace('.', '') + '.' + m.group(2)) if m else 0.0


async def fase1_coletar_coberturas(dados: dict, headless: bool = True) -> ResultadoFase1:
    pw, browser, ctx, page = await novo_browser(headless)
    session_id = "mag-" + str(id(page))
    try:
        print("[mag] iniciando fase1", flush=True)

        await _login(page)
        await _preencher_dados(page, dados)

        ok = await _editar_solucao(page)
        if not ok:
            raise Exception("EDITAR SOLUÇÃO não encontrado")

        # Descobre produtos disponíveis abrindo o combobox de produto.
        # Após EDITAR SOLUÇÃO, aparece um 4º input[aria-autocomplete="list"] além
        # dos 3 do formulário de dados. Aguarda ele aparecer (até 15s).
        try:
            await page.wait_for_function(
                "() => document.querySelectorAll('input[aria-autocomplete=\"list\"]').length > 3",
                timeout=15_000,
            )
        except Exception:
            await page.screenshot(path="/tmp/mag_no_product_combo.png")
            print("[mag] combobox de produto não encontrado após EDITAR SOLUÇÃO", flush=True)

        # Usa o último input[aria-autocomplete="list"] — é o de busca de produto
        combo = page.locator('input[aria-autocomplete="list"]').last
        try:
            await combo.scroll_into_view_if_needed(timeout=5_000)
        except Exception:
            pass
        await combo.click(force=True)
        await page.wait_for_timeout(300)
        await combo.fill("")
        await page.wait_for_timeout(2000)
        all_opts = await page.locator('[role="option"]').all_inner_texts()
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)

        coberturas = []
        for opt in all_opts:
            mt = re.match(r'^(.+?)\s*\((\d+)\)\s*$', opt.strip())
            if mt:
                coberturas.append(Cobertura(
                    id=f"mag_{mt.group(2)}",
                    nome=mt.group(1).strip(),
                    descricao="",
                    valor_min=1_000.0,
                    valor_max=3_000_000.0,
                    premio_referencia=0.0,
                    seguradora="mag",
                ))

        print(f"[mag] {len(coberturas)} coberturas encontradas", flush=True)
        _SESSOES[session_id] = {
            "pw": pw, "browser": browser, "ctx": ctx,
            "page": page, "dados": dados,
        }
        return ResultadoFase1(
            seguradora="mag", ok=True,
            coberturas=coberturas, session_id=session_id,
        )

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[mag] ERRO fase1: {e}", flush=True)
        await fechar_browser(pw, browser)
        return ResultadoFase1(seguradora="mag", ok=False, erro=str(e))


async def fase2_finalizar(session_id: str, selecoes: list[dict]) -> list[ResultadoCotacao]:
    sessao = _SESSOES.get(session_id)
    if not sessao:
        return [ResultadoCotacao(
            seguradora="mag", cobertura_nome="", valor_capital=0,
            premio_mensal=0, erro="Sessão expirada",
        )]

    page: Page = sessao["page"]
    resultados = []

    try:
        for sel in selecoes:
            await _adicionar_produto(page, sel["nome"], sel.get("valor", 0))

        premio_total = await _confirmar_solucao(page)
        print(f"[mag] prêmio total = R$ {premio_total:.2f}", flush=True)

        for sel in selecoes:
            resultados.append(ResultadoCotacao(
                seguradora="mag",
                cobertura_nome=sel["nome"],
                valor_capital=float(sel.get("valor", 0)),
                premio_mensal=round(premio_total / max(len(selecoes), 1), 2),
                link_proposta=page.url,
            ))

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[mag] ERRO fase2: {e}", flush=True)
        resultados.append(ResultadoCotacao(
            seguradora="mag", cobertura_nome="Erro", valor_capital=0,
            premio_mensal=0, erro=str(e),
        ))
    finally:
        sess = _SESSOES.pop(session_id, None)
        if sess:
            await fechar_browser(sess["pw"], sess["browser"])

    return resultados
