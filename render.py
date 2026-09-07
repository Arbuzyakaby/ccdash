#!/usr/bin/env python3
"""
ccdash / render.py - сборка дашборда index.html из usage.db.

Читает только агрегаты из SQLite (текста диалогов там нет по построению - см.
collect.py) и складывает один самодостаточный HTML-файл: стили, скрипты и данные
внутри, никаких CDN и сетевых запросов. Открывается двойным кликом.

Палитра проверена валидатором скилла dataviz в обоих режимах:
  типы токенов  - blue / aqua / orange / violet, adjacent-pairs, PASS light+dark
  модели        - green / magenta, all-pairs, PASS light+dark
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def fetch(conn: sqlite3.Connection) -> dict:
    q = conn.execute
    meta = dict(q("SELECT key, value FROM meta"))

    total = q("SELECT COUNT(*), COUNT(DISTINCT project_path), COUNT(DISTINCT session_id),"
              " MIN(date_local), MAX(date_local), SUM(input_tokens), SUM(output_tokens),"
              " SUM(thinking_tokens), SUM(cache_read), SUM(cache_write),"
              " SUM(cost_1h), SUM(cost_5m), COUNT(DISTINCT date_local)"
              " FROM calls").fetchone()

    pricing = json.loads((Path(__file__).resolve().parent / "pricing.json")
                         .read_text(encoding="utf-8"))["models"]

    def cost_parts(where: str = "", args: tuple = ()) -> dict:
        """Разложение стоимости на компоненты - иначе cache read съедает шкалу."""
        parts = {"input": 0.0, "output": 0.0, "cache_write": 0.0, "cache_read": 0.0}
        sql = ("SELECT model, SUM(input_tokens), SUM(output_tokens),"
               " SUM(cache_write_1h), SUM(cache_write_5m), SUM(cache_read)"
               " FROM calls " + where + " GROUP BY model")
        for model, i, o, cw1, cw5, cr in q(sql, args):
            rate = pricing.get(model)
            if not rate:
                continue
            parts["input"] += i * rate["input"] / 1e6
            parts["output"] += o * rate["output"] / 1e6
            parts["cache_write"] += (cw1 * rate["cache_write_1h"]
                                     + cw5 * rate["cache_write_5m"]) / 1e6
            parts["cache_read"] += cr * rate["cache_read"] / 1e6
        return parts

    days = []
    for row in q("SELECT date_local, COUNT(*), SUM(input_tokens), SUM(output_tokens),"
                 " SUM(thinking_tokens), SUM(cache_write), SUM(cache_read), SUM(cost_1h),"
                 " COUNT(DISTINCT session_id), MIN(weekday_local)"
                 " FROM calls GROUP BY date_local ORDER BY date_local"):
        parts = cost_parts("WHERE date_local = ?", (row[0],))
        days.append({
            "date": row[0], "weekday": WEEKDAYS[row[9]], "calls": row[1],
            "input": row[2], "output": row[3], "thinking": row[4],
            "cache_write": row[5], "cache_read": row[6],
            "cost": row[7], "sessions": row[8], "parts": parts,
        })

    projects = []
    for row in q("SELECT project_name, project_path, sessions, calls, input_tokens,"
                 " output_tokens, thinking_tokens, cache_write, cache_read, cost_1h,"
                 " cost_5m, first_day, last_day FROM v_project ORDER BY cost_1h DESC"):
        projects.append({
            "name": row[0], "path": row[1], "sessions": row[2], "calls": row[3],
            "input": row[4], "output": row[5], "thinking": row[6],
            "cache_write": row[7], "cache_read": row[8],
            "cost": row[9], "cost5m": row[10], "first": row[11], "last": row[12],
        })

    models = []
    for row in q("SELECT model, COUNT(*), SUM(input_tokens), SUM(output_tokens),"
                 " SUM(thinking_tokens), SUM(cache_write), SUM(cache_read), SUM(cost_1h),"
                 " COUNT(DISTINCT session_id)"
                 " FROM calls GROUP BY model ORDER BY SUM(cost_1h) DESC"):
        models.append({
            "model": row[0], "calls": row[1], "input": row[2], "output": row[3],
            "thinking": row[4], "cache_write": row[5], "cache_read": row[6],
            "cost": row[7], "sessions": row[8],
        })

    cross = [{"project": r[0], "model": r[1], "calls": r[2], "cost": r[3]}
             for r in q("SELECT project_name, model, COUNT(*), SUM(cost_1h)"
                        " FROM calls GROUP BY project_name, model")]

    hours = {h: {"hour": h, "calls": 0, "cost": 0.0, "output": 0} for h in range(24)}
    for h, calls, cost, out in q("SELECT hour_local, COUNT(*), SUM(cost_1h),"
                                 " SUM(output_tokens) FROM calls GROUP BY hour_local"):
        hours[h] = {"hour": h, "calls": calls, "cost": cost, "output": out}

    efforts = [{"effort": r[0], "calls": r[1], "output": r[2], "thinking": r[3],
                "cost": r[4]}
               for r in q("SELECT effort, COUNT(*), SUM(output_tokens),"
                          " SUM(thinking_tokens), SUM(cost_1h) FROM calls"
                          " GROUP BY effort ORDER BY SUM(cost_1h) DESC")]

    sessions = [{"id": r[0], "project": r[1], "calls": r[2], "started": r[3],
                 "ended": r[4], "models": r[5], "output": r[6], "thinking": r[7],
                 "cache_read": r[8], "cache_write": r[9], "cost": r[10]}
                for r in q("SELECT session_id, project_name, calls, started, ended,"
                           " models, output_tokens, thinking_tokens, cache_read,"
                           " cache_write, cost_1h FROM v_session"
                           " ORDER BY cost_1h DESC LIMIT 10")]

    branches = [{"branch": r[0] or "(нет ветки)", "calls": r[1], "cost": r[2]}
                for r in q("SELECT git_branch, COUNT(*), SUM(cost_1h) FROM calls"
                           " GROUP BY git_branch ORDER BY 3 DESC")]

    subagents = [{"kind": "сабагенты" if r[0] else "основной поток", "calls": r[1],
                  "output": r[2], "cost": r[3]}
                 for r in q("SELECT is_subagent, COUNT(*), SUM(output_tokens),"
                            " SUM(cost_1h) FROM calls GROUP BY is_subagent"
                            " ORDER BY is_subagent")]

    return {
        "meta": meta,
        "totals": {
            "calls": total[0], "projects": total[1], "sessions": total[2],
            "first": total[3], "last": total[4], "input": total[5],
            "output": total[6], "thinking": total[7], "cache_read": total[8],
            "cache_write": total[9], "cost": total[10], "cost5m": total[11],
            "days": total[12], "parts": cost_parts(),
        },
        "days": days, "projects": projects, "models": models, "cross": cross,
        "hours": list(hours.values()), "efforts": efforts, "sessions": sessions,
        "branches": branches, "subagents": subagents,
    }


CSS = (Path(__file__).resolve().parent / "theme.css").read_text(encoding="utf-8")

JS = r"""
const D = window.__CCDASH__;
const $ = (s, r=document) => r.querySelector(s);
const NS = "http://www.w3.org/2000/svg";

/* ---------- форматирование ---------- */
const nf = new Intl.NumberFormat("ru-RU");
const money = v => "$" + v.toLocaleString("ru-RU", {minimumFractionDigits:2, maximumFractionDigits:2});
const money0 = v => "$" + v.toLocaleString("ru-RU", {maximumFractionDigits: v < 10 ? 2 : 0});
function compact(v) {
  const a = Math.abs(v);
  if (a >= 1e9) return (v/1e9).toFixed(a >= 1e10 ? 0 : 1).replace(".", ",") + " млрд";
  if (a >= 1e6) return (v/1e6).toFixed(a >= 1e7 ? 0 : 1).replace(".", ",") + " млн";
  if (a >= 1e4) return Math.round(v/1e3) + " тыс.";
  return nf.format(v);
}
const pct = (a, b) => b ? (a/b*100).toFixed(1).replace(".", ",") + " %" : "—";
const el = (tag, attrs={}, parent=null) => {
  const n = document.createElementNS(NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
};

/* ---------- тултип ---------- */
const tip = $("#tip");
function showTip(ev, title, rows) {
  tip.innerHTML = '<div class="t"></div>' + rows.map(() => '<div class="r"><span></span><b></b></div>').join("");
  tip.firstChild.textContent = title;
  const rs = tip.querySelectorAll(".r");
  rows.forEach((r, i) => { rs[i].firstChild.textContent = r[0]; rs[i].lastChild.textContent = r[1]; });
  tip.style.opacity = 1;
  moveTip(ev);
}
function moveTip(ev) {
  const b = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 16;
  if (x + b.width > innerWidth - 12) x = ev.clientX - b.width - 14;
  if (y + b.height > innerHeight - 12) y = ev.clientY - b.height - 16;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
const hideTip = () => { tip.style.opacity = 0; };
function bindTip(node, title, rows) {
  node.addEventListener("mouseenter", e => showTip(e, title, rows));
  node.addEventListener("mousemove", moveTip);
  node.addEventListener("mouseleave", hideTip);
}

/* ---------- шкала ---------- */
function ticks(max, n=4) {
  if (max <= 0) return [0];
  const raw = max / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1,2,2.5,5,10].map(m => m*mag).find(s => s >= raw) || 10*mag;
  const out = []; for (let v = 0; v <= max*1.0001; v += step) out.push(v);
  if (out[out.length-1] < max) out.push(out[out.length-1] + step);
  return out;
}

/* Столбик со скруглённым верхом и квадратным основанием. */
function capPath(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, w/2, h));
  if (h <= 0.5) return "";
  return `M${x},${y+h} L${x},${y+r} Q${x},${y} ${x+r},${y} L${x+w-r},${y} Q${x+w},${y} ${x+w},${y+r} L${x+w},${y+h} Z`;
}
/* Полоса со скруглённым правым концом. */
function tipPath(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, w, h/2));
  if (w <= 0.5) return "";
  return `M${x},${y} L${x+w-r},${y} Q${x+w},${y} ${x+w},${y+r} L${x+w},${y+h-r} Q${x+w},${y+h} ${x+w-r},${y+h} L${x},${y+h} Z`;
}

const GAP = 2;          // разделитель цветом поверхности между сегментами
const BAR_MAX = 24;     // марки тонкие: ни один столбик не заливает слот

/* =========================================================== стек по дням */
function drawDaily(host) {
  const W = 900, H = 300, m = {t:14, r:16, b:44, l:56};
  const svg = el("svg", {viewBox:`0 0 ${W} ${H}`, role:"img",
                         "aria-label":"Стоимость по дням, разложенная на типы токенов"}, host);
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const max = Math.max(...D.days.map(d => d.cost));
  const tk = ticks(max), top = tk[tk.length-1];
  const y = v => m.t + ih - v/top*ih;

  tk.forEach(t => {
    el("line", {class:"grid", x1:m.l, x2:m.l+iw, y1:y(t), y2:y(t)}, svg);
    const tx = el("text", {class:"axis", x:m.l-10, y:y(t)+4, "text-anchor":"end"}, svg);
    tx.textContent = money0(t);
  });

  const band = iw / D.days.length;
  const bw = Math.min(BAR_MAX, band * 0.52);
  const series = [
    ["cache_read",  "--series-cr"],
    ["cache_write", "--series-cw"],
    ["output",      "--series-out"],
  ];
  D.days.forEach((d, i) => {
    const cx = m.l + band*i + band/2, x = cx - bw/2;
    let acc = 0;
    const segs = series.map(([k, c]) => ({k, c, v: d.parts[k]})).filter(s => s.v > 0);
    segs.forEach((s, idx) => {
      // Сегменты разделяет разрыв цветом поверхности, а не обводка: разрыв
      // забирается сверху у каждого сегмента, кроме самого верхнего.
      const isTop = idx === segs.length - 1;
      const bottom = y(acc), rawTop = y(acc + s.v);
      const drawTop = rawTop + (isTop ? 0 : GAP);
      const h = bottom - drawTop;
      if (h > 0.5) {
        el("path", {d: isTop ? capPath(x, drawTop, bw, h, 4)
                             : `M${x},${drawTop} h${bw} v${h} h${-bw} Z`,
                    fill:`var(${s.c})`}, svg);
      }
      acc += s.v;
    });
    const lb = el("text", {class:"dlabel", x:cx, y:y(d.cost)-9, "text-anchor":"middle"}, svg);
    lb.textContent = money0(d.cost);
    const a1 = el("text", {class:"axis-strong", x:cx, y:m.t+ih+18, "text-anchor":"middle"}, svg);
    a1.textContent = d.date.slice(8) + "." + d.date.slice(5,7);
    const a2 = el("text", {class:"axis", x:cx, y:m.t+ih+32, "text-anchor":"middle"}, svg);
    a2.textContent = d.weekday;

    const hit = el("rect", {class:"hit", x:m.l+band*i, y:m.t, width:band, height:ih}, svg);
    bindTip(hit, d.date + " · " + d.weekday, [
      ["Всего", money(d.cost)],
      ["  чтение кэша", money(d.parts.cache_read)],
      ["  запись кэша", money(d.parts.cache_write)],
      ["  output", money(d.parts.output)],
      ["Вызовов", nf.format(d.calls)],
      ["Сессий", nf.format(d.sessions)],
      ["Токенов всего", compact(d.input+d.output+d.cache_read+d.cache_write)],
    ]);
  });
}

/* =================================================== горизонтальные полосы */
function drawBars(host, rows, opts) {
  const {label, value, fmt, color, tipRows, showAll} = opts;
  const data = showAll ? rows : rows;
  const rowH = 30, W = 900, m = {t:6, r:78, b:6, l:196};
  const H = m.t + m.b + rowH*data.length;
  const svg = el("svg", {viewBox:`0 0 ${W} ${H}`, role:"img",
                         "aria-label":opts.aria}, host);
  const iw = W - m.l - m.r;
  const max = Math.max(...data.map(value));
  data.forEach((d, i) => {
    const y0 = m.t + rowH*i, bh = Math.min(BAR_MAX*0.75, rowH-12);
    const yb = y0 + (rowH - bh)/2;
    const w = max ? value(d)/max*iw : 0;
    const nm = el("text", {class:"axis-strong", x:m.l-14, y:yb+bh/2+4,
                           "text-anchor":"end"}, svg);
    nm.textContent = label(d).length > 26 ? label(d).slice(0,25)+"…" : label(d);
    el("path", {d: tipPath(m.l, yb, Math.max(w, 2), bh, 4),
                fill: typeof color === "function" ? color(d) : `var(${color})`}, svg);
    const vl = el("text", {class:"dlabel", x:m.l+Math.max(w,2)+10, y:yb+bh/2+4}, svg);
    vl.textContent = fmt(d);
    const hit = el("rect", {class:"hit", x:0, y:y0, width:W, height:rowH}, svg);
    bindTip(hit, label(d), tipRows(d));
  });
}

/* ============================================================ доля моделей */
function drawShare(host, parts, colors) {
  const W = 900, H = 54, m = {l:0, r:0};
  const svg = el("svg", {viewBox:`0 0 ${W} ${H}`, role:"img",
                         "aria-label":"Доля моделей в стоимости"}, host);
  const total = parts.reduce((a, p) => a + p.value, 0);
  let x = 0;
  parts.forEach((p, i) => {
    const w = p.value/total*W - (i ? GAP : 0);
    const xx = x + (i ? GAP : 0);
    let d;
    if (parts.length === 1) d = tipPath(xx, 0, w, 30, 4);
    else if (i === 0) d = `M${xx+4},0 h${w-4} v30 h${-(w-4)} q-4,0 -4,-4 v-22 q0,-4 4,-4 Z`;
    else if (i === parts.length-1) d = tipPath(xx, 0, w, 30, 4);
    else d = `M${xx},0 h${w} v30 h${-w} Z`;
    el("path", {d, fill:colors[i]}, svg);
    if (w > 120) {
      // Текст внутри цветной заливки - белый или чернильный по яркости заливки.
      // Ставим инлайн-стилем: класс .dlabel иначе перебивает атрибут fill.
      const t = el("text", {x:xx+12, y:20,
                            style:`font-size:11.5px;font-weight:600;fill:${p.ink}`}, svg);
      t.textContent = p.name + " · " + pct(p.value, total);
    }
    const hit = el("rect", {class:"hit", x:xx, y:0, width:w, height:30}, svg);
    bindTip(hit, p.name, p.rows);
    x += p.value/total*W;
  });
}

/* =========================================== точки на приближённой шкале */
/* Значения жмутся в узкий диапазон (94-99 %), и полосы от нуля неразличимы.
   Полоса обязана начинаться с нуля, точка - нет: поэтому здесь точки на
   шкале, приближенной к данным, с тонким поводком к левому краю. */
function drawDots(host, rows, opts) {
  const {label, value, fmt, color, tipRows, aria, axisFmt} = opts;
  const rowH = 30, W = 900, m = {t:26, r:78, b:8, l:196};
  const H = m.t + m.b + rowH*rows.length;
  const svg = el("svg", {viewBox:`0 0 ${W} ${H}`, role:"img", "aria-label":aria}, host);
  const iw = W - m.l - m.r;
  const vals = rows.map(value);
  const lo = Math.floor(Math.min(...vals) - 1), hi = Math.ceil(Math.max(...vals));
  const x = v => m.l + (v - lo)/(hi - lo)*iw;

  for (let t = lo; t <= hi; t += (hi - lo <= 6 ? 1 : 2)) {
    el("line", {class:"grid", x1:x(t), x2:x(t), y1:m.t-6, y2:m.t+rowH*rows.length-8}, svg);
    const tx = el("text", {class:"axis", x:x(t), y:m.t-12, "text-anchor":"middle"}, svg);
    tx.textContent = axisFmt(t);
  }
  rows.forEach((d, i) => {
    const cy = m.t + rowH*i + rowH/2 - 4, v = value(d), cx = x(v);
    const nm = el("text", {class:"axis-strong", x:m.l-14, y:cy+4, "text-anchor":"end"}, svg);
    nm.textContent = label(d).length > 26 ? label(d).slice(0,25)+"…" : label(d);
    el("line", {x1:m.l, x2:cx, y1:cy, y2:cy,
                stroke:"var(--line-strong)", "stroke-width":1}, svg);
    // кольцо цветом поверхности, чтобы точка читалась поверх поводка и сетки
    el("circle", {cx, cy, r:6, fill:`var(${color})`,
                  stroke:"var(--surface-1)", "stroke-width":2}, svg);
    const vl = el("text", {class:"dlabel", x:cx+14, y:cy+4}, svg);
    vl.textContent = fmt(d);
    const hit = el("rect", {class:"hit", x:0, y:m.t+rowH*i-4, width:W, height:rowH}, svg);
    bindTip(hit, label(d), tipRows(d));
  });
}

/* ================================================================ колонки */
function drawColumns(host, rows, opts) {
  const {W=900, H=210, x:xf, value, fmt, color, tipRows, labelEvery=1, aria} = opts;
  const m = {t:16, r:12, b:34, l:56};
  const svg = el("svg", {viewBox:`0 0 ${W} ${H}`, role:"img", "aria-label":aria}, host);
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const max = Math.max(...rows.map(value), 0.0001);
  const tk = ticks(max, 3), top = tk[tk.length-1];
  const y = v => m.t + ih - v/top*ih;
  tk.forEach(t => {
    el("line", {class:"grid", x1:m.l, x2:m.l+iw, y1:y(t), y2:y(t)}, svg);
    const tx = el("text", {class:"axis", x:m.l-10, y:y(t)+4, "text-anchor":"end"}, svg);
    tx.textContent = opts.tickFmt ? opts.tickFmt(t) : money0(t);
  });
  const band = iw/rows.length, bw = Math.min(BAR_MAX, band*0.62);
  rows.forEach((d, i) => {
    const cx = m.l + band*i + band/2, v = value(d);
    if (v > 0) el("path", {d: capPath(cx-bw/2, y(v), bw, m.t+ih-y(v), 4),
                           fill: typeof color === "function" ? color(d) : `var(${color})`}, svg);
    if (i % labelEvery === 0) {
      const t = el("text", {class:"axis", x:cx, y:m.t+ih+18, "text-anchor":"middle"}, svg);
      t.textContent = xf(d, i);
    }
    const hit = el("rect", {class:"hit", x:m.l+band*i, y:m.t, width:band, height:ih}, svg);
    bindTip(hit, opts.tipTitle(d), tipRows(d));
  });
}

/* ================================================================= сборка */
function tableToggle(section) {
  const btn = section.querySelector("[data-toggle]");
  if (!btn) return;
  const target = section.querySelector(".tablewrap");
  const chart = section.querySelector(".chartbox");
  btn.addEventListener("click", () => {
    const showTable = target.hidden;
    target.hidden = !showTable;
    if (chart) chart.hidden = showTable;
    btn.textContent = showTable ? "Показать график" : "Показать таблицей";
  });
}

function boot() {
  /* тема */
  const root = document.documentElement;
  const tbtn = $("#theme");
  const apply = t => {
    if (t === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", t);
    tbtn.textContent = {system:"Тема: системная", light:"Тема: светлая", dark:"Тема: тёмная"}[t];
    try { localStorage.setItem("ccdash-theme", t); } catch (e) {}
  };
  let theme = "system";
  try { theme = localStorage.getItem("ccdash-theme") || "system"; } catch (e) {}
  apply(theme);
  tbtn.addEventListener("click", () => {
    theme = {system:"light", light:"dark", dark:"system"}[theme];
    apply(theme);
  });

  /* кнопка обновления */
  const rbtn = $("#refresh");
  rbtn.addEventListener("click", async () => {
    const cmd = D.meta.rebuild_command;
    try {
      await navigator.clipboard.writeText(cmd);
      rbtn.textContent = "Команда скопирована";
    } catch (e) {
      rbtn.textContent = cmd;
    }
    setTimeout(() => { rbtn.textContent = "Обновить данные"; }, 3200);
  });

  drawDaily($("#chart-daily"));

  drawBars($("#chart-projects"), D.projects, {
    aria: "Стоимость по проектам",
    label: d => d.name,
    value: d => d.cost,
    fmt: d => money0(d.cost) + "  ·  " + pct(d.cost, D.totals.cost),
    color: "--series-cr",
    tipRows: d => [
      ["Стоимость", money(d.cost)],
      ["Доля", pct(d.cost, D.totals.cost)],
      ["Сессий", nf.format(d.sessions)],
      ["Вызовов", nf.format(d.calls)],
      ["Output", compact(d.output)],
      ["Чтение кэша", compact(d.cache_read)],
      ["Период", d.first + " … " + d.last],
    ],
  });

  const mcolors = ["var(--model-a)", "var(--model-b)"];
  const mink = ["#ffffff", "#2b1220"];   // по яркости model-a / model-b
  drawShare($("#chart-models"), D.models.map((m, i) => ({
    name: m.model, value: m.cost, ink: mink[i] || "#ffffff",
    rows: [["Стоимость", money(m.cost)], ["Доля", pct(m.cost, D.totals.cost)],
           ["Вызовов", nf.format(m.calls)], ["Output", compact(m.output)],
           ["Thinking", compact(m.thinking)], ["Чтение кэша", compact(m.cache_read)],
           ["Цена вызова", money(m.cost/m.calls)]],
  })), mcolors);

  drawColumns($("#chart-hours"), D.hours, {
    aria: "Активность по часам суток",
    x: d => String(d.hour).padStart(2, "0"),
    value: d => d.cost,
    color: "--series-cr",
    labelEvery: 2,
    tipTitle: d => String(d.hour).padStart(2, "0") + ":00 – " +
                   String(d.hour).padStart(2, "0") + ":59",
    tipRows: d => [["Стоимость", money(d.cost)], ["Вызовов", nf.format(d.calls)],
                   ["Output", compact(d.output)]],
  });

  const ramp = ["--ramp-1", "--ramp-2", "--ramp-3", "--ramp-4"];
  const order = ["low", "medium", "high", "xhigh"];
  drawColumns($("#chart-effort"), D.efforts.slice().sort(
      (a,b) => order.indexOf(a.effort) - order.indexOf(b.effort)), {
    aria: "Стоимость по уровням effort",
    H: 200,
    x: d => d.effort,
    value: d => d.cost,
    color: d => `var(${ramp[Math.max(0, order.indexOf(d.effort))]})`,
    tipTitle: d => "effort: " + d.effort,
    tipRows: d => [["Стоимость", money(d.cost)], ["Вызовов", nf.format(d.calls)],
                   ["Output", compact(d.output)],
                   ["Thinking", compact(d.thinking) + " (" + pct(d.thinking, d.output) + ")"]],
  });

  drawDots($("#chart-cache"), D.projects.filter(p => p.calls >= 10)
             .slice().sort((a,b) =>
               b.cache_read/(b.cache_read+b.cache_write+b.input) -
               a.cache_read/(a.cache_read+a.cache_write+a.input)), {
    aria: "Доля чтения кэша во входящих токенах по проектам",
    label: d => d.name,
    value: d => d.cache_read / (d.cache_read + d.cache_write + d.input) * 100,
    fmt: d => pct(d.cache_read, d.cache_read + d.cache_write + d.input),
    axisFmt: t => t + " %",
    color: "--series-cw",
    tipRows: d => [
      ["Чтение кэша", compact(d.cache_read)],
      ["Запись кэша", compact(d.cache_write)],
      ["Доля чтения", pct(d.cache_read, d.cache_read + d.cache_write + d.input)],
      ["Цена часового кэша", money(d.cost - d.cost5m)],
    ],
  });

  document.querySelectorAll("section").forEach(tableToggle);

  // Motion 2/10 по рекомендации скилла: короткое проявление секции при
  // входе в вьюпорт. При prefers-reduced-motion класс не вешается вовсе.
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches &&
      "IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach(e => {
        if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
      });
    }, {rootMargin: "0px 0px -8% 0px"});
    document.querySelectorAll("section").forEach(n => {
      n.classList.add("reveal"); io.observe(n);
    });
  }
}
document.addEventListener("DOMContentLoaded", boot);
"""


def esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def fmt_money(v: float) -> str:
    return f"${v:,.2f}".replace(",", " ")


def fmt_int(v) -> str:
    return f"{int(v):,}".replace(",", " ")


def compact_ru(v: float) -> str:
    v = float(v)
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}".replace(".", ",") + " млрд"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f}".replace(".", ",") + " млн"
    if abs(v) >= 1e4:
        return fmt_int(round(v / 1e3)) + " тыс."
    return fmt_int(v)


def pct(a: float, b: float) -> str:
    return f"{a / b * 100:.1f}".replace(".", ",") + " %" if b else "—"


def table(headers: list, rows: list, foot: list | None = None) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    tfoot = ("<tfoot><tr>" + "".join(f"<td>{c}</td>" for c in foot) + "</tr></tfoot>"
             if foot else "")
    return (f'<div class="tablewrap" hidden><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody>{tfoot}</table></div>")


def section(anchor: str, title: str, caption: str, legend: str = "",
            chart_id: str = "", extra: str = "", table_html: str = "",
            toggle: bool = True) -> str:
    btn = ('<button class="ghost" data-toggle>Показать таблицей</button>'
           if toggle and table_html else "")
    chart = (f'<div class="card panel chartbox">{legend}<div id="{chart_id}"></div>'
             f"{extra}</div>" if chart_id else extra)
    return f"""
<section id="{anchor}">
  <div class="head"><h2>{title}</h2><div class="spacer"></div>{btn}</div>
  <p class="cap">{caption}</p>
  {chart}
  {table_html}
</section>"""


def legend_html(items: list) -> str:
    return ('<div class="legend">' + "".join(
        f'<span><i class="swatch" style="background:var({c})"></i>{esc(n)}</span>'
        for n, c in items) + "</div>")


def build_html(d: dict) -> str:
    t = d["totals"]
    parts = t["parts"]
    tokens_total = t["input"] + t["output"] + t["cache_read"] + t["cache_write"]
    meta = d["meta"]
    dup_pct = (int(meta["duplicates_dropped"]) /
               max(int(meta["usage_rows_raw"]), 1) * 100)
    cache_hit = t["cache_read"] / (t["cache_read"] + t["cache_write"] + t["input"]) * 100
    top_project = d["projects"][0]

    # --- плитки
    tiles = [
        ("Токенов всего", compact_ru(tokens_total),
         f"{pct(t['cache_read'], tokens_total)} — чтение кэша"),
        ("Вызовов к API", fmt_int(t["calls"]),
         f"в среднем {fmt_money(t['cost'] / t['calls'])} за вызов"),
        ("Сессий", fmt_int(t["sessions"]), f"в {t['projects']} проектах"),
        ("Дней с активностью", fmt_int(t["days"]),
         f"{t['first']} … {t['last']}"),
        ("Попаданий в кэш", pct(t["cache_read"],
                                t["cache_read"] + t["cache_write"] + t["input"]),
         "доля чтения во входящих"),
        ("Thinking", compact_ru(t["thinking"]),
         f"{pct(t['thinking'], t['output'])} от всего output"),
    ]
    tiles_html = "".join(
        f'<div class="tile"><div class="label">{esc(a)}</div>'
        f'<div class="value">{esc(b)}</div><div class="hint">{esc(c)}</div></div>'
        for a, b, c in tiles)

    # --- дни
    days_rows = [[
        f'{r["date"]} <span class="mono">{r["weekday"]}</span>', fmt_int(r["calls"]),
        fmt_int(r["sessions"]), fmt_int(r["input"]), compact_ru(r["output"]),
        compact_ru(r["cache_write"]), compact_ru(r["cache_read"]),
        fmt_money(r["cost"]),
    ] for r in d["days"]]
    days_foot = ["Итого", fmt_int(t["calls"]), fmt_int(t["sessions"]),
                 fmt_int(t["input"]), compact_ru(t["output"]),
                 compact_ru(t["cache_write"]), compact_ru(t["cache_read"]),
                 fmt_money(t["cost"])]

    # --- проекты
    proj_rows = [[
        f'{esc(r["name"])} <span class="mono">{esc(r["path"])}</span>',
        fmt_int(r["sessions"]), fmt_int(r["calls"]), compact_ru(r["output"]),
        compact_ru(r["cache_write"]), compact_ru(r["cache_read"]),
        fmt_money(r["cost"]), pct(r["cost"], t["cost"]),
    ] for r in d["projects"]]

    # --- модели
    model_rows = [[
        f'<span class="key"><i class="swatch" style="background:'
        f'{"var(--model-a)" if i == 0 else "var(--model-b)"}"></i>{esc(r["model"])}</span>',
        fmt_int(r["calls"]), compact_ru(r["output"]), compact_ru(r["thinking"]),
        compact_ru(r["cache_read"]), fmt_money(r["cost"] / r["calls"]),
        fmt_money(r["cost"]), pct(r["cost"], t["cost"]),
    ] for i, r in enumerate(d["models"])]

    # --- модель x проект
    names = [p["name"] for p in d["projects"]]
    model_names = [m["model"] for m in d["models"]]
    grid = {(c["project"], c["model"]): c for c in d["cross"]}
    cross_rows = []
    for name in names:
        cells = [esc(name)]
        for mn in model_names:
            c = grid.get((name, mn))
            cells.append(
                f'{fmt_money(c["cost"])} <span class="mono">{fmt_int(c["calls"])}</span>'
                if c else '<span class="mono">—</span>')
        cross_rows.append(cells)

    # --- часы
    hour_rows = [[f'{r["hour"]:02d}:00', fmt_int(r["calls"]),
                  compact_ru(r["output"]), fmt_money(r["cost"])]
                 for r in d["hours"] if r["calls"]]

    # --- effort
    order = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
    effort_rows = [[
        esc(r["effort"]), fmt_int(r["calls"]), compact_ru(r["output"]),
        compact_ru(r["thinking"]), pct(r["thinking"], r["output"]),
        fmt_money(r["cost"]),
    ] for r in sorted(d["efforts"], key=lambda r: order.get(r["effort"], 9))]

    # --- сессии
    sess_rows = []
    for i, s in enumerate(d["sessions"], 1):
        span = f'{s["started"][:16].replace("T", " ")} → {s["ended"][11:16]}'
        sess_rows.append([
            f'{i}. {esc(s["project"])}<br><span class="mono">{esc(s["id"])}</span>',
            fmt_int(s["calls"]), span,
            esc(s["models"].replace(",", ", ")),
            compact_ru(s["output"]), compact_ru(s["cache_read"]),
            fmt_money(s["cost"]),
        ])

    # --- кэш
    cache_rows = [[
        esc(r["name"]),
        pct(r["cache_read"], r["cache_read"] + r["cache_write"] + r["input"]),
        compact_ru(r["cache_read"]), compact_ru(r["cache_write"]),
        fmt_money(r["cost"]), fmt_money(r["cost5m"]),
        fmt_money(r["cost"] - r["cost5m"]),
    ] for r in d["projects"] if r["calls"] >= 10]

    # --- прочее
    branch_rows = [[esc(r["branch"]), fmt_int(r["calls"]), fmt_money(r["cost"]),
                    pct(r["cost"], t["cost"])] for r in d["branches"]]
    sub_rows = [[esc(r["kind"]), fmt_int(r["calls"]), compact_ru(r["output"]),
                 fmt_money(r["cost"]), pct(r["cost"], t["cost"])]
                for r in d["subagents"]]

    # --- санитарные проверки
    checks = [
        ("битых JSON-строк", meta["bad_json_lines"]),
        ("записей без timestamp", meta["rows_no_timestamp"]),
        ("записей без модели", meta["rows_no_model"]),
        ("записей без рабочего каталога", meta["rows_no_cwd"]),
        ("записей без ключа дедупа", meta["rows_no_dedup_key"]),
        ("моделей без цены", "0" if meta["unknown_models"] == "{}"
         else meta["unknown_models"]),
        ("новых полей в usage", "0" if meta["unknown_usage_keys"] == "{}"
         else meta["unknown_usage_keys"]),
    ]
    checks_html = "".join(
        f'<div class="check"><i class="dot '
        f'{"ok" if str(v) in ("0", "{}") else "warn"}"></i>'
        f"<span>{esc(k)}: <b>{esc(v)}</b></span></div>" for k, v in checks)

    payload = json.dumps({**d, "meta": {**meta,
                                        "rebuild_command": "python collect.py"}},
                         ensure_ascii=False, separators=(",", ":"))

    sections = "".join([
        section(
            "days", "Расход по дням",
            f"Столбик — стоимость дня, разложенная на то, из чего она сложилась. "
            f"Ровно поэтому здесь деньги, а не токены: чтение кэша даёт "
            f"{pct(t['cache_read'], tokens_total)} объёма, и в шкале токенов "
            f"остальные три типа просто не видны. В деньгах пропорция честнее — "
            f"<b>{pct(parts['cache_read'], t['cost'])} чтение кэша, "
            f"{pct(parts['cache_write'], t['cost'])} запись, "
            f"{pct(parts['output'], t['cost'])} output</b>. "
            f"Входящие токены — {fmt_money(parts['input'])} за всю историю, "
            f"отдельным сегментом их нарисовать нельзя, они есть в таблице.",
            legend_html([("Чтение кэша", "--series-cr"),
                         ("Запись кэша", "--series-cw"),
                         ("Output", "--series-out")]),
            "chart-daily",
            table_html=table(["Дата", "Вызовов", "Сессий", "Input", "Output",
                              "Запись кэша", "Чтение кэша", "Стоимость"],
                             days_rows, days_foot)),
        section(
            "projects", "Проекты",
            f"<b>{esc(top_project['name'])}</b> забирает "
            f"{pct(top_project['cost'], t['cost'])} всей стоимости — "
            f"{fmt_money(top_project['cost'])} за {fmt_int(top_project['calls'])} "
            f"вызовов в {fmt_int(top_project['sessions'])} сессиях. "
            f"Подкаталоги схлопнуты в родительский проект: работа в "
            f"<code>RusSubjectMap\\geo_raw</code> считается тем же проектом, "
            f"что и работа в его корне.",
            "", "chart-projects",
            table_html=table(["Проект", "Сессий", "Вызовов", "Output",
                              "Запись кэша", "Чтение кэша", "Стоимость", "Доля"],
                             proj_rows)),
        section(
            "models", "Модели",
            f"Opus дороже за токен, но вы зовёте его реже. "
            f"В пересчёте на один вызов разрыв нагляднее всего: "
            + " против ".join(
                f"<b>{fmt_money(m['cost'] / m['calls'])}</b> у {esc(m['model'])}"
                for m in d["models"]) + ".",
            legend_html([(m["model"], "--model-a" if i == 0 else "--model-b")
                         for i, m in enumerate(d["models"])]),
            "chart-models",
            extra="",
            table_html=table(["Модель", "Вызовов", "Output", "Thinking",
                              "Чтение кэша", "Цена вызова", "Стоимость", "Доля"],
                             model_rows)),
        section(
            "cross", "Модель × проект",
            "Где какая модель работала. В ячейке — стоимость и число вызовов.",
            "", "",
            extra='<div class="card panel">'
                  + table(["Проект"] + model_names, cross_rows).replace(
                      ' hidden>', ">")
                  + "</div>",
            toggle=False),
        section(
            "hours", "Часы суток",
            "Локальное время. Видно, что рабочий день разорван: утренний заход, "
            "провал в середине дня и главный пик ближе к вечеру.",
            "", "chart-hours",
            table_html=table(["Час", "Вызовов", "Output", "Стоимость"], hour_rows)),
        section(
            "cache", "Эффективность кэша",
            f"Доля чтения кэша во всех входящих токенах — чем выше, тем реже "
            f"контекст пересобирается с нуля. Показаны проекты от 10 вызовов. "
            f"Отдельно: весь ваш кэш записывается на час (тариф 2× от базового "
            f"input), а не на пять минут (1.25×). Разница за всю историю — "
            f"<b>{fmt_money(t['cost'] - t['cost5m'])}</b> из {fmt_money(t['cost'])}: "
            f"столько стоит то, что кэш живёт дольше. Шкала графика приближена к данным — все проекты лежат в узкой полосе, и от нуля разницы между ними не видно вовсе.",
            "", "chart-cache",
            table_html=table(["Проект", "Доля чтения", "Чтение кэша", "Запись кэша",
                              "Стоимость (кэш 1ч)", "Было бы при 5 мин", "Разница"],
                             cache_rows)),
        section(
            "effort", "Effort и thinking",
            f"Thinking-токены — <b>{pct(t['thinking'], t['output'])}</b> всего output "
            f"({compact_ru(t['thinking'])} из {compact_ru(t['output'])}). "
            f"Уровень effort задаёт, насколько глубоко модель рассуждает, "
            f"и напрямую бьёт по этой доле.",
            "", "chart-effort",
            table_html=table(["Effort", "Вызовов", "Output", "Thinking",
                              "Доля thinking", "Стоимость"], effort_rows)),
        section(
            "sessions", "Топ-10 самых дорогих сессий",
            "Одна сессия — один непрерывный разговор. Длинные сессии дороги "
            "нелинейно: каждый следующий вызов перечитывает весь накопленный "
            "контекст, поэтому стоимость растёт быстрее числа сообщений.",
            "", "",
            extra='<div class="card panel">'
                  + table(["Сессия", "Вызовов", "Время", "Модели", "Output",
                           "Чтение кэша", "Стоимость"], sess_rows).replace(
                      ' hidden>', ">")
                  + "</div>",
            toggle=False),
        section(
            "misc", "Сабагенты и ветки",
            "Два разреза, которые пока почти ничего не показывают, но станут "
            "осмысленными, если вы начнёте активнее пользоваться сабагентами "
            "или вести работу в ветках.",
            "", "",
            extra='<div class="card panel">'
                  + table(["Поток", "Вызовов", "Output", "Стоимость", "Доля"],
                          sub_rows).replace(' hidden>', ">")
                  + '<div style="height:22px"></div>'
                  + table(["Git-ветка", "Вызовов", "Стоимость", "Доля"],
                          branch_rows).replace(' hidden>', ">")
                  + "</div>",
            toggle=False),
    ])

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Расход токенов Claude Code</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

<div class="top">
  <div>
    <h1>Расход токенов Claude Code</h1>
    <div class="sub">{t['first']} — {t['last']} · {fmt_int(t['calls'])} вызовов ·
      {fmt_int(t['sessions'])} сессий · {t['projects']} проектов<br>
      Собрано {esc(meta['built_at'][:16].replace('T', ' '))}</div>
  </div>
  <div class="spacer"></div>
  <div class="toolbar">
    <button class="ghost" id="refresh">Обновить данные</button>
    <button class="ghost" id="theme">Тема</button>
  </div>
</div>

<div class="hero-row">
  <div class="card hero">
    <div class="label">Стоимость по тарифам API</div>
    <div class="value">{fmt_money(t['cost'])}</div>
    <div class="note">Это оценка «во сколько обошлось бы то же самое по тарифам
      Anthropic API». У вас подписка Claude Pro — реальный платёж фиксированный
      и от этой цифры не зависит.</div>
  </div>
  <div class="tiles">{tiles_html}</div>
</div>

{sections}

<footer>
  <h3>Как это посчитано</h3>
  <p>Данные берутся из транскриптов сессий в
    <code>{esc(meta['source_dir'])}</code>: прочитано
    {esc(meta['files_scanned'])} файлов и {compact_ru(int(meta['lines_read']))}
    строк. Из {fmt_int(int(meta['usage_rows_raw']))} записей с данными об
    использовании <b>{fmt_int(int(meta['duplicates_dropped']))}
    ({dup_pct:.1f}&nbsp;%) — дубликаты</b>: один и тот же вызов переписывается
    в транскрипт при resume, rewind и в файлы сабагентов. Дедуп идёт по паре
    <code>message.id + requestId</code>, осталось
    {fmt_int(int(meta['rows_kept']))} уникальных вызовов. Ещё
    {esc(meta.get('partial_records_replaced', '0'))} записей оказались
    оборванными снимками стрима — вместо них взяты полные.</p>
  <p>Итоги сходятся с независимой утилитой <code>ccusage</code> до цента по
    input, output, записи и чтению кэша и по стоимости. Сверка выполняется на
    каждом прогоне <code>collect.py</code>.</p>

  <h3>Формат логов и что может сломаться</h3>
  <p>Формат транскриптов Claude Code недокументирован и меняется между версиями
    CLI — в этих данных их девять за одну неделю. Парсер написан защитно:
    отсутствующие поля не роняют прогон, а попадают в счётчики ниже. Если
    Anthropic поменяет структуру, эти счётчики станут ненулевыми — это и есть
    сигнал, что цифрам верить нельзя.</p>
  <div class="checks">{checks_html}</div>

  <h3>Приватность</h3>
  <p>В транскриптах лежат полные тексты диалогов и кода. В базу и в этот файл
    попадают только идентификаторы и числовые счётчики:
    <code>{esc(meta['extracted_fields'])}</code>. Поля с содержимым не читаются
    вовсе. Ни сборщик, ни страница не делают сетевых запросов — открывается
    офлайн двойным кликом.</p>

  <h3>Обновление</h3>
  <p>Данные в этом файле статичны на момент сборки. Чтобы пересобрать, запустите
    <code>python collect.py</code> в каталоге <code>ccdash</code> — кнопка
    «Обновить данные» наверху копирует эту команду в буфер обмена.</p>
</footer>

</div>
<div id="tip" role="tooltip"></div>
<script>window.__CCDASH__ = {payload};</script>
<script>{JS}</script>
</body>
</html>
"""


def render(db_path: Path, out_path: Path) -> Path:
    conn = sqlite3.connect(db_path)
    try:
        data = fetch(conn)
    finally:
        conn.close()
    out_path.write_text(build_html(data), encoding="utf-8")
    return out_path


def main() -> int:
    here = Path(__file__).resolve().parent
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "usage.db"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else here / "index.html"
    if not db.exists():
        print(f"Нет базы {db}. Сначала запустите collect.py", file=sys.stderr)
        return 1
    path = render(db, out)
    print(f"Дашборд собран: {path}  ({path.stat().st_size / 1024:.0f} КБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
