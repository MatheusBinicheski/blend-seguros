"""
Blend Seguros API — renderização server-side com Jinja2
"""
import asyncio, time, uuid, os, json
from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

from automacao import azos, mag, omint

JOBS: dict[str, dict] = {}
JOB_TTL = 1800


def _cpf_valido(cpf: str) -> bool:
    """Valida CPF brasileiro (11 dígitos + checksum)."""
    digitos = "".join(c for c in cpf if c.isdigit())
    if len(digitos) != 11 or digitos == digitos[0] * 11:
        return False
    for i in (9, 10):
        soma = sum(int(digitos[j]) * (i + 1 - j) for j in range(i))
        d = (soma * 10) % 11
        if d == 10:
            d = 0
        if d != int(digitos[i]):
            return False
    return True


def _nome_valido(nome: str) -> bool:
    """MAG rejeita nomes com números ou caracteres especiais — apenas letras/espaços/acentos."""
    import re as _re
    nome = nome.strip()
    if not nome or len(nome.split()) < 2:
        return False
    return bool(_re.fullmatch(r"[A-Za-zÀ-ÿ\s'\-]+", nome))


def _data_valida(data: str) -> bool:
    """Aceita DD/MM/AAAA com idade entre 18 e 80 anos."""
    import re as _re
    digits = _re.sub(r"\D", "", data)
    if len(digits) != 8:
        return False
    try:
        d, m, y = int(digits[:2]), int(digits[2:4]), int(digits[4:8])
        from datetime import date
        nasc = date(y, m, d)
        hoje = date.today()
        idade = hoje.year - nasc.year - ((hoje.month, hoje.day) < (nasc.month, nasc.day))
        return 18 <= idade <= 80
    except Exception:
        return False


def _novo_job() -> tuple[str, dict]:
    jid = str(uuid.uuid4())
    job = {
        "id": jid,
        "status": "pending",
        "msg": "Aguardando...",
        "created_at": time.time(),
        "fase1": {},
        "resultado": [],
        "erro": None,
    }
    JOBS[jid] = job
    return jid, job


def _limpar_jobs_antigos():
    agora = time.time()
    for jid in [k for k, j in JOBS.items() if agora - j["created_at"] > JOB_TTL]:
        JOBS.pop(jid, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Blend Seguros", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

MODULOS = {"azos": azos, "mag": mag, "omint": omint}

# ── Passo 1: formulário ───────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/debug/screenshot/{nome}")
async def debug_screenshot(nome: str):
    """Serve screenshots de debug salvas em /tmp/{nome}.png pelos módulos das seguradoras."""
    from fastapi.responses import FileResponse, PlainTextResponse
    path = f"/tmp/{nome}.png"
    if not os.path.exists(path):
        return PlainTextResponse(f"não existe: {path}", status_code=404)
    return FileResponse(path, media_type="image/png")


@app.get("/debug/dump/{nome}")
async def debug_dump(nome: str):
    """Serve HTML/JSON dumps de /tmp/{nome}.html|.json para depuração."""
    from fastapi.responses import FileResponse, PlainTextResponse
    for ext in (".html", ".json", ".txt"):
        path = f"/tmp/{nome}{ext}"
        if os.path.exists(path):
            return FileResponse(path)
    return PlainTextResponse(f"não existe: /tmp/{nome}.html|.json|.txt", status_code=404)


@app.post("/cotar")
async def cotar(
    request: Request,
    background_tasks: BackgroundTasks,
    nome:      str = Form(...),
    nascimento: str = Form(...),
    cpf:       str = Form(""),
    email:     str = Form(""),
    telefone:  str = Form(""),
    renda:     str = Form("5000"),
    sexo:      str = Form("M"),
    profissao: str = Form("Advogado"),
    ocupacao:  str = Form("Profissional Liberal"),
):
    if not _cpf_valido(cpf):
        return templates.TemplateResponse("erro.html", {
            "request": request,
            "msg": f"CPF '{cpf}' é inválido. Use um CPF válido (a MAG valida o checksum).",
        })
    if not _nome_valido(nome):
        return templates.TemplateResponse("erro.html", {
            "request": request,
            "msg": f"Nome '{nome}' é inválido. A MAG bloqueia números e caracteres especiais — use apenas letras (ex: 'João Silva').",
        })
    if not _data_valida(nascimento):
        return templates.TemplateResponse("erro.html", {
            "request": request,
            "msg": f"Data '{nascimento}' inválida ou idade fora do range 18-80 anos.",
        })
    _limpar_jobs_antigos()
    jid, job = _novo_job()
    dados = dict(
        nome=nome, nascimento=nascimento, cpf=cpf,
        email=email, telefone=telefone, renda_mensal=renda,
        sexo=sexo, profissao=profissao, ocupacao=ocupacao,
    )
    background_tasks.add_task(_executar_fase1, jid, dados)
    return RedirectResponse(f"/aguardando/{jid}", status_code=303)


# ── Passo 2: página de espera (auto-refresh Python-side) ─────────────────────

@app.get("/aguardando/{job_id}", response_class=HTMLResponse)
async def aguardando(request: Request, job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return templates.TemplateResponse("erro.html", {"request": request, "msg": "Job não encontrado."})
    if job["status"] == "fase1_ok":
        return RedirectResponse(f"/coberturas/{job_id}", status_code=303)
    if job["status"] == "erro":
        return templates.TemplateResponse("erro.html", {"request": request, "msg": job.get("erro", "Erro desconhecido.")})
    return templates.TemplateResponse("aguardando.html", {
        "request": request,
        "job_id": job_id,
        "msg": job.get("msg", "Processando..."),
    })


# ── Passo 3: tabela de coberturas (renderizada pelo Python) ───────────────────

@app.get("/coberturas/{job_id}", response_class=HTMLResponse)
async def coberturas(request: Request, job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return templates.TemplateResponse("erro.html", {"request": request, "msg": "Job não encontrado."})
    if job["status"] != "fase1_ok":
        return RedirectResponse(f"/aguardando/{job_id}", status_code=303)

    seguradoras = []
    todas_coberturas = []
    for seg, r in job["fase1"].items():
        seguradoras.append({
            "nome": seg.upper(),
            "ok": r.ok,
            "n": len(r.coberturas),
            "erro": (r.erro or "")[:200] if not r.ok else "",
        })
        for c in r.coberturas:
            todas_coberturas.append({
                "seg": seg,
                "session_id": r.session_id or "",
                "id": c.id,
                "nome": c.nome,
                "valor_min": int(c.valor_min),
                "valor_max": int(c.valor_max),
                "step": 10000 if c.valor_min >= 10000 else 1000,
            })

    return templates.TemplateResponse("coberturas.html", {
        "request": request,
        "job_id": job_id,
        "seguradoras": seguradoras,
        "coberturas": todas_coberturas,
    })


# ── Passo 4: finalizar blend ──────────────────────────────────────────────────

@app.post("/finalizar/{job_id}")
async def finalizar(
    background_tasks: BackgroundTasks,
    job_id: str,
    selecoes: str = Form(...),
):
    job = JOBS.get(job_id)
    if not job:
        return RedirectResponse("/", status_code=303)
    lista = json.loads(selecoes)
    job["status"] = "finalizando"
    job["msg"] = "Finalizando cotações..."
    background_tasks.add_task(_executar_fase2, job_id, lista)
    return RedirectResponse(f"/resultado/{job_id}", status_code=303)


# ── Passo 5: resultado ────────────────────────────────────────────────────────

@app.get("/resultado/{job_id}", response_class=HTMLResponse)
async def resultado(request: Request, job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return templates.TemplateResponse("erro.html", {"request": request, "msg": "Job não encontrado."})
    if job["status"] == "finalizando":
        return templates.TemplateResponse("aguardando.html", {
            "request": request,
            "job_id": job_id,
            "msg": job.get("msg", "Finalizando..."),
            "redirect": f"/resultado/{job_id}",
        })
    cotacoes = [
        {
            "seguradora": c.seguradora.upper(),
            "cobertura_nome": c.cobertura_nome,
            "valor_capital": int(c.valor_capital),
            "premio_mensal": c.premio_mensal,
            "link_proposta": c.link_proposta or "",
            "erro": c.erro or "",
        }
        for c in job.get("resultado", [])
    ]
    return templates.TemplateResponse("resultado.html", {
        "request": request,
        "cotacoes": cotacoes,
    })


# ── Background tasks ──────────────────────────────────────────────────────────

async def _limpar_sessoes_orfas():
    """Fecha browsers de sessões antigas que ficaram em _SESSOES sem chamar fase2 (vaza memória)."""
    from automacao import azos as _az, mag as _mg, omint as _om
    from automacao.base import fechar_browser
    for mod in (_az, _mg, _om):
        sessoes = getattr(mod, "_SESSOES", {})
        for sid in list(sessoes.keys()):
            sess = sessoes.pop(sid, None)
            if sess:
                try:
                    await fechar_browser(sess.get("pw"), sess.get("browser"))
                    print(f"[blend] sessão órfã fechada: {sid}", flush=True)
                except Exception as e:
                    print(f"[blend] erro fechando sessão {sid}: {e}", flush=True)


async def _executar_fase1(job_id: str, dados: dict):
    job = JOBS.get(job_id)
    if not job:
        return
    # Limpa browsers vazados de jobs anteriores antes de começar (evita OOM)
    await _limpar_sessoes_orfas()
    job["status"] = "coletando"
    job["msg"] = "Conectando às seguradoras..."

    # Railway tem RAM limitada — até 2 Chromium juntos causaram OOM.
    # Sem 1: executa uma seguradora por vez (AZOS → MAG → OMINT). ~9min total mas estável.
    sem = asyncio.Semaphore(1)

    async def coletar(seg: str):
        mod = MODULOS[seg]
        last_erro = ""
        for tentativa in range(3):
            async with sem:
                job["msg"] = f"[{seg}] coletando{'...' if tentativa == 0 else f' (tentativa {tentativa+1})'}"
                try:
                    result = await mod.fase1_coletar_coberturas(dados, headless=True)
                    if result.ok:
                        job["fase1"][seg] = result
                        print(f"[blend] {seg} ok (tentativa {tentativa+1})", flush=True)
                        return
                    last_erro = result.erro or "(sem mensagem)"
                    print(f"[blend] {seg} tentativa {tentativa+1} falhou: {last_erro}", flush=True)
                except Exception as e:
                    last_erro = str(e)
                    print(f"[blend] {seg} tentativa {tentativa+1} exceção: {last_erro}", flush=True)
            if tentativa < 2:
                # Espera maior entre tentativas para o GC liberar memória e o browser anterior fechar de vez
                await asyncio.sleep(8)
        from models import ResultadoFase1
        job["fase1"][seg] = ResultadoFase1(seguradora=seg, ok=False, erro=f"3x falhou — último: {last_erro[:150]}")

    await asyncio.gather(*[coletar(seg) for seg in MODULOS])
    ok = sum(1 for r in job["fase1"].values() if r.ok)
    job["status"] = "fase1_ok"
    job["msg"] = f"{ok}/{len(MODULOS)} seguradoras ok"


async def _executar_fase2(job_id: str, selecoes: list[dict]):
    job = JOBS.get(job_id)
    if not job:
        return
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
            return [ResultadoCotacao(seguradora=seg, cobertura_nome="Erro", valor_capital=0, premio_mensal=0, erro=str(e))]

    resultados = await asyncio.gather(*[finalizar_seg(seg, sels) for seg, sels in por_seg.items()])
    job["resultado"] = [c for lista in resultados for c in lista]
    job["status"] = "finalizado"
    job["msg"] = f"{len(job['resultado'])} cotações geradas"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main_api:app", host="0.0.0.0", port=8000, reload=True)
