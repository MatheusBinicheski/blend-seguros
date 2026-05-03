"""
Blend Seguros API
─────────────────
POST /cotar              → inicia coleta paralela nas 4 seguradoras
GET  /status/{job_id}    → progresso do job
GET  /coberturas/{job_id}→ coberturas disponíveis (quando fase1 concluída)
POST /finalizar/{job_id} → finaliza blend com seleções do corretor
GET  /resultado/{job_id} → resultado final
GET  /                   → interface web do corretor
"""
import asyncio, time, uuid, os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

from automacao import azos, mag, omint

# ── Estado em memória ─────────────────────────────────────────────────────────
JOBS: dict[str, dict] = {}
JOB_TTL = 1800  # 30 min


def _novo_job() -> tuple[str, dict]:
    jid = str(uuid.uuid4())
    job = {
        "id": jid,
        "status": "pending",
        "pct": 0,
        "msg": "Aguardando...",
        "created_at": time.time(),
        "fase1": {},       # {seguradora: ResultadoFase1}
        "resultado": [],   # [ResultadoCotacao]
        "erro": None,
    }
    JOBS[jid] = job
    return jid, job


def _limpar_jobs_antigos():
    agora = time.time()
    antigos = [jid for jid, j in JOBS.items() if agora - j["created_at"] > JOB_TTL]
    for jid in antigos:
        JOBS.pop(jid, None)


# ── App ───────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(title="Blend Seguros", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("static/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/cotar")
async def cotar(
    background_tasks: BackgroundTasks,
    nome:       str = Form(...),
    nascimento: str = Form(...),
    cpf:        str = Form(""),
    email:      str = Form(""),
    telefone:   str = Form(""),
    renda:      str = Form("5000"),
    sexo:       str = Form("M"),
):
    _limpar_jobs_antigos()
    jid, job = _novo_job()
    dados = dict(
        nome=nome, nascimento=nascimento, cpf=cpf,
        email=email, telefone=telefone, renda_mensal=renda, sexo=sexo,
    )
    background_tasks.add_task(_executar_fase1, jid, dados)
    return {"job_id": jid}


@app.get("/status/{job_id}")
async def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado")
    return {
        "status":  job["status"],
        "pct":     job["pct"],
        "msg":     job["msg"],
        "erro":    job["erro"],
    }


@app.get("/coberturas/{job_id}")
async def coberturas(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado")
    if job["status"] not in ("fase1_ok", "finalizado"):
        raise HTTPException(409, f"Fase 1 ainda não concluída: {job['status']}")

    resultado = {}
    for seg, r in job["fase1"].items():
        resultado[seg] = {
            "ok":      r.ok,
            "erro":    r.erro,
            "session_id": r.session_id,
            "coberturas": [
                {
                    "id":               c.id,
                    "nome":             c.nome,
                    "descricao":        c.descricao,
                    "valor_min":        c.valor_min,
                    "valor_max":        c.valor_max,
                    "premio_referencia": c.premio_referencia,
                    "seguradora":       c.seguradora,
                }
                for c in r.coberturas
            ],
        }
    return resultado


@app.post("/finalizar/{job_id}")
async def finalizar(
    job_id: str,
    background_tasks: BackgroundTasks,
    selecoes: str = Form(...),   # JSON string: [{seguradora, session_id, nome, valor}]
):
    import json
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado")

    try:
        lista = json.loads(selecoes)
    except Exception:
        raise HTTPException(400, "selecoes deve ser JSON válido")

    job["status"] = "finalizando"
    job["pct"] = 60
    job["msg"] = "Finalizando cotações nas seguradoras selecionadas..."
    background_tasks.add_task(_executar_fase2, job_id, lista)
    return {"ok": True, "job_id": job_id}


@app.get("/resultado/{job_id}")
async def resultado(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job não encontrado")
    if job["status"] != "finalizado":
        raise HTTPException(409, f"Ainda não finalizado: {job['status']}")

    return {
        "status": "finalizado",
        "cotacoes": [
            {
                "seguradora":      c.seguradora,
                "cobertura_nome":  c.cobertura_nome,
                "valor_capital":   c.valor_capital,
                "premio_mensal":   c.premio_mensal,
                "link_proposta":   c.link_proposta,
                "numero_proposta": c.numero_proposta,
                "erro":            c.erro,
            }
            for c in job["resultado"]
        ],
    }


# ── Lógica de background ──────────────────────────────────────────────────────

MODULOS = {
    "azos":  azos,
    "mag":   mag,
    "omint": omint,
}


async def _executar_fase1(job_id: str, dados: dict):
    job = JOBS.get(job_id)
    if not job:
        return

    job["status"] = "coletando"
    job["pct"] = 5
    job["msg"] = "Conectando às seguradoras em paralelo..."

    async def coletar(seg: str):
        mod = MODULOS[seg]
        job["msg"] = f"[{seg}] coletando coberturas..."
        try:
            result = await mod.fase1_coletar_coberturas(dados, headless=True)
            job["fase1"][seg] = result
            status = "ok" if result.ok else f"erro: {result.erro}"
            print(f"[blend] fase1 {seg} → {status}", flush=True)
        except Exception as e:
            from models import ResultadoFase1
            job["fase1"][seg] = ResultadoFase1(seguradora=seg, ok=False, erro=str(e))
            print(f"[blend] fase1 {seg} EXCEÇÃO: {e}", flush=True)

    # Executa todas em paralelo
    await asyncio.gather(*[coletar(seg) for seg in MODULOS])

    total = len(job["fase1"])
    ok    = sum(1 for r in job["fase1"].values() if r.ok)
    job["status"] = "fase1_ok"
    job["pct"]    = 50
    job["msg"]    = f"Coberturas coletadas: {ok}/{total} seguradoras ok. Monte o blend!"


async def _executar_fase2(job_id: str, selecoes: list[dict]):
    job = JOBS.get(job_id)
    if not job:
        return

    # Agrupa seleções por seguradora
    por_seg: dict[str, list[dict]] = {}
    sess_ids: dict[str, str] = {}
    for s in selecoes:
        seg = s["seguradora"]
        por_seg.setdefault(seg, []).append({"nome": s["nome"], "valor": s["valor"]})
        sess_ids[seg] = s.get("session_id", "")

    async def finalizar_seg(seg: str, sels: list[dict]):
        mod = MODULOS.get(seg)
        if not mod:
            return []
        try:
            return await mod.fase2_finalizar(sess_ids.get(seg, ""), sels)
        except Exception as e:
            from models import ResultadoCotacao
            return [ResultadoCotacao(
                seguradora=seg, cobertura_nome="Erro", valor_capital=0,
                premio_mensal=0, erro=str(e)
            )]

    resultados_por_seg = await asyncio.gather(
        *[finalizar_seg(seg, sels) for seg, sels in por_seg.items()]
    )

    todas = [c for lista in resultados_por_seg for c in lista]
    job["resultado"] = todas
    job["status"]    = "finalizado"
    job["pct"]       = 100
    job["msg"]       = f"Blend finalizado! {len(todas)} cotações geradas."
    print(f"[blend] fase2 concluída: {len(todas)} cotações", flush=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_api:app", host="0.0.0.0", port=8000, reload=True)
