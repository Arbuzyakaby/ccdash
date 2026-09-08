#!/usr/bin/env python3
"""
ccdash / collect.py - сбор статистики расхода токенов Claude Code.

ПРИВАТНОСТЬ. Скрипт читает JSONL-файлы сессий, в которых лежит полный текст
диалогов и кода. Из каждой записи извлекаются ТОЛЬКО служебные идентификаторы и
числовые счётчики токенов (список - в EXTRACTED_FIELDS ниже). Поля с содержимым
(message.content, toolUseResult, lastPrompt, customTitle, aiTitle, summary,
attachment) не читаются вообще. Скрипт не делает сетевых запросов.

ХРУПКОСТЬ ФОРМАТА. Формат transcript-файлов Claude Code недокументирован и
меняется между версиями CLI (в текущих данных их уже девять за одну неделю).
Поэтому здесь всюду .get() с дефолтами, а любая неожиданная строка попадает в
счётчик предупреждений, а не роняет прогон. Раздел "САНИТАРНЫЕ ПРОВЕРКИ" в конце
вывода показывает, не разъехался ли формат.

Зависимости: только стандартная библиотека.
Запуск: python collect.py [--verbose] [--db PATH] [--projects DIR] [--no-crosscheck]

По умолчанию печатается короткая сводка: сколько прочитано, итоговая сумма,
сверка с ccusage и предупреждения (только если они есть). Подробный постатейный
разбор (по дням/проектам/моделям/сессиям) — под флагом --verbose.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

EXTRACTED_FIELDS = (
    "message.id, requestId, sessionId, timestamp, cwd, gitBranch, version, effort, "
    "isSidechain, message.model, message.usage.*"
)


# ---------------------------------------------------------------- аргументы

def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Сбор статистики токенов Claude Code")
    p.add_argument("--projects", type=Path,
                   default=Path.home() / ".claude" / "projects",
                   help="каталог с транскриптами сессий")
    p.add_argument("--db", type=Path, default=here / "usage.db",
                   help="файл SQLite для агрегатов")
    p.add_argument("--html", type=Path, default=here / "index.html",
                   help="куда положить дашборд")
    p.add_argument("--no-crosscheck", action="store_true",
                   help="не сверяться с ccusage")
    p.add_argument("--no-html", action="store_true",
                   help="только пересобрать базу, дашборд не трогать")
    p.add_argument("--verbose", action="store_true",
                   help="подробный постатейный отчёт вместо короткой сводки")
    return p.parse_args()


# ---------------------------------------------------------------- утилиты

def num(value) -> int:
    """Целое из чего угодно; всё непонятное - ноль."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def dig(obj, *path, default=None):
    """Безопасный проход по вложенным словарям."""
    for key in path:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key)
    return obj if obj is not None else default


def normalize_cwd(cwd: str) -> str:
    """E:\\Foo и e:\\Foo - один и тот же проект. Буква диска -> верхний регистр."""
    cwd = (cwd or "").replace("/", "\\").rstrip("\\")
    if len(cwd) > 1 and cwd[1] == ":":
        cwd = cwd[0].upper() + cwd[1:]
    return cwd or "(unknown)"


def project_dir_of(path: Path, root: Path) -> str:
    """Папка проекта, которую завёл сам Claude Code (первый уровень под projects/).

    Она стабильна: транскрипты сессии и её сабагентов лежат внутри неё, даже если
    внутри сессии рабочий каталог менялся на подпапку.
    """
    try:
        return path.relative_to(root).parts[0]
    except (ValueError, IndexError):
        return path.parent.name


def label_projects(rows: list) -> None:
    """Имя проекта - по самому короткому cwd, встреченному в его папке.

    Иначе подкаталоги (RusSubjectMap\geo_raw, ...) разъезжаются в отдельные
    "проекты" и разрезы по проектам врут.
    """
    shortest: dict = {}
    for row in rows:
        key = row["project_dir"]
        cwd = row["_cwd"]
        if cwd == "(unknown)":
            continue
        current = shortest.get(key)
        if current is None or len(cwd) < len(current):
            shortest[key] = cwd
    for row in rows:
        path = shortest.get(row["project_dir"], row["_cwd"])
        row["project_path"] = path
        row["project_name"] = os.path.basename(path) or path
        del row["_cwd"]


# ------------------------------------------------- разбор одной записи

class Stats:
    """Счётчики того, что пошло не так. Печатаются в конце."""

    def __init__(self) -> None:
        self.files = 0
        self.lines = 0
        self.bad_json = 0
        self.usage_rows = 0
        self.duplicates = 0
        self.kept = 0
        self.no_dedup_key = 0
        self.partial_replaced = 0
        self.no_timestamp = 0
        self.no_model = 0
        self.no_cwd = 0
        self.unknown_models: Counter = Counter()
        self.unknown_usage_keys: Counter = Counter()
        self.read_errors: list[str] = []


KNOWN_USAGE_KEYS = {
    "input_tokens", "output_tokens", "cache_creation_input_tokens",
    "cache_read_input_tokens", "cache_creation", "output_tokens_details",
    "server_tool_use", "service_tier", "inference_geo", "iterations", "speed",
}


def iter_transcripts(root: Path):
    """Все .jsonl рекурсивно, включая <session>/subagents/agent-*.jsonl."""
    if not root.is_dir():
        raise SystemExit(f"Каталог с транскриптами не найден: {root}")
    yield from sorted(root.rglob("*.jsonl"))


def extract(record: dict, path: Path, projects_root: Path, stats: Stats):
    """Из записи транскрипта - одна строка метрик, либо None."""
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None

    stats.usage_rows += 1
    for key in usage:
        if key not in KNOWN_USAGE_KEYS:
            stats.unknown_usage_keys[key] += 1

    msg_id = message.get("id")
    req_id = record.get("requestId")
    if not msg_id or not req_id:
        stats.no_dedup_key += 1

    ts_raw = record.get("timestamp")
    if not ts_raw:
        stats.no_timestamp += 1
        return None
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        stats.no_timestamp += 1
        return None
    local = ts.astimezone()

    model = message.get("model") or "(unknown)"
    if model == "(unknown)":
        stats.no_model += 1

    cwd = record.get("cwd")
    if not cwd:
        stats.no_cwd += 1

    creation = usage.get("cache_creation")
    creation = creation if isinstance(creation, dict) else {}
    server_tools = usage.get("server_tool_use")
    server_tools = server_tools if isinstance(server_tools, dict) else {}

    cw_total = num(usage.get("cache_creation_input_tokens"))
    cw_1h = num(creation.get("ephemeral_1h_input_tokens"))
    cw_5m = num(creation.get("ephemeral_5m_input_tokens"))
    # Если разбивки нет (старая версия формата) - считаем весь кэш часовым:
    # именно так он тарифицируется в наблюдаемых данных.
    if cw_1h + cw_5m == 0 and cw_total:
        cw_1h = cw_total

    return {
        "msg_id": msg_id or f"{path.stem}:{record.get('uuid')}",
        "request_id": req_id or "",
        "ts_utc": ts.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "date_utc": ts.astimezone(timezone.utc).strftime("%Y-%m-%d"),
        "date_local": local.strftime("%Y-%m-%d"),
        "hour_local": local.hour,
        "weekday_local": local.weekday(),
        "_cwd": normalize_cwd(cwd or ""),
        "project_dir": project_dir_of(path, projects_root),
        "project_path": "",   # заполняется в label_projects()
        "project_name": "",
        "session_id": record.get("sessionId") or "(unknown)",
        "is_subagent": int("subagents" in path.parts),
        "model": model,
        "effort": record.get("effort") or "(none)",
        "cli_version": record.get("version") or "(unknown)",
        "git_branch": record.get("gitBranch") or "",
        "is_sidechain": int(bool(record.get("isSidechain"))),
        "input_tokens": num(usage.get("input_tokens")),
        "output_tokens": num(usage.get("output_tokens")),
        "thinking_tokens": num(dig(usage, "output_tokens_details", "thinking_tokens")),
        "cache_read": num(usage.get("cache_read_input_tokens")),
        "cache_write": cw_total,
        "cache_write_1h": cw_1h,
        "cache_write_5m": cw_5m,
        "web_search": num(server_tools.get("web_search_requests")),
        "web_fetch": num(server_tools.get("web_fetch_requests")),
    }


def collect(root: Path, stats: Stats) -> list:
    # Один и тот же вызов пишется в транскрипт по нескольку раз (resume, rewind,
    # копии в файлах сабагентов). Дедуп - по (message.id, requestId).
    #
    # Изредка копии одного ключа расходятся по output_tokens: сначала попадает
    # оборванный снимок стрима (2-8 токенов), затем финальная запись. Поэтому
    # оставляем не первую копию, а самую полную - иначе итог по output занижается
    # (на текущих данных - на 39 624 токена).
    best: dict = {}
    for path in iter_transcripts(root):
        stats.files += 1
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError as exc:
            stats.read_errors.append(f"{path.name}: {exc}")
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                stats.lines += 1
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    stats.bad_json += 1
                    continue
                if not isinstance(record, dict):
                    stats.bad_json += 1
                    continue
                try:
                    row = extract(record, path, root, stats)
                except Exception as exc:                      # noqa: BLE001
                    stats.read_errors.append(
                        f"{path.name}: {type(exc).__name__}: {exc}")
                    continue
                if row is None:
                    continue
                key = (row["msg_id"], row["request_id"])
                previous = best.get(key)
                if previous is None:
                    best[key] = row
                    continue
                stats.duplicates += 1
                if row["output_tokens"] > previous["output_tokens"]:
                    stats.partial_replaced += 1
                    best[key] = row
    rows = list(best.values())
    label_projects(rows)
    stats.kept = len(rows)
    return rows


# ---------------------------------------------------------------- деньги

def price_rows(rows: list, pricing: dict, stats: Stats) -> None:
    models = pricing.get("models", {})
    for row in rows:
        rate = models.get(row["model"])
        if rate is None:
            stats.unknown_models[row["model"]] += 1
            row["cost_1h"] = 0.0
            row["cost_5m"] = 0.0
            row["priced"] = 0
            continue
        base = (row["input_tokens"] * rate["input"]
                + row["output_tokens"] * rate["output"]
                + row["cache_read"] * rate["cache_read"]) / 1e6
        row["cost_1h"] = base + (row["cache_write_1h"] * rate["cache_write_1h"]
                                 + row["cache_write_5m"] * rate["cache_write_5m"]) / 1e6
        row["cost_5m"] = base + ((row["cache_write_1h"] + row["cache_write_5m"])
                                 * rate["cache_write_5m"]) / 1e6
        row["priced"] = 1


# ---------------------------------------------------------------- база

SCHEMA = """
DROP VIEW IF EXISTS v_daily;
DROP VIEW IF EXISTS v_project;
DROP VIEW IF EXISTS v_session;
DROP TABLE IF EXISTS calls;
CREATE TABLE calls (
    msg_id          TEXT NOT NULL,
    request_id      TEXT NOT NULL,
    ts_utc          TEXT NOT NULL,
    date_utc        TEXT NOT NULL,
    date_local      TEXT NOT NULL,
    hour_local      INTEGER NOT NULL,
    weekday_local   INTEGER NOT NULL,
    project_dir     TEXT NOT NULL,
    project_path    TEXT NOT NULL,
    project_name    TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    is_subagent     INTEGER NOT NULL,
    is_sidechain    INTEGER NOT NULL,
    model           TEXT NOT NULL,
    effort          TEXT NOT NULL,
    cli_version     TEXT NOT NULL,
    git_branch      TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    thinking_tokens INTEGER NOT NULL,
    cache_read      INTEGER NOT NULL,
    cache_write     INTEGER NOT NULL,
    cache_write_1h  INTEGER NOT NULL,
    cache_write_5m  INTEGER NOT NULL,
    web_search      INTEGER NOT NULL,
    web_fetch       INTEGER NOT NULL,
    cost_1h         REAL NOT NULL,
    cost_5m         REAL NOT NULL,
    priced          INTEGER NOT NULL,
    PRIMARY KEY (msg_id, request_id)
);
CREATE INDEX idx_calls_date    ON calls(date_local);
CREATE INDEX idx_calls_project ON calls(project_path);
CREATE INDEX idx_calls_session ON calls(session_id);

DROP TABLE IF EXISTS meta;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE VIEW v_daily AS
SELECT date_local AS date, model,
       COUNT(*) AS calls,
       SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
       SUM(thinking_tokens) AS thinking_tokens,
       SUM(cache_read) AS cache_read, SUM(cache_write) AS cache_write,
       SUM(cost_1h) AS cost_1h, SUM(cost_5m) AS cost_5m
FROM calls GROUP BY date_local, model;

CREATE VIEW v_project AS
SELECT project_path, project_name,
       COUNT(*) AS calls, COUNT(DISTINCT session_id) AS sessions,
       MIN(date_local) AS first_day, MAX(date_local) AS last_day,
       SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
       SUM(thinking_tokens) AS thinking_tokens,
       SUM(cache_read) AS cache_read, SUM(cache_write) AS cache_write,
       SUM(cost_1h) AS cost_1h, SUM(cost_5m) AS cost_5m
FROM calls GROUP BY project_path;

CREATE VIEW v_session AS
SELECT session_id, project_name, project_path,
       COUNT(*) AS calls, MIN(ts_utc) AS started, MAX(ts_utc) AS ended,
       GROUP_CONCAT(DISTINCT model) AS models,
       SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
       SUM(thinking_tokens) AS thinking_tokens,
       SUM(cache_read) AS cache_read, SUM(cache_write) AS cache_write,
       SUM(cost_1h) AS cost_1h, SUM(cost_5m) AS cost_5m
FROM calls GROUP BY session_id;
"""

COLUMNS = [
    "msg_id", "request_id", "ts_utc", "date_utc", "date_local", "hour_local",
    "weekday_local", "project_dir", "project_path", "project_name", "session_id", "is_subagent",
    "is_sidechain", "model", "effort", "cli_version", "git_branch",
    "input_tokens", "output_tokens", "thinking_tokens", "cache_read",
    "cache_write", "cache_write_1h", "cache_write_5m", "web_search", "web_fetch",
    "cost_1h", "cost_5m", "priced",
]


def write_db(db_path: Path, rows: list, stats: Stats, args) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    placeholders = ",".join("?" * len(COLUMNS))
    conn.executemany(
        f"INSERT OR IGNORE INTO calls ({','.join(COLUMNS)}) VALUES ({placeholders})",
        [tuple(r[c] for c in COLUMNS) for r in rows],
    )
    meta = {
        "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_dir": str(args.projects),
        "files_scanned": stats.files,
        "lines_read": stats.lines,
        "usage_rows_raw": stats.usage_rows,
        "duplicates_dropped": stats.duplicates,
        "partial_records_replaced": stats.partial_replaced,
        "rows_kept": stats.kept,
        "bad_json_lines": stats.bad_json,
        "rows_no_timestamp": stats.no_timestamp,
        "rows_no_model": stats.no_model,
        "rows_no_cwd": stats.no_cwd,
        "rows_no_dedup_key": stats.no_dedup_key,
        "unknown_models": json.dumps(dict(stats.unknown_models), ensure_ascii=False),
        "unknown_usage_keys": json.dumps(dict(stats.unknown_usage_keys),
                                         ensure_ascii=False),
        "read_errors": json.dumps(stats.read_errors[:20], ensure_ascii=False),
        "extracted_fields": EXTRACTED_FIELDS,
        "local_timezone": str(datetime.now().astimezone().tzinfo),
    }
    conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                     [(k, str(v)) for k, v in meta.items()])
    conn.commit()
    return conn


# ---------------------------------------------------------------- сверка

def crosscheck(conn: sqlite3.Connection) -> None:
    """Сверка итогов с ccusage, если он установлен. Только чтение, офлайн."""
    print("\n--- СВЕРКА С ccusage ---")
    try:
        out = subprocess.run(
            ["ccusage", "claude", "daily", "--json", "--offline"],
            capture_output=True, text=True, timeout=300, check=False,
            shell=(os.name == "nt"), encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"  ccusage недоступен ({type(exc).__name__}) - сверка пропущена.")
        return
    if out.returncode != 0 or not (out.stdout or "").strip():
        print(f"  ccusage вернул код {out.returncode} - сверка пропущена.")
        return
    try:
        days = json.loads(out.stdout)["daily"]
    except (json.JSONDecodeError, KeyError, TypeError):
        print("  Не удалось разобрать вывод ccusage - сверка пропущена.")
        return

    theirs = Counter()
    for day in days:
        theirs["input"] += day.get("inputTokens", 0)
        theirs["output"] += day.get("outputTokens", 0)
        theirs["cache_write"] += day.get("cacheCreationTokens", 0)
        theirs["cache_read"] += day.get("cacheReadTokens", 0)
        theirs["cost"] += day.get("totalCost", 0.0)

    mine = conn.execute(
        "SELECT SUM(input_tokens), SUM(output_tokens), SUM(cache_write),"
        "       SUM(cache_read), SUM(cost_1h) FROM calls"
    ).fetchone()
    labels = ["input", "output", "cache_write", "cache_read", "cost"]
    all_ok = True
    for label, value in zip(labels, mine):
        them = theirs[label]
        value = value or 0
        delta = value - them
        rel = abs(delta) / them * 100 if them else 0.0
        ok = rel < 0.01
        all_ok = all_ok and ok
        fmt = "{:>16,.2f}" if label == "cost" else "{:>16,.0f}"
        print(f"  {'OK ' if ok else '!! '}{label:<12} мой {fmt.format(value)}"
              f"   ccusage {fmt.format(them)}   Д {fmt.format(delta)} ({rel:.4f}%)")
    print("  Итог: цифры сходятся." if all_ok
          else "  Итог: есть расхождение - см. строки с !! выше.")


# ---------------------------------------------------------------- вывод

def report_short(conn: sqlite3.Connection, stats: Stats) -> None:
    """Сводка в несколько строк: что нужно увидеть на обычном прогоне.
    Постатейный разбор по дням/проектам/моделям/сессиям — под --verbose."""
    q = conn.execute
    total = q("SELECT COUNT(*), COUNT(DISTINCT project_path), COUNT(DISTINCT session_id),"
              " MIN(date_local), MAX(date_local), SUM(cost_1h) FROM calls").fetchone()
    calls, projects, sessions, d0, d1, cost = total
    top = q("SELECT project_name, cost_1h FROM v_project"
            " ORDER BY cost_1h DESC LIMIT 1").fetchone()

    print("\n--- СВОДКА ---")
    print(f"  прочитано  : {stats.files} файлов, {stats.lines:,} строк, "
          f"{stats.kept:,} уникальных вызовов "
          f"({stats.duplicates:,} дублей отброшено, "
          f"{stats.duplicates / max(stats.usage_rows, 1) * 100:.1f}%)")
    print(f"  период     : {d0} … {d1}   {projects} проектов   {sessions} сессий   "
          f"{calls:,} вызовов")
    print(f"  стоимость  : ${cost:,.2f}"
          + (f"   больше всего — {top[0]} (${top[1]:,.2f})" if top else ""))

    problems = [
        ("битых JSON-строк", stats.bad_json),
        ("записей без timestamp", stats.no_timestamp),
        ("записей без модели", stats.no_model),
        ("записей без cwd", stats.no_cwd),
        ("записей без ключа дедупа", stats.no_dedup_key),
        ("ошибок чтения файлов", len(stats.read_errors)),
    ]
    bad = [(label, count) for label, count in problems if count]
    if stats.unknown_models:
        bad.append(("моделей без цены в pricing.json", len(stats.unknown_models)))
    if stats.unknown_usage_keys:
        bad.append(("новых полей в message.usage", len(stats.unknown_usage_keys)))
    if bad:
        print("  !! формату не доверять, см. ниже:")
        for label, count in bad:
            print(f"     !! {label}: {count}")
        if stats.unknown_models:
            print(f"        модели без цены: {dict(stats.unknown_models)}")
        if stats.unknown_usage_keys:
            print(f"        новые поля usage: {dict(stats.unknown_usage_keys)}")
    else:
        print("  OK формат в порядке (санитарные проверки чистые)")
    print("  Подробный разбор по дням/проектам/моделям/сессиям: --verbose")


def report(conn: sqlite3.Connection, stats: Stats) -> None:
    q = conn.execute
    print("\n--- ЧТО ПРОЧИТАНО ---")
    print(f"  файлов транскриптов : {stats.files}")
    print(f"  строк разобрано     : {stats.lines:,}")
    print(f"  записей с usage     : {stats.usage_rows:,}")
    print(f"  дубликатов отброшено: {stats.duplicates:,} "
          f"({stats.duplicates / max(stats.usage_rows, 1) * 100:.1f}%)")
    print(f"  уникальных вызовов  : {stats.kept:,}")
    if stats.partial_replaced:
        print(f"  оборванных снимков заменено на полные: {stats.partial_replaced}")

    total = q("SELECT COUNT(*), COUNT(DISTINCT project_path), COUNT(DISTINCT session_id),"
              " MIN(date_local), MAX(date_local), SUM(input_tokens), SUM(output_tokens),"
              " SUM(thinking_tokens), SUM(cache_read), SUM(cache_write),"
              " SUM(cost_1h), SUM(cost_5m) FROM calls").fetchone()
    calls, projects, sessions, d0, d1, tin, tout, tthink, tcr, tcw, c1h, c5m = total
    grand = tin + tout + tcr + tcw
    print("\n--- ИТОГИ ЗА ВСЮ ИСТОРИЮ ---")
    print(f"  период        : {d0} ... {d1}   проектов: {projects}   сессий: {sessions}")
    print(f"  вызовов к API : {calls:,}")
    print(f"  input         : {tin:>14,}  ({tin / grand * 100:5.2f}%)")
    print(f"  output        : {tout:>14,}  ({tout / grand * 100:5.2f}%)"
          f"   из них thinking: {tthink:,}")
    print(f"  cache write   : {tcw:>14,}  ({tcw / grand * 100:5.2f}%)")
    print(f"  cache read    : {tcr:>14,}  ({tcr / grand * 100:5.2f}%)")
    print(f"  ВСЕГО токенов : {grand:>14,}")
    print(f"  стоимость по тарифам API: ${c1h:,.2f}   "
          f"(при 5-минутном кэше было бы ${c5m:,.2f}, "
          f"разница ${c1h - c5m:,.2f})")
    print("  Это условная оценка 'сколько стоило бы по API'. У вас подписка -")
    print("  реальный платёж от этих цифр не зависит.")

    print("\n--- ПО ДНЯМ ---")
    print(f"  {'дата':<12}{'вызовов':>9}{'input':>10}{'output':>11}"
          f"{'cache W':>12}{'cache R':>14}{'$':>9}")
    for r in q("SELECT date_local, COUNT(*), SUM(input_tokens), SUM(output_tokens),"
               " SUM(cache_write), SUM(cache_read), SUM(cost_1h)"
               " FROM calls GROUP BY date_local ORDER BY date_local"):
        print(f"  {r[0]:<12}{r[1]:>9,}{r[2]:>10,}{r[3]:>11,}"
              f"{r[4]:>12,}{r[5]:>14,}{r[6]:>9,.2f}")

    print("\n--- ПО ПРОЕКТАМ ---")
    print(f"  {'проект':<34}{'сессий':>7}{'вызовов':>9}{'output':>10}"
          f"{'cache R':>14}{'$':>9}{'доля':>7}")
    for r in q("SELECT project_name, sessions, calls, output_tokens, cache_read, cost_1h"
               " FROM v_project ORDER BY cost_1h DESC"):
        name = r[0] if len(r[0]) <= 33 else r[0][:32] + "~"
        print(f"  {name:<34}{r[1]:>7,}{r[2]:>9,}{r[3]:>10,}{r[4]:>14,}"
              f"{r[5]:>9,.2f}{r[5] / c1h * 100:>6.1f}%")

    print("\n--- ПО МОДЕЛЯМ ---")
    for r in q("SELECT model, COUNT(*), SUM(output_tokens), SUM(cache_read), SUM(cost_1h)"
               " FROM calls GROUP BY model ORDER BY 5 DESC"):
        print(f"  {r[0]:<24}{r[1]:>8,} выз.  out {r[2]:>10,}  "
              f"cacheR {r[3]:>13,}  ${r[4]:>9,.2f}  {r[4] / c1h * 100:5.1f}%")

    print("\n--- ТОП-10 САМЫХ ДОРОГИХ СЕССИЙ ---")
    rows = q("SELECT session_id, project_name, calls, started, ended,"
             " models, output_tokens, cache_read, cost_1h"
             " FROM v_session ORDER BY cost_1h DESC LIMIT 10").fetchall()
    for i, r in enumerate(rows, 1):
        span = f"{r[3][:16].replace('T', ' ')} -> {r[4][11:16]}"
        print(f"  {i:>2}. ${r[8]:>8,.2f}  {r[1][:22]:<22} {r[2]:>4} выз.  {span}  {r[5]}")
        print(f"      {r[0]}   out {r[6]:,}   cacheR {r[7]:,}")

    print("\n--- ПО EFFORT ---")
    for r in q("SELECT effort, COUNT(*), SUM(output_tokens), SUM(thinking_tokens),"
               " SUM(cost_1h) FROM calls GROUP BY effort ORDER BY 5 DESC"):
        print(f"  {r[0]:<10}{r[1]:>7,} выз.  out {r[2]:>10,}  "
              f"thinking {r[3]:>9,}  ${r[4]:>9,.2f}")

    print("\n--- САНИТАРНЫЕ ПРОВЕРКИ ФОРМАТА ---")
    problems = [
        ("битых JSON-строк", stats.bad_json),
        ("записей без timestamp", stats.no_timestamp),
        ("записей без модели", stats.no_model),
        ("записей без cwd", stats.no_cwd),
        ("записей без ключа дедупа", stats.no_dedup_key),
        ("ошибок чтения файлов", len(stats.read_errors)),
    ]
    for label, count in problems:
        print(f"  {'!! ' if count else 'OK '}{label:<30}{count}")
    if stats.unknown_models:
        print(f"  !! модели без цены в pricing.json: {dict(stats.unknown_models)}")
        print("     (их вызовы посчитаны в токенах, но со стоимостью 0)")
    else:
        print("  OK все модели есть в pricing.json")
    if stats.unknown_usage_keys:
        print(f"  !! новые поля в message.usage: {dict(stats.unknown_usage_keys)}")
        print("     Формат изменился - стоит расширить парсер.")
    else:
        print("  OK новых полей в message.usage не появилось")
    for err in stats.read_errors[:5]:
        print(f"     {err}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    pricing_path = Path(__file__).resolve().parent / "pricing.json"
    try:
        pricing = json.loads(pricing_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Не удалось прочитать {pricing_path}: {exc}", file=sys.stderr)
        return 1

    stats = Stats()
    print(f"Читаю транскрипты из {args.projects} ...")
    rows = collect(args.projects, stats)
    if not rows:
        print("Не найдено ни одной записи с данными об использовании токенов.",
              file=sys.stderr)
        return 1
    price_rows(rows, pricing, stats)
    conn = write_db(args.db, rows, stats, args)
    (report if args.verbose else report_short)(conn, stats)
    if not args.no_crosscheck:
        crosscheck(conn)
    conn.close()
    print(f"\nАгрегаты записаны в {args.db}")
    print(f"В базу попали только идентификаторы и счётчики токенов: {EXTRACTED_FIELDS}")
    if not args.no_html:
        try:
            import render
            path = render.render(args.db, args.html)
            print(f"Дашборд пересобран: {path} "
                  f"({path.stat().st_size / 1024:.0f} КБ)")
        except Exception as exc:                                  # noqa: BLE001
            print(f"\nДашборд собрать не удалось ({type(exc).__name__}: {exc}).",
                  file=sys.stderr)
            print("База при этом готова — запустите render.py отдельно.",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
