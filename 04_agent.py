"""4. 02 Tool Calling + 03 Naive RAG"""

import json
import pathlib
import re

import yaml

from llm_client import client, MODEL


# ============================================================
# 02. Tool Calling (원본)
# ============================================================
# 2. Tool Calling — 모델이 도구를 요청하면 실행해서 결과를 돌려주는 반복 구조


ORDERS = {
    o["order_id"]: o
    for o in json.load(open("data/system/orders.json", encoding="utf-8"))
}

SESSION_CUSTOMER = "C0017"  # 로그인한 고객 (세션에서 온 값 — 모델이 정하는 값이 아님)


def get_order(order_id: str) -> dict:
    order = ORDERS.get(order_id)

    if order is None:
        return {"error": "NOT_FOUND"}

    if order["customer_id"] != SESSION_CUSTOMER:  # 소유자 검증은 코드에서
        return {"error": "FORBIDDEN"}

    return {
        "order_id": order_id,
        "status": order["status"],
        "items": order["items"],
    }


TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_order",
        "description": "주문번호로 주문 정보를 조회",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "예: ORD-20260901-0001",
                }
            },
            "required": ["order_id"],
        },
    },
}]

FUNCS = {"get_order": get_order}

messages = [
    {
        "role": "system",
        "content": "너는 이커머스 고객 상담원이야. 주문 정보는 반드시 도구로 조회해.",
    },
    {
        "role": "user",
        "content": "ORD-20260901-0001 주문 상태 알려줘",
    },
]

for turn in range(5):  # 무한 반복 방지
    resp = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=TOOLS,
        temperature=0,
    )

    msg = resp.choices[0].message

    if not msg.tool_calls:  # 도구 요청이 없으면 최종 답변
        print("[답변]", msg.content)
        break

    messages.append(msg)  # 모델의 도구 요청을 대화에 추가

    for call in msg.tool_calls:
        args = json.loads(call.function.arguments)
        result = FUNCS[call.function.name](**args)  # 함수 호출

        print(f"[호출] {call.function.name} {args}")

        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(result, ensure_ascii=False),
        })  # 실행 결과를 role=tool 로 돌려줌


# ============================================================
# 03. Naive RAG (원본)
# ============================================================
# 3. Naive RAG 파이프라인 — 검색한 문서를 프롬프트에 넣어 답변 생성


def load_policy_docs(path):
    """정책 문서(.md)를 '## [DOC_ID] 제목' 단위로 나누고 yaml 메타데이터를 읽음"""
    text = pathlib.Path(path).read_text(encoding="utf-8")
    docs = []

    for chunk in re.split(r"\n(?=## \[)", text)[1:]:
        meta = yaml.safe_load(
            re.search(r"```yaml\n(.*?)```", chunk, re.S).group(1)
        )
        body = re.sub(r"```yaml\n.*?```", "", chunk, flags=re.S).strip()
        docs.append({"meta": meta, "text": body})

    return docs


DOCS = [
    d
    for f in sorted(pathlib.Path("data/knowledge").glob("*.md"))
    for d in load_policy_docs(f)
]


def bigrams(s):
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def search_policy(question, k=3):
    """가장 단순한 검색 — 질문과 글자 2개 단위로 많이 겹치는 문서 k개"""
    q = bigrams(question)
    ranked = sorted(
        DOCS,
        key=lambda d: len(q & bigrams(d["text"])),
        reverse=True,
    )
    return ranked[:k]


def ask_llm(prompt):
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )
    return resp.choices[0].message.content


def pipeline(question):
    documents = search_policy(question)
    context = "\n\n".join(d["text"] for d in documents)

    prompt = f"""다음 문서를 참고하여 고객 질문에 답변하세요.

[참고 문서]
{context}

[고객 질문]
{question}
"""

    answer = ask_llm(prompt)
    return answer, documents


if __name__ == "__main__":
    answer, documents = pipeline("무료 배송 기준이 얼마인가요?")

    for d in documents:
        print(
            f"[검색] {d['meta']['doc_id']} · "
            f"{d['meta']['status']} · {d['meta']['title']}"
        )

    print("[답변]", answer)
