"""
Diagnóstico: verifica quais seletores funcionam para as opções do dropdown MAG.
Testa: [role="option"], [id*="--option-"], [role="listbox"], get_by_text.
"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()
from automacao.base import novo_browser, fechar_browser
from automacao.mag import _login, _abrir_react_select


async def main():
    pw, browser, ctx, page = await novo_browser(headless=False)
    try:
        await _login(page)
        print(f"[ok] login → {page.url}")

        await page.goto("https://digital.mag.com.br/contratacao/simulacao",
                        wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(4000)

        # Abre dropdown Estado
        print("\n--- Abrindo Estado ---")
        await _abrir_react_select(page, "quoter_form__your-data__state_id__input")

        inp = page.locator('#quoter_form__your-data__state_id__input')
        await inp.press_sequentially("Paulo", delay=80)
        await page.wait_for_timeout(2500)

        val = await inp.input_value()
        print(f"  valor no input: {repr(val)}")

        # Testa cada seletor
        c1 = await page.locator('[role="option"]').count()
        print(f"\n  [role='option']         → {c1} elementos")

        c2 = await page.locator('[id*="--option-"]').count()
        print(f"  [id*='--option-']       → {c2} elementos")
        if c2:
            texts = await page.locator('[id*="--option-"]').all_inner_texts()
            for t in texts[:5]:
                print(f"    → {t!r}")
            # Testa filter por texto
            filtered = page.locator('[id*="--option-"]').filter(has_text="Paulo")
            fc = await filtered.count()
            print(f"  filter has_text='Paulo' → {fc} elementos")

        c3 = await page.locator('[role="listbox"]').count()
        print(f"  [role='listbox']        → {c3} elementos")
        if c3:
            lb_divs = await page.locator('[role="listbox"] div').count()
            print(f"  [role='listbox'] div    → {lb_divs} divs filhos")
            lb_texts = await page.locator('[role="listbox"] div').all_inner_texts()
            for t in lb_texts[:5]:
                print(f"    → {t!r}")

        try:
            gbt = page.get_by_text("São Paulo", exact=False)
            c4 = await gbt.count()
            print(f"  get_by_text('São Paulo') → {c4} elementos")
        except Exception as e:
            print(f"  get_by_text erro: {e}")

        # Tenta clicar na opção "SÃO PAULO" usando a melhor estratégia
        print("\n--- Tentando clicar em SÃO PAULO ---")
        clicked = False

        if c2:
            try:
                opt = page.locator('[id*="--option-"]').filter(has_text="Paulo").first
                await opt.click(timeout=2000)
                print("  ✅ Clicou via [id*='--option-']")
                clicked = True
            except Exception as e:
                print(f"  ✗ [id*='--option-'] click erro: {e}")

        if not clicked and c3:
            try:
                opt = page.locator('[role="listbox"] div').filter(has_text="Paulo").first
                await opt.click(timeout=2000)
                print("  ✅ Clicou via [role='listbox'] div")
                clicked = True
            except Exception as e:
                print(f"  ✗ [role='listbox'] click erro: {e}")

        if not clicked:
            try:
                await page.get_by_text("São Paulo", exact=False).first.click(timeout=2000)
                print("  ✅ Clicou via get_by_text")
                clicked = True
            except Exception as e:
                print(f"  ✗ get_by_text click erro: {e}")

        await page.wait_for_timeout(1000)

        # Verifica valor após selecionar
        final_val = await page.locator('input[name="state_id"], #quoter_form__your-data__state_id__input').first.input_value()
        print(f"\n  valor estado após click: {repr(final_val)}")

        # IDs dos options para referência
        print("\n--- IDs de todos elementos [id*='--option-'] ---")
        ids = await page.evaluate("""() =>
            [...document.querySelectorAll('[id*="--option-"]')].map(e => ({
                id: e.id,
                text: e.textContent.trim().slice(0, 40),
                visible: e.offsetParent !== null
            }))
        """)
        for item in ids[:10]:
            print(f"  {item['id']} → {item['text']!r} (visible={item['visible']})")

        await page.screenshot(path="/tmp/mag_selectors.png")
        print("\nScreenshot: /tmp/mag_selectors.png")

    finally:
        await fechar_browser(pw, browser)

asyncio.run(main())
