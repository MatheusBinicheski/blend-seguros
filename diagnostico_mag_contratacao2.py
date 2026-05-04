"""
Diagnóstico MAG — /contratacao/simulacao
Nunca fecha o navegador. Screenshots em cada passo.
"""
from dotenv import load_dotenv
load_dotenv()
import asyncio, sys, re, os
sys.path.insert(0, ".")
from playwright.async_api import async_playwright, Page

CNPJ  = os.getenv("MAG_CNPJ", "")
SENHA = os.getenv("MAG_SENHA", "")

DADOS = {
    "nome":         "Carlos Teste",
    "cpf":          "08299121183",
    "nascimento":   "18062006",       # DDMMYYYY
    "sexo":         "M",
    "estado":       "Paulo",
    "ocupacao":     "Profissional Liberal",
    "profissao":    "Biomédico",
    "renda_mensal": "5000",           # reais; script converte p/ centavos na máscara
}

URL_SIMULACAO = "https://digital.mag.com.br/contratacao/simulacao"
_step = [0]

# Produtos e capitais conforme guia de negócio — nomes exatos do dropdown
# Capital em reais; máscara BR = digitar capital * 100
PRODUTOS = [
    ("VIDA INTEIRA",                                    100_000),
    ("MORTE POR ACIDENTE",                              500_000),
    ("IPA COM MAJORAÇÃO + IFPD",                        500_000),
    ("DOENÇAS GRAVES ESSENCIAL",                        300_000),
    ("DOENÇAS GRAVES PLUS",                             300_000),
    ("DOENÇAS GRAVES VITAL",                            600_000),
    ("DIÁRIA POR INTERNAÇÃO HOSPITALAR + SUPORTE 150",    1_000),
    ("DIARIA POR INCAPACIDADE TEMPORÁRIA POR ACIDENTE",   1_000),
    ("CIRURGIAS + AMPARO",                              200_000),
    ("ASSISTENCIA INVIDA",                               15_000),
]


async def ss(page: Page, nome: str) -> str:
    _step[0] += 1
    p = f"/tmp/mag_{_step[0]:02d}_{nome}.png"
    await page.screenshot(path=p, full_page=True)
    print(f"  [📸] {p}", flush=True)
    return p


async def abrir_react_select(page: Page, inp_id: str):
    """Dispara mousedown no div control para abrir o React-Select."""
    await page.evaluate(f"""() => {{
        const inp = document.getElementById('{inp_id}');
        if (!inp) return;
        let el = inp;
        for (let i = 0; i < 12; i++) {{
            el = el && el.parentElement;
            if (!el) break;
            if ((el.className || '').includes('control')) {{
                el.dispatchEvent(new MouseEvent('mousedown', {{bubbles:true, cancelable:true}}));
                inp.focus();
                return;
            }}
        }}
        inp.focus();
    }}""")
    await page.wait_for_timeout(700)


async def escolher_react_select(page: Page, inp_id: str, texto: str, label: str, teclado: bool = False):
    """
    teclado=False (padrão): fill("") + inp.type() — funciona para vd-select (Modelo, Ocupação)
    teclado=True           : keyboard.type() sem fill — funciona para vd-input-suggestion (Estado)
    """
    try:
        await page.wait_for_selector('.loading', state='hidden', timeout=10_000)
    except Exception:
        pass
    await page.wait_for_timeout(300)

    await abrir_react_select(page, inp_id)

    inp = page.locator(f'#{inp_id}')
    if teclado:
        # Não usa fill — mantém eventos React intactos (vd-input-suggestion)
        await page.keyboard.press("Control+a")
        await page.wait_for_timeout(100)
        await page.keyboard.type(texto[:15], delay=80)
    else:
        # fill("") + type — abre menu do vd-select corretamente
        await inp.fill("")
        await inp.type(texto[:12], delay=50)
    await page.wait_for_timeout(3000)  # aguarda API / filtro

    # Opções disponíveis
    opcoes = await page.locator('div[class*="option"]').all_inner_texts()
    print(f"  [{label}] opções: {opcoes[:10]}", flush=True)

    # 1ª tentativa: texto exato (evita "CROSS SELL VIDA TODA VD STOA")
    opt_exato = page.locator('div[class*="option"]').filter(
        has_text=re.compile(r'^\s*' + re.escape(texto) + r'\s*$')
    )
    if await opt_exato.count():
        await opt_exato.first.click()
        print(f"  [{label}] ✓ exato: {texto}", flush=True)
    else:
        # 2ª tentativa: última opção que contém o texto
        opts_parcial = page.locator(f'div[class*="option"]:has-text("{texto.split()[0]}")')
        n = await opts_parcial.count()
        if n:
            txt = await opts_parcial.nth(n - 1).inner_text()
            await opts_parcial.nth(n - 1).click()
            print(f"  [{label}] ✓ parcial (última): {txt}", flush=True)
        else:
            await page.keyboard.press("ArrowDown")
            await page.wait_for_timeout(150)
            await page.keyboard.press("Enter")
            print(f"  [{label}] ✓ ArrowDown+Enter", flush=True)
    await page.wait_for_timeout(400)

    # Lê valor confirmado
    val = await page.evaluate(f"""() => {{
        const inp = document.getElementById('{inp_id}');
        let el = inp;
        for (let i = 0; i < 12; i++) {{
            el = el && el.parentElement;
            if (!el) break;
            const sv = el.querySelector('[class*="single-value"]');
            if (sv) return sv.innerText.trim();
        }}
        return inp ? inp.value : '';
    }}""")
    print(f"[mag] {label} = {val!r}", flush=True)
    return val


# ──────────────────────────────────────────────────────────────
async def login(page: Page):
    from automacao.base import resolver_captcha

    print("[login] abrindo identidade…", flush=True)
    await page.goto("https://digital.mag.com.br/simulador",
                    wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(3000)
    await ss(page, "login_form")

    await page.wait_for_selector('#Cpf', timeout=15_000)
    await page.locator('#Cpf').fill(CNPJ)
    await page.locator('input[type="password"]').first.fill(SENHA)
    await resolver_captcha(page)
    await page.wait_for_timeout(1000)
    await page.evaluate("const b=document.getElementById('btnAuth'); if(b) b.removeAttribute('disabled');")
    await page.wait_for_timeout(300)
    await page.locator('#btnAuth').first.click()
    print("[login] btnAuth clicado", flush=True)

    for _ in range(30):
        await page.wait_for_timeout(1000)
        if not any(x in page.url for x in ("identidade", "auth-callback", "login")):
            break

    await ss(page, "login_pos")

    if "/parceria" in page.url:
        print("[login] selecionando parceria…", flush=True)
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
            await page.evaluate("() => { const b=[...document.querySelectorAll('button')].find(b=>b.textContent.includes('Selecionar')); if(b)b.click(); }")
        for _ in range(15):
            await page.wait_for_timeout(700)
            if "/parceria" not in page.url:
                break
        await ss(page, "login_pos_parceria")

    print(f"[login] ok → {page.url}", flush=True)


# ──────────────────────────────────────────────────────────────
async def preencher_dados(page: Page):
    await page.goto(URL_SIMULACAO, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(3000)
    print(f"[form] url = {page.url}", flush=True)
    await ss(page, "form_inicial")

    # 1. Modelo de proposta
    print("[form] 1. Modelo de proposta…", flush=True)
    await escolher_react_select(
        page,
        "quoter_form__your-data__proposal_model_id__input",
        "VIDA TODA VD STOA",
        "Modelo"
    )
    await ss(page, "form_modelo")

    # 2. Nome
    print("[form] 2. Nome…", flush=True)
    nome_inp = page.locator('#quoter_form__your-data__input_name')
    await nome_inp.click()
    await nome_inp.fill(DADOS["nome"])
    await page.keyboard.press("Tab")
    await page.wait_for_timeout(400)
    # fecha modal nome social se aparecer
    await page.evaluate("""() => {
        const b = [...document.querySelectorAll('button')]
            .find(b => /não tem|nao tem/i.test(b.textContent||'') && b.offsetParent);
        if (b) b.click();
    }""")
    await page.wait_for_timeout(300)
    print(f"[form] Nome = {DADOS['nome']}", flush=True)

    # 3. CPF
    print("[form] 3. CPF…", flush=True)
    cpf = re.sub(r"\D", "", DADOS["cpf"])
    cpf_inp = page.locator('#quoter_form__your-data__document')
    await cpf_inp.click()
    await cpf_inp.press_sequentially(cpf, delay=30)
    await page.keyboard.press("Tab")
    print(f"[form] CPF = {await cpf_inp.input_value()}", flush=True)
    await page.wait_for_timeout(300)

    # 4. Nascimento
    print("[form] 4. Nascimento…", flush=True)
    nasc_inp = page.locator('#quoter_form__your-data__birthday')
    await nasc_inp.click()
    await nasc_inp.press_sequentially(DADOS["nascimento"], delay=40)
    await page.keyboard.press("Tab")
    print(f"[form] Nascimento = {await nasc_inp.input_value()}", flush=True)
    await page.wait_for_timeout(300)

    # 5. Sexo — clica no radio-list__label (intercepta o inner label)
    print("[form] 5. Sexo…", flush=True)
    gender_id = "quoter_form__your-data__gender__1" if DADOS["sexo"].upper() in ("M","MASCULINO") else "quoter_form__your-data__gender__2"
    await page.locator(f'label.radio-list__label[for="{gender_id}"]').click()
    print(f"[form] Sexo = {'MASCULINO' if 'gender__1' in gender_id else 'FEMININO'}", flush=True)
    await page.wait_for_timeout(200)
    await ss(page, "form_sexo")

    # 6. Estado — vd-input-suggestion: usa keyboard.type sem fill
    print("[form] 6. Estado…", flush=True)
    await escolher_react_select(
        page,
        "quoter_form__your-data__state_id__input",
        DADOS["estado"],
        "Estado",
        teclado=True
    )
    await ss(page, "form_estado")

    # 7. Ocupação
    print("[form] 7. Ocupação…", flush=True)
    await escolher_react_select(
        page,
        "quoter_form__your-data__occupation__input",
        DADOS["ocupacao"],
        "Ocupação"
    )
    await ss(page, "form_ocupacao")

    # 7b. Profissão (aparece após Profissional Liberal)
    await page.wait_for_timeout(600)
    works_inp = page.locator('#quoter_form__your-data__works_as__input')
    if await works_inp.is_visible():
        print("[form] 7b. Profissão…", flush=True)
        await escolher_react_select(
            page,
            "quoter_form__your-data__works_as__input",
            DADOS["profissao"],
            "Profissão"
        )
        await ss(page, "form_profissao")

    # 8. Renda mensal — máscara BR: últimos 2 dígitos = centavos → 5000 reais = "500000"
    print("[form] 8. Renda…", flush=True)
    centavos = str(int(re.sub(r'\D', '', DADOS["renda_mensal"])) * 100)
    renda_inp = page.locator('#quoter_form__your-data__currency')
    await renda_inp.click(click_count=3)
    await renda_inp.press_sequentially(centavos, delay=30)
    await page.keyboard.press("Tab")
    print(f"[form] Renda = {await renda_inp.input_value()}", flush=True)
    await page.wait_for_timeout(300)
    await ss(page, "form_renda")

    # 9. Cônjuge NÃO
    print("[form] 9. Cônjuge…", flush=True)
    await page.locator('label.radio-list__label[for="quoter_form__your-data__has_companion__0"]').click()
    print("[form] Cônjuge = NÃO", flush=True)
    await page.wait_for_timeout(300)

    await ss(page, "form_completo")
    print("\n[form] ✓ todos os campos preenchidos", flush=True)


# ──────────────────────────────────────────────────────────────
async def editar_solucao(page: Page):
    print("[editar] clicando EDITAR SOLUÇÃO…", flush=True)
    btn = page.locator('button:has-text("EDITAR SOLUÇÃO"), button:has-text("Editar Solução")').first
    if not await btn.count():
        print("[editar] ⚠️ botão não encontrado", flush=True)
        await ss(page, "editar_nao_encontrado")
        return False

    await btn.click()
    await page.wait_for_timeout(5000)
    await ss(page, "editar_pos")
    print(f"[editar] ok — url = {page.url}", flush=True)
    return True


# ──────────────────────────────────────────────────────────────
async def adicionar_produto(page: Page, nome: str, capital_reais: int):
    """Busca o produto no react-select-2-input, seleciona, preenche o capital."""
    print(f"[produto] adicionando: {nome} — R$ {capital_reais:,}", flush=True)

    # Abre dropdown — NÃO usa fill("") para não apagar tags de produtos anteriores
    await abrir_react_select(page, "react-select-2-input")
    await page.wait_for_timeout(300)
    inp_prod = page.locator('#react-select-2-input')
    await inp_prod.focus()
    await page.keyboard.type(nome[:20], delay=60)
    await page.wait_for_timeout(2500)

    opcoes = await page.locator('div[class*="option"]').all_inner_texts()
    print(f"  opções: {opcoes[:6]}", flush=True)

    codigo = None

    # Clica na opção correspondente — extrai código do texto da opção clicada
    opt_exato = page.locator('div[class*="option"]').filter(
        has_text=re.compile(re.escape(nome[:15]), re.IGNORECASE)
    )
    if await opt_exato.count():
        opt_text = await opt_exato.first.inner_text()
        m = re.search(r'\((\d+)\)', opt_text)
        if m:
            codigo = m.group(1)
        await opt_exato.first.click()
        print(f"  ✓ selecionado: {opt_text.strip()}", flush=True)
    elif opcoes:
        opt_text = opcoes[0]
        m = re.search(r'\((\d+)\)', opt_text)
        if m:
            codigo = m.group(1)
        await page.locator('div[class*="option"]').first.click()
        print(f"  ✓ primeiro disponível: {opt_text}", flush=True)
    else:
        await page.keyboard.press("Escape")
        print(f"  ⚠️ nenhuma opção para: {nome}", flush=True)
        return
    await page.wait_for_timeout(3000)  # aguarda renderização completa do card

    # Preenche o capital — qualquer input[id*="product_{codigo}"][id*="benefit"]
    centavos = str(capital_reais * 100)
    filled_any = False

    async def preencher_benefits(locator):
        nonlocal filled_any
        cnt = await locator.count()
        for i in range(cnt):
            benefit = locator.nth(i)
            eid = await benefit.get_attribute('id') or f"idx{i}"
            try:
                await benefit.scroll_into_view_if_needed(timeout=8000)
            except Exception:
                pass
            await benefit.focus()
            await page.keyboard.press("Control+a")
            await page.keyboard.type(centavos, delay=25)
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(300)
            val = await benefit.input_value()
            print(f"  benefício [{eid}] = {val}", flush=True)
            filled_any = True
        return cnt

    if codigo:
        loc1 = page.locator(f'input[id*="product_{codigo}"][id*="benefit"]')
        cnt1 = await preencher_benefits(loc1)

        if cnt1 == 0:
            # Dumpa inputs do produto para diagnóstico
            cand_all = page.locator(f'input[id*="product_{codigo}"]')
            n = await cand_all.count()
            for i in range(min(n, 8)):
                eid = await cand_all.nth(i).get_attribute('id')
                print(f"  debug input: id={eid}")
        else:
            # Segundo pass: alguns inputs aparecem após o preenchimento do primeiro
            await page.wait_for_timeout(800)
            loc2 = page.locator(f'input[id*="product_{codigo}"][id*="benefit"]')
            cnt2 = await loc2.count()
            if cnt2 > cnt1:
                print(f"  [{cnt2 - cnt1} inputs adicionais apareceram]", flush=True)
                for i in range(cnt1, cnt2):
                    benefit = loc2.nth(i)
                    eid = await benefit.get_attribute('id') or f"idx{i}"
                    try:
                        await benefit.scroll_into_view_if_needed(timeout=8000)
                    except Exception:
                        pass
                    await benefit.focus()
                    await page.keyboard.press("Control+a")
                    await page.keyboard.type(centavos, delay=25)
                    await page.keyboard.press("Tab")
                    await page.wait_for_timeout(300)
                    val = await benefit.input_value()
                    print(f"  benefício2 [{eid}] = {val}", flush=True)

    if not filled_any:
        print(f"  ⚠️ input benefício não encontrado (codigo={codigo})", flush=True)

    await page.wait_for_timeout(300)


async def descobrir_produtos(page: Page):
    """Abre dropdown com busca vazia para listar todos os produtos disponíveis."""
    print("\n[discover] abrindo dropdown vazio…", flush=True)
    await abrir_react_select(page, "react-select-2-input")
    inp = page.locator('#react-select-2-input')
    await inp.fill("")
    await page.wait_for_timeout(2000)
    all_opts = await page.locator('div[class*="option"]').all_inner_texts()
    print(f"[discover] TODOS OS PRODUTOS DISPONÍVEIS ({len(all_opts)}):")
    for o in all_opts:
        print(f"  — {o}")
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(500)
    return all_opts


async def adicionar_produtos(page: Page):
    print("\n[produtos] iniciando seleção de coberturas…", flush=True)
    await ss(page, "produtos_inicio")

    # Descobre nomes reais dos produtos no modelo
    todos = await descobrir_produtos(page)
    print(f"\n[produtos] {len(todos)} produtos encontrados", flush=True)

    for nome, capital in PRODUTOS:
        await adicionar_produto(page, nome, capital)
        await ss(page, f"prod_{nome[:12].replace(' ','_')}")

    await ss(page, "produtos_todos_adicionados")

    # Dump da tela para verificar totais
    txt = await page.inner_text("body")
    print("\n=== TELA PÓS PRODUTOS ===")
    print(txt[-2000:])
    print("========================")


async def confirmar_solucao(page: Page):
    print("\n[confirmar] clicando CONFIRMAR SOLUÇÃO…", flush=True)

    # Aguarda qualquer loading terminar
    try:
        await page.wait_for_selector('.loading', state='hidden', timeout=10_000)
    except Exception:
        pass

    # Extrai valor total de contribuição antes de confirmar
    total_txt = await page.evaluate("""() => {
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            if (/CONTRIBUI/i.test(node.textContent)) {
                return node.parentElement ? node.parentElement.closest('[class]')?.textContent || node.textContent : node.textContent;
            }
        }
        return '';
    }""")
    print(f"VALOR TOTAL DE CONTRIBUIÇÃO{total_txt.strip()}", flush=True)

    btn = page.locator('button:has-text("CONFIRMAR SOLUÇÃO")').first
    await btn.wait_for(state='visible', timeout=10_000)
    await ss(page, "confirmar_pre")
    await btn.click()
    await page.wait_for_timeout(3000)

    # Fecha modal de validação se aparecer
    for _ in range(5):
        modal_ok = page.locator('button:has-text("OK"), button:has-text("Ok"), button:has-text("Fechar")')
        if await modal_ok.count():
            modal_txt = await page.locator('[role="dialog"], .modal, .alert').first.inner_text() if await page.locator('[role="dialog"], .modal, .alert').count() else ""
            print(f"  [modal] {modal_txt.strip()[:300]}", flush=True)
            await ss(page, "confirmar_modal")
            await modal_ok.first.click()
            await page.wait_for_timeout(1500)
        else:
            break

    await page.wait_for_timeout(3000)
    await ss(page, "confirmar_pos")

    txt = await page.inner_text("body")
    print("\n=== RESULTADO FINAL ===")
    print(txt[:5000])
    print("=======================")


# ──────────────────────────────────────────────────────────────
async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=30)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        try:
            print("=== LOGIN ===")
            await login(page)

            print("\n=== PREENCHE DADOS ===")
            await preencher_dados(page)

            print("\n=== EDITAR SOLUÇÃO ===")
            ok = await editar_solucao(page)
            if not ok:
                raise RuntimeError("EDITAR SOLUÇÃO não clicado")

            print("\n=== ADICIONA PRODUTOS ===")
            await adicionar_produtos(page)

            print("\n=== CONFIRMA SOLUÇÃO ===")
            await confirmar_solucao(page)

            print("\n[done] navegador mantido aberto.", flush=True)
            await asyncio.sleep(300)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"\n❌ ERRO: {e}", flush=True)
            await ss(page, "ERRO")
            print("Navegador mantido aberto.", flush=True)
            await asyncio.sleep(300)

asyncio.run(main())
