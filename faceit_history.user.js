// ==UserScript==
// @name         FACEIT Match History
// @namespace    https://github.com/Defou1t322/Faceit_history
// @version      1.1.1
// @description  Мультипоиск истории матчей FACEIT, статистика по никам и определение пати. Работает прямо в браузере (без сервера) через GM_xmlhttpRequest.
// @author       Defou1t/Eduard P
// @homepageURL  https://github.com/Defou1t322/Faceit_history
// @supportURL   https://github.com/Defou1t322/Faceit_history/issues
// @updateURL    https://raw.githubusercontent.com/Defou1t322/Faceit_history/main/faceit_history.user.js
// @downloadURL  https://raw.githubusercontent.com/Defou1t322/Faceit_history/main/faceit_history.user.js
// @match        *://*.faceit.com/*
// @match        *://faceit.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_setClipboard
// @grant        GM_registerMenuCommand
// @connect      open.faceit.com
// @run-at       document-idle
// @noframes
// ==/UserScript==

/*
 * Как пользоваться:
 *   1. Установите Tampermonkey (расширение для браузера).
 *   2. Добавьте этот файл как новый скрипт (Tampermonkey → Создать скрипт → вставить → сохранить),
 *      либо откройте .user.js — Tampermonkey предложит установку.
 *   3. Зайдите на faceit.com — справа внизу появится оранжевая кнопка «FH». Нажмите её.
 *   4. Вставьте свой API-ключ FACEIT (developers.faceit.com), ник(и) — и поехали.
 *
 * Ключ хранится только в хранилище Tampermonkey на вашем компьютере и отправляется
 * исключительно в заголовке запроса к open.faceit.com.
 */

(function () {
  'use strict';

  // ---- константы (паритет с faceit_core.py) ----
  const API_BASE = 'https://open.faceit.com/data/v4';
  const MIN_INTERVAL = 100;        // мс между запросами (~10 req/s)
  const DATE_MODE_MAX = 2000;
  const PARTY_MIN_MATCHES = 3;
  const PARTY_MIN_RATIO = 0.6;
  const GAMES = ['cs2', 'csgo', 'dota2', 'valorant'];
  const PERIODS = [['last', 'Последние N матчей'], ['dates', 'Даты (от–до)'], ['offset', 'Номера матчей (offset)']];
  const EXPORT_COLS = ['match_id', 'date', 'map', 'score', 'team', 'result', 'nickname',
    'kills', 'deaths', 'assists', 'kda', 'kd_ratio', 'searched', 'relation', 'party'];
  const STAT_COLS = ['nickname', 'role', 'matches', 'with_searched', 'vs_searched',
    'avg_kills', 'avg_deaths', 'avg_assists', 'avg_kda', 'avg_kd_ratio', 'direct_app'];
  const CANCELLED = Symbol('cancelled');

  // =========================================================================
  // Сетевой клиент: троттлинг + ретрай 429 + счётчик. Через GM_xmlhttpRequest.
  // =========================================================================
  function gmGet(url, apiKey) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET', url, timeout: 30000,
        headers: { Authorization: 'Bearer ' + apiKey, accept: 'application/json' },
        onload: r => resolve(r),
        onerror: () => reject(new Error('Нет соединения с FACEIT API. Проверьте интернет.')),
        ontimeout: () => reject(new Error('Превышено время ожидания ответа FACEIT API.')),
      });
    });
  }

  function httpErrorMessage(status) {
    return ({
      400: '400 — некорректный запрос (проверьте ник/игру/период).',
      401: '401 — неверный или отсутствующий API-ключ.',
      403: '403 — доступ запрещён (проверьте права ключа).',
      404: '404 — не найдено.',
      429: '429 — превышен лимит запросов. Подождите и повторите.',
      503: '503 — сервис FACEIT временно недоступен.',
    })[status] || ('Ошибка HTTP ' + status);
  }

  class Client {
    constructor(apiKey, onCount) {
      this.apiKey = apiKey;
      this.onCount = onCount;
      this.count = 0;
      this.last = 0;
      this.cancel = false;
    }
    _sleep(ms) {
      return new Promise((resolve, reject) => {
        const end = performance.now() + ms;
        const tick = () => {
          if (this.cancel) return reject(CANCELLED);
          const rem = end - performance.now();
          if (rem <= 0) return resolve();
          setTimeout(tick, Math.min(50, rem));
        };
        tick();
      });
    }
    async get(url, allow404) {
      if (this.cancel) throw CANCELLED;
      let r = null;
      for (let attempt = 0; attempt < 6; attempt++) {
        const wait = MIN_INTERVAL - (performance.now() - this.last);
        if (wait > 0) await this._sleep(wait);
        this.last = performance.now();

        r = await gmGet(url, this.apiKey);
        this.count++;
        if (this.onCount) this.onCount(this.count);

        if (r.status === 429) {
          const m = /retry-after:\s*([\d.]+)/i.exec(r.responseHeaders || '');
          const delay = m ? parseFloat(m[1]) * 1000 : Math.min(2 ** attempt, 16) * 1000;
          await this._sleep(Math.max(delay, 1000));
          continue;
        }
        if (allow404 && r.status === 404) return null;
        if (r.status < 200 || r.status >= 300) throw new Error(httpErrorMessage(r.status));
        return JSON.parse(r.responseText);
      }
      throw new Error('429 — лимит запросов FACEIT не отпускает даже после ретраев.');
    }
  }

  // =========================================================================
  // API-логика (порт faceit_core.py)
  // =========================================================================
  function nickFromInput(v) {
    v = v.trim();
    const m = /\/players\/([^/?#]+)/.exec(v);
    return m ? m[1] : v;
  }
  function parseMatchUrl(v) {
    v = v.trim();
    const m = /\/room\/([^/?#]+)/.exec(v);
    return m ? m[1] : v;
  }
  function qs(params) {
    return Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
      .map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&');
  }
  async function getPlayerId(client, nick) {
    const data = await client.get(`${API_BASE}/players?` + qs({ nickname: nick }), true);
    if (!data) throw new Error(`Игрок '${nick}' не найден на FACEIT.`);
    return data.player_id;
  }
  async function getHistory(client, pid, game, period) {
    const url = `${API_BASE}/players/${pid}/history`;
    const items = [];
    if (period.mode === 'last') {
      let offset = 0;
      while (items.length < period.limit) {
        const batch = Math.min(100, period.limit - items.length);
        const page = (await client.get(url + '?' + qs({ game, offset, limit: batch }))).items || [];
        if (!page.length) break;
        items.push(...page); offset += batch;
      }
      return items.slice(0, period.limit);
    }
    if (period.mode === 'offset') {
      let offset = period.start;
      while (offset < period.end) {
        const batch = Math.min(100, period.end - offset);
        const page = (await client.get(url + '?' + qs({ game, offset, limit: batch }))).items || [];
        if (!page.length) break;
        items.push(...page); offset += batch;
      }
      return items;
    }
    // dates
    const cap = period.max || DATE_MODE_MAX;
    let offset = 0;
    while (items.length < cap) {
      const batch = Math.min(100, cap - items.length);
      const page = (await client.get(url + '?' + qs({ game, offset, limit: batch, from: period.from, to: period.to }))).items || [];
      if (!page.length) break;
      items.push(...page); offset += batch;
    }
    return items;
  }
  async function parseMatch(client, mid) {
    const data = await client.get(`${API_BASE}/matches/${mid}/stats`, true);
    if (!data || !data.rounds) return null;
    const rnd = data.rounds[0];
    const rs = rnd.round_stats || {};
    const teams = (rnd.teams || []).map(team => {
      const ts = team.team_stats || {};
      const players = (team.players || []).map(p => {
        const st = p.player_stats || {};
        const k = st.Kills || '0', d = st.Deaths || '0', a = st.Assists || '0';
        return {
          nickname: p.nickname, player_id: p.player_id,
          kills: k, deaths: d, assists: a, kda: `${k}/${d}/${a}`,
          kd_ratio: st['K/D Ratio'] || '',
        };
      });
      return { name: ts.Team || team.team_id || '', won: ts['Team Win'] === '1', final_score: ts['Final Score'] || '', players };
    });
    return { match_id: mid, map: rs.Map || '', score: rs.Score || '?', teams };
  }

  // ---- статистика / пати ----
  const toInt = x => { const n = parseInt(x, 10); return isNaN(n) ? 0 : n; };
  function searchedTeamIndices(match, searched) {
    const idx = new Set();
    match.teams.forEach((t, i) => { if (t.players.some(p => searched.has(p.player_id))) idx.add(i); });
    return idx;
  }
  function classifyRole(d) {
    if (d.is_searched) return '★ искомый';
    const together = d.with + d.vs;
    if (d.with >= PARTY_MIN_MATCHES && d.with >= d.vs && together && d.with / together >= PARTY_MIN_RATIO) return 'пати?';
    if (d.with > 0) return 'вместе';
    return 'против';
  }
  function computeStats(results, searched, directMatchIds) {
    directMatchIds = directMatchIds || new Set();
    const agg = {};
    for (const m of results) {
      const isDirect = directMatchIds.has(m.match_id);
      const sTeams = searchedTeamIndices(m, searched);
      const any = sTeams.size > 0;
      m.teams.forEach((team, ti) => {
        const same = sTeams.has(ti);
        for (const p of team.players) {
          const nick = p.nickname;
          if (!nick) continue;
          const d = agg[nick] || (agg[nick] = { matches: 0, k: 0, d: 0, a: 0, kd_sum: 0, kd_n: 0, with: 0, vs: 0, is_searched: false, player_id: p.player_id, direct_app: 0 });
          d.matches++; d.k += toInt(p.kills); d.d += toInt(p.deaths); d.a += toInt(p.assists);
          const kd = parseFloat(p.kd_ratio); if (!isNaN(kd)) { d.kd_sum += kd; d.kd_n++; }
          if (searched.has(p.player_id)) d.is_searched = true;
          else if (any) { if (same) d.with++; else d.vs++; }
          if (isDirect) d.direct_app++;
        }
      });
    }
    const rows = Object.entries(agg).map(([nick, d]) => {
      const n = d.matches;
      const r = {
        nickname: nick, matches: n,
        avg_k: d.k / n, avg_d: d.d / n, avg_a: d.a / n,
        avg_kda: `${(d.k / n).toFixed(1)}/${(d.d / n).toFixed(1)}/${(d.a / n).toFixed(1)}`,
        avg_kd: d.kd_n ? d.kd_sum / d.kd_n : 0,
        with: d.with, vs: d.vs, is_searched: d.is_searched, player_id: d.player_id, direct_app: d.direct_app,
      };
      r.role = classifyRole(d);
      return r;
    });
    rows.sort((a, b) => (b.is_searched - a.is_searched) || (b.direct_app - a.direct_app) || (b.matches - a.matches) || (b.with - a.with));
    return rows;
  }
  function partyNicknames(stats) { return stats.filter(s => s.role === 'пати?').map(s => s.nickname); }

  function unixToStr(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const p = n => String(n).padStart(2, '0');
    return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
  }
  function parseDateToUnix(text, endOfDay) {
    text = (text || '').trim();
    if (!text) return null;
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
    if (!m) throw new Error(`Неверная дата: ${text} (нужен формат ГГГГ-ММ-ДД).`);
    let ts = Math.floor(Date.UTC(+m[1], +m[2] - 1, +m[3]) / 1000);
    return endOfDay ? ts + 86399 : ts;
  }

  // =========================================================================
  // Интерфейс (Shadow DOM — изоляция от вёрстки FACEIT)
  // =========================================================================
  const ST = { results: [], stats: [], searched: new Set(), party: new Set(), directMatchIds: new Set(), sortK: null, sortRev: false };
  let client = null, searching = false, root = null;

  const CSS = `
:host{all:initial}
*{box-sizing:border-box;font-family:'Segoe UI',Roboto,system-ui,sans-serif}
.launcher{position:fixed;right:20px;bottom:20px;width:52px;height:52px;border-radius:50%;
  background:linear-gradient(135deg,#ff5500,#ff8a3d);color:#1a0f06;font-weight:800;font-size:17px;
  border:none;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.45);z-index:2147483000}
.launcher:hover{transform:scale(1.06)}
.overlay{position:fixed;inset:0;background:rgba(6,8,11,.72);backdrop-filter:blur(4px);
  z-index:2147483001;display:none;overflow:auto;padding:10px}
.overlay.show{display:block}
.modal{max-width:1560px;margin:0 auto;background:#0f1216;color:#e6eaef;border:1px solid #2a323c;
  border-radius:16px;padding:24px 30px;font-size:14px;line-height:1.5}
.modal-hd{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:14px}
.modal-hd-btns{display:flex;gap:8px;align-items:center;flex-shrink:0}
.x{background:none;border:none;color:#8a96a3;font-size:24px;cursor:pointer;line-height:1;padding:0 2px}
.x:hover{color:#e6eaef}
h1{font-size:20px;margin:0 0 3px} .sub{color:#8a96a3;font-size:12px;margin:0}
.card{background:#171c22;border:1px solid #2a323c;border-radius:12px;padding:15px;margin-bottom:14px}
label{display:block;font-size:12px;color:#8a96a3;margin:0 0 5px}
input,select,textarea{width:100%;background:#1e252d;border:1px solid #2a323c;border-radius:9px;
  padding:9px 11px;color:#e6eaef;outline:none;font-size:14px}
input:focus,select:focus,textarea:focus{border-color:#ff5500}
textarea{resize:vertical;min-height:90px;font-family:Consolas,monospace}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>div{flex:1;min-width:140px}
.key-wrap{display:flex;gap:8px}.key-wrap input{flex:1}
.btn{background:#1e252d;border:1px solid #2a323c;border-radius:9px;padding:9px 15px;color:#e6eaef;
  cursor:pointer;white-space:nowrap;font-size:14px}
.btn:hover{border-color:#ff5500}.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.primary{background:#ff5500;border-color:#ff5500;color:#1a0f06;font-weight:700}
.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:13px;align-items:center}
.spacer{flex:1}.muted{color:#8a96a3}.hidden{display:none}
a{color:#ffa066}
.bar{height:8px;background:#1e252d;border-radius:6px;overflow:hidden;margin-top:12px}
.bar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,#ff5500,#ffa066);transition:.2s}
.statusline{display:flex;justify-content:space-between;gap:12px;margin-top:8px;font-size:12px}
.tabs{display:flex;gap:6px;margin-bottom:12px}
.tab{padding:8px 16px;border-radius:9px 9px 0 0;cursor:pointer;color:#8a96a3;border:1px solid transparent;border-bottom:none}
.tab.active{color:#e6eaef;background:#171c22;border-color:#2a323c}
.pane{display:none}.pane.active{display:block}
.log{font-family:Consolas,monospace;font-size:12.5px;white-space:pre-wrap;max-height:200px;overflow:auto;
  background:#0c0f13;border:1px solid #2a323c;border-radius:9px;padding:10px;margin-bottom:12px}
.log .head{color:#ffa066;font-weight:700}.log .err{color:#ff5c5c}.log .warn{color:#f0a13b}.log .ok{color:#37c871}
.match{border:1px solid #2a323c;border-radius:11px;margin-bottom:12px;overflow:hidden}
.match h3{margin:0;padding:10px 13px;background:#1e252d;font-size:13px;display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
.match h3 .map{color:#8a96a3;font-weight:400}
.teams{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:#2a323c}
.team{background:#171c22;padding:10px 13px}
.team .th{display:flex;justify-content:space-between;font-size:12px;margin-bottom:7px}
.team .th .res{font-weight:700}.win .res{color:#37c871}.lose .res{color:#8a96a3}
.pl{display:flex;justify-content:space-between;gap:8px;padding:3px 0;font-size:13px}
.pl .nk{display:flex;align-items:center;gap:6px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.pl .kda{color:#8a96a3;font-family:Consolas,monospace;font-size:12px;white-space:nowrap}
.pl.self .nk{color:#5aa0ff;font-weight:700}.pl.party .nk{color:#c779e8;font-weight:700}.pl.mate .nk{color:#5ed29a}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;text-align:center;border-bottom:1px solid #2a323c}
th{color:#8a96a3;font-weight:600;cursor:pointer;user-select:none}
th:first-child,td:first-child{text-align:left}
tbody tr:hover{background:#1e252d}tr.self td{color:#5aa0ff}
.role{font-size:11px;padding:2px 7px;border-radius:6px;background:#1e252d;border:1px solid #2a323c;white-space:nowrap;display:inline-block}
.role.party{color:#c779e8;border-color:#c779e8}.role.self{color:#5aa0ff;border-color:#5aa0ff}
.filters{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.filters input{width:80px}.legend{font-size:11.5px;color:#8a96a3;margin-top:10px}
.legend b{color:#5aa0ff}.legend i{color:#c779e8;font-style:normal}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#1e252d;border:1px solid #ff5500;
  padding:10px 18px;border-radius:10px;opacity:0;transition:.25s;pointer-events:none;z-index:2147483002}
.toast.show{opacity:1}
@media(max-width:640px){.teams{grid-template-columns:1fr}}
@media(max-width:860px){.form-grid{grid-template-columns:1fr}}
.form-grid{display:grid;grid-template-columns:3fr 2fr;gap:24px;align-items:start}
.form-right-sep{border-top:1px solid #2a323c;margin:14px 0}
.help-panel{background:#0c0f13;border:1px solid #2a323c;border-radius:12px;padding:20px;margin-bottom:16px}
.help-panel h2{font-size:13px;font-weight:700;color:#ffa066;text-transform:uppercase;letter-spacing:.06em;margin:0 0 14px}
.help-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media(max-width:700px){.help-grid{grid-template-columns:1fr}}
.hb{margin-bottom:16px}
.hb h3{font-size:11.5px;font-weight:700;color:#e6eaef;text-transform:uppercase;letter-spacing:.05em;margin:0 0 8px;border-bottom:1px solid #1e252d;padding-bottom:6px}
.hb p,.hb li{font-size:12.5px;color:#8a96a3;margin:0 0 5px;line-height:1.6}
.hb ul{margin:4px 0 0;padding:0 0 0 16px}
.hb li{margin-bottom:4px}
.ht{width:100%;border-collapse:collapse;margin-top:6px;font-size:12.5px}
.ht td{padding:5px 8px;border-bottom:1px solid #1a2028;color:#8a96a3;vertical-align:top}
.ht td:first-child{color:#e6eaef;white-space:nowrap;font-weight:600;padding-right:14px;min-width:100px}
.ms{color:#5aa0ff;font-weight:700}.mp{color:#c779e8;font-weight:700}.mm{color:#5ed29a;font-weight:700}
`;

  const HTML = `
<button class="launcher" id="launcher" title="FACEIT Match History">FH</button>
<div class="overlay" id="overlay">
  <div class="modal">
    <div class="modal-hd">
      <div>
        <h1>FACEIT Match History</h1>
        <div class="sub">Мультипоиск · статистика по никам · определение пати</div>
      </div>
      <div class="modal-hd-btns">
        <button class="btn" id="helpBtn" style="font-size:13px;padding:7px 14px">? Инструкция</button>
        <button class="x" id="close">×</button>
      </div>
    </div>

    <div id="helpPane" class="help-panel hidden">
      <h2>Как пользоваться</h2>
      <div class="help-grid">
        <div>
          <div class="hb">
            <h3>Начало работы</h3>
            <ul>
              <li>Получите API-ключ на <a href="https://developers.faceit.com" target="_blank" rel="noopener">developers.faceit.com</a> → вставьте в поле «API-ключ»</li>
              <li>Введите никнеймы или ссылки на профили игроков (один в строке)</li>
              <li>Выберите игру и период → нажмите «Получить историю»</li>
              <li>Ключ сохраняется локально — вводить повторно не нужно</li>
            </ul>
          </div>
          <div class="hb">
            <h3>Три режима работы</h3>
            <table class="ht">
              <tr><td>По игрокам</td><td>Ники/ссылки в поле «Игроки» → загружает историю матчей</td></tr>
              <tr><td>По матчам</td><td>Ссылки в поле «Конкретные матчи» → анализирует участников без истории</td></tr>
              <tr><td>Комбо</td><td>Оба поля сразу → история + прямые матчи объединяются</td></tr>
            </table>
          </div>
          <div class="hb">
            <h3>Периоды (только для поиска по игрокам)</h3>
            <table class="ht">
              <tr><td>Последние N</td><td>Последние N матчей (до 2000)</td></tr>
              <tr><td>Даты</td><td>Фильтр по диапазону ГГГГ-ММ-ДД</td></tr>
              <tr><td>Смещение</td><td>Матчи с № по №. Удобно для нескольких поисков без пересечений: первый 0–100, второй 100–200</td></tr>
            </table>
          </div>
        </div>
        <div>
          <div class="hb">
            <h3>Обозначения в матчах</h3>
            <table class="ht">
              <tr><td><span class="ms">★ синий</span></td><td>искомый игрок</td></tr>
              <tr><td><span class="mp">◆ фиолетовый</span></td><td>вероятная пати (≥3 матчей в одной команде, доля ≥60%)</td></tr>
              <tr><td><span class="mm">+ зелёный</span></td><td>сокомандник (вероятно рандом)</td></tr>
              <tr><td>без метки</td><td>соперник</td></tr>
            </table>
            <p style="margin-top:8px">Данных о пати в API нет — признак статистический.</p>
          </div>
          <div class="hb">
            <h3>Статистика и колонки</h3>
            <table class="ht">
              <tr><td>вместе</td><td>матчей в одной команде с искомым</td></tr>
              <tr><td>против</td><td>матчей на противоположной стороне</td></tr>
              <tr><td>В заданных</td><td>сколько раз встретился в конкретных указанных матчах</td></tr>
            </table>
          </div>
          <div class="hb">
            <h3>Расследование: «специально попадает против»</h3>
            <ul>
              <li>Поиск по жертве (Y) → в Статистике ищите игроков с высоким «против» и низким «вместе»</li>
              <li>Скопируйте ссылки на матчи, где подозреваемый X встретился с Y</li>
              <li>Вставьте их в «Конкретные матчи» → колонка «В заданных» покажет, кто там повторяется</li>
              <li>Одни и те же люди рядом с X = вероятно организованная группа</li>
            </ul>
          </div>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="form-grid">
        <div>
          <label>API-ключ FACEIT <span class="muted">(хранится только локально · <a href="https://developers.faceit.com" target="_blank" rel="noopener">получить</a>)</span></label>
          <div class="key-wrap">
            <input id="key" type="password" placeholder="вставьте ключ Data API" autocomplete="off">
            <button class="btn" id="keyToggle" type="button">показать</button>
          </div>
          <div style="margin-top:14px">
            <label>Игроки — по одному в строке (ник или ссылка)</label>
            <textarea id="players" placeholder="gwizdakk&#10;https://www.faceit.com/ru/players/..."></textarea>
          </div>
          <div style="margin-top:12px">
            <label>Конкретные матчи — ссылки или ID, по одному в строке <span class="muted">(необязательно · для анализа пересечений)</span></label>
            <textarea id="matchUrls" placeholder="https://www.faceit.com/ru/cs2/room/1-xxx&#10;1-yyy-zzz-..."></textarea>
          </div>
        </div>
        <div>
          <div class="row">
            <div><label>Игра</label><select id="game"></select></div>
            <div><label>Период</label><select id="period"></select></div>
          </div>
          <div style="margin-top:12px">
            <div id="f-last" class="row"><div><label>Сколько матчей</label><input id="limit" type="number" min="1" max="2000" value="20"></div></div>
            <div id="f-dates" class="row hidden">
              <div><label>От (ГГГГ-ММ-ДД)</label><input id="dfrom" placeholder="2026-06-01"></div>
              <div><label>До</label><input id="dto" placeholder="2026-06-30"></div>
              <div><label>Макс. матчей</label><input id="dmax" type="number" min="1" value="500"></div>
            </div>
            <div id="f-offset" class="row hidden">
              <div><label>С матча №</label><input id="ostart" type="number" min="0" value="0"></div>
              <div><label>По № (не включая)</label><input id="oend" type="number" min="1" value="100"></div>
            </div>
          </div>
          <div class="form-right-sep"></div>
          <div class="actions" style="margin-top:0">
            <button class="btn primary" id="run">Получить историю</button>
            <button class="btn" id="cancel" disabled>Отмена</button>
          </div>
          <div class="actions" style="margin-top:10px">
            <button class="btn" id="expCsv" disabled>Матчи: CSV</button>
            <button class="btn" id="expTsv" disabled>Копировать TSV</button>
            <button class="btn" id="expJson" disabled>JSON</button>
            <button class="btn" id="stCsv" style="display:none" disabled>Стат: CSV</button>
            <button class="btn" id="stTsv" style="display:none" disabled>Стат: TSV</button>
          </div>
          <div class="bar"><i id="prog"></i></div>
          <div class="statusline"><span id="status" class="muted">Готово.</span><span id="count" class="muted">Запросов: 0</span></div>
        </div>
      </div>
    </div>

    <div class="tabs">
      <div class="tab active" data-tab="matches">Матчи</div>
      <div class="tab" data-tab="stats">Статистика</div>
    </div>
    <div class="pane active" id="pane-matches"><div class="log" id="log"></div><div id="matches"></div></div>
    <div class="pane" id="pane-stats">
      <div class="filters">
        <label style="margin:0;line-height:2.2">Мин. матчей</label><input id="minMatches" type="number" min="1" value="2" style="width:70px">
      </div>
      <div class="card" style="max-height:55vh;overflow:auto;padding:0">
        <table id="statsTable"><thead><tr>
          <th data-k="nickname">Никнейм</th><th data-k="role">Роль</th><th data-k="matches">Матчей</th>
          <th data-k="with">вместе</th><th data-k="vs">против</th><th data-k="avg_k">ср.K</th>
          <th data-k="avg_d">ср.D</th><th data-k="avg_a">ср.A</th><th data-k="avg_kda">ср.KDA</th><th data-k="avg_kd">ср.K/D</th>
          <th data-k="direct_app" class="col-direct hidden" title="Сколько раз встретился в указанных матчах">В заданных</th>
        </tr></thead><tbody></tbody></table>
      </div>
      <div class="legend">★ <b>искомый</b> · ◆ <i>вероятная пати</i> (часто в одной команде) · «вместе» — разово (вероятно рандом) · «против» — соперник.
        Признак пати статистический — точных данных о пати официальный API не отдаёт.</div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>`;

  function $(s) { return root.querySelector(s); }
  function esc(s) { return String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }
  function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 1800); }
  function setStatus(msg, cls) { const s = $('#status'); s.textContent = msg; s.style.color = ({ err: '#ff5c5c', ok: '#37c871', warn: '#f0a13b' })[cls] || '#8a96a3'; }
  function setCount(n) { $('#count').textContent = 'Запросов к API: ' + n; }
  function setProgress(pct) { $('#prog').style.width = pct + '%'; }
  function log(msg, level) { const d = document.createElement('div'); if (level) d.className = level; d.textContent = msg; $('#log').append(d); $('#log').scrollTop = 1e9; }

  function buildUI() {
    const host = document.createElement('div');
    host.id = 'fh-host';
    document.body.appendChild(host);
    root = host.attachShadow({ mode: 'open' });
    const style = document.createElement('style'); style.textContent = CSS;
    const cont = document.createElement('div'); cont.innerHTML = HTML;
    root.append(style, cont);

    GAMES.forEach(g => { const o = document.createElement('option'); o.value = o.textContent = g; $('#game').append(o); });
    PERIODS.forEach(([v, t]) => { const o = document.createElement('option'); o.value = v; o.textContent = t; $('#period').append(o); });

    $('#key').value = GM_getValue('key', '');
    $('#players').value = GM_getValue('players', '');
    $('#game').value = GM_getValue('game', 'cs2');
    $('#key').addEventListener('input', () => GM_setValue('key', $('#key').value));
    $('#players').addEventListener('input', () => GM_setValue('players', $('#players').value));
    $('#matchUrls').value = GM_getValue('matchUrls', '');
    $('#matchUrls').addEventListener('input', () => GM_setValue('matchUrls', $('#matchUrls').value));
    $('#game').addEventListener('change', () => GM_setValue('game', $('#game').value));

    $('#launcher').onclick = () => $('#overlay').classList.add('show');
    $('#close').onclick = () => $('#overlay').classList.remove('show');
    $('#helpBtn').onclick = () => { $('#helpPane').classList.toggle('hidden'); $('#helpBtn').textContent = $('#helpPane').classList.contains('hidden') ? '? Инструкция' : '✕ Закрыть'; };
    $('#overlay').onclick = e => { if (e.target.id === 'overlay') $('#overlay').classList.remove('show'); };
    $('#keyToggle').onclick = () => { const k = $('#key'); const p = k.type === 'password'; k.type = p ? 'text' : 'password'; $('#keyToggle').textContent = p ? 'скрыть' : 'показать'; };
    $('#period').onchange = () => {
      $('#f-last').classList.toggle('hidden', $('#period').value !== 'last');
      $('#f-dates').classList.toggle('hidden', $('#period').value !== 'dates');
      $('#f-offset').classList.toggle('hidden', $('#period').value !== 'offset');
    };
    root.querySelectorAll('.tab').forEach(t => t.onclick = () => {
      root.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
      root.querySelectorAll('.pane').forEach(x => x.classList.remove('active'));
      t.classList.add('active'); $('#pane-' + t.dataset.tab).classList.add('active');
    });
    $('#minMatches').oninput = renderStats;
    root.querySelectorAll('#statsTable th').forEach(th => th.onclick = () => {
      const k = th.dataset.k; ST.sortRev = (ST.sortK === k) ? !ST.sortRev : true; ST.sortK = k; renderStats();
    });
    $('#run').onclick = runSearch;
    $('#cancel').onclick = () => { if (client) client.cancel = true; };
    $('#players').addEventListener('keydown', e => { if (e.ctrlKey && e.key === 'Enter') runSearch(); });
    $('#expCsv').onclick = () => download('faceit_history.csv', toCSV(EXPORT_COLS, flatRows()), 'text/csv');
    $('#expJson').onclick = () => download('faceit_history.json', JSON.stringify(ST.results, null, 2), 'application/json');
    $('#expTsv').onclick = () => { GM_setClipboard(toTSV(EXPORT_COLS, flatRows())); toast('Скопировано для Google Sheets (Ctrl+V)'); };
    $('#stCsv').onclick = () => download('faceit_stats.csv', toCSV(STAT_COLS, statFlat()), 'text/csv');
    $('#stTsv').onclick = () => { GM_setClipboard(toTSV(STAT_COLS, statFlat())); toast('Статистика скопирована (Ctrl+V)'); };

    GM_registerMenuCommand('Открыть FACEIT Match History', () => $('#overlay').classList.add('show'));
  }

  function buildPeriod() {
    const m = $('#period').value;
    if (m === 'dates') {
      const from = parseDateToUnix($('#dfrom').value), to = parseDateToUnix($('#dto').value, true);
      if (!from && !to) throw new Error('Укажите хотя бы одну дату (От или До).');
      if (from && to && from > to) throw new Error('Дата «От» позже даты «До».');
      return { mode: 'dates', from, to, max: Math.max(1, +$('#dmax').value || DATE_MODE_MAX) };
    }
    if (m === 'offset') {
      const start = Math.max(0, +$('#ostart').value || 0), end = +$('#oend').value || 0;
      if (end <= start) throw new Error('«По №» должно быть больше «С матча №».');
      return { mode: 'offset', start, end };
    }
    return { mode: 'last', limit: Math.max(1, +$('#limit').value || 20) };
  }

  function setRunning(on) {
    $('#run').disabled = on; $('#cancel').disabled = !on;
    if (on) {
      ['expCsv', 'expTsv', 'expJson'].forEach(id => $('#' + id).disabled = true);
      ['stCsv', 'stTsv'].forEach(id => { const el = $('#' + id); el.disabled = true; el.style.display = 'none'; });
    }
  }

  async function runSearch() {
    if (searching) return;
    const players = $('#players').value.split('\n').map(s => s.trim()).filter(Boolean);
    const apiKey = $('#key').value.trim();
    if (!apiKey) { setStatus('Не введён API-ключ.', 'err'); return; }
    const matchUrlsVal = ($('#matchUrls').value || '').split('\n').map(s => s.trim()).filter(Boolean);
    if (!players.length && !matchUrlsVal.length) { setStatus('Укажите игроков или конкретные матчи.', 'err'); return; }
    let period;
    try { period = buildPeriod(); } catch (e) { setStatus(e.message, 'err'); return; }

    searching = true; client = new Client(apiKey, setCount);
    setRunning(true); $('#log').innerHTML = ''; $('#matches').innerHTML = '';
    setProgress(0); setCount(0); setStatus('Запрос…');
    ST.results = []; ST.stats = []; ST.searched = new Set(); ST.party = new Set(); ST.directMatchIds = new Set(); ST.sortK = null;

    try {
      const matchUrlsRaw = ($('#matchUrls').value || '').split('\n').map(s => s.trim()).filter(Boolean);
      const directIds = new Set(matchUrlsRaw.map(parseMatchUrl));
      ST.directMatchIds = directIds;
      const logParts = [];
      if (players.length) logParts.push(`игроков: ${players.length} | игра: ${$('#game').value}`);
      if (directIds.size) logParts.push(`матчей напрямую: ${directIds.size}`);
      log(logParts.join(' · '), 'head');
      const ids = {};
      for (const raw of players) {
        const nick = nickFromInput(raw);
        const pid = await getPlayerId(client, nick);
        ids[nick] = pid; log(`  ${nick} → ${pid}`);
      }
      ST.searched = new Set(Object.values(ids));
      const dates = {}, order = [];
      for (const [nick, pid] of Object.entries(ids)) {
        setStatus(`История: ${nick}…`);
        const hist = await getHistory(client, pid, $('#game').value, period);
        log(`  ${nick}: матчей в истории — ${hist.length}`);
        for (const it of hist) {
          const mid = it.match_id;
          if (mid && !(mid in dates)) { dates[mid] = it.finished_at || it.started_at; order.push(mid); }
        }
      }
      for (const mid of directIds) {
        if (mid && !(mid in dates)) { dates[mid] = null; order.push(mid); }
      }
      log(`Уникальных матчей: ${order.length}`, 'head');
      const results = [];
      for (let i = 0; i < order.length; i++) {
        if (client.cancel) { log('Прервано пользователем.', 'warn'); break; }
        setProgress(100 * (i + 1) / order.length); setStatus(`Матч ${i + 1}/${order.length}…`);
        const m = await parseMatch(client, order[i]);
        if (m) { m.date = unixToStr(dates[order[i]]); results.push(m); }
      }
      ST.results = results;
      ST.stats = computeStats(results, ST.searched, ST.directMatchIds);
      ST.party = new Set(partyNicknames(ST.stats));
      renderMatches(); renderParty(); renderStats();
      if (results.length) ['expCsv', 'expTsv', 'expJson'].forEach(id => $('#' + id).disabled = false);
      if (ST.stats.length) ['stCsv', 'stTsv'].forEach(id => { const el = $('#' + id); el.disabled = false; el.style.display = ''; });
      setProgress(100);
      setStatus(`Готово: ${results.length} матч(ей). Запросов: ${client.count}.`, 'ok');
    } catch (e) {
      if (e === CANCELLED) setStatus('Отменено.', 'warn');
      else { log(e.message || String(e), 'err'); setStatus(e.message || 'Ошибка', 'err'); }
    } finally { searching = false; setRunning(false); }
  }

  function renderMatches() {
    const wrap = $('#matches'); wrap.innerHTML = '';
    ST.results.forEach((m, idx) => {
      const st = searchedTeamIndices(m, ST.searched);
      const el = document.createElement('div'); el.className = 'match';
      let html = `<h3><span>Матч #${idx + 1} · ${esc(m.score || '')}</span><span class="map">${esc(m.map || '')} · ${esc(m.date || '')}</span></h3><div class="teams">`;
      m.teams.forEach((t, ti) => {
        html += `<div class="team ${t.won ? 'win' : 'lose'}"><div class="th"><span>${esc(t.name || '')}</span><span class="res">${t.won ? 'WIN' : 'LOSE'}</span></div>`;
        for (const p of t.players) {
          const isS = ST.searched.has(p.player_id);
          let cls = '', mark = '';
          if (isS) { cls = 'self'; mark = '★'; }
          else if (ST.party.has(p.nickname) && st.has(ti)) { cls = 'party'; mark = '◆'; }
          else if (st.has(ti)) { cls = 'mate'; mark = '+'; }
          html += `<div class="pl ${cls}"><span class="nk">${mark ? `<span>${mark}</span>` : ''}${esc(p.nickname || '')}</span><span class="kda">${esc(p.kda || '')} · ${esc(String(p.kd_ratio || ''))}</span></div>`;
        }
        html += '</div>';
      });
      el.innerHTML = html + '</div>'; wrap.append(el);
    });
  }
  function renderParty() {
    const party = ST.stats.filter(s => s.role === 'пати?');
    log(''); log('── Анализ пати (★ искомый, ◆ вероятная пати) ──', 'head');
    if (!party.length) { log('   Постоянных сокомандников не выявлено — похоже на соло-очередь.'); return; }
    party.forEach(s => log(`   ◆ ${s.nickname}  вместе ×${s.with}  против ×${s.vs}  ср.KDA ${s.avg_kda}`, 'warn'));
  }
  function renderStats() {
    const min = +$('#minMatches').value || 1;
    const hasDirect = ST.directMatchIds && ST.directMatchIds.size > 0;
    root.querySelectorAll('.col-direct').forEach(el => el.classList.toggle('hidden', !hasDirect));
    const tb = $('#statsTable').querySelector('tbody'); tb.innerHTML = '';
    let rows = ST.stats.slice();
    if (ST.sortK) {
      const k = ST.sortK;
      rows.sort((a, b) => { const x = a[k], y = b[k]; return (typeof x === 'number') ? x - y : String(x).localeCompare(String(y)); });
      if (ST.sortRev) rows.reverse();
    }
    for (const s of rows) {
      if (!s.is_searched && s.matches < min) continue;
      const tr = document.createElement('tr');
      const rc = s.is_searched ? 'self' : (s.role === 'пати?' ? 'party' : '');
      tr.className = rc;
      tr.innerHTML = `<td>${esc(s.nickname)}</td><td><span class="role ${rc}">${esc(s.role)}</span></td>
        <td>${s.matches}</td><td>${s.with}</td><td>${s.vs}</td>
        <td>${s.avg_k.toFixed(2)}</td><td>${s.avg_d.toFixed(2)}</td><td>${s.avg_a.toFixed(2)}</td>
        <td>${esc(s.avg_kda)}</td><td>${s.avg_kd.toFixed(2)}</td>
        <td class="col-direct">${hasDirect ? (s.direct_app || 0) : ''}</td>`;
      tb.append(tr);
    }
  }

  // ---- экспорт ----
  function flatRows() {
    const out = [];
    for (const m of ST.results) {
      const st = searchedTeamIndices(m, ST.searched);
      m.teams.forEach((t, ti) => {
        const res = t.won ? 'win' : 'lose';
        for (const p of t.players) {
          const isS = ST.searched.has(p.player_id);
          const rel = isS ? 'self' : (st.has(ti) ? 'team' : (st.size ? 'enemy' : ''));
          out.push([m.match_id, m.date || '', m.map || '', m.score, t.name, res, p.nickname,
            p.kills, p.deaths, p.assists, p.kda, p.kd_ratio, isS ? 'yes' : '', rel,
            (!isS && ST.party.has(p.nickname)) ? 'party?' : '']);
        }
      });
    }
    return out;
  }
  function statFlat() {
    return ST.stats.map(s => [s.nickname, s.role, s.matches, s.with, s.vs,
      s.avg_k.toFixed(2), s.avg_d.toFixed(2), s.avg_a.toFixed(2), s.avg_kda, s.avg_kd.toFixed(2), s.direct_app || 0]);
  }
  function toCSV(cols, rows) {
    const e = v => { v = String(v == null ? '' : v); return /[;"\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
    return '﻿' + [cols.join(';'), ...rows.map(r => r.map(e).join(';'))].join('\n');
  }
  function toTSV(cols, rows) { return [cols.join('\t'), ...rows.map(r => r.join('\t'))].join('\n'); }
  function download(name, text, type) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type }));
    a.download = name; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }

  buildUI();
})();
