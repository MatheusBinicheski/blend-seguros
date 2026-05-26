"""
Blend Seguros — Cotação automática AZOS + MAG (somente cotação, sem proposta).

Fluxo de jobs:
  POST /cotar         → cria job, retorna {job_id} imediatamente.
  GET  /status/{id}   → polling até status=done/error.
  GET  /              → UI web single-page (VSL + form + resultado).
"""
import os, uuid, time, asyncio
from pathlib import Path
from typing import Dict, Any
from fastapi import FastAPI, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

from automacao.azos        import fase1_dados_pessoais, fase2_selecionar_coberturas
from automacao         import mag as mag_mod
from automacao.recomendador import recomendar, capital_recomendado_morte

app   = FastAPI(title="Blend Seguros — Cotação")
_BASE = Path(__file__).parent

try:
    app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")
except Exception:
    pass

# Jobs em memória: { job_id: {status, pct, msg, result, error, created_at} }
_jobs: Dict[str, Dict[str, Any]] = {}

# Concurrency: Railway tem RAM apertada. Rodamos AZOS e MAG SEQUENCIAIS por job.
# Cada job consome um slot — controlamos paralelismo entre clientes via semáforo.
_MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "1"))
_sem: asyncio.Semaphore | None = None

# Quando o usuário escolhe "em_vida" (foco em invalidez/doenças graves) reduzimos
# o capital MAG (morte) para um piso simbólico; nos outros casos usamos o capital
# completo recomendado pela renda (10x renda anual).
_FATOR_MAG_POR_TIPO = {
    "em_vida":    0.2,   # MAG complementar — 20% do recomendado
    "apos_morte": 1.0,   # MAG entra com cobertura plena por renda
    "mix":        0.6,   # meio termo
}


def _job_set(job_id: str, **kw):
    if job_id in _jobs:
        _jobs[job_id].update(kw)


def _cleanup_old_jobs():
    cutoff = time.time() - 1800
    old = [jid for jid, j in _jobs.items() if j["created_at"] < cutoff]
    for jid in old:
        del _jobs[jid]


@app.on_event("startup")
async def startup():
    global _sem
    _sem = asyncio.Semaphore(_MAX_CONCURRENT)
    print(f"[blend] startup ok — PORT={os.getenv('PORT','8000')} MAX_CONCURRENT={_MAX_CONCURRENT}",
          flush=True)


# ── Rotas básicas ────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((_BASE / "templates" / "index.html").read_text(encoding="utf-8"))


@app.get("/health")
async def health():
    ativos = sum(1 for j in _jobs.values() if j["status"] in ("queued", "running"))
    return {"status": "ok", "jobs_ativos": ativos}


@app.get("/status/{job_id}")
async def status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return JSONResponse(job)


# ── Debug endpoints (úteis em prod p/ entender Playwright) ───────────────────
@app.get("/debug/screenshot/{nome}")
async def debug_screenshot(nome: str):
    path = f"/tmp/{nome}.png"
    if not os.path.exists(path):
        return PlainTextResponse(f"não existe: {path}", status_code=404)
    return FileResponse(path, media_type="image/png")


@app.get("/debug/dump/{nome}")
async def debug_dump(nome: str):
    for ext in (".html", ".json", ".txt"):
        path = f"/tmp/{nome}{ext}"
        if os.path.exists(path):
            return FileResponse(path)
    return PlainTextResponse(f"não existe: /tmp/{nome}.html|.json|.txt", status_code=404)


# ── POST /cotar ──────────────────────────────────────────────────────────────
@app.post("/cotar")
async def cotar(
    background_tasks: BackgroundTasks,
    nome:                    str = Form(...),
    nascimento:              str = Form(...),
    altura:                  str = Form("175"),
    peso:                    str = Form("80"),
    profissao:               str = Form("Empresário"),
    sexo:                    str = Form("M"),
    fumante:                 str = Form("nao"),
    renda_mensal:            str = Form(...),
    cpf:                     str = Form(""),
    email:                   str = Form(""),
    telefone:                str = Form(""),
    estado_civil:            str = Form("Solteira/o"),
    cep:                     str = Form(""),
    numero:                  str = Form(""),
    complemento:             str = Form(""),
    tipo_cobertura:          str = Form("mix"),
    pratica_esporte_radical: str = Form("nao"),
    pilota_aviao:            str = Form("nao"),
    viaja_exterior:          str = Form("nao"),
    doenca_preexistente:     str = Form("nao"),
    internacao_2anos:        str = Form("nao"),
    cirurgia_prevista:       str = Form("nao"),
    imc_acima_40:            str = Form("nao"),
    diagnostico_cancer:      str = Form("nao"),
    diagnostico_cardio:      str = Form("nao"),
    diagnostico_diabetes:    str = Form("nao"),
    diagnostico_renal:       str = Form("nao"),
    diagnostico_hiv:         str = Form("nao"),
    uso_drogas:              str = Form("nao"),
):
    def num(s, d=0.0):
        try:
            return float(str(s).replace(".", "").replace(",", ".").replace("R$", "").strip())
        except Exception:
            return d

    def sim(s):
        return str(s).strip().lower() == "sim"

    cliente = {
        "nome": nome, "nascimento": nascimento,
        "altura": altura, "peso": peso, "profissao": profissao,
        "sexo": sexo, "fumante": fumante == "sim",
        "renda_mensal": num(renda_mensal),
        "cpf": cpf, "email": email, "telefone": telefone,
        "estado_civil": estado_civil,
        "cep": cep, "numero": numero, "complemento": complemento,
        "tipo_cobertura": tipo_cobertura,
    }

    saude = {
        "pratica_esporte_radical": sim(pratica_esporte_radical),
        "pilota_aviao":            sim(pilota_aviao),
        "viaja_exterior":          sim(viaja_exterior),
        "doenca_preexistente":     sim(doenca_preexistente),
        "internacao_2anos":        sim(internacao_2anos),
        "cirurgia_prevista":       sim(cirurgia_prevista),
        "imc_acima_40":            sim(imc_acima_40),
        "diagnostico_cancer":      sim(diagnostico_cancer),
        "diagnostico_cardio":      sim(diagnostico_cardio),
        "diagnostico_diabetes":    sim(diagnostico_diabetes),
        "diagnostico_renal":       sim(diagnostico_renal),
        "diagnostico_hiv":         sim(diagnostico_hiv),
        "uso_drogas":              sim(uso_drogas),
        "_cliente":                cliente,
    }

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "status":     "queued",
        "pct":        0,
        "msg":        "Na fila...",
        "result":     None,
        "error":      None,
        "created_at": time.time(),
    }
    _cleanup_old_jobs()
    background_tasks.add_task(_run_cotacao, job_id, cliente, saude)
    return JSONResponse({"job_id": job_id})


# ── Worker assíncrono que executa AZOS + MAG ─────────────────────────────────
async def _run_cotacao(job_id: str, cliente: dict, saude: dict):
    global _sem
    _job_set(job_id, status="queued", msg="Aguardando slot disponível...", pct=2)

    async with _sem:  # garante 1 job por vez (Railway low-RAM)
        result = {"azos": {"erro": "não rodou"}, "mag": {"erro": "não rodou"}}
        tipo_cob = cliente.get("tipo_cobertura", "mix")

        # ── AZOS ───────────────────────────────────────────────────────────
        try:
            _job_set(job_id, status="running", pct=10,
                     msg="Abrindo portal Azos e fazendo login...")

            fase1 = await fase1_dados_pessoais(cliente)
            if fase1.get("erro"):
                result["azos"] = {"erro": fase1["erro"][:300]}
            else:
                _job_set(job_id, pct=30,
                         msg="Azos: montando planejamento conforme seu perfil...")
                coberturas_limits = {c["nome"]: c for c in fase1["coberturas"]}
                nomes = list(coberturas_limits.keys())
                selecoes = recomendar(cliente, nomes, tipo_cobertura=tipo_cob)

                # Clampa cada cobertura aos limites reais do Azos
                for sel in selecoes:
                    lim = coberturas_limits.get(sel["nome"], {})
                    v_min = float(lim.get("valor_min") or 50_000)
                    v_max = float(lim.get("valor_max") or 5_000_000)
                    sel["valor"] = int(max(v_min, min(v_max, sel["valor"])))

                fase2 = await fase2_selecionar_coberturas(
                    fase1["session_id"], selecoes, saude=saude,
                    coberturas_limits=coberturas_limits,
                    parar_cotacao=True,   # blend: ler prêmio direto da tela de coberturas
                )
                result["azos"] = {
                    "premio_mensal": fase2.get("premio_mensal"),
                    "premio_anual":  fase2.get("premio_anual"),
                    "selecoes":      fase2.get("selecoes") or selecoes,
                    "erro":          fase2.get("erro"),
                }
        except Exception as e:
            result["azos"] = {"erro": str(e)[:300]}

        # ── MAG ────────────────────────────────────────────────────────────
        # Sequencial após AZOS (Railway: 2 Chromium juntos = OOM).
        try:
            _job_set(job_id, pct=65,
                     msg="MAG: consultando Vida Inteira (CG 3082/3083)...")
            capital_base = capital_recomendado_morte(cliente)
            fator = _FATOR_MAG_POR_TIPO.get(tipo_cob, 0.6)
            capital_mag = max(50_000, int(round(capital_base * fator / 10_000) * 10_000))
            mag_out = await mag_mod.cotar(cliente, capital=capital_mag, headless=True)
            result["mag"] = {
                "premio_mensal": mag_out.get("premio_mensal"),
                "capital":       mag_out.get("capital"),
                "produto":       mag_out.get("produto"),
                "erro":          mag_out.get("erro"),
            }
        except Exception as e:
            result["mag"] = {"erro": str(e)[:300]}

        # ── Conclusão ──────────────────────────────────────────────────────
        algum_ok = (
            (result["azos"].get("premio_mensal") and not result["azos"].get("erro"))
            or (result["mag"].get("premio_mensal") and not result["mag"].get("erro"))
        )
        _job_set(
            job_id,
            status="done" if algum_ok else "error",
            pct=100,
            msg="Cotação concluída!" if algum_ok else "Nenhuma cotação retornada.",
            result={
                "nome":        cliente["nome"],
                "nascimento":  cliente["nascimento"],
                "tipo_cobertura": tipo_cob,
                "azos":        result["azos"],
                "mag":         result["mag"],
            },
            error=None if algum_ok else "Ambas seguradoras falharam.",
        )


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main_api:app", host="0.0.0.0", port=port, reload=False)
