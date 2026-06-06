"""
Automação Azos — fluxo completo com browser visível
Fase 1: preenche dados pessoais → retorna coberturas disponíveis
Fase 2: seleciona coberturas → preenche saúde/riscos → retorna cotação final
"""
import asyncio, os, re, uuid, tempfile
from pathlib import Path

AZOS_URL_LOGIN = "https://corretores.azos.com.br/login"
AZOS_URL_SIM   = "https://contratacao.azos.com.br/simulacao/dados-pessoais"
# Hardcode: env grs4027 ficou USER_DISABLED no Firebase Azos.
AZOS_EMAIL = "grsouza93ip@gmail.com"
AZOS_SENHA = os.getenv("AZOS_SENHA", "1964Dns#*")

# Pasta temporária cross-platform (/tmp no Linux/Mac, %TEMP% no Windows)
_TMP = Path(tempfile.gettempdir())

# Modo headless: True em produção (servidor), False para debug local
_HEADLESS = os.getenv("HEADLESS", "true").lower() not in ("false", "0", "no")

# Sessões ativas: session_id → {playwright, browser, page}
_sessoes: dict = {}

# Pool persistente de browser — main.py inicializa no startup p/ evitar
# lançar um novo Chromium a cada cotação (economiza ~10s e RAM).
# Quando _BROWSER_POOL existe, fase1 cria apenas new_context() (isolado por sessão).
_PW_POOL = None       # async_playwright instance
_BROWSER_POOL = None  # Chromium browser persistente
_POOL_LOCK = asyncio.Lock()  # protege re-inicialização concorrente do pool
_POOL_JOB_COUNT = 0   # contador de cotações servidas pelo browser atual
_POOL_MAX_JOBS = int(os.getenv("POOL_RECYCLE_AFTER", "400"))  # recicla browser após N jobs


async def init_browser_pool():
    """Inicializa pool persistente de browser (chamar no startup)."""
    global _PW_POOL, _BROWSER_POOL, _POOL_JOB_COUNT
    if _BROWSER_POOL is not None and _BROWSER_POOL.is_connected():
        return
    from playwright.async_api import async_playwright
    _PW_POOL = await async_playwright().start()
    _launch_args = [
        "--window-size=1280,900",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-blink-features=AutomationControlled",
    ]
    _BROWSER_POOL = await _PW_POOL.chromium.launch(headless=_HEADLESS, args=_launch_args)
    _POOL_JOB_COUNT = 0
    print(f"[azos][pool] browser pool inicializado (headless={_HEADLESS}, "
          f"recycle_after={_POOL_MAX_JOBS} jobs)", flush=True)


async def get_pool_browser():
    """Retorna browser do pool. Reinicializa automaticamente se:
       1) browser morto/desconectado, ou
       2) atingiu o limite de jobs (POOL_RECYCLE_AFTER) — previne memory leak Chromium.
    Lock garante uma só reinicialização concorrente."""
    global _PW_POOL, _BROWSER_POOL, _POOL_JOB_COUNT
    needs_recycle = (_BROWSER_POOL is None
                     or not _BROWSER_POOL.is_connected()
                     or _POOL_JOB_COUNT >= _POOL_MAX_JOBS)
    if not needs_recycle:
        _POOL_JOB_COUNT += 1
        return _BROWSER_POOL
    async with _POOL_LOCK:
        # Re-check inside lock
        needs_recycle_inner = (_BROWSER_POOL is None
                                or not _BROWSER_POOL.is_connected()
                                or _POOL_JOB_COUNT >= _POOL_MAX_JOBS)
        if not needs_recycle_inner:
            _POOL_JOB_COUNT += 1
            return _BROWSER_POOL
        reason = ("desconectado" if (_BROWSER_POOL is None or not _BROWSER_POOL.is_connected())
                  else f"recycle (atingiu {_POOL_JOB_COUNT} jobs)")
        print(f"[azos][pool] reinicializando browser — {reason}", flush=True)
        # Limpa estado antigo
        if _BROWSER_POOL is not None:
            try: await _BROWSER_POOL.close()
            except Exception: pass
        if _PW_POOL is not None:
            try: await _PW_POOL.stop()
            except Exception: pass
        _BROWSER_POOL = None
        _PW_POOL = None
        await init_browser_pool()
        _POOL_JOB_COUNT = 1
        return _BROWSER_POOL


# ── Session reuse — login 1× por replica, cookies/state reusados em N cotações
# Sob alta concorrência (1000+ cotações), fazer login fresh por cotação satura
# o formulário de login Azos. Solução: cada replica loga UMA VEZ no startup,
# captura storage_state (cookies + localStorage), e cada cotação cria new_context
# carregando esse state — entra já autenticado, vai direto pra simulação.
_AUTH_STATE = None      # dict com cookies + localStorage pós-login
_AUTH_LOCK = asyncio.Lock()


async def init_auth_state(force: bool = False):
    """Faz login Azos UMA vez por CLUSTER (todas as replicas compartilham) e captura
    storage_state pra reuso. Estratégia:
      1. Lê state do Postgres (outra replica pode ter logado)
      2. Se vazio/expirado/force: pega advisory lock global Postgres
      3. Dentro do lock: re-checa DB, faz login se necessário, salva no DB
      4. Releases lock — outras replicas que esperavam leem o state do DB
    Resultado: 1 login por cluster, não 1 por replica."""
    global _AUTH_STATE
    if _AUTH_STATE is not None and not force:
        return _AUTH_STATE
    async with _AUTH_LOCK:  # lock local (por replica)
        if _AUTH_STATE is not None and not force:
            return _AUTH_STATE
        # Tenta ler do Postgres primeiro (outra replica já pode ter logado)
        try:
            from app import db as job_db
            if not force:
                state_db = await job_db.get_auth_state()
                if state_db:
                    _AUTH_STATE = state_db
                    n_cookies = len(state_db.get("cookies", []))
                    print(f"[azos][auth] storage_state lido do Postgres "
                          f"({n_cookies} cookies) — login pulado", flush=True)
                    return _AUTH_STATE
            # Não tem state no DB OU force=True. Pega advisory lock (1 login por cluster)
            print(f"[azos][auth] tentando pegar lock distribuído pra login...", flush=True)
            got_lock = await job_db.try_acquire_auth_lock(timeout_sec=120)
            if not got_lock:
                print(f"[azos][auth] não pegou lock após 120s — tentando ler DB de novo", flush=True)
                state_db = await job_db.get_auth_state()
                if state_db:
                    _AUTH_STATE = state_db
                    return _AUTH_STATE
                raise RuntimeError("lock não pegou e DB sem state")
            # Re-checa DB inside lock (outra replica pode ter logado enquanto esperávamos)
            if not force:
                state_db = await job_db.get_auth_state()
                if state_db:
                    _AUTH_STATE = state_db
                    print(f"[azos][auth] outra replica já logou — lendo do DB", flush=True)
                    await job_db.release_auth_lock()
                    return _AUTH_STATE
        except Exception as _de:
            print(f"[azos][auth] DB lookup falhou ({_de}) — login fresh sem lock", flush=True)

        from playwright.async_api import TimeoutError as PWTimeout
        browser = await get_pool_browser()
        print(f"[azos][auth] EU SOU O ELEITO — fazendo login fresh pra capturar storage_state...", flush=True)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        try:
            await ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR', 'pt', 'en-US']});
                window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
            """)
            page = await ctx.new_page()
            # Jitter startup pra não bater 42 replicas no exato mesmo ms
            import random
            await page.wait_for_timeout(int(random.uniform(0, 8000)))
            await page.goto(AZOS_URL_LOGIN, wait_until="networkidle", timeout=60_000)
            # Aguarda React hidratar antes de tentar interagir — sem isso o
            # form vira GET default (password vai na URL!)
            await page.wait_for_selector('[data-testid="login-button__submit"]', timeout=30_000)
            await page.wait_for_timeout(800)
            await page.fill('[data-testid="login-input__email"]', AZOS_EMAIL)
            await page.fill('[data-testid="login-input__password"]', AZOS_SENHA)
            # Enter no password dispara o React submit handler (mais confiável que click)
            await page.press('[data-testid="login-input__password"]', "Enter")
            # Retry até 5x pra login completar (sob carga inicial pode ser lento)
            for tent in range(5):
                try:
                    await page.wait_for_url("**/corretor/**", timeout=60_000)
                    break
                except PWTimeout:
                    print(f"[azos][auth] retry login {tent+1}/5 — url={page.url}", flush=True)
                    if "/login" in page.url:
                        try:
                            await page.wait_for_timeout(1500)
                            await page.fill('[data-testid="login-input__email"]', AZOS_EMAIL)
                            await page.fill('[data-testid="login-input__password"]', AZOS_SENHA)
                            await page.press('[data-testid="login-input__password"]', "Enter")
                        except Exception:
                            pass
            if "/corretor/" not in page.url:
                raise RuntimeError(f"init_auth_state falhou — url final={page.url}")
            # Captura cookies + localStorage
            state = await ctx.storage_state()
            _AUTH_STATE = state
            n_cookies = len(state.get("cookies", []))
            n_origins = len(state.get("origins", []))
            print(f"[azos][auth] storage_state capturado: {n_cookies} cookies, "
                  f"{n_origins} origins (compartilhando via Postgres com outras replicas)", flush=True)
            # Salva no Postgres pra outras replicas reusarem
            try:
                from app import db as job_db
                await job_db.save_auth_state(state)
                print(f"[azos][auth] state salvo no Postgres — outras replicas podem ler", flush=True)
            except Exception as _se:
                print(f"[azos][auth] save_auth_state DB falhou ({_se}) — só state local", flush=True)
            return _AUTH_STATE
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
            try:
                from app import db as job_db
                await job_db.release_auth_lock()
            except Exception:
                pass


async def shutdown_browser_pool():
    """Fecha pool persistente (chamar no shutdown)."""
    global _PW_POOL, _BROWSER_POOL
    if _BROWSER_POOL is not None:
        try:
            await _BROWSER_POOL.close()
        except Exception:
            pass
        _BROWSER_POOL = None
    if _PW_POOL is not None:
        try:
            await _PW_POOL.stop()
        except Exception:
            pass
        _PW_POOL = None
    print(f"[azos][pool] browser pool fechado", flush=True)


async def cleanup_sessao(session_id: str):
    """Fecha context e remove sessão do dict — libera RAM mantendo browser vivo."""
    sess = _sessoes.pop(session_id, None)
    if not sess:
        return
    # Fecha context (libera page e RAM associada)
    ctx = sess.get("context")
    if ctx:
        try:
            await ctx.close()
        except Exception:
            pass
    # Se essa sessão tem browser próprio (não pool), fecha também
    if sess.get("_owned_browser") and sess.get("browser"):
        try:
            await sess["browser"].close()
        except Exception:
            pass
    if sess.get("_owned_pw") and sess.get("pw"):
        try:
            await sess["pw"].stop()
        except Exception:
            pass


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

    pw = None
    browser = None
    owned_browser = False
    owned_pw = False
    try:
        print(f"[azos][fase1] iniciando session_id={session_id} headless={_HEADLESS} "
              f"pool={'on' if _BROWSER_POOL else 'off'}", flush=True)

        # Pool persistente (produção): reusa Chromium global, cria só context isolado.
        # get_pool_browser() re-inicializa o pool se estiver desconectado.
        # Fallback (tests locais): lança Chromium próprio nessa cotação.
        if _BROWSER_POOL is not None or _PW_POOL is not None:
            browser = await get_pool_browser()
            pw = _PW_POOL
        else:
            pw = await async_playwright().start()
            owned_pw = True
            _launch_args = [
                "--window-size=1280,900",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
                "--disable-blink-features=AutomationControlled",
            ]
            print(f"[azos][fase1] lançando chromium (sem pool)...", flush=True)
            browser = await pw.chromium.launch(headless=_HEADLESS,
                                                slow_mo=0 if _HEADLESS else 120,
                                                args=_launch_args)
            owned_browser = True

        # ── SESSION REUSE — usa storage_state global se disponível ───────
        # Cada replica loga UMA vez no startup → captura cookies/storage.
        # Cada cotação cria new_context com esse state → entra já autenticado,
        # vai DIRETO pra /simulacao/dados-pessoais (pula login completamente).
        # Reduz 10000 logins → 42 (1 por replica).
        use_session_reuse = _AUTH_STATE is not None

        async def _criar_context(state=None):
            ctx_opts = {
                "viewport": {"width": 1280, "height": 900},
                "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0.0.0 Safari/537.36"),
            }
            if state:
                ctx_opts["storage_state"] = state
            c = await browser.new_context(**ctx_opts)
            await c.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR', 'pt', 'en-US']});
                window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
            """)
            return c

        context = await _criar_context(state=_AUTH_STATE if use_session_reuse else None)
        page = await context.new_page()

        if use_session_reuse:
            # Pula login. Vai direto pra simulação. Se cookie expirou, detecta
            # redirect pra /login e refaz autenticação.
            print(f"[azos][fase1] session-reuse on — pulando login, indo pra simulação", flush=True)
            await page.goto(AZOS_URL_SIM, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(1_500)
            if "/login" in page.url:
                print(f"[azos][fase1] cookie expirou (caiu em /login) — refresh _AUTH_STATE", flush=True)
                try:
                    await context.close()
                except Exception: pass
                # Refaz login → captura state novo → recria context
                await init_auth_state(force=True)
                context = await _criar_context(state=_AUTH_STATE)
                page = await context.new_page()
                await page.goto(AZOS_URL_SIM, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(1_500)
                if "/login" in page.url:
                    raise PWTimeout(f"Refresh auth state falhou — url={page.url}")
            await page.screenshot(path=str(_TMP / "azos_debug_02_dashboard.png"), full_page=False)
        else:
            # Modo legado (sem session reuse) — faz login fresh
            print(f"[azos][fase1] browser ok — navegando para login: {AZOS_URL_LOGIN}", flush=True)
            import random
            jitter = random.uniform(0, 3)
            await page.wait_for_timeout(int(jitter * 1000))
            await page.goto(AZOS_URL_LOGIN, wait_until="networkidle", timeout=60_000)
            # Aguarda React hidratar (sem isso o form faz GET default com pwd na URL)
            await page.wait_for_selector('[data-testid="login-button__submit"]', timeout=30_000)
            await page.wait_for_timeout(800)
            await page.screenshot(path=str(_TMP / "azos_debug_01_login.png"), full_page=False)
            await page.fill('[data-testid="login-input__email"]',    AZOS_EMAIL)
            await page.fill('[data-testid="login-input__password"]', AZOS_SENHA)
            # Enter dispara React submit handler (mais robusto que click)
            await page.press('[data-testid="login-input__password"]', "Enter")
            await page.wait_for_timeout(1_000)

            async def _esperar_pos_login():
                for tentativa in range(3):
                    if "/corretor/" in page.url:
                        return True
                    try:
                        await page.wait_for_url("**/corretor/**", timeout=90_000)
                        return True
                    except PWTimeout:
                        if "/login" in page.url:
                            try:
                                await page.wait_for_timeout(1_500)
                                await page.fill('[data-testid="login-input__email"]',    AZOS_EMAIL)
                                await page.fill('[data-testid="login-input__password"]', AZOS_SENHA)
                                await page.press('[data-testid="login-input__password"]', "Enter")
                                await page.wait_for_timeout(1_500)
                            except Exception: pass
                return False

            if not await _esperar_pos_login():
                raise PWTimeout(f"Login não completou após 3 retries — url={page.url}")
            print(f"[azos][fase1] login ok — url={page.url}", flush=True)
            await page.screenshot(path=str(_TMP / "azos_debug_02_dashboard.png"), full_page=False)

        # ── Simulação ────────────────────────────────────────────────────
        # Em modo session-reuse já estamos na URL de simulação. Só navega
        # se ainda não estiver lá.
        if "/simulacao/dados-pessoais" not in page.url:
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

        # Guarda sessão aberta para Fase 2 — flags _owned_* indicam se cleanup
        # deve fechar pw/browser (lifecycle próprio) ou só o context (pool)
        _sessoes[session_id] = {
            "pw": pw, "browser": browser, "context": context, "page": page,
            "_owned_pw": owned_pw, "_owned_browser": owned_browser,
        }
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


async def _detectar_modal_agravo(page) -> bool:
    """Detecta o modal 'Tivemos uma alteração na proposta' (agravo Azos)."""
    try:
        texto = (await page.inner_text("body")).lower()
        return ("tivemos uma alteração" in texto
                or "alteracao na proposta" in texto
                or "alteração na proposta" in texto
                or ("editar coberturas" in texto and "concordar" in texto))
    except Exception:
        return False


async def _ler_premio_modal_agravo(page) -> float | None:
    """Lê o prêmio FINAL exibido no modal de agravo (após ajustes)."""
    import re
    try:
        texto = await page.inner_text("body")
    except Exception:
        return None
    # Procura padrão "R$ X,XX por mês" — pega o VALOR FINAL (último ocorrência grande).
    # No modal: cotação inicial (riscada) e final (verde, em destaque).
    matches = list(re.finditer(r'R\$\s*([\d.]+),(\d{2})\s*(?:por\s*m[eê]s|/\s*m[eê]s)', texto, re.IGNORECASE))
    if not matches:
        return None
    # Pega o último match (geralmente o final após o riscado)
    m = matches[-1]
    try:
        val = float(m.group(1).replace('.', '') + '.' + m.group(2))
        if 5 < val < 2000:
            return val
    except Exception:
        pass
    return None


async def _clicar_modal_agravo(page, acao: str) -> bool:
    """Clica botão do modal de agravo. acao: 'editar' | 'concordar'."""
    if acao == "editar":
        seletores = [
            'button:has-text("Editar coberturas")',
            'button:has-text("Editar cobertura")',
            'button:has-text("editar")',
        ]
    else:  # concordar
        seletores = [
            'button:has-text("Concordar e continuar")',
            'button:has-text("Concordar")',
            'button:has-text("Aceitar")',
            'button:has-text("Continuar")',
        ]
    for sel in seletores:
        try:
            btn = page.locator(sel).first
            if await btn.count() and await btn.is_visible():
                await btn.scroll_into_view_if_needed()
                await btn.click()
                await page.wait_for_timeout(2_500)
                return True
        except Exception:
            continue
    return False


async def _continuar_habilitado(page) -> bool:
    """Retorna True se algum botão de avanço está clicável (não desabilitado).
    Verifica todos os candidatos: basta UM estar habilitado para retornar True.
    Usa Playwright Python puro (sem evaluate JS).

    IMPORTANTE: NÃO inclui 'fazer cotação' nas keywords porque esse é o botão
    do sidebar que sempre está habilitado (inicia nova simulação). Incluí-lo
    causa falso-positivo: _continuar_habilitado retorna True mesmo quando o
    botão real de avanço ("Ir para o Resumo") está disabled.
    """
    keywords = [
        'ir para o resumo', 'ir para o resumo da cotação',
        'ver cotação', 'continuar', 'próximo', 'avançar',
        'calcular', 'ver cota',
    ]
    try:
        all_btns = page.locator('button, [role="button"]')
        count = await all_btns.count()
        found_any = False
        for i in range(count):
            btn = all_btns.nth(i)
            try:
                text = (await btn.inner_text()).strip().lower()
            except Exception:
                continue
            # Ignora explicitamente o botão "Fazer cotação" do sidebar
            if "fazer cotação" in text or "fazer cotacao" in text:
                continue
            if not any(k in text for k in keywords):
                continue
            found_any = True
            try:
                if await btn.is_enabled() and await btn.is_visible():
                    return True
            except Exception:
                continue
        # Nenhum botão de avanço encontrado → assume bloqueado
        return False
    except Exception:
        return False


async def _ler_limites_slider(page, nome: str) -> dict:
    """Lê limites min/max/now do painel direito via Playwright Python (sem evaluate JS)."""
    nome_curto = nome[:30]
    try:
        all_inputs = page.locator('input[type="tel"]')
        n = await all_inputs.count()
        for i in range(n):
            inp = all_inputs.nth(i)
            if not await inp.is_visible():
                continue
            # Confirma que o input pertence à cobertura correta via H3 no container
            h3_loc = inp.locator('xpath=ancestor::*[descendant::h3][1]//h3[1]')
            if await h3_loc.count() > 0:
                h3_txt = (await h3_loc.inner_text()).strip()
                if nome_curto.lower()[:20] not in h3_txt.lower():
                    continue
            # Valor atual do input
            raw_val = (await inp.input_value()).strip()
            cleaned = re.sub(r'[^\d,]', '', raw_val).replace(',', '.')
            try:
                now_val = float(cleaned) if cleaned else 0.0
            except ValueError:
                now_val = 0.0
            # Atributos HTML min/max (geralmente ausentes em input[type=tel])
            min_attr = await inp.get_attribute("min")
            max_attr = await inp.get_attribute("max")
            min_val = float(min_attr) if min_attr else 0.0
            max_val = float(max_attr) if max_attr else 5_000_000.0
            # Tenta ler max de label vizinha: "Capital segurado (Máx R$ 3.000.000,00)"
            if not max_attr:
                lbl_loc = inp.locator('xpath=ancestor::*[contains(@class,"flex")][1]//label[1]')
                if await lbl_loc.count() > 0:
                    lbl_txt = (await lbl_loc.first.inner_text()).strip()
                    m = re.search(r'R\$\s*([\d.]+),([\d]{2})', lbl_txt)
                    if m:
                        try:
                            max_val = float(m.group(1).replace('.', '') + '.' + m.group(2))
                        except ValueError:
                            pass
            return {"min": min_val, "max": max_val, "now": now_val}
        return {}
    except Exception:
        return {}


async def _ajustar_capitais_acima_do_limite(page) -> int:
    """Lê mensagens 'O valor máximo é R$ X' do DOM e reduz inputs que excedem.

    Estratégia:
    1. Para cada cobertura no painel de coberturas, busca pelo padrão
       'Cobertura (Máx R$ N) ... O valor máximo é R$ N' que aparece quando o
       capital ultrapassou o limite do portal AZOS.
    2. Lê o capital atual do input correspondente.
    3. Se capital > limite, seta o input para o limite (clamp).
    4. Dispara eventos input/change pro React-Select reconhecer a mudança.

    Retorna: número de capitais ajustados.
    """
    ajustados = 0
    try:
        import re as _re

        def _parse_brl(val_str: str) -> int:
            """Parseia '1.800.000,00' ou '1800000' → 1800000."""
            s = val_str.strip().replace("R$", "").strip()
            m = _re.match(r'^([\d.]+)(?:,(\d{1,2}))?$', s)
            if m:
                return int(m.group(1).replace('.', ''))
            cleaned = _re.sub(r'[^\d]', '', s)
            return int(cleaned) if cleaned else 0

        # ESTRATÉGIA: pra cada input visível, ler o LABEL adjacente (que tem
        # "Cobertura (Máx R$ N)"). Se valor atual > max do próprio label,
        # clampar. Isso evita confusão entre coberturas e respeita o limite
        # específico de CADA input.
        all_inputs = page.locator('input[type="tel"]')
        n_inputs = await all_inputs.count()
        print(f"[azos][ajuste] checando {n_inputs} inputs type=tel...", flush=True)
        for i in range(n_inputs):
            inp = all_inputs.nth(i)
            try:
                if not await inp.is_visible():
                    continue
                # Valor atual
                val_str = (await inp.input_value()).strip()
                val_atual = _parse_brl(val_str)
                if val_atual <= 0:
                    continue
                # Lê o max do label adjacente — tenta vários xpaths
                # (a estrutura do React do AZOS varia entre coberturas)
                max_val = None
                lbl_txt = ""
                for xp in [
                    'xpath=ancestor::*[contains(@class,"flex")][1]//label',
                    'xpath=ancestor::*[3]//label',
                    'xpath=ancestor::*[4]//label',
                    'xpath=ancestor::div[descendant::label][1]//label',
                ]:
                    lbl_loc = inp.locator(xp).first
                    if await lbl_loc.count() == 0:
                        continue
                    try:
                        lbl_txt = (await lbl_loc.inner_text()).strip()
                    except Exception:
                        continue
                    m_max = _re.search(r'Máx\s*R\$\s*([\d.]+)(?:,\d{2})?', lbl_txt)
                    if m_max:
                        max_val = int(m_max.group(1).replace('.', ''))
                        break
                if max_val is None:
                    print(f"[azos][ajuste] input idx={i} val={val_atual}: sem label 'Máx R$'", flush=True)
                    continue
                if val_atual <= max_val:
                    continue
                novo_val = max_val
                print(f"[azos][ajuste] input idx={i} (label='{lbl_txt[:60]}'): {val_atual} → {novo_val}", flush=True)
                await inp.evaluate(f"""(el) => {{
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, '{novo_val}');
                    el.dispatchEvent(new Event('input', {{bubbles: true}}));
                    el.dispatchEvent(new Event('change', {{bubbles: true}}));
                    el.blur();
                }}""")
                await page.wait_for_timeout(400)
                ajustados += 1
            except Exception as e_inp:
                print(f"[azos][ajuste] erro input {i}: {str(e_inp)[:80]}", flush=True)
                continue
    except Exception as e:
        print(f"[azos][ajuste] ERRO: {str(e)[:120]}", flush=True)
    return ajustados


async def _desligar_cobertura(page, nome: str):
    """Desativa cobertura clicando no toggle (bg-primary → bg-black = deselect)."""
    nome_curto = nome[:30]
    nome_lower = nome.lower()
    nome_curto_lower = nome_curto.lower()
    try:
        toggle = None
        tentative = None
        all_toggles = page.locator('button.min-w-11.min-h-11[data-slot="tooltip-trigger"]')
        n_toggles = await all_toggles.count()
        for i in range(n_toggles):
            btn = all_toggles.nth(i)
            h3_loc = btn.locator('xpath=ancestor::*[descendant::h3][1]//h3[1]')
            if await h3_loc.count() == 0:
                continue
            h3_text = (await h3_loc.inner_text()).strip()
            h3_lower = h3_text.lower()
            if h3_lower == nome_lower:
                toggle = btn
                break
            if nome_curto_lower in h3_lower and tentative is None:
                tentative = btn
        if toggle is None:
            toggle = tentative

        if not toggle:
            print(f"[azos][adj] toggle nao encontrado para desligar '{nome_curto}'", flush=True)
            return

        cls = await toggle.get_attribute("class") or ""
        aria_checked = await toggle.get_attribute("aria-checked")
        is_selected = ("bg-primary" in cls and "bg-black" not in cls) or aria_checked == "true"

        if is_selected:
            await toggle.scroll_into_view_if_needed()
            await toggle.click()
            await page.wait_for_timeout(600)
            print(f"[azos][adj] desligou '{nome_curto}'", flush=True)
        else:
            print(f"[azos][adj] '{nome_curto}' ja estava desligada", flush=True)
    except Exception as e:
        print(f"[azos][adj] erro desligar '{nome_curto}': {e}", flush=True)


# ──────────────────────────────────────────────────────────────────────────────
# FASE 2 — Seleciona coberturas, preenche saúde/riscos, retorna cotação final
# ──────────────────────────────────────────────────────────────────────────────
async def fase2_selecionar_coberturas(session_id: str, selecoes: list[dict],
                                       saude: dict | None = None,
                                       coberturas_limits: dict | None = None,
                                       budget_target: float = 50.0,
                                       dry_run: bool = False,
                                       parar_cotacao: bool = False) -> dict:
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

    resultado = {"premio_mensal": None, "premio_anual": None, "detalhes": "", "erro": None,
                 "link_pagamento": None}

    sessao = _sessoes.get(session_id)
    if not sessao:
        resultado["erro"] = "Sessão não encontrada ou expirada"
        print(f"[azos][fase2] sessão {session_id} não encontrada", flush=True)
        return resultado

    page = sessao["page"]
    saude = saude or {}
    # Faixa do orçamento — 6% de tolerância abaixo do teto, target = teto
    _budget_target = float(budget_target or 50.0)
    _budget_max    = _budget_target
    _budget_min    = round(_budget_target * 0.94, 2)
    print(f"[azos][fase2] iniciando, session_id={session_id}, url={page.url}, "
          f"budget=[{_budget_min}-{_budget_max}] target={_budget_target}", flush=True)

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
        # Reseta: desliga todas as coberturas selecionadas
        print(f"[azos][fase2] resetando seleções para estado fresco...", flush=True)
        _ativos_reset = page.locator('button.min-w-11.min-h-11.bg-primary[data-slot="tooltip-trigger"]')
        _n_reset = await _ativos_reset.count()
        for _ in range(_n_reset):
            try:
                await _ativos_reset.first.click()
                await page.wait_for_timeout(200)
            except Exception:
                break
        await page.wait_for_timeout(800)

        # ── ESTRATÉGIA SELECT-ALL + FILL-BY-INDEX (validada R$49,59 vs screenshot) ──
        # Bug histórico: select+fill one-by-one disparava re-render do React e
        # snapava coberturas anteriores aos defaults Azos. Solução validada:
        # 1) Toggle ON cada cobertura na ORDEM (sem off+on)
        # 2) Após todas selecionadas, fill por ÍNDICE posicional dos inputs
        #    (input[i] no painel direito = i-ésima cobertura selecionada)
        print(f"[azos][fase2] selecionando coberturas: {[s['nome'] for s in selecoes]}", flush=True)

        # PHASE 1: toggle ON cada cobertura (sem fill nesta passada)
        for sel in selecoes:
            await _selecionar_cobertura(page, sel["nome"], 0)  # valor=0 = só seleciona
            await page.wait_for_timeout(500)
        await page.wait_for_timeout(3_000)  # estabiliza React state
        print(f"[azos][fase2] todas selecionadas (defaults Azos)", flush=True)

        # PHASE 2: fill por índice (ordem dos inputs = ordem de seleção)
        # USA verificação com retry para combater snap-up Azos: cada input é
        # re-setado até DOM mostrar o valor (max 8 tentativas).
        inputs_tel = page.locator('input[type="tel"]')
        n_inp = await inputs_tel.count()
        print(f"[azos][fase2] {n_inp} inputs visíveis no painel direito", flush=True)
        for i, sel in enumerate(selecoes):
            if i >= n_inp:
                print(f"[azos][fase2] sem input para idx {i} ({sel['nome']})", flush=True)
                continue
            valor = sel.get("valor", 0)
            if valor <= 0:
                continue
            inp = inputs_tel.nth(i)
            ok, val_final = await _setar_input_com_verificacao(page, inp, valor)
            marker = "OK" if ok else "DIVERGENTE"
            print(f"[azos][fase2] [{i}] {sel['nome'][:30]:30s} alvo={valor:>6} DOM={val_final:>10.0f} {marker}",
                  flush=True)
        await page.wait_for_timeout(2_000)
        print(f"[azos][fase2] coberturas filled por índice (com verificação)", flush=True)

        # ── BLEND: para na cotação (após selecionar + preencher). Não entra em
        # PHASE 3, calibração ou DPS. Só lê o prêmio resultante e devolve.
        if parar_cotacao:
            await page.wait_for_timeout(3_000)  # estabiliza React + cálculo Azos
            await page.screenshot(path=str(_TMP / "azos_cotacao_final.png"), full_page=False)
            premio_bl = await _ler_premio_coberturas(page)
            if premio_bl is None:
                try:
                    texto_bl = await page.inner_text("body")
                    premio_bl = _extrair_premio_mensal(texto_bl)
                except Exception:
                    pass
            resultado["premio_mensal"] = premio_bl
            resultado["premio_anual"]  = round(premio_bl * 12, 2) if premio_bl else None
            resultado["selecoes"] = selecoes
            try:
                resultado["detalhes"] = (await page.inner_text("body"))[:3000]
            except Exception:
                pass
            print(f"[azos][fase2] BLEND parar_cotacao - premio={premio_bl} "
                  f"selecoes={[s['nome'][:25] for s in selecoes]}", flush=True)

            # ── PERSISTÊNCIA: o Azos só salva a cotação com valor quando o
            # corretor avança da tela de coberturas. Se fechamos aqui, o portal
            # grava como rascunho R$ 0. Clica "Ir para o Resumo" 1x e aguarda
            # — não preenche DPS (só queremos que o portal mantenha o valor).
            try:
                url_antes = page.url
                print(f"[azos][fase2] BLEND parar_cotacao - URL antes={url_antes}", flush=True)

                # Aguarda o botão habilitar (até 20s). Pode estar disabled
                # enquanto o portal recalcula o prêmio OU porque algum capital
                # excede o limite do portal (ex: Morte Acidental máx 1MM).
                avancou_url = False
                for tentativa in range(8):
                    habilitado = await _continuar_habilitado(page)
                    if habilitado:
                        print(f"[azos][fase2] BLEND botão habilitado na tentativa {tentativa}", flush=True)
                        break

                    # AUTO-AJUSTE: se botão está bloqueado, lê mensagens
                    # "Máx R$ X" do DOM e reduz capitais que excedem.
                    if tentativa == 2:
                        ajustou = await _ajustar_capitais_acima_do_limite(page)
                        if ajustou:
                            print(f"[azos][fase2] BLEND auto-ajustou {ajustou} capitais — aguardando portal recalcular", flush=True)
                            await page.wait_for_timeout(3_500)
                            continue
                    print(f"[azos][fase2] BLEND tentativa {tentativa}: botão ainda BLOQUEADO, aguardando 2s...", flush=True)
                    await page.wait_for_timeout(2_000)
                else:
                    # Se chegou no else (8 tentativas sem habilitar), dump do
                    # body procurando mensagem de bloqueio
                    try:
                        body_txt = await page.inner_text("body")
                        for marcador in ["valor máximo", "valor maximo", "limite", "inválido", "obrigatório"]:
                            idx = body_txt.lower().find(marcador)
                            if idx >= 0:
                                trecho = body_txt[max(0, idx-100):idx+200].replace('\n', ' | ')
                                print(f"[azos][fase2] BLEND BLOQUEIO inline '{marcador}': ...{trecho}...", flush=True)
                                break
                    except Exception:
                        pass

                # Tenta clicar até 3x — botão pode ainda estar bouncing de
                # disabled<->enabled enquanto AZOS recalcula
                for retry in range(3):
                    _avancou = await _clicar_continuar(page)
                    print(f"[azos][fase2] BLEND retry={retry} clicou_continuar={_avancou}", flush=True)
                    if _avancou:
                        await page.wait_for_timeout(3_500)
                        # Verifica se URL realmente mudou (avançou pra resumo/proposta)
                        if page.url != url_antes:
                            avancou_url = True
                            print(f"[azos][fase2] BLEND URL mudou: {url_antes} → {page.url}", flush=True)
                            break
                        else:
                            print(f"[azos][fase2] BLEND URL não mudou ainda (click sem efeito) — retry", flush=True)
                            await page.wait_for_timeout(1_500)
                    else:
                        await page.wait_for_timeout(2_000)

                # Aguarda gravar
                await page.wait_for_timeout(4_000)
                await page.screenshot(path=str(_TMP / "azos_cotacao_persistida.png"), full_page=False)
                if avancou_url:
                    print(f"[azos][fase2] BLEND ✅ cotação persistida em {page.url}", flush=True)
                else:
                    print(f"[azos][fase2] BLEND ⚠️ NÃO avançou da tela de coberturas — portal pode gravar R$ 0. URL final={page.url}", flush=True)
            except Exception as e:
                print(f"[azos][fase2] BLEND falha ao persistir (ignorando): {str(e)[:200]}", flush=True)

            return resultado

        # ── PHASE 3: protocolo do corretor para mandatórias ──────────────────
        # Se TODAS são mandatórias, executa o protocolo:
        # mins já filled → incrementa linha por linha até hit [47-55].
        # Skip do calibrador antigo (que escalaria via toggle off+on → resetaria).
        _todas_mandatory_inicial = all(s.get("mandatory", False) for s in selecoes)
        if _todas_mandatory_inicial:
            print(f"[azos][fase2] PHASE 3: protocolo corretor (incremento linha por linha)", flush=True)
            ativas_inc, premio_inc, sucesso_inc = await _incrementar_coberturas_mandatorias(
                page, selecoes,
                budget_min=_budget_min, budget_max=_budget_max, budget_target=_budget_target,
            )
            print(f"[azos][fase2] PHASE 3 result: premio={premio_inc} sucesso={sucesso_inc}", flush=True)
            selecoes = ativas_inc
            resultado["selecoes_finais"] = list(ativas_inc)
            # Tenta avançar diretamente sem rodar o calibrador antigo
            await page.keyboard.press("End")
            await page.wait_for_timeout(500)
            avancou_inc = await _clicar_continuar(page)
            print(f"[azos][fase2] avançou após PHASE 3: {avancou_inc}", flush=True)
            _skip_calibrador = True
        else:
            _skip_calibrador = False

        await page.screenshot(path=str(_TMP / "azos_debug_f2_01_coberturas.png"), full_page=False)

        # ── Loop inteligente: calibra coberturas até Continuar habilitado ──────
        # Alvo: prêmio ≈ orçamento escolhido. Aceita [BUDGET-TOL, BUDGET+TOL].
        # Abaixo da faixa → escala coberturas para cima.
        # Acima da faixa → escala para baixo (ou desliga se já no mínimo).
        BUDGET       = _budget_target
        # Tolerância proporcional ao orçamento (16% pra absorver steps do slider).
        TOLERANCIA   = max(8.0, _budget_target * 0.16)
        BUDGET_MIN   = BUDGET - TOLERANCIA
        BUDGET_MAX   = BUDGET + TOLERANCIA
        limits     = coberturas_limits or {}
        ativas     = list(selecoes)   # cópia mutável das coberturas selecionadas
        _caso4_prev_vals: tuple | None = None   # detecta estagnação no Caso 4

        def _is_diaria(nome: str) -> bool:
            # Apenas Diária de Internação Hospitalar tem valor diário fixo (~R$50–500).
            # Funeral e Assistência funeral têm capital segurado normal → calibrar normalmente.
            nl = nome.lower()
            return "diária" in nl or "diaria" in nl or "internação" in nl or "internacao" in nl

        for adj_iter in range(0 if _skip_calibrador else 12):
            await page.wait_for_timeout(1_500)

            premio     = await _ler_premio_coberturas(page)
            habilitado = await _continuar_habilitado(page)
            p_str      = f"R${premio:.2f}" if premio is not None else "N/A"
            print(f"[azos][adj] ══ iter={adj_iter} premio={p_str} "
                  f"continuar={'OK' if habilitado else 'BLOQUEADO'}", flush=True)

            # Caso 1b — TODAS mandatórias E Continuar=OK → aceita IMEDIATO
            # Esta verificação vem PRIMEIRO. Se todas mandatórias e Continuar
            # está habilitado, NUNCA escala (nem pra cima, nem pra baixo) —
            # porque qualquer scale chama _selecionar_cobertura que faz
            # toggle off+on e RESETA valores aos defaults Azos. O premio
            # explode e tudo é destruído. Aceita o que tem.
            todas_mand = all(s.get("mandatory", False) for s in ativas) if ativas else False
            if todas_mand and habilitado:
                print(f"[azos][adj] aceito mandatory! {p_str} (todas mandatórias + Continuar=OK)", flush=True)
                selecoes = ativas
                break

            # Caso 1 — prêmio na faixa [42, 58] E botão habilitado → avança
            # premio is None: não conseguiu ler, mas botão habilitado → assume ok
            if habilitado and (premio is None or BUDGET_MIN <= premio <= BUDGET_MAX):
                print(f"[azos][adj] aceito! {p_str} dentro da faixa [{BUDGET_MIN:.0f}-{BUDGET_MAX:.0f}] — avancando", flush=True)
                selecoes = ativas
                break

            # Caso 2 — Prêmio na faixa mas Continuar bloqueado
            # React não reconheceu o estado → recicla switches (off→on) e re-aplica sliders
            if premio is not None and BUDGET_MIN <= premio <= BUDGET_MAX and not habilitado:
                print(f"[azos][adj] premio ok mas continuar bloqueado → reciclando switches", flush=True)
                for sel in ativas:
                    await _desligar_cobertura(page, sel["nome"])
                await page.wait_for_timeout(400)
                for sel in ativas:
                    await _selecionar_cobertura(page, sel["nome"], sel["valor"])
                await page.wait_for_timeout(600)
                continue

            # Se todas mandatórias mas Continuar=BLOQUEADO: NÃO escala (resetaria
            # valores). Sai do loop e deixa o force-click/recarga do outer loop agir.
            if todas_mand and not habilitado:
                print(f"[azos][adj] todas mandatórias + Continuar BLOQUEADO — não escala (premio={p_str})", flush=True)
                selecoes = ativas
                break

            # Caso 3 — Prêmio ACIMA da faixa (> 58) → escala para baixo
            if premio is not None and premio > BUDGET_MAX:
                ratio = BUDGET / premio
                novas = []
                todas_no_min = True
                for sel in ativas:
                    # Diária de internação: valor diário (R$/dia, escala 50–500).
                    # Escalada com step de 10 — DIFERENTE de capital (step 1000).
                    if _is_diaria(sel["nome"]):
                        v_min_d = 50
                        v_max_d = 500
                        novo_d = int((sel["valor"] * ratio) / 10) * 10
                        if ratio < 1 and novo_d >= sel["valor"]:
                            novo_d = sel["valor"] - 10
                        novo_d = int(max(v_min_d, min(v_max_d, novo_d)))
                        if novo_d > v_min_d:
                            todas_no_min = False
                        print(f"[azos][adj]   {sel['nome'][:25]}: {sel['valor']}→{novo_d} (diaria, vmin={v_min_d})", flush=True)
                        novas.append({**sel, "valor": novo_d})
                        continue
                    # Capital normal: limites vêm de coberturas_limits (lido 1× em fase1).
                    # Sem leitura de DOM por iter — usa só o snapshot inicial.
                    lim   = limits.get(sel["nome"], {})
                    v_min = float(lim.get("valor_min") or 1_000)
                    v_max = float(lim.get("valor_max") or 5_000_000)
                    target = sel["valor"] * ratio
                    novo_v = int(target / 1_000) * 1_000
                    # Se ratio < 1 mas o floor não reduziu, força passo de -1000.
                    if ratio < 1 and novo_v >= sel["valor"]:
                        novo_v = sel["valor"] - 1_000
                    novo_v = int(max(v_min, min(v_max, novo_v)))
                    if novo_v > v_min:
                        todas_no_min = False
                    print(f"[azos][adj]   {sel['nome'][:25]}: {sel['valor']}→{novo_v} (vmin={int(v_min)})", flush=True)
                    novas.append({**sel, "valor": novo_v})

                # Sub-caso: todas no mínimo e ainda caro
                if todas_no_min:
                    # 1. Antes de tudo, se premio é aceitável (≤ 1.5×BUDGET) → aceita.
                    if premio is not None and premio <= BUDGET * 1.5 and habilitado:
                        print(f"[azos][adj] todas no min, premio {p_str} aceitável (≤ {BUDGET*1.5:.0f}) — aceita p/ evitar desligar", flush=True)
                        selecoes = ativas
                        break

                    # 2. Só pode desligar coberturas NÃO-mandatórias.
                    nao_mandatorias = [s for s in ativas if not s.get("mandatory", False)]
                    mandatorias     = [s for s in ativas if s.get("mandatory", False)]

                    # Se TODAS são mandatórias → não pode desligar nenhuma.
                    # Premio acima de 1.5×BUDGET com mandatórias no mínimo: configuração
                    # IMPOSSÍVEL no orçamento. Aceita o mínimo possível e segue
                    # (não há nada a fazer — Azos não permite reduzir mais).
                    if not nao_mandatorias:
                        print(f"[azos][adj] todas as {len(mandatorias)} cobs são MANDATORY no min — premio {p_str} é o mínimo Azos para este perfil", flush=True)
                        selecoes = ativas
                        break

                    if len(nao_mandatorias) > 0:
                        mais_cara = max(
                            nao_mandatorias,
                            key=lambda s: float(limits.get(s["nome"], {}).get("valor_min") or 0)
                        )
                        print(f"[azos][adj] todas no min + caro ({p_str} > {BUDGET*1.5:.0f}) → desligando NÃO-mandatory '{mais_cara['nome'][:25]}'", flush=True)
                        await _desligar_cobertura(page, mais_cara["nome"])
                        ativas = [s for s in ativas if s["nome"] != mais_cara["nome"]]
                    else:
                        # Única cobertura no mínimo e ainda cara
                        # Tenta trocar para "Morte acidental" se ainda não estiver usando
                        # (taxa 0.050 → menor mínimo real no Azos)
                        nome_unico = ativas[0]["nome"].lower()
                        nomes_disp = list(limits.keys()) if limits else []
                        nome_morte = next(
                            (n for n in nomes_disp if "morte acidental" in n.lower()), None
                        )
                        nome_inv = next(
                            (n for n in nomes_disp if "invalidez total por acidente" in n.lower()), None
                        )
                        alternativa = None
                        if nome_morte and "morte acidental" not in nome_unico:
                            alternativa = nome_morte
                        elif nome_inv and "invalidez total por acidente" not in nome_unico:
                            alternativa = nome_inv
                        if alternativa:
                            print(f"[azos][adj] unica cob cara → trocando para '{alternativa[:30]}'", flush=True)
                            await _desligar_cobertura(page, ativas[0]["nome"])
                            novo_cap = max(50_000, min(5_000_000,
                                int(round(BUDGET / 0.050 * 1_000 / 1_000) * 1_000)))
                            ativas = [{"nome": alternativa, "valor": novo_cap}]
                            await _selecionar_cobertura(page, alternativa, novo_cap)
                            await page.wait_for_timeout(800)
                        else:
                            # Sem alternativa — aceita o prêmio do mínimo
                            print(f"[azos][adj] cobertura unica no min → aceita {p_str}", flush=True)
                            selecoes = ativas
                            break
                else:
                    for sel in novas:
                        await _selecionar_cobertura(page, sel["nome"], sel["valor"])
                    ativas = novas

                await page.screenshot(path=str(_TMP / f"azos_debug_adj_{adj_iter:02d}.png"))
                continue

            # Caso 4 — Continuar bloqueado (prêmio baixo/nulo ou inputs não atualizaram React)
            if not habilitado:
                cur_vals = tuple(s["valor"] for s in ativas)
                if _caso4_prev_vals == cur_vals:
                    # Valores idênticos em 2 iterações seguidas → inputs disabled não respondem.
                    # Sai do loop e deixa o mecanismo de force-click do loop externo agir.
                    print(f"[azos][adj] Caso4 sem progresso (inputs disabled) → saindo do loop adj", flush=True)
                    selecoes = ativas
                    break
                _caso4_prev_vals = cur_vals

                print(f"[azos][adj] continuar bloqueado + premio baixo → garantindo mínimos (Python)", flush=True)
                novas = []
                for sel in ativas:
                    if _is_diaria(sel["nome"]):
                        novas.append(sel)
                        continue
                    v_min = float(limits.get(sel["nome"], {}).get("valor_min") or 1_000)
                    novo_v = int(max(sel["valor"], v_min))
                    print(f"[azos][adj]   {sel['nome'][:25]}: {sel['valor']}→{novo_v} (vmin={int(v_min)})", flush=True)
                    novas.append({**sel, "valor": novo_v})
                for sel in novas:
                    await _selecionar_cobertura(page, sel["nome"], sel["valor"])
                ativas = novas
                continue

            # Caso 5 — Prêmio ABAIXO da faixa (< 42) E botão habilitado → escala para cima
            if premio is not None and premio < BUDGET_MIN and habilitado:
                ratio = BUDGET / premio
                print(f"[azos][adj] premio baixo ({p_str}) — escalando para cima ratio={ratio:.2f}", flush=True)
                novas = []
                for sel in ativas:
                    if _is_diaria(sel["nome"]):
                        # Diária: scale com step 10, máx 500
                        novo_d = int((sel["valor"] * ratio) / 10) * 10
                        if ratio > 1 and novo_d <= sel["valor"]:
                            novo_d = sel["valor"] + 10
                        novo_d = int(min(500, novo_d))
                        print(f"[azos][adj]   {sel['nome'][:25]}: {sel['valor']}→{novo_d} (diaria)", flush=True)
                        novas.append({**sel, "valor": novo_d})
                        continue
                    # Capital: usa apenas coberturas_limits (sem DOM read por iter)
                    lim   = limits.get(sel["nome"], {})
                    v_max = float(lim.get("valor_max") or 5_000_000)
                    target = sel["valor"] * ratio
                    novo_v = int(target / 1_000) * 1_000
                    if ratio > 1 and novo_v <= sel["valor"]:
                        novo_v = sel["valor"] + 1_000
                    novo_v = int(min(v_max, novo_v))
                    print(f"[azos][adj]   {sel['nome'][:25]}: {sel['valor']}→{novo_v} (vmax={int(v_max)})", flush=True)
                    novas.append({**sel, "valor": novo_v})
                for sel in novas:
                    await _selecionar_cobertura(page, sel["nome"], sel["valor"])
                ativas = novas
                continue

        else:
            # Esgotou iterações — usa o que estiver disponível e segue em frente
            print(f"[azos][adj] max iteracoes — seguindo com estado atual", flush=True)
            selecoes = ativas

        # IMPORTANTE: armazena as selecoes REAIS (após calibração) no resultado.
        # main.py usa isso pra mostrar ao usuário EXATAMENTE o que foi enviado
        # à Azos — sem essa propagação o sistema mostra as coberturas originais
        # do recomendador (potencialmente diferentes do que sobrou após desligar).
        resultado["selecoes_finais"] = list(selecoes)

        # ── Avança para próximo step ──────────────────────────────────────
        print(f"[azos][fase2] clicando continuar...", flush=True)
        await page.wait_for_timeout(1_500)
        await _clicar_continuar(page)
        await page.wait_for_timeout(4_000)
        print(f"[azos][fase2] após continuar, url={page.url}", flush=True)
        await page.screenshot(path=str(_TMP / "azos_debug_f2_02_pos_continuar.png"), full_page=False)

        # ── Loop por TODOS os steps até link de pagamento ────────────────
        urls_visitadas: set[str] = set()
        premio_extraido    = False
        checkout_preenchido = False

        print(f"[azos][fase2] iniciando loop de steps...", flush=True)
        cadastro_retries = 0  # contador de sub-steps do cadastro visitados
        dps_outer_calls  = 0  # quantas vezes _preencher_dps_completo foi chamado
        agravo_retries   = 0  # contador de re-balanceamentos por agravo (max 3)
        for tentativa in range(60):
            await page.wait_for_timeout(1_500)
            url_atual = page.url
            titulo    = (await _titulo_pagina(page)).lower()
            texto_pg  = await page.inner_text("body")
            print(f"[azos][fase2] ══ tentativa={tentativa} ══════════════════════════", flush=True)
            print(f"[azos][fase2]    url={url_atual}", flush=True)
            print(f"[azos][fase2]    titulo='{titulo[:80]}'", flush=True)

            await page.screenshot(path=str(_TMP / f"azos_step_{tentativa:02d}.png"), full_page=True)
            print(f"[azos][fase2]    screenshot=azos_step_{tentativa:02d}.png", flush=True)

            # ── Detecta redirect de sessão expirada (saiu do domínio contratacao) ──
            # Se a sessão expirou, Azos redireciona para corretores.azos.com.br
            # ou para a tela inicial do fluxo — fora do domínio contratacao.
            _no_fluxo = ("contratacao.azos.com.br" in url_atual
                         or "simulacao" in url_atual
                         or "contratacao" in url_atual)
            if not _no_fluxo and tentativa >= 2:
                print(f"[azos][fase2] sessão expirada ou redirect inesperado: {url_atual}", flush=True)
                await page.screenshot(path=str(_TMP / "azos_session_redirect.png"), full_page=True)
                resultado["erro"] = f"Sessão redirecionada (fora do fluxo): {url_atual[:120]}"
                break

            # ── Modal "CPF vinculado" — sempre verifica e dispensa ───────
            if "vinculado ao e-mail" in texto_pg.lower() or "esse cpf" in texto_pg.lower():
                await _fechar_modal_cpf_vinculado(page)
                await page.wait_for_timeout(500)
                continue  # reavalia o estado após fechar o modal

            # ── Modal "Tivemos uma alteração na proposta" (agravo Azos) ────
            # Após DPS, Azos pode reduzir capitais ou remover coberturas (agravo).
            # Premio cai. Re-balanceamos coberturas restantes até hit [45-50].
            # NUNCA encerrar sem enviar proposta — após max retries, concordar
            # mesmo se abaixo da faixa.
            if await _detectar_modal_agravo(page):
                premio_agravado = await _ler_premio_modal_agravo(page)
                print(f"[azos][fase2] 🔔 MODAL AGRAVO detectado — premio={premio_agravado} "
                      f"retries={agravo_retries}/3", flush=True)
                await page.screenshot(path=str(_TMP / f"azos_agravo_{agravo_retries:02d}.png"),
                                       full_page=True)

                # Premio na faixa OU max retries atingido → CONCORDAR (não encerrar)
                _ok_faixa = (premio_agravado is not None
                             and _budget_min <= premio_agravado <= _budget_max)
                if _ok_faixa or agravo_retries >= 3:
                    motivo = ("dentro da faixa" if _ok_faixa
                              else f"max retries ({agravo_retries})")
                    print(f"[azos][fase2] agravo: concordando ({motivo}) — premio final={premio_agravado}",
                          flush=True)
                    if premio_agravado is not None:
                        resultado["premio_mensal"] = premio_agravado
                    ok = await _clicar_modal_agravo(page, "concordar")
                    print(f"[azos][fase2] click 'Concordar' = {ok}", flush=True)
                    await page.wait_for_timeout(3_500)
                    urls_visitadas.discard(url_atual)
                    continue

                # Premio fora da faixa → editar coberturas e re-balancear
                print(f"[azos][fase2] agravo: editando coberturas (premio {premio_agravado} fora de "
                      f"[{_budget_min}-{_budget_max}])", flush=True)
                agravo_retries += 1
                ok = await _clicar_modal_agravo(page, "editar")
                print(f"[azos][fase2] click 'Editar coberturas' = {ok}", flush=True)
                await page.wait_for_timeout(3_000)

                # Re-roda calibração na página de coberturas (caps atualizados pela Azos)
                try:
                    ativas_reb, premio_reb, sucesso_reb = await _incrementar_coberturas_mandatorias(
                        page, selecoes,
                        budget_min=_budget_min, budget_max=_budget_max, budget_target=_budget_target,
                    )
                    print(f"[azos][fase2] re-balanceamento: premio={premio_reb} sucesso={sucesso_reb}",
                          flush=True)
                    # Atualiza selecoes em resultado pra consistência sistema↔proposta
                    resultado["selecoes_finais"] = [
                        {"nome": s["nome"], "valor": s["valor"],
                         "motivo": s.get("motivo", ""), "mandatory": s.get("mandatory", False)}
                        for s in ativas_reb
                    ]
                except Exception as _re:
                    print(f"[azos][fase2] erro no re-balanceamento: {_re}", flush=True)

                # Avança de novo: vai re-passar DPS (já preenchida automaticamente)
                await _clicar_continuar(page)
                await page.wait_for_timeout(3_000)
                urls_visitadas.clear()  # reset para refluir todos os steps
                continue

            # ── Tela de Proposta enviada — extrai link e para ────────────
            if "contratacao" in url_atual and "proposta-enviada" in url_atual:
                # Salva HTML para debug
                try:
                    html_env = await page.content()
                    with open(_TMP / "azos_proposta_enviada.html", "w", encoding="utf-8") as _f:
                        _f.write(html_env)
                    await page.screenshot(path=str(_TMP / "azos_proposta_enviada.png"), full_page=True)
                except Exception:
                    pass

                # Tenta capturar links que não foram obtidos na tela anterior
                if not resultado.get("link_assinatura") or not resultado.get("link_pagamento"):
                    try:
                        links_env = await page.evaluate(r"""() => {
                            const urls = [];
                            for (const inp of document.querySelectorAll('input[readonly]')) {
                                const v = (inp.value || '').trim();
                                if (v.startsWith('http')) urls.push(v);
                            }
                            for (const a of document.querySelectorAll('a[href]')) {
                                const h = (a.href || '').trim();
                                if (h.startsWith('http') && h !== window.location.href) urls.push(h);
                            }
                            for (const el of document.querySelectorAll('p,span,div,strong')) {
                                const t = (el.innerText || '').trim();
                                if (t.startsWith('https://') && t.length < 500) urls.push(t);
                            }
                            return [...new Set(urls)];
                        }""")
                        for lnk in links_env:
                            lu = lnk.lower()
                            is_sign = any(k in lu for k in ["clicksign", "assinar", "assinatura", "sign", "/sign/", "d4sign"])
                            if is_sign and not resultado.get("link_assinatura"):
                                resultado["link_assinatura"] = lnk
                            elif not is_sign and not resultado.get("link_pagamento") and lnk != resultado.get("link_assinatura"):
                                resultado["link_pagamento"] = lnk
                    except Exception:
                        pass

                resultado["detalhes"] = texto_pg[:5000]
                break

            # ── Tela de Proposta (revisão) — extrai links e envia para assinatura ─
            if "contratacao" in url_atual and "proposta" in url_atual:
                # Salva HTML para debug
                try:
                    html_pg = await page.content()
                    with open(_TMP / "azos_proposta_review.html", "w", encoding="utf-8") as _f:
                        _f.write(html_pg)
                except Exception:
                    pass

                # Extrai links de assinatura e pagamento da página
                links_proposta = await page.evaluate(r"""() => {
                    const resultado = {assinatura: null, pagamento: null};

                    // Estratégia 1: readonly inputs com URLs
                    const inputs = Array.from(document.querySelectorAll('input[readonly], input[type="text"][readonly]'));
                    for (const inp of inputs) {
                        const v = (inp.value || '').trim();
                        if (!v.startsWith('http')) continue;
                        const label = (inp.closest('div,section,label,p')?.innerText || '').toLowerCase();
                        if (!resultado.assinatura && (label.includes('assinatura') || label.includes('assinar') || label.includes('clicksign') || v.includes('clicksign'))) {
                            resultado.assinatura = v;
                        } else if (!resultado.pagamento && (label.includes('pagamento') || label.includes('pagar') || label.includes('checkout'))) {
                            resultado.pagamento = v;
                        } else if (!resultado.assinatura) {
                            resultado.assinatura = v;
                        } else if (!resultado.pagamento) {
                            resultado.pagamento = v;
                        }
                    }

                    // Estratégia 2: data-link ou data-url em botões/elementos
                    if (!resultado.assinatura || !resultado.pagamento) {
                        const els = Array.from(document.querySelectorAll('[data-link],[data-url],[data-href],[data-copy]'));
                        for (const el of els) {
                            const v = (el.dataset.link || el.dataset.url || el.dataset.href || el.dataset.copy || '').trim();
                            if (!v.startsWith('http')) continue;
                            const label = (el.closest('div,section')?.innerText || '').toLowerCase();
                            if (!resultado.assinatura && (label.includes('assinatura') || v.includes('clicksign'))) {
                                resultado.assinatura = v;
                            } else if (!resultado.pagamento && label.includes('pagamento')) {
                                resultado.pagamento = v;
                            } else if (!resultado.assinatura) {
                                resultado.assinatura = v;
                            } else if (!resultado.pagamento) {
                                resultado.pagamento = v;
                            }
                        }
                    }

                    // Estratégia 3: links (a href) com textos relevantes
                    if (!resultado.assinatura || !resultado.pagamento) {
                        const anchors = Array.from(document.querySelectorAll('a[href]'));
                        for (const a of anchors) {
                            const href = (a.href || '').trim();
                            if (!href.startsWith('http') || href === window.location.href) continue;
                            const txt = (a.innerText || a.textContent || '').toLowerCase();
                            if (!resultado.assinatura && (href.includes('clicksign') || txt.includes('assinatura') || txt.includes('assinar'))) {
                                resultado.assinatura = href;
                            } else if (!resultado.pagamento && (txt.includes('pagamento') || txt.includes('pagar') || txt.includes('checkout'))) {
                                resultado.pagamento = href;
                            }
                        }
                    }

                    // Estratégia 4: qualquer elemento com texto de URL (span/p/div)
                    if (!resultado.assinatura || !resultado.pagamento) {
                        const tudo = Array.from(document.querySelectorAll('span,p,div,td'));
                        for (const el of tudo) {
                            if (el.children.length > 0) continue;  // só folhas
                            const v = (el.textContent || '').trim();
                            if (!v.startsWith('http') || v.length > 500) continue;
                            const label = (el.closest('div,section')?.innerText || '').toLowerCase();
                            if (!resultado.assinatura && (v.includes('clicksign') || label.includes('assinatura'))) {
                                resultado.assinatura = v;
                            } else if (!resultado.pagamento && label.includes('pagamento')) {
                                resultado.pagamento = v;
                            } else if (!resultado.assinatura) {
                                resultado.assinatura = v;
                            } else if (!resultado.pagamento) {
                                resultado.pagamento = v;
                            }
                        }
                    }

                    return resultado;
                }""")

                if links_proposta.get("assinatura"):
                    resultado["link_assinatura"] = links_proposta["assinatura"]
                if links_proposta.get("pagamento"):
                    resultado["link_pagamento"] = links_proposta["pagamento"]

                # Se não achou pelos métodos passivos, tenta clicar botões de copiar
                # e ler clipboard (requer permissão — só como fallback)
                if not resultado.get("link_assinatura") and not resultado.get("link_pagamento"):
                    try:
                        btn_copiar_list = await page.locator(
                            'button:has-text("Copiar"), button:has-text("copiar"), '
                            'button:has-text("Copy"), [aria-label*="opiar"], [title*="opiar"]'
                        ).all()
                        for i, btn_c in enumerate(btn_copiar_list[:3]):
                            try:
                                await btn_c.click()
                                await page.wait_for_timeout(400)
                                clip = await page.evaluate("navigator.clipboard.readText().catch(()=>'')")
                                if clip and clip.startswith("http"):
                                    if i == 0:
                                        resultado["link_assinatura"] = clip
                                    else:
                                        resultado["link_pagamento"] = clip
                            except Exception:
                                pass
                    except Exception:
                        pass

                # ── DRY_RUN: para antes de enviar (modo teste de calibração) ──
                if dry_run:
                    if not premio_extraido:
                        resultado["premio_mensal"] = _extrair_premio_mensal(texto_pg)
                        resultado["premio_anual"]  = _extrair_premio_anual(texto_pg)
                        resultado["detalhes"]      = (
                            "[DRY_RUN] cotação calibrada — proposta NÃO enviada\n\n"
                            + texto_pg[:4500]
                        )
                        premio_extraido = True
                    else:
                        resultado["detalhes"] = (
                            "[DRY_RUN] cotação calibrada — proposta NÃO enviada\n\n"
                            + (resultado.get("detalhes") or "")[:4500]
                        )
                    print(f"[azos][fase2] DRY_RUN — parando ANTES de 'Enviar para assinatura'. "
                          f"premio={resultado.get('premio_mensal')}", flush=True)
                    break

                # Clica "Enviar para assinatura"
                for sel_env in [
                    'button:has-text("Enviar para assinatura")',
                    'button[type="submit"]',
                ]:
                    try:
                        btn_env = page.locator(sel_env).first
                        if await btn_env.count() and await btn_env.is_visible():
                            await btn_env.click()
                            await page.wait_for_timeout(3_000)
                            break
                    except Exception:
                        pass
                urls_visitadas.discard(url_atual)
                continue

            # ── Tela de checkout/pagamento — preenche e avança ────────────
            _e_checkout = (
                "checkout" in url_atual
                or ("contratacao" in url_atual and any(
                    k in url_atual for k in ["pagamento", "payment", "cartao", "boleto"]))
                or any(k in titulo for k in ["pagamento", "payment", "cartão", "cartao",
                                             "boleto", "pagar", "forma de pagamento"])
            )
            if _e_checkout:
                if not premio_extraido:
                    resultado["premio_mensal"] = _extrair_premio_mensal(texto_pg)
                    resultado["premio_anual"]  = _extrair_premio_anual(texto_pg)
                    resultado["detalhes"]      = texto_pg[:5000]
                    premio_extraido = True

                # ── Extrai links de assinatura e pagamento do checkout ────────
                if not resultado.get("link_assinatura") or not resultado.get("link_pagamento"):
                    try:
                        html_ck = await page.content()
                        with open(_TMP / "azos_checkout.html", "w", encoding="utf-8") as _f:
                            _f.write(html_ck)
                        await page.screenshot(path=str(_TMP / "azos_checkout.png"), full_page=True)
                    except Exception:
                        pass

                    try:
                        links_ck = await page.evaluate(r"""() => {
                            const resultado = {assinatura: null, pagamento: null};

                            // Estratégia 1: readonly inputs com URLs
                            for (const inp of document.querySelectorAll('input[readonly]')) {
                                const v = (inp.value || '').trim();
                                if (!v.startsWith('http')) continue;
                                const ctx = (inp.closest('div,section,label,p,li')?.innerText || '').toLowerCase();
                                const isSign = v.includes('clicksign') || ctx.includes('assinatura') || ctx.includes('assinar') || ctx.includes('sign');
                                const isPay  = ctx.includes('pagamento') || ctx.includes('pagar') || ctx.includes('checkout') || ctx.includes('payment');
                                if (!resultado.assinatura && (isSign || (!isPay && !resultado.assinatura)))
                                    resultado.assinatura = v;
                                else if (!resultado.pagamento && (isPay || !resultado.pagamento))
                                    resultado.pagamento = v;
                            }

                            // Estratégia 2: data-link / data-url / data-copy em qualquer elemento
                            if (!resultado.assinatura || !resultado.pagamento) {
                                for (const el of document.querySelectorAll('[data-link],[data-url],[data-href],[data-copy],[data-clipboard-text]')) {
                                    const v = (el.dataset.link || el.dataset.url || el.dataset.href || el.dataset.copy || el.dataset.clipboardText || '').trim();
                                    if (!v.startsWith('http')) continue;
                                    const ctx = (el.closest('div,section,li')?.innerText || '').toLowerCase();
                                    const isSign = v.includes('clicksign') || ctx.includes('assinatura') || ctx.includes('sign');
                                    if (!resultado.assinatura && isSign) resultado.assinatura = v;
                                    else if (!resultado.pagamento && !isSign) resultado.pagamento = v;
                                    else if (!resultado.assinatura) resultado.assinatura = v;
                                    else if (!resultado.pagamento) resultado.pagamento = v;
                                }
                            }

                            // Estratégia 3: links <a href>
                            if (!resultado.assinatura || !resultado.pagamento) {
                                for (const a of document.querySelectorAll('a[href]')) {
                                    const href = (a.href || '').trim();
                                    if (!href.startsWith('http') || href === window.location.href) continue;
                                    const txt = (a.innerText || a.textContent || '').toLowerCase();
                                    const isSign = href.includes('clicksign') || txt.includes('assinatura') || txt.includes('assinar');
                                    const isPay  = txt.includes('pagamento') || txt.includes('pagar') || txt.includes('checkout');
                                    if (!resultado.assinatura && isSign) resultado.assinatura = href;
                                    else if (!resultado.pagamento && isPay) resultado.pagamento = href;
                                }
                            }

                            // Estratégia 4: texto visível de nós folha com URL
                            if (!resultado.assinatura || !resultado.pagamento) {
                                for (const el of document.querySelectorAll('span,p,div,td,li')) {
                                    if (el.children.length > 0) continue;
                                    const v = (el.textContent || '').trim();
                                    if (!v.startsWith('https://') || v.length > 500) continue;
                                    const ctx = (el.closest('div,section,li')?.innerText || '').toLowerCase();
                                    const isSign = v.includes('clicksign') || ctx.includes('assinatura');
                                    const isPay  = ctx.includes('pagamento') || ctx.includes('pagar');
                                    if (!resultado.assinatura && (isSign || (!isPay && !resultado.assinatura))) resultado.assinatura = v;
                                    else if (!resultado.pagamento && (isPay || !resultado.pagamento)) resultado.pagamento = v;
                                }
                            }

                            return resultado;
                        }""")

                        if links_ck.get("assinatura"):
                            resultado["link_assinatura"] = links_ck["assinatura"]
                        if links_ck.get("pagamento"):
                            resultado["link_pagamento"] = links_ck["pagamento"]
                    except Exception:
                        pass

                    # Fallback: clica botões "Copiar" e lê clipboard
                    if not resultado.get("link_assinatura") and not resultado.get("link_pagamento"):
                        try:
                            btns_copiar = await page.locator(
                                'button:has-text("Copiar"), button:has-text("copiar"), '
                                '[aria-label*="opiar" i], [title*="opiar" i]'
                            ).all()
                            for i, btn_c in enumerate(btns_copiar[:4]):
                                try:
                                    await btn_c.click()
                                    await page.wait_for_timeout(400)
                                    clip = await page.evaluate("navigator.clipboard.readText().catch(()=>'')")
                                    if clip and clip.startswith("http"):
                                        if i == 0:
                                            resultado["link_assinatura"] = clip.strip()
                                        else:
                                            resultado["link_pagamento"] = clip.strip()
                                except Exception:
                                    pass
                        except Exception:
                            pass

                if not checkout_preenchido:
                    await _preencher_checkout(page)
                    checkout_preenchido = True
                    urls_visitadas.discard(url_atual)
                    await page.wait_for_timeout(3_000)
                else:
                    avancou = await _clicar_continuar(page)
                    if not avancou:
                        break
                    await page.wait_for_timeout(3_000)
                continue

            # ── Step de riscos de vida OU saúde / DPS ────────────────────
            # IMPORTANTE: verificar DPS ANTES do guard urls_visitadas porque
            # múltiplas perguntas DPS ficam na mesma URL /contratacao/dps
            _e_step_saude = (
                any(k in titulo for k in ["risco", "atividade", "esporte",
                                           "saúde", "saude", "doença", "doenca",
                                           "declaração", "declaracao", "histórico",
                                           "historico", "condição", "condicao",
                                           "dps", "questionário", "questionario"])
                or any(k in url_atual for k in ["risco", "atividade", "saude",
                                                 "declaracao", "dps", "questionario"])
            )
            if _e_step_saude:
                dps_outer_calls += 1
                if dps_outer_calls > 3:
                    print(f"[azos][fase2] DPS travado após {dps_outer_calls} chamadas — forçando avanço", flush=True)
                    await page.screenshot(path=str(_TMP / "azos_dps_stuck.png"), full_page=True)
                    await _clicar_continuar(page)
                    await page.wait_for_timeout(2_000)
                    continue
                await _preencher_dps_completo(page, saude)
                await page.wait_for_timeout(800)
                continue

            # ── Re-seleção se travado na página de coberturas ─────────────
            # Ocorre quando o Continuar inicial falhou (ex: valor fora do range do slider)
            # CRÍTICO: se PHASE 3 rodou (mandatórias calibradas), NÃO chamar
            # _selecionar_cobertura — ele toggle off+on que reseta valores ao
            # default Azos (snap-up). Apenas força click no Continuar.
            if "simulacao/coberturas" in url_atual:
                print(f"[azos][fase2] stuck em coberturas (tentativa={tentativa}) "
                      f"skip_recalibrar={_skip_calibrador}", flush=True)
                # Fecha popup do chat que pode bloquear o botão Continuar
                await _fechar_popup_chat(page)
                await page.wait_for_timeout(400)
                # Só re-seleciona se PHASE 3 NÃO rodou (caso contrário, valores foram
                # calibrados e re-seleção via toggle resetaria tudo)
                if not _skip_calibrador:
                    for sel in selecoes:
                        await _selecionar_cobertura(page, sel["nome"], sel.get("valor", 0))
                    await page.wait_for_timeout(2_500)
                else:
                    print(f"[azos][fase2] PHASE 3 já rodou — pulando re-seleção "
                          f"(evita reset de valores via toggle)", flush=True)
                # DEBUG: lista TODOS os botões visíveis para diagnóstico
                try:
                    todos_btns = await page.locator('button, [role="button"]').all()
                    print(f"[azos][fase2][debug] {len(todos_btns)} botões na página coberturas:", flush=True)
                    for i, b in enumerate(todos_btns[:40]):
                        try:
                            if not await b.is_visible():
                                continue
                            txt = (await b.inner_text())[:50].replace("\n", " ").strip()
                            disab = await b.get_attribute("disabled")
                            aria_d = await b.get_attribute("aria-disabled")
                            if txt:
                                state = "ENABLED" if (disab is None and aria_d != "true") else "DISABLED"
                                print(f"[azos][fase2][debug]   [{i}] {state} '{txt}'", flush=True)
                        except Exception:
                            continue
                except Exception as _db:
                    print(f"[azos][fase2][debug] erro ao listar botões: {_db}", flush=True)
                # Verifica estado dos switches antes de tentar avançar
                switches = await page.locator('button[role="switch"]').all()
                switches_ativos = [await s.get_attribute("aria-checked") for s in switches]
                print(f"[azos][fase2] switches estado: {switches_ativos}", flush=True)
                await page.keyboard.press("End")
                await page.wait_for_timeout(800)
                avancou = await _clicar_continuar(page)
                print(f"[azos][fase2] re-selecao avancou={avancou} url={page.url}", flush=True)

                # Após 2 falhas: força click direto no botão (sem disabled check)
                if not avancou and tentativa >= 2:
                    print(f"[azos][fase2] tentando force-click no Continuar...", flush=True)
                    for txt in ['Ver cotação', 'Continuar', 'Próximo', 'Avançar', 'Calcular']:
                        try:
                            btn = page.locator(f'button:has-text("{txt}")').last
                            if await btn.count():
                                await btn.scroll_into_view_if_needed()
                                await btn.click(force=True)
                                await page.wait_for_timeout(2_000)
                                avancou = page.url != url_atual
                                print(f"[azos][fase2] force-click '{txt}' avancou={avancou}", flush=True)
                                if avancou:
                                    break
                        except Exception:
                            pass

                # Após 3 falhas com PHASE 3: REVERTE valores para iniciais
                # (Azos pode estar bloqueando Continuar por valor inválido/overshoot)
                # Premio cai para o inicial (~R$37) mas conseguimos avançar.
                # Premio < 45 é aceito — usuário prefere proposta enviada a stuck.
                if not avancou and tentativa >= 3 and _skip_calibrador:
                    print(f"[azos][fase2] PHASE 3 stuck — revertendo capitais para iniciais "
                          f"(prefere proposta enviada a stuck)", flush=True)
                    try:
                        inputs_tel = page.locator('input[type="tel"]')
                        n_inp = await inputs_tel.count()
                        for i, s in enumerate(selecoes):
                            if i >= n_inp:
                                break
                            try:
                                inp = inputs_tel.nth(i)
                                # Pega valor INICIAL do recomendador (não o calibrado)
                                v_ini = s.get("valor_inicial") or s["valor"]
                                await inp.fill(str(int(v_ini)))
                                await page.wait_for_timeout(150)
                            except Exception as _fe:
                                print(f"[azos][fase2] fill {i} erro: {_fe}", flush=True)
                        await page.wait_for_timeout(2_000)
                        await page.keyboard.press("End")
                        await page.wait_for_timeout(500)
                        avancou = await _clicar_continuar(page)
                        print(f"[azos][fase2] após revert+continuar avancou={avancou}", flush=True)
                    except Exception as _re:
                        print(f"[azos][fase2] revert erro: {_re}", flush=True)

                # Após 4 falhas: recarrega a página SÓ se PHASE 3 não rodou
                # (caso contrário, reload reseta toda a calibração)
                if not avancou and tentativa >= 4 and not _skip_calibrador:
                    print(f"[azos][fase2] recarregando coberturas (reset React)...", flush=True)
                    await page.goto("https://contratacao.azos.com.br/simulacao/coberturas",
                                    wait_until="domcontentloaded", timeout=20_000)
                    await page.wait_for_timeout(2_000)
                    await page.add_style_tag(content="""
                        [class*='chat'],[class*='copilot'],[class*='widget'],
                        [class*='Chat'],[class*='Copilot'],[class*='Widget'],
                        iframe[src*='chat'],iframe[src*='copilot'] {
                            display:none!important;pointer-events:none!important;
                        }
                    """)
                    # Reseta switches ativos e re-seleciona após reload
                    for sw in await page.locator('button[role="switch"][aria-checked="true"]').all():
                        try:
                            await sw.click()
                        except Exception:
                            pass
                    await page.wait_for_timeout(800)
                    for sel in selecoes:
                        await _selecionar_cobertura(page, sel["nome"], sel.get("valor", 0))
                    await page.wait_for_timeout(2_000)
                    avancou = await _clicar_continuar(page)
                    print(f"[azos][fase2] após reload+reselecao avancou={avancou} url={page.url}", flush=True)
                    await page.wait_for_timeout(3_000)

                # Stuck irreversível: aborta para não consumir o tempo todo
                if not avancou and tentativa >= 6:
                    print(f"[azos][fase2] stuck irreversível — abortando para liberar slot", flush=True)
                    resultado["erro"] = ("Stuck em coberturas — Continuar não habilita "
                                          "(possível agravo Azos imediato)")
                    break

                await page.wait_for_timeout(3_000)
                continue

            # ── Estudo personalizado — extrai prêmio e avança para DPS ──────
            # Página intermediária do Azos: mostra o quote. "Continuar" vai para /contratacao/dps.
            # IMPORTANTE: clicar "Continuar" aqui (não "Fazer cotação" do sidebar).
            if "estudo personalizado" in texto_pg.lower() or \
               "estudo" in url_atual.lower() or \
               "estudo personalizado" in titulo:
                if not premio_extraido:
                    resultado["premio_mensal"] = _extrair_premio_mensal(texto_pg)
                    resultado["premio_anual"]  = _extrair_premio_anual(texto_pg)
                    resultado["detalhes"]      = texto_pg[:5000]
                    premio_extraido = True
                    print(f"[azos][fase2] 'Estudo personalizado': "
                          f"R${resultado.get('premio_mensal', '?')}/mês — avançando para DPS", flush=True)
                await page.screenshot(path=str(_TMP / "azos_estudo_personalizado.png"), full_page=True)
                # Clica "Continuar" bottom-right (last), evita sidebar "Fazer cotação"
                try:
                    btn_cont = page.locator('button:has-text("Continuar")').last
                    if await btn_cont.count() and await btn_cont.is_visible():
                        await btn_cont.click()
                    else:
                        await _clicar_continuar(page)
                except Exception:
                    await _clicar_continuar(page)
                await page.wait_for_timeout(3_000)
                continue

            # ── Evita loop infinito na mesma URL ─────────────────────────
            if url_atual in urls_visitadas:
                # Cadastro tem 3 sub-steps no mesmo URL — nunca faz break aqui
                if "cadastro" in url_atual:
                    cadastro_retries += 1
                    if cadastro_retries > 10:
                        print(f"[azos][fase2] cadastro: max retries atingido", flush=True)
                        break
                    try:
                        cliente_dados = saude.get("_cliente", {})
                        await _preencher_cadastro(page, cliente_dados)
                        await page.wait_for_timeout(1_500)
                    except Exception as _ce:
                        print(f"[azos][fase2] cadastro preencher erro: {_ce}", flush=True)
                    await page.screenshot(path=str(_TMP / f"azos_cadastro_sub_{cadastro_retries:02d}.png"), full_page=True)
                    # Remove da set para permitir que o próximo sub-step entre como visita fresca
                    urls_visitadas.discard(url_atual)
                    await page.keyboard.press("End")
                    await page.wait_for_timeout(500)
                    avancou_cad = await _clicar_continuar(page)
                    print(f"[azos][fase2] cadastro sub {cadastro_retries} avancou={avancou_cad}", flush=True)
                    await page.wait_for_timeout(3_000)
                    continue
                # Outras páginas travadas: tenta rolar + clicar, verifica mudança de URL
                url_antes_stuck = url_atual
                await page.keyboard.press("End")
                await page.wait_for_timeout(500)
                avancou = await _clicar_continuar(page)
                if not avancou:
                    break
                await page.wait_for_timeout(3_000)
                if page.url == url_antes_stuck:
                    # URL não mudou apesar do clique — desiste
                    break
                continue
            urls_visitadas.add(url_atual)

            # ── Voltou para dados-pessoais (sidebar "Fazer cotação" acidentalmente) ──
            # Navega direto para coberturas para retomar o fluxo.
            if "dados-pessoais" in url_atual or "simulacao/dados" in url_atual:
                print(f"[azos][fase2] detectou dados-pessoais — voltando para coberturas", flush=True)
                await page.screenshot(path=str(_TMP / "azos_dados_pessoais_redirect.png"), full_page=True)
                await page.goto("https://contratacao.azos.com.br/simulacao/coberturas",
                                wait_until="domcontentloaded", timeout=20_000)
                await page.wait_for_timeout(2_000)
                urls_visitadas.discard(url_atual)
                continue

            # ── Tela de resultado/cotação: extrai prêmio e continua ───────
            if any(k in titulo for k in ["resultado", "cotação", "cotacao", "proposta",
                                          "resumo", "plano", "prêmio", "premio"]) or \
               any(k in url_atual for k in ["resultado", "cotacao", "proposta", "resumo", "plano"]):
                if not premio_extraido:
                    resultado["premio_mensal"] = _extrair_premio_mensal(texto_pg)
                    resultado["premio_anual"]  = _extrair_premio_anual(texto_pg)
                    resultado["detalhes"]      = texto_pg[:5000]
                    premio_extraido = True
                await _clicar_continuar(page)
                await page.wait_for_timeout(4_000)
                continue

            # ── Step de cadastro ─────────────────────────────────────────
            if "cadastro" in url_atual:
                cliente_dados = saude.get("_cliente", {})
                await _preencher_cadastro(page, cliente_dados)
                await page.wait_for_timeout(1_000)
                avancou = await _clicar_continuar(page)
                if not avancou:
                    await page.keyboard.press("End")
                    await page.wait_for_timeout(500)
                    avancou = await _clicar_continuar(page)
                if avancou:
                    await page.wait_for_timeout(3_000)
                continue

            # ── Step desconhecido: avança sem preencher ───────────────────
            avancou = await _clicar_continuar(page)
            if not avancou:
                # Pode ser que o botão ainda não esteja visível — rola a página
                await page.keyboard.press("End")
                await page.wait_for_timeout(1_000)
                avancou = await _clicar_continuar(page)
                if not avancou:
                    break
            await page.wait_for_timeout(3_000)

        # ── Garante screenshot e texto final ─────────────────────────────
        await page.screenshot(path=str(_TMP / "azos_cotacao_final.png"), full_page=True)
        if not resultado["detalhes"]:
            texto = await page.inner_text("body")
            resultado["detalhes"] = texto[:5000]
        if not premio_extraido:
            texto = await page.inner_text("body")
            resultado["premio_mensal"] = _extrair_premio_mensal(texto)
            resultado["premio_anual"]  = _extrair_premio_anual(texto)

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
        if parar_cotacao:
            # Blend: já leu o prêmio, fecha browser na hora para liberar RAM.
            try: await sessao["browser"].close()
            except Exception: pass
            try: await sessao["pw"].stop()
            except Exception: pass
            _sessoes.pop(session_id, None)
        else:
            # Modo Guardian: mantém browser aberto 10 min para acompanhamento manual.
            import asyncio as _asyncio
            async def _fechar_depois(s):
                await _asyncio.sleep(600)
                try:
                    await s["browser"].close()
                    await s["pw"].stop()
                except Exception:
                    pass
                _sessoes.pop(session_id, None)
            _asyncio.create_task(_fechar_depois(sessao))

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

    # Renda — cuidado: str(50000.0) = "50000.0", .replace(".","") = "500000" (BUG anterior)
    # Quando float, converte para int primeiro pra evitar contaminação do ponto decimal.
    renda_raw = c.get("renda_mensal", "0")
    if isinstance(renda_raw, (int, float)):
        renda = str(int(renda_raw))
    else:
        renda = str(renda_raw).replace(".", "").replace(",", "").replace("R$", "").strip()
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
    """Extrai coberturas da página Azos via Playwright Python (sem evaluate JS)."""
    coberturas = []
    try:
        await page.wait_for_timeout(2_000)
        all_toggles = page.locator('button.min-w-11.min-h-11[data-slot="tooltip-trigger"]')
        n_toggles = await all_toggles.count()
        vistos: set = set()

        for i in range(n_toggles):
            btn = all_toggles.nth(i)
            # Nome: H3 do container via XPath (mesmo padrão de _selecionar_cobertura)
            h3_loc = btn.locator('xpath=ancestor::*[descendant::h3][1]//h3[1]')
            if await h3_loc.count() == 0:
                continue
            nome = (await h3_loc.inner_text()).strip()
            if not nome or len(nome) < 3 or nome in vistos or nome == "Indisponível":
                continue
            vistos.add(nome)

            cls = (await btn.get_attribute("class")) or ""
            ativo = "bg-primary" in cls and "bg-black" not in cls

            # Descrição: primeiro <p> no mesmo container
            desc_loc = btn.locator('xpath=ancestor::*[descendant::h3][1]//p[1]')
            descricao = ""
            if await desc_loc.count() > 0:
                try:
                    descricao = (await desc_loc.first.inner_text()).strip()[:150]
                except Exception:
                    pass

            coberturas.append({
                "nome":      nome[:80],
                "descricao": descricao,
                "ativo":     ativo,
                "valor_max": 5_000_000,
                "valor_min": 1_000,
            })

    except Exception as e:
        coberturas = [{"nome": "Erro ao extrair coberturas", "descricao": str(e),
                       "valor_max": 0, "valor_min": 0, "ativo": False}]
    return coberturas


async def _achar_input_por_h3(page, nome: str):
    """Acha o input[type='tel'] da cobertura por XPath following-input do H3.

    Estratégia: cada cobertura no painel direito tem H3 com nome + input logo
    depois (mesmo card, em ordem DOM). O XPath
    `//h3[contains(text(), "X")]/following::input[@type='tel'][1]`
    pega o PRIMEIRO input após o H3 — que é o input dessa cobertura.

    Fallback: se não achar via following::, itera inputs e procura pelo H3
    do menor ancestral comum (card).
    """
    nome_curto = nome[:25]
    # Estratégia 1: XPath following::input após o H3
    try:
        xpath = f"//h3[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚÂÊÔÃÕÇ', 'abcdefghijklmnopqrstuvwxyzáéíóúâêôãõç'), {repr(nome_curto.lower())[1:-1]!r})]/following::input[@type='tel'][1]"
        # Simpler: just use the original case-insensitive containment
        xpath = f"//h3[contains(., {repr(nome_curto)})]/following::input[@type='tel'][1]"
        loc = page.locator(f"xpath={xpath}")
        if await loc.count():
            cand = loc.first
            if await cand.is_visible():
                return cand
    except Exception:
        pass

    # Estratégia 2: itera inputs, sobe e procura H3 que vem ANTES no DOM
    nome_lower = nome.lower().strip()
    nome_curto_lower = nome_curto.lower().strip()
    all_inputs = page.locator('input[type="tel"]')
    n = await all_inputs.count()
    for i in range(n):
        inp = all_inputs.nth(i)
        try:
            if not await inp.is_visible():
                continue
            # Pega o H3 imediatamente anterior a este input em ordem DOM
            h3_text = await inp.evaluate(r"""el => {
                // Caminha de trás pra frente em document order procurando o H3 mais próximo
                function* prevElements(start) {
                    let cur = start;
                    while (cur) {
                        // Move pra prev sibling, ou se não houver sobe pro pai e segue
                        if (cur.previousElementSibling) {
                            cur = cur.previousElementSibling;
                            // Desce no último filho recursivamente
                            while (cur.lastElementChild) cur = cur.lastElementChild;
                        } else {
                            cur = cur.parentElement;
                            if (!cur) return;
                        }
                        yield cur;
                    }
                }
                let steps = 0;
                for (const prev of prevElements(el)) {
                    if (++steps > 200) return '';
                    if (prev.tagName === 'H3') return prev.innerText;
                    // Se subiu até H3 dentro de descendentes
                    const inner = prev.querySelector ? prev.querySelector('h3') : null;
                    if (inner && prev.tagName !== 'BODY') {
                        // Só retorna se este ancestral também contém o input
                        if (prev.contains(el)) return inner.innerText;
                    }
                }
                return '';
            }""")
            h3_lower = h3_text.lower().strip()
            if not h3_lower:
                continue
            if h3_lower == nome_lower or h3_lower.startswith(nome_curto_lower) or nome_curto_lower in h3_lower:
                return inp
        except Exception:
            continue
    return None


async def _setar_input_com_verificacao(page, inp, valor: int, max_tentativas: int = 8) -> tuple[bool, float]:
    """Seta valor no input com retry + verificação contra snap-up Azos.

    Azos faz snap-up para defaults altos após native setter. Estratégia:
    seta, verifica, retry. Em iters >= 3, tenta remover disabled attribute
    via JS antes de re-setar. Retorna (sucesso, valor_final_no_dom).
    """
    valor_alvo = int(valor)
    valor_final = 0.0
    for tentativa in range(max_tentativas):
        # Em tentativas avançadas, tenta forçar enable do input
        if tentativa >= 3:
            try:
                await inp.evaluate("""el => {
                    if (el.disabled) { el.disabled = false; el.removeAttribute('disabled'); }
                    if (el.readOnly) { el.readOnly = false; el.removeAttribute('readonly'); }
                }""")
            except Exception:
                pass
        # Native setter (bypassa disabled via property setter direto)
        await _setar_input_via_native_setter(inp, valor_alvo)
        await page.wait_for_timeout(400)
        # Lê valor real do DOM e parse pt-BR "R$ 400.000,00"
        try:
            cur = await inp.evaluate("el => el.value || ''")
            clean = (cur.replace('R$', '').replace(' ', '')
                      .replace('.', '').replace(',', '.').strip())
            valor_final = float(clean) if clean else 0.0
            # Tolerância: 1k para capital (R$1.000), 10 para diária
            tol = 10 if valor_alvo < 1000 else 1000
            if abs(valor_final - valor_alvo) <= tol:
                return True, valor_final
        except Exception:
            pass
        await page.wait_for_timeout(600)
    return False, valor_final


async def _setar_input_via_native_setter(inp_locator, valor: int) -> bool:
    """Seta value do input bypassando o React mask handler.

    React-controlled inputs guardam o value no state. fill() / type() passa
    pelo onChange do mask que pode snap-up para defaults. Esta técnica usa
    o setter nativo de HTMLInputElement.prototype.value e dispara input/change
    events sintéticos — o React intercepta esses eventos e atualiza o state
    SEM passar pelo mask.

    Valor é computado em Python e passado aqui como int puro. O setter é
    apenas o mecanismo de transporte para o React.
    """
    try:
        await inp_locator.evaluate(
            r"""(el, val) => {
                const setter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
            }""",
            str(int(valor)),
        )
        return True
    except Exception as e:
        print(f"[azos][cob] native setter erro: {str(e)[:80]}", flush=True)
        return False


async def _incrementar_coberturas_mandatorias(page, selecoes: list, budget_min: float = 47.0,
                                               budget_max: float = 50.0, budget_target: float = 50.0) -> tuple:
    """Protocolo do corretor para coberturas mandatórias:
    1) Assume que TODAS já estão filled com capital mínimo (1k cada, Diária 50)
    2) Lê preço atual
    3) Se preço < budget_min: AUMENTA capital linha por linha até hit [min, max]
    4) Se atingir o max ao incrementar uma linha, volta o último step

    NÃO chama _selecionar_cobertura (que toggle off+on e reseta valores).
    Usa native_setter direto no input[i] correspondente à i-ésima cobertura.

    Retorna (selecoes_finais, premio_final, sucesso).
    """
    # Lê premio inicial
    await page.wait_for_timeout(2_000)
    premio = await _ler_premio_coberturas(page)
    print(f"[azos][inc] início — premio={premio} (alvo {budget_target}, faixa [{budget_min}-{budget_max}])", flush=True)

    if premio is not None and budget_min <= premio <= budget_max:
        print(f"[azos][inc] já dentro da faixa — sem incremento necessário", flush=True)
        return list(selecoes), premio, True

    inputs_tel_initial = page.locator('input[type="tel"]')
    n_inp_initial = await inputs_tel_initial.count()

    # Inicializa ativas ANTES do scale-down (fallback pode mexer nessa lista)
    ativas = []
    for s in selecoes:
        s_copy = dict(s)
        s_copy["valor_inicial"] = s.get("valor_inicial", s["valor"])
        ativas.append(s_copy)

    # ── SCALE-DOWN: premio acima do max → reduz capitais ──────────────────
    # Em prod Azos faz snap-up para defaults ALTOS (Morte R$400k, Invalidez R$1M).
    # Usa _setar_input_com_verificacao (retry 8x + force-enable disabled).
    if premio is not None and premio > budget_max:
        print(f"[azos][inc] premio {premio} > {budget_max} — SCALE-DOWN (com verificação)", flush=True)
        for scale_iter in range(4):
            fator = budget_target / max(premio, 1.0)
            print(f"[azos][inc] scale-down iter {scale_iter} fator={fator:.3f}", flush=True)
            for i, s in enumerate(selecoes):
                if i >= n_inp_initial:
                    break
                inp = inputs_tel_initial.nth(i)
                # Lê valor atual do DOM (Azos formata "R$ 400.000,00" — pt-BR)
                try:
                    val_dom_raw = await inp.evaluate("el => el.value || ''")
                    clean = (val_dom_raw.replace('R$', '').replace(' ', '')
                              .replace('.', '').replace(',', '.').strip())
                    val_dom = float(clean) if clean else 0
                except Exception:
                    val_dom = s["valor"]
                # Diária: floor 50, max 500
                nl = s["nome"].lower()
                if "diária" in nl or "diaria" in nl or "internação" in nl or "internacao" in nl:
                    v_novo = max(50, min(500, int(val_dom * fator)))
                else:
                    v_novo = max(10_000, min(250_000, int(val_dom * fator)))
                # Helper retry+verifica (até 8 tentativas com force-enable disabled)
                ok, val_final = await _setar_input_com_verificacao(page, inp, v_novo, max_tentativas=4)
                marker = "OK" if ok else "AZOS_SNAP_UP"
                s["valor"] = int(val_final) if val_final else v_novo
                print(f"[azos][inc] [{i}] {s['nome'][:25]:25s} {int(val_dom)} → alvo={v_novo} DOM={int(val_final):>6} {marker}", flush=True)
            await page.wait_for_timeout(2_500)
            premio = await _ler_premio_coberturas(page)
            print(f"[azos][inc] após scale-down iter{scale_iter}: premio={premio}", flush=True)
            if premio is None or premio <= budget_max:
                break

        # FALLBACK ESCALONADO: desativa coberturas EM ORDEM (mais caras primeiro)
        # até premio ficar próximo do alvo. Azos faz snap-up de algumas
        # coberturas para defaults R$1M; não dá pra evitar — só desativar.
        DESATIVAR_EM_ORDEM = [
            "morte acidental",
            "invalidez permanente",
            "invalidez total por acidente",
            "seguro de vida",
            "cirurgias",
            "doenças graves 30",
            "doencas graves 30",
            "doenças graves 13",
            "doencas graves 13",
            "rupturas e fraturas",
        ]
        removidas = []
        for nome_alvo in DESATIVAR_EM_ORDEM:
            if premio is None or premio <= budget_max + 5:
                break
            # Acha cobertura ativa com esse nome
            for s in list(selecoes):
                if nome_alvo in s["nome"].lower() and s not in [r for r in removidas]:
                    print(f"[azos][inc] FALLBACK — premio {premio} > {budget_max + 5}, "
                          f"desativando '{s['nome']}'", flush=True)
                    ok_desat = await _desativar_cobertura(page, s["nome"])
                    if ok_desat:
                        removidas.append(s)
                        if s in ativas: ativas.remove(s)
                        if s in selecoes: selecoes.remove(s)
                        await page.wait_for_timeout(2_000)
                        premio = await _ler_premio_coberturas(page)
                        print(f"[azos][inc] após desativar: premio={premio}", flush=True)
                    break
        print(f"[azos][inc] total removidas no fallback: {[r['nome'] for r in removidas]}", flush=True)

        # Se conseguiu reduzir para dentro da faixa, retorna
        if premio is not None and budget_min <= premio <= budget_max:
            print(f"[azos][inc] scale-down hit faixa: {premio}", flush=True)
            return list(selecoes), premio, True

    # Adaptive step: tamanho do incremento varia conforme distância ao alvo.
    # Coberturas baratas (apos_morte) têm taxa ~R$0.10/R$1k — step grande no início
    # converge rápido; step pequeno no fim garante precisão.
    def _eh_diaria(nome: str) -> bool:
        nl = nome.lower()
        return "diária" in nl or "diaria" in nl or "internação" in nl or "internacao" in nl

    def step_para(nome: str, delta: float) -> int:
        """delta = budget_target - premio_atual. Escolhe step com base na distância.
        Steps conservadores em prod — Azos pode snap-up valores e overshoot rapido."""
        if _eh_diaria(nome):
            if delta > 15: return 30
            if delta > 5:  return 15
            return 10
        # Capital: ramp 5k → 2k → 1k (max step 5k pra evitar overshoot em prod)
        if delta > 15: return 5_000
        if delta > 5:  return 2_000
        return 1_000

    # Caps (limites superiores razoáveis pra cada cobertura, evita Azos snap-up)
    def cap_para(nome: str) -> int:
        if _eh_diaria(nome):
            return 500   # diária máx R$500/dia
        # Capital max 250k — em prod Azos faz snap-up perto de valores altos
        # e prêmio overshooting o alvo. Conservador.
        return 250_000

    inputs_tel = page.locator('input[type="tel"]')
    n_inp = await inputs_tel.count()
    print(f"[azos][inc] {n_inp} inputs no painel direito", flush=True)

    # ativas já foi inicializada antes do scale-down. Re-sincroniza com selecoes
    # caso fallback tenha removido coberturas.
    if not ativas or len(ativas) != len(selecoes):
        ativas = []
        for s in selecoes:
            s_copy = dict(s)
            s_copy["valor_inicial"] = s.get("valor_inicial", s["valor"])
            ativas.append(s_copy)

    # Estado: índice da próxima cobertura a incrementar (round-robin)
    idx_atual = 0
    max_iters = 400  # safety cap (subido de 200 pra suportar adaptive steps grandes)
    iters_done = 0

    while iters_done < max_iters:
        iters_done += 1
        if premio is None or premio >= budget_target:
            break

        # Acha próxima cobertura que ainda tem espaço pra crescer
        tentativas_idx = 0
        sel = None
        while tentativas_idx < len(ativas):
            i_try = (idx_atual + tentativas_idx) % len(ativas)
            s_try = ativas[i_try]
            cap = cap_para(s_try["nome"])
            if s_try["valor"] < cap:
                sel = s_try
                i_atual = i_try
                break
            tentativas_idx += 1
        if sel is None:
            print(f"[azos][inc] todas no cap máximo — para aqui", flush=True)
            break

        # Incrementa — step adaptativo conforme distância ao alvo
        delta = budget_target - (premio if premio is not None else 0)
        step = step_para(sel["nome"], delta)
        novo_valor = sel["valor"] + step
        novo_valor = min(novo_valor, cap_para(sel["nome"]))
        # Aplica via native setter no input[i_atual]
        if i_atual >= n_inp:
            print(f"[azos][inc] idx {i_atual} fora dos inputs disponíveis", flush=True)
            break
        inp = inputs_tel.nth(i_atual)
        await _setar_input_via_native_setter(inp, novo_valor)
        await page.wait_for_timeout(600)
        # Lê novo premio
        premio_novo = await _ler_premio_coberturas(page)
        p_str = f"R${premio_novo:.2f}" if premio_novo is not None else "N/A"
        print(f"[azos][inc] +{step:>5} {sel['nome'][:25]:25s} = {novo_valor:>7} → premio={p_str}", flush=True)

        if premio_novo is None:
            await page.wait_for_timeout(1_000)
            premio_novo = await _ler_premio_coberturas(page)

        if premio_novo is not None and premio_novo > budget_max:
            # Ultrapassou o max — volta o último step. Se step era grande, NÃO break:
            # mantém premio anterior e continua loop com step menor (delta menor → step menor).
            print(f"[azos][inc] >  {budget_max} — revertendo step de {step}", flush=True)
            await _setar_input_via_native_setter(inp, sel["valor"])  # volta valor antigo
            await page.wait_for_timeout(600)
            premio = await _ler_premio_coberturas(page)
            if step <= 1_000:
                break  # já no menor step, não tem como afinar mais
            # Round-robin pra próxima cobertura e tenta com step menor (delta ficou menor)
            idx_atual = (i_atual + 1) % len(ativas)
            continue

        sel["valor"] = novo_valor
        premio = premio_novo

        if premio is not None and budget_min <= premio <= budget_max:
            print(f"[azos][inc] hit faixa! premio={premio:.2f}", flush=True)
            break

        # Round-robin: avança índice pra distribuir os incrementos
        idx_atual = (i_atual + 1) % len(ativas)

    print(f"[azos][inc] FIM premio={premio} ({iters_done} iters)", flush=True)
    sucesso = premio is not None and budget_min <= premio <= budget_max
    return ativas, premio, sucesso


async def _reduz_todos_sliders_min(page) -> int:
    """Pressiona Home em todos os [role='slider'] visíveis (vai ao MIN).

    Esta é a forma confiável de reduzir o capital segurado no Azos:
    o React calcula premio pelo state do slider (Radix UI), não pelo
    .value do input. Setar input.value via fill() não propaga ao slider.
    Home = MIN do slider.
    """
    try:
        sliders = await page.locator('[role="slider"]').all()
    except Exception:
        sliders = []
    count = 0
    for s in sliders:
        try:
            if not await s.is_visible():
                continue
            await s.scroll_into_view_if_needed()
            await s.focus()
            await page.wait_for_timeout(120)
            await page.keyboard.press("Home")
            await page.wait_for_timeout(250)
            count += 1
        except Exception:
            continue
    print(f"[azos][sliders] {count} sliders → MIN (Home)", flush=True)
    return count


async def _desativar_cobertura(page, nome: str) -> bool:
    """Clica toggle UMA VEZ pra desativar (se ativa). Retorna True se foi desativada."""
    nome_curto = nome[:30]
    print(f"[azos][cob] desativando '{nome_curto}'", flush=True)
    try:
        nome_lower = nome.lower()
        all_toggles = page.locator('button.min-w-11.min-h-11[data-slot="tooltip-trigger"]')
        n_toggles = await all_toggles.count()
        toggle = None
        for i in range(n_toggles):
            btn = all_toggles.nth(i)
            h3_loc = btn.locator('xpath=ancestor::*[descendant::h3][1]//h3[1]')
            if await h3_loc.count() == 0:
                continue
            h3_text = (await h3_loc.inner_text()).strip().lower()
            if h3_text == nome_lower or nome_curto.lower() in h3_text:
                toggle = btn
                break
        if not toggle:
            print(f"[azos][cob] toggle não encontrado pra desativar: '{nome_curto}'", flush=True)
            return False
        cls = await toggle.get_attribute("class") or ""
        is_selected = "bg-primary" in cls and "bg-black" not in cls
        if not is_selected:
            print(f"[azos][cob] '{nome_curto}' já estava desativada", flush=True)
            return True
        await toggle.scroll_into_view_if_needed()
        await page.wait_for_timeout(200)
        await toggle.click()
        await page.wait_for_timeout(800)
        cls2 = await toggle.get_attribute("class") or ""
        agora_desativada = "bg-black" in cls2 and "bg-primary" not in cls2
        print(f"[azos][cob] '{nome_curto}' desativada={agora_desativada}", flush=True)
        return agora_desativada
    except Exception as e:
        print(f"[azos][cob] erro desativar '{nome_curto}': {e}", flush=True)
        return False


async def _selecionar_cobertura(page, nome: str, valor: float):
    """Ativa a cobertura (novo UI: botão + bg-black) e define o valor via fill()."""
    nome_curto = nome[:30]
    print(f"[azos][cob] selecionando '{nome_curto}' valor={valor}", flush=True)
    try:
        # ── 1. Localiza o botão toggle no painel esquerdo ────────────────────
        # Itera todos os toggles e lê o H3 do container via XPath puro (sem evaluate JS).
        # XPath ancestor::*[descendant::h3][1] sobe até o ancestral mais próximo que
        # contenha um H3 — que é exatamente o card/row de cada cobertura.
        toggle = None
        nome_lower = nome.lower()
        nome_curto_lower = nome_curto.lower()
        all_toggles = page.locator('button.min-w-11.min-h-11[data-slot="tooltip-trigger"]')
        n_toggles = await all_toggles.count()
        tentative = None
        for i in range(n_toggles):
            btn = all_toggles.nth(i)
            h3_loc = btn.locator('xpath=ancestor::*[descendant::h3][1]//h3[1]')
            if await h3_loc.count() == 0:
                continue
            h3_text = (await h3_loc.inner_text()).strip()
            h3_lower = h3_text.lower()
            if h3_lower == nome_lower:
                toggle = btn
                break
            if nome_curto_lower in h3_lower and tentative is None:
                tentative = btn

        if toggle is None:
            toggle = tentative

        if not toggle:
            print(f"[azos][cob] toggle nao encontrado: '{nome_curto}' (n_toggles={n_toggles})", flush=True)
            return

        print(f"[azos][cob] toggle encontrado para '{nome_curto}'", flush=True)

        await toggle.scroll_into_view_if_needed()
        await page.wait_for_timeout(400)

        # Verifica se já está selecionado
        cls = await toggle.get_attribute("class") or ""
        aria_checked = await toggle.get_attribute("aria-checked")
        is_selected = ("bg-primary" in cls and "bg-black" not in cls) or aria_checked == "true"
        print(f"[azos][cob] is_selected={is_selected} cls_hint={'bg-primary' if 'bg-primary' in cls else 'bg-black'}", flush=True)

        # Toggle off+on quando já selecionada — necessário para o React aceitar
        # novo valor via fill(). Sem isso, o React mantém o valor antigo e o
        # premium não recalcula. O reset transitório para default é compensado
        # pela fill() que segue logo após.
        if is_selected:
            await toggle.click()           # desseleciona
            await page.wait_for_timeout(600)
            await toggle.click()           # resseleciona
            await page.wait_for_timeout(1_500)
        else:
            await toggle.click()
            await page.wait_for_timeout(1_500)
            cls = await toggle.get_attribute("class") or ""
            is_selected = ("bg-primary" in cls and "bg-black" not in cls)
            print(f"[azos][cob] apos click is_selected={is_selected}", flush=True)
            if not is_selected:
                bbox = await toggle.bounding_box()
                if bbox:
                    await page.mouse.click(bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2)
                    await page.wait_for_timeout(1_000)

        if valor <= 0:
            return

        # ── 2. Acha input específico da cobertura via H3 ancestral ─────────
        # CRÍTICO: o método correto é subir do input até o H3 mais próximo,
        # NÃO descer do H3 via CSS :has (que captura ancestrais errados quando
        # múltiplas cards estão visíveis no painel direito — o bug dava todos
        # os fills no input errado, causando premio R$164 ao invés de R$49).
        inp = await _achar_input_por_h3(page, nome)

        if not inp:
            print(f"[azos][cob] input nao encontrado: '{nome_curto}'", flush=True)
            return

        try:
            await inp.scroll_into_view_if_needed()
            await page.wait_for_timeout(200)
        except Exception:
            pass

        # ── 3. Aguarda input ficar habilitado (rápido — 2s só) ────────────────
        try:
            await inp.wait_for(state="enabled", timeout=2_000)
        except Exception:
            pass

        # ── 4. Define valor via NATIVE SETTER do HTMLInputElement.prototype ──
        # IMPORTANTE: fill() / press_sequentially() / type() passam pelo mask
        # handler do React que SNAPA o valor para defaults (ex: REF 35k → 100k).
        # Solução: usar Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,
        # 'value').set.call(inp, val) — isso bypassa o mask e dispara onChange
        # nativo. O React detecta o evento e atualiza state SEM passar pelo mask.
        #
        # Valor é COMPUTADO em Python (recomendador via _TAXAS). O JS é apenas
        # o mecanismo de set; toda lógica de cálculo está em Python.
        val_int = int(valor)
        ok_set  = await _setar_input_via_native_setter(inp, val_int)

        if ok_set:
            await page.wait_for_timeout(500)
            # Verifica: extrai apenas dígitos do DOM e compara
            try:
                cur = await inp.input_value(timeout=2_000)
                digits_cur = "".join(c for c in cur if c.isdigit())
                digits_exp = str(val_int)
                if digits_cur == digits_exp or digits_cur == digits_exp + "00":
                    print(f"[azos][cob] valor OK (native setter): {val_int} (DOM={cur})", flush=True)
                else:
                    print(f"[azos][cob] valor após native setter: DOM={cur!r} esperava={val_int}", flush=True)
            except Exception:
                print(f"[azos][cob] valor setado via native setter: {val_int}", flush=True)
        else:
            # Fallback: fill() tradicional caso o native setter falhe
            try:
                await inp.fill(str(val_int), timeout=2_000)
                await page.wait_for_timeout(400)
                print(f"[azos][cob] fallback fill: {val_int}", flush=True)
            except Exception as _fe:
                print(f"[azos][cob] ERRO fallback fill: {str(_fe)[:100]}", flush=True)

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
    for iteracao in range(20):
        await page.wait_for_timeout(400)

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

        # Tipo D: pergunta de motocicleta (responde conforme dado do cliente)
        e_moto = any(k in texto_pagina for k in [
            "motocicleta", "motociclismo", "moto ", "utiliza moto",
            "anda de moto", "pilota moto", "conduz moto",
        ])
        anda_de_moto = saude.get("anda_de_moto", False)

        async def _selecionar_pelo_tipo():
            if e_vinculo_prof:
                ok = await _selecionar_vinculo_profissional(page)
                if ok:
                    return True
                # Fallback: e_vinculo_prof pode ter sido disparado por "profissão envolve
                # atividades manuais" (que tem botões Sim./Não.) — tenta Não.
                return await _selecionar_nao_exato(page)
            elif tem_nenhum_desses:
                ok = await _clicar_nenhum_desses(page)
                if ok:
                    return True
                return await _selecionar_nao_exato(page)
            elif e_moto:
                if anda_de_moto:
                    return await _selecionar_sim_exato(page)
                else:
                    return await _selecionar_nao_exato(page)
            elif e_estilo_vida:
                ok = await _responder_estilo_vida(page)
                if ok:
                    return True
                return await _selecionar_nao_exato(page)
            else:
                return await _selecionar_nao_exato(page)

        selecionou = await _selecionar_pelo_tipo()

        print(f"[azos][dps] iteracao={iteracao} selecionou={selecionou} vinculo={e_vinculo_prof} nenhum={tem_nenhum_desses} moto={e_moto}({anda_de_moto}) estilo={e_estilo_vida}", flush=True)
        if not selecionou:
            await page.screenshot(path=str(_TMP / f"azos_debug_dps_stuck_{iteracao:02d}.png"), full_page=True)

        # Scroll até o fim e aguarda o Radix UI processar o click
        await page.keyboard.press("End")
        await page.wait_for_timeout(600)

        avancou = await _clicar_continuar(page)
        print(f"[azos][dps] iteracao={iteracao} avancou={avancou}", flush=True)
        if not avancou:
            # Re-seleciona: Radix pode não ter reagido ao primeiro click
            print(f"[azos][dps] iteracao={iteracao} re-selecionando e aguardando...", flush=True)
            await _selecionar_pelo_tipo()
            await page.wait_for_timeout(800)
            avancou = await _clicar_continuar(page)
            if not avancou:
                print(f"[azos][dps] iteracao={iteracao} nao avançou após 2 tentativas", flush=True)
                await page.screenshot(path=str(_TMP / f"azos_debug_dps_fail_{iteracao:02d}.png"), full_page=True)

        await page.wait_for_timeout(600)

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
        # CSS: esconde qualquer elemento fixo que contenha "copiloto" ou "Olá"
        try:
            await page.add_style_tag(content="""
                [class*='chat'],[class*='copilot'],[class*='widget'],
                [class*='Chat'],[class*='Copilot'],[class*='Widget'],
                iframe[src*='chat'],iframe[src*='copilot'] {
                    display:none!important;pointer-events:none!important;
                }
            """)
        except Exception:
            pass
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

    # ── 3. Último recurso: qualquer checkbox visível ─────────────────────
    try:
        for cb in await page.locator('button[role="checkbox"]').all():
            try:
                if await cb.is_visible():
                    await cb.scroll_into_view_if_needed()
                    await cb.click(force=True)
                    await page.wait_for_timeout(400)
                    return True
            except Exception:
                pass
    except Exception:
        pass

    return False


async def _selecionar_sim_exato(page) -> bool:
    """Seleciona 'Sim.' em telas do Azos (botão ou radio)."""
    await _fechar_popup_chat(page)
    await page.wait_for_timeout(200)

    # ── 0. Locator por texto DOM direto (mais confiável para plain buttons) ──
    for txt in ["Sim.", "Sim"]:
        try:
            btn = page.locator("button").filter(has_text=txt)
            if await btn.count():
                await btn.first.scroll_into_view_if_needed()
                await btn.first.click()
                await page.wait_for_timeout(300)
                return True
        except Exception:
            pass

    # ── 1. get_by_role button (accessibility name) ──
    for txt in ["Sim.", "Sim"]:
        try:
            btn = page.get_by_role("button", name=txt, exact=True)
            if await btn.count():
                await btn.first.scroll_into_view_if_needed()
                await btn.first.click()
                await page.wait_for_timeout(300)
                return True
        except Exception:
            pass

    # ── 2. get_by_role radio ──────────────────────────────────────────────
    for nome in ["Sim.", "Sim"]:
        try:
            radio = page.get_by_role("radio", name=nome, exact=True)
            if await radio.count():
                await radio.first.scroll_into_view_if_needed()
                await radio.first.click(force=True)
                await page.wait_for_timeout(500)
                return True
        except Exception:
            pass

    # ── 3. Label com button[role="radio"] ────────────────────────────────
    try:
        labels = await page.locator("label").all()
        for lbl in labels:
            try:
                txt = (await lbl.inner_text()).strip().lower()
            except Exception:
                continue
            if txt not in {"sim.", "sim"} and not txt.startswith("sim"):
                continue
            btn_radio = lbl.locator('button[role="radio"]').first
            if await btn_radio.count():
                await btn_radio.scroll_into_view_if_needed()
                await btn_radio.click(force=True)
                await page.wait_for_timeout(500)
                return True
            await lbl.scroll_into_view_if_needed()
            await lbl.click(force=True)
            await page.wait_for_timeout(500)
            return True
    except Exception:
        pass

    return False


async def _selecionar_nao_exato(page) -> bool:
    """
    Seleciona 'Não.' em telas do Azos (botão simples ou Radix radio).
    Ordem: filter has_text → get_by_role → label+button → radio genérico → JS.
    """
    await _fechar_popup_chat(page)
    await page.wait_for_timeout(200)

    NAO_EXATOS = {"não.", "não", "nao.", "nao"}

    # ── 0. Locator por texto DOM direto (mais confiável para plain buttons) ──
    for txt in ["Não.", "Não"]:
        try:
            btn = page.locator("button").filter(has_text=txt)
            cnt = await btn.count()
            if cnt:
                alvo = btn.last if cnt > 1 else btn.first
                await alvo.scroll_into_view_if_needed()
                await alvo.click()
                await page.wait_for_timeout(300)
                return True
        except Exception:
            pass

    # ── 1. get_by_role button (accessibility name) ──
    for txt in ["Não.", "Não"]:
        try:
            btn = page.get_by_role("button", name=txt, exact=True)
            if await btn.count():
                await btn.last.scroll_into_view_if_needed()
                await btn.last.click()
                await page.wait_for_timeout(300)
                return True
        except Exception:
            pass

    # ── 2. get_by_role radio (Radix UI) ──────────────────────────────────
    for nome in ["Não.", "Não"]:
        try:
            radio = page.get_by_role("radio", name=nome, exact=True)
            if await radio.count():
                await radio.last.scroll_into_view_if_needed()
                await radio.last.click(force=True)
                await page.wait_for_timeout(500)
                return True
        except Exception:
            pass

    # ── 3. Label com button[role="radio"] dentro (Pattern Radix) ──────────
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
                await page.wait_for_timeout(500)
                return True
            await lbl.scroll_into_view_if_needed()
            await lbl.click(force=True)
            await page.wait_for_timeout(500)
            return True
    except Exception:
        pass

    # ── 4. Último button[role="radio"] do radiogroup ──────────────────────
    try:
        btns_grupo = await page.locator('div[role="radiogroup"] button[role="radio"]').all()
        if btns_grupo:
            ultimo = btns_grupo[-1]
            await ultimo.scroll_into_view_if_needed()
            await ultimo.click(force=True)
            await page.wait_for_timeout(500)
            return True
    except Exception:
        pass

    # ── 5. Último [role="radio"] genérico ────────────────────────────────
    try:
        radios = await page.locator('input[type="radio"], [role="radio"]').all()
        if radios:
            await radios[-1].click(force=True)
            await page.wait_for_timeout(500)
            return True
    except Exception:
        pass

    # ── 5. Último recurso: qualquer botão visível que contenha "não" ────────
    try:
        for btn in await page.locator("button").all():
            try:
                txt = (await btn.inner_text()).strip().lower()
                if txt in ("não.", "não", "nao.", "nao") and await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(force=True)
                    await page.wait_for_timeout(500)
                    return True
            except Exception:
                pass
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

    # ── 1. Continuar dentro do dialog ──────────────────────────────────────
    for dialog_sel in ['[role="dialog"]', '[role="alertdialog"]', '[data-radix-dialog-content]', '[data-state="open"]']:
        try:
            modal = page.locator(dialog_sel).first
            if not await modal.count() or not await modal.is_visible():
                continue
            btn = modal.locator("button").filter(has_text="Continuar").last
            if not await btn.count():
                btn = modal.locator("button").last
            if await btn.count():
                await btn.scroll_into_view_if_needed()
                await btn.click(force=True)
                await page.wait_for_timeout(1_200)
                if not _modal_ainda_aberto(await page.inner_text("body")):
                    return True
            break
        except Exception:
            pass

    # ── 2. Qualquer botão Continuar na página ──────────────────────────────
    try:
        btn = page.locator("button").filter(has_text="Continuar").last
        if await btn.count():
            await btn.scroll_into_view_if_needed()
            await btn.click(force=True)
            await page.wait_for_timeout(1_200)
            if not _modal_ainda_aberto(await page.inner_text("body")):
                return True
    except Exception:
        pass

    # ── 3. Teclado: Tab + Enter ────────────────────────────────────────────
    try:
        for _ in range(3):
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(150)
            focused = page.locator(":focus")
            if await focused.count():
                txt = (await focused.inner_text()).lower()
                if "continuar" in txt:
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(1_200)
                    if not _modal_ainda_aberto(await page.inner_text("body")):
                        return True
                    break
    except Exception:
        pass

    # ── 4. CSS hide backdrop + force click ─────────────────────────────────
    try:
        await page.add_style_tag(content="""
            [data-radix-dialog-overlay],[data-overlay],
            [class*="backdrop"],[class*="overlay"] {
                pointer-events:none!important;display:none!important;
            }
        """)
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

    # ── 5. Nuclear: Escape e assume fechado ────────────────────────────────
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(600)
        return True
    except Exception:
        pass

    return False


async def _preencher_dados_segurado(page, cliente: dict):
    """Sub-step 1 de /contratacao/cadastro: preenche 'Dados da pessoa segurada'."""
    await page.wait_for_timeout(500)

    # Valores a preencher
    nome = str(cliente.get("nome", "")).strip()
    nasc_raw = str(cliente.get("nascimento", "")).replace("/", "").replace("-", "").strip()
    try:
        alt_raw = str(cliente.get("altura", "175")).strip()
        alt_m = alt_raw.replace(",", ".") if ("." in alt_raw or "," in alt_raw) else f"{int(alt_raw) / 100:.2f}"
    except Exception:
        alt_m = "1.75"
    try:
        peso_val = str(int(float(str(cliente.get("peso", "80")).replace(",", "."))))
    except Exception:
        peso_val = "80"
    profissao = str(cliente.get("profissao", "Empresário")).strip()
    try:
        renda = str(int(float(str(cliente.get("renda_mensal", 5000)).replace(",", "."))))
    except Exception:
        renda = "5000"

    async def _type_into(inp, value, clear=True):
        """Clica, limpa e digita value via type() para acionar eventos React."""
        try:
            await inp.scroll_into_view_if_needed()
            await inp.click(force=True)
            await page.wait_for_timeout(100)
            if clear:
                await inp.fill("")
                await page.wait_for_timeout(50)
            await inp.type(value, delay=40)
            await page.wait_for_timeout(200)
            return True
        except Exception:
            return False

    async def _fill_field(names, placeholders, label_texts, value, clear=True):
        """Tenta preencher campo por name → get_by_label → get_by_placeholder → positional."""
        # 1. Por name attribute
        for sel in names:
            try:
                inp = page.locator(sel).first
                if await inp.count() > 0:
                    if await _type_into(inp, value, clear):
                        return True
            except Exception:
                pass
        # 2. Por get_by_label
        for lbl in label_texts:
            try:
                inp = page.get_by_label(lbl, exact=False)
                if await inp.count() > 0:
                    if await _type_into(inp.first, value, clear):
                        return True
            except Exception:
                pass
        # 3. Por placeholder
        for ph in placeholders:
            try:
                inp = page.get_by_placeholder(ph)
                if await inp.count() > 0:
                    if await _type_into(inp.first, value, clear):
                        return True
            except Exception:
                pass
        return False

    # ── Nome completo ─────────────────────────────────────────────────────────
    if nome:
        await _fill_field(
            names=['input[name="fullName"]', 'input[name="full_name"]',
                   'input[name="name"]', 'input[name="insuredName"]',
                   'input[name="insured_name"]', 'input[name="completeName"]'],
            placeholders=["Digite aqui"],
            label_texts=["Nome completo", "Nome"],
            value=nome,
        )

    # ── Data de nascimento ────────────────────────────────────────────────────
    if nasc_raw:
        await _fill_field(
            names=['input[name="birthDate"]', 'input[name="birth_date"]',
                   'input[name="dateOfBirth"]', 'input[name="insuredBirthDate"]'],
            placeholders=["dd/mm/aaaa"],
            label_texts=["Data de nascimento", "Nascimento"],
            value=nasc_raw,
        )

    # ── Altura ────────────────────────────────────────────────────────────────
    await _fill_field(
        names=['input[name="height"]', 'input[name="altura"]', 'input[name="insuredHeight"]'],
        placeholders=["0.00"],
        label_texts=["Altura"],
        value=alt_m,
    )

    # ── Peso ──────────────────────────────────────────────────────────────────
    await _fill_field(
        names=['input[name="weight"]', 'input[name="peso"]', 'input[name="insuredWeight"]'],
        placeholders=["00.0", "0.0"],
        label_texts=["Peso"],
        value=peso_val,
    )

    # ── Profissão — tenta dialog primeiro, depois text input ─────────────────
    try:
        btn_prof = page.locator('button[name="professionId"]').first
        if await btn_prof.count() > 0 and await btn_prof.is_visible():
            await btn_prof.click()
            await page.wait_for_timeout(1_200)
            search = page.locator('[role="dialog"] input').first
            await search.type(profissao[:6], delay=80)
            await page.wait_for_timeout(1_500)
            await page.locator('[role="dialog"]').locator('button, li, [role="option"]').first.click()
            await page.wait_for_timeout(800)
        else:
            await _fill_field(
                names=['input[name="occupation"]', 'input[name="profession"]',
                       'input[name="profissao"]', 'input[name="job"]'],
                placeholders=[],
                label_texts=["Profissão", "Profissao exercida", "Profissão exercida"],
                value=profissao,
            )
    except Exception:
        pass

    # ── Renda mensal ──────────────────────────────────────────────────────────
    await _fill_field(
        names=['input[name="monthlyIncome"]', 'input[name="monthly_income"]',
               'input[name="income"]', 'input[name="renda"]'],
        placeholders=["R$", "0,00"],
        label_texts=["Renda mensal", "Renda"],
        value=renda,
    )

    await page.wait_for_timeout(300)

    # ── Sexo ──────────────────────────────────────────────────────────────────
    sexo = str(cliente.get("sexo", "M")).upper()
    try:
        keyword = "masculino" if sexo != "F" else "feminino"
        locs = [
            page.locator(f'label:has-text("{keyword}")'),
            page.get_by_role("radio", name=f"Sexo {keyword}"),
            page.locator('button[role="radio"]').filter(has_text=keyword),
        ]
        for loc in locs:
            if await loc.count():
                await loc.first.scroll_into_view_if_needed()
                await loc.first.click(force=True)
                await page.wait_for_timeout(300)
                break
    except Exception:
        pass

    # ── Fumante ───────────────────────────────────────────────────────────────
    fumante = cliente.get("fumante", False)
    alvo = "Sim" if fumante else "Não"
    try:
        labels = await page.locator('label').all()
        for lb in labels:
            if (await lb.inner_text()).strip() == alvo:
                await lb.click(); break
        else:
            radio = page.get_by_role("radio", name=alvo, exact=True)
            if await radio.count():
                await radio.last.click(force=True)
        await page.wait_for_timeout(300)
    except Exception:
        pass

    await page.wait_for_timeout(500)
    await page.screenshot(path=str(_TMP / "azos_debug_cadastro_dados.png"), full_page=True)


async def _preencher_cadastro(page, cliente: dict):
    """
    Preenche a tela /contratacao/cadastro (novo UI 2025).
    Sub-step 1: Dados da pessoa segurada (nome, nascimento, altura, peso, profissão, renda, sexo, fumante)
    Sub-step 2: CPF / contato / estado civil / PEP
    Sub-step 3: Endereço (CEP)
    """
    await page.wait_for_timeout(500)
    corpo = await page.inner_text("body")
    corpo_lower = corpo.lower()

    # ── Sub-step 3: Endereço (prioridade máxima — CEP input visível) ────────────
    try:
        cep_inp3 = page.locator('input[name="cep"]').first
        if await cep_inp3.count() > 0 and await cep_inp3.is_visible():
            await _preencher_endereco(page, cliente)
            return
    except Exception:
        pass

    # ── Sub-step 1: "Dados da pessoa segurada" ───────────────────────────────
    # Detecta por: (a) campos de física visíveis ou (b) texto único do step 1
    _is_step1 = False
    try:
        _h = page.locator('input[name="height"], input[name="weight"], input[name="altura"], input[name="peso"]').first
        if await _h.count() > 0 and await _h.is_visible():
            _is_step1 = True
    except Exception:
        pass
    if not _is_step1:
        _is_step1 = any(k in corpo_lower for k in [
            "profissão exercida atualmente", "profissao exercida atualmente",
            "renda mensal declarada individual",
            "fumante",
        ])
    if _is_step1:
        await _preencher_dados_segurado(page, cliente)
        return

    # ── Sub-step 2: CPF / contato / estado civil / PEP ───────────────────────
    # CPF — input[name="cpf"] type="tel"
    cpf = str(cliente.get("cpf", "")).replace(".", "").replace("-", "").strip()
    if cpf:
        try:
            inp = page.locator('input[name="cpf"]').first
            if await inp.count() and await inp.is_visible():
                await inp.click()
                await inp.fill("")
                await inp.type(cpf, delay=40)
                # Tab dispara blur → aguarda modal "CPF vinculado" (pode levar até 4s)
                await inp.press("Tab")
                for _ in range(8):
                    await page.wait_for_timeout(500)
                    corpo_check = (await page.inner_text("body")).lower()
                    if "vinculado ao e-mail" in corpo_check or "esse cpf" in corpo_check:
                        break
                await _fechar_modal_cpf_vinculado(page)
                await page.wait_for_timeout(500)
        except Exception:
            pass

    # Telefone — input[name="phone"]
    telefone = str(cliente.get("telefone", "")).replace("(", "").replace(")", "").replace(" ", "").replace("-", "").replace("+", "")
    if telefone.startswith("55") and len(telefone) in (11, 12, 13):
        candidate = telefone[2:]
        if len(candidate) in (9, 10, 11):
            telefone = candidate
    if len(telefone) == 10 and telefone[2] != "9":
        telefone = telefone[:2] + "9" + telefone[2:]
    if telefone:
        for sel in ['input[name="phone"]', 'input[name="celular"]',
                    'input[name="phoneNumber"]', 'input[name="mobile"]']:
            try:
                inp = page.locator(sel).first
                if await inp.count() and await inp.is_visible():
                    await inp.type(telefone, delay=40)
                    await page.wait_for_timeout(400)
                    break
            except Exception:
                pass

    # Email — input[name="email"]
    email = str(cliente.get("email", "")).strip()
    if email:
        try:
            inp = page.locator('input[name="email"]').first
            if await inp.count() and await inp.is_visible():
                await inp.fill(email)
                await page.wait_for_timeout(400)
        except Exception:
            pass

    # Estado civil — Opções: "Solteira/o", "Casada/o", "Viúva/o", "Divorciada/o", "União estável"
    # UI 2025: buttons sem role="radio" (5 botões outlined em uma linha).
    # Estratégia: tenta múltiplos seletores + clica por texto exato + verifica
    # via classe selecionada (bg-primary / aria-pressed / aria-checked).
    estado = str(cliente.get("estado_civil", "Solteira/o"))
    estado_lower = estado.lower()
    ec_selecionado = False
    for _attempt in range(3):
        try:
            await page.keyboard.press("End")
            await page.wait_for_timeout(200)

            # Lista candidatos pela ordem: role=radio, role=button accessible name, plain button
            candidatos = []
            for loc in [
                page.locator('button[role="radio"]').filter(has_text=estado),
                page.get_by_role("radio", name=estado, exact=True),
                page.get_by_role("button", name=estado, exact=True),
                page.locator('button').filter(has_text=estado),
                page.locator('[role="button"]').filter(has_text=estado),
                page.locator('label').filter(has_text=estado),
            ]:
                try:
                    n = await loc.count()
                    for i in range(n):
                        candidatos.append(loc.nth(i))
                except Exception:
                    pass

            for c in candidatos:
                try:
                    if not await c.is_visible():
                        continue
                    txt = (await c.inner_text()).strip().lower()
                    # Match exato ou início (evita "Solteira/o" pegar texto de "Solteira/o ou viúva")
                    if txt != estado_lower and not txt.startswith(estado_lower):
                        continue
                    await c.scroll_into_view_if_needed()
                    await c.click(force=True, timeout=3_000)
                    await page.wait_for_timeout(500)
                    # Verifica se ficou selecionado
                    cls = (await c.get_attribute("class")) or ""
                    aria_p = await c.get_attribute("aria-pressed")
                    aria_c = await c.get_attribute("aria-checked")
                    if ("bg-primary" in cls or aria_p == "true" or aria_c == "true"):
                        ec_selecionado = True
                        print(f"[azos][cad] estado_civil selecionado: {estado}", flush=True)
                        break
                    # Se não confirmou estado, tenta o body text para ver se "O estado civil é obrigatório" sumiu
                    body_l = (await page.inner_text("body")).lower()
                    if "estado civil é obrigatório" not in body_l and "estado civil e obrigatorio" not in body_l:
                        ec_selecionado = True
                        print(f"[azos][cad] estado_civil click sem aria/bg mas erro sumiu: {estado}", flush=True)
                        break
                except Exception as _ec_e:
                    print(f"[azos][cad] estado_civil click erro: {str(_ec_e)[:80]}", flush=True)
                    continue
            if ec_selecionado:
                break
            print(f"[azos][cad] estado_civil retry {_attempt+1} — nao confirmou seleção", flush=True)
            await page.wait_for_timeout(500)
        except Exception:
            pass

    # PEP (is_politically_exposed_person) → Não (value="false")
    # UI 2025: o "Não" pode ser um label/button com texto "Não", e os círculos
    # à esquerda são os indicadores visuais. Estratégia múltipla com verificação
    # do texto de erro "Precisamos dessa informação..." que some quando OK.
    pep_selecionado = False
    for _pep_attempt in range(3):
        try:
            await page.keyboard.press("End")
            await page.wait_for_timeout(300)

            estrategias = [
                ('input radio name', page.locator('input[type="radio"][name="is_politically_exposed_person"][value="false"]').first),
                ('input radio value=false', page.locator('input[type="radio"][value="false"]').last),
                ('role=radio Não', page.get_by_role("radio", name="Não", exact=True).last),
                ('label Não', page.locator('label').filter(has_text="Não").last),
                ('button Não exact', page.get_by_role("button", name="Não", exact=True).last),
                ('button has-text Não last', page.locator('button:has-text("Não")').last),
                ('span Não last', page.locator('span:has-text("Não")').last),
            ]
            for label, loc in estrategias:
                try:
                    if not await loc.count():
                        continue
                    if not await loc.is_visible():
                        continue
                    await loc.scroll_into_view_if_needed()
                    await loc.click(force=True, timeout=2_500)
                    await page.wait_for_timeout(500)
                    # Verifica se a mensagem de erro PEP sumiu
                    body_l = (await page.inner_text("body")).lower()
                    if "precisamos dessa informação" not in body_l and "precisamos dessa informacao" not in body_l:
                        pep_selecionado = True
                        print(f"[azos][cad] PEP selecionado via {label}", flush=True)
                        break
                except Exception as _e:
                    print(f"[azos][cad] PEP {label} erro: {str(_e)[:80]}", flush=True)
                    continue
            if pep_selecionado:
                break

            # Fallback: clica todos os button[role="radio"] vazios (círculos) — último = Não
            all_radios = await page.locator('button[role="radio"]').all()
            empty_radios = []
            for r in all_radios:
                try:
                    if not (await r.inner_text()).strip() and await r.is_visible():
                        empty_radios.append(r)
                except Exception:
                    pass
            if empty_radios:
                await empty_radios[-1].scroll_into_view_if_needed()
                await empty_radios[-1].click(force=True)
                await page.wait_for_timeout(500)
                body_l = (await page.inner_text("body")).lower()
                if "precisamos dessa informação" not in body_l and "precisamos dessa informacao" not in body_l:
                    pep_selecionado = True
                    print(f"[azos][cad] PEP selecionado via circulo vazio (ultimo)", flush=True)
                    break

            print(f"[azos][cad] PEP retry {_pep_attempt+1}", flush=True)
        except Exception:
            pass

    await page.wait_for_timeout(500)

    # Verifica novamente o modal CPF
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
                        preencheu = False
                        for sel in ['input[name="state"]', 'input[name="city"]',
                                    'input[name="street"]', 'input[name="neighborhood"]',
                                    'select[name="state"]', 'select[name="city"]',
                                    '[name="state"]', '[name="city"]']:
                            try:
                                el = page.locator(sel).first
                                if await el.count():
                                    val = await el.input_value()
                                    if val.strip():
                                        preencheu = True
                                        break
                            except Exception:
                                pass
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
    """Clica no botão de avançar (vários nomes possíveis). Ignora botões desabilitados.

    IMPORTANTE: Ignora explicitamente o botão 'Fazer cotação' do sidebar
    porque ele navega de volta para dados-pessoais (inicia nova simulação).
    """
    # Se botão "Ir para o Resumo" existir mas estiver disabled, despeja a
    # mensagem de erro inline pra ajudar o LP a entender o que travou.
    # Causa comum: capital de alguma cobertura acima do limite do portal AZOS
    # (ex: Morte Acidental máx R$ 1MM). Portal aceita o input mas trava o avanço.
    try:
        resumo_btn = page.locator('button:has-text("Ir para o Resumo")')
        if await resumo_btn.count() and await resumo_btn.first.get_attribute("disabled") is not None:
            body_txt = await page.inner_text("body")
            idx = body_txt.find("valor máximo")
            if idx >= 0:
                trecho = body_txt[max(0, idx-80):idx+200].replace('\n', ' | ')
                print(f"[azos][_continuar] BLOQUEIO inline: ...{trecho}...", flush=True)
    except Exception:
        pass

    seletores = [
        'button:has-text("Ir para o Resumo")',
        'button:has-text("Ir para o resumo")',
        'button:has-text("Continuar")',
        'button:has-text("Próximo")',
        'button:has-text("Avançar")',
        'button:has-text("Ver cotação")',
        'button:has-text("Ver cotacao")',
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
                    txt = (await btn.inner_text()).strip().lower()
                    if "fazer cotação" in txt or "fazer cotacao" in txt:
                        continue
                    disabled = await btn.get_attribute("disabled")
                    aria_disabled = await btn.get_attribute("aria-disabled")
                    if disabled is not None or aria_disabled == "true":
                        continue
                    await btn.scroll_into_view_if_needed()
                    await btn.click()
                    print(f"[azos] _clicar_continuar OK: '{txt}'", flush=True)
                    return True
                except Exception:
                    continue
        except Exception:
            pass

    # Fallback: force-click qualquer botão de avanço não-desabilitado
    try:
        for txt_target in ['Ir para o Resumo', 'Ir para o resumo', 'Continuar', 'Próximo',
                            'Avançar', 'Ver cotação', 'Ver cotacao', 'Calcular', 'Finalizar', 'Confirmar']:
            try:
                all_b = page.locator(f'button:has-text("{txt_target}")')
                n = await all_b.count()
                # Tenta do último (geralmente o botão da página principal, não sidebar)
                for j in range(n - 1, -1, -1):
                    btn = all_b.nth(j)
                    if not await btn.is_visible():
                        continue
                    inner = (await btn.inner_text()).strip().lower()
                    if "fazer cotação" in inner or "fazer cotacao" in inner:
                        continue
                    disabled = await btn.get_attribute("disabled")
                    aria_disabled = await btn.get_attribute("aria-disabled")
                    if disabled is None and aria_disabled != "true":
                        await btn.scroll_into_view_if_needed()
                        await btn.click(force=True)
                        print(f"[azos] _clicar_continuar fallback force-click '{txt_target}'", flush=True)
                        return True
            except Exception:
                pass
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
    # Prioridade: "R$ X,XX/mês" — formato exato do Azos "Estudo personalizado"
    m = re.search(r'R\$\s*([\d\.]+,\d{2})\s*/\s*m[eê]s', texto, re.IGNORECASE)
    if m:
        try:
            val = float(m.group(1).replace(".", "").replace(",", "."))
            if 1 <= val <= 5000:
                return val
        except Exception:
            pass

    # Padrões secundários
    patterns = [
        r'(?:prêmio|premio|mensalidade|parcela|por\s+mês|por\s+mes)[^\n]{0,60}?R\$\s*([\d\.]+,\d{2})',
        r'(?:mensal|mês|mes)[^\n]{0,30}?R\$\s*([\d\.]+,\d{2})',
    ]
    for pat in patterns:
        m = re.search(pat, texto, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(".", "").replace(",", "."))
                if 1 <= val <= 5000:
                    return val
            except Exception:
                pass

    # Fallback: menor valor R$ que não seja capital segurado
    all_vals = re.findall(r'R\$\s*([\d\.]+,\d{2})', texto)
    candidatos = []
    for v in all_vals:
        try:
            f = float(v.replace(".", "").replace(",", "."))
            if 1 <= f <= 5000:
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
