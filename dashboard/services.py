from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random
import re
import unicodedata
from datetime import datetime, timezone as datetime_timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import time

from django.conf import settings
from django.core.cache import cache
from .models import ElectionPoint

ROOT = "https://resultados.tse.jus.br"
TSE_REFRESH_SECONDS = 10
STATE_FILE = Path(settings.BASE_DIR) / "dashboard" / "data" / "electorate.json"
STATES = json.loads(STATE_FILE.read_text(encoding="utf-8"))
STATE_BY_UF = {s["uf"]: s for s in STATES}

CANDIDATES = {
    "president": [
        {"key": "lula", "name": "Lula", "color": "#e74646", "aliases": ["lula", "luiz inacio lula da silva"]},
        {"key": "flavio", "name": "Flávio Bolsonaro", "color": "#2875d0", "aliases": ["flavio", "flavio bolsonaro"]},
    ],
    "governor_mg": [
        {"key": "patrus", "name": "Patrus Ananias", "color": "#e74646", "aliases": ["patrus", "patrus ananias"]},
        {"key": "cleitinho", "name": "Kleitinho (Cleitinho Azevedo)", "color": "#2875d0", "aliases": ["kleitinho", "cleitinho", "cleitinho azevedo"]},
        {"key": "kalil", "name": "Kalil", "color": "#14a47a", "aliases": ["kalil", "alexandre kalil"]},
    ],
}

def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in value if not unicodedata.combining(c)).lower().strip()


def _number(value, default=0.0) -> float:
    try:
        if isinstance(value, str):
            value = value.replace(".", "").replace(",", ".") if "," in value else value
        return float(value)
    except (TypeError, ValueError):
        return default


def _fetch_json(url: str, timeout=8):
    key = "tse-json:" + str(abs(hash(url)))
    cached = cache.get(key)
    if cached is not None:
        if cached.get("_not_found"):
            raise FileNotFoundError("Arquivo ainda não publicado pelo TSE; nova tentativa em breve.")
        return cached
    req = Request(url, headers={"User-Agent": "Eleicao2026Dashboard/1.0 (local research)"})
    for attempt in range(2):
        try:
            with urlopen(req, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8-sig"))
                cache.set(key, data, TSE_REFRESH_SECONDS)
                return data
        except HTTPError as exc:
            if exc.code == 404:
                cache.set(key, {"_not_found": True}, TSE_REFRESH_SECONDS)
            raise
        except (URLError, TimeoutError):
            if attempt:
                raise
            time.sleep(.2)


def _config(base: str, environment: str):
    key = f"tse-ele-c:{environment}:{base}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    path = f"{base.rstrip('/')}/{environment}/comum/config/ele-c.json"
    data = _fetch_json(path)
    cache.set(key, data, 300)
    return data


def _find_election(config: dict, office: str, turn: int):
    wanted_cargo = "1" if office == "president" else "3"
    for pleito in config.get("pl", []):
        if pleito.get("c") != "ele2026":
            continue
        for election in pleito.get("e", []):
            cargo_codes = {str(c.get("cd")) for abr in election.get("abr", []) for c in abr.get("cp", [])}
            if wanted_cargo not in cargo_codes:
                continue
            if turn == 1 and str(election.get("t", "1")) == "1":
                return pleito, election
            if turn == 2 and election.get("cdt2"):
                second = dict(election)
                second["cd"] = election["cdt2"]
                second["t"] = "2"
                return pleito, second
    raise LookupError("O arquivo de configuração do TSE ainda não publicou este cargo/turno.")


def _result_url(config, pleito, election, office, uf, turn):
    folder = next((x["dir"] for x in config.get("arq", []) if x.get("tp") == "u"), None)
    if not folder:
        raise LookupError("Diretório de resultado unificado não encontrado no EA11.")
    replacements = {
        "base": os.getenv("TSE_BASE_URL", ROOT),
        "ambiente": os.getenv("TSE_ENVIRONMENT", "oficial"),
        "ciclo": str(pleito.get("c", "ele2026")),
        "cd_eleicao": str(election["cd"]),
        "cd_pleito": str(pleito.get("cd", "")),
        "uf": uf.lower(),
    }
    path = folder
    for key, value in replacements.items():
        path = path.replace(f"<{key}>", value)
    cargo = "0001" if office == "president" else "0003"
    election_id = str(election["cd"]).zfill(6)
    area = "br" if office == "president" and uf == "BR" else uf.lower()
    filename = f"{area}-c{cargo}-e{election_id}-u.json"
    return f"{path.rstrip('/')}/{filename}"


def _extract_live_row(data: dict, uf: str, office: str):
    candidate_map = CANDIDATES[office]
    wanted = {c["key"]: [_normalized(a) for a in c["aliases"]] for c in candidate_map}
    found = {}
    official_names = []
    for cargo in data.get("carg", []):
        for group in cargo.get("agr", []):
            for party in group.get("par", []):
                for candidate in party.get("cand", []):
                    name = candidate.get("nmu") or candidate.get("nm") or ""
                    official_names.append(name)
                    normalized = _normalized(name)
                    for key, aliases in wanted.items():
                        if any(alias and (alias in normalized or normalized in alias) for alias in aliases):
                            ident = str(candidate.get("sqcand") or candidate.get("n") or normalized)
                            found[key] = {"votes": _number(candidate.get("vap")), "official_name": name, "id": ident}
    vote_data = data.get("v", {})
    valid = _number(vote_data.get("vvc"))
    if valid <= 0:
        valid = sum(v["votes"] for v in found.values())
    elector = _number((data.get("e") or {}).get("te"))
    counted_elector = _number((data.get("e") or {}).get("est"))
    coverage = _number((data.get("e") or {}).get("pest"))
    if coverage <= 0:
        coverage = _number((data.get("s") or {}).get("pst"))
    if coverage > 1:
        coverage /= 100
    row = {
        "uf": uf, "name": STATE_BY_UF.get(uf, {}).get("name", uf),
        "electorate": elector or STATE_BY_UF.get(uf, {}).get("electorate", 0),
        "counted_electorate": counted_elector, "progress": min(1.0, max(0.0, coverage)),
        "valid_votes": valid, "candidates": {}, "matched_keys": list(found), "source": "TSE",
        "updated_at": f"{data.get('dg', '')} {data.get('hg', '')}".strip(),
        "official_names": sorted(set(official_names)),
    }
    for candidate in candidate_map:
        item = found.get(candidate["key"])
        if item and valid > 0:
            row["candidates"][candidate["key"]] = {**item, "share": item["votes"] / valid}
    return row


def live_rows(office: str, turn: int):
    base = os.getenv("TSE_BASE_URL", ROOT)
    environment = os.getenv("TSE_ENVIRONMENT", "oficial")
    try:
        config = _config(base, environment)
        cache_id = f"tse-data:{office}:{turn}:{environment}:{base}"
        cached = cache.get(cache_id)
        if cached is not None:
            return cached
        pleito, election = _find_election(config, office, turn)
        ufs = ["MG"] if office == "governor_mg" else [s["uf"] for s in STATES]
        rows = []
        errors = []
        # Um único arquivo Brasil funciona como sonda: evita 27 respostas 404 antes do início da apuração.
        if office == "president":
            _fetch_json(_result_url(config, pleito, election, office, "BR", turn))
        def one(uf):
            url = _result_url(config, pleito, election, office, uf, turn)
            return uf, _fetch_json(url), url
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(one, uf): uf for uf in ufs}
            for future in as_completed(futures):
                uf = futures[future]
                try:
                    _uf, data, _url = future.result()
                    rows.append(_extract_live_row(data, uf, office))
                except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
                    errors.append(f"{uf}: {exc}")
        if not rows:
            raise RuntimeError("Ainda não há arquivos de resultado para este cargo/turno. " + (errors[0] if errors else ""))
        matched = {key for row in rows for key in row.get("matched_keys", [])}
        missing_candidates = [candidate["name"] for candidate in CANDIDATES[office] if candidate["key"] not in matched]
        if missing_candidates and any(row.get("valid_votes", 0) > 0 for row in rows):
            raise RuntimeError("O TSE publicou arquivos, mas os nomes não foram associados a: " + ", ".join(missing_candidates) + ". Revise os aliases em dashboard/services.py.")
        if office == "president":
            received = {row["uf"]: row for row in rows}
            for meta in STATES:
                if meta["uf"] not in received:
                    rows.append({"uf": meta["uf"], "name": meta["name"], "region": meta["region"], "electorate": meta["electorate"], "counted_electorate": 0, "progress": 0, "valid_votes": 0, "candidates": {}, "matched_keys": [], "source": "TSE", "updated_at": ""})
        result = {"rows": sorted(rows, key=lambda x: x["uf"]), "errors": errors,
                  "updated_at": max((x["updated_at"] for x in rows), default=""),
                  "election_id": str(election["cd"]), "turn": turn, "environment": environment,
                  "api_info": {"config_endpoint": f"{base.rstrip('/')}/{environment}/comum/config/ele-c.json",
                               "result_endpoint_example": _result_url(config, pleito, election, office, "BR" if office == "president" else "MG", turn),
                               "refresh_seconds": TSE_REFRESH_SECONDS,
                               "result_files_per_refresh": len(ufs) + (1 if office == "president" else 0)}}
        cache.set(cache_id, result, TSE_REFRESH_SECONDS)
        return result
    except (HTTPError, URLError, TimeoutError, ValueError, LookupError, OSError) as exc:
        raise RuntimeError(str(exc)) from exc


def _weighted_line(points):
    if not points:
        return 0.0, 0.0, .22
    if len(points) == 1:
        return points[0]["shares"], 0.0, .18
    xs = [p["progress"] for p in points]
    ys = [p["shares"] for p in points]
    weights = [max(.08, p["progress"]) ** .65 * math.exp(-(xs[-1] - p["progress"]) * 2.2) for p in points]
    def fit(ws):
        sw = sum(ws) or 1
        mx = sum(w*x for w,x in zip(ws,xs))/sw
        my = sum(w*y for w,y in zip(ws,ys))/sw
        denom = sum(w*(x-mx)**2 for w,x in zip(ws,xs))
        slope = sum(w*(x-mx)*(y-my) for w,x,y in zip(ws,xs,ys))/denom if denom > 1e-9 else 0
        return my-slope*mx, slope
    a,b=fit(weights)
    for _ in range(3):
        residual=[y-(a+b*x) for x,y in zip(xs,ys)]
        med=sorted(abs(r) for r in residual)[len(residual)//2] or .01
        scale=max(.006,med*1.4826)
        huber=[min(1.0,1.5*scale/max(abs(r),1e-9)) for r in residual]
        a,b=fit([w*h for w,h in zip(weights,huber)])
    residual=[y-(a+b*x) for x,y in zip(xs,ys)]
    rmse=math.sqrt(sum(r*r for r in residual)/len(residual))
    return a,b,rmse


def _forecast(rows, office):
    candidates=CANDIDATES[office]
    if office == "governor_mg":
        allowed={"MG"}; rows=[r for r in rows if r["uf"] in allowed]
    observed=[r for r in rows if r.get("progress",0)>0 and r.get("valid_votes",0)>0]
    global_slopes=[]
    fits={}
    for row in observed:
        history=row.get("history") or []
        fits[row["uf"]]={}
        for c in candidates:
            points=[{"progress":p["progress"],"shares":p["shares"][candidates.index(c)]} for p in history]
            if not points and row["candidates"].get(c["key"],{}).get("share") is not None:
                points=[{"progress":row["progress"],"shares":row["candidates"][c["key"]]["share"]}]
            intercept,slope,rmse=_weighted_line(points)
            fits[row["uf"]][c["key"]]=(intercept,slope,rmse,points)
            if len(points)>1: global_slopes.append((c["key"],slope,max(.1,row["progress"])))
    global_by_key={c["key"]:0.0 for c in candidates}
    for c in candidates:
        values=[(b,w) for key,b,w in global_slopes if key==c["key"]]
        if values: global_by_key[c["key"]]=sum(b*w for b,w in values)/sum(w for _,w in values)
    per_state=[]
    for row in rows:
        progress=row.get("progress",0)
        valid=row.get("valid_votes",0)
        predictions={}
        errors={}
        for c in candidates:
            item=row.get("candidates",{}).get(c["key"],{})
            current=item.get("share")
            if current is None and valid>0: current=item.get("votes",0)/valid
            if current is None: current=0
            if row["uf"] in fits:
                _,slope,rmse,points=fits[row["uf"]][c["key"]]
                shrink=progress/(progress+.18)
                slope=slope*shrink+global_by_key[c["key"]]*(1-shrink)
                pred=current+slope*(1-progress)
                sigma=min(.24,max(.015,rmse+(1-progress)**.72*.12))
            else:
                pred=None; sigma=.23
            predictions[c["key"]]=None if pred is None else min(.99,max(0,pred))
            errors[c["key"]]=sigma
        if not any(v is not None for v in predictions.values()):
            continue
        per_state.append({**row,"forecast":predictions,"uncertainty":errors})
    # As UFs sem dados recebem a média da região observada; sem referência regional, usam a média nacional observada.
    for row in rows:
        if any(x["uf"]==row["uf"] for x in per_state): continue
        region=row.get("region") or STATE_BY_UF.get(row["uf"],{}).get("region")
        for c in candidates:
            peers=[x for x in per_state if (x.get("region") or STATE_BY_UF.get(x["uf"],{}).get("region"))==region and x["forecast"].get(c["key"]) is not None]
            if not peers: peers=[x for x in per_state if x["forecast"].get(c["key"]) is not None]
            if peers:
                w=[max(1,x.get("valid_votes",0)) for x in peers]
                row.setdefault("forecast",{})[c["key"]]=sum(x["forecast"][c["key"]]*ww for x,ww in zip(peers,w))/sum(w)
            else: row.setdefault("forecast",{})[c["key"]]=1/len(candidates)
            row.setdefault("uncertainty",{})[c["key"]]=.25
        row["_fallback"]=True
        per_state.append(row)
    # Pesos finais usam o colégio eleitoral multiplicado pela participação estimada de cada UF.
    for row in per_state:
        eligible=row.get("electorate",0)
        counted=row.get("counted_electorate",0)
        participation=(row.get("valid_votes",0)/max(1,counted)) if counted>0 else .72
        remaining=max(0,eligible-counted)*min(.98,max(.35,participation))
        row["forecast_valid_votes"]=row.get("valid_votes",0)+remaining
    denom=sum(x["forecast_valid_votes"] for x in per_state) or 1
    aggregate={}
    for c in candidates:
        aggregate[c["key"]]=sum((x["forecast"].get(c["key"]) or 0)*x["forecast_valid_votes"] for x in per_state)/denom
    # Monte Carlo exploratório: erro local cresce nos estados pouco apurados; inclui choque nacional comum.
    rng=random.Random(sum(ord(ch) for ch in office)+sum(int(x.get("progress",0)*10000) for x in per_state))
    sims={c["key"]:[] for c in candidates}; winner_counts={c["key"]:0 for c in candidates}
    for _ in range(1200):
        common=rng.gauss(0,.012)
        totals={c["key"]:0.0 for c in candidates}
        for row in per_state:
            w=row["forecast_valid_votes"]/denom
            draw=[]
            for c in candidates:
                mean=row["forecast"].get(c["key"]) or 0
                sigma=row["uncertainty"].get(c["key"],.2)
                draw.append(max(0,rng.gauss(mean+common,sigma)))
            for c,val in zip(candidates,draw): totals[c["key"]]+=w*val
        winner=max(totals,key=totals.get)
        winner_counts[winner]+=1
        for c in candidates:sims[c["key"]].append(totals[c["key"]])
    def quantile(a,q):
        a=sorted(a);return a[min(len(a)-1,int(q*(len(a)-1)))]
    summary=[]
    for c in candidates:
        vals=sims[c["key"]]
        current_votes=sum(x.get("candidates",{}).get(c["key"],{}).get("votes",0) for x in per_state)
        current_total=sum(x.get("valid_votes",0) for x in per_state)
        summary.append({**c,"votes":round(current_votes),"observed_pct":100*current_votes/current_total if current_total else None,
                        "projected_pct":100*aggregate[c["key"]],"interval":[100*quantile(vals,.05),100*quantile(vals,.95)],
                        "win_probability":winner_counts[c["key"]]/1200})
    total_electorate=sum(x.get("electorate",0) for x in per_state)
    counted_electorate=sum(x.get("counted_electorate",0) for x in per_state)
    total_votes=sum(x.get("valid_votes",0) for x in per_state)
    return {"candidates":summary,"states":per_state,"electorate":total_electorate,"counted_electorate":counted_electorate,
            "progress":counted_electorate/total_electorate if total_electorate else 0,"valid_votes":round(total_votes),
            "projected_valid_votes":round(denom),"model":"Regressão robusta hierárquica por UF + ponderação pelo eleitorado + Monte Carlo",
            "method_note":"Inclinação local Huber, retração para tendência agregada e suavização crescente com a cobertura; UFs sem dados recebem referência regional/nacional e incerteza maior.",
            "probability_note":"Probabilidades exploratórias condicionadas ao modelo e às incertezas assumidas; ainda não calibradas em backtest eleitoral."}


def build_snapshot(office: str, turn: int):
    if office not in CANDIDATES:
        raise ValueError("Cargo desconhecido")
    candidates = CANDIDATES[office]
    live=live_rows(office,turn)
    rows=live["rows"]
    for r in rows:
        r["region"]=STATE_BY_UF.get(r["uf"],{}).get("region")
        r["history"]=[]
    histkey=f"live-history:{office}:{turn}"
    trend_key=f"live-trend:{office}:{turn}"
    history=cache.get(histkey)
    trend=cache.get(trend_key)
    if history is None or trend is None:
        saved=list(ElectionPoint.objects.filter(office=office,turn=turn,election_id=live["election_id"]).order_by("id"))
        if history is None:
            history=[]
            latest_by_uf={}
            for snapshot in saved:
                for point in snapshot.state_points:
                    previous=latest_by_uf.get(point["uf"])
                    if previous and previous["valid_votes"]==point["valid_votes"] and previous["progress"]==point["progress"]:
                        continue
                    history.append(point)
                    latest_by_uf[point["uf"]]=point
            history=history[-400:]
        if trend is None:
            trend=[{"progress":snapshot.progress,"valid_votes":snapshot.valid_votes,
                    "observed":snapshot.observed,"forecast":snapshot.forecast,"signature":snapshot.signature}
                   for snapshot in saved][-5000:]
    for row in rows:
        if row.get("valid_votes",0)>0:
            previous = next((point for point in reversed(history) if point["uf"] == row["uf"]), None)
            if previous and previous.get("valid_votes") == row["valid_votes"] and previous["progress"] == row["progress"]:
                continue
            history.append({"uf":row["uf"],"progress":row["progress"],"valid_votes":row["valid_votes"],
                            "shares":[row.get("candidates",{}).get(c["key"],{}).get("share",0) or 0 for c in candidates]})
    history=history[-400:]
    cache.set(histkey,history,172800)
    for row in rows:
        row["history"]=[{"progress":x["progress"],"shares":x["shares"]} for x in history if x["uf"]==row["uf"]]
    forecast=_forecast(rows,office)
    observed=forecast_raw(rows,candidates)
    state_points=[{"uf":row["uf"],"progress":row["progress"],"valid_votes":row["valid_votes"],
                   "shares":[row.get("candidates",{}).get(c["key"],{}).get("share",0) or 0 for c in candidates]}
                  for row in rows if row.get("valid_votes",0)>0]
    signature=hashlib.sha256(json.dumps({"election":live["election_id"],"office":office,"turn":turn,
                                          "states":[{"uf":row["uf"],"valid_votes":row["valid_votes"],
                                                     "counted_electorate":row["counted_electorate"],
                                                     "candidate_votes":{c["key"]:row.get("candidates",{}).get(c["key"],{}).get("votes",0) for c in candidates}}
                                                    for row in rows]},sort_keys=True).encode()).hexdigest()
    if forecast["valid_votes"]>0 and (not trend or trend[-1].get("signature")!=signature):
        projection={c["key"]:c["projected_pct"] for c in forecast["candidates"]}
        ElectionPoint.objects.get_or_create(signature=signature,defaults={
            "office":office,"turn":turn,"election_id":live["election_id"],
            "progress":forecast["progress"],"valid_votes":forecast["valid_votes"],
            "state_points":state_points,"observed":observed,"forecast":projection})
        trend.append({"progress":forecast["progress"],"valid_votes":forecast["valid_votes"],
                      "observed":observed,"forecast":projection,"signature":signature})
        trend=trend[-5000:]
        cache.set(trend_key,trend,172800)
    forecast.update({"mode":"live","turn":turn,"updated_at":live["updated_at"],"election_id":live["election_id"],"environment":live["environment"],"errors":live["errors"],"api_info":live["api_info"],"history":trend})
    return forecast


def build_shared_live_snapshot(office: str, turn: int):
    """Refresh a live snapshot once per cache window; concurrent visitors read the same copy."""
    snapshot_key = f"shared-live-snapshot:{office}:{turn}"
    lock_key = f"shared-live-lock:{office}:{turn}"
    error_key = f"shared-live-error:{office}:{turn}"
    now = time.time()
    cached = cache.get(snapshot_key)
    if cached and now - cached["refreshed_at"] < TSE_REFRESH_SECONDS:
        return _snapshot_with_cache_info(cached, refreshing=False)
    recent_error = cache.get(error_key)
    if recent_error:
        if cached:
            return _snapshot_with_cache_info(cached, refreshing=False, error=recent_error)
        raise RuntimeError(recent_error)

    if cache.add(lock_key, True, timeout=30):
        try:
            snapshot = build_snapshot(office, turn)
            wrapped = {"snapshot": snapshot, "refreshed_at": time.time()}
            cache.set(snapshot_key, wrapped, 120)
            cache.delete(error_key)
            return _snapshot_with_cache_info(wrapped, refreshing=False)
        except Exception as exc:
            cache.set(error_key, str(exc), TSE_REFRESH_SECONDS)
            if cached:
                return _snapshot_with_cache_info(cached, refreshing=False, error=str(exc))
            raise
        finally:
            cache.delete(lock_key)

    if cached:
        return _snapshot_with_cache_info(cached, refreshing=True)
    error = cache.get(error_key)
    if error:
        raise RuntimeError(error)
    # Give the single updater a short head start when several clients arrive together.
    for _ in range(8):
        time.sleep(.1)
        cached = cache.get(snapshot_key)
        if cached:
            return _snapshot_with_cache_info(cached, refreshing=False)
    raise RuntimeError("A apuração está sendo atualizada pelo servidor; tente novamente em alguns segundos.")


def _snapshot_with_cache_info(wrapped, refreshing=False, error=None):
    snapshot = copy.deepcopy(wrapped["snapshot"])
    backend = settings.CACHES["default"]["BACKEND"]
    shared = backend.endswith(("RedisCache", "DatabaseCache"))
    refreshed_at = datetime.fromtimestamp(wrapped["refreshed_at"], datetime_timezone.utc).isoformat()
    snapshot["cache_info"] = {"single_refresh": True, "shared_backend": shared,
                              "refreshed_at": refreshed_at,
                              "refresh_seconds": TSE_REFRESH_SECONDS,
                              "refresh_in_progress": refreshing, "stale": bool(error)}
    if error:
        snapshot.setdefault("errors", []).append("Falha na atualização; exibindo o último resultado em cache: " + error)
    return snapshot


def forecast_raw(rows,candidates):
    denominator=sum(x.get("valid_votes",0) for x in rows)
    return {c["key"]:(100*sum(x.get("candidates",{}).get(c["key"],{}).get("votes",0) for x in rows)/denominator if denominator else None) for c in candidates}


