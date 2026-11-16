#!/usr/bin/env python3
"""ClickHouse 녹화 표 목록을 원천의 system 표에서 만든다(v4 자동 발견, 2026-11-10 R1).

capture-eval-case.sh 가 원천 CH 의 system.tables, system.columns 와 구체화 뷰 관계를 떠서 넘긴다.
여기서는 표 데이터를 읽지 않는다. 결과는 표마다 내보낼 질의와 파일 자리를 담은 JSON 이다.

    ch-scope-plan.py --emit catalog-sql               # 캡처가 원천에 던질 질의 셋(탭 구분 출력)
    ch-scope-plan.py --catalog DIR --skip lucida.otel_traces_local ... \\
        --window-start 2026-11-10T00:00:00Z --window-end 2026-11-10T03:00:00Z   # 계획 JSON

--skip 은 v3 13개처럼 캡처가 따로 담는 표다. 그 표는 계획에 넣지 않는다.

표마다 정하는 것:
- 창 자르기(slice): 분할 키 안의 시각 열, 없으면 정렬 키 첫 열이 시각 열일 때 그 열로 자른다. 그 열이
  부분(part)과 그래뉼을 건너뛰게 하므로 날짜 분할 필터가 되고 표 전체를 훑지 않는다(128 전체 훑기
  사고, 2026-11-09). 시각 열이 그 자리에 있다는 것이 곧 시계열 표라는 표시이기도 하다.
- 겹쳐 쓰는 엔진(Replacing, Aggregating, Summing, Collapsing 계열)은 정렬 키가 같은 행을 하나로 합친다.
  분할 키의 시각 열이 정렬 키에 없으면 그 열은 사건 시각이 아니라 "마지막으로 쓴 시각"이다(사전 표:
  하루 한 행을 묶음마다 다시 쓰며 그 묶음의 마지막 시각을 적는다). 이런 표를 그 열로 자르면 창 안 사건이
  가리키는 사전 행이 창 밖 시각을 달고 있어 빠진다(v3 process_meta 를 snap 으로 둔 것과 같은 함정).
  그래서 정렬 키 안의 시각 열로 자르고(대개 날짜 열이라 창의 날짜 범위), 그 열이 분할 시각 열에서
  계산된 열(MATERIALIZED, DEFAULT 식이 toDate(분할 열))이면 같은 날짜 조건을 분할 열에도 걸어 부분을
  건너뛴다. 정렬 키에 시각 열이 없으면 통째로.
- 통째로(snap): 그 밖의 표는 기준 표나 상태 표다(집계 진행 표시, 텍스트 사전처럼 시각 열이 있어도
  사건 시각이 아니다). whole_max_bytes 까지만 통째로 담고, 넘으면 담지 않고 이유를 남긴다. 부분을
  건너뛸 수 없는 창 조건(정렬 키 시각 열이 분할과 무관)도 표 전체를 읽으므로 같은 상한을 건다.
- 형식: Parquet. AggregateFunction 열이 있으면 Parquet 으로 낼 수 없어(CH 26.3 실측 UNKNOWN_TYPE)
  Native 로 낸다. UUID 열은 v3 의 두 표와 같이 문자열로 바꾼다.
- 제외: 저장소가 없는 엔진(View, MaterializedView, Null 등), 설정의 exclude, 크기 상한.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "ch-scope.json"
TIME_TYPE = re.compile(r"^(?:Nullable\()?(DateTime64|DateTime|Date32|Date)\b")
# 정렬 키가 같은 행을 병합 때 하나로 합치는 엔진. 이런 표의 시각 열은 정렬 키에 있을 때만 사건 시각이다.
MERGING = re.compile(r"(Replacing|Aggregating|Summing|Collapsing|VersionedCollapsing|Graphite|Coalescing)MergeTree$")
# MergeTree 가 아니면서 데이터를 자기 저장소에 두는 엔진. 시각 조건으로 부분을 건너뛸 수 없어 통째로(상한 안).
LOCAL_STORAGE = {"Memory", "Log", "TinyLog", "StripeLog", "Join", "EmbeddedRocksDB"}
# 자기 저장소가 있어도 SELECT 로 읽을 수 없는 엔진. CH 26.3.17.56 실측: Set 은 "Method read is not supported by
# storage Set (NOT_IMPLEMENTED)". Join 은 ANY, ALL, SEMI, ANTI 와 LEFT, INNER 조합 모두 SELECT 로 읽혀 통째로 담는다.
NO_DIRECT_READ = {"Set": "SELECT 로 읽을 수 없는 엔진(Set). IN 조건용 집합이라 행으로 내보낼 수 없다"}


def catalog_queries(databases: list[str]) -> dict[str, str]:
    dbs = ", ".join("'" + d.replace("'", "") + "'" for d in databases)
    return {
        "tables": ("SELECT database, name, engine, partition_key, sorting_key, total_rows, total_bytes "
                   f"FROM system.tables WHERE database IN ({dbs}) AND NOT is_temporary ORDER BY database, name"),
        "columns": ("SELECT database, table, name, type, default_kind, default_expression FROM system.columns "
                    f"WHERE database IN ({dbs}) ORDER BY database, table, position"),
        "mviews": ("SELECT database, name, extract(create_table_query, ' TO ([^ (]+)'), "
                   "arrayStringConcat(arrayMap((d, t) -> concat(d, '.', t), dependencies_database, dependencies_table), ',') "
                   f"FROM system.tables WHERE database IN ({dbs}) ORDER BY database, name"),
    }


def load(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    if cfg.get("schema_version") != 1:
        sys.exit(f"ch-scope.json schema_version={cfg.get('schema_version')} 를 모른다")
    return cfg


def _rows(text: str, width: int) -> list[list[str]]:
    out = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= width:
            out.append(parts)
    return out


def _int(v: str) -> int | None:
    return int(v) if re.fullmatch(r"\d+", v or "") else None


def parse_catalog(texts: dict[str, str]) -> dict:
    tables: dict[str, dict] = {}
    for p in _rows(texts.get("tables", ""), 7):
        if p[0] == "database":
            continue  # 머리줄
        tables[f"{p[0]}.{p[1]}"] = {"database": p[0], "name": p[1], "engine": p[2],
                                     "partition_key": p[3], "sorting_key": p[4],
                                     "total_rows": _int(p[5]), "total_bytes": _int(p[6])}
    columns: dict[str, list[tuple[str, str]]] = {}
    derived: dict[str, dict[str, str]] = {}
    for p in _rows(texts.get("columns", ""), 4):
        key = f"{p[0]}.{p[1]}"
        if key in tables:
            columns.setdefault(key, []).append((p[2], p[3]))
            if len(p) > 5 and p[4] in ("MATERIALIZED", "DEFAULT", "ALIAS") and p[5]:
                derived.setdefault(key, {})[p[2]] = p[5]
    mv_target_of: dict[str, list[str]] = {}
    mv_source_of: dict[str, list[str]] = {}
    for p in _rows(texts.get("mviews", ""), 3):
        key = f"{p[0]}.{p[1]}"
        if key not in tables:
            continue
        target = p[2].replace("`", "")
        if target:
            if "." not in target:
                target = f"{p[0]}.{target}"
            mv_target_of.setdefault(target, []).append(key)
        deps = p[3] if len(p) > 3 else ""
        for d in filter(None, deps.split(",")):
            mv_source_of.setdefault(key, []).append(d)
    return {"tables": tables, "columns": columns, "derived": derived,
            "mv_target_of": mv_target_of, "mv_source_of": mv_source_of}


def load_catalog(d: Path) -> dict:
    return parse_catalog({k: (d / f"{k}.tsv").read_text() if (d / f"{k}.tsv").exists() else ""
                          for k in ("tables", "columns", "mviews")})


def _idents(expr: str) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr or "")


def _quote(name: str) -> str:
    return "`" + name.replace("`", "") + "`"


def plan_tables(cfg: dict, catalog: dict, skip: set[str], window_start: str, window_end: str) -> dict:
    exclude = cfg.get("exclude", {})
    prefixes = cfg.get("exclude_name_prefixes", {})
    whole_max = cfg.get("whole_max_bytes")
    ws = f"parseDateTime64BestEffort('{window_start}')"
    we = f"parseDateTime64BestEffort('{window_end}')"

    planned, excluded = [], []
    for key in sorted(catalog["tables"]):
        t = catalog["tables"][key]
        db, name = t["database"], t["name"]
        if key in skip:
            continue
        reason = None
        if key in exclude:
            reason = exclude[key]
        else:
            for pre, why in prefixes.items():
                if name.startswith(pre):
                    reason = why
        merge_tree = t["engine"].endswith("MergeTree")
        if reason is None and t["engine"] in NO_DIRECT_READ:
            reason = NO_DIRECT_READ[t["engine"]]
        if reason is None and not merge_tree and t["engine"] not in LOCAL_STORAGE:
            reason = f"자기 저장소가 없거나 바깥 원천을 읽는 엔진({t['engine']})"
        if reason:
            excluded.append({"database": db, "table": name, "engine": t["engine"], "reason": reason})
            continue

        cols = catalog["columns"].get(key, [])
        typed = {c: ty for c, ty in cols}
        times = [c for c, ty in cols if TIME_TYPE.match(ty)]
        part_ids = _idents(t["partition_key"]) if merge_tree else []
        sort_ids = _idents(t["sorting_key"]) if merge_tree else []
        part_time = next((c for c in part_ids if c in times), None)
        sort_time = next((c for c in sort_ids if c in times), None)
        merging = bool(MERGING.search(t["engine"]))
        derived = catalog.get("derived", {}).get(key, {})
        size = t["total_bytes"] or 0
        entry = {"database": db, "table": name, "engine": t["engine"],
                 "total_rows": t["total_rows"], "total_bytes": t["total_bytes"],
                 "mv_target_of": sorted(catalog["mv_target_of"].get(key, [])),
                 "mv_source_of": sorted(catalog["mv_source_of"].get(key, []))}

        # (시각 열, 근거, 부분을 건너뛰는 덧조건)
        time_col, basis, prune = None, None, ""
        if part_time and (not merging or part_time in sort_ids):
            time_col, basis = part_time, "partition_time"
        elif merging and part_time and sort_time:
            # 사전 표 모양: 분할 시각 열은 마지막으로 쓴 시각이다. 정렬 키 안의 시각 열로 자른다.
            time_col, basis = sort_time, "sorting_key_time"
            expr = re.sub(r"[\s`]", "", derived.get(sort_time, ""))
            if expr in (f"toDate({part_time})", f"toDate32({part_time})"):
                prune = (f" AND toDate({_quote(part_time)}) >= toDate({ws})"
                         f" AND toDate({_quote(part_time)}) <= toDate({we})")
        elif not part_time and sort_ids[:1] and sort_ids[0] in times:
            time_col, basis = sort_ids[0], "sorting_key_first"
        pruned = basis in ("partition_time", "sorting_key_first") or bool(prune)

        if time_col is None or not pruned:
            if whole_max is not None and size > whole_max:
                why = ("분할 키나 정렬 키 첫 열에 시각 열이 없어 기준 표로 보는데" if time_col is None
                       else f"시각 열 {time_col} 로는 부분을 건너뛸 수 없어 표 전체를 읽는데")
                excluded.append({**{k: entry[k] for k in ("database", "table", "engine")},
                                 "total_bytes": size, "reason": f"{why} 상한 {whole_max} 바이트를 넘는다"})
                continue
        if time_col is None:
            entry.update(scope="snap", time_column=None, time_type=None, slice_basis=None, where="")
        else:
            kind = TIME_TYPE.match(typed[time_col]).group(1)
            if kind.startswith("Date") and not kind.startswith("DateTime"):
                where = f" WHERE {_quote(time_col)} >= toDate({ws}) AND {_quote(time_col)} <= toDate({we})"
            else:
                where = f" WHERE {_quote(time_col)} >= {ws} AND {_quote(time_col)} <= {we}"
            entry.update(scope="slice", time_column=time_col, time_type=kind, slice_basis=basis,
                         where=where + prune)

        uuid_cols = [c for c, ty in cols if re.match(r"^(Nullable\()?UUID\b", ty)]
        agg = any("AggregateFunction(" in ty and not ty.startswith("SimpleAggregateFunction") for _, ty in cols)
        select = "*"
        if uuid_cols:
            select = "* REPLACE(" + ", ".join(f"toString({_quote(c)}) AS {_quote(c)}" for c in uuid_cols) + ")"
        fmt = "Native" if agg else "Parquet"
        ext = "native" if agg else "parquet"
        sub = "" if db == "lucida" else f"{db}/"
        entry.update(select=select, format=fmt, file=f"data/clickhouse/{sub}{name}.{ext}",
                     source=f"{_quote(db)}.{_quote(name)}")
        planned.append(entry)
    return {"tables": planned, "excluded": excluded, "max_execution_time": cfg.get("max_execution_time")}


# ---------------------------------------------------------------- 보존 기간 (2026-11-16 CAPFIX 2회차)
#
# 러너가 캡처만 다시 할 때(주입은 끝났고 캡처만 실패) 그 캡처가 담을 표의 보존 기간(TTL)을 원천에서 읽는다.
# 창 시작이 가장 짧은 TTL 에 여유를 뺀 값보다 오래됐으면 창 앞쪽 행이 이미 지워졌을 수 있어 재시도를 거절한다
# (캡처는 행 수와 최소, 최대 시각을 적기만 해서 빠진 행이 오류 없이 지나간다, CAPFIX 리뷰 상 1).
#
# TTL 식은 표마다 모양이 다르다(119, 2026-11-16 실측): 시각 열 + toIntervalDay(3), toStartOfMonth(열) + 일수,
# 행마다 다른 열(retention_days, retention_hours, multiIf(tier ...)), WHERE 가 붙은 여러 항. 식을 글자로 풀지 않고
# ClickHouse 가 계산하게 한다: 시각 열은 모두 창 시작, 다른 열은 열 기본값(식이 다른 열을 부르면 타입 기본값)인
# 한 행을 만들어 지우는 TTL 항마다 만료 시각을 구하고 창 시작과의 차이(초)를 TTL 로 본다. 창 시작의 행이 가장
# 먼저 지워지므로 이 값이 그 표에서 창이 버티는 시간이다. WHERE 가 붙은 항도 조건과 상관없이 센다(조건에 맞는
# 행만 지워져도 녹화가 달라진다). GROUP BY(롤업)도 행을 바꾸므로 센다. 지우지 않는 항(TO DISK, TO VOLUME, RECOMPRESS)은 뺀다.
# 표 데이터는 읽지 않는다(FROM 이 리터럴 한 행).
# 행을 지우지 않는 항. GROUP BY(롤업)는 행을 바꾸므로 지우는 항과 같이 센다(6회차, 재리뷰 하 4).
TTL_NOT_DELETE = re.compile(r"\b(TO\s+DISK|TO\s+VOLUME|RECOMPRESS)\b", re.I)


def ttl_query(databases: list[str]) -> str:
    dbs = ", ".join("'" + d.replace("'", "") + "'" for d in databases)
    return (f"SELECT database, name, engine_full FROM system.tables WHERE database IN ({dbs}) AND NOT is_temporary "
            "ORDER BY database, name")


def _tsv_unescape(v: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n", "r": "\r", "0": "\0"}.get(m.group(1), m.group(1)), v)


def _split_top(text: str, sep: str = ",") -> list[str]:
    """괄호와 작은따옴표 밖의 sep 로 나눈다."""
    out, depth, quote, cur, i = [], 0, False, [], 0
    while i < len(text):
        ch = text[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 1
            elif ch == "'":
                quote = False
        elif ch == "'":
            quote = True
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


def _top_keyword(text: str, word: str) -> int:
    """괄호와 따옴표 밖에서 처음 나오는 낱말 word(공백으로 둘러싸인)의 위치. 없으면 -1."""
    depth, quote = 0, False
    pat = re.compile(rf"\s{word}\s", re.I)
    for i, ch in enumerate(text):
        if quote:
            if ch == "'" and text[i - 1] != "\\":
                quote = False
            continue
        if ch == "'":
            quote = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and pat.match(text, i):
            return i
    return -1


def ttl_delete_terms(engine_full: str) -> list[str] | None:
    """engine_full 의 표 TTL 절에서 행을 지우는 항의 식. TTL 이 없으면 [], 절을 못 찾아 낼 모양이면 None."""
    text = engine_full or ""
    start = _top_keyword(text, "TTL")
    if start < 0:
        return []
    body = text[start:].strip()[3:]
    end = _top_keyword(body, "SETTINGS")
    if end >= 0:
        body = body[:end]
    terms = []
    for raw in _split_top(body):
        term = raw.strip()
        if not term:
            return None
        padded = " " + term + " "
        cuts = [c for c in (_top_keyword(padded, "WHERE"), _top_keyword(padded, "GROUP")) if c >= 0]
        expr = term if not cuts else padded[:min(cuts)].strip()
        if TTL_NOT_DELETE.search(expr):
            continue
        expr = re.sub(r"\s+DELETE\s*$", "", expr, flags=re.I).strip()
        if not expr:
            return None
        terms.append(expr)
    return terms


def retention_probe(catalog: dict, key: str, terms: list[str], window_start: str) -> str:
    """terms 의 만료 시각과 창 시작의 차(초) 가운데 가장 작은 값을 내는 질의. 표 데이터는 읽지 않는다."""
    cols = dict(catalog["columns"].get(key, []))
    derived = catalog.get("derived", {}).get(key, {})
    ws = f"parseDateTime64BestEffort('{window_start}', 3, 'UTC')"
    used = [c for c in cols if any(c in _idents(t) for t in terms)]
    values = []
    for c in used:
        ty = _tsv_unescape(cols[c])  # 열 타입도 TSV 로 와서 시간대 인자의 작은따옴표가 이스케이프돼 있다
        default = _tsv_unescape(derived.get(c, ""))
        if TIME_TYPE.match(ty):
            value = f"CAST({ws} AS {ty})"
        elif default and not (set(_idents(default)) & set(cols)):
            value = f"CAST(({default}) AS {ty})"
        else:
            value = f"defaultValueOfTypeName('{ty}')"
        values.append(f"{value} AS {_quote(c)}")
    diffs = ", ".join(f"dateDiff('second', {ws}, toDateTime64({t}, 3, 'UTC'))" for t in terms)
    row = ", ".join(values) if values else "1 AS _one"
    return f"SELECT arrayMin([{diffs}]) FROM (SELECT {row})"


def retention_check(cfg: dict, query, v3_keys: set[str], window_start: str, window_end: str) -> dict:
    """캡처가 담을 표(v3 고정 표와 발견 표)마다 TTL 을 원천에서 계산한다. query(sql) -> str(TSV) 는 원천 CH 질의.

    읽지 못한 표는 unreadable 에 이유와 함께 둔다. 부르는 쪽(러너)은 하나라도 있으면 닫힌 쪽(거절)으로 간다.
    """
    texts = {k: query(q + " FORMAT TSV") for k, q in catalog_queries(cfg["databases"]).items()}
    catalog = parse_catalog(texts)
    engines = {}
    for p in _rows(query(ttl_query(cfg["databases"]) + " FORMAT TSV"), 3):
        engines[f"{p[0]}.{p[1]}"] = _tsv_unescape(p[2])
    plan = plan_tables(cfg, catalog, v3_keys, window_start, window_end)
    keys = sorted(v3_keys | {f"{e['database']}.{e['table']}" for e in plan["tables"]})
    tables, unreadable = [], []
    for key in keys:
        if key not in catalog["tables"]:
            unreadable.append({"table": key, "error": "원천에 표가 없다"})
            continue
        terms = ttl_delete_terms(engines.get(key, ""))
        if terms is None:
            unreadable.append({"table": key, "error": "TTL 절을 나누지 못했다"})
            continue
        if not terms:
            tables.append({"table": key, "ttl_seconds": None})
            continue
        try:
            out = query(retention_probe(catalog, key, terms, window_start) + " FORMAT TSV").strip()
            seconds = int(out)
        except Exception as error:  # noqa: BLE001  표 하나를 못 읽어도 나머지는 계속 보고, 이 표는 읽지 못함으로
            unreadable.append({"table": key, "error": f"TTL 계산 실패: {str(error)[:300]}"})
            continue
        tables.append({"table": key, "ttl_seconds": seconds, "terms": len(terms)})
    bounded = [t for t in tables if t["ttl_seconds"] is not None]
    shortest = min(bounded, key=lambda t: (t["ttl_seconds"], t["table"])) if bounded else None
    return {"window_start": window_start, "window_end": window_end, "tables": tables, "unreadable": unreadable,
            "shortest": {"table": shortest["table"], "ttl_seconds": shortest["ttl_seconds"]} if shortest else None}


def v3_keys_from_capture_script(path: Path) -> set[str]:
    """capture-eval-case.sh 의 ch_v3_tables 목록(녹화 전 점검과 같은 읽기). 목록은 그 스크립트 한 곳에만 둔다."""
    block = re.search(r"ch_v3_tables=\((.*?)\n  \)", path.read_text(), re.S)
    if block is None:
        raise RuntimeError(f"ch_v3_tables 목록을 찾지 못했다: {path}")
    names = re.findall(r"'([a-z0-9_]+):", block.group(1))
    if len(names) < 12:
        raise RuntimeError(f"ch_v3_tables 목록이 짧다({len(names)}): {path}")
    return {f"lucida.{n}" for n in names}


def http_query(url: str, user: str, password: str, timeout: float = 60):
    import urllib.request

    def run(sql: str) -> str:
        req = urllib.request.Request(url.rstrip("/") + "/", data=sql.encode(), method="POST")
        if user:
            req.add_header("X-ClickHouse-User", user)
            req.add_header("X-ClickHouse-Key", password)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode()
        except urllib.error.HTTPError as error:
            raise RuntimeError(error.read().decode(errors="replace")[:300].strip()) from None
    return run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--emit", choices=["catalog-sql", "plan", "retention-check"], default="plan")
    ap.add_argument("--capture-script", type=Path, help="retention-check: v3 고정 표 목록을 읽을 capture-eval-case.sh")
    ap.add_argument("--catalog", type=Path, help="catalog-sql 결과 디렉터리(tables.tsv, columns.tsv, mviews.tsv)")
    ap.add_argument("--skip", action="append", default=[], help="db.table. 따로 담는 표(v3 13개)")
    ap.add_argument("--window-start")
    ap.add_argument("--window-end")
    a = ap.parse_args()
    cfg = load(a.config)
    if a.emit == "catalog-sql":
        print(json.dumps(catalog_queries(cfg["databases"]), ensure_ascii=False))
        return 0
    if a.emit == "retention-check":
        # 원천 CH 는 환경변수(CH_URL, CH_USER, CH_PASSWORD)로. 캡처 스크립트와 같은 이름, 같은 기본값.
        import os
        if not (a.capture_script and a.window_start and a.window_end):
            sys.exit("--capture-script, --window-start, --window-end 가 필요하다")
        query = http_query(os.environ.get("CH_URL", "http://192.168.230.119:18123"),
                           os.environ.get("CH_USER", "lucida"), os.environ.get("CH_PASSWORD", ""))
        print(json.dumps(retention_check(cfg, query, v3_keys_from_capture_script(a.capture_script),
                                         a.window_start, a.window_end), ensure_ascii=False, indent=1))
        return 0
    if not (a.catalog and a.window_start and a.window_end):
        sys.exit("--catalog, --window-start, --window-end 가 필요하다")
    print(json.dumps(plan_tables(cfg, load_catalog(a.catalog), set(a.skip), a.window_start, a.window_end),
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
