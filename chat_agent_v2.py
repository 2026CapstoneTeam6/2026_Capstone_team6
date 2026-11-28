"""직접 입력 + 주문 조회(02) + 정책 검색(03) 상담 에이전트 — v2

[버전 기록]
v1: 02 + 03 합친 기본 에이전트
    - reset 입력 시 대화 기록 초기화
    - get_order 가 결제 금액·사용 포인트·환불 상태·환불 금액도 반환 (데이터 정합성)
v2: v1 + 고객 안내에 쓸 수 있는 정책 문서만 검색 (data/README.md 기준)
    - status 가 approved 인 문서만 사용 (draft·deprecated·rejected·expired·unverified 제외 → REF-020 등)
    - 오늘(한국 시간)이 effective_from ~ effective_to 기간 안인 문서만 사용
    - visibility: internal 인 내부 문서 제외 (CPN-007, MAN-001/003/005)
    - 허용된 문서 안에서만 순위를 매겨 3개를 고름 (변경 위치: to_date, is_customer_doc, search_policy)
"""
import datetime
import importlib
import json
from llm_client import client, MODEL

KST = datetime.timezone(datetime.timedelta(hours=9))  # 한국 시간 (Windows 에서도 추가 패키지 없이 동작)

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
    amounts = order.get("amounts") or {}
    return {
        "order_id": order_id,
        "status": order["status"],
        "items": order["items"],
        "paid_amount": amounts.get("paid_amount"),  # 실제 결제 금액
        "points_used": amounts.get("points_used"),  # 사용한 포인트
        "refund_status": claim.get("status"),       # 환불·취소 요청의 처리 상태
        "refund": claim.get("refund"),              # 환불 금액
    }


def to_date(value):
    """yaml 날짜(date) 또는 문자열을 date 로 바꿈. null 이면 None"""
    if value is None:
        return None
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value))


def is_customer_doc(meta: dict, today: datetime.date) -> bool:
    """고객 안내 근거로 쓸 수 있는 문서인지 판단 (data/README.md 기준)"""
    if meta.get("status") != "approved":  # 초안·폐기·부결·만료·미확인 제외
        return False
    start, end = to_date(meta.get("effective_from")), to_date(meta.get("effective_to"))
    if start is not None and today < start:  # 아직 시행 전
        return False
    if end is not None and today > end:  # 시행 기간 끝남
        return False
    if meta.get("visibility") == "internal":  # 내부 문서는 고객에게 공개 금지
        return False
    return True


def search_policy(question: str, k: int = 3) -> dict:
    today = datetime.datetime.now(KST).date()
    allowed = [d for d in rag.DOCS if is_customer_doc(d["meta"], today)]
    # 03 과 같은 방식(글자 2개 단위 겹침)으로 순위를 매기되, 허용된 문서 안에서만 k개를 고름
    q = rag.bigrams(question)
    documents = sorted(allowed, key=lambda d: len(q & rag.bigrams(d["text"])), reverse=True)[:k]
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

print("질문을 입력하세요. 대화 기록을 지우려면 reset, 끝내려면 exit")

while True:
    question = input("\n나: ").strip()
    if question.lower() == "exit":
        break
    if not question:
        continue
    if question.lower() == "reset":
        del messages[1:]  # 시스템 지시만 남기고 대화 기록 삭제
        print("대화 기록을 지웠습니다.")
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
