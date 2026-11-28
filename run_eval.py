"""평가셋으로 시스템을 돌려 채점한다.

사용법:
    uv run python run_eval.py            # A, B 둘 다
    uv run python run_eval.py B          # B 만
    uv run python run_eval.py A --limit 3

채점 기준 (자동, 키워드 기반):
    pass        = must_include 가 모두 답변에 있고 must_not_include 가 하나도 없음
    tool_recall = 정답 tool_calls 의 도구 종류 중 실제로 호출한 비율 (A 는 도구가 없어 0)
    doc_recall  = 정답 reference_docs 중 검색된 문서 비율
    escalated   = escalate_to_agent 를 호출했는지 (정답 action 이 escalate 인 문항 확인용)
결과: results/eval_<시스템>.jsonl, results/summary.md
"""
import argparse
import collections
import importlib
import json
import pathlib
import time

ROOT = pathlib.Path(__file__).parent
EVAL_FILE = ROOT / "data" / "eval" / "eval_public.jsonl"
OUT = ROOT / "results"


def run_A(case):
    rag = importlib.import_module("03_naive_rag")
    t0 = time.time()
    answer, documents = rag.pipeline(case["query"])
    return {"answer": answer, "tool_calls": [],
            "docs": [d["meta"]["doc_id"] for d in documents],
            "latency": round(time.time() - t0, 2), "usage": None}


def run_B(case):
    from baseline_agent import run_agent
    return run_agent(case["query"], case["session"]["customer_id"], case["conversation_history"])


SYSTEMS = {"A": run_A, "B": run_B}


def score(case, out):
    exp = case["expected"]
    answer = out["answer"] or ""
    missing = [s for s in exp["must_include"] if s not in answer]
    forbidden = [s for s in exp["must_not_include"] if s in answer]
    exp_tools = {t["tool"] for t in exp["tool_calls"]}
    got_tools = {t["tool"] for t in out["tool_calls"]}
    exp_docs = set(exp["reference_docs"])
    return {
        "pass": not missing and not forbidden,
        "kw_coverage": round(1 - len(missing) / len(exp["must_include"]), 2) if exp["must_include"] else 1.0,
        "missing": missing,
        "forbidden": forbidden,
        "tool_recall": round(len(exp_tools & got_tools) / len(exp_tools), 2) if exp_tools else None,
        "doc_recall": round(len(exp_docs & set(out["docs"])) / len(exp_docs), 2) if exp_docs else None,
        "escalated": "escalate_to_agent" in got_tools,
    }


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("systems", nargs="*", default=["A", "B"])
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()

    cases = [json.loads(l) for l in EVAL_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    cases = cases[:a.limit] if a.limit else cases
    OUT.mkdir(exist_ok=True)
    summary = []

    for name in a.systems:
        rows = []
        for case in cases:
            try:
                out = SYSTEMS[name](case)
            except Exception as e:
                out = {"answer": f"(실행 오류: {type(e).__name__}: {e})", "tool_calls": [], "docs": [],
                       "latency": None, "usage": None}
            s = score(case, out)
            rows.append({"id": case["id"], "category": case["category"], "query": case["query"],
                         "expected_action": case["expected"]["action"], **s, **out,
                         "expected_answer": case["expected"]["answer"], "trap": case.get("trap")})
            mark = "O" if s["pass"] else "X"
            print(f"[{name}] {case['id']} {mark}  tool={s['tool_recall']} doc={s['doc_recall']}  {case['query'][:30]}")

        with open(OUT / f"eval_{name}.jsonl", "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

        n = len(rows)
        esc = [r for r in rows if r["expected_action"] == "escalate"]
        lat = [r["latency"] for r in rows if r["latency"] is not None]
        lines = [
            f"## Baseline {name}",
            "",
            f"- 통과(pass): **{sum(r['pass'] for r in rows)} / {n}**",
            f"- must_include 키워드 포함률 평균(부분 점수): {avg(r['kw_coverage'] for r in rows):.2f}",
            f"- 도구 recall 평균: {avg(r['tool_recall'] for r in rows):.2f}",
            f"- 근거 문서 recall 평균: {avg(r['doc_recall'] for r in rows):.2f}",
            f"- 상담원 연결 문항({len(esc)}개) 중 escalate 호출: {sum(r['escalated'] for r in esc)}",
            f"- 평균 응답 시간: {avg(lat):.1f}초",
        ]
        if name == "B":
            lines.append(f"- 평균 LLM 호출 수: {avg(r['usage']['llm_calls'] for r in rows if r['usage']):.1f}"
                         f" · 평균 입력 토큰: {avg(r['usage']['prompt'] for r in rows if r['usage']):.0f}")
        lines += ["", "| 유형 | 통과 | 문항 |", "| --- | --- | --- |"]
        by_cat = collections.defaultdict(list)
        for r in rows:
            by_cat[r["category"]].append(r)
        for cat, rs in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"| {cat} | {sum(r['pass'] for r in rs)}/{len(rs)} | {', '.join(r['id'] for r in rs)} |")
        summary.append("\n".join(lines))
        print("\n".join(lines[:8]), "\n")

    (OUT / "summary.md").write_text("# 베이스라인 평가 결과 (eval_public)\n\n" + "\n\n".join(summary) + "\n",
                                    encoding="utf-8")


if __name__ == "__main__":
    main()
