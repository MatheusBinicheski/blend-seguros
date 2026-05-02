"""Utilitários compartilhados entre os módulos de cada seguradora."""
from __future__ import annotations
import re
from playwright.async_api import async_playwright, Browser, BrowserContext, Page


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
