"""Diagnostica o reCAPTCHA na tela de login MAG."""
import asyncio
from playwright.async_api import async_playwright


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.goto("https://digital.mag.com.br/simulador", wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(8000)
    await page.screenshot(path="/tmp/diag_recaptcha.png", full_page=True)

    info = await page.evaluate("""() => {
        const out = {
            url: location.href,
            title: document.title,
            data_sitekey_elements: [],
            recaptcha_iframes: [],
            hcaptcha: !!document.querySelector('.h-captcha, [class*="hcaptcha"]'),
            grecaptcha_elements: [],
            generic_recaptcha: [],
        };
        for (const el of document.querySelectorAll('[data-sitekey]')) {
            out.data_sitekey_elements.push({
                tag: el.tagName,
                cls: el.className,
                sitekey: el.getAttribute('data-sitekey'),
            });
        }
        for (const iframe of document.querySelectorAll('iframe')) {
            const src = iframe.src || '';
            if (src.includes('recaptcha') || src.includes('captcha')) {
                out.recaptcha_iframes.push(src.substring(0, 200));
            }
        }
        for (const el of document.querySelectorAll('.g-recaptcha, [class*="recaptcha"], [id*="recaptcha"]')) {
            out.grecaptcha_elements.push({
                tag: el.tagName, cls: el.className, id: el.id,
                sitekey: el.getAttribute('data-sitekey'),
            });
        }
        out.body_html_snippet = document.body.outerHTML.substring(0, 5000);
        return out;
    }""")
    import json
    print(json.dumps({k: v for k, v in info.items() if k != "body_html_snippet"},
                     indent=2, ensure_ascii=False))
    # Salva HTML pra analise
    with open("/tmp/diag_recaptcha.html", "w") as f:
        f.write(info["body_html_snippet"])
    print("HTML snippet salvo em /tmp/diag_recaptcha.html")
    await browser.close()
    await pw.stop()


asyncio.run(main())
