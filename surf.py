"""Previsão de surf personalizada para a Praia da Barrinha (Barra Velha/SC).

Uso:
  python surf.py                                  # gera previsao.html e abre no navegador
  python surf.py log <nota 1-5> <altura_m> ["AAAA-MM-DD HH"] ["obs"]
  python surf.py calibrar                         # ajusta fatores com o diário de sessões
"""
import argparse
import csv
import json
import os
import math
import statistics
import sys
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DIR = Path(__file__).parent
SESSOES = DIR / "sessoes.csv"
CALIBRACAO = DIR / "calibracao.json"
SAIDA = DIR / "previsao.html"
BRT = timezone(timedelta(hours=-3))  # Brasil sem horário de verão desde 2019
MAPA = DIR / "mapa" / "barrinha.svg"
PONTOS = DIR / "mapa" / "pontos.json"
CAMPOS_SESSAO = ["data_hora", "nota", "altura_m", "obs", "ponto_levanta", "ponto_morre"]

CONFIG = {
    "nome": "Praia da Barrinha",
    "local": "Barra Velha · SC",
    "janela": (5, 18),  # horas do dia em que dá pra surfar
    "lat": -26.690961,
    "lon": -48.684986,
    "tz": "America/Sao_Paulo",
    "dias": 7,
    "modelos_onda": ["ecmwf_wam025", "ncep_gfswave025", "meteofrance_wave", "dwd_gwam"],
    "modelos_vento": ["ecmwf_ifs025", "gfs_seamless", "icon_seamless"],
    # Hipótese inicial (direção de onde vem o swell -> fração da altura que chega na Barrinha).
    # Praia virada para ~60° (ENE); molhe no canto sul apontando ~27° bloqueia S/SE.
    "fator_direcao": [(0, 0.5), (30, 0.8), (60, 1.0), (100, 1.0), (130, 0.75),
                      (170, 0.45), (210, 0.3), (250, 0.1), (330, 0.1), (360, 0.5)],
    "praia_virada_para": 60,  # vento vindo dessa direção é maral (onshore)
    "vento_fraco_kmh": 6,  # abaixo disso o vento não atrapalha
    # Nível intermediário, pouco condicionamento: ideal entre 0.7 e 1.5 m, acima de 1.8 m pesa.
    "altura_ideal": (0.7, 1.5),
    "altura_limite": 1.8,
}

NOMES_ONDA = {"ecmwf_wam025": "ECMWF", "ncep_gfswave025": "GFS (WW3)",
              "meteofrance_wave": "MeteoFrance", "dwd_gwam": "DWD"}
SETORES = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
# Partições do mar (ondulações separadas). Nem todo modelo traz; usamos o primeiro que tiver.
PARTICOES = {"swell": "Principal", "secondary_swell": "Secundária", "wind": "Vaga de vento"}
MODELOS_PARTICAO = ["ncep_gfswave025", "meteofrance_wave"]


def get_json(url, params):
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=30) as r:
        return json.load(r)


def setor(graus):
    return SETORES[round(graus / 45) % 8]


def interp(pontos, x):
    x %= 360
    for (x0, y0), (x1, y1) in zip(pontos, pontos[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return pontos[-1][1]


def carregar_calibracao():
    return json.loads(CALIBRACAO.read_text(encoding="utf-8")) if CALIBRACAO.exists() else {}


def fator_direcao(graus, cal):
    bins = cal.get("fator_setor", {})
    return bins.get(setor(graus), interp(CONFIG["fator_direcao"], graus))


def media(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def media_vento(velocidades, direcoes):
    """Média vetorial do vento de vários modelos -> (km/h, direção de onde vem)."""
    pares = [(v, d) for v, d in zip(velocidades, direcoes) if v is not None and d is not None]
    if not pares:
        return None, None
    u = sum(v * math.sin(math.radians(d)) for v, d in pares) / len(pares)
    w = sum(v * math.cos(math.radians(d)) for v, d in pares) / len(pares)
    return math.hypot(u, w), math.degrees(math.atan2(u, w)) % 360


def componente_maral(vel, dir_):
    """Quanto do vento sopra do mar para a praia (km/h). Negativo = terral."""
    return vel * math.cos(math.radians(dir_ - CONFIG["praia_virada_para"]))


def fator_tamanho(altura):
    lo, hi = CONFIG["altura_ideal"]
    if altura < lo:
        return max((altura - 0.3) / (lo - 0.3), 0)  # 0.3 m = flat
    if altura <= hi:
        return 1.0
    return max(1 - (altura - hi) / 1.2 * 0.8, 0.2)  # cai conforme passa do seu nível


def nota(altura, periodo, vel, dir_):
    f_periodo = min(max((periodo - 4) / 6, 0.4), 1)  # 10 s ou mais = ótimo
    f_vento = 1.0
    if vel is not None and vel > CONFIG["vento_fraco_kmh"]:
        maral = componente_maral(vel, dir_)
        f_vento = 1.0 if maral <= 3 else max(0.15, 1 - (maral - 3) / 22)
    return round(5 * fator_tamanho(altura) * f_periodo * f_vento, 1)


# ---------------------------------------------------------------- textos

def tamanho_txt(h):
    for limite, txt in ((0.4, "flat / marola"), (0.6, "joelho a cintura"), (0.9, "cintura a peito"),
                        (1.2, "peito a ombro"), (1.6, "altura da cabeça"), (2.0, "acima da cabeça"),
                        (99, "bem acima da cabeça")):
        if h < limite:
            return txt


def prancha(h, periodo, vel, dir_):
    """-> (nome, descrição, pontos longboard 0-100, pontos pranchinha 0-100)"""
    mexido = vel is not None and vel > CONFIG["vento_fraco_kmh"] and componente_maral(vel, dir_) > 12
    hi, limite = CONFIG["altura_ideal"][1], CONFIG["altura_limite"]
    lon = 0 if h < 0.3 else min((h - 0.3) / 0.2, 1) if h < 0.5 else 1 if h <= 1.0 else max(1 - (h - 1.0) / 0.8, 0)
    if mexido:
        lon *= 0.6  # mar pequeno e mexido: long sofre na espuma
    pr = 0 if h < 0.5 else min((h - 0.5) / 0.4, 1) if h < 0.9 else 1 if h <= hi else max(1 - (h - hi) / 0.6 * 0.7, 0.3)
    pr *= 0.6 + 0.4 * min(max((periodo - 6) / 5, 0), 1)
    if h < 0.35:
        nome, desc = "Flat", "Sem onda pra surfar"
    elif h > limite + 0.3:
        nome, desc = "Acima do seu nível", "Melhor assistir da areia"
    elif pr >= lon:
        nome, desc = "Pranchinha", "Pesado pro seu condicionamento" if h > limite else "Onda com força e parede"
    else:
        nome, desc = "Longboard", "Onda pequena, boa pra remar"
    return nome, desc, round(lon * 100), round(pr * 100)


def vento_txt(vel, dir_):
    if vel is None:
        return "?"
    if vel <= CONFIG["vento_fraco_kmh"]:
        return "sem vento (liso)"
    maral = componente_maral(vel, dir_)
    if maral <= -vel * 0.5:
        return "terral (liso)"
    if maral <= 3:
        return "lateral"
    return "maral fraco (pouco mexido)" if maral <= 12 else "maral forte (mexido)"


def roupa(agua):
    if agua is None:
        return "?"
    for limite, txt in ((17, "long john 4/3"), (20, "long john 3/2"), (23, "john curto ou lycra")):
        if agua < limite:
            return txt
    return "bermuda"


# ---------------------------------------------------------------- dados

def buscar_ondas(extra):
    p = {"latitude": CONFIG["lat"], "longitude": CONFIG["lon"], "timezone": CONFIG["tz"],
         "hourly": ",".join(f"{v}_{x}" if v else x for v in ("", *PARTICOES)
                            for x in ("wave_height", "wave_period", "wave_direction")),
         "models": ",".join(CONFIG["modelos_onda"]), **extra}
    return get_json("https://marine-api.open-meteo.com/v1/marine", p)["hourly"]


def buscar_mar(extra):
    """Maré e temperatura da água (modelo padrão)."""
    p = {"latitude": CONFIG["lat"], "longitude": CONFIG["lon"], "timezone": CONFIG["tz"],
         "hourly": "sea_level_height_msl,sea_surface_temperature", **extra}
    return get_json("https://marine-api.open-meteo.com/v1/marine", p)["hourly"]


def buscar_vento(extra, url="https://api.open-meteo.com/v1/forecast"):
    p = {"latitude": CONFIG["lat"], "longitude": CONFIG["lon"], "timezone": CONFIG["tz"],
         "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,temperature_2m",
         "models": ",".join(CONFIG["modelos_vento"]), **extra}
    return get_json(url, p)["hourly"]


def montar_horas(ondas, vento, mar, cal):
    pesos = cal.get("peso_modelo", {})
    mare = mar["sea_level_height_msl"]
    horas = []
    for i, t in enumerate(ondas["time"]):
        por_modelo = {}
        for m in CONFIG["modelos_onda"]:
            h, p, d = (ondas[f"{v}_{m}"][i] for v in ("wave_height", "wave_period", "wave_direction"))
            if None not in (h, p, d):
                por_modelo[m] = {"h": h, "p": p, "d": d, "est": h * fator_direcao(d, cal)}
        if not por_modelo:
            continue
        w = {m: pesos.get(m, 1.0) for m in por_modelo}
        tot = sum(w.values())
        est = sum(por_modelo[m]["est"] * w[m] for m in por_modelo) / tot
        periodo = sum(por_modelo[m]["p"] * w[m] for m in por_modelo) / tot
        _, dir_onda = media_vento([1] * len(por_modelo), [x["d"] for x in por_modelo.values()])
        ventos = CONFIG["modelos_vento"]
        vel, dir_v = media_vento([vento[f"wind_speed_10m_{m}"][i] for m in ventos],
                                 [vento[f"wind_direction_10m_{m}"][i] for m in ventos])
        rajada = max((x for m in ventos if (x := vento[f"wind_gusts_10m_{m}"][i]) is not None), default=None)
        ar = media(vento[f"temperature_2m_{m}"][i] for m in ventos)
        agua = mar["sea_surface_temperature"][i]
        prox = mare[min(i + 1, len(mare) - 1)]
        nome_p, desc_p, pts_long, pts_pranchinha = prancha(est, periodo, vel, dir_v)
        ondulacoes = []
        for m in MODELOS_PARTICAO:
            for k, nome_o in PARTICOES.items():
                h, p, d = (ondas[f"{k}_{v}_{m}"][i] for v in ("wave_height", "wave_period", "wave_direction"))
                if None not in (h, p, d) and h >= 0.1 and p > 0:
                    ondulacoes.append({"nome": nome_o, "h": round(h, 1), "p": round(p), "d": round(d),
                                       "setor": setor(d)})
            if ondulacoes:
                break
        horas.append({
            "t": t, "est": round(est, 1), "periodo": round(periodo), "setor_onda": setor(dir_onda),
            "dir_onda": round(dir_onda), "ondulacoes": ondulacoes,
            "min": round(min(x["est"] for x in por_modelo.values()), 1),
            "max": round(max(x["est"] for x in por_modelo.values()), 1),
            "vel": vel and round(vel), "rajada": rajada and round(rajada),
            "dir_vento": dir_v and round(dir_v), "setor_vento": dir_v is not None and setor(dir_v),
            "mare": mare[i],
            "mare_txt": None if None in (mare[i], prox) else ("enchendo" if prox > mare[i] else "vazando"),
            "agua": agua and round(agua), "ar": ar and round(ar), "roupa": roupa(agua),
            "tamanho": tamanho_txt(est), "prancha": nome_p, "prancha_desc": desc_p,
            "pts_long": pts_long, "pts_pranchinha": pts_pranchinha,
            "vento_txt": vento_txt(vel, dir_v),
            "nota": nota(est, periodo, vel, dir_v),
        })
    return horas


# ---------------------------------------------------------------- comandos

def cmd_previsao():
    cal = carregar_calibracao()
    extra = {"forecast_days": CONFIG["dias"]}
    horas = montar_horas(buscar_ondas(extra), buscar_vento(extra), buscar_mar(extra), cal)
    dias = defaultdict(list)
    for h in horas:
        if CONFIG["janela"][0] <= int(h["t"][11:13]) <= CONFIG["janela"][1]:
            dias[h["t"][:10]].append(h)
    resumo = [{"dia": dia, "melhor": max(hs, key=lambda h: h["nota"])}
              for dia, hs in dias.items()]
    dados = {"nome": CONFIG["nome"], "local": CONFIG["local"], "gerado": datetime.now(BRT).strftime("%d/%m %H:%M"),
             "horas": horas, "ideal_inicio": CONFIG["janela"][0], "ideal_fim": CONFIG["janela"][1],
             "calibrado": bool(cal.get("fator_setor") or cal.get("peso_modelo")),
             "n_sessoes": cal.get("n_sessoes", 0)}
    mapa = MAPA.read_text(encoding="utf-8") if MAPA.exists() else ""
    SAIDA.write_text(HTML.replace("__DADOS__", json.dumps(dados)).replace("__MAPA__", mapa), encoding="utf-8")
    print(f"Gerado {SAIDA}")
    for d in resumo:
        m = d["melhor"]
        print(f"  {d['dia']} {m['t'][11:13]}h  nota {m['nota']}  ~{m['est']} m ({m['tamanho']})  "
              f"{m['prancha']}  vento: {m['vento_txt']}  água {m['agua']}°C")
    if not os.environ.get("CI"):  # no GitHub Actions não tem navegador
        webbrowser.open(SAIDA.as_uri())


def cmd_log(args):
    pontos = {pt["id"]: pt["nome"] for pt in json.loads(PONTOS.read_text(encoding="utf-8"))["pontos"]}         if PONTOS.exists() else {}
    ap = argparse.ArgumentParser(prog="surf.py log")
    ap.add_argument("nota", type=int, choices=range(1, 6), help="1-5, pensando no seu nível")
    ap.add_argument("altura", type=float, help="altura real em metros")
    ap.add_argument("quando", nargs="?", default="", help='"AAAA-MM-DD HH" (vazio = agora)')
    ap.add_argument("obs", nargs="?", default="")
    ap.add_argument("--levanta", type=int, choices=pontos or None, help="ponto do mapa onde a onda levanta")
    ap.add_argument("--morre", type=int, choices=pontos or None, help="ponto do mapa onde a onda perde força")
    ap.add_argument("--substituir", action="store_true", help="troca a sessão já registrada nessa hora")
    a = ap.parse_args(args)
    quando = datetime.strptime(a.quando, "%Y-%m-%d %H") if a.quando else datetime.now()
    data_hora = quando.strftime("%Y-%m-%dT%H:00")

    linhas = []
    if SESSOES.exists():
        with SESSOES.open(encoding="utf-8") as f:
            linhas = list(csv.DictReader(f))
    if any(r["data_hora"] == data_hora for r in linhas):
        if not a.substituir:
            sys.exit(f"Já existe sessão em {quando:%d/%m %Hh}. Use --substituir para trocar.")
        linhas = [r for r in linhas if r["data_hora"] != data_hora]
    linhas.append({"data_hora": data_hora, "nota": a.nota, "altura_m": a.altura, "obs": a.obs,
                   "ponto_levanta": a.levanta or "", "ponto_morre": a.morre or ""})
    linhas.sort(key=lambda r: r["data_hora"])
    with SESSOES.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS_SESSAO, restval="")
        w.writeheader()
        w.writerows(linhas)
    onde = "".join(f", {k} no {v} ({pontos.get(v, '?')})" for k, v in (("levanta", a.levanta), ("morre", a.morre)) if v)
    print(f"Sessão registrada: {quando:%d/%m %Hh} nota {a.nota}, {a.altura} m{onde}")


def cmd_calibrar():
    if not SESSOES.exists():
        sys.exit("Nenhuma sessão registrada ainda. Use: python surf.py log <nota> <altura>")
    with SESSOES.open(encoding="utf-8") as f:
        sessoes = list(csv.DictReader(f))
    erros, razoes, linhas = defaultdict(list), defaultdict(list), []
    for s in sessoes:
        dia, hora = s["data_hora"][:10], s["data_hora"]
        extra = {"start_date": dia, "end_date": dia}
        ondas = buscar_ondas(extra)
        vento = buscar_vento(extra, "https://historical-forecast-api.open-meteo.com/v1/forecast")
        mare = buscar_mar(extra)["sea_level_height_msl"]
        i = ondas["time"].index(hora)
        real = float(s["altura_m"])
        por_setor = defaultdict(list)
        for m in CONFIG["modelos_onda"]:
            h, d = ondas[f"wave_height_{m}"][i], ondas[f"wave_direction_{m}"][i]
            if h is None or d is None:
                continue
            erros[m].append(abs(h * fator_direcao(d, {}) - real))
            por_setor[setor(d)].append(real / h)
        for sec, r in por_setor.items():  # uma amostra por sessão em cada setor
            razoes[sec].append(statistics.mean(r))
        vel, dir_v = media_vento([vento[f"wind_speed_10m_{m}"][i] for m in CONFIG["modelos_vento"]],
                                 [vento[f"wind_direction_10m_{m}"][i] for m in CONFIG["modelos_vento"]])
        if vel is not None:
            linhas.append((int(s["nota"]), componente_maral(vel, dir_v), mare[i]))

    mae = {m: round(statistics.mean(e), 2) for m, e in erros.items()}
    print(f"\n{len(sessoes)} sessões\n\nErro médio de altura por modelo (m):")
    for m, e in sorted(mae.items(), key=lambda x: x[1]):
        print(f"  {NOMES_ONDA[m]:12} {e}")
    print("\nAltura real ÷ prevista por direção do swell:")
    fator_setor = {}
    for sec in SETORES:
        if r := razoes.get(sec):
            usado = len(r) >= 2  # só substitui a hipótese com pelo menos 2 sessões
            print(f"  {sec:3} {statistics.mean(r):.2f}  ({len(r)} sessões){'' if usado else ' — precisa de 2+'}")
            if usado:
                fator_setor[sec] = round(statistics.mean(r), 2)
    if len(linhas) >= 3:
        notas, marais, mares = zip(*linhas)
        for nome, xs in (("vento maral", marais), ("maré", [m or 0 for m in mares])):
            try:
                print(f"\nCorrelação nota x {nome}: {statistics.correlation(notas, xs):+.2f}")
            except statistics.StatisticsError:
                pass
    CALIBRACAO.write_text(json.dumps({
        "n_sessoes": len(sessoes), "mae": mae, "fator_setor": fator_setor,
        # pesos por modelo só depois de algumas sessões, senão uma sessão decide tudo
        "peso_modelo": {m: round(1 / max(e, 0.05), 2) for m, e in mae.items()} if len(sessoes) >= 3 else {},
    }, indent=2), encoding="utf-8")
    print(f"\nSalvo em {CALIBRACAO}")


HTML = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="1800">
<title>Barrinha Forecast</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,800&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root{--bg:#0B1B2B;--card:#12283B;--line:#1E3A50;--dim:#2A4A62;--fg:#EAF2F1;--muted:#8FA6B2;--teal:#5FD1C2;--accent:#FF8A3D;--yellow:#FFC23D;--bad:#F2726B}
*{box-sizing:border-box}
html,body{margin:0;background:#07131F;color:var(--fg)}
body{font-family:'Bricolage Grotesque',system-ui,sans-serif;padding:24px 16px}
.mono{font-family:'IBM Plex Mono',monospace}
.board{max-width:1440px;margin:0 auto;padding:32px;background:var(--bg);border-radius:40px;display:flex;flex-direction:column;gap:18px}
header{display:flex;justify-content:space-between;align-items:flex-end;gap:24px;flex-wrap:wrap}
.eyebrow{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
h1{margin:0;font-size:40px;font-weight:800;letter-spacing:-.02em;line-height:1}
.pill{display:flex;align-items:center;gap:8px;padding:10px 18px;border-radius:999px;background:var(--accent);color:var(--bg);font-weight:600;font-size:16px;white-space:nowrap}
.dias{display:flex;gap:8px;overflow-x:auto;padding-bottom:2px}
.dia{flex:0 0 auto;display:flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;background:var(--card);border:1px solid var(--line);color:var(--fg);font:inherit;font-size:14px;cursor:pointer}
.dia .mono{font-size:12px;color:var(--muted)}
.dia.on{background:var(--accent);border-color:var(--accent);color:var(--bg)}.dia.on .mono{color:var(--bg)}
.grid{display:grid;gap:16px;grid-template-columns:250px minmax(0,1fr) 250px;
  grid-template-areas:"alt mapa vento" "prancha mapa mare" "horario horario horario"}
section{background:var(--card);border:1px solid var(--line);border-radius:24px;padding:18px 20px;display:flex;flex-direction:column;justify-content:space-between;gap:10px;min-width:0}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px}
.mapa{grid-area:mapa;position:relative;border-radius:24px;overflow:hidden;background:#0A1A29;border:1px solid var(--line);display:flex;align-items:center}
.mapa svg{display:block;width:100%;height:auto}
@media (min-width:1101px){.mapa{display:block}.mapa svg{height:100%}}
.mapa #mapa-titulo,.mapa #swell-fixo{display:none}
.legenda{position:absolute;top:12px;right:64px;background:rgba(11,27,43,.92);border:1px solid var(--line);border-radius:14px;padding:10px 14px;font:12px/1.7 'IBM Plex Mono',monospace;color:var(--fg)}
.legenda .t{font-size:10px;letter-spacing:.12em;color:var(--muted)}
.legenda i{display:inline-block;width:18px;height:0;border-top:4px solid;border-radius:2px;margin-right:8px;vertical-align:middle}
.bars{flex:1;display:flex;align-items:flex-end;gap:5px;height:76px}
.bars>div{flex:1;display:flex;flex-direction:column;align-items:center;gap:5px}
.bars i{display:block;width:100%;border-radius:5px}
.bars span{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--muted)}
.meter{height:6px;border-radius:3px;background:var(--bg)}.meter i{display:block;height:6px;border-radius:3px}
.small{font-size:12px;color:var(--muted);line-height:1.5}
.foot{font-size:13px;color:var(--muted);text-align:center}
@media (max-width:1100px){
  .grid{grid-template-columns:1fr 1fr;grid-template-areas:"mapa mapa" "alt vento" "prancha mare" "horario horario"}
}
@media (max-width:640px){
  body{padding:0}.board{border-radius:0;padding:22px 14px}h1{font-size:32px}
  .grid{grid-template-columns:minmax(0,1fr);grid-template-areas:"mapa" "alt" "vento" "prancha" "mare" "horario"}
  .mapa{overflow-x:auto;display:block}.mapa svg{min-width:720px}
  .horario{flex-direction:column;align-items:stretch!important}
  #falarTxt{display:none}
}
/* modo tela: celular deitado no quiosque, tudo numa tela só, sem rolagem */
@media (orientation:landscape) and (max-height:520px){
  html,body{height:100%;overflow:hidden}body{padding:0}
  .board{height:100vh;max-width:none;border-radius:0;padding:8px 10px;gap:6px;
    display:grid;grid-template-columns:auto minmax(0,1fr);grid-template-rows:auto minmax(0,1fr)}
  header{flex-wrap:nowrap;align-items:center;gap:10px}
  #local,#quando,.foot{display:none}
  h1{font-size:18px}
  .pill{padding:3px 10px;font-size:11px;gap:5px}.pill svg{width:12px;height:12px}
  .dias{justify-self:end;max-width:100%;gap:3px;align-self:center}
  .dia{padding:3px 6px;font-size:10px;gap:3px}.dia .mono{font-size:9px}
  .grid{grid-column:1/-1;min-height:0;gap:6px;
    grid-template-columns:54% minmax(0,1.15fr) minmax(0,1fr);grid-template-rows:minmax(0,1fr) minmax(0,1fr) auto;
    grid-template-areas:"mapa alt vento" "mapa prancha mare" "mapa horario horario"}
  .mapa{display:block;border-radius:14px;overflow:hidden}.mapa svg{height:100%;min-width:0}
  .legenda{top:auto;bottom:6px;right:auto;left:6px;padding:4px 7px;border-radius:8px;font-size:8.5px;line-height:1.55}
  .legenda .t{font-size:7.5px}.legenda i{width:10px;border-top-width:3px;margin-right:4px}
  section{padding:7px 9px;border-radius:14px;gap:2px;overflow:hidden}
  .eyebrow{font-size:8.5px}
  .small{font-size:9.5px;line-height:1.35}
  #altura{font-size:38px!important}#altura+span{font-size:15px!important}
  #nota{font-size:11px!important}#tendencia{font-size:9.5px!important}
  #prancha{font-size:17px!important}.row{font-size:11px!important}
  .meter,.meter i{height:4px}#medidores{gap:3px!important}
  #bussola{width:42px;height:42px}#vel{font-size:24px!important}#dirVento{font-size:11px!important}
  #ventoTipo{padding:2px 7px!important}
  section .row,#ventoTipo,#rajada,#extremos span,#janela{white-space:nowrap}
  #rajada{font-size:9.5px}
  #mare{font-size:16px!important}#mareSeta{width:14px;height:14px}#mareNivel{font-size:10px!important}
  #curva{height:26px}#extremos{font-size:8.5px!important;gap:4px}
  .horario{flex-direction:row!important;align-items:flex-end!important;gap:10px!important}
  .horario>div:first-child{min-width:0!important;gap:1px!important}
  #janela{font-size:17px!important}#motivo{font-size:9px}
  .bars{height:38px;gap:2px}.bars span{font-size:7.5px}
}
</style></head><body>
<div class="board">
  <header>
    <div style="display:flex;flex-direction:column;gap:4px">
      <div class="eyebrow" style="font-size:13px;letter-spacing:.12em" id="local"></div>
      <h1 id="nome"></h1>
    </div>
    <div style="display:flex;align-items:center;gap:16px">
      <div class="mono" style="font-size:13px;color:var(--muted);text-align:right;line-height:1.5" id="quando"></div>
      <button class="pill" id="falar" aria-label="Ouvir a previsão" style="background:var(--card);color:var(--fg);border:1px solid var(--line);font:inherit;cursor:pointer"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a10 10 0 0 1 0 14"/></svg><span id="falarTxt">Ouvir</span></button>
      <div class="pill" id="status"></div>
    </div>
  </header>
  <nav class="dias" id="dias"></nav>

  <div class="grid">
    <section style="grid-area:alt">
      <div class="row"><div class="eyebrow" id="hAltura"></div>
        <span style="font-size:13px;font-weight:600" id="nota"></span></div>
      <div style="display:flex;align-items:baseline;gap:6px">
        <span style="font-size:72px;font-weight:800;letter-spacing:-.05em;line-height:.85" id="altura"></span>
        <span style="font-size:24px;font-weight:600;color:var(--muted)">m</span></div>
      <div style="display:flex;gap:4px" id="notaBarras"></div>
      <div class="mono" style="font-size:12px;color:var(--teal)" id="tendencia"></div>
      <div class="small" id="detalhes"></div>
    </section>

    <section style="grid-area:prancha">
      <div class="eyebrow">Prancha ideal</div>
      <div><div style="font-size:26px;font-weight:800;letter-spacing:-.02em;line-height:1" id="prancha"></div>
        <div class="small" style="margin-top:4px" id="pranchaDesc"></div></div>
      <div style="display:flex;flex-direction:column;gap:8px" id="medidores"></div>
    </section>

    <div class="mapa"><div class="legenda" id="legenda"></div>__MAPA__</div>

    <section style="grid-area:vento">
      <div class="eyebrow">Vento</div>
      <div style="display:flex;align-items:center;gap:14px">
        <svg width="72" height="72" viewBox="0 0 96 96" fill="none" role="img" id="bussola">
          <circle cx="48" cy="48" r="42" stroke="#2A4A62" stroke-width="2"/>
          <circle cx="48" cy="48" r="30" stroke="#1E3A50" stroke-width="1" stroke-dasharray="3 4"/>
          <text x="48" y="15" text-anchor="middle" font-family="IBM Plex Mono, monospace" font-size="11" fill="#8FA6B2">N</text>
          <g id="seta" stroke="#5FD1C2" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round">
            <line x1="48" y1="24" x2="48" y2="72"/><polyline points="38,62 48,72 58,62"/></g>
        </svg>
        <div><div style="display:flex;align-items:baseline;gap:4px"><span style="font-size:38px;font-weight:800;letter-spacing:-.03em;line-height:1" id="vel"></span><span style="font-size:13px;color:var(--muted)">km/h</span></div>
          <div style="font-size:14px;font-weight:600" id="dirVento"></div></div>
      </div>
      <div class="row" style="font-size:13px">
        <span style="padding:5px 11px;border-radius:999px;background:var(--bg);font-weight:600" id="ventoTipo"></span>
        <span style="color:var(--muted)" id="rajada"></span></div>
    </section>

    <section style="grid-area:mare">
      <div class="eyebrow">Maré</div>
      <div style="display:flex;align-items:center;gap:8px">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#5FD1C2" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" id="mareSeta"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5,12 12,5 19,12"/></svg>
        <span style="font-size:26px;font-weight:800;letter-spacing:-.02em;line-height:1" id="mare"></span>
        <span class="mono" style="font-size:13px;color:var(--muted)" id="mareNivel"></span></div>
      <svg width="100%" height="48" viewBox="0 0 400 120" preserveAspectRatio="none" fill="none" role="img" aria-label="Curva da maré do dia" id="curva"></svg>
      <div class="row mono" style="font-size:11px" id="extremos"></div>
    </section>

    <section class="horario" style="grid-area:horario;flex-direction:row;align-items:flex-end;gap:28px">
      <div style="display:flex;flex-direction:column;gap:6px;min-width:250px">
        <div class="eyebrow">Melhor horário</div>
        <div style="font-size:38px;font-weight:800;letter-spacing:-.03em;line-height:1" id="janela"></div>
        <div class="small" id="motivo"></div>
      </div>
      <div class="bars" id="barras"></div>
    </section>
  </div>
  <div class="foot" id="rodape"></div>
</div>
<script>
const D = __DADOS__;
const $ = id => document.getElementById(id);
const num = (x, d = 1) => x == null ? '?' : x.toFixed(d).replace('.', ',');
const SEM = ['Dom','Seg','Ter','Qua','Qui','Sex','Sáb'];
const MES = ['jan','fev','mar','abr','mai','jun','jul','ago','set','out','nov','dez'];
const PT = {N:'N',NE:'NE',E:'L',SE:'SE',S:'S',SW:'SO',W:'O',NW:'NO'};
const COR_OND = {'Principal':'#FF8A3D','Secundária':'#FFC23D','Vaga de vento':'#EAF2F1'};
const dataDe = d => new Date(d + 'T12:00');
const tipo = t => t.replace(/ \(.*\)/, '');

const porDia = {};
D.horas.forEach(h => (porDia[h.t.slice(0,10)] ??= []).push(h));
const dias = Object.keys(porDia).filter(d => porDia[d].length === 24);
const agora = new Date();
const hojeISO = `${agora.getFullYear()}-${String(agora.getMonth()+1).padStart(2,'0')}-${String(agora.getDate()).padStart(2,'0')}`;
const surfaveis = d => porDia[d].filter(h => { const hr = +h.t.slice(11,13); return hr >= D.ideal_inicio && hr <= D.ideal_fim; });
const melhor = d => surfaveis(d).reduce((a, b) => b.nota > a.nota ? b : a);

$('local').textContent = D.local;
$('nome').textContent = D.nome;
$('rodape').textContent = D.calibrado ? `Calibrado com ${D.n_sessoes} sessões` :
  `${D.n_sessoes} sessão(ões) registrada(s) · ainda usando as regras iniciais do molhe`;
const svgMapa = document.querySelector('.mapa svg');
const TELA = matchMedia('(orientation: landscape) and (max-height: 520px)');  // celular deitado (quiosque)
const enquadrar = () => {  // corta a faixa de terra à esquerda e preenche o card
  if (!svgMapa) return;
  let vb = '250 0 1371 662', ajuste = 'xMidYMid slice';
  if (TELA.matches) {  // largura acompanha o formato do card, centrada nos pontos (x 440–1045)
    const r = svgMapa.parentElement.getBoundingClientRect(), MIN = 780;
    const w = Math.min(1100, Math.max(MIN, 662 * r.width / r.height));
    vb = `${745 - w / 2} 0 ${w} 662`;
    if (w === MIN) ajuste = 'xMidYMid meet';  // card estreito: sobra mar em cima/embaixo, mas nenhum ponto some
  }
  svgMapa.setAttribute('viewBox', vb);
  svgMapa.setAttribute('preserveAspectRatio', ajuste);
};
enquadrar();
addEventListener('resize', enquadrar);

$('dias').innerHTML = dias.map(d => {
  const dt = dataDe(d), m = melhor(d);
  return `<button class="dia" data-d="${d}">${d === hojeISO ? 'Hoje' : SEM[dt.getDay()] + ' ' + d.slice(8)}<span class="mono">${num(m.nota)}</span></button>`;
}).join('');
$('dias').onclick = e => { const b = e.target.closest('.dia'); if (b) mostrar(b.dataset.d); };

function mostrar(d) {
  document.querySelectorAll('.dia').forEach(b => b.classList.toggle('on', b.dataset.d === d));
  const horas = porDia[d], m = melhor(d), dt = dataDe(d);
  const ehHoje = d === hojeISO && agora.getHours() <= D.ideal_fim;
  const h = ehHoje ? horas[agora.getHours()] : m;  // hoje: agora; outros dias: melhor hora
  $('quando').innerHTML = `${SEM[dt.getDay()]}, ${d.slice(8)} ${MES[dt.getMonth()]}<br>Atualizado ${D.gerado}`;

  const st = h.prancha === 'Acima do seu nível' ? ['Muito grande', 'var(--bad)'] : h.prancha === 'Flat' ? ['Flat', 'var(--muted)'] :
    h.nota >= 3 ? ['Bom pra cair', 'var(--accent)'] : h.nota >= 1.5 ? ['Dá pra cair', 'var(--yellow)'] : ['Fraco', 'var(--muted)'];
  $('status').style.background = st[1];
  $('status').innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12c2-3 4-3 6 0s4 3 6 0 4-3 6 0"/><path d="M2 18c2-3 4-3 6 0s4 3 6 0 4-3 6 0"/></svg>${st[0]}`;

  // altura
  $('hAltura').textContent = ehHoje ? 'Altura agora' : `Altura às ${h.t.slice(11,13)}h`;
  const cheias = Math.round(h.nota);
  $('notaBarras').innerHTML = [0,1,2,3,4].map(i => `<div style="flex:1;height:6px;border-radius:3px;background:${i < cheias ? 'var(--accent)' : 'var(--dim)'}"></div>`).join('');
  $('nota').textContent = `${num(h.nota)}/5`;
  $('altura').textContent = num(h.est);
  const i = D.horas.indexOf(h), depois = D.horas[Math.min(i + 3, D.horas.length - 1)];
  const dif = depois.est - h.est;
  $('tendencia').textContent = `${dif > 0.1 ? '▲ subindo' : dif < -0.1 ? '▼ baixando' : '● estável'} · ${num(h.min)}–${num(h.max)} m`;
  $('detalhes').innerHTML = `${h.tamanho} · ${h.periodo} s · ar ${h.ar ?? '?'} °C<br>Água ${h.agua ?? '?'} °C · ${h.roupa}`;

  // prancha
  $('prancha').textContent = h.prancha;
  $('pranchaDesc').textContent = h.prancha_desc;
  $('medidores').innerHTML = [['Pranchinha', h.pts_pranchinha, 'var(--accent)'], ['Longboard', h.pts_long, 'var(--teal)']]
    .sort((x, y) => y[1] - x[1]).map(([n, v, c], k) => `<div style="display:flex;flex-direction:column;gap:4px">
    <div class="row" style="font-size:13px;${k ? 'color:var(--muted)' : ''}"><span>${n}</span><span class="mono">${v}</span></div>
    <div class="meter"><i style="width:${v}%;background:${c}"></i></div></div>`).join('');

  // vento (seta aponta para onde o vento sopra)
  const vt = tipo(h.vento_txt), ruim = vt.startsWith('maral');
  $('seta').setAttribute('transform', `rotate(${h.dir_vento ?? 0} 48 48)`);
  $('seta').setAttribute('stroke', ruim ? 'var(--bad)' : 'var(--teal)');
  $('bussola').setAttribute('aria-label', `Vento de ${PT[h.setor_vento] ?? '?'}`);
  $('vel').textContent = h.vel ?? '?';
  $('dirVento').textContent = `${PT[h.setor_vento] ?? '?'} ${h.dir_vento ?? ''}°`;
  $('ventoTipo').textContent = vt[0].toUpperCase() + vt.slice(1);
  $('ventoTipo').style.color = ruim ? 'var(--bad)' : 'var(--teal)';
  $('rajada').textContent = h.rajada != null ? `Rajadas ${h.rajada}` : '';

  // maré
  const enchendo = h.mare_txt === 'enchendo';
  $('mare').textContent = enchendo ? 'Enchendo' : 'Vazando';
  $('mareSeta').style.transform = enchendo ? '' : 'rotate(180deg)';
  $('mareNivel').textContent = `${num(h.mare)} m`;
  const niv = horas.map(x => x.mare ?? 0), lo = Math.min(...niv), hi = Math.max(...niv);
  const y = v => 105 - (v - lo) / ((hi - lo) || 1) * 90, x = k => k * 400 / 23;
  const ext = cmp => niv.map((v, k) => k > 0 && k < 23 && cmp(v, niv[k-1]) && cmp(v, niv[k+1]) ? k : -1).filter(k => k >= 0);
  const desde = +h.t.slice(11,13), prox = ks => ks.find(k => k >= desde) ?? ks[0];
  const pm = prox(ext((v, w) => v >= w)), bm = prox(ext((v, w) => v <= w));
  const fmt = k => k == null ? '–' : `${String(k).padStart(2,'0')}h ${num(niv[k])} m`;
  $('extremos').innerHTML = `<span><span style="color:var(--muted)">▲ </span>${fmt(pm)}</span><span><span style="color:var(--muted)">▼ </span>${fmt(bm)}</span>`;
  $('curva').innerHTML = `<polyline points="${niv.map((v, k) => `${x(k).toFixed(1)},${y(v).toFixed(1)}`).join(' ')}" stroke="#5FD1C2" stroke-width="2.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
    <line x1="${x(desde)}" y1="0" x2="${x(desde)}" y2="120" stroke="#EAF2F1" stroke-width="1" stroke-dasharray="3 3" vector-effect="non-scaling-stroke"/>
    <line x1="${x(desde)}" y1="${y(niv[desde])}" x2="${x(desde) + 0.01}" y2="${y(niv[desde])}" stroke="#FF8A3D" stroke-width="11" stroke-linecap="round" vector-effect="non-scaling-stroke"/>`;

  // melhor horário: horas vizinhas com nota próxima da melhor
  const surf = surfaveis(d), bi = surf.indexOf(m), lim = Math.max(m.nota - 0.4, m.nota * 0.85);
  let a = bi, b = bi;
  while (a > 0 && surf[a-1].nota >= lim) a--;
  while (b < surf.length - 1 && surf[b+1].nota >= lim) b++;
  const ini = +surf[a].t.slice(11,13), fim = +surf[b].t.slice(11,13) + 1;
  $('janela').textContent = `${String(ini).padStart(2,'0')}:00 – ${String(fim).padStart(2,'0')}:00`;
  const vm = tipo(m.vento_txt);
  $('motivo').textContent = `${vm === 'sem vento' ? 'Sem vento' : 'Vento ' + vm} e maré ${m.mare_txt ?? '?'} · ${m.prancha.toLowerCase()}`;
  const max = Math.max(...surf.map(x => x.nota), 0.1);
  $('barras').innerHTML = surf.map((x, k) => {
    const on = k >= a && k <= b, cor = on ? 'var(--accent)' : x.nota >= lim * 0.75 ? 'var(--dim)' : 'var(--line)';
    return `<div title="${x.t.slice(11,13)}h · nota ${num(x.nota)} · ${num(x.est)} m"><i style="height:${Math.max(4, (TELA.matches ? 24 : 60) * x.nota / max)}px;background:${cor}"></i>
      <span style="${on ? 'color:var(--fg)' : ''}">${x.t.slice(11,13)}</span></div>`;
  }).join('');

  fala = `Previsão para ${d === hojeISO ? 'hoje' : DIA[dt.getDay()] + ', dia ' + +d.slice(8)}. ` +
    `${ehHoje ? 'Agora' : 'Às ' + +h.t.slice(11,13) + ' horas'}: ${st[0].toLowerCase()}, ondas de ${num(h.est)} metros, nota ${num(h.nota)} de 5. ` +
    `Vento ${vt}, ${h.vel ?? '?'} quilômetros por hora. Maré ${enchendo ? 'enchendo' : 'vazando'}. ` +
    `Melhor horário das ${ini} às ${fim} horas. Prancha: ${m.prancha.toLowerCase()}.`;
  desenharMapa(h);
}

const DIA = ['domingo','segunda','terça','quarta','quinta','sexta','sábado'];
let fala = '';
if (!('speechSynthesis' in window)) $('falar').hidden = true;
$('falar').onclick = () => {  // toca de novo = para
  if (speechSynthesis.speaking) return speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(fala);
  u.lang = 'pt-BR';
  speechSynthesis.speak(u);
};

// setas das ondulações e do vento no mapa (norte para cima; direção = de onde vem)
function desenharMapa(h) {
  if (!svgMapa) return;
  let g = svgMapa.querySelector('#mapa-dyn');
  if (!g) {
    g = document.createElementNS('http://www.w3.org/2000/svg', 'g'); g.id = 'mapa-dyn';
    (svgMapa.querySelector('g[clip-path]') || svgMapa).insertBefore(g, svgMapa.querySelector('#pontos'));
  }
  const vec = d => [Math.sin(d * Math.PI / 180), -Math.cos(d * Math.PI / 180)];
  const seta = (x1, y1, x2, y2, cor, w, op = 1, tracejado = '') => {
    const ang = Math.atan2(y2 - y1, x2 - x1), c = 9 + w * 2.5;
    const p = s => `${(x2 - c * Math.cos(ang + s)).toFixed(1)},${(y2 - c * Math.sin(ang + s)).toFixed(1)}`;
    return `<g stroke="${cor}" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round" fill="none" opacity="${op}">
      <line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke-dasharray="${tracejado}"/><polyline points="${p(0.5)} ${x2},${y2} ${p(-0.5)}"/></g>`;
  };
  const onds = h.ondulacoes.length ? h.ondulacoes : [{nome: 'Principal', h: h.est, p: h.periodo, d: h.dir_onda, setor: h.setor_onda}];
  let out = '';
  onds.forEach((o, k) => {
    const cor = COR_OND[o.nome], [ux, uy] = vec(o.d), L = 330 - k * 45;
    const desloc = [0, -30, 30][k] ?? 0;                      // separa setas paralelas
    const alvo = [935 - uy * desloc, 300 + ux * desloc];
    const ini = [alvo[0] + ux * L, alvo[1] + uy * L], w = Math.min(2 + o.h * 3, 6);
    if (k === 0)  // cristas só na principal, pra não embolar
      for (const t of [0.25, 0.5, 0.75]) {
        const cx = ini[0] - ux * L * t, cy = ini[1] - uy * L * t, r = 26 + 18 * t;
        out += `<path d="M${cx - uy * r},${cy + ux * r} Q${cx - ux * 10},${cy - uy * 10} ${cx + uy * r},${cy - ux * r}"
          stroke="${cor}" stroke-width="${w * 0.7}" fill="none" stroke-linecap="round" opacity="${0.3 + 0.4 * t}"/>`;
      }
    out += seta(ini[0], ini[1], alvo[0] + ux * 45, alvo[1] + uy * 45, cor, w, 1, o.nome === 'Vaga de vento' ? '8 8' : '');
  });
  const ruim = h.vento_txt.startsWith('maral'), corV = ruim ? '#F2726B' : '#5FD1C2';
  if (h.vel != null && h.dir_vento != null) {
    const [vx, vy] = vec(h.dir_vento), Lv = 30 + Math.min(h.vel, 40) * 1.5;
    for (const [x, y] of [[620, 70], [1010, 110], [1200, 300], [1480, 330], [700, 610]])
      out += seta(x + vx * Lv / 2, y + vy * Lv / 2, x - vx * Lv / 2, y - vy * Lv / 2, corV, 3, 0.85);
  }
  g.innerHTML = out;
  // legenda em HTML (legível mesmo com o mapa pequeno)
  const linhas = [...onds.map(o => [COR_OND[o.nome], `${o.nome} ${PT[o.setor]} ${o.d}° · ${num(o.h)} m · ${o.p} s`, o.nome === 'Vaga de vento']),
                  [corV, `Vento ${PT[h.setor_vento] ?? '?'} ${h.vel ?? '?'} km/h · ${tipo(h.vento_txt)}`, false]];
  $('legenda').innerHTML = `<div class="t">MAR ABERTO · ${h.t.slice(11,13)}H ${h.t.slice(8,10)}/${h.t.slice(5,7)}</div>` +
    linhas.map(([c, t, tr]) => `<div><i style="border-color:${c};border-top-style:${tr ? 'dashed' : 'solid'}"></i>${t}</div>`).join('');
}

mostrar(dias.includes(hojeISO) ? hojeISO : dias[0]);
</script></body></html>
"""

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "previsao"
    {"previsao": lambda: cmd_previsao(), "log": lambda: cmd_log(sys.argv[2:]),
     "calibrar": lambda: cmd_calibrar()}.get(cmd, lambda: sys.exit(__doc__))()
