"""guards.py — 중요한 판단을 LLM 대신 코드로 처리하는 함수 모음

이 파일에는 함수만 있습니다 (LLM 호출 루프 없음). 다른 파일에서 import 해서 씁니다.

    from guards import check_risk, risk_response, make_tools, safe_call

1. check_risk / risk_response : LLM 을 부르기 전에 위험 상황(E01·E02·E08)을 키워드로 감지
2. escalate_to_agent          : 상담원 연결 접수 (MAN-001, MAN-002 기준)
3. check_coupon_restore       : 취소·반품 시 쿠폰 복원 여부 판정 (CPN-010, REF-017 기준)
4. safe_call                  : 도구 실행 중 오류가 나면 상담원 연결로 넘김
5. verify_answer              : 최종 답변이 도구 판정과 다르거나 보상을 약속하면 잡아냄
   parse_text_tool_calls      : 모델이 도구 호출을 글자로 출력했을 때 찾아냄
6. make_tools(customer_id)    : 위 도구들의 스키마(TOOLS)와 함수 매핑(FUNCS)

고객 ID 는 도구 인자로 받지 않습니다. make_tools(세션 고객 ID) 로 코드에서 고정합니다.
"""

import datetime as dt
import itertools
import json
import os
import re

KST = dt.timezone(dt.timedelta(hours=9))

ORDERS = {
    o["order_id"]: o
    for o in json.load(open("data/system/orders.json", encoding="utf-8"))
}
COUPONS = {
    c["coupon_id"]: c
    for c in json.load(open("data/system/coupons.json", encoding="utf-8"))
}


def now():
    """현재 시각 (KST).

    환경변수 CAPSTONE_NOW 가 있으면 그 시각으로 고정합니다 (테스트·평가용).
    데이터와 평가 정답은 2026-11-28(토) 10:00~17:00 기준이라, 다른 날 테스트할 때 사용하세요.
        PowerShell:  $env:CAPSTONE_NOW = "2026-11-28T13:00:00+09:00"
    """
    fixed = os.getenv("CAPSTONE_NOW")
    if fixed:
        t = dt.datetime.fromisoformat(fixed)
        return t if t.tzinfo else t.replace(tzinfo=KST)
    return dt.datetime.now(KST)


def now_text() -> str:
    """프롬프트에 넣을 시각 문자열 — 예: 2026-11-28(토) 14:05 KST"""
    t = now()
    return f"{t:%Y-%m-%d}({'월화수목금토일'[t.weekday()]}) {t:%H:%M} KST"


# ============================================================
# 1. 위험 상황 사전 감지 — LLM 을 부르기 전에 실행 (도구 아님)
# ============================================================

# MAN-001 의 긴급 코드만 키워드로 잡습니다.
# 나머지(E04 보상 요구, E05 예외 요구, E10 상담원 요청 등)는 문맥 판단이 필요해서
# 모델이 escalate_to_agent 도구를 부르게 합니다.
RISK_KEYWORDS = {
    "E01": ["소송", "고소", "고발", "소비자원", "소비자보호원", "변호사", "법적 조치",
            "법적으로", "언론", "기자", "제보"],
    "E02": ["다쳤", "다침", "부상", "상처", "피가 나", "피가 났", "화상", "화재",
            "불이 났", "불났", "불이 붙", "폭발", "터졌", "감전", "연기가 나",
            "병원", "응급실", "찔렸", "베였"],
    "E08": ["해킹", "도용", "명의도용", "제가 주문한 게 아닌", "내가 주문한 게 아닌",
            "주문한 적 없", "주문한 적이 없", "비밀번호가 바뀌", "개인정보 유출",
            "정보가 유출"],
}

ESCALATION_CODES = {
    "E01": ("법적 분쟁 언급", "긴급"),
    "E02": ("신체 상해·화재·안전사고", "긴급"),
    "E03": ("욕설·폭언 반복", "높음"),
    "E04": ("보상·위로금·재량 보상 요구", "보통"),
    "E05": ("정책상 불가 사항 예외 처리 요구", "보통"),
    "E06": ("환불 예정 금액 50만원 이상", "높음"),
    "E07": ("배송 사고(분실·파손·오배송) 접수", "보통"),
    "E08": ("개인정보 유출·계정 도용 의심", "긴급"),
    "E09": ("동일 문의 3회 이상 반복 미해결", "보통"),
    "E10": ("고객의 상담원 연결 요청", "보통"),
    "E11": ("답변 근거를 찾을 수 없음", "보통"),
}
URGENT = {"E01", "E02", "E08"}

SAFETY_MESSAGES = {
    "E01": "말씀하신 내용은 정확한 확인이 필요한 사안이라 담당 상담원이 직접 도와드리겠습니다.",
    "E02": ("많이 놀라셨을 텐데 먼저 사과드립니다. 다치신 곳이 있다면 제품 사용을 즉시 중단하시고, "
            "필요하면 119 또는 가까운 병원에서 먼저 치료를 받으세요. "
            "상품과 포장은 버리지 말고 사진과 함께 보관해 주세요."),
    "E08": ("계정 보호를 위해 지금 바로 비밀번호를 변경해 주세요. "
            "본인이 하지 않은 주문이나 결제가 있다면 상담원이 확인 후 조치해 드립니다."),
}


def check_risk(user_text: str):
    """위험 키워드가 있으면 {"code", "matched"} 를, 없으면 None 을 돌려줌"""
    text = user_text.replace(" ", "")
    for code, words in RISK_KEYWORDS.items():
        for w in words:
            if w.replace(" ", "") in text:
                return {"code": code, "matched": w}
    return None


def risk_response(risk: dict, customer_id: str, user_text: str) -> str:
    """check_risk 결과로 상담원 연결을 접수하고, 고객에게 보여줄 고정 안내문을 만듦"""
    ticket = escalate_to_agent(
        customer_id, risk["code"], f"[자동 감지: '{risk['matched']}'] {user_text}"
    )
    return f"{SAFETY_MESSAGES[risk['code']]}\n\n{ticket['message']}"


# ============================================================
# 2. 상담원 연결 (도구)
# ============================================================

_ticket_seq = itertools.count(1)


def _next_business_day(t: dt.datetime) -> dt.date:
    d = t.date() + dt.timedelta(days=1)
    while d.weekday() >= 5:  # 토(5)·일(6) 휴무. 공휴일은 반영하지 않음
        d += dt.timedelta(days=1)
    return d


def _in_office_hours(t: dt.datetime) -> bool:
    """MAN-002: 평일 09:00~18:00, 점심 12:00~13:00 제외"""
    if t.weekday() >= 5:
        return False
    hm = t.hour * 60 + t.minute
    return 9 * 60 <= hm < 18 * 60 and not (12 * 60 <= hm < 13 * 60)


def escalate_to_agent(customer_id: str, reason_code: str, summary: str = "",
                      order_id: str | None = None) -> dict:
    if reason_code not in ESCALATION_CODES:
        return {"error": "INVALID_REASON_CODE", "allowed": list(ESCALATION_CODES)}

    t = now()
    label, priority = ESCALATION_CODES[reason_code]

    if reason_code in URGENT:
        route = "당직 상담원 즉시 연결"
        message = "긴급 건으로 접수되어 당직 상담원에게 바로 연결해 드리겠습니다. 잠시만 기다려 주세요."
    elif _in_office_hours(t):
        route = "상담원 연결 대기"
        message = "상담원에게 연결해 드리겠습니다. 잠시만 기다려 주세요."
    elif t.weekday() < 5 and t.hour == 12:  # 평일 점심시간 → 오늘 13시 이후 콜백
        d = t.date()
        weekday = "월화수목금토일"[d.weekday()]
        route = f"콜백 예약 ({d.isoformat()} 13:00 이후)"
        message = (f"지금은 상담원 점심시간(12:00~13:00)이라 상담 예약으로 접수했습니다. "
                   f"오늘 {d.month}월 {d.day}일({weekday}) 13시 이후에 연락드리겠습니다.")
    else:
        d = t.date() if (t.weekday() < 5 and t.hour < 9) else _next_business_day(t)
        weekday = "월화수목금토일"[d.weekday()]
        route = f"콜백 예약 ({d.isoformat()} 오전)"
        message = (f"지금은 상담원 운영 시간(평일 09:00~18:00)이 아니라 상담 예약으로 접수했습니다. "
                   f"{d.month}월 {d.day}일({weekday}) 오전 중 연락드리겠습니다.")

    return {
        "result": "ACCEPTED",
        "ticket_id": f"ESC-{t:%Y%m%d}-{next(_ticket_seq):04d}",
        "reason_code": reason_code,
        "reason": label,
        "priority": priority,
        "route": route,
        "message": message,  # 모델은 이 문장을 그대로 안내
        # 상담원에게 넘기는 요약 (MAN-001) — 고객 ID 는 세션 값
        "handoff": {"customer_id": customer_id, "order_id": order_id, "summary": summary},
    }


# ============================================================
# 3. 쿠폰 복원 판정 (도구) — CPN-010, REF-017
# ============================================================

def check_coupon_restore(customer_id: str, order_id: str, scope: str = "전체") -> dict:
    """scope: "전체"(전체 취소·반품) 또는 "부분"(부분 취소·반품)"""
    order = ORDERS.get(order_id)
    if order is None or order["customer_id"] != customer_id:
        # MAN-003: 타인 주문은 존재 여부도 알려주지 않음
        return {"error": "NOT_FOUND_OR_FORBIDDEN"}

    coupon_id = order.get("coupon_id")
    if not coupon_id:
        return {"restorable": False, "reason": "이 주문에는 쿠폰이 사용되지 않았습니다.",
                "policy_ref": None}

    coupon = COUPONS.get(coupon_id, {})
    base = {"coupon_id": coupon_id, "coupon_name": coupon.get("name"),
            "expires_at": coupon.get("expires_at")}

    # 이미 취소·반품이 끝난 주문이면 시스템에 기록된 실제 결과를 우선 (다시 계산하지 않음)
    claim = order.get("claim") or {}
    if coupon.get("restored") or claim.get("coupon_restored") is True:
        return {**base, "restorable": True, "already_processed": True,
                "reason": "취소·반품 처리 때 쿠폰이 이미 복원되었습니다. 유효기간은 연장되지 않습니다.",
                "record": coupon.get("restore_note"), "policy_ref": "CPN-010"}
    if claim.get("coupon_restored") is False or (
            claim.get("status") in ("환불완료", "취소완료") and coupon.get("restore_note")):
        return {**base, "restorable": False, "already_processed": True,
                "reason": "취소·반품 처리 때 쿠폰이 복원되지 않았습니다. 만료된 쿠폰은 재발급되지 않습니다.",
                "record": coupon.get("restore_note"), "policy_ref": "CPN-010, CPN-001"}

    if scope != "전체":
        return {**base, "restorable": False,
                "reason": "부분 취소·반품 시에는 쿠폰이 복원되지 않습니다. "
                          "쿠폰 할인액은 상품 금액 비율로 나눠 환불금액에서 차감됩니다.",
                "policy_ref": "CPN-010, REF-017"}

    expires = dt.datetime.fromisoformat(coupon["expires_at"])
    if expires < now():
        return {**base, "restorable": False,
                "reason": "쿠폰 유효기간이 지나 전체 취소·반품을 해도 복원되지 않습니다.",
                "policy_ref": "CPN-010, REF-017"}

    return {**base, "restorable": True,
            "reason": "전체 취소·반품 시 쿠폰이 복원됩니다. 복원된 쿠폰의 유효기간은 연장되지 않습니다.",
            "policy_ref": "CPN-010, REF-017"}


# ============================================================
# 4. 안전한 도구 실행 — 오류가 나면 모델이 지어내지 않도록 상담원 연결
# ============================================================

def safe_call(funcs: dict, name: str, args: dict, customer_id: str) -> dict:
    if name not in funcs:
        return {"error": "UNKNOWN_TOOL", "available": list(funcs)}
    try:
        return funcs[name](**args)
    except TypeError as e:  # 인자 이름·개수 오류 → 모델이 다시 시도하도록 돌려줌
        return {"error": "BAD_ARGUMENTS", "detail": str(e)}
    except Exception as e:  # 시스템 오류 → 상담원 연결
        ticket = escalate_to_agent(customer_id, "E11", f"도구 {name} 실행 오류: {e!r}")
        return {"error": "SYSTEM_ERROR",
                "instruction": "임의로 답하지 말고 아래 message 를 그대로 안내할 것",
                "message": "시스템 확인 중 문제가 생겼습니다. " + ticket["message"],
                "ticket_id": ticket["ticket_id"]}


# ============================================================
# 5. 최종 답변 검증 — 모델 답변이 도구 판정 결과와 다르면 잡아냄
# ============================================================

NEGATION = ("않", "없", "안 ", "안돼", "안 돼", "불가", "못 ", "못받", "어렵")
PROMISE = re.compile(
    r"(보상|위로금|적립금|포인트|쿠폰|환불|배송비).{0,20}"
    r"(드리겠|드릴게|드릴 수 있|해 ?드립니다|지급됩니다|지급해|지급하겠|발급해|발급하겠|면제해|면제됩니다)"
)
INTERNAL_ID = re.compile(r"\b(?:CPN|REF|SHIP|TOS|MAN|FAQ)-\d{3}\b")
TOOL_NAMES = ("get_order", "search_policy", "check_coupon_restore", "escalate_to_agent", "safe_call")
ORDER_ID = re.compile(r"ORD-\d{8}-\d{4}")
# 상담원에게 넘겼다/처리하겠다는 표현 — 실제 escalate_to_agent 접수 없이 쓰면 거짓 안내
ESCALATION_CLAIM = re.compile(
    r"(상담원|담당자|관리자|담당 부서).{0,15}"
    r"(전달|연결해|연결됩|연결되|접수|상의|넘겨|넘기|확인 후 처리|처리하겠|처리해 ?드리|도와드리겠|연락드리겠)"
    r"|처리 중입니다|접수되었습니다|접수했습니다"
)
CONDITIONAL = ("원하시면", "요청하시면", "말씀해 주시면", "말씀해주시면", "원하신다면")
REFUSAL_WORDS = ("본인 주문만", "본인의 주문만", "조회할 수 없", "조회가 불가", "확인이 어렵", "확인할 수 없",
                 "안내해 드릴 수 없", "알려드릴 수 없")


def _sentences(text: str):
    return [s for s in re.split(r"(?<=[.!?。])\s+|\n+", text) if s.strip()]


def verify_answer(answer: str, tool_log: list) -> list:
    """최종 답변을 검사해서 위반 사항 목록을 돌려줌 (빈 목록이면 통과)

    tool_log: [{"name": 도구이름, "args": {...}, "result": {...}}, ...]
    """
    problems = []
    answer = answer or ""

    # (1) 쿠폰 복원 — 도구 판정과 답변이 반대인지
    for t in tool_log:
        if t["name"] != "check_coupon_restore" or "restorable" not in t["result"]:
            continue
        ok = t["result"]["restorable"]
        for s in _sentences(answer):
            if "복원" not in s and "돌려" not in s:
                continue
            negated = any(n in s for n in NEGATION)
            if ok is False and not negated:
                problems.append(f"쿠폰은 복원되지 않는데 복원된다고 말함: \"{s.strip()}\"")
            if ok is True and negated and "연장" not in s:
                problems.append(f"쿠폰은 복원되는데 안 된다고 말함: \"{s.strip()}\"")

    # (2) 보상·환불 약속 — AI 는 약속할 수 없음 (CPN-005, CPN-007)
    for s in _sentences(answer):
        if PROMISE.search(s) and not any(n in s for n in NEGATION) and "상담원" not in s:
            problems.append(f"보상·환불을 약속함: \"{s.strip()}\"")

    # (3) 내부 정보 노출
    ids = INTERNAL_ID.findall(answer)
    if ids:
        problems.append(f"내부 문서 ID 노출: {sorted(set(ids))}")
    names = [n for n in TOOL_NAMES if n in answer]
    if names:
        problems.append(f"도구 이름 노출: {names}")

    # (4) 조회 권한 없는 주문의 정보 노출 (MAN-003)
    for t in tool_log:
        if t["result"].get("error") in ("FORBIDDEN", "NOT_FOUND_OR_FORBIDDEN"):
            order = ORDERS.get(t["args"].get("order_id", ""))
            if order:
                leaked = [i["product_name"] for i in order["items"] if i["product_name"] in answer]
                leaked += [order["status"]] if order["status"] in answer else []
                if leaked:
                    problems.append(f"타인 주문 정보 노출: {leaked}")

    # (5) 상담원 연결이 접수됐으면 안내 날짜가 빠지면 안 됨
    for t in tool_log:
        if t["name"] == "escalate_to_agent" and t["result"].get("result") == "ACCEPTED":
            m = re.search(r"(\d+월 \d+일)", t["result"]["message"])
            if m and m.group(1) not in answer:
                problems.append(f"상담원 연결 안내에서 '{m.group(1)}' 연락 일정이 빠짐")

    # (6) 확인되지 않은 주문 이야기 — 모델이 get_order 를 건너뛰고 지어내는 경우 (MAN-003)
    #     get_order 안의 소유자 검증은 "도구를 불렀을 때"만 작동하므로, 답변 쪽에서도 막음
    confirmed, refused = set(), set()
    for t in tool_log:
        oid = t["args"].get("order_id")
        if not oid:
            continue
        if t["result"].get("error"):
            refused.add(oid)
        else:
            confirmed.add(oid)  # 이번 질문에서 본인 주문으로 정상 조회됨
    for oid in sorted(set(ORDER_ID.findall(answer))):
        if oid in confirmed:
            continue
        if oid in refused:
            # 조회가 거절된 주문은 '안내 불가'라고 말할 때만 언급 가능
            if not any(w in answer for w in REFUSAL_WORDS):
                problems.append(f"조회가 거절된 주문({oid})에 대해 거절 안내 없이 답함")
        else:
            problems.append(f"조회하지 않은 주문({oid})의 정보를 말함 — 도구 확인 없이 지어냈을 가능성")

    # (7) 상담원 접수를 하지 않았는데 "전달했습니다·처리하겠습니다"라고 말함 (거짓 안내)
    escalated = any(t["name"] == "escalate_to_agent" and t["result"].get("result") == "ACCEPTED"
                    for t in tool_log)
    if not escalated:
        for s in _sentences(answer):
            if ESCALATION_CLAIM.search(s) and not any(c in s for c in CONDITIONAL):
                problems.append(f"상담원 접수 없이 접수·처리한 것처럼 말함: \"{s.strip()}\"")
                break

    return problems


def parse_text_tool_calls(text: str, allowed: set) -> list:
    """모델이 도구 호출을 정상 형식이 아닌 '글자'로 출력한 경우 찾아냄

    예) ' iNdEx\\n{"name": "search_policy", "arguments": {"question": "무료배송"}}\\n</tool_call>'
    → [("search_policy", {"question": "무료배송"})]
    allowed 에 있는 도구 이름만 인정 (그 외 이름은 무시)
    """
    calls, decoder = [], json.JSONDecoder()
    for m in re.finditer(r'\{\s*"name"\s*:', text or ""):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        name, args = obj.get("name"), obj.get("arguments", obj.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                continue
        if name in allowed and isinstance(args, dict):
            calls.append((name, args))
    return calls


def fallback_answer(tool_log: list) -> str:
    """재작성도 실패했을 때 쓰는 안전한 답변 — 도구 결과 문장만으로 구성"""
    parts = []
    for t in tool_log:
        r = t["result"]
        if t["name"] == "check_coupon_restore" and "reason" in r:
            parts.append(r["reason"])
        elif r.get("message"):
            parts.append(r["message"])
        elif r.get("error") in ("FORBIDDEN", "NOT_FOUND_OR_FORBIDDEN", "NOT_FOUND"):
            parts.append("본인 주문만 조회할 수 있습니다. 주문번호를 다시 확인해 주세요.")
    if not parts:
        # 실제로 상담원 접수를 하지 않았으므로 "연결해 드리겠습니다"라고 하지 않음
        parts.append("확인된 정보가 없어 지금 바로 안내해 드리기 어렵습니다. "
                     "본인 주문번호를 다시 알려주시거나, 상담원 연결을 원하시면 말씀해 주세요.")
    return " ".join(dict.fromkeys(parts))  # 중복 제거


# ============================================================
# 6. 도구 스키마 + 함수 매핑
# ============================================================

def make_tools(customer_id: str):
    """세션 고객 ID 를 고정한 (TOOLS, FUNCS) 를 돌려줌"""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "escalate_to_agent",
                "description": (
                    "상담원 연결을 접수한다. 다음 경우 스스로 답하거나 약속하지 말고 반드시 호출: "
                    "보상·위로금 요구(E04), 정책상 불가한 예외 요구(E05), 배송 사고 접수(E07), "
                    "욕설 반복(E03), 같은 문의 3회 이상(E09), 고객의 상담원 요청(E10), "
                    "근거를 찾을 수 없음(E11). 결과의 message 를 고객에게 그대로 안내한다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason_code": {"type": "string", "enum": list(ESCALATION_CODES)},
                        "summary": {"type": "string", "description": "문의 내용과 지금까지 안내한 내용 요약"},
                        "order_id": {"type": "string", "description": "관련 주문번호 (있으면)"},
                    },
                    "required": ["reason_code", "summary"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_coupon_restore",
                "description": (
                    "주문을 취소·반품할 때 사용한 쿠폰이 복원되는지 판정한다. "
                    "쿠폰 복원 질문에는 반드시 이 도구 결과(restorable, reason)대로만 답한다."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string", "description": "예: ORD-20261126-0001"},
                        "scope": {"type": "string", "enum": ["전체", "부분"],
                                  "description": "전체 취소·반품인지 일부 상품만인지"},
                    },
                    "required": ["order_id"],
                },
            },
        },
    ]
    funcs = {
        "escalate_to_agent": lambda **a: escalate_to_agent(customer_id, **a),
        "check_coupon_restore": lambda **a: check_coupon_restore(customer_id, **a),
    }
    return tools, funcs
