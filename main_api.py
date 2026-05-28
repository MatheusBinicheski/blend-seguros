"""
Blend Seguros — Ferramenta de planejamento para Life Planners.

Fluxo B2B (corretor monta o plano antes de cotar):

  POST /planejamento  → recebe dados + tipo de cobertura, devolve a grid
                        recomendada (AZOS + MAG) com capital sugerido por
                        cobertura. Sem Playwright — resposta instantânea.

  POST /cotar         → recebe blend ajustado pelo Life Planner (seleções AZOS
                        + se inclui MAG) + dados completos + saúde, cria job
                        e dispara Playwright sequencial (AZOS depois MAG).

  GET  /status/{id}   → polling até status=done/error.
  GET  /              → UI single-page (3 telas: dados → planejamento → resultado).
"""
import os, uuid, time, asyncio, json
from pathlib import Path
from typing import Dict, Any
from fastapi import FastAPI, Form, BackgroundTasks, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

from automacao.azos        import fase1_dados_pessoais, fase2_selecionar_coberturas
from automacao         import mag as mag_mod
from automacao.recomendador import planejamento_grid, blends_de_ouro

app   = FastAPI(title="Blend Seguros — Life Planner")
_BASE = Path(__file__).parent

try:
    app.mount("/static", StaticFiles(directory=str(_BASE / "static")), name="static")
except Exception:
    pass

_jobs: Dict[str, Dict[str, Any]] = {}

_MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "1"))
_sem: asyncio.Semaphore | None = None


def _num(s, default=0.0):
    try:
        return float(str(s).replace(".", "").replace(",", ".").replace("R$", "").strip())
    except Exception:
        return default


def _sim(s):
    return str(s).strip().lower() == "sim"


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
    print(f"[blend] startup ok — PORT={os.getenv('PORT','8000')} MAX_CONCURRENT={_MAX_CONCURRENT}", flush=True)


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


# ── POST /audio-historia ─────────────────────────────────────────────────────
# Recebe gravação de áudio do Life Planner descrevendo a história do cliente.
# Salva em /tmp/blend_audios/{timestamp}_{nome}.webm.
@app.post("/audio-historia")
async def audio_historia(
    audio: UploadFile = File(...),
    cliente_nome: str = Form(""),
):
    pasta = Path("/tmp/blend_audios")
    pasta.mkdir(parents=True, exist_ok=True)
    nome_safe = "".join(c if c.isalnum() else "_" for c in cliente_nome)[:60] or "cliente"
    fname = f"{int(time.time())}_{nome_safe}.webm"
    destino = pasta / fname
    try:
        with open(destino, "wb") as f:
            f.write(await audio.read())
        print(f"[blend][audio] gravado: {destino} ({destino.stat().st_size} bytes)", flush=True)
        return {"ok": True, "file": fname, "bytes": destino.stat().st_size}
    except Exception as e:
        return JSONResponse({"ok": False, "erro": str(e)[:200]}, status_code=500)


# ── Debug endpoints ──────────────────────────────────────────────────────────
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


# ── POST /planejamento ──────────────────────────────────────────────────────
# Resposta instantânea (sem Playwright). LP usa pra revisar/ajustar capitais.
@app.post("/planejamento")
async def planejamento(
    nome:           str  = Form(...),
    nascimento:     str  = Form(...),
    renda_mensal:   str  = Form(...),
    tipo_cobertura: str  = Form("mix"),
    altura:         str  = Form("175"),
    peso:           str  = Form("80"),
    fumante:        str  = Form("nao"),
    profissao:      str  = Form(""),
    estado_civil:   str  = Form("solteiro"),
    med_continuo:   str  = Form("nao"),
    tem_dependentes:str  = Form(""),
):
    cliente = {
        "nome":           nome,
        "nascimento":     nascimento,
        "renda_mensal":   _num(renda_mensal),
        "altura":         _num(altura, 175),
        "peso":           _num(peso, 80),
        "fumante":        fumante,
        "profissao":      profissao,
        "estado_civil":   estado_civil,
        "med_continuo":   med_continuo,
        "tem_dependentes": bool(tem_dependentes),
    }
    grid = planejamento_grid(cliente, tipo_cobertura=tipo_cobertura)
    grid["blends_de_ouro"] = blends_de_ouro(cliente)
    return JSONResponse(grid)


# ── POST /cotar ──────────────────────────────────────────────────────────────
# Dispara Playwright com o blend final (escolhas do LP).
# Recebe o blend serializado em JSON via field `blend` do form.
@app.post("/cotar")
async def cotar(
    background_tasks: BackgroundTasks,
    # cadastro completo (necessário para o portal)
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
    # saúde
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
    # blend final escolhido pelo LP (JSON)
    blend:                   str = Form(...),
):
    cliente = {
        "nome": nome, "nascimento": nascimento,
        "altura": altura, "peso": peso, "profissao": profissao,
        "sexo": sexo, "fumante": fumante == "sim",
        "renda_mensal": _num(renda_mensal),
        "cpf": cpf, "email": email, "telefone": telefone,
        "estado_civil": estado_civil,
        "cep": cep, "numero": numero, "complemento": complemento,
        "tipo_cobertura": tipo_cobertura,
    }

    saude = {
        "pratica_esporte_radical": _sim(pratica_esporte_radical),
        "pilota_aviao":            _sim(pilota_aviao),
        "viaja_exterior":          _sim(viaja_exterior),
        "doenca_preexistente":     _sim(doenca_preexistente),
        "internacao_2anos":        _sim(internacao_2anos),
        "cirurgia_prevista":       _sim(cirurgia_prevista),
        "imc_acima_40":            _sim(imc_acima_40),
        "diagnostico_cancer":      _sim(diagnostico_cancer),
        "diagnostico_cardio":      _sim(diagnostico_cardio),
        "diagnostico_diabetes":    _sim(diagnostico_diabetes),
        "diagnostico_renal":       _sim(diagnostico_renal),
        "diagnostico_hiv":         _sim(diagnostico_hiv),
        "uso_drogas":              _sim(uso_drogas),
        "_cliente":                cliente,
    }

    try:
        blend_dict = json.loads(blend)
    except Exception:
        return JSONResponse({"erro": "blend JSON inválido"}, status_code=400)

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "status": "queued", "pct": 0, "msg": "Na fila...",
        "result": None, "error": None, "created_at": time.time(),
    }
    _cleanup_old_jobs()
    background_tasks.add_task(_run_cotacao, job_id, cliente, saude, blend_dict)
    return JSONResponse({"job_id": job_id})


# ── Worker assíncrono que executa AZOS + MAG ─────────────────────────────────
async def _run_cotacao(job_id: str, cliente: dict, saude: dict, blend: dict):
    """
    blend = {
      "azos": [{"nome_no_portal": str, "capital": int, "linha_id": str, "linha_nome": str}, ...],
      "mag":  [{"nome_no_portal": str, "capital": int, "linha_id": str}, ...],
    }
    Linhas inativas/não escolhidas já vieram filtradas pelo frontend.
    """
    global _sem
    _job_set(job_id, status="queued", msg="Aguardando slot disponível...", pct=2)

    azos_blend = blend.get("azos") or []
    mag_blend  = blend.get("mag")  or []

    async with _sem:
        result = {
            "azos": {"erro": None, "premio_mensal": None, "selecoes": []},
            "mag":  {"erro": None, "premio_mensal": None, "capital": None,
                     "produto": None},
        }

        # ── AZOS ───────────────────────────────────────────────────────────
        if azos_blend:
            try:
                _job_set(job_id, status="running", pct=10,
                         msg="Abrindo portal Azos e fazendo login...")
                fase1 = await fase1_dados_pessoais(cliente)
                if fase1.get("erro"):
                    result["azos"]["erro"] = fase1["erro"][:300]
                else:
                    _job_set(job_id, pct=35,
                             msg="Azos: aplicando blend escolhido pelo Life Planner...")
                    coberturas_limits = {c["nome"]: c for c in fase1["coberturas"]}

                    # Mapeia cada linha do blend (nome_no_portal é prefixo/substring)
                    # para o nome EXATO no portal Azos.
                    selecoes = []
                    nao_encontradas = []
                    for line in azos_blend:
                        alvo = (line.get("nome_no_portal") or line.get("nome_no_azos") or "").lower()
                        match = next(
                            (n for n in coberturas_limits if alvo and alvo in n.lower()),
                            None,
                        )
                        if not match:
                            nao_encontradas.append(alvo)
                            continue
                        lim = coberturas_limits[match]
                        v_min = float(lim.get("valor_min") or 50_000)
                        v_max = float(lim.get("valor_max") or 5_000_000)
                        cap   = int(max(v_min, min(v_max, int(line.get("capital") or v_min))))
                        selecoes.append({
                            "nome":   match,
                            "valor":  cap,
                            "motivo": line.get("linha_nome") or line.get("motivo") or "",
                        })
                    if nao_encontradas:
                        print(f"[blend] AZOS coberturas não encontradas no portal: {nao_encontradas}", flush=True)

                    if not selecoes:
                        result["azos"]["erro"] = "Nenhuma cobertura do blend bateu com o catálogo Azos"
                    else:
                        fase2 = await fase2_selecionar_coberturas(
                            fase1["session_id"], selecoes, saude=saude,
                            coberturas_limits=coberturas_limits,
                            parar_cotacao=True,
                        )
                        result["azos"] = {
                            "premio_mensal": fase2.get("premio_mensal"),
                            "premio_anual":  fase2.get("premio_anual"),
                            "selecoes":      fase2.get("selecoes") or selecoes,
                            "erro":          fase2.get("erro"),
                        }
            except Exception as e:
                result["azos"]["erro"] = str(e)[:300]
        else:
            result["azos"]["erro"] = "Nenhuma cobertura AZOS selecionada"

        # ── MAG ────────────────────────────────────────────────────────────
        # Hoje o wrapper mag.cotar só conhece SAF 3061. Whole Life Sucessão e
        # Term Life MAG ainda não têm fluxo Playwright dedicado — quando o LP
        # selecionar essas linhas, o sistema retorna o prêmio ESTIMADO da grid
        # (sem ir ao portal) e marca como "estimativa" na resposta.
        if mag_blend:
            try:
                # Procura linha SAF (única com scraping real implementado hoje)
                saf_line = next(
                    (m for m in mag_blend if "saf" in (m.get("linha_id") or "").lower()
                     or "saf" in (m.get("nome_no_portal") or "").lower()),
                    None,
                )
                outras = [m for m in mag_blend if m is not saf_line]

                premio_mag_total = 0.0
                capital_mag_total = 0
                produto_descricao = []

                # 1) Se SAF está no blend, cota de verdade no portal
                if saf_line:
                    _job_set(job_id, pct=70,
                             msg="MAG: consultando SAF Essencial Familiar (3061)...")
                    capital_mag = int(saf_line.get("capital") or 5_500)
                    mag_out = await mag_mod.cotar(cliente, capital=capital_mag, headless=True)
                    if mag_out.get("erro") and not mag_out.get("premio_mensal"):
                        result["mag"]["erro"] = mag_out["erro"]
                    else:
                        premio_mag_total  += float(mag_out.get("premio_mensal") or 0)
                        capital_mag_total += int(mag_out.get("capital") or 0)
                        produto_descricao.append(mag_out.get("produto") or "SAF 3061")

                # 2) Outras linhas MAG (Whole Life / Term Life / DG / Cirurgias):
                #    sem scraping real ainda — devolve estimativa do catálogo (campo
                #    `premio_estimado_input` enviado pelo frontend, se houver).
                for m in outras:
                    p_est = float(m.get("premio_estimado") or 0)
                    if p_est > 0:
                        premio_mag_total  += p_est
                        capital_mag_total += int(m.get("capital") or 0)
                        produto_descricao.append(f"{m.get('linha_nome') or m.get('produto') or 'MAG'} (estimado)")

                if premio_mag_total > 0:
                    result["mag"] = {
                        "premio_mensal": round(premio_mag_total, 2),
                        "capital":       capital_mag_total,
                        "produto":       " + ".join(produto_descricao) if produto_descricao else None,
                        "erro":          None,
                    }
                elif not result["mag"].get("erro"):
                    result["mag"]["erro"] = "Nenhuma linha MAG retornou prêmio"
            except Exception as e:
                result["mag"]["erro"] = str(e)[:300]
        else:
            result["mag"]["erro"] = "MAG não incluída no blend"

        # ── Conclusão ──────────────────────────────────────────────────────
        algum_ok = (
            (result["azos"].get("premio_mensal") and not result["azos"].get("erro"))
            or (result["mag"].get("premio_mensal") and not result["mag"].get("erro"))
        )
        _job_set(
            job_id,
            status="done" if algum_ok else "error",
            pct=100,
            msg="Cotação concluída!" if algum_ok else "Nenhuma seguradora retornou prêmio.",
            result={
                "nome":           cliente["nome"],
                "nascimento":     cliente["nascimento"],
                "tipo_cobertura": cliente.get("tipo_cobertura"),
                "azos":           result["azos"],
                "mag":            result["mag"],
            },
            error=None if algum_ok else "Ambas seguradoras falharam.",
        )


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main_api:app", host="0.0.0.0", port=port, reload=False)
