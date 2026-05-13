"""Automação AZOS — portal corretores (novo UI 2025)."""
from __future__ import annotations
import os, re, asyncio, tempfile
from pathlib import Path
from playwright.async_api import Page
from .base import novo_browser, fechar_browser, clicar_continuar, extrair_valor_monetario, resolver_captcha
from models import Cobertura, ResultadoFase1, ResultadoCotacao

URL_LOGIN = "https://corretores.azos.com.br/login"
URL_SIM   = "https://contratacao.azos.com.br/simulacao/dados-pessoais"
EMAIL = os.getenv("AZOS_EMAIL", "")
SENHA = os.getenv("AZOS_SENHA", "")

_TMP = Path(tempfile.gettempdir())

# Sessões abertas aguardando fase 2
_SESSOES: dict[str, dict] = {}


# ─── FASE 1: login + dados pessoais + coleta de coberturas ───────────────────

async def fase1_coletar_coberturas(dados: dict, headless: bool = True) -> ResultadoFase1:
    pw, browser, ctx, page = await novo_browser(headless)
    session_id = dados.get("session_id", "azos-" + str(id(page)))
    try:
        print(f"[azos] iniciando fase1 session={session_id}", flush=True)

        # Login
        await page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_selector('input[name="email"]', timeout=15_000)
        await page.fill('input[name="email"]', EMAIL)
        await page.fill('input[name="password"]', SENHA)
        await resolver_captcha(page)
        await page.click('button[type="submit"]')
        # Aguarda saída da página de login (domcontentloaded evita timeout por recursos lentos)
        for _ in range(60):
            await page.wait_for_timeout(1000)
            if "login" not in page.url:
                break
        else:
            raise Exception(f"Login AZOS falhou — ainda em: {page.url}")
        print(f"[azos] login ok → {page.url}", flush=True)

        # Navegação para simulação
        await page.goto(URL_SIM, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)
        print(f"[azos] dados-pessoais → {page.url}", flush=True)

        # "Novo cliente" — tenta silenciosamente
        for sel in ['button:has-text("Novo cliente")', 'text="Novo cliente"', '[data-testid*="new"]']:
            try:
                el = page.locator(sel).first
                if await el.count() and await el.is_visible():
                    await el.click()
                    await page.wait_for_timeout(600)
                    break
            except Exception:
                pass

        # Preenche dados pessoais
        await _preencher_dados_pessoais(page, dados)
        await page.wait_for_timeout(500)

        # Avança para coberturas
        cont = page.locator('button:has-text("Continuar")').first
        try:
            await cont.click(timeout=5_000)
        except Exception:
            await cont.click(force=True)
        await page.wait_for_timeout(2000)

        for _ in range(10):
            if "dados-pessoais" not in page.url:
                break
            await page.wait_for_timeout(1000)
        print(f"[azos] pós-continuar → {page.url}", flush=True)

        await page.wait_for_load_state("domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(1500)

        # Aguarda React renderizar os toggles de cobertura (novo UI: button.min-w-11)
        try:
            await page.wait_for_selector(
                'button.min-w-11.min-h-11[data-slot="tooltip-trigger"]',
                timeout=10_000,
            )
        except Exception:
            # Fallback UI antigo
            try:
                await page.wait_for_selector('button[role="switch"]', timeout=5_000)
            except Exception:
                pass

        coberturas = await _extrair_coberturas(page)
        print(f"[azos] {len(coberturas)} coberturas extraídas | url={page.url}", flush=True)

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
    # Nome
    nome = d.get("nome", "")
    if nome:
        await page.locator('input[name="fullName"]').fill(nome)
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(200)

    # Data de nascimento (campo mascarado dd/mm/aaaa)
    nasc = d.get("nascimento", "")
    if nasc:
        digits = re.sub(r"\D", "", nasc)
        nasc_inp = page.locator('input[name="birthDate"]').first
        await nasc_inp.click()
        await nasc_inp.press_sequentially(digits, delay=80)
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(200)

    # Altura em cm (ex: "175")
    try:
        h_inp = page.locator('input[name="height"]').first
        if await h_inp.count() and await h_inp.is_visible():
            await h_inp.click()
            await h_inp.press_sequentially("175", delay=50)
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(200)
    except Exception:
        pass

    # Peso em kg
    try:
        w_inp = page.locator('input[name="weight"]').first
        if await w_inp.count() and await w_inp.is_visible():
            await w_inp.fill("70")
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(200)
    except Exception:
        pass

    # Profissão — Radix UI cmdk: trigger + busca + clique na opção
    profissao_busca = d.get("profissao", "Advogado")
    try:
        prof_btn = page.locator('button[name="professionId"]').first
        if await prof_btn.count() and await prof_btn.is_visible():
            await prof_btn.click(force=True)
            # Aguarda o dialog/cmdk abrir (headless pode ser mais lento)
            try:
                await page.wait_for_selector('input[cmdk-input]', state='visible', timeout=5_000)
            except Exception:
                pass
            prof_inp = page.locator('input[cmdk-input]').first
            if await prof_inp.count():
                await prof_inp.fill(profissao_busca)
                await page.wait_for_timeout(800)
                opt = page.locator('[cmdk-item][role="option"]').first
                if await opt.count():
                    await opt.click()
                    await page.wait_for_timeout(400)
                else:
                    # Fallback: ArrowDown + Enter seleciona a primeira opção disponível
                    await page.keyboard.press("ArrowDown")
                    await page.wait_for_timeout(200)
                    await page.keyboard.press("Enter")
            else:
                await page.keyboard.press("Escape")
    except Exception:
        pass
    await page.wait_for_timeout(300)

    # Renda mensal
    renda_val = d.get("renda_mensal", "5000")
    try:
        renda_int = int(float(str(renda_val).replace(",", ".").replace(" ", "")))
    except Exception:
        renda_int = 5000
    try:
        renda_inp = page.locator('input[name="monthlyIncome"]').first
        if await renda_inp.count() and await renda_inp.is_visible():
            await renda_inp.click(click_count=3)
            await renda_inp.fill(str(renda_int))
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(200)
    except Exception:
        pass

    # Gênero: labels com input[name="gender"]
    sexo = d.get("sexo", "M")
    sexo_idx = 1 if sexo.upper() in ("M", "MASCULINO") else 0
    try:
        lbl = page.locator('label:has(input[name="gender"])').nth(sexo_idx)
        if await lbl.count():
            await lbl.click()
            await page.wait_for_timeout(200)
    except Exception:
        pass

    # Fumante: primeiro label = Não fumante
    try:
        lbl_smoke = page.locator('label:has(input[name="isSmoker"])').first
        if await lbl_smoke.count():
            await lbl_smoke.click()
            await page.wait_for_timeout(200)
    except Exception:
        pass


async def _preencher_selects(page: Page, d: dict):
    mapeamentos = [
        ('select[name="gender"], [aria-label*="sexo" i]',         d.get("sexo", "M")),
        ('select[name="marital"], [aria-label*="estado civil" i]', d.get("estado_civil", "")),
        ('input[name="income"], input[placeholder*="renda" i]',    d.get("renda_mensal", "5000")),
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
                await el.select_option(label=str(val))
            else:
                await el.fill(str(val))
            await page.wait_for_timeout(150)
        except Exception:
            pass


# ─── Extrai coberturas (novo UI 2025: toggle bg-black/bg-primary) ─────────────

async def _extrair_coberturas(page: Page) -> list[Cobertura]:
    coberturas = []
    try:
        await page.wait_for_timeout(1000)
        raw = await page.evaluate("""() => {
            const result = [];
            const seen = new Set();

            function lerMinMax(container) {
                // Tenta ler min/max do input de capital dentro do card da cobertura
                const inp = container.querySelector(
                    'input[type="tel"], input[type="number"], input[name*="capital"], input[name*="valor"]'
                );
                let vMin = 50000, vMax = 5000000;
                if (inp) {
                    const minA = parseFloat(inp.getAttribute('min') || '0');
                    const maxA = parseFloat(inp.getAttribute('max') || '0');
                    if (minA > 0) vMin = minA;
                    if (maxA > 0) vMax = maxA;
                }
                return { vMin, vMax };
            }

            // Novo UI 2025: toggle buttons min-w-11 min-h-11 data-slot="tooltip-trigger"
            const toggles = document.querySelectorAll(
                'button.min-w-11.min-h-11[data-slot="tooltip-trigger"]'
            );
            toggles.forEach(btn => {
                let container = btn.parentElement;
                for (let i = 0; i < 8; i++) {
                    if (!container) break;
                    const h3 = container.querySelector('h3');
                    if (h3) {
                        const nome = h3.innerText.trim();
                        if (!nome || nome.length < 3 || seen.has(nome)) {
                            container = container.parentElement;
                            continue;
                        }
                        seen.add(nome);
                        const descEl = container.querySelector('p');
                        const cls = btn.className || '';
                        const ativo = cls.includes('bg-primary') && !cls.includes('bg-black');
                        const { vMin, vMax } = lerMinMax(container);
                        result.push({
                            nome: nome.substring(0, 80),
                            descricao: descEl ? descEl.innerText.trim().substring(0, 150) : '',
                            ativo,
                            valor_min: vMin,
                            valor_max: vMax,
                        });
                        break;
                    }
                    container = container.parentElement;
                }
            });

            // Fallback UI antigo: header + switch
            if (result.length === 0) {
                document.querySelectorAll('header').forEach(header => {
                    const sw = header.querySelector('button[role="switch"]');
                    const h3 = header.querySelector('h3');
                    if (!sw || !h3) return;
                    const nome = h3.innerText.trim();
                    if (!nome || nome.length < 3 || seen.has(nome)) return;
                    seen.add(nome);
                    const container = header.parentElement;
                    const descEl = container ? container.querySelector('p') : null;
                    const { vMin, vMax } = lerMinMax(container || header);
                    result.push({
                        nome: nome.substring(0, 80),
                        descricao: descEl ? descEl.innerText.trim().substring(0, 150) : '',
                        ativo: sw.getAttribute('aria-checked') === 'true',
                        valor_min: vMin,
                        valor_max: vMax,
                    });
                });
            }
            return result;
        }""")

        vistos: set[str] = set()
        for item in raw:
            nome = item.get("nome", "").strip()
            if nome and nome not in vistos and nome != "Indisponível" and len(nome) > 3:
                vistos.add(nome)
                v_min = item.get("valor_min") or 50_000
                v_max = item.get("valor_max") or 1_000_000
                coberturas.append(Cobertura(
                    id=f"azos_{re.sub(r'[^a-z0-9]', '_', nome.lower()[:30])}",
                    nome=nome,
                    descricao=item.get("descricao", ""),
                    valor_min=float(v_min),
                    valor_max=float(v_max),
                    premio_referencia=0.0,
                    seguradora="azos",
                ))

    except Exception as e:
        print(f"[azos] erro ao extrair coberturas: {e}", flush=True)

    return coberturas


# ─── FASE 2: seleciona coberturas e finaliza cotação ─────────────────────────

async def fase2_finalizar(session_id: str, selecoes: list[dict]) -> list[ResultadoCotacao]:
    """selecoes = [{"nome": str, "valor": float}, ...]"""
    sessao = _SESSOES.get(session_id)
    if not sessao:
        return [ResultadoCotacao(
            seguradora="azos", cobertura_nome="", valor_capital=0,
            premio_mensal=0, erro="Sessão expirada — reinicie a coleta",
        )]

    page: Page = sessao["page"]
    dados_cliente = sessao.get("dados", {})
    resultados: list[ResultadoCotacao] = []

    try:
        # Esconde popup do chat que pode bloquear botões
        await page.add_style_tag(content="""
            [class*='chat'],[class*='copilot'],[class*='widget'],
            [class*='Chat'],[class*='Copilot'],[class*='Widget'],
            iframe[src*='chat'],iframe[src*='copilot'] {
                display:none!important;pointer-events:none!important;
            }
        """)

        # Garante que está na página de coberturas
        if "coberturas" not in page.url and "simulacao" not in page.url:
            await page.goto(
                "https://contratacao.azos.com.br/simulacao/coberturas",
                wait_until="domcontentloaded", timeout=20_000,
            )
            await page.wait_for_timeout(1500)

        # Reseta: desliga todas coberturas selecionadas (novo UI + legacy)
        await page.evaluate("""() => {
            document.querySelectorAll(
                'button.min-w-11.min-h-11.bg-primary[data-slot="tooltip-trigger"]'
            ).forEach(btn => btn.click());
            document.querySelectorAll(
                'button[role="switch"][aria-checked="true"]'
            ).forEach(sw => sw.click());
        }""")
        await page.wait_for_timeout(800)

        # Seleciona cada cobertura
        for sel in selecoes:
            await _selecionar_cobertura(page, sel["nome"], float(sel["valor"]))

        await page.wait_for_timeout(1000)

        # Avança — "Ir para o Resumo" ou "Continuar"
        await _clicar_continuar_azos(page)
        await page.wait_for_timeout(3000)

        # Loop principal: DPS → cadastro → resultado
        premio = await _percorrer_fluxo(page, dados_cliente)

        for sel in selecoes:
            resultados.append(ResultadoCotacao(
                seguradora="azos",
                cobertura_nome=sel["nome"],
                valor_capital=float(sel["valor"]),
                premio_mensal=premio / max(len(selecoes), 1) if premio else 0,
                link_proposta=page.url,
            ))

    except Exception as e:
        print(f"[azos] ERRO fase2: {e}", flush=True)
        resultados.append(ResultadoCotacao(
            seguradora="azos", cobertura_nome="Erro", valor_capital=0,
            premio_mensal=0, erro=str(e),
        ))
    finally:
        sess = _SESSOES.pop(session_id, None)
        if sess:
            await fechar_browser(sess["pw"], sess["browser"])

    return resultados


async def _percorrer_fluxo(page: Page, dados_cliente: dict) -> float | None:
    """Avança por DPS → cadastro → resultado e retorna prêmio mensal."""
    _saude_urls = ("dps", "saude", "health", "declaracao", "questionario")

    for iteracao in range(60):
        await page.wait_for_timeout(1500)
        url = page.url

        # Chegou ao resultado
        if any(k in url for k in ["resultado", "proposta", "checkout", "pagamento", "sucesso", "proposta-enviada"]):
            break

        # Preenche DPS
        if any(k in url for k in _saude_urls):
            await _preencher_dps_completo(page, {})
            await page.wait_for_timeout(2000)
            if not any(k in page.url for k in _saude_urls):
                continue

        # Preenche cadastro
        if "cadastro" in url:
            await _preencher_cadastro(page, dados_cliente)
            await page.wait_for_timeout(1000)
            avancou = await _clicar_continuar_azos(page)
            if not avancou:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(500)
                await _clicar_continuar_azos(page)
            await page.wait_for_timeout(3000)
            continue

        # Avança etapas desconhecidas
        avancou = await _clicar_continuar_azos(page)
        if not avancou:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(500)
            avancou = await _clicar_continuar_azos(page)
            if not avancou:
                break
        await page.wait_for_timeout(2000)

    txt = await page.inner_text("body")
    return _extrair_premio_mensal(txt)


# ─── Selecionar cobertura (novo UI 2025) ─────────────────────────────────────

async def _selecionar_cobertura(page: Page, nome: str, valor: float):
    """Ativa toggle da cobertura e define valor via fill()."""
    nome_curto = nome[:30]
    print(f"[azos] selecionando '{nome_curto}' valor={valor}", flush=True)
    try:
        toggle = None
        for sel in [
            f'div:has(h3:has-text("{nome_curto}")) button.min-w-11[data-slot="tooltip-trigger"]',
            f'*:has(h3:has-text("{nome_curto}")) button.min-w-11',
            f'*:has(h3:has-text("{nome_curto}")) button.bg-black',
            f'header:has(h3:has-text("{nome_curto}")) button[role="switch"]',
            f'*:has(h3:has-text("{nome_curto}")) button[role="switch"]',
        ]:
            candidate = page.locator(sel).first
            if await candidate.count():
                toggle = candidate
                break

        if not toggle:
            print(f"[azos] toggle não encontrado: '{nome_curto}'", flush=True)
            return

        await toggle.scroll_into_view_if_needed()
        await page.wait_for_timeout(400)

        cls = await toggle.get_attribute("class") or ""
        aria_checked = await toggle.get_attribute("aria-checked")
        is_selected = ("bg-primary" in cls and "bg-black" not in cls) or aria_checked == "true"

        if not is_selected:
            await toggle.click()
            await page.wait_for_timeout(1000)
            cls = await toggle.get_attribute("class") or ""
            is_selected = "bg-primary" in cls and "bg-black" not in cls
            if not is_selected:
                bbox = await toggle.bounding_box()
                if bbox:
                    await page.mouse.click(bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2)
                    await page.wait_for_timeout(1000)

        if valor <= 0:
            return

        # Input de valor: painel direito (novo UI) ou card direto (legado)
        inp = None
        try:
            card = page.locator('div.border.border-neutral-100.p-4').filter(has_text=nome_curto).first
            if await card.count():
                inp_c = card.locator('input[type="tel"]').first
                if await inp_c.count():
                    inp = inp_c
        except Exception:
            pass

        if not inp:
            inp_all = page.locator('input[type="tel"]')
            if await inp_all.count():
                inp = inp_all.first

        if not inp:
            for sel in [
                f'*:has(h3:has-text("{nome_curto}")) input[type="tel"]',
                f'*:has(h3:has-text("{nome_curto}")) input[type="number"]',
            ]:
                c = page.locator(sel).first
                if await c.count():
                    inp = c
                    break

        if not inp:
            print(f"[azos] input não encontrado: '{nome_curto}'", flush=True)
            return

        try:
            await inp.wait_for(state="visible", timeout=5_000)
        except Exception:
            pass

        # fill() define valor numérico bruto (site formata como moeda)
        await inp.fill(str(int(valor)))
        await page.wait_for_timeout(600)
        print(f"[azos] valor definido via fill: {int(valor)}", flush=True)

    except Exception as e:
        print(f"[azos] ERRO selecionando '{nome_curto}': {e}", flush=True)


# ─── Botão avançar ────────────────────────────────────────────────────────────

async def _clicar_continuar_azos(page: Page) -> bool:
    """Clica em 'Ir para o Resumo', 'Continuar', etc. Ignora botões disabled."""
    seletores = [
        'button:has-text("Ir para o Resumo")',
        'button:has-text("Continuar")',
        'button:has-text("Próximo")',
        'button:has-text("Avançar")',
        'button:has-text("Ver cotação")',
        'button:has-text("Calcular")',
        'button:has-text("Finalizar")',
        'button:has-text("Confirmar")',
        'button[type="submit"]',
    ]
    for sel in seletores:
        try:
            all_btns = page.locator(sel)
            count = await all_btns.count()
            if not count:
                continue
            for i in range(count):
                btn = all_btns.nth(i)
                try:
                    if not await btn.is_visible():
                        continue
                    if await btn.get_attribute("disabled") is not None:
                        continue
                    if await btn.get_attribute("aria-disabled") == "true":
                        continue
                    await btn.click()
                    return True
                except Exception:
                    continue
        except Exception:
            pass

    # Fallback por coordenadas JS
    try:
        coords = await page.evaluate("""() => {
            const textos = ['Ir para o Resumo','Ver cotação','Continuar','Próximo','Avançar','Calcular','Finalizar','Confirmar'];
            for (const txt of textos) {
                const btns = [...document.querySelectorAll('button')].filter(b =>
                    b.textContent.trim().includes(txt) && !b.disabled &&
                    b.getAttribute('aria-disabled') !== 'true'
                );
                for (const btn of btns) {
                    const r = btn.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0)
                        return {x: r.left + r.width/2, y: r.top + r.height/2};
                }
            }
            return null;
        }""")
        if coords:
            await page.mouse.click(coords["x"], coords["y"])
            return True
    except Exception:
        pass

    return False


# ─── DPS — Declaração Pessoal de Saúde ────────────────────────────────────────

async def _preencher_dps_completo(page: Page, saude: dict):
    """Loop 40 iterações respondendo perguntas de saúde até sair de /dps."""
    _saude_urls = ("dps", "saude", "health", "declaracao", "questionario")
    print(f"[azos][dps] iniciando, url={page.url}", flush=True)

    for iteracao in range(40):
        await page.wait_for_timeout(1200)

        url_atual = page.url
        if not any(k in url_atual for k in _saude_urls):
            print(f"[azos][dps] saindo (url sem dps): {url_atual}", flush=True)
            break

        texto = (await page.inner_text("body")).lower()

        e_vinculo = any(k in texto for k in [
            "vínculo profissional", "vinculo profissional",
            "selecione o seu vínculo", "carteira assinada", "clt",
            "sou dono e trabalho", "autonomo informal", "autônomo informal",
            "atividades manuais", "profissão envolve", "profissao envolve",
            "somente atividades administrativas",
        ])
        tem_nenhum = any(k in texto for k in [
            "nenhum desses", "nenhuma desses", "nenhuma delas",
            "nenhum delas", "nenhuma dessas", "nenhum dos anteriores",
            "nenhuma das anteriores", "selecione todas as opções",
        ])
        e_estilo = any(k in texto for k in [
            "estilo de vida", "atividade física", "atividade fisica",
            "exercício", "exercicio", "bebida alcoólica", "bebida alcoolica",
            "álcool", "alcool", "cigarro", "tabaco", "frequência", "frequencia",
        ])

        async def _responder():
            if e_vinculo:
                return await _selecionar_vinculo_profissional(page)
            elif tem_nenhum:
                return await _clicar_nenhum_desses(page)
            elif e_estilo:
                return await _responder_estilo_vida(page)
            else:
                return await _selecionar_nao_exato(page)

        selecionou = await _responder()
        print(f"[azos][dps] iter={iteracao} selecionou={selecionou} vinculo={e_vinculo} nenhum={tem_nenhum} estilo={e_estilo}", flush=True)

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1500)

        avancou = await _clicar_continuar_azos(page)
        if not avancou:
            await _responder()
            await page.wait_for_timeout(2500)
            avancou = await _clicar_continuar_azos(page)
            if not avancou:
                try:
                    await page.evaluate("""() => {
                        const textos = ['Continuar','Próximo','Avançar','Ver cotação','Calcular'];
                        const btn = [...document.querySelectorAll('button')].find(b =>
                            textos.some(t => b.textContent.trim().startsWith(t)) && !b.getAttribute('disabled')
                        );
                        if (btn) btn.click();
                    }""")
                    await page.wait_for_timeout(2000)
                    avancou = page.url != url_atual
                except Exception:
                    pass

        print(f"[azos][dps] iter={iteracao} avancou={avancou}", flush=True)
        await page.wait_for_timeout(2000)

        if not any(k in page.url for k in _saude_urls):
            print(f"[azos][dps] saindo pós-continuar: {page.url}", flush=True)
            break


async def _selecionar_vinculo_profissional(page: Page) -> bool:
    await _fechar_popup_chat(page)
    await page.wait_for_timeout(400)

    preferencias = [
        "mais de 15", "3 a 15", "Sou dono",
        "Autonomo informal", "carteira assinada",
        "Não, somente atividades administrativas",
        "somente atividades administrativas", "Não",
    ]
    for pref in preferencias:
        try:
            el = page.locator("label").filter(has_text=pref).first
            if await el.count():
                await el.click(force=True)
                await page.wait_for_timeout(500)
                return True
            btn = page.locator("button").filter(has_text=pref).first
            if await btn.count():
                await btn.click(force=True)
                await page.wait_for_timeout(500)
                return True
        except Exception:
            pass

    try:
        radio = page.locator('button[role="radio"]').first
        if await radio.count():
            await radio.click(force=True)
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass

    return False


async def _fechar_popup_chat(page: Page):
    try:
        for sel in [
            'button[aria-label*="fechar"]', 'button[aria-label*="Fechar"]',
            'button[aria-label*="close"]', 'button[aria-label*="Close"]',
            'button:has-text("×")', 'button:has-text("✕")',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(300)
                    return
            except Exception:
                pass
        await page.evaluate("""() => {
            document.querySelectorAll('*').forEach(el => {
                const txt = (el.innerText || '').toLowerCase();
                if ((txt.includes('copiloto') || txt.includes('olá!')) &&
                    getComputedStyle(el).position === 'fixed') {
                    el.style.display = 'none';
                }
            });
        }""")
    except Exception:
        pass


async def _clicar_nenhum_desses(page: Page) -> bool:
    await _fechar_popup_chat(page)
    await page.wait_for_timeout(300)

    try:
        labels = await page.locator("label").all()
        for lbl in labels:
            try:
                txt = (await lbl.inner_text()).strip().lower()
            except Exception:
                continue
            if not (txt.startswith("nenhum") or txt.startswith("nenhuma")):
                continue
            try:
                await lbl.scroll_into_view_if_needed()
                await page.wait_for_timeout(300)
            except Exception:
                pass
            btn_cb = lbl.locator('button[role="checkbox"]').first
            if await btn_cb.count():
                await btn_cb.click(force=True)
                await page.wait_for_timeout(400)
                return True
            await lbl.click(force=True)
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass

    try:
        cb = page.get_by_role("checkbox").last
        if await cb.count():
            await cb.scroll_into_view_if_needed()
            await cb.click(force=True)
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass

    coords = await page.evaluate("""() => {
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            const txt = node.textContent.trim().toLowerCase();
            if (txt.startsWith('nenhum') || txt.startsWith('nenhuma')) {
                const el = node.parentElement;
                if (!el) continue;
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.height > 0)
                    return {x: r.left + r.width/2, y: r.top + r.height/2};
            }
        }
        return null;
    }""")
    if coords:
        try:
            await page.mouse.click(coords["x"], coords["y"])
            await page.wait_for_timeout(400)
            return True
        except Exception:
            pass

    return False


async def _selecionar_nao_exato(page: Page) -> bool:
    await _fechar_popup_chat(page)
    await page.wait_for_timeout(300)

    coords = await page.evaluate("""() => {
        function isNao(txt) {
            const t = txt.trim().toLowerCase();
            return t === 'não.' || t === 'não' || t === 'nao.' || t === 'nao' ||
                   (t.startsWith('não') && t.length < 100) ||
                   (t.startsWith('nao') && t.length < 100);
        }
        for (const el of document.querySelectorAll('button, label, [role="radio"], [role="option"]')) {
            const txt = (el.innerText || el.textContent || '').trim();
            if (!isNao(txt)) continue;
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0)
                return {x: r.left + r.width/2, y: r.top + r.height/2};
        }
        for (const el of document.querySelectorAll('label, p, span, div, li')) {
            const txt = (el.innerText || '').trim();
            if (!isNao(txt)) continue;
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0)
                return {x: r.left + r.width/2, y: r.top + r.height/2};
        }
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            const t = node.textContent.trim().toLowerCase();
            if (t === 'não.' || t === 'não' || t === 'nao.' || t === 'nao') {
                const pai = node.parentElement;
                if (!pai) continue;
                const r = pai.getBoundingClientRect();
                if (r.width > 0 && r.height > 0)
                    return {x: r.left + r.width/2, y: r.top + r.height/2};
            }
        }
        return null;
    }""")
    if coords:
        try:
            await page.mouse.click(coords["x"], coords["y"])
            await page.wait_for_timeout(400)
            return True
        except Exception:
            pass

    NAO_EXATOS = {"não.", "não", "nao.", "nao"}
    try:
        labels = await page.locator("label").all()
        for lbl in reversed(labels):
            try:
                txt = (await lbl.inner_text()).strip().lower()
            except Exception:
                continue
            if txt not in NAO_EXATOS and not txt.startswith("não") and not txt.startswith("nao"):
                continue
            btn_radio = lbl.locator('button[role="radio"]').first
            if await btn_radio.count():
                await btn_radio.scroll_into_view_if_needed()
                await btn_radio.click(force=True)
                await page.wait_for_timeout(400)
                return True
            await lbl.scroll_into_view_if_needed()
            await lbl.click(force=True)
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass

    for nome_radio in ["Não.", "Não"]:
        try:
            radio = page.get_by_role("radio", name=nome_radio, exact=True)
            if await radio.count():
                await radio.last.click(force=True)
                await page.wait_for_timeout(400)
                return True
        except Exception:
            pass

    try:
        btns_grupo = await page.locator('div[role="radiogroup"] button[role="radio"]').all()
        if btns_grupo:
            await btns_grupo[-1].scroll_into_view_if_needed()
            await btns_grupo[-1].click(force=True)
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass

    try:
        radios = await page.locator('input[type="radio"], [role="radio"]').all()
        if radios:
            await radios[-1].click(force=True)
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass

    return False


async def _responder_estilo_vida(page: Page) -> bool:
    if await _selecionar_nao_exato(page):
        return True

    opcoes_saudaveis = [
        "Não faço", "Não pratico", "Não consumo", "Não fumo",
        "Nunca", "Nunca fumei", "Menos de 1 vez", "Raramente",
        "Não bebo", "Não uso",
    ]
    for opcao in opcoes_saudaveis:
        try:
            radio = page.get_by_role("radio", name=opcao, exact=False)
            if await radio.count():
                await radio.first.click()
                await page.wait_for_timeout(300)
                return True
            lbl = page.locator(f'label:has-text("{opcao}")')
            if await lbl.count() and await lbl.first.is_visible():
                await lbl.first.click()
                await page.wait_for_timeout(300)
                return True
        except Exception:
            pass

    try:
        radios = await page.locator('input[type="radio"]').all()
        if radios:
            await radios[0].click()
            await page.wait_for_timeout(300)
            return True
    except Exception:
        pass

    return False


# ─── Cadastro ─────────────────────────────────────────────────────────────────

async def _preencher_cadastro(page: Page, cliente: dict):
    """Preenche /contratacao/cadastro (novo UI 2025)."""
    await page.wait_for_timeout(500)

    # CPF
    cpf = str(cliente.get("cpf", "")).replace(".", "").replace("-", "").strip()
    if cpf:
        try:
            inp = page.locator('input[name="cpf"]').first
            if await inp.count() and await inp.is_visible():
                await inp.click()
                await inp.fill("")
                await inp.type(cpf, delay=40)
                await inp.press("Tab")
                for _ in range(8):
                    await page.wait_for_timeout(500)
                    corpo = (await page.inner_text("body")).lower()
                    if "vinculado ao e-mail" in corpo or "esse cpf" in corpo:
                        break
                await _fechar_modal_cpf_vinculado(page)
                await page.wait_for_timeout(500)
        except Exception:
            pass

    # Telefone
    tel = str(cliente.get("telefone", "")).replace("(", "").replace(")", "").replace(" ", "").replace("-", "").replace("+", "")
    if tel.startswith("55") and len(tel) in (11, 12, 13):
        candidate = tel[2:]
        if len(candidate) in (9, 10, 11):
            tel = candidate
    if len(tel) == 10 and tel[2] != "9":
        tel = tel[:2] + "9" + tel[2:]
    if tel:
        try:
            inp = page.locator('input[name="phone"]').first
            if await inp.count() and await inp.is_visible():
                await inp.fill(tel)
                await page.wait_for_timeout(400)
        except Exception:
            pass

    # Email
    email = str(cliente.get("email", "")).strip()
    if email:
        try:
            inp = page.locator('input[name="email"]').first
            if await inp.count() and await inp.is_visible():
                await inp.fill(email)
                await page.wait_for_timeout(400)
        except Exception:
            pass

    # Estado civil — novo UI: button[role="radio"] sem wrapper div[name="marital_status"]
    estado = str(cliente.get("estado_civil", "Solteira/o"))
    try:
        btn = page.locator('button[role="radio"]').filter(has_text=estado).first
        if not await btn.count():
            btn = page.locator('button[role="radio"]').first
        if await btn.count() and await btn.is_visible():
            await btn.click(force=True)
            await page.wait_for_timeout(400)
    except Exception:
        pass

    # PEP "Não" — botões são círculos puros com innerText VAZIO
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(400)
        coords = await page.evaluate("""() => {
            // button[role="radio"] com innerText vazio = botões PEP (círculos)
            const emptyBtns = [...document.querySelectorAll('button[role="radio"]')]
                .filter(b => !b.innerText.trim());
            const naoPep = emptyBtns[emptyBtns.length - 1];
            if (naoPep) {
                naoPep.scrollIntoView({block: 'center'});
                const r = naoPep.getBoundingClientRect();
                if (r.width >= 10) return {x: r.left + r.width/2, y: r.top + r.height/2, src:'empty-btn'};
            }
            // Fallback: input radio nativo visível
            const inp = document.querySelector(
                'input[type="radio"][name="is_politically_exposed_person"][value="false"]'
            );
            if (inp) {
                inp.scrollIntoView({block: 'center'});
                const r = inp.getBoundingClientRect();
                if (r.width >= 10) return {x: r.left + r.width/2, y: r.top + r.height/2, src:'input'};
            }
            // Fallback: nó de texto "Não" → clica no pai
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                if (node.textContent.trim() !== 'Não') continue;
                const pai = node.parentElement;
                if (!pai) continue;
                const r = pai.getBoundingClientRect();
                if (r.width >= 5) return {x: r.left + r.width/2, y: r.top + r.height/2, src:'text-node'};
            }
            return null;
        }""")
        if coords:
            await page.wait_for_timeout(200)
            await page.mouse.click(coords["x"], coords["y"])
            await page.wait_for_timeout(400)
    except Exception:
        pass

    await page.wait_for_timeout(500)
    await _fechar_modal_cpf_vinculado(page)

    # Sub-tela de endereço (mesmo URL /contratacao/cadastro)
    cep_inp = page.locator('input[name="cep"]').first
    if await cep_inp.count() and await cep_inp.is_visible():
        await _preencher_endereco(page, cliente)


async def _preencher_endereco(page: Page, cliente: dict):
    cep = str(cliente.get("cep", "")).replace("-", "").replace(".", "").replace(" ", "").strip()
    if cep and len(cep) == 7:
        cep = cep + "0"
    if cep and len(cep) == 8 and cep.isdigit():
        for sel in ['input[name="cep"]', 'input[name="zipcode"]', 'input[name="zip_code"]']:
            try:
                inp = page.locator(sel).first
                if await inp.count() and await inp.is_visible():
                    await inp.click()
                    await inp.fill("")
                    await inp.type(cep, delay=80)
                    for _ in range(12):
                        await page.wait_for_timeout(500)
                        preencheu = await page.evaluate("""() => {
                            const sels = ['input[name="state"]','input[name="city"]',
                                          'input[name="street"]','input[name="neighborhood"]'];
                            for (const s of sels) {
                                const el = document.querySelector(s);
                                if (el && (el.value||'').trim().length > 0) return true;
                            }
                            return false;
                        }""")
                        if preencheu:
                            break
                    break
            except Exception:
                pass

    numero = str(cliente.get("numero", ""))
    if numero:
        for sel in ['input[name="number"]', 'input[name="numero"]', 'input[name="street_number"]']:
            try:
                inp = page.locator(sel).first
                if await inp.count() and await inp.is_visible():
                    await inp.click()
                    await inp.fill("")
                    await inp.type(numero, delay=40)
                    await page.wait_for_timeout(300)
                    break
            except Exception:
                pass

    complemento = str(cliente.get("complemento", ""))
    if complemento:
        for sel in ['input[name="complement"]', 'input[name="complemento"]']:
            try:
                inp = page.locator(sel).first
                if await inp.count() and await inp.is_visible():
                    await inp.click()
                    await inp.fill("")
                    await inp.type(complemento, delay=40)
                    await page.wait_for_timeout(300)
                    break
            except Exception:
                pass

    await page.wait_for_timeout(500)


async def _fechar_modal_cpf_vinculado(page: Page) -> bool:
    try:
        corpo = await page.inner_text("body")
        if "vinculado ao e-mail" not in corpo.lower() and "esse cpf" not in corpo.lower():
            return False
    except Exception:
        return False

    def _aberto(txt: str) -> bool:
        t = txt.lower()
        return "vinculado ao e-mail" in t or "esse cpf" in t

    try:
        coords = await page.evaluate("""() => {
            const SELS = ['[role="dialog"]','[role="alertdialog"]','[data-radix-dialog-content]','[data-state="open"]'];
            for (const sel of SELS) {
                for (const dlg of document.querySelectorAll(sel)) {
                    const r0 = dlg.getBoundingClientRect();
                    if (r0.width < 10 || r0.height < 10) continue;
                    const btns = Array.from(dlg.querySelectorAll('button, a[role="button"]'));
                    const btn = btns.find(b => (b.innerText||b.textContent||'').trim().toLowerCase().includes('continuar'))
                                || btns[btns.length - 1];
                    if (btn) {
                        const r = btn.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0)
                            return {x: r.left + r.width/2, y: r.top + r.height/2};
                    }
                }
            }
            return null;
        }""")
        if coords:
            await page.mouse.click(coords["x"], coords["y"])
            await page.wait_for_timeout(1200)
            if not _aberto(await page.inner_text("body")):
                return True
    except Exception:
        pass

    try:
        await page.evaluate("""() => {
            const SELS = ['[role="dialog"]','[role="alertdialog"]','[data-radix-dialog-content]',
                          '[data-state="open"]','[class*="modal"]','[class*="dialog"]'];
            for (const sel of SELS) {
                document.querySelectorAll(sel).forEach(el => {
                    if ((el.innerText||'').toLowerCase().includes('vinculado')
                        || (el.innerText||'').toLowerCase().includes('esse cpf')) {
                        el.remove();
                    }
                });
            }
            document.querySelectorAll('*').forEach(el => {
                const s = window.getComputedStyle(el);
                if ((s.position==='fixed'||s.position==='absolute') &&
                    parseFloat(s.zIndex||'0') > 100 &&
                    !el.querySelector('input,select,textarea')) {
                    el.style.display = 'none';
                }
            });
        }""")
        await page.wait_for_timeout(600)
        return True
    except Exception:
        pass

    return False


# ─── Helpers de prêmio ────────────────────────────────────────────────────────

def _extrair_premio_mensal(texto: str) -> float | None:
    patterns = [
        r'(?:prêmio|premio|mensalidade|parcela|por\s+mês|por\s+mes)[^\n]{0,60}?R\$\s*([\d\.]+,\d{2})',
        r'R\$\s*([\d\.]+,\d{2})\s*/?\s*(?:mês|mes|mensal)',
        r'(?:mensal|mês|mes)[^\n]{0,30}?R\$\s*([\d\.]+,\d{2})',
    ]
    for pat in patterns:
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(".", "").replace(",", "."))
                if 20 <= val <= 2000:
                    return val
            except Exception:
                pass

    all_vals = re.findall(r'R\$\s*([\d\.]+,\d{2})', texto)
    candidatos = []
    for v in all_vals:
        try:
            f = float(v.replace(".", "").replace(",", "."))
            if 20 <= f <= 5000:
                candidatos.append(f)
        except Exception:
            pass
    return min(candidatos) if candidatos else None
