"""Automação MAG Seguros — Venda Digital."""
from __future__ import annotations
import asyncio, os, re
from playwright.async_api import Page
from .base import novo_browser, fechar_browser, extrair_valor_monetario, resolver_captcha
from models import Cobertura, ResultadoFase1, ResultadoCotacao

URL_LOGIN = "https://digital.mag.com.br"
CNPJ  = os.getenv("MAG_CNPJ", "")
SENHA = os.getenv("MAG_SENHA", "")

_SESSOES: dict[str, dict] = {}

_INJECT_STATES_JS = """(items) => {
    const sel = document.querySelector('#b2-b18-DropdownSelect');
    if (!sel) return 0;
    while (sel.options.length > 1) sel.remove(1);
    items.forEach(item => {
        const s = item.States;
        const opt = document.createElement('option');
        opt.value = String(s.Id);
        opt.text = s.Label;
        sel.appendChild(opt);
    });
    return sel.options.length;
}"""


async def fase1_coletar_coberturas(dados: dict, headless: bool = True) -> ResultadoFase1:
    pw, browser, ctx, page = await novo_browser(headless)
    session_id = "mag-" + str(id(page))
    try:
        print(f"[mag] iniciando fase1", flush=True)
        await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(2000)

        await _login(page)
        print(f"[mag] login ok → {page.url}", flush=True)

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
    await page.wait_for_selector('input#Cpf', timeout=15_000)
    await page.locator('input#Cpf').fill(CNPJ)
    await page.locator('input[type="password"]').first.fill(SENHA)
    await resolver_captcha(page)
    await page.wait_for_timeout(1000)

    await page.evaluate(
        "const b = document.getElementById('btnAuth'); if (b) b.removeAttribute('disabled');"
    )
    await page.wait_for_timeout(300)
    await page.locator('#btnAuth').first.click()

    await page.wait_for_selector('label.radio-list__label', timeout=45_000)
    await page.wait_for_timeout(1000)

    if "/parceria" in page.url:
        label_stoa = page.locator('.area__partnership label.radio-list__label').first
        if await label_stoa.count():
            await label_stoa.click(force=True)
        else:
            await page.locator('input[name*="partnerships"]').first.click(force=True)
        await page.wait_for_timeout(500)

        label_hier = page.locator('.area__hierarchy label.radio-list__label').first
        if await label_hier.count():
            await label_hier.click(force=True)
            await page.wait_for_timeout(300)

        sel_btn = page.locator('button:has-text("Selecionar")').last
        try:
            await sel_btn.click(timeout=3000)
        except Exception:
            try:
                await sel_btn.click(force=True)
            except Exception:
                await page.evaluate("""() => {
                    const btns = [...document.querySelectorAll('button')];
                    const btn = btns.find(b => b.textContent.includes('Selecionar'));
                    if (btn) btn.click();
                }""")

        for _ in range(15):
            await page.wait_for_timeout(700)
            if "/parceria" not in page.url:
                break
        await page.wait_for_timeout(1000)
        print(f"[mag] pós-parceria → {page.url}", flush=True)


async def _navegar_simulacao(page: Page, dados: dict):
    states_data: dict = {"items": None}
    ev = asyncio.Event()

    async def _on_states(resp):
        if "ScreenDataSetGetStates" in resp.url:
            try:
                body = await resp.json()
                items = body.get("data", {}).get("List", {}).get("List", [])
                if items:
                    states_data["items"] = items
                    ev.set()
            except Exception:
                pass

    page.on("response", _on_states)
    try:
        await page.goto("https://digital.mag.com.br/simulador", wait_until="domcontentloaded", timeout=30_000)
        print(f"[mag] simulador → {page.url}", flush=True)

        # Aguarda iframe OutSystems
        frame = None
        for _ in range(20):
            frame = next((f for f in page.frames if "outsystems" in f.url or "simple2u" in f.url), None)
            if frame:
                break
            await page.wait_for_timeout(500)
        if not frame:
            raise Exception("iframe OutSystems não encontrado")

        # Aguarda estados da API (max 15s)
        try:
            await asyncio.wait_for(ev.wait(), timeout=15)
        except asyncio.TimeoutError:
            print("[mag] atenção: GetStates não chegou a tempo", flush=True)

        await page.wait_for_timeout(2000)

        fl = page.frame_locator("iframe[src*='simple2u'], iframe[src*='outsystems']").first

        # 1. LGPD
        await frame.evaluate("() => document.querySelector('#b2-BtnSideBarLgpd')?.click()")
        await page.wait_for_timeout(800)

        # 2. Nome
        nome = dados.get("nome", "")
        if nome:
            await fl.locator('#b2-InputName').evaluate("el => el.scrollIntoView({block:'center'})")
            await fl.locator('#b2-InputName').press_sequentially(nome, delay=40)
            await fl.locator('#b2-InputName').press("Tab")
            await page.wait_for_timeout(400)
            # Rejeita dialog de nome social
            await frame.evaluate(
                "() => { const b=[...document.querySelectorAll('button')]"
                ".find(b=>b.textContent.trim()==='Não tem'&&b.offsetParent); if(b) b.click(); }"
            )
            await page.wait_for_timeout(300)

        # 3. CPF
        cpf = re.sub(r"\D", "", dados.get("cpf", ""))
        if cpf:
            await fl.locator('#b2-InputDocument').evaluate("el => el.scrollIntoView({block:'center'})")
            await fl.locator('#b2-InputDocument').press_sequentially(cpf, delay=50)
            await fl.locator('#b2-InputDocument').press("Tab")
            await page.wait_for_timeout(200)

        # 4. Nascimento — DDMMYYYY
        nasc_raw = dados.get("nascimento", "")
        if nasc_raw:
            digits = re.sub(r"\D", "", nasc_raw)
            if len(digits) == 8 and digits[:4].isdigit() and int(digits[:4]) > 1900:
                # ISO YYYYMMDD → DDMMYYYY
                digits = digits[6:8] + digits[4:6] + digits[0:4]
            await fl.locator('#b2-InputBirthdate').evaluate("el => el.scrollIntoView({block:'center'})")
            await fl.locator('#b2-InputBirthdate').press_sequentially(digits, delay=80)
            await fl.locator('#b2-InputBirthdate').press("Tab")
            await page.wait_for_timeout(200)

        # 5. Gênero + Ocupação via JS (sem interação visual)
        sexo = dados.get("sexo", "M")
        radio_id = "#b2-RadioButtonMale-input" if sexo.upper() in ("M", "MASCULINO") else "#b2-RadioButtonFemale-input"
        await frame.evaluate(f"() => document.querySelector('{radio_id}')?.click()")
        await page.wait_for_timeout(200)

        await frame.evaluate("""() => {
            const s = document.querySelector('#b2-DropdownOccupation');
            if (s) { s.value = '3'; s.dispatchEvent(new Event('change', {bubbles: true})); }
        }""")
        await page.wait_for_timeout(400)

        # 6. Estado — inject estados capturados + Choices.js click
        if states_data["items"]:
            await frame.evaluate(_INJECT_STATES_JS, states_data["items"])
            # Abre o combobox Choices.js e clica no item São Paulo
            combobox = fl.locator('.choices[role="combobox"]').first
            await combobox.scroll_into_view_if_needed()
            await combobox.click()
            await page.wait_for_timeout(500)
            sp_item = fl.locator('.choices__item.needsclick').filter(has_text="São Paulo").first
            if await sp_item.count():
                await sp_item.click()
                await page.wait_for_timeout(300)
            print(f"[mag] estado SP selecionado", flush=True)
        else:
            print("[mag] estados não capturados — estado ficará vazio", flush=True)

        # 7. Renda via nativeSetter com valor formatado
        renda_val = dados.get("renda_mensal", "5000")
        try:
            renda_int = int(float(str(renda_val).replace(",", ".").replace(" ", "")))
        except Exception:
            renda_int = 5000
        renda_fmt = "R$ " + f"{renda_int:,}".replace(",", ".") + ",00"
        await frame.evaluate(f"""() => {{
            const inp = document.querySelector('#b2-b19-CurrencyInput');
            if (!inp) return;
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            setter.call(inp, '{renda_fmt}');
            inp.dispatchEvent(new Event('focus', {{bubbles: true}}));
            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
            inp.dispatchEvent(new Event('blur', {{bubbles: true}}));
        }}""")
        await page.wait_for_timeout(300)
        print(f"[mag] renda={renda_fmt}", flush=True)

        # 8. Cônjuge — Não
        await frame.evaluate("() => document.querySelector('#b2-RadioButtonSpouseNo-input')?.click()")
        await page.wait_for_timeout(200)

        # 9. AVANÇAR
        await frame.evaluate("""() => {
            const av = [...document.querySelectorAll('button')]
                .find(b => /avan[çc]ar/i.test(b.textContent || ''));
            if (av) av.click();
        }""")
        await page.wait_for_timeout(5000)
        print(f"[mag] pós-avançar → {page.url}", flush=True)

    finally:
        try:
            page.remove_listener("response", _on_states)
        except Exception:
            pass


async def _extrair_coberturas(page: Page) -> list[Cobertura]:
    coberturas = []
    try:
        frame = next((f for f in page.frames if "outsystems" in f.url or "simple2u" in f.url), None)
        if not frame:
            return coberturas

        # Aguarda página de produtos carregar
        try:
            await frame.wait_for_selector('text=Selecionar Produtos', timeout=10_000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)

        # Extrai nomes do inner_text — linhas com padrão "NOME - RIDER (XXXX)" ou "(XXXX)"
        txt = await frame.inner_text("body")
        vistos: set[str] = set()
        for linha in txt.splitlines():
            nome = linha.strip()
            if (
                nome
                and len(nome) >= 5
                and re.search(r'\(\d{3,}\)$', nome)
                and "VER DETALHES" not in nome.upper()
                and nome not in vistos
            ):
                vistos.add(nome)
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
        frame = next((f for f in page.frames if "outsystems" in f.url or "simple2u" in f.url), None)
        if not frame:
            raise Exception("iframe OutSystems não encontrado em fase2")

        fl = page.frame_locator("iframe[src*='simple2u'], iframe[src*='outsystems']").first

        # Seleciona cada produto clicando no item da lista
        for sel in selecoes:
            nome_curto = sel["nome"][:50]
            item = fl.locator(f'text="{nome_curto}"').first
            if await item.count():
                await item.click()
                await page.wait_for_timeout(400)

        # AVANÇAR
        await frame.evaluate("""() => {
            const av = [...document.querySelectorAll('button')]
                .find(b => /avan[çc]ar/i.test(b.textContent || ''));
            if (av) av.click();
        }""")
        await page.wait_for_timeout(5000)

        txt = await page.inner_text("body")
        m = re.search(r'R\$\s*([\d.]+),([\d]{2})', txt)
        premio = float(m.group(1).replace('.', '') + '.' + m.group(2)) if m else 0.0

        for sel in selecoes:
            resultados.append(ResultadoCotacao(
                seguradora="mag",
                cobertura_nome=sel["nome"],
                valor_capital=float(sel.get("valor", 0)),
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
