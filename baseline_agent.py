"""베이스라인 B. Naive Agent — LLM 이 필요한 도구와 실행 순서를 스스로 정한다 (02_tool_calling.py 확장판).

에이전트가 일할 수 있도록 최소한의 세션 정보(로그인 고객 ID, 현재 시각)만 시스템 프롬프트에 넣었다.
검증·의도 분석·정책 필터 같은 장치는 일부러 넣지 않았다.
"""
import json
import time

from baseline_tools import FUNCS, TOOLS, ToolContext
from llm_client import MODEL, client

NOW = "2026-11-28(토) 13:00"
MAX_TURNS = 8


def run_agent(question, customer_id, history=()):
    ctx = ToolContext(customer_id)
    messages = [
        {"role": "system", "content": (
            "너는 이커머스 A사 고객 상담원이야. 필요한 정보는 도구로 조회해서 한국어로 답해.\n"
            f"로그인한 고객 ID: {customer_id}\n현재 시각: {NOW}")},
        *history,
        {"role": "user", "content": question},
    ]
    calls, usage = [], {"prompt": 0, "completion": 0, "llm_calls": 0}
    t0 = time.time()
    answer = None
    for _ in range(MAX_TURNS):
        resp = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS, temperature=0)
        usage["prompt"] += resp.usage.prompt_tokens
        usage["completion"] += resp.usage.completion_tokens
        usage["llm_calls"] += 1
        msg = resp.choices[0].message
        if not msg.tool_calls:
            answer = msg.content
            break
        messages.append(msg)
        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
                result = FUNCS[call.function.name](ctx, **args)
            except Exception as e:  # 잘못된 도구 이름·인자도 그대로 모델에게 돌려줌
                args, result = call.function.arguments, {"error": f"{type(e).__name__}: {e}"}
            calls.append({"tool": call.function.name, "args": args})
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result, ensure_ascii=False, default=str)})
    return {
        "answer": answer if answer is not None else "(최대 턴 초과 — 답변 없음)",
        "tool_calls": calls,
        "docs": ctx.retrieved_docs,
        "latency": round(time.time() - t0, 2),
        "usage": usage,
    }


if __name__ == "__main__":
    out = run_agent("어머니(강준서) 주문도 제가 대신 관리하거든요. 배송 상태 알려주세요.", "C0001")
    for c in out["tool_calls"]:
        print("[호출]", c["tool"], c["args"])
    print("[답변]", out["answer"])
