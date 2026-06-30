#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FACEIT Match History — локальная веб-версия.

Поднимает лёгкий локальный сервер (только стандартная библиотека) и открывает
современный интерфейс в браузере. Сервер проксирует FACEIT Data API (из браузера
напрямую нельзя — нет CORS) и переиспользует логику из faceit_core.py.

API-ключ вводится в интерфейсе и хранится только в браузере пользователя
(localStorage) — на сервер он попадает лишь как заголовок запроса к FACEIT и
нигде не сохраняется.

Запуск:   python faceit_web.py
Зависимости:  pip install requests
"""

import json
import socket
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import faceit_core as core

HOST = "127.0.0.1"
DEFAULT_PORT = 8731


# ---------------------------------------------------------------------------
# Поисковая логика поверх ядра — отдаёт события через emit(dict)
# ---------------------------------------------------------------------------
def build_period(p: dict) -> dict:
    mode = (p or {}).get("mode", "last")
    if mode == "dates":
        frm = core.parse_date_to_unix(p.get("from", ""))
        to = core.parse_date_to_unix(p.get("to", ""), end_of_day=True)
        if not frm and not to:
            raise ValueError("Укажите хотя бы одну дату (От или До).")
        if frm and to and frm > to:
            raise ValueError("Дата «От» позже даты «До».")
        cap = int(p.get("max") or core.DATE_MODE_MAX)
        return {"mode": "dates", "from": frm, "to": to, "max": max(1, cap)}
    if mode == "offset":
        start = int(p.get("start") or 0)
        end = int(p.get("end") or 0)
        if end <= start:
            raise ValueError("«По №» должно быть больше «С матча №».")
        return {"mode": "offset", "start": max(0, start), "end": end}
    return {"mode": "last", "limit": max(1, int(p.get("limit") or 20))}


def run_search(api_key, players, game, period, emit, cancel):
    client = core.Client(api_key, cancel=cancel,
                         on_count=lambda n: emit({"type": "count", "n": n}))

    emit({"type": "log", "level": "head",
          "msg": f"Игроков: {len(players)} | игра: {game}"})

    player_ids = {}
    for raw in players:
        nick = core.nickname_from_input(raw)
        pid = core.get_player_id(client, nick)
        player_ids[nick] = pid
        emit({"type": "log", "level": "info", "msg": f"  {nick} → {pid}"})
    searched_ids = list(player_ids.values())

    match_dates, order = {}, []
    for nick, pid in player_ids.items():
        emit({"type": "status", "msg": f"История: {nick}…"})
        hist = core.get_history(client, pid, game, period)
        emit({"type": "log", "level": "info",
              "msg": f"  {nick}: матчей в истории — {len(hist)}"})
        for it in hist:
            mid = it.get("match_id")
            if mid and mid not in match_dates:
                match_dates[mid] = it.get("finished_at") or it.get("started_at")
                order.append(mid)

    total = len(order)
    emit({"type": "log", "level": "head", "msg": f"Уникальных матчей: {total}"})
    emit({"type": "total", "total": total})

    results = []
    for i, mid in enumerate(order, 1):
        if cancel.is_set():
            break
        emit({"type": "progress", "i": i, "total": total})
        m = core.parse_match(client, mid)
        if m:
            m["date"] = core.unix_to_str(match_dates[mid])
            results.append(m)

    stats = core.compute_stats(results, set(searched_ids))
    party = sorted(core.party_nicknames(stats))
    emit({"type": "done", "results": results, "stats": stats,
          "searched_ids": searched_ids, "party_nicks": party,
          "request_count": client.count})


# ---------------------------------------------------------------------------
# HTTP-сервер
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # тихо

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/search":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self.send_error(400, "bad json")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        cancel = threading.Event()

        def emit(obj):
            try:
                self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                cancel.set()
                raise core.Cancelled()

        api_key = (req.get("api_key") or "").strip()
        players = [p.strip() for p in (req.get("players") or []) if p and p.strip()]
        game = req.get("game") or "cs2"
        try:
            if not api_key:
                raise ValueError("Не введён API-ключ.")
            if not players:
                raise ValueError("Не указаны игроки.")
            period = build_period(req.get("period") or {})
            run_search(api_key, players, game, period, emit, cancel)
        except core.Cancelled:
            pass
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            self._safe_emit(emit, {"type": "error", "msg": core.http_error_message(status)})
        except requests.ConnectionError:
            self._safe_emit(emit, {"type": "error",
                                   "msg": "Нет соединения с FACEIT API. Проверьте интернет."})
        except requests.Timeout:
            self._safe_emit(emit, {"type": "error",
                                   "msg": "Превышено время ожидания ответа FACEIT API."})
        except ValueError as e:
            self._safe_emit(emit, {"type": "error", "msg": str(e)})
        except Exception as e:               # noqa: BLE001
            traceback.print_exc()
            self._safe_emit(emit, {"type": "error", "msg": f"Ошибка сервера: {e}"})

    @staticmethod
    def _safe_emit(emit, obj):
        try:
            emit(obj)
        except core.Cancelled:
            pass


def find_free_port(start: int) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((HOST, port)) != 0:
                return port
    return start


def main():
    port = find_free_port(DEFAULT_PORT)
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/"
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    print(f"FACEIT Match History — открыто в браузере: {url}")
    print("Чтобы остановить — закройте это окно или нажмите Ctrl+C.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# ---------------------------------------------------------------------------
# Фронтенд (одностраничное приложение). CSS + JS встроены — один файл удобнее
# раздавать и собирать в .exe.
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FACEIT Match History</title>
<style>
:root{
  --bg:#0f1216; --panel:#171c22; --panel2:#1e252d; --line:#2a323c;
  --txt:#e6eaef; --mut:#8a96a3; --acc:#ff5500; --acc2:#ffa066;
  --ok:#37c871; --warn:#f0a13b; --err:#ff5c5c;
  --self:#5aa0ff; --party:#c779e8; --mate:#5ed29a;
  --radius:12px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
  font:14px/1.5 'Segoe UI',Roboto,system-ui,sans-serif}
a{color:var(--acc2)}
.wrap{max-width:1080px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;align-items:center;gap:12px;margin-bottom:18px}
header .logo{width:34px;height:34px;border-radius:9px;
  background:linear-gradient(135deg,var(--acc),#ff8a3d);
  display:grid;place-items:center;font-weight:800;color:#1a0f06}
header h1{font-size:19px;margin:0;font-weight:700}
header .sub{color:var(--mut);font-size:12px}
.card{background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);padding:16px;margin-bottom:16px}
label{display:block;font-size:12px;color:var(--mut);margin:0 0 5px}
input,select,textarea,button{font:inherit;color:var(--txt)}
input,select,textarea{width:100%;background:var(--panel2);
  border:1px solid var(--line);border-radius:9px;padding:9px 11px;outline:none}
input:focus,select:focus,textarea:focus{border-color:var(--acc)}
textarea{resize:vertical;min-height:74px;font-family:Consolas,monospace}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row>div{flex:1;min-width:150px}
.key-wrap{display:flex;gap:8px}
.key-wrap input{flex:1}
.btn{background:var(--panel2);border:1px solid var(--line);border-radius:9px;
  padding:9px 15px;cursor:pointer;white-space:nowrap;transition:.15s}
.btn:hover{border-color:var(--acc)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.primary{background:var(--acc);border-color:var(--acc);color:#1a0f06;font-weight:700}
.btn.primary:hover{background:#ff6a1f}
.btn.ghost{background:transparent}
.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px;align-items:center}
.spacer{flex:1}
.muted{color:var(--mut)}
.period-fields{margin-top:10px}
.hidden{display:none}
.bar{height:8px;background:var(--panel2);border-radius:6px;overflow:hidden;margin-top:12px}
.bar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:.2s}
.statusline{display:flex;justify-content:space-between;gap:12px;margin-top:8px;font-size:12px}
.tabs{display:flex;gap:6px;margin-bottom:12px}
.tab{padding:8px 16px;border-radius:9px 9px 0 0;cursor:pointer;color:var(--mut);
  border:1px solid transparent;border-bottom:none}
.tab.active{color:var(--txt);background:var(--panel);border-color:var(--line)}
.pane{display:none}
.pane.active{display:block}
.log{font-family:Consolas,monospace;font-size:12.5px;white-space:pre-wrap;
  max-height:230px;overflow:auto;background:#0c0f13;border:1px solid var(--line);
  border-radius:9px;padding:10px;margin-bottom:14px}
.log .head{color:var(--acc2);font-weight:700}
.log .err{color:var(--err)} .log .warn{color:var(--warn)} .log .ok{color:var(--ok)}
.match{border:1px solid var(--line);border-radius:11px;margin-bottom:12px;overflow:hidden}
.match h3{margin:0;padding:10px 13px;background:var(--panel2);font-size:13px;
  display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
.match h3 .map{color:var(--mut);font-weight:400}
.teams{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line)}
.team{background:var(--panel);padding:10px 13px}
.team .th{display:flex;justify-content:space-between;font-size:12px;margin-bottom:7px}
.team .th .res{font-weight:700}
.win .res{color:var(--ok)} .lose .res{color:var(--mut)}
.pl{display:flex;justify-content:space-between;gap:8px;padding:3px 0;font-size:13px}
.pl .nk{display:flex;align-items:center;gap:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pl .kda{color:var(--mut);font-family:Consolas,monospace;font-size:12px}
.pl.self .nk{color:var(--self);font-weight:700}
.pl.party .nk{color:var(--party);font-weight:700}
.pl.mate .nk{color:var(--mate)}
.tag{font-size:10px;padding:1px 6px;border-radius:6px;border:1px solid currentColor}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;text-align:center;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600;cursor:pointer;user-select:none;position:sticky;top:0;background:var(--panel)}
th:first-child,td:first-child{text-align:left}
tbody tr:hover{background:var(--panel2)}
tr.self td{color:var(--self)} tr.party td:nth-child(2){color:var(--party);font-weight:700}
.role{font-size:11px;padding:2px 7px;border-radius:6px;background:var(--panel2);border:1px solid var(--line);white-space:nowrap;display:inline-block}
.role.party{color:var(--party);border-color:var(--party)}
.role.self{color:var(--self);border-color:var(--self)}
.filters{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.filters label{margin:0}
.filters input{width:80px}
.legend{font-size:11.5px;color:var(--mut);margin-top:10px}
.legend b{color:var(--self)} .legend i{color:var(--party);font-style:normal}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);
  background:var(--panel2);border:1px solid var(--acc);padding:10px 18px;
  border-radius:10px;opacity:0;transition:.25s;pointer-events:none}
.toast.show{opacity:1}
@media(max-width:640px){.teams{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">F</div>
    <div>
      <h1>FACEIT Match History</h1>
      <div class="sub">Мультипоиск · статистика по никам · определение пати</div>
    </div>
  </header>

  <div class="card">
    <label>API-ключ FACEIT <span class="muted">(хранится только в этом браузере · <a href="https://developers.faceit.com" target="_blank" rel="noopener">получить</a>)</span></label>
    <div class="key-wrap">
      <input id="key" type="password" placeholder="вставьте ключ Data API" autocomplete="off">
      <button class="btn" id="keyToggle" type="button">показать</button>
    </div>

    <div class="row" style="margin-top:13px">
      <div>
        <label>Игроки — по одному в строке (ник или ссылка)</label>
        <textarea id="players" placeholder="gwizdakk&#10;https://www.faceit.com/ru/players/..."></textarea>
      </div>
    </div>

    <div class="row" style="margin-top:13px">
      <div style="max-width:180px">
        <label>Игра</label>
        <select id="game"></select>
      </div>
      <div style="max-width:240px">
        <label>Период</label>
        <select id="period"></select>
      </div>
    </div>

    <div class="period-fields">
      <div id="f-last" class="row">
        <div style="max-width:160px"><label>Сколько матчей</label>
          <input id="limit" type="number" min="1" max="2000" value="20"></div>
      </div>
      <div id="f-dates" class="row hidden">
        <div style="max-width:160px"><label>От (ГГГГ-ММ-ДД)</label><input id="dfrom" placeholder="2026-06-01"></div>
        <div style="max-width:160px"><label>До</label><input id="dto" placeholder="2026-06-30"></div>
        <div style="max-width:140px"><label>Макс. матчей</label><input id="dmax" type="number" min="1" value="500"></div>
      </div>
      <div id="f-offset" class="row hidden">
        <div style="max-width:160px"><label>С матча №</label><input id="ostart" type="number" min="0" value="0"></div>
        <div style="max-width:160px"><label>По № (не включая)</label><input id="oend" type="number" min="1" value="100"></div>
      </div>
    </div>

    <div class="actions">
      <button class="btn primary" id="run">Получить историю</button>
      <button class="btn" id="cancel" disabled>Отмена</button>
      <span class="spacer"></span>
      <button class="btn ghost" id="expCsv" disabled>Матчи: CSV</button>
      <button class="btn ghost" id="expTsv" disabled>Матчи: копировать TSV</button>
      <button class="btn ghost" id="expJson" disabled>JSON</button>
    </div>

    <div class="bar"><i id="prog"></i></div>
    <div class="statusline">
      <span id="status" class="muted">Готово.</span>
      <span id="count" class="muted">Запросов к API: 0</span>
    </div>
  </div>

  <div class="tabs">
    <div class="tab active" data-tab="matches">Матчи</div>
    <div class="tab" data-tab="stats">Статистика</div>
  </div>

  <div class="pane active" id="pane-matches">
    <div class="log" id="log"></div>
    <div id="matches"></div>
  </div>

  <div class="pane" id="pane-stats">
    <div class="filters">
      <label>Мин. матчей</label><input id="minMatches" type="number" min="1" value="2">
      <span class="spacer"></span>
      <button class="btn ghost" id="stCsv" disabled>Статистика: CSV</button>
      <button class="btn ghost" id="stTsv" disabled>Копировать TSV</button>
    </div>
    <div class="card" style="max-height:60vh;overflow:auto;padding:0">
      <table id="statsTable">
        <thead><tr>
          <th data-k="nickname">Никнейм</th><th data-k="role">Роль</th>
          <th data-k="matches">Матчей</th><th data-k="with">вместе</th>
          <th data-k="vs">против</th><th data-k="avg_k">ср.K</th>
          <th data-k="avg_d">ср.D</th><th data-k="avg_a">ср.A</th>
          <th data-k="avg_kda">ср.KDA</th><th data-k="avg_kd">ср.K/D</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="legend">★ <b>искомый игрок</b> · ◆ <i>вероятная пати</i> (часто в одной команде) ·
      «вместе» — разово в команде (вероятно рандом) · «против» — соперник.
      Признак пати — статистический (точных данных о пати официальный API не отдаёт).</div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const GAMES = ["cs2","csgo","dota2","valorant"];
const PERIODS = [["last","Последние N матчей"],["dates","Даты (от–до)"],["offset","Номера матчей (offset)"]];
const EXPORT_COLS = ["match_id","date","map","score","team","result","nickname",
  "kills","deaths","assists","kda","kd_ratio","searched","relation","party"];

let ST = {results:[], stats:[], searched:new Set(), party:new Set(), sortK:null, sortRev:false};
let controller = null;

// --- init selects, restore key ---
GAMES.forEach(g=>{const o=document.createElement("option");o.value=o.textContent=g;$("#game").append(o);});
PERIODS.forEach(([v,t])=>{const o=document.createElement("option");o.value=v;o.textContent=t;$("#period").append(o);});
$("#key").value = localStorage.getItem("faceit_key") || "";
$("#players").value = localStorage.getItem("faceit_players") || "";
$("#game").value = localStorage.getItem("faceit_game") || "cs2";
$("#key").addEventListener("input", ()=>localStorage.setItem("faceit_key",$("#key").value));
$("#players").addEventListener("input", ()=>localStorage.setItem("faceit_players",$("#players").value));
$("#game").addEventListener("change", ()=>localStorage.setItem("faceit_game",$("#game").value));

$("#keyToggle").onclick=()=>{const k=$("#key");const p=k.type==="password";k.type=p?"text":"password";$("#keyToggle").textContent=p?"скрыть":"показать";};
$("#period").onchange=()=>{
  $("#f-last").classList.toggle("hidden",$("#period").value!=="last");
  $("#f-dates").classList.toggle("hidden",$("#period").value!=="dates");
  $("#f-offset").classList.toggle("hidden",$("#period").value!=="offset");
};
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
  document.querySelectorAll(".pane").forEach(x=>x.classList.remove("active"));
  t.classList.add("active"); $("#pane-"+t.dataset.tab).classList.add("active");
});

function toast(msg){const t=$("#toast");t.textContent=msg;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),1800);}
function setStatus(msg,cls){const s=$("#status");s.textContent=msg;s.style.color=({err:"var(--err)",ok:"var(--ok)",warn:"var(--warn)"})[cls]||"var(--mut)";}
function logLine(msg,level){const d=document.createElement("div");if(level)d.className=level;d.textContent=msg;$("#log").append(d);$("#log").scrollTop=1e9;}

function buildPeriod(){
  const m=$("#period").value;
  if(m==="dates") return {mode:"dates",from:$("#dfrom").value.trim(),to:$("#dto").value.trim(),max:+$("#dmax").value};
  if(m==="offset") return {mode:"offset",start:+$("#ostart").value,end:+$("#oend").value};
  return {mode:"last",limit:+$("#limit").value};
}
function searchedTeams(m){const s=new Set();m.teams.forEach((t,i)=>{if(t.players.some(p=>ST.searched.has(p.player_id)))s.add(i);});return s;}

function setRunning(on){
  $("#run").disabled=on;$("#cancel").disabled=!on;
  ["expCsv","expTsv","expJson","stCsv","stTsv"].forEach(id=>{if(on)$("#"+id).disabled=true;});
}

async function run(){
  const players=$("#players").value.split("\n").map(s=>s.trim()).filter(Boolean);
  if(!$("#key").value.trim()){setStatus("Не введён API-ключ.","err");return;}
  if(!players.length){setStatus("Не указаны игроки.","err");return;}
  $("#log").innerHTML=""; $("#matches").innerHTML=""; $("#prog").style.width="0";
  $("#count").textContent="Запросов к API: 0"; setStatus("Запрос…");
  ST.results=[];ST.stats=[];ST.searched=new Set();ST.party=new Set();
  setRunning(true);
  controller=new AbortController();
  let buf="";
  try{
    const resp=await fetch("/api/search",{method:"POST",signal:controller.signal,
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({api_key:$("#key").value.trim(),players,game:$("#game").value,period:buildPeriod()})});
    const reader=resp.body.getReader();const dec=new TextDecoder();
    while(true){
      const {value,done}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      let nl;while((nl=buf.indexOf("\n"))>=0){const line=buf.slice(0,nl);buf=buf.slice(nl+1);if(line.trim())handle(JSON.parse(line));}
    }
  }catch(e){
    if(e.name==="AbortError")setStatus("Отменено.","warn");
    else setStatus("Сбой соединения с локальным сервером.","err");
  }finally{setRunning(false);controller=null;}
}

function handle(ev){
  if(ev.type==="log") logLine(ev.msg,ev.level);
  else if(ev.type==="status") setStatus(ev.msg);
  else if(ev.type==="count") $("#count").textContent="Запросов к API: "+ev.n;
  else if(ev.type==="total"){ if(!ev.total){setStatus("Матчи не найдены.","warn");} }
  else if(ev.type==="progress"){ $("#prog").style.width=(100*ev.i/ev.total)+"%"; setStatus(`Матч ${ev.i}/${ev.total}…`); }
  else if(ev.type==="error"){ logLine(ev.msg,"err"); setStatus(ev.msg,"err"); }
  else if(ev.type==="done"){
    ST.results=ev.results; ST.stats=ev.stats;
    ST.searched=new Set(ev.searched_ids); ST.party=new Set(ev.party_nicks);
    $("#prog").style.width="100%";
    renderMatches(); renderParty(); renderStats();
    const on=ST.results.length>0;
    ["expCsv","expTsv","expJson"].forEach(id=>$("#"+id).disabled=!on);
    ["stCsv","stTsv"].forEach(id=>$("#"+id).disabled=!ST.stats.length);
    setStatus(`Готово: ${ST.results.length} матч(ей). Запросов: ${ev.request_count}.`,"ok");
  }
}

function renderMatches(){
  const wrap=$("#matches");wrap.innerHTML="";
  ST.results.forEach((m,idx)=>{
    const st=searchedTeams(m);
    const el=document.createElement("div");el.className="match";
    let html=`<h3><span>Матч #${idx+1} · ${m.score||""}</span><span class="map">${m.map||""} · ${m.date||""}</span></h3><div class="teams">`;
    m.teams.forEach((t,ti)=>{
      html+=`<div class="team ${t.won?"win":"lose"}"><div class="th"><span>${esc(t.name||"")}</span><span class="res">${t.won?"WIN":"LOSE"}</span></div>`;
      t.players.forEach(p=>{
        const isS=ST.searched.has(p.player_id);
        let cls="",mark="";
        if(isS){cls="self";mark="★";}
        else if(ST.party.has(p.nickname)&&st.has(ti)){cls="party";mark="◆";}
        else if(st.has(ti)){cls="mate";mark="+";}
        html+=`<div class="pl ${cls}"><span class="nk">${mark?`<span>${mark}</span>`:""}${esc(p.nickname||"")}</span><span class="kda">${esc(p.kda||"")} · ${esc(String(p.kd_ratio||""))}</span></div>`;
      });
      html+=`</div>`;
    });
    el.innerHTML=html+`</div>`;wrap.append(el);
  });
}

function renderParty(){
  const party=ST.stats.filter(s=>s.role==="пати?");
  logLine("");
  logLine("── Анализ пати (★ искомый, ◆ вероятная пати) ──","head");
  if(!party.length){logLine("   Постоянных сокомандников не выявлено — похоже на соло-очередь.");return;}
  party.forEach(s=>logLine(`   ◆ ${s.nickname}  вместе ×${s.with}  против ×${s.vs}  ср.KDA ${s.avg_kda}`,"warn"));
}

function renderStats(){
  const min=+$("#minMatches").value||1;
  const tb=$("#statsTable").querySelector("tbody");tb.innerHTML="";
  let rows=ST.stats.slice();
  if(ST.sortK){const k=ST.sortK;rows.sort((a,b)=>{const x=a[k],y=b[k];return (typeof x==="number")?(x-y):String(x).localeCompare(String(y));});if(ST.sortRev)rows.reverse();}
  rows.forEach(s=>{
    if(!s.is_searched && s.matches<min) return;
    const tr=document.createElement("tr");
    tr.className=s.is_searched?"self":(s.role==="пати?"?"party":"");
    const roleCls=s.is_searched?"self":(s.role==="пати?"?"party":"");
    tr.innerHTML=`<td>${esc(s.nickname)}</td><td><span class="role ${roleCls}">${esc(s.role)}</span></td>
      <td>${s.matches}</td><td>${s.with}</td><td>${s.vs}</td>
      <td>${s.avg_k.toFixed(2)}</td><td>${s.avg_d.toFixed(2)}</td><td>${s.avg_a.toFixed(2)}</td>
      <td>${esc(s.avg_kda)}</td><td>${s.avg_kd.toFixed(2)}</td>`;
    tb.append(tr);
  });
}
$("#minMatches").oninput=renderStats;
document.querySelectorAll("#statsTable th").forEach(th=>th.onclick=()=>{
  const k=th.dataset.k;ST.sortRev=(ST.sortK===k)?!ST.sortRev:true;ST.sortK=k;renderStats();
});

// --- export ---
function flatRows(){
  const out=[];
  ST.results.forEach(m=>{
    const st=searchedTeams(m);
    m.teams.forEach((t,ti)=>{
      const res=t.won?"win":"lose";
      t.players.forEach(p=>{
        const isS=ST.searched.has(p.player_id);
        const rel=isS?"self":(st.has(ti)?"team":(st.size?"enemy":""));
        out.push([m.match_id,m.date||"",m.map||"",m.score,t.name,res,p.nickname,
          p.kills,p.deaths,p.assists,p.kda,p.kd_ratio,isS?"yes":"",rel,
          (!isS&&ST.party.has(p.nickname))?"party?":""]);
      });
    });
  });
  return out;
}
function toCSV(cols,rows){const esc=v=>{v=String(v??"");return /[;"\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;};
  return "﻿"+[cols.join(";"),...rows.map(r=>r.map(esc).join(";"))].join("\n");}
function toTSV(cols,rows){return [cols.join("\t"),...rows.map(r=>r.join("\t"))].join("\n");}
function download(name,text,type){const b=new Blob([text],{type});const a=document.createElement("a");
  a.href=URL.createObjectURL(b);a.download=name;a.click();URL.revokeObjectURL(a.href);}

$("#expCsv").onclick=()=>download("faceit_history.csv",toCSV(EXPORT_COLS,flatRows()),"text/csv");
$("#expJson").onclick=()=>download("faceit_history.json",JSON.stringify(ST.results,null,2),"application/json");
$("#expTsv").onclick=()=>{navigator.clipboard.writeText(toTSV(EXPORT_COLS,flatRows()));toast("Скопировано для Google Sheets (Ctrl+V)");};
const STAT_COLS=["nickname","role","matches","with_searched","vs_searched","avg_kills","avg_deaths","avg_assists","avg_kda","avg_kd_ratio"];
function statFlat(){return ST.stats.map(s=>[s.nickname,s.role,s.matches,s.with,s.vs,
  s.avg_k.toFixed(2),s.avg_d.toFixed(2),s.avg_a.toFixed(2),s.avg_kda,s.avg_kd.toFixed(2)]);}
$("#stCsv").onclick=()=>download("faceit_stats.csv",toCSV(STAT_COLS,statFlat()),"text/csv");
$("#stTsv").onclick=()=>{navigator.clipboard.writeText(toTSV(STAT_COLS,statFlat()));toast("Статистика скопирована (Ctrl+V)");};

function esc(s){return String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}

$("#run").onclick=run;
$("#cancel").onclick=()=>{if(controller)controller.abort();};
$("#players").addEventListener("keydown",e=>{if(e.ctrlKey&&e.key==="Enter")run();});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
