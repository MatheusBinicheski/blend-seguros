"""Diagnóstico: lista produtos disponíveis no combobox MAG.

Faz login + preenche dados + abre tela de adicionar produto e despeja TODOS os
itens visíveis filtrando por substring (WHOLE, SUCESS, TERM LIFE, DG, etc).

Uso:
    python3 diag_mag_produtos.py [filtro]

Filtro: substring case-insensitive a buscar nos itens. Sem filtro → lista 60 primeiros.
"""
import asyncio
import sys
from dotenv import load_dotenv
load_dotenv()
from automacao.mag import fase1_coletar_coberturas, _SESSOES


async def main():
    filtro = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    cliente = {
        "nome": "Mateus Teste MAG",
        "nascimento": "15/03/1988",
        "cpf": "52998224725",
        "email": "diag@blend.test",
        "telefone": "11999990001",
        "renda_mensal": "18000",
        "profissao": "Advogado",
        "ocupacao": "Profissional Liberal",
        "sexo": "M",
        "altura": "175",
        "peso": "78",
        "fumante": "nao",
        "cep": "01310100",
        "numero": "100",
        "estado_civil": "casado",
    }
    print("[diag] iniciando fase1 (login + dados) HEADLESS=FALSE...", flush=True)
    r1 = await fase1_coletar_coberturas(cliente, headless=False)
    if not r1.ok:
        print(f"[diag] FALHOU fase1: {r1.erro}")
        return

    print(f"[diag] fase1 OK session_id={r1.session_id}", flush=True)
    sessao = _SESSOES[r1.session_id]
    page = sessao["page"]

    # Abre o combobox do último produto (tela de adicionar)
    combo = page.locator('input[aria-autocomplete="list"]').last
    try:
        await combo.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    await combo.click(force=True)
    await page.wait_for_timeout(800)

    # Lista TODOS os itens do dropdown via JS
    itens = await page.evaluate("""(filtro) => {
        const out = new Set();
        for (const el of document.querySelectorAll('div, li, span')) {
            if (el.children.length > 0 || !el.offsetParent) continue;
            const txt = (el.textContent || '').trim();
            const m = txt.match(/^(.+)\\((\\d+)\\)\\s*$/);
            if (m) {
                if (!filtro || txt.toLowerCase().includes(filtro)) {
                    out.add(txt);
                }
            }
        }
        return Array.from(out).sort();
    }""", filtro)

    print(f"\n[diag] {len(itens)} produtos no combobox (filtro='{filtro}'):", flush=True)
    for it in itens[:80]:
        print(f"  {it}")

    await page.screenshot(path="/tmp/diag_mag_combobox.png", full_page=True)
    await sessao["browser"].close()
    await sessao["pw"].stop()


if __name__ == "__main__":
    asyncio.run(main())
