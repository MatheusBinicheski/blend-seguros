"""
Exploração interativa do flow AZOS para mapear sondagem de preço Morte R$ 100k.

Roda com headless=False para inspeção visual. Após cada step, salva screenshot
e dump do DOM relevante para mapear seletores precisos.
"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from automacao import azos


DADOS = {
    "nome": "Carlos Mendes",
    "nascimento": "22/08/1988",
    "cpf": "111.444.777-35",
    "email": "carlos@teste.com",
    "telefone": "11988887777",
    "renda_mensal": "12000",
    "sexo": "M",
    "profissao": "Advogado",
    "ocupacao": "Profissional Liberal",
}


async def main():
    # Usa a fase1 da AZOS para chegar até a tela de coberturas
    print("\n=== AZOS — Fase1 (login + dados pessoais + coberturas) ===")
    res = await azos.fase1_coletar_coberturas(DADOS, headless=False)
    print(f"ok={res.ok} | coberturas={len(res.coberturas)} | session={res.session_id} | erro={res.erro}")
    if not res.ok:
        print("Fase1 falhou. Abortando.")
        return

    session = azos._SESSOES[res.session_id]
    page = session["page"]

    print(f"\n=== Página atual: {page.url} ===")
    await page.screenshot(path="/tmp/explore_azos_01_coberturas.png", full_page=True)
    print("Screenshot: /tmp/explore_azos_01_coberturas.png")

    print("\n=== Procurando toggle de 'Seguro de vida' e clicando ===")
    await azos._selecionar_cobertura(page, "Seguro de vida", 100_000.0)
    await page.wait_for_timeout(3000)
    await page.screenshot(path="/tmp/explore_azos_02_morte_selected.png", full_page=True)
    print("Screenshot: /tmp/explore_azos_02_morte_selected.png")

    # Dump DOM para entender estrutura: capital inputs visíveis + preço estimado
    print("\n=== Dump: inputs[type=tel] e painel direito ===")
    info = await page.evaluate("""() => {
        const inputs = [...document.querySelectorAll('input[type="tel"], input[type="number"]')]
            .map(i => ({
                name: i.name || '',
                id: i.id || '',
                placeholder: i.placeholder || '',
                value: i.value || '',
                visible: !!i.offsetParent,
                rect: i.getBoundingClientRect().toJSON()
            }));
        // Painel direito: procura "Prêmio estimado"
        const panel = [...document.querySelectorAll('*')].find(el =>
            (el.innerText || '').includes('Prêmio estimado') && (el.innerText || '').length < 1000
        );
        const panelText = panel ? panel.innerText.substring(0, 500) : null;
        return { inputs, panelText };
    }""")
    print(f"Inputs encontrados: {len(info['inputs'])}")
    for i, inp in enumerate(info['inputs']):
        print(f"  [{i}] name={inp['name']!r} id={inp['id']!r} placeholder={inp['placeholder']!r} value={inp['value']!r} visible={inp['visible']}")
    print(f"\nPainel direito (Prêmio estimado): {info['panelText']!r}")

    print("\n=== Clicando 'Ir para o Resumo' ou Continuar ===")
    await azos._clicar_continuar_azos(page)
    await page.wait_for_timeout(6000)
    print(f"URL após click: {page.url}")
    await page.screenshot(path="/tmp/explore_azos_03_apos_continuar.png", full_page=True)
    print("Screenshot: /tmp/explore_azos_03_apos_continuar.png")

    # Procura preço no resumo
    txt = await page.inner_text("body")
    import re
    rs = re.findall(r'R\$\s*[\d.]+,\d{2}', txt)
    print(f"\nR$ encontrados na página atual ({len(rs)}):")
    for r in rs[:15]:
        print(f"  {r}")

    print("\n=== Mantendo browser aberto por 60s para inspeção manual ===")
    await page.wait_for_timeout(60_000)

    await azos.fechar_browser(session["pw"], session["browser"])


if __name__ == "__main__":
    asyncio.run(main())
