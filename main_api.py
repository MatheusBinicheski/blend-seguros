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
from automacao.recomendador import (
    planejamento_grid, blends_de_ouro, relatorio_catalogo, gerar_resumo_auditavel,
)

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


@app.get("/diagnostico/catalogo")
async def diagnostico_catalogo():
    """Roda o auditor estático contra o catálogo. Útil pra ver erros de
    pareamento, falta de fontes, coberturas Azos oficiais não cobertas."""
    return JSONResponse(relatorio_catalogo())


@app.get("/status/{job_id}")
async def status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return JSONResponse(job)


# ── POST /audio-historia ─────────────────────────────────────────────────────
# Recebe gravação de áudio do Life Planner descrevendo a história do cliente,
# transcreve LOCALMENTE com faster-whisper, extrai sinais via regras
# (profissões de risco, doenças, dependentes, esportes, sucessão) e retorna
# os ajustes sugeridos pro `cliente` + `linhas` do planejamento.
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

        # Roda transcrição + análise em uma task pra não bloquear o request
        from automacao.audio_inteligencia import analisar_audio
        # cliente_nome vem do form; outros campos não disponíveis aqui
        cliente_stub = {"nome": cliente_nome}
        analise = analisar_audio(str(destino), cliente_stub)

        return {
            "ok": True,
            "file": fname,
            "bytes": destino.stat().st_size,
            "texto": analise.get("texto", ""),
            "sinais": analise.get("sinais", []),
            "ajustes_cliente": {
                k: v for k, v in (analise.get("cliente_enriquecido") or {}).items()
                if k != "nome"
            },
            "ajustes_linhas": analise.get("ajustes_linhas", {}),
            "erro_analise":   analise.get("erro"),
        }
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
    audio_ajustes_cliente: str = Form(""),
    audio_ajustes_linhas:  str = Form(""),
    modo_simplificado:     str = Form(""),
):
    import json as _json
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
    # Aplica ajustes do áudio (form do LP vence sobre dedução, mas campos
    # vazios são enriquecidos: ex: form sem fumante + áudio diz "fumo" → sim)
    try:
        ajustes_cli = _json.loads(audio_ajustes_cliente) if audio_ajustes_cliente else {}
    except Exception:
        ajustes_cli = {}
    for k, v in ajustes_cli.items():
        if str(cliente.get(k) or "").strip() in ("", "nao", "Solteira/o", "solteiro"):
            cliente[k] = v
    try:
        ajustes_lin = _json.loads(audio_ajustes_linhas) if audio_ajustes_linhas else {}
    except Exception:
        ajustes_lin = {}

    grid = planejamento_grid(
        cliente,
        tipo_cobertura=tipo_cobertura,
        modo_simplificado=modo_simplificado,
    )

    # Aplica ajustes_linhas do áudio: ativa linhas extras + bumpa capital
    if ajustes_lin:
        for L in grid.get("linhas", []):
            acao = ajustes_lin.get(L["id"])
            if not acao:
                continue
            if acao in ("forcar_ativa", "forcar_ativa_max"):
                L["ativo_default"] = True
                L["audio_forcado"] = True
                L["audio_motivo"]  = acao
            if acao == "forcar_ativa_max":
                # Sobe capital pro teto da linha (max do catálogo)
                L["capital_sugerido"] = L.get("capital_max") or L.get("capital_sugerido")
            if acao == "reduzir_capital":
                # Mantém ativa mas reduz pro mínimo (perfil solteiro/sem deps)
                L["capital_sugerido"] = max(L.get("capital_min") or 0,
                                            (L.get("capital_sugerido") or 0) // 2)

    grid["blends_de_ouro"] = blends_de_ouro(cliente)
    grid["audio_ajustes_aplicados"] = {
        "cliente": ajustes_cli,
        "linhas":  ajustes_lin,
    }
    # Resumo auditável (markdown) embutido no payload — UI pode mostrar em <details>
    try:
        grid["resumo_auditavel_md"] = gerar_resumo_auditavel(cliente, grid)
    except Exception as e:  # noqa: BLE001
        grid["resumo_auditavel_md"] = f"_(falha ao gerar resumo: {e})_"
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
                        # Não re-clampar com defaults do extrator (1000..5MM são
                        # placeholders sem base no portal real). O frontend +
                        # recomendador já clamparam dentro do limite REAL da
                        # seguradora (ex: AZOS RIT 100..600 R$/dia, Morte
                        # Acidental ≤ R$ 1MM). Re-clampar aqui quebrava linhas
                        # como RIT cujo capital é R$/dia (300) e ficava 1000.
                        cap = int(line.get("capital") or 0)
                        if cap <= 0:
                            continue
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
        # cotar_blend() roda no canal Vida Toda VD STOA cotando todas as
        # linhas MAG no portal em uma sessão só. Linhas com nome_no_portal
        # contendo "WHOLE LIFE" ou "TERM LIFE" são do canal PRIVATE VD STOA
        # (fluxo UI diferente — botão "Editar Solução") e ainda não são cotadas
        # automaticamente: retornam premio_estimado do catálogo.
        if mag_blend:
            try:
                _job_set(job_id, pct=70,
                         msg="MAG: cotando produtos no canal Vida Toda VD STOA...")

                blend_para_mag = [
                    {
                        "linha_id":       m.get("linha_id"),
                        "nome_no_portal": m.get("nome_no_portal"),
                        "capital":        int(m.get("capital") or 0),
                    }
                    for m in mag_blend
                ]
                mag_out = await mag_mod.cotar_blend(cliente, blend_para_mag, headless=True)

                premio_mag_total  = float(mag_out.get("premio_mensal_total") or 0)
                capital_mag_total = sum(int(i.get("capital_real") or i.get("capital_pedido") or 0)
                                        for i in (mag_out.get("itens") or []))
                produto_descricao = [
                    f"{i.get('nome_no_portal','?')}: R$ {i.get('premio_estimado') or 0:.2f}"
                    for i in (mag_out.get("itens") or [])
                    if i.get("premio_estimado")
                ]

                # Soma as estimativas dos produtos do canal PRIVATE (Whole Life,
                # Term Life) que ficaram fora da cotação real
                nao_cotados = mag_out.get("nao_cotados") or []
                if nao_cotados:
                    for m in mag_blend:
                        if m.get("nome_no_portal") in nao_cotados:
                            p_est = float(m.get("premio_estimado") or 0)
                            if p_est > 0:
                                premio_mag_total  += p_est
                                capital_mag_total += int(m.get("capital") or 0)
                                produto_descricao.append(
                                    f"{m.get('nome_no_portal')}: R$ {p_est:.2f} (estimado, canal PRIVATE)"
                                )

                if premio_mag_total > 0:
                    result["mag"] = {
                        "premio_mensal": round(premio_mag_total, 2),
                        "capital":       capital_mag_total,
                        "produto":       " + ".join(produto_descricao) if produto_descricao else None,
                        "itens":         mag_out.get("itens"),
                        "nao_cotados":   nao_cotados,
                        "erro":          None,
                    }
                else:
                    result["mag"]["erro"] = mag_out.get("erro") or "Nenhuma linha MAG retornou prêmio"
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
