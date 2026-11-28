"""베이스라인 B 용 조회 도구 — data/README.md 의 '전제한 도구 인터페이스'를 그대로 구현.

일부러 '단순하게' 만든 버전이다.
- search_knowledge 는 03_naive_rag 와 같은 글자 겹침 검색 (승인 상태·시행 기간 필터 없음, filters 를 넘겨도 status 만 처리)
- 상태 변경 도구(cancel_order, create_claim)는 필수 범위가 아니라서 제외
- 소유자 검증은 PDF 예제처럼 get_order 에만 있음
"""
import json
import pathlib
import re

import yaml

DATA = pathlib.Path(__file__).parent / "data"


def _load(name):
    return json.loads((DATA / "system" / f"{name}.json").read_text(encoding="utf-8"))


CUSTOMERS = {c["customer_id"]: c for c in _load("customers")}
ORDERS = {o["order_id"]: o for o in _load("orders")}
SHIPMENTS = {s["order_id"]: s for s in _load("shipments")}
PRODUCTS = {p["product_id"]: p for p in _load("products")}
RETURN_CONDITIONS = {r["product_id"]: r for r in _load("product_return_conditions")}
COUPONS = _load("coupons")


# ---------- 정책 문서 (03_naive_rag 와 같은 방식) ----------
def _load_policy_docs(path):
    text = path.read_text(encoding="utf-8")
    docs = []
    for chunk in re.split(r"\n(?=## \[)", text)[1:]:
        meta = yaml.safe_load(re.search(r"```yaml\n(.*?)```", chunk, re.S).group(1))
        body = re.sub(r"```yaml\n.*?```", "", chunk, flags=re.S).strip()
        docs.append({"meta": meta, "text": body})
    return docs


DOCS = [d for f in sorted((DATA / "knowledge").glob("*.md")) for d in _load_policy_docs(f)]


def _bigrams(s):
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


# ---------- 도구 함수 ----------
class ToolContext:
    """세션 정보 (로그인 고객). 모델이 정하는 값이 아님."""

    def __init__(self, customer_id):
        self.customer_id = customer_id
        self.retrieved_docs = []  # 채점용: search_knowledge 가 돌려준 문서 ID 기록


def get_customer_profile(ctx, customer_id):
    c = CUSTOMERS.get(customer_id)
    if c is None:
        return {"error": "NOT_FOUND"}
    return {k: c[k] for k in ("customer_id", "name", "grade", "points", "address", "status")}


def list_orders(ctx, customer_id, status=None, date_from=None):
    if customer_id != ctx.customer_id:
        return {"error": "FORBIDDEN"}
    out = []
    for o in ORDERS.values():
        if o["customer_id"] != customer_id:
            continue
        if status and o["status"] != status:
            continue
        if date_from and o["ordered_at"][:10] < date_from:
            continue
        out.append({
            "order_id": o["order_id"],
            "ordered_at": o["ordered_at"],
            "status": o["status"],
            "items": [i["product_name"] for i in o["items"]],
            "paid_amount": o["amounts"]["paid_amount"],
        })
    return {"orders": sorted(out, key=lambda x: x["ordered_at"], reverse=True)}


def get_order(ctx, order_id):
    o = ORDERS.get(order_id)
    if o is None:
        return {"error": "NOT_FOUND"}
    if o["customer_id"] != ctx.customer_id:
        return {"error": "FORBIDDEN"}
    return o


def get_shipment(ctx, order_id):
    s = SHIPMENTS.get(order_id)
    if s is None:
        return {"error": "NOT_FOUND"}
    return s


def get_product(ctx, product_id):
    return PRODUCTS.get(product_id) or {"error": "NOT_FOUND"}


def get_return_conditions(ctx, product_id):
    return RETURN_CONDITIONS.get(product_id) or {"error": "NOT_FOUND"}


def list_coupons(ctx, customer_id, status=None):
    return {"coupons": [c for c in COUPONS
                        if c["customer_id"] == customer_id and (not status or c["status"] == status)]}


def search_knowledge(ctx, query, filters=None, k=3):
    docs = DOCS
    if filters and filters.get("status"):
        docs = [d for d in docs if d["meta"].get("status") == filters["status"]]
    q = _bigrams(query)
    ranked = sorted(docs, key=lambda d: len(q & _bigrams(d["text"])), reverse=True)[:k]
    ctx.retrieved_docs += [d["meta"]["doc_id"] for d in ranked]
    return {"documents": [{"doc_id": d["meta"]["doc_id"], "status": d["meta"].get("status"),
                           "title": d["meta"].get("title"), "text": d["text"]} for d in ranked]}


def escalate_to_agent(ctx, reason_code, summary=""):
    return {"result": "ACCEPTED", "reason_code": reason_code}


FUNCS = {f.__name__: f for f in (
    get_customer_profile, list_orders, get_order, get_shipment, get_product,
    get_return_conditions, list_coupons, search_knowledge, escalate_to_agent)}


def _tool(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


_S = {"type": "string"}
TOOLS = [
    _tool("get_customer_profile", "고객 프로필(등급, 적립금, 주소) 조회", {"customer_id": _S}, ["customer_id"]),
    _tool("list_orders", "고객의 주문 목록 조회", {
        "customer_id": _S,
        "status": {"type": "string", "description": "주문 상태로 필터 (선택)"},
        "date_from": {"type": "string", "description": "YYYY-MM-DD 이후 주문만 (선택)"}}, ["customer_id"]),
    _tool("get_order", "주문 상세 조회 (상품, 금액, 쿠폰, 클레임 등)",
          {"order_id": {"type": "string", "description": "예: ORD-20260901-0001"}}, ["order_id"]),
    _tool("get_shipment", "주문의 배송 상태 조회 (택배사, 도착 예정일, 이벤트)", {"order_id": _S}, ["order_id"]),
    _tool("get_product", "상품 상세 조회 (재고, 판매상태)", {"product_id": _S}, ["product_id"]),
    _tool("get_return_conditions", "상품별 교환·환불 조건 조회", {"product_id": _S}, ["product_id"]),
    _tool("list_coupons", "고객 보유 쿠폰 조회", {"customer_id": _S, "status": _S}, ["customer_id"]),
    _tool("search_knowledge", "배송·환불·쿠폰·약관 등 정책 문서 검색", {
        "query": _S,
        "filters": {"type": "object", "description": "예: {\"status\": \"approved\"}"}}, ["query"]),
    _tool("escalate_to_agent", "상담원 연결 접수", {
        "reason_code": {"type": "string", "description": "E01~E11"}, "summary": _S}, ["reason_code"]),
]
