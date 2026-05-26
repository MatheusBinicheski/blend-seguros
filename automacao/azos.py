"""
Automação Azos — fluxo completo com browser visível
Fase 1: preenche dados pessoais → retorna coberturas disponíveis
Fase 2: seleciona coberturas → preenche saúde/riscos → retorna cotação final
"""
import asyncio, os, uuid, tempfile
from pathlib import Path

AZOS_URL_LOGIN = "https://corretores.azos.com.br/login"
AZOS_URL_SIM   = "https://contratacao.azos.com.br/simulacao/dados-pessoais"
AZOS_EMAIL = os.getenv("AZOS_EMAIL", "grsouza93ip@gmail.com")
AZOS_SENHA = os.getenv("AZOS_SENHA", "1964Dns#*")

# Pasta temporária cross-platform (/tmp no Linux/Mac, %TEMP% no Windows)
_TMP = Path(tempfile.gettempdir())

# Modo headless: True em produção (servidor), False para debug local
_HEADLESS = os.getenv("HEADLESS", "true").lower() not in ("false", "0", "no")

# Sessões ativas: session_id → {playwright, browser, page}
_sessoes: dict = {}


# ──────────────────────────────────────────────────────────────────────────────
# FASE 1 — Preenche dados pessoais e retorna coberturas disponíveis
# ──────────────────────────────────────────────────────────────────────────────
async def fase1_dados_pessoais(cliente: dict) -> dict:
    """
    Abre browser VISÍVEL, faz login e preenche o Step 1 (dados pessoais).
    Avança para Step 2 (coberturas) e extrai as opções disponíveis.

    Retorna:
    {
      "session_id": str,
      "coberturas": [{"id": str, "nome": str, "descricao": str, "valor_max": float, "valor_min": float}],
      "erro": str | None
    }
    """
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    session_id = str(uuid.uuid4())
    resultado  = {"session_id": session_id, "coberturas": [], "erro": None}

    try:
        print(f"[azos][fase1] iniciando session_id={session_id} headless={_HEADLESS}", flush=True)
        pw      = await async_playwright().start()
        _launch_args = [
            "--window-size=1280,900",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process",
            # Esconde fingerprint de automação (navigator.webdriver, etc.)
            "--disable-blink-features=AutomationControlled",
        ]
        print(f"[azos][fase1] lançando chromium...", flush=True)
        browser = await pw.chromium.launch(headless=_HEADLESS, slow_mo=0 if _HEADLESS else 120,
                                           args=_launch_args)
        # Context com user-agent real + init script anti-detecção
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR', 'pt', 'en-US']});
            window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
        """)
        page = await context.new_page()
        print(f"[azos][fase1] browser ok — navegando para login: {AZOS_URL_LOGIN}", flush=True)

        # ── Login ────────────────────────────────────────────────────────
        print(f"[azos][fase1] goto login (domcontentloaded)...", flush=True)
        await page.goto(AZOS_URL_LOGIN, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(2_000)
        print(f"[azos][fase1] login page carregada, url={page.url}", flush=True)
        await page.screenshot(path=str(_TMP / "azos_debug_01_login.png"), full_page=False)

        # Espera o campo email ficar visível antes de preencher
        await page.wait_for_selector('input[name="email"]', timeout=15_000)
        await page.fill('input[name="email"]',    AZOS_EMAIL)
        await page.fill('input[name="password"]', AZOS_SENHA)
        print(f"[azos][fase1] credenciais preenchidas, clicando submit...", flush=True)
        await page.click('button[type="submit"]')
        await page.wait_for_timeout(1_000)

        print(f"[azos][fase1] aguardando redirect para dashboard...", flush=True)
        await page.wait_for_url("**/dashboard**", timeout=30_000)
        print(f"[azos][fase1] login ok — url={page.url}", flush=True)
        await page.screenshot(path=str(_TMP / "azos_debug_02_dashboard.png"), full_page=False)

        # ── Simulação ────────────────────────────────────────────────────
        print(f"[azos][fase1] navegando para simulação: {AZOS_URL_SIM}", flush=True)
        await page.goto(AZOS_URL_SIM, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2_000)
        print(f"[azos][fase1] simulação carregada, url={page.url}", flush=True)
        await page.screenshot(path=str(_TMP / "azos_debug_03_simulacao.png"), full_page=False)

        await page.locator('text="Novo cliente"').click()
        await page.wait_for_timeout(800)
        print(f"[azos][fase1] clicou 'Novo cliente'", flush=True)
        await page.screenshot(path=str(_TMP / "azos_debug_04_novo_cliente.png"), full_page=False)

        # ── Dados pessoais ───────────────────────────────────────────────
        print(f"[azos][fase1] preenchendo dados pessoais...", flush=True)
        await _preencher_dados(page, cliente)
        print(f"[azos][fase1] dados pessoais preenchidos", flush=True)
        await page.screenshot(path=str(_TMP / "azos_debug_05_dados.png"), full_page=False)

        # ── Avança para coberturas ────────────────────────────────────────
        print(f"[azos][fase1] clicando Continuar...", flush=True)
        await page.locator('button:has-text("Continuar")').click()
        await page.wait_for_timeout(4_000)
        await page.wait_for_load_state("domcontentloaded", timeout=20_000)
        print(f"[azos][fase1] avançou para coberturas, url={page.url}", flush=True)
        await page.screenshot(path=str(_TMP / "azos_debug_06_coberturas.png"), full_page=False)

        # ── Extrai coberturas ─────────────────────────────────────────────
        coberturas = await _extrair_coberturas(page)
        resultado["coberturas"] = coberturas
        print(f"[azos][fase1] coberturas extraídas: {len(coberturas)} itens", flush=True)

        # Guarda sessão aberta para Fase 2
        _sessoes[session_id] = {"pw": pw, "browser": browser, "context": context, "page": page}
        print(f"[azos][fase1] sessão salva, retornando resultado", flush=True)

    except PWTimeout as e:
        msg = f"Timeout: {str(e)[:200]}"
        print(f"[azos][fase1] ERRO PWTimeout: {msg}", flush=True)
        resultado["erro"] = msg
        try:
            await page.screenshot(path=str(_TMP / "azos_debug_timeout.png"), full_page=False)
        except Exception:
            pass
    except Exception as e:
        msg = str(e)[:300]
        print(f"[azos][fase1] ERRO Exception: {msg}", flush=True)
        resultado["erro"] = msg
        try:
            await page.screenshot(path=str(_TMP / "azos_debug_exception.png"), full_page=False)
        except Exception:
            pass

    return resultado


async def _ler_premio_coberturas(page) -> float | None:
    """Lê o prêmio MENSAL estimado da tela de coberturas.
    Feito em Python puro para evitar qualquer problema de parsing JS/Chromium.
    """
    import re
    try:
        txt = await page.inner_text("body")
    except Exception as e:
        print(f"[azos][adj] erro ao ler texto da pagina: {e}", flush=True)
        return None

    try:
        # Prioridade 1: "R$ 50,60/mês" na mesma linha
        m = re.search(r'R\$\s*([\d.]+),([\d]{2})\s*/\s*m[eê]s', txt, re.IGNORECASE)
        if m:
            val = float(m.group(1).replace('.', '') + '.' + m.group(2))
            if 5 < val < 2000:
                print(f"[azos][adj] premio mensal lido: R${val:.2f}", flush=True)
                return val

        # Prioridade 2: linha com preço imediatamente antes de "/mês" (texto separado por newline)
        linhas = txt.split('\n')
        for i, linha in enumerate(linhas):
            if re.search(r'm[eê]s', linha, re.IGNORECASE):
                for candidato in [linha, linhas[i - 1] if i > 0 else '']:
                    m2 = re.search(r'R\$\s*([\d.]+),([\d]{2})', candidato)
                    if m2:
                        val = float(m2.group(1).replace('.', '') + '.' + m2.group(2))
                        if 5 < val < 2000:
                            print(f"[azos][adj] premio mensal lido: R${val:.2f}", flush=True)
                            return val

        # Fallback: todos os preços entre R$5 e R$1000 (exclui anuais)
        todos = [
            float(m.group(1).replace('.', '') + '.' + m.group(2))
            for m in re.finditer(r'R\$\s*([\d.]+),([\d]{2})', txt)
        ]
        mensais = [p for p in todos if 5 < p < 1000]
        if mensais:
            val = max(mensais)
            print(f"[azos][adj] premio mensal lido (fallback): R${val:.2f}", flush=True)
            return val

        return None
    except Exception as e:
        print(f"[azos][adj] erro ao parsear premio: {e}", flush=True)
        return None


async def _continuar_habilitado(page) -> bool:
    """Retorna True se o botão Continuar/Ver cotação está clicável (não desabilitado)."""
    try:
        return await page.evaluate("""() => {
            const textos = ['ver cotação', 'continuar', 'próximo', 'avançar', 'calcular', 'ver cota'];
            const candidatos = [
                ...document.querySelectorAll('button'),
                ...document.querySelectorAll('[role="button"]'),
                ...document.querySelectorAll('a[class*="btn"], a[class*="button"]'),
            ];
            for (const btn of candidatos) {
                const t = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                if (!textos.some(k => t.includes(k))) continue;
                // Verifica disabled por atributo, aria, ou CSS
                if (btn.disabled) return false;
                if (btn.getAttribute('aria-disabled') === 'true') return false;
                const style = window.getComputedStyle(btn);
                if (style.pointerEvents === 'none') return false;
                if (parseFloat(style.opacity) < 0.4) return false;
                return true;  // encontrou e está habilitado
            }
            // Botão não encontrado — assume habilitado para não bloquear o fluxo
            return true;
        }""")
    except Exception:
        return True  # em caso de erro, não bloqueia


async def _ler_limites_slider(page, nome: str) -> dict:
    """Lê aria-valuemin/max/now do slider da cobertura a partir do DOM."""
    nome_curto = nome[:30]
    try:
        result = await page.evaluate("""(nome) => {
            const sliders = [...document.querySelectorAll('[role="slider"]')];
            for (const thumb of sliders) {
                let anc = thumb;
                for (let i = 0; i < 15; i++) {
                    anc = anc.parentElement;
                    if (!anc) break;
                    if (anc.textContent.includes(nome)) {
                        return {
                            min: parseFloat(thumb.getAttribute('aria-valuemin') || '0'),
                            max: parseFloat(thumb.getAttribute('aria-valuemax') || '9999999'),
                            now: parseFloat(thumb.getAttribute('aria-valuenow') || '0'),
                        };
                    }
                }
            }
            // Fallback: input[type="range"]
            const inputs = [...document.querySelectorAll('input[type="range"]')];
            for (const inp of inputs) {
                let anc = inp;
                for (let i = 0; i < 15; i++) {
                    anc = anc.parentElement;
                    if (!anc) break;
                    if (anc.textContent.includes(nome)) {
                        return {
                            min: parseFloat(inp.min || '0'),
                            max: parseFloat(inp.max || '9999999'),
                            now: parseFloat(inp.value || '0'),
                        };
                    }
                }
            }
            return null;
        }""", nome_curto)
        return result or {}
    except Exception:
        return {}


async def _desligar_cobertura(page, nome: str):
    """Desativa o switch de uma cobertura se estiver ON."""
    nome_curto = nome[:30]
    try:
        switch = page.locator(f'header:has(h3:has-text("{nome_curto}")) button[role="switch"]')
        if not await switch.count():
            switch = page.locator(f'*:has(h3:has-text("{nome_curto}")) button[role="switch"]')
        if await switch.count():
            if await switch.first.get_attribute("aria-checked") == "true":
                await switch.first.click(force=True)
                await page.wait_for_timeout(600)
                print(f"[azos][adj] desligou '{nome_curto}'", flush=True)
    except Exception as e:
        print(f"[azos][adj] erro desligar '{nome_curto}': {e}", flush=True)


# ──────────────────────────────────────────────────────────────────────────────
# FASE 2 — Seleciona coberturas, preenche saúde/riscos, retorna cotação final
# ──────────────────────────────────────────────────────────────────────────────
async def fase2_selecionar_coberturas(session_id: str, selecoes: list[dict],
                                       saude: dict | None = None,
                                       coberturas_limits: dict | None = None) -> dict:
    """
    selecoes = [{"nome": str, "valor": float}, ...]
    saude = {
        "pratica_esporte_radical": bool,
        "pilota_aviao": bool,
        "viaja_exterior": bool,
        "doenca_preexistente": bool,
        "internacao_2anos": bool,
        "cirurgia_prevista": bool,
        "imc_acima_40": bool,
        "diagnostico_cancer": bool,
        "diagnostico_cardio": bool,
        "diagnostico_diabetes": bool,
        "diagnostico_renal": bool,
        "diagnostico_hiv": bool,
        "uso_drogas": bool,
    }
    Retorna: {"premio_mensal": float, "premio_anual": float, "detalhes": str, "erro": None}
    """
    from playwright.async_api import TimeoutError as PWTimeout

    resultado = {"premio_mensal": None, "premio_anual": None, "detalhes": "",
                 "erro": None, "selecoes": []}

    sessao = _sessoes.get(session_id)
    if not sessao:
        resultado["erro"] = "Sessão não encontrada ou expirada"
        print(f"[azos][fase2] sessão {session_id} não encontrada", flush=True)
        return resultado

    page = sessao["page"]
    saude = saude or {}
    print(f"[azos][fase2] iniciando, session_id={session_id}, url={page.url}", flush=True)

    try:
        # ── Esconde chat popup permanentemente via CSS ────────────────────
        # O popup do copiloto cobre o botão Continuar e precisa sumir para sempre
        await page.add_style_tag(content="""
            [class*='chat'], [class*='copilot'], [class*='widget'],
            [class*='Chat'], [class*='Copilot'], [class*='Widget'],
            iframe[src*='chat'], iframe[src*='copilot'],
            div[style*='z-index: 9'], div[style*='z-index:9'] {
                display: none !important;
                pointer-events: none !important;
            }
        """)
        await page.wait_for_timeout(300)

        # ── Seleciona cada cobertura ─────────────────────────────────────
        # Primeiro: reseta TODOS os switches para OFF (força ciclo de estado React)
        print(f"[azos][fase2] resetando switches para garantir estado fresco...", flush=True)
        await page.evaluate("""() => {
            document.querySelectorAll('button[role="switch"][aria-checked="true"]').forEach(sw => {
                sw.click();
            });
        }""")
        await page.wait_for_timeout(1_000)

        print(f"[azos][fase2] selecionando coberturas: {[s['nome'] for s in selecoes]}", flush=True)
        for sel in selecoes:
            await _selecionar_cobertura(page, sel["nome"], sel.get("valor", 0))
        print(f"[azos][fase2] coberturas selecionadas", flush=True)
        await page.screenshot(path=str(_TMP / "azos_debug_f2_01_coberturas.png"), full_page=False)

        # ── Blend: sem teto de prêmio — aplica os valores recomendados pelo perfil
        # e só clampa aos limites reais do DOM (Azos pode ter min/max por idade/profissão).
        limits = coberturas_limits or {}
        print(f"[azos][fase2] clampando valores recomendados aos limites reais do DOM...", flush=True)

        novas = []
        for sel in selecoes:
            lim   = limits.get(sel["nome"], {})
            v_min = float(lim.get("valor_min") or 50_000)
            v_max = float(lim.get("valor_max") or 5_000_000)
            try:
                dom = await _ler_limites_slider(page, sel["nome"])
                if dom.get("min"): v_min = max(v_min, dom["min"])
                if dom.get("max"): v_max = min(v_max, dom["max"])
            except Exception:
                pass
            valor = int(max(v_min, min(v_max, sel.get("valor", v_min))))
            print(f"[azos][fase2]   {sel['nome'][:30]}: alvo={sel.get('valor')} -> clamp={valor} (min={v_min:.0f}, max={v_max:.0f})", flush=True)
            novas.append({**sel, "valor": valor})

        # Re-aplica cada cobertura com o valor já clampado (assegura React captou)
        for sel in novas:
            await _selecionar_cobertura(page, sel["nome"], sel["valor"])
        selecoes = novas

        # Espera React processar e estabilizar prêmio
        await page.wait_for_timeout(3_000)

        # Se Continuar continuar bloqueado, recicla switches uma vez (workaround React)
        if not await _continuar_habilitado(page):
            print(f"[azos][fase2] Continuar bloqueado - reciclando switches uma vez", flush=True)
            await page.evaluate("""() => {
                document.querySelectorAll('button[role="switch"][aria-checked="true"]')
                    .forEach(sw => sw.click());
            }""")
            await page.wait_for_timeout(800)
            for sel in selecoes:
                await _selecionar_cobertura(page, sel["nome"], sel["valor"])
            await page.wait_for_timeout(2_500)

        # ── Para AQUI: blend só precisa da cotação, não da proposta ─────
        print(f"[azos][fase2] calibração finalizada — capturando prêmio final", flush=True)
        await page.wait_for_timeout(1_200)
        await page.screenshot(path=str(_TMP / "azos_cotacao_final.png"), full_page=False)

        premio_final = await _ler_premio_coberturas(page)
        if premio_final is None:
            texto = await page.inner_text("body")
            premio_final = _extrair_premio_mensal(texto)
            resultado["premio_anual"] = _extrair_premio_anual(texto)
        resultado["premio_mensal"] = premio_final
        if not resultado.get("premio_anual") and premio_final:
            resultado["premio_anual"] = round(premio_final * 12, 2)
        resultado["selecoes"] = selecoes
        try:
            texto = await page.inner_text("body")
            resultado["detalhes"] = texto[:3000]
        except Exception:
            pass
        print(f"[azos][fase2] premio_mensal={resultado['premio_mensal']} "
              f"selecoes={[s['nome'][:25] for s in selecoes]}", flush=True)

    except PWTimeout as e:
        msg = f"Timeout: {str(e)[:200]}"
        print(f"[azos][fase2] ERRO PWTimeout: {msg}", flush=True)
        resultado["erro"] = msg
        try:
            await page.screenshot(path=str(_TMP / "azos_debug_f2_timeout.png"), full_page=False)
        except Exception:
            pass
    except Exception as e:
        msg = str(e)[:300]
        print(f"[azos][fase2] ERRO Exception: {msg}", flush=True)
        resultado["erro"] = msg
        try:
            await page.screenshot(path=str(_TMP / "azos_debug_f2_exception.png"), full_page=False)
        except Exception:
            pass
    finally:
        print(f"[azos][fase2] finalizando, resultado={resultado.get('erro') or 'ok'}", flush=True)
        # Blend para na cotação → fecha browser imediatamente para liberar RAM (Railway tem pouco).
        try:
            await sessao["browser"].close()
        except Exception:
            pass
        try:
            await sessao["pw"].stop()
        except Exception:
            pass
        _sessoes.pop(session_id, None)

    return resultado


# ──────────────────────────────────────────────────────────────────────────────
# Helpers internos
# ──────────────────────────────────────────────────────────────────────────────

async def _preencher_dados(page, c: dict):
    # Nome
    await page.locator('input[name="fullName"]').click()
    await page.locator('input[name="fullName"]').type(c["nome"], delay=50)

    # Nascimento
    nasc = c["nascimento"].replace("/", "").replace("-", "")
    await page.locator('input[name="birthDate"]').click()
    await page.locator('input[name="birthDate"]').type(nasc, delay=40)

    # Altura
    altura = str(c.get("altura", "175")).replace(".", "")
    await page.locator('input[name="height"]').click()
    await page.locator('input[name="height"]').type(altura, delay=40)

    # Peso
    peso = str(c.get("peso", "80")).replace(",", ".")
    await page.locator('input[name="weight"]').click()
    await page.locator('input[name="weight"]').type(peso, delay=40)

    # Renda
    renda = str(c.get("renda_mensal", "0")).replace(".", "").replace(",", "").replace("R$", "").strip()
    await page.locator('input[name="monthlyIncome"]').click()
    await page.locator('input[name="monthlyIncome"]').type(renda, delay=40)

    # Profissão — dialog Radix UI
    await page.locator('button[name="professionId"]').click()
    await page.wait_for_timeout(1_200)
    search = page.locator('[role="dialog"] input').first
    await search.type(c.get("profissao", "Empresário")[:6], delay=80)
    await page.wait_for_timeout(1_500)
    await page.locator('[role="dialog"]').locator('button, li, [role="option"]').first.click()
    await page.wait_for_timeout(800)

    # Sexo
    if c.get("sexo", "M") == "F":
        await page.locator('label:has-text("feminino")').click()
    else:
        await page.locator('label:has-text("masculino")').click()
    await page.wait_for_timeout(300)

    # Fumante
    labels = await page.locator('label').all()
    alvo   = "Sim" if c.get("fumante") else "Não"
    for lb in labels:
        if (await lb.inner_text()).strip() == alvo:
            await lb.click()
            break
    await page.wait_for_timeout(400)


async def _extrair_coberturas(page) -> list:
    """Extrai coberturas da página Azos."""
    coberturas = []
    try:
        await page.wait_for_timeout(2_000)
        raw = await page.evaluate("""() => {
            const result = [];
            document.querySelectorAll('header').forEach(header => {
                const sw  = header.querySelector('button[role="switch"]');
                const h3  = header.querySelector('h3');
                if (!sw || !h3) return;
                const nome = h3.innerText.trim();
                if (!nome || nome.length < 3) return;
                let container = header.parentElement;
                const desc_el = container ? container.querySelector('p, [class*="desc"]') : null;
                const desc    = desc_el ? desc_el.innerText.trim().substring(0, 150) : '';
                const inp = container ? container.querySelector(
                    'input[type="range"], input[type="number"], input[type="tel"]'
                ) : null;
                result.push({
                    nome:      nome.substring(0, 80),
                    descricao: desc,
                    ativo:     sw.getAttribute('aria-checked') === 'true',
                    valor_max: inp ? parseFloat(inp.max  || 0) : 500000,
                    valor_min: inp ? parseFloat(inp.min  || 0) : 10000,
                });
            });
            return result;
        }""")

        vistos = set()
        for item in raw:
            nome = item.get("nome", "").strip()
            if nome and nome not in vistos and nome != "Indisponível" and len(nome) > 3:
                vistos.add(nome)
                if not item["valor_max"]: item["valor_max"] = 1_000_000
                if not item["valor_min"]: item["valor_min"] = 50_000
                coberturas.append(item)

    except Exception as e:
        coberturas = [{"nome": "Erro ao extrair coberturas", "descricao": str(e),
                       "valor_max": 0, "valor_min": 0, "ativo": False}]
    return coberturas


async def _selecionar_cobertura(page, nome: str, valor: float):
    """Ativa o toggle da cobertura e define o valor via simulação real de mouse/teclado."""
    nome_curto = nome[:30]
    print(f"[azos][cob] selecionando '{nome_curto}' valor={valor}", flush=True)
    try:
        # ── 1. Localiza e clica o switch ─────────────────────────────────────
        switch = page.locator(f'header:has(h3:has-text("{nome_curto}")) button[role="switch"]')
        if not await switch.count():
            switch = page.locator(f'*:has(h3:has-text("{nome_curto}")) button[role="switch"]')
        if not await switch.count():
            print(f"[azos][cob] switch nao encontrado: '{nome_curto}'", flush=True)
            return

        await switch.first.scroll_into_view_if_needed()
        await page.wait_for_timeout(400)

        checked = await switch.first.get_attribute("aria-checked")
        print(f"[azos][cob] switch aria-checked={checked}", flush=True)

        if checked != "true":
            # Click normal (sem force) → React processa todos os eventos nativos
            await switch.first.click()
            await page.wait_for_timeout(1000)
            checked = await switch.first.get_attribute("aria-checked")
            print(f"[azos][cob] apos click, aria-checked={checked}", flush=True)

            # Fallback: mouse.click por coordenada
            if checked != "true":
                bbox = await switch.first.bounding_box()
                if bbox:
                    await page.mouse.click(
                        bbox["x"] + bbox["width"] / 2,
                        bbox["y"] + bbox["height"] / 2,
                    )
                    await page.wait_for_timeout(1000)
                    checked = await switch.first.get_attribute("aria-checked")
                    print(f"[azos][cob] apos mouse.click, aria-checked={checked}", flush=True)

        if valor <= 0:
            return

        # ── 2. Localiza o input de valor dentro do mesmo card ─────────────────
        # Usa :has() do Playwright para achar o input no container que tem o switch
        for sel in [
            f'*:has(> header:has(h3:has-text("{nome_curto}"))) input[type="tel"]',
            f'*:has(h3:has-text("{nome_curto}")) input[type="tel"]',
            f'*:has(h3:has-text("{nome_curto}")) input[type="number"]',
            f'*:has(h3:has-text("{nome_curto}")) [role="slider"]',
        ]:
            inp = page.locator(sel).first
            if await inp.count():
                break
        else:
            print(f"[azos][cob] input de valor nao encontrado: '{nome_curto}'", flush=True)
            return

        await inp.scroll_into_view_if_needed()

        # ── 3. Aguarda o input ser habilitado (até 5 s) ───────────────────────
        try:
            await inp.wait_for(state="enabled", timeout=5000)
            print(f"[azos][cob] input habilitado", flush=True)
        except Exception:
            print(f"[azos][cob] input nao habilitou em 5s — tentando por coordenada", flush=True)

        # ── 4. Clica no input ─────────────────────────────────────────────────
        try:
            await inp.click(timeout=2000)
        except Exception:
            # Playwright bloqueou (disabled/interceptado): click por coordenada
            bbox = await inp.bounding_box()
            if bbox:
                await page.mouse.click(
                    bbox["x"] + bbox["width"] / 2,
                    bbox["y"] + bbox["height"] / 2,
                )
                print(f"[azos][cob] click por coordenada ({bbox['x']:.0f},{bbox['y']:.0f})", flush=True)

        await page.wait_for_timeout(200)

        # ── 5. Digita o valor (Ctrl+A → type → Tab) ───────────────────────────
        await page.keyboard.press("Control+a")
        await page.keyboard.type(str(int(valor)), delay=25)
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(500)
        print(f"[azos][cob] valor digitado: {int(valor)}", flush=True)

    except Exception as e:
        print(f"[azos][cob] ERRO em '{nome_curto}': {e}", flush=True)


async def _preencher_dps_completo(page, saude: dict):
    """
    Preenche a DPS (Declaração Pessoal de Saúde) do Azos.
    URL: /contratacao/dps

    Estrutura real:
    - Tela 0: vínculo profissional (radio buttons — seleciona opção empresário/dono)
    - Telas seguintes: Sim/Não para doenças, estilo de vida, histórico
    - Resposta padrão = "Não." (texto EXATO)
    - Seção "estilo de vida": frequência de atividades — seleciona opção mais saudável

    Fica em loop até sair de /dps.
    """
    _saude_urls = ("dps", "saude", "health", "declaracao", "questionario")

    print(f"[azos][dps] iniciando, url={page.url}", flush=True)
    for iteracao in range(40):
        await page.wait_for_timeout(1_200)

        url_dps_atual = page.url
        if not any(k in url_dps_atual for k in _saude_urls):
            print(f"[azos][dps] saindo do loop (url sem saude keyword): {url_dps_atual}", flush=True)
            break

        await page.screenshot(path=str(_TMP / f"azos_debug_dps_{iteracao:02d}.png"), full_page=True)

        texto_pagina = (await page.inner_text("body")).lower()
        # Extrai a pergunta atual (primeira linha não vazia que não seja menu)
        linhas_pg = [l.strip() for l in texto_pagina.split("\n") if l.strip() and len(l.strip()) > 8]
        pergunta_atual = linhas_pg[0][:120] if linhas_pg else ""
        print(f"[azos][dps] iteracao={iteracao} url={url_dps_atual}", flush=True)
        print(f"[azos][dps] pergunta='{pergunta_atual}'", flush=True)

        # ── Detecta o tipo de tela ────────────────────────────────────────
        # Tipo 0: vínculo profissional OU profissão manual (mesma lógica → seleciona opção mais saudável)
        e_vinculo_prof = any(k in texto_pagina for k in [
            "vínculo profissional", "vinculo profissional",
            "selecione o seu vínculo", "selecione seu vínculo",
            "carteira assinada", "clt",
            "sou dono e trabalho", "autonomo informal", "autônomo informal",
            "sócio investidor", "socio investidor",
            "atividades manuais", "profissão envolve", "profissao envolve",
            "envolve atividades", "somente atividades administrativas",
        ])

        # Tipo A: checkboxes com opção "nenhum/nenhuma" no final
        tem_nenhum_desses = any(k in texto_pagina for k in [
            "nenhum desses", "nenhuma desses", "nenhuma delas",
            "nenhum delas", "nenhuma dessas", "nenhum dos anteriores",
            "nenhuma das anteriores", "selecione todas as opções",
            "selecione todas as opcoes",
        ])

        # Tipo C: estilo de vida (frequência)
        e_estilo_vida = any(k in texto_pagina for k in [
            "estilo de vida", "atividade física", "atividade fisica",
            "exercício", "exercicio", "sedentário", "sedentario",
            "bebida alcoólica", "bebida alcoolica", "álcool", "alcool",
            "cigarro", "tabaco", "fumo", "frequência", "frequencia",
        ])

        async def _selecionar_pelo_tipo():
            if e_vinculo_prof:
                return await _selecionar_vinculo_profissional(page)
            elif tem_nenhum_desses:
                return await _clicar_nenhum_desses(page)
            elif e_estilo_vida:
                return await _responder_estilo_vida(page)
            else:
                return await _selecionar_nao_exato(page)

        selecionou = await _selecionar_pelo_tipo()

        print(f"[azos][dps] iteracao={iteracao} selecionou={selecionou} vinculo={e_vinculo_prof} nenhum={tem_nenhum_desses} estilo={e_estilo_vida}", flush=True)
        if not selecionou:
            await page.screenshot(path=str(_TMP / f"azos_debug_dps_stuck_{iteracao:02d}.png"), full_page=True)

        # Scroll até o fim e aguarda o Radix UI processar o click (pode levar 1-2s)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1_500)

        avancou = await _clicar_continuar(page)
        print(f"[azos][dps] iteracao={iteracao} avancou={avancou}", flush=True)
        if not avancou:
            # Botão ainda desabilitado: re-seleciona (Radix pode não ter reagido ao primeiro click)
            print(f"[azos][dps] iteracao={iteracao} re-selecionando e aguardando...", flush=True)
            await _selecionar_pelo_tipo()
            await page.wait_for_timeout(2_500)
            avancou = await _clicar_continuar(page)
            if not avancou:
                # Última tentativa: clica via JS direto no botão (bypassa verificação disabled)
                try:
                    await page.evaluate("""() => {
                        const textos = ['Continuar','Próximo','Avançar','Ver cotação','Calcular','Confirmar'];
                        const btn = [...document.querySelectorAll('button')].find(b =>
                            textos.some(t => b.textContent.trim().startsWith(t)) &&
                            !b.getAttribute('disabled')
                        );
                        if (btn) btn.click();
                    }""")
                    await page.wait_for_timeout(2_000)
                    avancou = page.url != url_dps_atual
                except Exception:
                    pass
                if not avancou:
                    print(f"[azos][dps] iteracao={iteracao} nao conseguiu avançar após 3 tentativas, continuando...", flush=True)
                    await page.screenshot(path=str(_TMP / f"azos_debug_dps_fail_{iteracao:02d}.png"), full_page=True)
                    # Não quebra — continua o loop para tentar na próxima iteração

        await page.wait_for_timeout(2_000)

        if not any(k in page.url for k in _saude_urls):
            print(f"[azos][dps] saindo do loop pós-continuar: {page.url}", flush=True)
            break


async def _selecionar_vinculo_profissional(page) -> bool:
    """
    Seleciona o vínculo profissional na primeira tela da DPS.
    DOM real: label > button[role="radio"] + input[hidden] + p.body(texto)
    Clica no label que contém texto correspondente ao perfil empresário.
    """
    await _fechar_popup_chat(page)
    await page.wait_for_timeout(400)

    # Ordem de preferência para perfil empresário / resposta administrativa
    preferencias = [
        # Tela de vínculo profissional
        "mais de 15",
        "3 a 15",
        "Sou dono",
        "Autonomo informal",
        "carteira assinada",
        # Tela "Sua profissão envolve atividades manuais?"
        "Não, somente atividades administrativas",
        "somente atividades administrativas",
        "Não",
    ]

    for pref in preferencias:
        try:
            # Tenta em labels primeiro
            el = page.locator("label").filter(has_text=pref).first
            if await el.count():
                await el.click(force=True)
                await page.wait_for_timeout(500)
                print(f"[azos][vinculo] clicou label '{pref}'", flush=True)
                return True
            # Tenta em botões diretos (Azos às vezes usa button sem label wrapper)
            btn = page.locator("button").filter(has_text=pref).first
            if await btn.count():
                await btn.click(force=True)
                await page.wait_for_timeout(500)
                print(f"[azos][vinculo] clicou button '{pref}'", flush=True)
                return True
        except Exception:
            pass

    # Fallback: clica no primeiro button[role="radio"] visível
    try:
        radio = page.locator('button[role="radio"]').first
        if await radio.count():
            await radio.click(force=True)
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass

    return False


async def _fechar_popup_chat(page):
    """Fecha o popup 'Olá! Eu sou o seu copiloto' que bloqueia cliques na DPS."""
    try:
        # Botão X do popup
        for sel in [
            'button[aria-label*="fechar"]', 'button[aria-label*="Fechar"]',
            'button[aria-label*="close"]', 'button[aria-label*="Close"]',
            '[class*="chat"] button[class*="close"]',
            '[class*="chat"] button[class*="dismiss"]',
            # Tenta pelo X visível próximo ao texto "Olá"
            'button:has-text("×")', 'button:has-text("✕")', 'button:has-text("x")',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(300)
                    return
            except Exception:
                pass
        # JS: fecha qualquer elemento flutuante/overlay que contenha "copiloto" ou "Olá"
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


async def _clicar_nenhum_desses(page) -> bool:
    """
    Tela de checkboxes Radix do Azos: clica em 'Nenhum desses/Nenhuma dessas'.
    DOM real: label > button[role="checkbox"] + input[hidden] + span
    O input está oculto (pointer-events:none) — deve-se clicar no button via Playwright.
    """
    await _fechar_popup_chat(page)
    await page.wait_for_timeout(300)

    # ── 1. Itera labels e encontra a que começa com "nenhum" ─────────────
    # Playwright .click() simula pointerdown+pointerup — Radix responde.
    try:
        labels = await page.locator("label").all()
        for lbl in labels:
            try:
                txt = (await lbl.inner_text()).strip().lower()
            except Exception:
                continue
            if not (txt.startswith("nenhum") or txt.startswith("nenhuma")):
                continue
            # Faz scroll até o elemento ficar visível
            try:
                await lbl.scroll_into_view_if_needed()
                await page.wait_for_timeout(300)
            except Exception:
                pass
            # Tenta clicar no button[role="checkbox"] dentro do label
            btn_cb = lbl.locator('button[role="checkbox"]').first
            if await btn_cb.count():
                await btn_cb.click(force=True)
                await page.wait_for_timeout(400)
                return True
            # Fallback: clica no label inteiro
            await lbl.click(force=True)
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass

    # ── 2. Playwright get_by_role checkbox — último da página ─────────────
    try:
        cb = page.get_by_role("checkbox").last
        if await cb.count():
            await cb.scroll_into_view_if_needed()
            await cb.click(force=True)
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass

    # ── 3. Mouse click via coordenadas JS (fallback) ──────────────────────
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
                    return { x: r.left + r.width/2, y: r.top + r.height/2 };
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


async def _selecionar_nao_exato(page) -> bool:
    """
    Seleciona 'Não.' em telas de radio Radix do Azos.
    DOM real:
      - Pattern A (Radix): div[role="radiogroup"] > label > button[role="radio"] + input[hidden] + p
      - Pattern B (plain): div[role="radiogroup"] > button (com texto)
    Usa page.mouse.click() com coordenadas JS para garantir eventos pointer corretos no Radix.
    """
    await _fechar_popup_chat(page)
    await page.wait_for_timeout(300)

    NAO_EXATOS = {"não.", "não", "nao.", "nao"}

    # ── 1. Coordenadas JS: mais confiável para Radix UI ──────────────────
    # Aceita texto exato "Não." OU texto que COMEÇA com "Não," (ex: "Não, somente atividades...")
    coords = await page.evaluate("""() => {
        function isNao(txt) {
            const t = txt.trim().toLowerCase();
            return t === 'não.' || t === 'não' || t === 'nao.' || t === 'nao' ||
                   (t.startsWith('não') && t.length < 100) ||
                   (t.startsWith('nao') && t.length < 100);
        }

        // Prioridade: botões e labels clicáveis
        for (const el of document.querySelectorAll('button, label, [role="radio"], [role="option"]')) {
            const txt = (el.innerText || el.textContent || '').trim();
            if (!isNao(txt)) continue;
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0)
                return { x: r.left + r.width/2, y: r.top + r.height/2 };
        }

        // Fallback: qualquer elemento com texto "Não..."
        for (const el of document.querySelectorAll('label, p, span, div, li')) {
            const txt = (el.innerText || '').trim();
            if (!isNao(txt)) continue;
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0)
                return { x: r.left + r.width/2, y: r.top + r.height/2 };
        }

        // Procura em nós de texto
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            if (isNao(node.textContent)) {
                const pai = node.parentElement;
                if (!pai) continue;
                const r = pai.getBoundingClientRect();
                if (r.width > 0 && r.height > 0)
                    return { x: r.left + r.width/2, y: r.top + r.height/2 };
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

    # ── 2. Pattern A (Radix): label com button[role="radio"] ─────────────
    try:
        labels = await page.locator("label").all()
        for lbl in reversed(labels):
            try:
                txt = (await lbl.inner_text()).strip().lower()
            except Exception:
                continue
            # Aceita exato "Não." OU começa com "não" (ex: "Não, somente atividades...")
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

    # ── 3. get_by_role radio com nome "Não." ─────────────────────────────
    for nome in ["Não.", "Não"]:
        try:
            radio = page.get_by_role("radio", name=nome, exact=True)
            if await radio.count():
                await radio.last.click(force=True)
                await page.wait_for_timeout(400)
                return True
        except Exception:
            pass

    # ── 4. Pattern B: último button[role="radio"] do radiogroup ──────────
    try:
        btns_grupo = await page.locator('div[role="radiogroup"] button[role="radio"]').all()
        if btns_grupo:
            ultimo = btns_grupo[-1]
            await ultimo.scroll_into_view_if_needed()
            await ultimo.click(force=True)
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass

    # ── 5. Último radio genérico da página ───────────────────────────────
    try:
        radios = await page.locator(
            'input[type="radio"], [role="radio"]'
        ).all()
        if radios:
            await radios[-1].click(force=True)
            await page.wait_for_timeout(400)
            return True
    except Exception:
        pass

    return False


async def _responder_estilo_vida(page) -> bool:
    """
    Responde perguntas de estilo de vida.
    Tenta 'Não' exato primeiro; se não houver, seleciona opção mais saudável.
    """
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

    # Último recurso: primeira opção (permite avançar)
    try:
        radios = await page.locator('input[type="radio"]').all()
        if radios:
            await radios[0].click()
            await page.wait_for_timeout(300)
            return True
    except Exception:
        pass

    return False


async def _fechar_modal_cpf_vinculado(page) -> bool:
    """
    Fecha o modal 'Esse CPF já está vinculado ao e-mail' clicando em 'Continuar'.
    Estratégia: busca o botão DENTRO do dialog (não na página inteira),
    tenta mouse.click por coordenadas, teclado, e remoção DOM como fallback.
    """
    try:
        corpo = await page.inner_text("body")
        if ("vinculado ao e-mail" not in corpo.lower()
                and "esse cpf" not in corpo.lower()):
            return False
    except Exception:
        return False

    # Salva HTML para debug
    try:
        html = await page.content()
        (_TMP / "azos_modal_cpf.html").write_text(html, encoding="utf-8")
    except Exception:
        pass

    def _modal_ainda_aberto(texto: str) -> bool:
        t = texto.lower()
        return "vinculado ao e-mail" in t or "esse cpf" in t

    # ── 1. Encontra Continuar DENTRO do dialog → page.mouse.click ────────
    try:
        coords = await page.evaluate("""() => {
            const DIALOG_SELS = [
                '[role="dialog"]', '[role="alertdialog"]',
                '[data-radix-dialog-content]', '[data-state="open"]'
            ];
            for (const sel of DIALOG_SELS) {
                for (const dlg of document.querySelectorAll(sel)) {
                    const r0 = dlg.getBoundingClientRect();
                    if (r0.width < 10 || r0.height < 10) continue;
                    // Busca botão Continuar dentro deste dialog
                    const btns = Array.from(dlg.querySelectorAll('button, a[role="button"]'));
                    const btn = btns.find(b =>
                        (b.innerText || b.textContent || '').trim()
                            .toLowerCase().includes('continuar')
                    ) || btns[btns.length - 1];
                    if (btn) {
                        const r = btn.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0)
                            return {x: r.left + r.width/2, y: r.top + r.height/2,
                                    src: sel, text: (btn.innerText||'').trim()};
                    }
                }
            }
            return null;
        }""")
        if coords:
            await page.mouse.click(coords["x"], coords["y"])
            await page.wait_for_timeout(1_200)
            if not _modal_ainda_aberto(await page.inner_text("body")):
                return True
    except Exception:
        pass

    # ── 2. ÚLTIMO botão Continuar na página → page.mouse.click ───────────
    try:
        coords2 = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button')).reverse();
            const btn = btns.find(b =>
                (b.innerText || b.textContent || '').trim()
                    .toLowerCase().includes('continuar')
            );
            if (btn) {
                const r = btn.getBoundingClientRect();
                if (r.width > 0 && r.height > 0)
                    return {x: r.left + r.width/2, y: r.top + r.height/2,
                            text: btn.innerText.trim()};
            }
            return null;
        }""")
        if coords2:
            await page.mouse.click(coords2["x"], coords2["y"])
            await page.wait_for_timeout(1_200)
            if not _modal_ainda_aberto(await page.inner_text("body")):
                return True
    except Exception:
        pass

    # ── 3. React fiber onClick (chama handler diretamente) ────────────────
    try:
        ok = await page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button')).reverse();
            for (const btn of btns) {
                if (!(btn.innerText||'').toLowerCase().includes('continuar')) continue;
                const r = btn.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                // React props
                for (const key of Object.keys(btn)) {
                    if (key.startsWith('__reactProps')) {
                        const p = btn[key];
                        if (p && p.onClick) {
                            p.onClick({type:'click',preventDefault:()=>{},stopPropagation:()=>{}});
                            return 'reactProps';
                        }
                    }
                    if (key.startsWith('__reactFiber')) {
                        let f = btn[key];
                        while (f) {
                            if (f.memoizedProps && f.memoizedProps.onClick) {
                                f.memoizedProps.onClick({type:'click',preventDefault:()=>{},stopPropagation:()=>{}});
                                return 'fiberMemo';
                            }
                            f = f.return;
                        }
                    }
                }
                btn.click();
                return 'jsClick';
            }
            return false;
        }""")
        if ok:
            await page.wait_for_timeout(1_200)
            if not _modal_ainda_aberto(await page.inner_text("body")):
                return True
    except Exception:
        pass

    # ── 4. Teclado: Tab + Enter ───────────────────────────────────────────
    try:
        # Pressiona Tab 1-3 vezes tentando focar no botão Continuar, depois Enter
        for _ in range(3):
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(150)
            focused = await page.evaluate(
                "() => (document.activeElement?.innerText||'').toLowerCase()"
            )
            if "continuar" in focused:
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(1_200)
                if not _modal_ainda_aberto(await page.inner_text("body")):
                    return True
                break
    except Exception:
        pass

    # ── 5. Remove backdrop → Playwright force click ───────────────────────
    try:
        await page.evaluate("""() => {
            // Desabilita apenas o backdrop/overlay para liberar o clique
            document.querySelectorAll(
                '[data-radix-dialog-overlay], [data-overlay], [class*="backdrop"], [class*="overlay"]'
            ).forEach(el => {
                el.style.pointerEvents = 'none';
                el.style.display = 'none';
            });
        }""")
        await page.wait_for_timeout(200)
        btn = page.locator('[role="dialog"] button, [role="alertdialog"] button').last
        if not await btn.count():
            btn = page.locator("button").filter(has_text="Continuar").last
        if await btn.count():
            await btn.click(force=True)
            await page.wait_for_timeout(1_000)
            if not _modal_ainda_aberto(await page.inner_text("body")):
                return True
    except Exception:
        pass

    # ── 6. Remove modal do DOM (nuclear) ─────────────────────────────────
    try:
        await page.evaluate("""() => {
            const SELS = ['[role="dialog"]', '[role="alertdialog"]',
                          '[data-radix-dialog-content]', '[data-state="open"]',
                          '[class*="modal"]', '[class*="dialog"]'];
            for (const sel of SELS) {
                document.querySelectorAll(sel).forEach(el => {
                    if ((el.innerText||'').toLowerCase().includes('vinculado')
                        || (el.innerText||'').toLowerCase().includes('esse cpf')) {
                        el.remove();
                    }
                });
            }
            // Remove overlays fixos que possam estar bloqueando o form
            document.querySelectorAll('*').forEach(el => {
                const s = window.getComputedStyle(el);
                if ((s.position === 'fixed' || s.position === 'absolute')
                    && parseFloat(s.zIndex || '0') > 100
                    && !el.querySelector('input, select, textarea')) {
                    el.style.display = 'none';
                }
            });
        }""")
        await page.wait_for_timeout(600)
        return True
    except Exception:
        pass

    return False


async def _preencher_cadastro(page, cliente: dict):
    """
    Preenche a tela /contratacao/cadastro.
    Campos reais: cpf (name="cpf" type="tel"), phone (name="phone"),
    email (name="email"), estado civil (div[name="marital_status"] button[role="radio"]),
    PEP (label > button[role="radio"] + span.body2).
    """
    await page.wait_for_timeout(500)

    # CPF — input[name="cpf"] type="tel"
    cpf = str(cliente.get("cpf", "")).replace(".", "").replace("-", "").strip()
    if cpf:
        try:
            inp = page.locator('input[name="cpf"]').first
            if await inp.count():
                await inp.click()
                await inp.fill("")
                await inp.type(cpf, delay=40)
                # Tab dispara o blur do campo → aguarda o modal aparecer (pode levar até 4s)
                await inp.press("Tab")
                for _ in range(8):
                    await page.wait_for_timeout(500)
                    corpo_check = (await page.inner_text("body")).lower()
                    if "vinculado ao e-mail" in corpo_check or "esse cpf" in corpo_check:
                        break  # modal detectado, para de esperar
                # Modal "Esse CPF já está vinculado ao e-mail" → clica Continuar
                await _fechar_modal_cpf_vinculado(page)
                await page.wait_for_timeout(500)
        except Exception:
            pass

    # Telefone — input[name="phone"]
    # Remove formatação: parênteses, espaços, traços, +
    telefone = str(cliente.get("telefone", "")).replace("(", "").replace(")", "").replace(" ", "").replace("-", "").replace("+", "")
    # Remove prefixo de país "55" se presente (ex: 5561999991234 → 61999991234)
    # Aceita de 11 até 13 dígitos com "55" no início
    if telefone.startswith("55") and len(telefone) in (11, 12, 13):
        candidate = telefone[2:]
        if len(candidate) in (9, 10, 11):
            telefone = candidate
    # Números de 10 dígitos são fixos (pré-2012); adiciona "9" após o DDD
    # para converter para padrão móvel (11 dígitos) que o Azos exige
    if len(telefone) == 10 and telefone[2] != "9":
        telefone = telefone[:2] + "9" + telefone[2:]
    if telefone:
        try:
            inp = page.locator('input[name="phone"]').first
            if await inp.count() and await inp.is_visible():
                # Usa fill() direto — não depende de foco, funciona com modal sobreposto
                await inp.fill(telefone)
                await page.wait_for_timeout(400)
        except Exception:
            pass

    # Email — input[name="email"] type="text"
    # IMPORTANTE: usa fill() direto para evitar que type() vá para elemento errado
    # (quando o modal CPF está sobreposto, click() falha e type() digita no foco errado)
    email = str(cliente.get("email", "")).strip()
    if email:
        try:
            inp = page.locator('input[name="email"]').first
            if await inp.count() and await inp.is_visible():
                await inp.fill(email)
                await page.wait_for_timeout(400)
        except Exception:
            pass

    # Estado civil — div[name="marital_status"] button[role="radio"]
    # Textos reais: "Solteira/o", "Casada/o", "Viúva/o", "Divorciada/o"
    estado = str(cliente.get("estado_civil", "Solteiro"))
    try:
        btn = page.locator('div[name="marital_status"] button[role="radio"]').filter(has_text=estado).first
        if not await btn.count():
            btn = page.locator('div[name="marital_status"] button[role="radio"]').first
        if await btn.count():
            await btn.click(force=True)
            await page.wait_for_timeout(400)
    except Exception:
        pass

    # PEP (is_politically_exposed_person) → Não
    try:
        label_nao = page.locator('label').filter(has_text="Não").last
        if await label_nao.count():
            await label_nao.click(force=True)
            await page.wait_for_timeout(400)
    except Exception:
        pass

    await page.wait_for_timeout(500)

    # Verifica novamente o modal (pode ter aparecido durante o preenchimento)
    await _fechar_modal_cpf_vinculado(page)

    # Tela 2: Endereço (mesmo URL /contratacao/cadastro)
    cep_inp = page.locator('input[name="cep"]').first
    if await cep_inp.count() and await cep_inp.is_visible():
        await _preencher_endereco(page, cliente)


async def _preencher_endereco(page, cliente: dict):
    """Preenche endereço residencial. CEP auto-preenche cidade/estado/rua/bairro."""
    cep = str(cliente.get("cep", "")).replace("-", "").replace(".", "").replace(" ", "").strip()
    # CEP brasileiro deve ter 8 dígitos — completa com zero à direita se faltar 1
    if cep and len(cep) == 7:
        cep = cep + "0"
    # Só prossegue se tiver exatamente 8 dígitos numéricos
    if cep and len(cep) == 8 and cep.isdigit():
        for sel in ['input[name="cep"]', 'input[name="zipcode"]', 'input[name="zip_code"]']:
            try:
                inp = page.locator(sel).first
                if await inp.count() and await inp.is_visible():
                    await inp.click()
                    await inp.fill("")
                    await inp.type(cep, delay=80)
                    # Aguarda auto-fill do CEP (chamada de API pode demorar)
                    # Polling: para assim que Estado ou Cidade ficar preenchido
                    for _ in range(12):  # até 6s
                        await page.wait_for_timeout(500)
                        preencheu = await page.evaluate("""() => {
                            const sels = [
                                'input[name="state"]', 'input[name="city"]',
                                'input[name="street"]', 'input[name="neighborhood"]',
                                'select[name="state"]', 'select[name="city"]',
                                '[name="state"]', '[name="city"]',
                            ];
                            for (const s of sels) {
                                const el = document.querySelector(s);
                                if (el && (el.value || el.innerText || '').trim().length > 0)
                                    return true;
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


async def _preencher_riscos_vida(page, saude: dict):
    """Delega para _preencher_dps_completo (mesmo mecanismo)."""
    await _preencher_dps_completo(page, saude)


async def _preencher_condicoes_saude(page, saude: dict):
    """Delega para _preencher_dps_completo (mesmo mecanismo)."""
    await _preencher_dps_completo(page, saude)


async def _preencher_checkout(page) -> None:
    """
    Na tela de checkout/pagamento:
    1. Tenta encontrar e clicar em 'Enviar link' / 'Compartilhar proposta' (opção direta).
    2. Se não encontrar, seleciona PIX como forma de pagamento e clica Continuar.
    """
    await page.wait_for_timeout(500)

    # Tenta botão de "enviar link" / "compartilhar" direto na página
    for sel in [
        'button:has-text("Enviar link")',
        'button:has-text("Compartilhar")',
        'button:has-text("Enviar proposta")',
        'a:has-text("Enviar link")',
        'a:has-text("Compartilhar")',
        '[aria-label*="compartilhar" i]',
        '[aria-label*="enviar link" i]',
    ]:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                await el.click()
                await page.wait_for_timeout(1_500)
                return
        except Exception:
            pass

    # Salva HTML para debug
    try:
        html = await page.content()
        from pathlib import Path
        (_TMP / "azos_checkout.html").write_text(html, encoding="utf-8")
    except Exception:
        pass

    # Seleciona PIX (sem necessidade de dados de cartão)
    for sel in [
        'button:has-text("PIX")',
        'label:has-text("PIX")',
        'div[role="radiogroup"] button:has-text("PIX")',
        '[data-value="pix"]',
        'input[value="pix"]',
    ]:
        try:
            el = page.locator(sel).first
            if await el.count() and await el.is_visible():
                await el.click()
                await page.wait_for_timeout(800)
                break
        except Exception:
            pass

    # Clica em Continuar
    await page.wait_for_timeout(500)
    await _clicar_continuar(page)


async def _extrair_link_proposta(page) -> str | None:
    """
    Captura o link de assinatura ClickSign da proposta.
    Estratégia:
      1. Tenta na tela atual (proposta-enviada)
      2. Navega para /vendas, abre a proposta mais recente e pega o link de assinatura
    Salva HTML para debug.
    """
    from pathlib import Path

    try:
        html = await page.content()
        (_TMP / "azos_proposta_enviada.html").write_text(html, encoding="utf-8")
    except Exception:
        pass
    await page.screenshot(path=str(_TMP / "azos_proposta_enviada.png"), full_page=True)

    def _e_link_assinatura(url: str) -> bool:
        """Retorna True se parece ser um link de assinatura ClickSign ou Azos."""
        u = url.lower()
        return any(k in u for k in [
            "clicksign", "assinar", "assinatura", "sign", "signature",
            "app.clicksign", "/sign/", "d4sign"
        ])

    # ── 1. Verifica ClickSign ou link de assinatura na página atual ──────────
    try:
        links_pag = await page.evaluate("""() => {
            // inputs readonly (botão "Copiar link")
            const inputs = Array.from(document.querySelectorAll('input[readonly]'))
                .map(i => i.value).filter(v => v.startsWith('http'));
            // links <a>
            const hrefs = Array.from(document.querySelectorAll('a[href]'))
                .map(a => a.href).filter(h => h.startsWith('http'));
            // texto visível com URL
            const textos = Array.from(document.querySelectorAll('p,span,div,strong'))
                .map(el => (el.innerText||'').trim())
                .filter(t => t.startsWith('https://') && t.length < 500);
            return [...new Set([...inputs, ...hrefs, ...textos])];
        }""")
        # Prioridade: link ClickSign
        for lnk in links_pag:
            if _e_link_assinatura(lnk):
                return lnk
    except Exception:
        pass

    # ── 2. Botão "Copiar link" → clipboard ───────────────────────────────────
    for sel in ['button:has-text("Copiar link")', 'button:has-text("Copiar")',
                '[aria-label*="copiar" i]', 'button:has-text("Compartilhar")']:
        try:
            btn = page.locator(sel).first
            if await btn.count() and await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(800)
                link = await page.evaluate("navigator.clipboard.readText().catch(()=>'')")
                if link and link.startswith("http"):
                    return link.strip()
        except Exception:
            pass

    # ── 3. Navega para /vendas e pega o link de assinatura da proposta mais recente
    try:
        url_volta = page.url
        await page.goto("https://corretores.azos.com.br/corretor/vendas",
                        wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2_500)
        await page.screenshot(path=str(_TMP / "azos_vendas.png"), full_page=True)

        # Clica na proposta mais recente (primeira linha da lista)
        for sel_row in [
            'table tbody tr:first-child',
            '[data-testid="proposal-row"]:first-child',
            'a[href*="/vendas/"]:first-of-type',
            'tr:first-child td a',
            '[class*="row"]:first-child',
            'li:first-child a',
        ]:
            try:
                el = page.locator(sel_row).first
                if await el.count() and await el.is_visible():
                    await el.click()
                    await page.wait_for_timeout(2_500)
                    break
            except Exception:
                pass

        await page.screenshot(path=str(_TMP / "azos_venda_detalhe.png"), full_page=True)
        html_v = await page.content()
        (_TMP / "azos_venda_detalhe.html").write_text(html_v, encoding="utf-8")

        # Procura link ClickSign na página de detalhe
        links_v = await page.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll('input[readonly]'))
                .map(i => i.value).filter(v => v.startsWith('http'));
            const hrefs = Array.from(document.querySelectorAll('a[href]'))
                .map(a => a.href).filter(h => h.startsWith('http'));
            const textos = Array.from(document.querySelectorAll('p,span,div,strong,input'))
                .map(el => (el.innerText || el.value || '').trim())
                .filter(t => t.startsWith('https://') && t.length < 500);
            return [...new Set([...inputs, ...hrefs, ...textos])];
        }""")

        for lnk in links_v:
            if _e_link_assinatura(lnk):
                return lnk

        # Botão "Copiar link de assinatura" na venda
        for sel in ['button:has-text("Copiar link")', 'button:has-text("Link de assinatura")',
                    'button:has-text("Copiar")', '[aria-label*="assinatura" i]',
                    'button:has-text("Enviar link")']:
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(800)
                    link = await page.evaluate("navigator.clipboard.readText().catch(()=>'')")
                    if link and link.startswith("http"):
                        return link.strip()
            except Exception:
                pass

    except Exception:
        pass

    # ── 4. Fallback: retorna o que houver (inclusive PDF) ────────────────────
    try:
        links_fb = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a[href]'))
                .map(a => a.href)
                .filter(h => h.startsWith('http') &&
                             !h.includes('corretores.azos') &&
                             !h.includes('googletagmanager') &&
                             !h.includes('icomoon') &&
                             !h.includes('cloudflare'));
        }""")
        if links_fb:
            return links_fb[0]
    except Exception:
        pass

    return None

    return None


async def _clicar_continuar(page):
    """Clica no botão de avançar (vários nomes possíveis). Ignora botões desabilitados."""
    seletores = [
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
            btn = page.locator(sel).first
            if not await btn.count():
                continue
            if not await btn.is_visible():
                continue
            disabled = await btn.get_attribute("disabled")
            aria_disabled = await btn.get_attribute("aria-disabled")
            if disabled is not None or aria_disabled == "true":
                continue
            await btn.click()
            return True
        except Exception:
            pass

    # Fallback: mouse.click por coordenadas JS (ignora oclusão do popup)
    try:
        coords = await page.evaluate("""() => {
            const textos = ['Ver cotação', 'Continuar', 'Próximo', 'Avançar', 'Calcular', 'Finalizar', 'Confirmar'];
            for (const txt of textos) {
                const btn = [...document.querySelectorAll('button')].find(b =>
                    b.textContent.trim().includes(txt) && !b.disabled && b.getAttribute('aria-disabled') !== 'true'
                );
                if (btn) {
                    const r = btn.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0)
                        return {x: r.left + r.width/2, y: r.top + r.height/2, txt: btn.textContent.trim()};
                }
            }
            return null;
        }""")
        if coords:
            print(f"[azos] _clicar_continuar fallback mouse.click em '{coords.get('txt','')}' ({coords['x']:.0f},{coords['y']:.0f})", flush=True)
            await page.mouse.click(coords["x"], coords["y"])
            return True
    except Exception:
        pass

    return False


async def _tentar_continuar(page) -> bool:
    """Tenta avançar e retorna True se conseguiu."""
    return await _clicar_continuar(page)


async def _titulo_pagina(page) -> str:
    """Extrai título/heading da página atual para detectar o step."""
    try:
        h1 = page.locator('h1, h2, [class*="title"], [class*="heading"]').first
        if await h1.count():
            return await h1.inner_text()
    except Exception:
        pass
    return page.url


def _extrair_premio_mensal(texto: str) -> float | None:
    """
    Extrai o prêmio mensal da página de cotação final.
    Evita pegar o capital segurado (valores muito altos).
    """
    import re
    # Padrões específicos para prêmio (valores baixos, tipicamente R$50–R$500/mês)
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
                # Prêmio mensal tipicamente entre R$20 e R$2000
                if 20 <= val <= 2000:
                    return val
            except Exception:
                pass

    # Fallback: pega o menor valor R$ encontrado (provavelmente é o prêmio, não o capital)
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


def _extrair_premio_anual(texto: str) -> float | None:
    """Extrai o prêmio anual da página de cotação."""
    import re
    patterns = [
        r'(?:anual|anuidade)[^\n]{0,60}?R\$\s*([\d\.]+,\d{2})',
        r'R\$\s*([\d\.]+,\d{2})\s*/?\s*(?:ano|anual)',
    ]
    for pat in patterns:
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(".", "").replace(",", "."))
                if 200 <= val <= 20000:
                    return val
            except Exception:
                pass
    return None
