"""Utilitários compartilhados entre os módulos de cada seguradora."""
from __future__ import annotations
import asyncio, os, re
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

_CAPTCHA_API_KEY = os.getenv("TWOCAPTCHA_API_KEY", "")


async def novo_browser(headless: bool = True) -> tuple[object, Browser, BrowserContext, Page]:
    """Abre Chromium e retorna (playwright, browser, context, page)."""
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
    )
    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="pt-BR",
    )
    await ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['pt-BR', 'pt', 'en-US']});
        window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
    """)
    page = await ctx.new_page()
    return pw, browser, ctx, page


async def resolver_captcha(page: Page) -> bool:
    """Detecta e resolve reCAPTCHA v2 ou hCaptcha via 2captcha. Retorna True se resolveu."""
    if not _CAPTCHA_API_KEY:
        return False
    try:
        # Detecta sitekey de reCAPTCHA ou hCaptcha
        info = await page.evaluate("""() => {
            const rc = document.querySelector('.g-recaptcha[data-sitekey], [data-sitekey]');
            const hc = document.querySelector('.h-captcha[data-sitekey]');
            if (hc) return {type: 'hcaptcha', sitekey: hc.getAttribute('data-sitekey')};
            if (rc) return {type: 'recaptcha', sitekey: rc.getAttribute('data-sitekey')};
            // iframe reCAPTCHA embutido
            for (const iframe of document.querySelectorAll('iframe[src*="recaptcha"]')) {
                const m = iframe.src.match(/[?&]k=([^&]+)/);
                if (m) return {type: 'recaptcha', sitekey: m[1]};
            }
            return null;
        }""")
        if not info or not info.get("sitekey"):
            return False

        from twocaptcha import TwoCaptcha
        solver = TwoCaptcha(_CAPTCHA_API_KEY)
        url = page.url
        sitekey = info["sitekey"]
        captcha_type = info["type"]
        print(f"[captcha] resolvendo {captcha_type} sitekey={sitekey[:20]}... aguarde", flush=True)

        if captcha_type == "hcaptcha":
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: solver.hcaptcha(sitekey=sitekey, url=url)
            )
        else:
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: solver.recaptcha(sitekey=sitekey, url=url)
            )

        token = result["code"]
        print(f"[captcha] token recebido ({len(token)} chars)", flush=True)

        # Injeta o token e dispara o callback registrado no elemento reCAPTCHA
        await page.evaluate("""(token) => {
            // 1. Seta a textarea do reCAPTCHA v2
            const resp = document.getElementById('g-recaptcha-response');
            if (resp) { resp.innerHTML = token; resp.value = token; }
            // 2. hCaptcha
            const hresp = document.querySelector('[name="h-captcha-response"]');
            if (hresp) { hresp.value = token; }
            // 3. Dispara o data-callback do elemento (habilita botões como #btnAuth no MAG)
            const rcEl = document.querySelector('[data-callback]');
            if (rcEl) {
                const cbName = rcEl.getAttribute('data-callback');
                if (cbName && typeof window[cbName] === 'function') {
                    window[cbName](token);
                }
            }
        }""", token)
        await page.wait_for_timeout(1000)
        return True

    except Exception as e:
        print(f"[captcha] ERRO ao resolver: {e}", flush=True)
        return False


async def fechar_browser(pw, browser):
    try:
        await browser.close()
    except Exception:
        pass
    try:
        await pw.stop()
    except Exception:
        pass


def extrair_valor_monetario(texto: str) -> float | None:
    """Extrai o maior valor monetário no formato R$ 1.234,56 de um texto."""
    matches = re.findall(r'R\$\s*([\d.]+),([\d]{2})', texto)
    valores = [float(m[0].replace('.', '') + '.' + m[1]) for m in matches]
    return max(valores) if valores else None


async def clicar_continuar(page: Page) -> bool:
    """Clica no botão de avançar. Retorna True se clicou."""
    textos = ["Continuar", "Próximo", "Avançar", "Ver cotação", "Calcular", "Finalizar", "Confirmar"]
    for txt in textos:
        btn = page.locator(f'button:has-text("{txt}")').first
        try:
            if not await btn.count():
                continue
            if not await btn.is_visible():
                continue
            disabled = await btn.get_attribute("disabled")
            aria_dis = await btn.get_attribute("aria-disabled")
            if disabled is not None or aria_dis == "true":
                continue
            await btn.click()
            return True
        except Exception:
            pass

    # Fallback por coordenada (ignora oclusão)
    try:
        coords = await page.evaluate("""() => {
            const texts = ['Continuar','Próximo','Avançar','Ver cotação','Calcular','Finalizar'];
            for (const t of texts) {
                const btn = [...document.querySelectorAll('button')]
                    .find(b => b.textContent.includes(t) && !b.disabled);
                if (btn) {
                    const r = btn.getBoundingClientRect();
                    if (r.width > 0) return {x: r.left + r.width/2, y: r.top + r.height/2, t};
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
