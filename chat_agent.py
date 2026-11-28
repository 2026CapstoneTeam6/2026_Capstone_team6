"""직접 입력 + 주문 조회(02) + 정책 검색(03) 상담 에이전트"""
import importlib
import json
from llm_client import client, MODEL

# 파일 이름이 숫자로 시작해서 import 문 대신 이 방법을 사용
rag = importlib.import_module("03_naive_rag")

ORDERS = {o["order_id"]: o for o in json.load(open("data/system/orders.json", encoding="utf-8"))}
SESSION_CUSTOMER = "C0017"  # 로그인한 고객 (세션에서 온 값 — 모델이 정하는 값이 아님)


def get_order(order_id: str) -> dict:
    order = ORDERS.get(order_id)
    if order is None:
        return {"error": "NOT_FOUND"}
    if order["customer_id"] != SESSION_CUSTOMER:  # 소유자 검증은 코드에서
        return {"error": "FORBIDDEN"}
    claim = order.get("claim") or {}
    return {
        "order_id": order_id,
        "status": order["status"],
        "items": order["items"],
        "paid_amount": order["amounts"]["paid_amount"],
        "points_used": order["amounts"]["points_used"],
        "refund_status": claim.get("status"),
        "refund": claim.get("refund"),
    }


def search_policy(question: str) -> dict:
    documents = rag.search_policy(question)  # 03 의 검색을 그대로 사용
    for d in documents:
        print(f"[검색] {d['meta']['doc_id']} · {d['meta']['status']} · {d['meta']['title']}")
    return {"documents": [d["text"] for d in documents]}  # 03 처럼 본문만 모델에게 전달


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "주문번호로 주문 정보를 조회",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "예: ORD-20260901-0001"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_policy",
            "description": "배송, 환불, 교환, 쿠폰, 약관 등 정책 문서를 검색",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string", "description": "검색할 질문"}},
                "required": ["question"],
            },
        },
    },
]
FUNCS = {"get_order": get_order, "search_policy": search_policy}

messages = [{
    "role": "system",
    "content": "너는 이커머스 고객 상담원이야. 한국어로 짧게 답해. "
               "주문 정보는 반드시 get_order 로 조회하고, 정책 질문은 반드시 search_policy 로 검색해.",
}]

print("질문을 입력하세요. 끝내려면 exit")

while True:
    question = input("\n나: ").strip()
    if question.lower() == "exit":
        break
    if not question:
        continue
    messages.append({"role": "user", "content": question})

    for turn in range(5):  # 무한 반복 방지
        resp = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS, temperature=0)
        msg = resp.choices[0].message
        messages.append(msg)  # 모델의 답 또는 도구 요청을 대화에 추가
        if not msg.tool_calls:  # 도구 요청이 없으면 최종 답변
            print("AI:", msg.content)
            break
        for call in msg.tool_calls:
            args = json.loads(call.function.arguments)
            print(f"[호출] {call.function.name} {args}")
            result = FUNCS[call.function.name](**args)
            messages.append({  # 실행 결과를 role=tool 로 돌려줌
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
    else:
        print("AI: 도구 호출이 5번을 넘어 답변을 만들지 못했습니다.")