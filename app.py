# ============================================================
# BMDM 실험 플랫폼 (Streamlit + Claude API) — 통합 완성본
#
# ★ 2×2 between-subjects factorial design
#   셀A: BMDM + 분석적 과제  |  셀B: BMDM + 창의적 과제
#   셀C: 통제 + 분석적 과제  |  셀D: 통제 + 창의적 과제
#   참가자 1명 = 1개 셀, 1개 과제만 수행 (셀당 최대 30명)
#
# ★ 본 버전 반영 사항
#   1) 브레히트적 소외 전략 6종 (몰입 차단 추가) / FIXED_CYCLES = 6
#   2) 3.2절 Claim 측정: Claude API 기반 파라미터 평가 (+키워드 폴백)
#   3) 적응형 전략 선택: 예상 HI 감소폭 최대 전략 선택 (steepest descent)
#   4) 사전·사후 설문: 첨부 설문 PDF 기준으로 통일
#      - 과신 5문항, 정서 중시 4문항
#      - 사전설문에 메타인지 4개 차원 baseline + LLM 환각 단일문항 추가
#      - 사후 인지적 거리두기 4문항(sd5 제거), sm3·cf1 문구 PDF 반영
#   5) 확신도(완벽도) 슬라이더는 보조 탐색지표로 유지
# ============================================================

import streamlit as st
import json, re, copy, random, datetime, os, uuid, glob
import anthropic
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional

# ============================================================
# 설정
# ============================================================
CLAUDE_API_KEY = st.secrets.get("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
CLAUDE_MODEL   = "claude-haiku-4-5-20251001"
FIXED_CYCLES   = 6          # ★ 전략 6종에 맞춰 6사이클
HOST_PASSWORD  = st.secrets.get("HOST_PASSWORD", "bmdm2025admin")
MAX_PER_CELL   = 30

CELLS = {
    "A": {"group": "experimental", "task": "factual",  "label": "BMDM + 분석적 과제"},
    "B": {"group": "experimental", "task": "creative", "label": "BMDM + 창의적 과제"},
    "C": {"group": "control",      "task": "factual",  "label": "통제 + 분석적 과제"},
    "D": {"group": "control",      "task": "creative", "label": "통제 + 창의적 과제"},
}

# ── 한국 표준시(KST, UTC+9) ──
# Streamlit Community Cloud 서버는 UTC로 동작하므로 datetime.now()는 한국시간보다 9시간 느림.
# KST는 서머타임이 없어 고정 +9 오프셋이 연중 항상 정확하다.
KST = datetime.timezone(datetime.timedelta(hours=9))

def now_kst():
    """현재 한국 시각(timezone-aware)."""
    return datetime.datetime.now(KST)

def now_kst_str():
    """기록용 한국 시각 문자열 (예: 2026-06-25 20:55:44)."""
    return now_kst().strftime("%Y-%m-%d %H:%M:%S")

# ============================================================
# Google Sheets 연결 (Streamlit Community Cloud용)
# ============================================================
GSHEET_NAME = st.secrets.get("GSHEET_NAME", "BMDM_Results")

@st.cache_resource
def _get_gspread_client():
    """Google Sheets 클라이언트 초기화 (서비스 계정 인증)."""
    if "gcp_service_account" not in st.secrets:
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(
            dict(st.secrets["gcp_service_account"]), scopes=scope
        )
        return gspread.authorize(creds)
    except Exception as e:
        return None

def _get_worksheet(sheet_name):
    """스프레드시트에서 워크시트를 가져오거나 생성."""
    client = _get_gspread_client()
    if not client:
        return None
    try:
        spreadsheet = client.open(GSHEET_NAME)
        try:
            return spreadsheet.worksheet(sheet_name)
        except:
            return spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=200)
    except Exception as e:
        return None

# ============================================================
# 셀 인원 관리 (Google Sheets 우선, 로컬 JSON 폴백)
# ============================================================
CELL_COUNT_FILE = "cell_counts.json"

def _load_cell_counts():
    ws = _get_worksheet("cell_counts")
    if ws:
        try:
            data = ws.get_all_records()
            if data:
                return {row["cell"]: int(row["count"]) for row in data}
            else:
                ws.update("A1:B1", [["cell", "count"]])
                rows = [[k, 0] for k in CELLS]
                ws.update(f"A2:B{len(rows)+1}", rows)
                return {k: 0 for k in CELLS}
        except:
            pass
    if os.path.exists(CELL_COUNT_FILE):
        with open(CELL_COUNT_FILE, "r") as f:
            return json.load(f)
    return {k: 0 for k in CELLS}

def _save_cell_counts(counts):
    ws = _get_worksheet("cell_counts")
    if ws:
        try:
            rows = [["cell", "count"]] + [[k, v] for k, v in counts.items()]
            ws.update(f"A1:B{len(rows)}", rows)
        except:
            pass
    with open(CELL_COUNT_FILE, "w") as f:
        json.dump(counts, f)

def assign_random_cell() -> Optional[str]:
    """빈자리가 있는 셀 중 무작위 배정. 모두 찼으면 None."""
    counts = _load_cell_counts()
    available = [k for k, v in counts.items() if v < MAX_PER_CELL]
    if not available:
        return None
    cell = random.choice(available)
    counts[cell] += 1
    _save_cell_counts(counts)
    return cell

def assign_specific_cell(cell_key: str):
    """관리자용: 특정 셀에 배정 (인원 제한 무시)."""
    counts = _load_cell_counts()
    counts[cell_key] = counts.get(cell_key, 0) + 1
    _save_cell_counts(counts)

def get_cell_status() -> Dict:
    return _load_cell_counts()

def reset_cell_counts():
    """모든 셀 카운트를 0으로 초기화 (구글시트 + 로컬 동시)."""
    _save_cell_counts({k: 0 for k in CELLS})

def reset_all_results():
    """수집된 응답 데이터까지 모두 삭제 (구글시트 results 탭 + 로컬 JSON/CSV).
    되돌릴 수 없으므로 주의."""
    ws = _get_worksheet("results")
    if ws:
        try:
            ws.clear()
        except:
            pass
    for fp in glob.glob("results/*.json") + glob.glob("results/*.csv"):
        try:
            os.remove(fp)
        except:
            pass

# ============================================================
# Claude API
# ============================================================
def call_claude_api(system_prompt: str, user_message: str, max_tokens: int = 500) -> str:
    try:
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        r = client.messages.create(model=CLAUDE_MODEL, max_tokens=max_tokens,
                                   system=system_prompt, messages=[{"role":"user","content":user_message}])
        return r.content[0].text.strip()
    except:
        return ""

# ============================================================
# ★ 3.2절: Claim 파라미터 측정 (Claude API 기반)
#   사용자 발화를 분석하여 Certainty / Affect / Evidence / Source를
#   각각 [0,1]로 평가한다. API 실패 시 키워드 휴리스틱으로 폴백.
# ============================================================
CLAIM_ASSESS_SYS = """당신은 BMDM의 Claim 분석기입니다. 사용자 발화를 분석하여 아래 네 파라미터를 각각 0.0~1.0 실수로 평가하세요.
- certainty(확신 수준): "반드시·완벽·확실·절대" 등 단정·보편·절대 표현이 강할수록 높음
- affect_intensity(정서 강도): 자부심·명예·애착 등 정서적 함의가 강할수록 높음
- evidence_status(근거 상태): 수치·데이터·실험·인용·출처 등 검증 가능한 근거가 명시될수록 높음
- source_ambiguity(출처 모호성): 출처가 생략·불분명할수록 높음
설명 없이 JSON만 응답: {"certainty":0.0,"affect_intensity":0.0,"evidence_status":0.0,"source_ambiguity":0.0}"""

def assess_claim_via_llm(text: str):
    """발화의 네 파라미터를 LLM으로 평가. 실패 시 None."""
    r = call_claude_api(CLAIM_ASSESS_SYS, f'발화: "{text}"', max_tokens=200)
    if r:
        try:
            clean = re.sub(r'```json\s*|```\s*', '', r.strip())
            d = json.loads(clean)
            clamp = lambda x: max(0.0, min(1.0, float(x)))
            return (clamp(d.get("certainty", 0.5)), clamp(d.get("affect_intensity", 0.5)),
                    clamp(d.get("evidence_status", 0.5)), clamp(d.get("source_ambiguity", 0.5)))
        except Exception:
            return None
    return None

# ============================================================
# 실험집단: 필수 프롬프트 + Claude API 도입 문장
# ============================================================
MANDATORY_PROMPTS = {
    "Immersion_Interruption": [
        "현재 핵심 주장을 한 문장으로 정리하면 무엇입니까?",
        "이 주장에 대한 확신 수준을 0~100%로 표현하면 어느 정도입니까?"
    ],
    "Externalize": [
        "이 생각을 내가 아닌 다른 사람의 관점에서는 어떻게 설명할 수 있겠습니까?",
        "이 주장을 외부에서 관찰한다면 어떤 특징이 보일까요?"
    ],
    "Origin_Source_Differentiation": [
        "이 생각은 당신의 어떤 경험이나 정보에서 비롯되었습니까?",
        "당신이 직접 관찰한 사실과, 관찰이 아닌 추론한 부분을 구분해볼 수 있습니까?"
    ],
    "Counter_Position": [
        "이 주장에 대해 가장 설득력 있는 반대 설명은 무엇일까요?",
        "다른 해석 가능성은 무엇이 있을까요?"
    ],
    "Evidence_Calibration": [
        "이 주장을 지지하는 근거는 무엇입니까?",
        "어떤 조건에서 이 주장이 수정되어야 합니까?"
    ],
    "Probability_Framing": [
        "이 주장을 확률적 표현으로 바꾼다면 어떻게 표현할 수 있습니까?",
        "확률을 변화시킬 수 있는 추가 정보는 무엇입니까?"
    ],
}

STRATEGY_DESCRIPTIONS = {
    "Immersion_Interruption": "자동적·습관적 인지 흐름을 일시 중단시키고 핵심 주장을 명시적으로 재진술하게 하여 인지적 자동성을 약화시킵니다.",
    "Externalize": "특정 신념을 자기 자신과 분리된 관점으로 재구성하도록 유도하여 인지적 거리를 형성합니다.",
    "Origin_Source_Differentiation": "생각의 기원과 근거 출처를 구분하도록 요구합니다.",
    "Counter_Position": "현재 주장과 경쟁하는 대안적 해석을 생성하도록 요구합니다.",
    "Evidence_Calibration": "주장의 증거와 수정/철회 조건을 동시에 명시하도록 요구합니다.",
    "Probability_Framing": "이분법적 판단을 확률 표현으로 변환하도록 유도합니다.",
}

EXP_SYSTEM_PROMPT = """당신은 브레히트의 소외효과에 기반한 메타인지 활성화 AI 조수입니다.
사용자의 발화를 읽고, 곧 제시될 메타인지 질문에 자연스럽게 연결되는 '맥락적 도입 문장'을 1문장만 생성하세요.
- 사용자의 구체적 발화 내용을 언급하면서 질문으로 자연스럽게 이어지도록 합니다.
- 절대 사용자의 주장을 부정하거나 비판하지 마세요.
- 질문은 생성하지 마세요. 도입 문장 1개만 생성하세요.
- 반드시 한국어로 응답하세요.
현재 전략: [{strategy_name}] — {strategy_description}
"""

def generate_experimental_prompt(strategy_mode, user_input, history, task_key):
    mandatory = MANDATORY_PROMPTS.get(strategy_mode, ["현재 판단을 검토해볼 수 있습니까?"])
    system = EXP_SYSTEM_PROMPT.format(
        strategy_name=strategy_mode,
        strategy_description=STRATEGY_DESCRIPTIONS.get(strategy_mode, ""))
    task_label = "분석적·사실기반 과제" if task_key == "factual" else "창의적·개방형 과제"
    hist = ""
    if history:
        for t in history[-3:]:
            hist += f"\n[AI] {', '.join(t.get('assistant_prompts',[]))}\n[사용자] {t.get('user_response','')}"
    msg = f"과제: {task_label}\n사용자 발화: \"{user_input}\"{hist}\n\n곧 아래 질문이 제시됩니다:\n" + \
          "\n".join(f"- {q}" for q in mandatory) + "\n\n도입 문장 1개만 생성하세요."
    intro = call_claude_api(system, msg, 150)
    result = []
    if intro:
        line = re.sub(r'^\d+[\.\)]\s*', '', intro.strip().split("\n")[0].strip())
        if line and len(line) > 5:
            result.append(line)
    result.extend(mandatory)
    return result

# ============================================================
# 통제집단: 템플릿 고정 LLM 호출 (엄격한 제약 + 검증 + 폴백)
# ============================================================
CTRL_CYCLE_TEMPLATES = {
    "creative": [
        {"intent": "전반적 격려 + 내용 보완 요청",
         "base":   "매우 좋습니다. 더 나은 결과를 위해 내용을 보완해 주세요."},
        {"intent": "긍정 평가 + 표현 다듬기 요청",
         "base":   "좋은 방향입니다. 표현을 조금 더 다듬어서 발전시켜 주세요."},
        {"intent": "진행 인정 + 내용 확장 요청",
         "base":   "잘 진행되고 있습니다. 내용을 더 풍부하게 확장해 주세요."},
        {"intent": "강한 칭찬 + 완성도 보완 요청",
         "base":   "훌륭합니다. 완성도를 높이기 위해 조금 더 보완해 주세요."},
        {"intent": "완성 임박 인정 + 마무리 정리 요청",
         "base":   "거의 완성에 가깝습니다. 마무리를 위해 내용을 정리해 주세요."},
        {"intent": "최종 마감 격려 + 다듬기 요청",
         "base":   "좋습니다. 마지막으로 전체를 한 번 더 다듬어 주세요."},
    ],
    "factual": [
        {"intent": "전반적 격려 + 내용 보완 요청",
         "base":   "매우 좋습니다. 더 나은 결과를 위해 내용을 보완해 주세요."},
        {"intent": "긍정 평가 + 구체화 요청",
         "base":   "좋은 분석입니다. 내용을 조금 더 구체적으로 작성해 주세요."},
        {"intent": "진행 인정 + 서술 명확화 요청",
         "base":   "잘 진행되고 있습니다. 서술을 좀 더 명확하게 다듬어 주세요."},
        {"intent": "강한 칭찬 + 완성도 보완 요청",
         "base":   "훌륭합니다. 완성도를 높이기 위해 조금 더 보완해 주세요."},
        {"intent": "완성 임박 인정 + 마무리 정리 요청",
         "base":   "거의 완성에 가깝습니다. 마무리를 위해 내용을 정리해 주세요."},
        {"intent": "최종 마감 격려 + 정리 요청",
         "base":   "좋습니다. 마지막으로 전체 내용을 한 번 더 정리해 주세요."},
    ],
}

CTRL_SYSTEM_STRICT = """당신은 사용자의 작업을 격려하는 단순 응원 도우미입니다.

[절대 규칙 — 위반 시 실험이 무효화됩니다]
1. 질문을 절대 하지 마세요. 물음표(?)를 사용하지 마세요.
2. 사용자 주장의 옳고 그름, 근거의 유무, 출처의 타당성을 절대 언급하지 마세요.
3. "왜", "어떻게", "무엇이", "정말", "확실합니까", "근거는", "출처는" 같은 검증 표현을 절대 쓰지 마세요.
4. 대안적 해석, 반대 관점, 다른 가능성을 절대 제시하지 마세요.
5. 사용자에게 생각을 돌아보라고 하지 마세요.
6. "~해볼까요", "~생각해봅시다", "~살펴봅시다" 같은 성찰 유도 표현을 쓰지 마세요.

[당신이 할 일]
- 아래 '기본 문장'을 거의 그대로 사용하되, 자연스럽게 한 번만 다시 표현하세요.
- 한국어로 1문장만 출력하세요. 평서문으로 끝내세요(~주세요, ~바랍니다 등).
- 기본 문장의 의미(칭찬 + 내용/표현 개선 요청)를 절대 벗어나지 마세요.
- 설명, 해설, 부가문을 덧붙이지 마세요. 1문장만."""


_CTRL_FORBIDDEN_PATTERNS = [
    r'\?', r'\bwhy\b', r'\bhow\b',
    '왜', '어떻게', '무엇이', '무엇을', '무엇인', '어디에서', '어떤 점', '어떤 근거',
    '근거는', '출처는', '확실', '정말로', '검증', '반대', '대안', '다른 관점',
    '돌아보', '성찰', '살펴보', '생각해보', '생각해 보', '해볼까', '해 볼까',
    '타당', '옳은', '맞는지', '틀린', '의문', '의심'
]

def _validate_ctrl_output(text: str) -> bool:
    """LLM 출력이 통제 조건을 위반하지 않는지 검증"""
    if not text or len(text.strip()) < 5 or len(text) > 120:
        return False
    low = text.lower()
    for pat in _CTRL_FORBIDDEN_PATTERNS:
        if re.search(pat, low):
            return False
    if not re.search(r'(요|다|오|시오|바랍니다)\.?\s*$', text.strip()):
        return False
    return True


def generate_control_prompt(user_input, history, task_key):
    """통제집단: 템플릿 고정 LLM 호출 + 검증 + 폴백"""
    cycle = len(history) if history else 0
    templates = CTRL_CYCLE_TEMPLATES.get(task_key, CTRL_CYCLE_TEMPLATES["factual"])
    tpl = templates[cycle % len(templates)]
    base_sentence = tpl["base"]
    intent = tpl["intent"]

    user_msg = f"""[의도] {intent}
[기본 문장] "{base_sentence}"

위 '기본 문장'을 거의 그대로 사용하되, 자연스럽게 한 번만 다시 표현하여 1문장으로 출력하세요.
질문·검증·성찰 유도 표현은 절대 금지입니다. 평서문 1문장만."""

    for _ in range(2):
        out = call_claude_api(CTRL_SYSTEM_STRICT, user_msg, max_tokens=120)
        if out:
            line = out.strip().split("\n")[0].strip()
            line = re.sub(r'^["\'\d\.\)\s\-]+', '', line).strip()
            line = line.strip('"').strip("'").strip()
            if _validate_ctrl_output(line):
                return [line]

    return [base_sentence]

# ============================================================
# 메타인지 활성화 평가
# ============================================================
META_EVAL_SYS = """메타인지 활성화 증분값을 평가하세요. 0.0~0.35 범위.
JSON만 응답: {"cognitive_distance":0.0,"reality_monitoring":0.0,"counterfactual_simulation":0.0,"epistemic_humility":0.0}"""

def evaluate_meta_cognitive(mode, user_response, current):
    r = call_claude_api(META_EVAL_SYS, f"전략: {mode}\n응답: \"{user_response}\"", 200)
    if r:
        try:
            clean = re.sub(r'```json\s*|```\s*', '', r.strip())
            u = json.loads(clean)
            keys = ["cognitive_distance","reality_monitoring","counterfactual_simulation","epistemic_humility"]
            return {k: min(1.0, current.get(k,0) + max(0, min(0.35, float(u.get(k,0))))) for k in keys}
        except: pass
    text = user_response.lower()
    updates = dict(current)
    if mode=="Externalize" and any(k in text for k in ["외부","대중","다를 수","다르게"]):
        updates["cognitive_distance"] = min(1.0, updates.get("cognitive_distance",0)+0.25)
    if mode=="Origin_Source_Differentiation" and any(k in text for k in ["감정","상징","직접","관찰","추론","정보"]):
        updates["reality_monitoring"] = min(1.0, updates.get("reality_monitoring",0)+0.28)
    if mode=="Counter_Position" and any(k in text for k in ["반대","다른 해석","배타","지배적","부정적"]):
        updates["counterfactual_simulation"] = min(1.0, updates.get("counterfactual_simulation",0)+0.32)
    if mode=="Evidence_Calibration" and any(k in text for k in ["기준","명확","근거","정의","반증"]):
        updates["reality_monitoring"] = min(1.0, updates.get("reality_monitoring",0)+0.22)
        updates["epistemic_humility"] = min(1.0, updates.get("epistemic_humility",0)+0.18)
    if mode=="Probability_Framing" and ("%" in user_response or any(k in text for k in ["확률","가능성","50","60","70"])):
        updates["epistemic_humility"] = min(1.0, updates.get("epistemic_humility",0)+0.35)
    if mode=="Immersion_Interruption" and any(k in text for k in ["요약","정리","핵심","확신"]):
        updates["cognitive_distance"] = min(1.0, updates.get("cognitive_distance",0)+0.10)
    return updates

# ============================================================
# 데이터 구조
# ============================================================
@dataclass
class Claim:
    content:str; certainty:float=0.5; affect_intensity:float=0.5
    evidence_status:float=0.5; source_ambiguity:float=0.5

@dataclass
class ConversationState:
    cycle_count:int=0; history:list=field(default_factory=list)
    meta_cognitive_activation:Dict[str,float]=field(default_factory=lambda:{
        "cognitive_distance":0.0,"reality_monitoring":0.0,
        "counterfactual_simulation":0.0,"epistemic_humility":0.0})
    hallucination_metrics:Dict[str,float]=field(default_factory=dict)
    stop_reason:str=""

# ============================================================
# 과제 안내문
# ============================================================
TASK_INFO = {
    "creative": {
        "label": "창의적·개방형 과제 — 월드컵 영화 기획",
        "instruction": """**[과제] 영화 기획**

월드컵을 소재로 한 영화를 만든다면 적절한 영화 제목과 간단한 줄거리(300자 이내)를 창의적으로 작성해 보세요.""",
        "final_label": "최종 결정한 영화 제목",
        "confidence_q": "방금 작성하신 영화 제목과 줄거리가 얼마나 완벽하다고 생각하시나요?",
    },
    "factual": {
        "label": "분석적·사실기반 과제 — 월드컵 우승팀 분석",
        "instruction": """**[과제] 월드컵 우승 가능성 분석**

우리가 월드컵 경기를 볼 때 어떤 팀이 우승할 확률이 높다고 생각하시나요? 최근 주변 사람들의 의견이나 본인이 기억하는 월드컵 경기의 사례를 바탕으로 비교하여 분석해 보세요.""",
        "final_label": "최종 도출한 핵심 분석 (한 줄 요약)",
        "confidence_q": "방금 작성하신 분석이 얼마나 완벽하다고 생각하시나요?",
    }
}

# ============================================================
# BMDM 엔진
# ============================================================
class BMDMEngine:
    # ★ 전략 6종 (몰입 차단 포함)
    ALL_STRATEGIES = [
        "Immersion_Interruption","Externalize","Origin_Source_Differentiation",
        "Counter_Position","Evidence_Calibration","Probability_Framing"]
    STEP_LABELS = {
        "Immersion_Interruption":"몰입 차단(Immersion Interruption)",
        "Externalize":"외재화(Externalization)",
        "Origin_Source_Differentiation":"기원–출처 분리(Origin–Source Differentiation)",
        "Counter_Position":"대안 관점 유도(Counter-Position Induction)",
        "Evidence_Calibration":"근거 재구성(Evidence Calibration)",
        "Probability_Framing":"확률적 재구성(Probabilistic Reframing)",
        "Control_Supportive":"AI 피드백"}

    def Extract_Claims(self, text):
        """★ 3.2절: Claude API로 4개 파라미터 평가, 실패 시 키워드 휴리스틱 폴백."""
        t = text.strip()
        vals = assess_claim_via_llm(t)
        if vals:
            c, a, e, s = vals
            return [Claim(content=t, certainty=c, affect_intensity=a,
                          evidence_status=e, source_ambiguity=s)]
        return [Claim(content=t, certainty=self._est_cert(t), affect_intensity=self._est_affect(t),
                       evidence_status=self._est_evidence(t), source_ambiguity=self._est_source(t))]

    # ── 적응형 전략 선택 (steepest descent) ──────────────────
    def _expected_effect(self, claim, mode):
        """전략별 예상 상태 변화 모델 (사전 정의된 idealized 효과)."""
        c = copy.deepcopy(claim)
        if mode == "Immersion_Interruption":
            c.certainty = max(0.0, c.certainty - 0.05)
        elif mode == "Externalize":
            c.certainty = max(0.0, c.certainty - 0.10)
            c.source_ambiguity = max(0.0, c.source_ambiguity - 0.06)
        elif mode == "Origin_Source_Differentiation":
            c.source_ambiguity = max(0.0, c.source_ambiguity - 0.20)
            c.evidence_status = min(1.0, c.evidence_status + 0.08)
        elif mode == "Counter_Position":
            c.certainty = max(0.0, c.certainty - 0.16)
            c.affect_intensity = max(0.0, c.affect_intensity - 0.05)
        elif mode == "Evidence_Calibration":
            c.evidence_status = min(1.0, c.evidence_status + 0.20)
            c.certainty = max(0.0, c.certainty - 0.10)
        elif mode == "Probability_Framing":
            # certainty를 evidence_status 쪽으로 보정 → Calibration Error 감소
            c.certainty = c.certainty - 0.5 * (c.certainty - c.evidence_status)
        return c

    def Select_Best_Strategy(self, claim, state, used_modes):
        """예상 HI 감소폭이 가장 큰 미사용 전략 선택 (greedy risk minimization).
        반환: (선택 전략, 예상 HI 감소량)."""
        candidates = [m for m in self.ALL_STRATEGIES if m not in used_modes]
        if not candidates:
            return self.ALL_STRATEGIES[-1], 0.0
        cur_hi = self.Calculate_HI(claim, state)["Hallucination_Index"]
        best, best_drop = candidates[0], -1.0
        for m in candidates:
            sim = self._expected_effect(claim, m)
            sim_hi = self.Calculate_HI(sim, state)["Hallucination_Index"]
            drop = cur_hi - sim_hi
            if drop > best_drop:
                best, best_drop = m, drop
        return best, round(best_drop, 3)

    def Select_Next_Strategy(self, used_modes):
        """순차 선택 (관리자 건너뛰기 등 폴백 용도)."""
        for m in self.ALL_STRATEGIES:
            if m not in used_modes: return m
        return self.ALL_STRATEGIES[-1]

    def Calculate_HI(self, claim, state):
        c=claim.certainty; eg=1.0-claim.evidence_status; a=claim.affect_intensity
        ce=abs(claim.certainty-claim.evidence_status)
        inc=self._est_inconsistency(state.history)
        fu,_=self._fuzzy_risk(c,eg); fs,_=self._fuzzy_risk(claim.source_ambiguity,eg)
        fa,_=self._fuzzy_risk(a,c)
        hi=0.30*fu+0.25*fs+0.20*fa+0.15*ce+0.10*inc
        return {"Fuzzy_Unsupported_Claim":round(fu,3),"Fuzzy_Source_Risk":round(fs,3),
                "Fuzzy_Affective_Risk":round(fa,3),"Calibration_Error":round(ce,3),
                "Inconsistency":round(inc,3),"Hallucination_Index":round(hi,3)}

    def Update_Claim(self, claim, mode, resp, group="experimental"):
        """Claim 파라미터 갱신 — 양 집단 동일한 측정 로직(조건 불변).

        논문 3.5절 "조건 차이는 오직 프롬프팅 방식에만 있다"에 맞춰,
        HI 측정은 참가자 발화 텍스트만으로 두 집단 모두 동일하게 수행한다.
        (group 파라미터는 하위호환용이며 측정에 영향을 주지 않는다.)
        """
        self._apply_common_update(claim, resp)

    def _apply_common_update(self, claim, resp):
        """양 집단 공통: 참가자 발화 텍스트만으로 파라미터 갱신 (조건 불변 측정)."""
        t = resp.lower()
        STEP = 0.04
        if any(k in t for k in ["데이터","근거","통계","연구","조사","실험","자료","보고서","문헌"]):
            claim.evidence_status = min(1.0, claim.evidence_status + STEP)
        if any(k in t for k in ["에 따르면","보고서","발표","출처","인용","논문"]):
            claim.source_ambiguity = max(0.0, claim.source_ambiguity - STEP)
        if any(k in t for k in ["아마","가능","추정","것 같","일지","어쩌면","모르","불확실"]):
            claim.certainty = max(0.0, claim.certainty - STEP)
        elif any(k in t for k in ["반드시","완벽","확실","절대","틀림없","분명","유일"]):
            claim.certainty = min(1.0, claim.certainty + STEP)
        if any(k in t for k in ["자부심","위상","애착","감동","상징","명예","희망"]):
            claim.affect_intensity = min(1.0, claim.affect_intensity + STEP)
        elif any(k in t for k in ["객관","중립","냉정","사실적"]):
            claim.affect_intensity = max(0.0, claim.affect_intensity - STEP)

        # ── 기존 _apply_strategy_bonus의 내용 탐지를 mode/집단과 무관하게 통합 ──
        if any(k in t for k in ["외부","제3자","다를 수","다르게","관점에서"]):       # 외재화
            claim.certainty = max(0.0, claim.certainty - 0.10)
            claim.source_ambiguity = max(0.0, claim.source_ambiguity - 0.06)
        if any(k in t for k in ["반대","다른 해석","배타","지배적","부정적"]):          # 대안관점
            claim.certainty = max(0.0, claim.certainty - 0.16)
            claim.affect_intensity = max(0.0, claim.affect_intensity - 0.05)
        if any(k in t for k in ["반증","판단 기준","수정되어야"]):                      # 근거 재구성
            claim.evidence_status = min(1.0, claim.evidence_status + 0.20)

        # 참가자가 '직접 텍스트로' 확률/확신 수치를 진술한 경우만 반영 (슬라이더 주입값 아님)
        nums = re.findall(r'(\d+)\s*%', resp)
        if nums:
            claim.certainty = min(max(int(nums[-1]) / 100.0, 0.0), 1.0)

    def _apply_strategy_bonus(self, claim, mode, resp):
        """실험집단 전용: BMDM 전략별 추가 조정."""
        t = resp.lower()
        if mode == "Immersion_Interruption":
            # 핵심 주장 재진술 — 확신 수준은 슬라이더 입력값으로 run_task_phase에서 반영됨
            pass
        elif mode == "Externalize" and any(k in t for k in ["외부","다를 수","다르게"]):
            claim.certainty = max(0, claim.certainty - 0.10)
            claim.source_ambiguity = max(0, claim.source_ambiguity - 0.06)
        elif mode == "Origin_Source_Differentiation":
            if any(k in t for k in ["감정","상징","애착"]):
                claim.source_ambiguity = max(0, claim.source_ambiguity - 0.20)
            if any(k in t for k in ["데이터","정보","근거"]):
                claim.evidence_status = min(1, claim.evidence_status + 0.08)
        elif mode == "Counter_Position" and any(k in t for k in ["배타","지배적","부정적","반대"]):
            claim.certainty = max(0, claim.certainty - 0.16)
            claim.affect_intensity = max(0, claim.affect_intensity - 0.05)
        elif mode == "Evidence_Calibration":
            if any(k in t for k in ["근거","반증","판단 기준"]):
                claim.evidence_status = min(1, claim.evidence_status + 0.20)
            if any(k in t for k in ["정의하지 않았다","기준이 없다"]):
                claim.certainty = max(0, claim.certainty - 0.10)
        elif mode == "Probability_Framing":
            nums = re.findall(r'(\d+)\s*%', resp)
            if nums:
                claim.certainty = min(max(int(nums[-1]) / 100, 0), 1)

    # fuzzy helpers
    def _tri(self,x,a,b,c):
        if x<=a or x>=c: return 0.0
        return 1.0 if x==b else (x-a)/(b-a+1e-8) if x<b else (c-x)/(c-b+1e-8)
    def _trap(self,x,a,b,c,d):
        if x<=a or x>=d: return 0.0
        if b<=x<=c: return 1.0
        return (x-a)/(b-a+1e-8) if a<x<b else (d-x)/(d-c+1e-8)
    def _fuzzy_risk(self,v1,v2):
        h1=self._trap(v1,0.55,0.75,1,1); m1=self._tri(v1,0.30,0.50,0.70); l1=self._trap(v1,0,0,0.25,0.45)
        h2=self._trap(v2,0.60,0.80,1,1); m2=self._tri(v2,0.25,0.50,0.75); l2=self._trap(v2,0,0,0.20,0.40)
        vh=min(h1,h2); hi=max(min(h1,m2),min(m1,h2)); me=max(min(m1,m2),min(h1,l2)); lo=min(l1,l2)
        n=lo*0.20+me*0.50+hi*0.75+vh*0.95; d=lo+me+hi+vh+1e-8
        return n/d, ""
    def _est_cert(self,t):
        s=0.55
        for m in ["완벽","반드시","확실","틀림없","유일","분명","절대"]:
            if m in t: s+=0.08
        return min(s,0.98)
    def _est_affect(self,t):
        s=0.45
        for m in ["자부심","상징","위상","애착","감동","명예"]:
            if m in t: s+=0.07
        return min(s,0.95)
    def _est_evidence(self,t):
        s=0.35
        for m in ["데이터","실험","조사","근거","연구","통계","출처"]:
            if m in t: s+=0.10
        return min(s,0.95)
    def _est_source(self,t):
        s=0.50
        for m in ["생각","느낀다","상징","자부심","완벽","느낌","추정"]:
            if m in t: s+=0.06
        return min(s,0.95)
    def _est_inconsistency(self, history):
        vals = []
        for h in history:
            if not isinstance(h, dict):
                continue
            c = h.get("claim_certainty", None)
            if c is not None:
                vals.append(float(c))
        if len(vals) < 2:
            return 0.05
        return min(0.30, (max(vals) - min(vals)) * 0.5)

# ============================================================
# 과제 실행 (참가자: 수치 비노출)
# ============================================================
def run_task_phase():
    engine = st.session_state.engine
    group  = st.session_state.group
    task_key = st.session_state.task_key
    task = TASK_INFO[task_key]
    prefix = "t_"

    st.title("📝 과제 수행")

    # [A] 초기 입력
    if not st.session_state.get(prefix+"initial_input"):
        st.markdown(task["instruction"])
        with st.form("initial_form"):
            user_input = st.text_area("당신의 주장과 이유를 자세히 작성하세요", height=150,
                                       placeholder="자유롭게 작성해주세요...")
            st.markdown("---")
            st.caption(task["confidence_q"])
            pre_conf = st.slider("완벽도 (0~100%)", 0, 100, 50, key="pre_conf")  # 보조 탐색지표
            submitted = st.form_submit_button("제출 및 AI와 대화 시작", use_container_width=True, type="primary")
        if submitted and user_input.strip():
            claims = engine.Extract_Claims(user_input)
            claim = claims[0]
            state = ConversationState()
            init_m = engine.Calculate_HI(claim, state)
            state.hallucination_metrics = init_m
            st.session_state[prefix+"initial_input"] = user_input
            st.session_state[prefix+"pre_confidence"] = pre_conf
            st.session_state[prefix+"claim"] = claim
            st.session_state[prefix+"state"] = state
            st.session_state[prefix+"initial_metrics"] = init_m
            st.session_state[prefix+"used_modes"] = []
            st.session_state[prefix+"transcript"] = []
            st.session_state[prefix+"is_done"] = False
            # 첫 프롬프트
            if group == "control":
                st.session_state[prefix+"cur_prompts"] = generate_control_prompt(user_input, [], task_key)
                st.session_state[prefix+"cur_mode"] = "Control_Supportive"
            else:
                # ★ 적응형: 예상 HI 감소폭이 가장 큰 전략 선택
                mode, _ = engine.Select_Best_Strategy(claim, state, [])
                st.session_state[prefix+"cur_mode"] = mode
                st.session_state[prefix+"cur_prompts"] = generate_experimental_prompt(mode, user_input, [], task_key)
            st.rerun()

    # [B] 대화 진행
    else:
        claim = st.session_state[prefix+"claim"]
        state = st.session_state[prefix+"state"]
        transcript = st.session_state[prefix+"transcript"]
        cycle_done = len(transcript)
        is_done = st.session_state.get(prefix+"is_done", False)

        st.progress(cycle_done/FIXED_CYCLES, text=f"대화 진행 중 ({cycle_done}/{FIXED_CYCLES})")

        if transcript:
            st.markdown("---")
            for turn in transcript:
                with st.chat_message("assistant"):
                    for p in turn["assistant_prompts"]:
                        st.markdown(p)
                with st.chat_message("user"):
                    if turn.get("probability_slider") is not None:
                        st.caption(f"📊 선택한 값: {turn['probability_slider']}%")
                    st.write(turn["user_response"])

        if not is_done:
            cur_mode = st.session_state[prefix+"cur_mode"]
            # ★ 몰입 차단 / 확률적 재구성 전략일 때 0~100% 슬라이더 표시
            needs_conf_slider = (group == "experimental"
                                 and cur_mode in ("Probability_Framing", "Immersion_Interruption"))

            with st.chat_message("assistant"):
                for p in st.session_state[prefix+"cur_prompts"]:
                    st.markdown(p)
                if needs_conf_slider:
                    st.markdown("**아래 슬라이더로 0~100% 값을 선택해 주세요.**")

            with st.form(f"chat_{cycle_done}", clear_on_submit=True):
                remaining = FIXED_CYCLES - cycle_done

                prob_val = None
                if needs_conf_slider:
                    slider_label = ("이 주장이 옳을 확률 (0~100%)"
                                    if cur_mode == "Probability_Framing"
                                    else "현재 이 주장에 대한 확신 수준 (0~100%)")
                    prob_val = st.slider(
                        slider_label, 0, 100, 50,
                        key=f"{prefix}prob_slider",
                    )

                user_resp = st.text_area(f"응답을 입력하세요 ({remaining}회 남음):", height=100)
                sent = st.form_submit_button("전송")

            if sent and user_resp.strip():
                cur_mode = st.session_state[prefix+"cur_mode"]

                # [#1,#3] 측정은 참가자 자유응답 텍스트만 사용. 슬라이더 값은 HI에 주입하지 않음.
                engine.Update_Claim(claim, cur_mode, user_resp)

                # [#2] 비일관성 산출용 — 각 턴의 '측정된' certainty를 기록 (양 집단 공통)
                state.history.append({"cycle": cycle_done+1, "mode": cur_mode,
                                      "user_response": user_resp,
                                      "claim_certainty": claim.certainty,
                                      "probability_slider": prob_val})

                # [#4] 메타인지 활성화 — 양 집단 동일하게 측정
                state.meta_cognitive_activation = evaluate_meta_cognitive(
                    cur_mode, user_resp, state.meta_cognitive_activation)

                metrics = engine.Calculate_HI(claim, state)
                state.hallucination_metrics = metrics
                state.cycle_count += 1
                transcript.append({
                    "cycle": cycle_done+1, "mode": cur_mode,
                    "assistant_prompts": st.session_state[prefix+"cur_prompts"],
                    "user_response": user_resp,
                    "probability_slider": prob_val,
                    "hallucination_metrics": dict(metrics),
                    "meta_cognitive_activation": dict(state.meta_cognitive_activation),
                })
                st.session_state[prefix+"transcript"] = transcript
                st.session_state[prefix+"state"] = state
                st.session_state[prefix+"claim"] = claim
                if cycle_done+1 >= FIXED_CYCLES:
                    state.stop_reason = f"{FIXED_CYCLES}회 완료"
                    st.session_state[prefix+"is_done"] = True
                else:
                    if group == "control":
                        st.session_state[prefix+"cur_prompts"] = generate_control_prompt(user_resp, transcript, task_key)
                        st.session_state[prefix+"cur_mode"] = "Control_Supportive"
                    else:
                        used = st.session_state[prefix+"used_modes"] + [cur_mode]
                        st.session_state[prefix+"used_modes"] = used
                        # ★ 적응형: 갱신된 claim·state 기준으로 다음 전략 선택
                        nxt, _ = engine.Select_Best_Strategy(claim, state, used)
                        st.session_state[prefix+"cur_mode"] = nxt
                        st.session_state[prefix+"cur_prompts"] = generate_experimental_prompt(nxt, user_resp, transcript, task_key)
                st.rerun()

        if is_done:
            st.success("과제가 완료되었습니다. 수고하셨습니다!")
            if st.button("사후 설문으로 이동 →", use_container_width=True, type="primary"):
                st.session_state.phase = "post_survey"
                st.rerun()

        render_withdraw_button("task")

# ============================================================
# UI 헬퍼
# ============================================================
def likert7(key, label):
    return st.slider(label, 1, 7, 4, key=key, help="1=전혀 그렇지 않다 / 7=매우 그렇다")

def hi_color(val):
    if val>=0.6: return "#E24B4A"
    elif val>=0.4: return "#EF9F27"
    elif val>=0.2: return "#639922"
    else: return "#1D9E75"

def metric_bar(label, value):
    pct=int(value*100); color=hi_color(value)
    st.markdown(f'<div style="margin-bottom:10px"><div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:3px"><span>{label}</span><span style="font-weight:500;color:{color}">{value:.3f}</span></div><div style="background:#e0e0e0;border-radius:4px;height:8px"><div style="width:{pct}%;background:{color};height:8px;border-radius:4px"></div></div></div>', unsafe_allow_html=True)

def show_metrics_panel(metrics, title="HI"):
    hi=metrics.get("Hallucination_Index",0); color=hi_color(hi)
    st.markdown(f'<div style="border:1px solid {color};border-radius:10px;padding:16px;margin:8px 0"><div style="font-size:13px;color:gray">{title}</div><div style="font-size:26px;font-weight:600;color:{color};margin:4px 0 10px">환각지수(Hallucination Index) = {hi:.3f}</div>', unsafe_allow_html=True)
    for k,l in [("Fuzzy_Unsupported_Claim","확신 대비 근거 부족(Unsupported Claim Risk)"),("Fuzzy_Source_Risk","출처 모호성(Source Risk)"),("Fuzzy_Affective_Risk","정서 기반 판단(Affective Risk)"),("Calibration_Error","확신-근거 불일치(Calibration Error)"),("Inconsistency","비일관된 판단(Inconsistency)")]:
        metric_bar(l, metrics.get(k,0))
    st.markdown("</div>", unsafe_allow_html=True)

# ============================================================
# 결과 데이터 매핑 (관리자 CSV + Google Sheets 공통)
# ============================================================
_SURVEY_ITEM_MAP = {
    # ── 사전 설문 (성향 척도) ──
    "ebr1": "[사전_근거1] 근거 중요성", "ebr2": "[사전_근거2] 데이터 기반", "ebr3": "[사전_근거3] 증거 확인", "ebr4": "[사전_근거4] 직관보다 근거", "ebr_mean": "[사전_근거_평균]",
    "aha1": "[사전_AI환각인식1] AI 틀린 정보", "aha2": "[사전_AI환각인식2] 무조건 신뢰 안함", "aha3": "[사전_AI환각인식3] 검증 필요", "aha4": "[사전_AI환각인식4] 사실과 다른 내용", "aha_mean": "[사전_AI환각인식_평균]",
    "oc1": "[사전_과신편향1] 이상/원칙 충성", "oc2": "[사전_과신편향2] 지지자 구분", "oc3": "[사전_과신편향3] 생각변화는 약함", "oc4": "[사전_과신편향4] 논리보다 느낌", "oc5": "[사전_과신편향5] 반대증거 무시", "oc_mean": "[사전_과신편향_평균]",
    "emo1": "[사전_정서중시1] 삶 방향 영향", "emo2": "[사전_정서중시2] 삶을 흥미롭게", "emo3": "[사전_정서중시3] 느끼는것이 건강", "emo4": "[사전_정서중시4] 감정 통해 배움", "emo_mean": "[사전_정서중시_평균]",
    # ── 사전 설문 (메타인지 baseline, 현재형) ──
    "psd1": "[사전_거리1] 제3자 시각", "psd2": "[사전_거리2] 거리감 평가", "psd3": "[사전_거리3] 감정/판단 분리", "psd4": "[사전_거리4] 객관적 검토", "psd_mean": "[사전] 거리두기 평균",
    "psm1": "[사전_현실1] 출처 확인", "psm2": "[사전_현실2] 사실/추론 구분", "psm3": "[사전_현실3] 사실/상상 구별", "psm4": "[사전_현실4] 근거출처 성찰", "psm_mean": "[사전] 현실모니터링 평균",
    "pcf1": "[사전_반사실1] 틀릴 수 있음", "pcf2": "[사전_반사실2] 다른 정답", "pcf3": "[사전_반사실3] 다양한 해석", "pcf4": "[사전_반사실4] 확신오류 없음", "pcf_mean": "[사전] 반사실적사고 평균",
    "pih1": "[사전_지겸1] 학습 필요성", "pih2": "[사전_지겸2] 오류 인정", "pih3": "[사전_지겸3] 수정 의향", "pih4": "[사전_지겸4] 경청 의지", "pih_mean": "[사전] 지적겸손 평균",
    "llm_halluc": "[사전] LLM환각인식(단일)",
    # ── 사후 설문 ──
    "bae1": "[사후_소외1] 낯설게 바라봄", "bae2": "[사후_소외2] 떨어져서 봄", "bae3": "[사후_소외3] 비판적 검토", "bae4": "[사후_소외4] 자동적반응 멈춤", "bae_mean": "[사후] 소외효과 조작점검 평균",
    "mc1": "[사후_메타1] 목표 부합 점검", "mc2": "[사후_메타2] 오류 인식", "mc3": "[사후_메타3] 옵션 점검", "mc4": "[사후_메타4] 타당성 자문", "mc_mean": "[사후] 메타인지(전반) 평균",
    "sd1": "[사후_거리1] 제3자 시각", "sd2": "[사후_거리2] 거리감 유지", "sd3": "[사후_거리3] 감정/판단 분리", "sd4": "[사후_거리4] 객관적 검토", "sd_mean": "[사후] 메타인지(거리두기) 평균",
    "sm1": "[사후_출처1] 출처 확인", "sm2": "[사후_출처2] 사실/추론 구분", "sm3": "[사후_출처3] 사실/상상 구별", "sm4": "[사후_출처4] 근거출처 성찰", "sm_mean": "[사후] 메타인지(출처모니터링) 평균",
    "cf1": "[사후_반사실1] 틀릴 수 있음", "cf2": "[사후_반사실2] 다른 정답 인지", "cf3": "[사후_반사실3] 다양한 해석", "cf4": "[사후_반사실4] 확신오류 감소", "cf_mean": "[사후] 메타인지(반사실적사고) 평균",
    "ih1": "[사후_지적겸손1] 학습 필요성", "ih2": "[사후_지적겸손2] 오류 인정", "ih3": "[사후_지적겸손3] 수정 의향", "ih4": "[사후_지적겸손4] 경청 의지", "ih_mean": "[사후] 메타인지(지적겸손) 평균",
    "lr1": "[사후_환각_근거부족1] 결론 인지", "lr2": "[사후_환각_근거부족2] 확신 인지", "lr3": "[사후_환각_근거부족3] 제시 미흡", "lr4": "[사후_환각_근거부족4] 설명 없음", "lr5": "[사후_환각_근거부족5] 점검 미흡", "lr_mean": "[사후] 자기보고 환각(근거부족) 평균",
    "lf1": "[사후_환각_출처모호1] 불명확", "lf2": "[사후_환각_출처모호2] 혼동", "lf3": "[사후_환각_출처모호3] 미확인", "lf4": "[사후_환각_출처모호4] 검증 미흡", "lf_mean": "[사후] 자기보고 환각(출처모호) 평균",
    "ah1": "[사후_환각_정서판단1] 판단 변화", "ah2": "[사후_환각_정서판단2] 선호 확신", "ah3": "[사후_환각_정서판단3] 판단 인지", "ah4": "[사후_환각_정서판단4] 기분 영향", "ah_mean": "[사후] 자기보고 환각(정서판단) 평균",
    "ic1": "[사후_환각_비일관성1] 판단 변화", "ic2": "[사후_환각_비일관성2] 다른 결론", "ic3": "[사후_환각_비일관성3] 기준 유지", "ic4": "[사후_환각_비일관성4] 기준 변화", "ic_mean": "[사후] 자기보고 환각(비일관성) 평균",
    "ci1": "[사후_공동창출의향1] 적극 상호작용", "ci2": "[사후_공동창출의향2] 학습 시도", "ci3": "[사후_공동창출의향3] 추가정보 제공", "ci4": "[사후_공동창출의향4] 결과 수정개선", "ci5": "[사후_공동창출의향5] 시간노력 투자", "ci6": "[사후_공동창출의향6] 공동 발전", "ci7": "[사후_공동창출의향7] 문제해결 참여", "ci_mean": "[사후] 공동창출 의향 평균",
    "ce1": "[사후_공동창출효과1] 유용한 피드백", "ce2": "[사후_공동창출효과2] 명확한 표현", "ce3": "[사후_공동창출효과3] 방안 탐색", "ce4": "[사후_공동창출효과4] 적극 반응", "ce5": "[사후_공동창출효과5] 좋은 해결책", "ce6": "[사후_공동창출효과6] 반복 수정발전", "ce7": "[사후_공동창출효과7] 더 나은 결과", "ce_mean": "[사후] 공동창출 효과 평균",
}

_SYSTEM_LEAF_MAP = {
    "participant_id": "[시스템] 참가자_ID", "timestamp": "[시스템] 참여_일시",
    "cell": "[시스템] 배정_셀", "group": "[시스템] 실험집단", "task_type": "[시스템] 과제유형",
    "perceived_task_type": "[사후] 지각된 과제유형",
    "design": "[시스템] 실험_설계", "fixed_cycles": "[시스템] 고정_대화_턴수",
    "api_model": "[시스템] 사용_모델", "task_order": "[시스템] 과제_수행_순서",
    "total_cycles": "[시스템] 진행된_대화_턴수",
    "gender": "[사전] 성별", "age_group": "[사전] 연령대",
    "initial_input": "[과제] 최초 주장 내용", "hi_change": "[분석] 환각지수 감소량",
    "transcript": "[로그] 대화 전체 기록",
    "final_title": "[최종] 결과물 제목", "final_reason": "[최종] 판단 이유",
    "reflection": "[최종] 주관적 성찰 기록", "overall_satisfaction": "[결과] 전체 대화 만족도",
    "creativity_index": "[결과] 창의성 종합지수",
    "cognitive_distance": "[알고리즘_메타인지] 인지적 거리두기",
    "counterfactual_simulation": "[알고리즘_메타인지] 반사실적 사고",
    "epistemic_humility": "[알고리즘_메타인지] 지적 겸손",
    "reality_monitoring": "[알고리즘_메타인지] 현실 모니터링",
}

def get_korean_name(key_path):
    key_str = str(key_path).lower()
    last_part = key_str.split('.')[-1]
    # ★ 개별 설문 문항은 정확 키로 먼저 매핑 (광범위 부분문자열 검사보다 우선 → sd1~4 등 충돌 방지)
    if last_part in _SURVEY_ITEM_MAP:
        return _SURVEY_ITEM_MAP[last_part]
    if last_part in _SYSTEM_LEAF_MAP:
        return _SYSTEM_LEAF_MAP[last_part]
    # ── 경로 맥락이 필요한 항목 (잎 이름만으론 구분 불가) ──
    if "creativity" in key_str and "fit" in key_str: return "[결과] 적합성"
    if "creativity" in key_str and "original" in key_str: return "[결과] 독창성"
    if "creativity" in key_str and "useful" in key_str: return "[결과] 유용성"
    if "confidence" in key_str and "change" in key_str: return "[분석] 확신도 변화량(보조)"
    if "confidence" in key_str and "pre" in key_str: return "[실험] 사전 확신도(보조)"
    if "confidence" in key_str and "post" in key_str: return "[실험] 사후 확신도(보조)"
    if "initial_hi" in key_str:
        if "hallucination_index" in key_str: return "[알고리즘] 초기 환각지수(HI)"
        if "calibration_error" in key_str: return "[알고리즘_초기] 확신-근거 불일치"
        if "affective_risk" in key_str: return "[알고리즘_초기] 정서 기반 판단"
        if "source_risk" in key_str: return "[알고리즘_초기] 출처 모호성"
        if "unsupported_claim" in key_str: return "[알고리즘_초기] 확신 대비 근거 부족"
        if "inconsistency" in key_str: return "[알고리즘_초기] 비일관성"
    if "final_hi" in key_str:
        if "hallucination_index" in key_str: return "[알고리즘] 최종 환각지수(HI)"
        if "calibration_error" in key_str: return "[알고리즘_최종] 확신-근거 불일치"
        if "affective_risk" in key_str: return "[알고리즘_최종] 정서 기반 판단"
        if "source_risk" in key_str: return "[알고리즘_최종] 출처 모호성"
        if "unsupported_claim" in key_str: return "[알고리즘_최종] 확신 대비 근거 부족"
        if "inconsistency" in key_str: return "[알고리즘_최종] 비일관성"
    return f"[미확인] {last_part}"

# ============================================================
# 표준 칼럼 순서 (CANONICAL) — 설문 화면 제시 순서와 1:1 일치
# flatten_result_full(result) 가 생성하는 151개 키의 정확한 순열.
# (중복 0 / 누락 0 / 발명 0 — 검증 완료)
# CSV·구글시트 양쪽이 모두 이 순서를 단일 기준으로 사용한다.
# ============================================================
CANONICAL_COLUMNS = [
    "[시스템] 참가자_ID", "[시스템] 참여_일시", "[시스템] 배정_셀", "[시스템] 실험집단", "[시스템] 과제유형", "[시스템] 사용_모델", "[시스템] 실험_설계", "[시스템] 고정_대화_턴수",
    "[사전] 성별", "[사전] 연령대",
    "[사전_근거1] 근거 중요성", "[사전_근거2] 데이터 기반", "[사전_근거3] 증거 확인", "[사전_근거4] 직관보다 근거", "[사전_근거_평균]",
    "[사전_AI환각인식1] AI 틀린 정보", "[사전_AI환각인식2] 무조건 신뢰 안함", "[사전_AI환각인식3] 검증 필요", "[사전_AI환각인식4] 사실과 다른 내용", "[사전_AI환각인식_평균]",
    "[사전_과신편향1] 이상/원칙 충성", "[사전_과신편향2] 지지자 구분", "[사전_과신편향3] 생각변화는 약함", "[사전_과신편향4] 논리보다 느낌", "[사전_과신편향5] 반대증거 무시", "[사전_과신편향_평균]",
    "[사전_정서중시1] 삶 방향 영향", "[사전_정서중시2] 삶을 흥미롭게", "[사전_정서중시3] 느끼는것이 건강", "[사전_정서중시4] 감정 통해 배움", "[사전_정서중시_평균]",
    "[사전_거리1] 제3자 시각", "[사전_거리2] 거리감 평가", "[사전_거리3] 감정/판단 분리", "[사전_거리4] 객관적 검토", "[사전] 거리두기 평균",
    "[사전_현실1] 출처 확인", "[사전_현실2] 사실/추론 구분", "[사전_현실3] 사실/상상 구별", "[사전_현실4] 근거출처 성찰", "[사전] 현실모니터링 평균",
    "[사전_반사실1] 틀릴 수 있음", "[사전_반사실2] 다른 정답", "[사전_반사실3] 다양한 해석", "[사전_반사실4] 확신오류 없음", "[사전] 반사실적사고 평균",
    "[사전_지겸1] 학습 필요성", "[사전_지겸2] 오류 인정", "[사전_지겸3] 수정 의향", "[사전_지겸4] 경청 의지", "[사전] 지적겸손 평균",
    "[사전] LLM환각인식(단일)",
    "[과제] 최초 주장 내용", "[실험] 사전 확신도(보조)", "[알고리즘] 초기 환각지수(HI)",
    "[사후] 지각된 과제유형",
    "[사후_소외1] 낯설게 바라봄", "[사후_소외2] 떨어져서 봄", "[사후_소외3] 비판적 검토", "[사후_소외4] 자동적반응 멈춤", "[사후] 소외효과 조작점검 평균",
    "[사후_메타1] 목표 부합 점검", "[사후_메타2] 오류 인식", "[사후_메타3] 옵션 점검", "[사후_메타4] 타당성 자문", "[사후] 메타인지(전반) 평균",
    "[사후_환각_근거부족1] 결론 인지", "[사후_환각_근거부족2] 확신 인지", "[사후_환각_근거부족3] 제시 미흡", "[사후_환각_근거부족4] 설명 없음", "[사후_환각_근거부족5] 점검 미흡", "[사후] 자기보고 환각(근거부족) 평균",
    "[사후_환각_출처모호1] 불명확", "[사후_환각_출처모호2] 혼동", "[사후_환각_출처모호3] 미확인", "[사후_환각_출처모호4] 검증 미흡", "[사후] 자기보고 환각(출처모호) 평균",
    "[사후_환각_정서판단1] 판단 변화", "[사후_환각_정서판단2] 선호 확신", "[사후_환각_정서판단3] 판단 인지", "[사후_환각_정서판단4] 기분 영향", "[사후] 자기보고 환각(정서판단) 평균",
    "[사후_환각_비일관성1] 판단 변화", "[사후_환각_비일관성2] 다른 결론", "[사후_환각_비일관성3] 기준 유지", "[사후_환각_비일관성4] 기준 변화", "[사후] 자기보고 환각(비일관성) 평균",
    "[사후_거리1] 제3자 시각", "[사후_거리2] 거리감 유지", "[사후_거리3] 감정/판단 분리", "[사후_거리4] 객관적 검토", "[사후] 메타인지(거리두기) 평균",
    "[사후_출처1] 출처 확인", "[사후_출처2] 사실/추론 구분", "[사후_출처3] 사실/상상 구별", "[사후_출처4] 근거출처 성찰", "[사후] 메타인지(출처모니터링) 평균",
    "[사후_반사실1] 틀릴 수 있음", "[사후_반사실2] 다른 정답 인지", "[사후_반사실3] 다양한 해석", "[사후_반사실4] 확신오류 감소", "[사후] 메타인지(반사실적사고) 평균",
    "[사후_지적겸손1] 학습 필요성", "[사후_지적겸손2] 오류 인정", "[사후_지적겸손3] 수정 의향", "[사후_지적겸손4] 경청 의지", "[사후] 메타인지(지적겸손) 평균",
    "[사후_공동창출의향1] 적극 상호작용", "[사후_공동창출의향2] 학습 시도", "[사후_공동창출의향3] 추가정보 제공", "[사후_공동창출의향4] 결과 수정개선", "[사후_공동창출의향5] 시간노력 투자", "[사후_공동창출의향6] 공동 발전", "[사후_공동창출의향7] 문제해결 참여", "[사후] 공동창출 의향 평균",
    "[사후_공동창출효과1] 유용한 피드백", "[사후_공동창출효과2] 명확한 표현", "[사후_공동창출효과3] 방안 탐색", "[사후_공동창출효과4] 적극 반응", "[사후_공동창출효과5] 좋은 해결책", "[사후_공동창출효과6] 반복 수정발전", "[사후_공동창출효과7] 더 나은 결과", "[사후] 공동창출 효과 평균",
    "[실험] 사후 확신도(보조)", "[결과] 독창성", "[결과] 유용성", "[결과] 적합성", "[결과] 창의성 종합지수",
    "[최종] 결과물 제목", "[최종] 판단 이유", "[결과] 전체 대화 만족도", "[최종] 주관적 성찰 기록",
    "[분석] 환각지수 감소량", "[분석] 확신도 변화량(보조)", "[알고리즘] 최종 환각지수(HI)",
    "[알고리즘_초기] 확신 대비 근거 부족", "[알고리즘_초기] 출처 모호성", "[알고리즘_초기] 정서 기반 판단", "[알고리즘_초기] 확신-근거 불일치", "[알고리즘_초기] 비일관성",
    "[알고리즘_최종] 확신 대비 근거 부족", "[알고리즘_최종] 출처 모호성", "[알고리즘_최종] 정서 기반 판단", "[알고리즘_최종] 확신-근거 불일치", "[알고리즘_최종] 비일관성",
    "[알고리즘_메타인지] 인지적 거리두기", "[알고리즘_메타인지] 현실 모니터링", "[알고리즘_메타인지] 반사실적 사고", "[알고리즘_메타인지] 지적 겸손",
    "[시스템] 진행된_대화_턴수", "[로그] 대화 전체 기록",
]

def flatten_result_full(d, prefix=""):
    """result dict를 전체 칼럼 매핑으로 평탄화."""
    items = {}
    for k, v in d.items():
        full_path = f"{prefix}{k}" if prefix else k
        if isinstance(v, dict):
            items.update(flatten_result_full(v, full_path + "."))
        else:
            mapped_key = get_korean_name(full_path)
            items[mapped_key] = json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
    return items

# ============================================================
# 결과 저장 헬퍼 — CSV·구글시트 공통 (단일 기준: CANONICAL_COLUMNS)
# ============================================================
def _cell_str(v):
    """셀 문자열화 (None→'')."""
    return "" if v is None else str(v)

def _row_dict_from_result(result):
    """result → {한글칼럼명: 값} 평탄화. 관리자 사전설문 우회 흔적은 제거."""
    flat = flatten_result_full(result)
    flat.pop("[미확인] admin_skip", None)
    return flat

def _desired_header(flat, old_header):
    """무손실 헤더 = CANONICAL + (flat의 신규 키) + (구헤더에만 있던 키).
    어떤 값도 버려지지 않도록 모든 키를 보존하되, 표준 칼럼을 앞에 둔다."""
    header = list(CANONICAL_COLUMNS)
    seen = set(header)
    for k in flat.keys():           # 표준에 없는 신규 키 보존
        if k and k not in seen:
            seen.add(k); header.append(k)
    for k in old_header:            # 과거 헤더에만 있던 키도 보존
        if k and k not in seen:
            seen.add(k); header.append(k)
    return header

def _save_row_to_gsheet(result):
    """구글시트 results 탭에 1행 추가.
    기존 헤더가 표준과 다르면(구버전) 기존 데이터를 '칼럼명 기준'으로
    새 순서에 무손실 재배치한 뒤 전체를 다시 기록한다.
    - clear() 없이 덮어쓰기 → 중간 실패 시에도 데이터 공백이 생기지 않음
    - value_input_option 미지정(기본 RAW) → 일시 문자열이 날짜로 변환되지 않음
    """
    from gspread.utils import rowcol_to_a1
    ws = _get_worksheet("results")
    if not ws:
        return
    flat = _row_dict_from_result(result)
    existing = ws.get_all_values()
    has_header = bool(existing) and any(c.strip() for c in existing[0])
    old_header = existing[0] if has_header else []
    desired = _desired_header(flat, old_header)
    new_row = [_cell_str(flat.get(col, "")) for col in desired]

    # 시트 폭 확보 (절대 축소되지 않도록 하한 적용)
    try:
        ws.resize(rows=max(len(existing) + 10, 1000), cols=max(len(desired) + 5, 26))
    except Exception:
        pass

    # 헤더가 이미 표준과 동일 → 단순 추가
    if has_header and old_header == desired:
        ws.append_row(new_row)
        return

    # 헤더가 다르거나 없음 → 기존 행을 칼럼명 기준으로 재매핑하여 전체 재기록
    migrated = [desired]
    for row in existing[1:]:
        if not any(c.strip() for c in row):
            continue  # 빈 행 건너뜀
        rowmap = {old_header[i]: row[i] for i in range(min(len(old_header), len(row)))}
        migrated.append([_cell_str(rowmap.get(col, "")) for col in desired])
    migrated.append(new_row)

    end = rowcol_to_a1(len(migrated), len(desired))
    ws.update(f"A1:{end}", migrated)

# ============================================================
# 호스트 관리 패널
# ============================================================
def render_host_panel():
    with st.sidebar:
        st.markdown("---")
        st.markdown("#### 🔒 관리자 모드")
        if not st.session_state.get("host_auth", False):
            pwd = st.text_input("비밀번호", type="password", key="host_pwd")
            if st.button("인증", key="host_auth_btn"):
                if pwd == HOST_PASSWORD:
                    st.session_state.host_auth = True; st.rerun()
                else: st.error("비밀번호가 틀립니다.")
            return
        st.success("관리자 인증 완료")
        if st.button("로그아웃", key="host_logout"):
            st.session_state.host_auth = False; st.rerun()

        # 셀 현황
        st.markdown("---")
        st.markdown("##### 📊 셀 인원 현황")
        counts = get_cell_status()
        for k, info in CELLS.items():
            cnt = counts.get(k, 0)
            color = "#E24B4A" if cnt >= MAX_PER_CELL else "#639922"
            st.markdown(f"셀{k} [{info['label']}]: <span style='color:{color};font-weight:600'>{cnt}/{MAX_PER_CELL}</span>", unsafe_allow_html=True)

        # 관리자 셀 직접 선택
        st.markdown("---")
        st.markdown("##### 🎮 실험 제어")
        cell_choice = st.selectbox("셀 직접 선택", list(CELLS.keys()),
            format_func=lambda k: f"셀{k}: {CELLS[k]['label']}", key="host_cell")
        if st.button("이 셀로 시작 ⏭️", key="host_start_cell", use_container_width=True):
            info = CELLS[cell_choice]
            for k in [kk for kk in st.session_state if kk.startswith("t_")]:
                del st.session_state[k]            # ★ 이전 과제 상태 초기화 (다른 셀 전환 시 잔존 방지)
            st.session_state.participant_id = "ADMIN_" + uuid.uuid4().hex[:6].upper()
            st.session_state.cell = cell_choice
            st.session_state.group = info["group"]
            st.session_state.task_key = info["task"]
            st.session_state.engine = BMDMEngine()
            st.session_state.phase = "task"
            st.rerun()

        # 단계 건너뛰기
        engine_ref = BMDMEngine()
        all_steps = [("intro","인트로"),("consent","IRB 동의서"),("pre_survey","사전설문"),("task_init","과제—초기입력")]
        for i, s in enumerate(engine_ref.ALL_STRATEGIES):
            all_steps.append((f"task_c{i+1}", f"과제—{engine_ref.STEP_LABELS.get(s,s)}"))
        all_steps += [("post_survey","사후설문"),("done","완료")]
        step_keys = [s[0] for s in all_steps]
        step_labels = [s[1] for s in all_steps]

        sel = st.selectbox("구간 건너뛰기", range(len(all_steps)),
                           format_func=lambda i: step_labels[i], key="host_skip_sel")
        if st.button("이 구간으로 ⏭️", key="host_skip_go", use_container_width=True):
            target = step_keys[sel]
            if not st.session_state.get("participant_id"):
                info = CELLS[st.session_state.get("cell", cell_choice)]
                st.session_state.participant_id = "ADMIN_" + uuid.uuid4().hex[:6].upper()
                st.session_state.cell = st.session_state.get("cell", cell_choice)
                st.session_state.group = info["group"]
                st.session_state.task_key = info["task"]
                st.session_state.engine = BMDMEngine()
                if not st.session_state.get("pre_survey_data"):
                    st.session_state.pre_survey_data = {"admin_skip": True}
            if target in ("intro","consent","pre_survey","post_survey","done"):
                if target in ("post_survey","done"):
                    _ensure_task_done()
                st.session_state.phase = target
            elif target == "task_init":
                for k in [kk for kk in st.session_state if kk.startswith("t_")]:
                    del st.session_state[k]
                st.session_state.phase = "task"
            elif target.startswith("task_c"):
                cyc = int(target.split("c")[1])
                for k in [kk for kk in st.session_state if kk.startswith("t_")]:
                    del st.session_state[k]        # ★ 잔존 위젯/상태 초기화 후 해당 사이클 재구성
                _setup_task_at_cycle(cyc)
                st.session_state.phase = "task"
            st.rerun()

        # 초기화
        st.markdown("")
        if st.button("🔄 세션만 초기화 (현재 화면)", key="host_reset", use_container_width=True):
            keep = {"host_auth"}
            for k in list(st.session_state.keys()):
                if k not in keep: del st.session_state[k]
            st.session_state.phase = "intro"; st.rerun()

        st.markdown("---")
        st.markdown("##### ⚠️ 데이터 초기화 (영구)")
        st.caption("셀 카운트·수집 데이터는 세션 초기화로 안 지워집니다. 아래로 영구 삭제하세요.")
        confirm = st.checkbox("초기화에 동의합니다 (되돌릴 수 없음)", key="host_reset_confirm")

        if st.button("🧮 셀 카운트만 0으로 초기화", key="host_reset_counts",
                     use_container_width=True, disabled=not confirm):
            reset_cell_counts()
            st.success("셀 카운트를 모두 0으로 초기화했습니다.")
            st.rerun()

        if st.button("🗑️ 셀 카운트 + 수집 응답 전체 삭제", key="host_reset_all_data",
                     use_container_width=True, disabled=not confirm):
            reset_cell_counts()
            reset_all_results()
            st.success("셀 카운트와 수집된 모든 응답(구글시트 results 탭 + 로컬 파일)을 삭제했습니다.")
            st.rerun()

        # 현재 세션 모니터링
        st.markdown("---")
        st.markdown("##### 📋 현재 세션")
        if st.session_state.get("participant_id"):
            st.caption(f"ID: {st.session_state.participant_id}")
            st.caption(f"셀: {st.session_state.get('cell','?')} ({CELLS.get(st.session_state.get('cell',''),'?').get('label','') if isinstance(CELLS.get(st.session_state.get('cell',''),'?'), dict) else '?'})")
            state_obj = st.session_state.get("t_state")
            if state_obj and state_obj.hallucination_metrics:
                with st.expander("환각지수(HI) 상세", expanded=False):
                    show_metrics_panel(state_obj.hallucination_metrics)
                    mca = state_obj.meta_cognitive_activation
                    st.markdown("**메타인지 활성화(Meta-Cognitive Activation)**")
                    MCA_LABELS = {
                        "cognitive_distance": "인지적 거리두기(self-distancing)",
                        "reality_monitoring": "현실 모니터링(source monitoring)",
                        "counterfactual_simulation": "반사실적 사고(counterfactual thinking)",
                        "epistemic_humility": "지적 겸손(intellectual humility)"
                    }
                    for k,v in mca.items():
                        metric_bar(MCA_LABELS.get(k, k), v)

        # 저장된 결과
        st.markdown("---")
        st.markdown("##### 📁 결과 파일")
        files = sorted(glob.glob("results/*.json"), reverse=True)
        if files:
            st.caption(f"총 {len(files)}건")
            sf = st.selectbox("파일", files, key="host_file")
            if sf:
                with open(sf,"r",encoding="utf-8") as f: data=json.load(f)
                st.json(data, expanded=False)
                st.download_button("다운로드", json.dumps(data,ensure_ascii=False,indent=2),
                                   os.path.basename(sf),"application/json",key="host_dl")
            if len(files)>1:
                all_r = [json.load(open(fp,"r",encoding="utf-8")) for fp in files]
                st.download_button(f"전체 JSON ({len(all_r)}건)",
                                   json.dumps(all_r,ensure_ascii=False,indent=2),
                                   "all_results.json","application/json",key="host_dl_all")
            # CSV 다운로드
            if files:
                all_r = [json.load(open(fp, "r", encoding="utf-8")) for fp in files]
                processed_data = [_row_dict_from_result(r) for r in all_r]

                if processed_data:
                    import io, csv
                    output = io.StringIO()

                    # 저장 경로와 동일한 표준 순서(CANONICAL) + 행에만 있는 신규 키 보존 → 무손실
                    final_columns = list(CANONICAL_COLUMNS)
                    seen = set(final_columns)
                    for row in processed_data:
                        for k in row.keys():
                            if k and k not in seen:
                                seen.add(k); final_columns.append(k)

                    writer = csv.DictWriter(output, fieldnames=final_columns, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(processed_data)

                    st.download_button(
                        label="📊 전체 데이터 CSV 다운로드",
                        data=output.getvalue().encode('utf-8-sig'),
                        file_name="final_all_results_ordered.csv",
                        mime="text/csv",
                        key="host_dl_csv_final"
                    )


def _ensure_task_done():
    if st.session_state.get("t_is_done"): return
    engine = st.session_state.get("engine", BMDMEngine())
    claim = Claim(content="(관리자 건너뛰기)")
    state = ConversationState()
    state.hallucination_metrics = engine.Calculate_HI(claim, state)
    state.stop_reason = "관리자 건너뛰기"
    st.session_state["t_initial_input"] = "(더미)"
    st.session_state["t_pre_confidence"] = 50
    st.session_state["t_claim"] = claim
    st.session_state["t_state"] = state
    st.session_state["t_initial_metrics"] = state.hallucination_metrics
    st.session_state["t_used_modes"] = []
    st.session_state["t_transcript"] = []
    st.session_state["t_is_done"] = True

def _setup_task_at_cycle(target_cycle):
    engine = st.session_state.get("engine", BMDMEngine())
    group = st.session_state.get("group","experimental")
    task_key = st.session_state.get("task_key","creative")
    claim = Claim(content="(관리자 건너뛰기)")
    state = ConversationState()
    state.hallucination_metrics = engine.Calculate_HI(claim, state)
    st.session_state["t_initial_input"] = "(더미)"
    st.session_state["t_pre_confidence"] = 50
    st.session_state["t_claim"] = claim
    st.session_state["t_state"] = state
    st.session_state["t_initial_metrics"] = state.hallucination_metrics
    transcript = []
    used = []
    for c in range(target_cycle - 1):
        mode = engine.ALL_STRATEGIES[c] if c < len(engine.ALL_STRATEGIES) else engine.ALL_STRATEGIES[-1]
        used.append(mode)
        transcript.append({"cycle":c+1,"mode":mode,"assistant_prompts":MANDATORY_PROMPTS.get(mode,["(더미)"]),
                           "user_response":"(건너뛰기)","hallucination_metrics":{},"meta_cognitive_activation":{}})
        state.cycle_count += 1
    st.session_state["t_transcript"] = transcript
    st.session_state["t_used_modes"] = used
    st.session_state["t_is_done"] = False
    ci = target_cycle - 1
    cur = engine.ALL_STRATEGIES[ci] if ci < len(engine.ALL_STRATEGIES) else engine.ALL_STRATEGIES[-1]
    if group == "experimental":
        st.session_state["t_cur_mode"] = cur
        st.session_state["t_cur_prompts"] = MANDATORY_PROMPTS.get(cur, ["검토해주세요."])
    else:
        st.session_state["t_cur_mode"] = "Control_Supportive"
        st.session_state["t_cur_prompts"] = ["계속 작성해 주세요."]

# ============================================================
# 메인 라우팅
# ============================================================
st.set_page_config(page_title="BMDM 실험", page_icon="🎭", layout="centered")

def init_session():
    for k,v in {"phase":"intro","host_auth":False}.items():
        if k not in st.session_state: st.session_state[k] = v

init_session()
render_host_panel()

# 관리자 배너
if st.session_state.get("host_auth"):
    g = st.session_state.get("group","?")
    c = st.session_state.get("cell","?")
    p = st.session_state.get("phase","intro")
    st.markdown(f'<div style="background:#1a1a1a;border:1px solid #444;border-radius:8px;padding:8px 14px;margin-bottom:12px;font-size:13px;color:#fff;">🔧 <b>관리자</b> — 셀{c} | {p}</div>', unsafe_allow_html=True)

# 참여 종료 — 사이드바 버튼
if st.session_state.get("phase") in ("pre_survey", "task", "post_survey", "consent"):
    with st.sidebar:
        st.markdown("---")
        if st.button("🚪 참여 종료", key="withdraw_btn", use_container_width=True):
            st.session_state.phase = "withdrawn"; st.rerun()

# 참여 종료 — 각 페이지 하단 우측 작은 링크
def render_withdraw_button(key_suffix=""):
    st.markdown("")
    col_left, col_right = st.columns([4, 1])
    with col_right:
        if st.button("연구참여 종료", key=f"withdraw_{key_suffix}", type="secondary"):
            st.session_state.phase = "withdrawn"; st.rerun()

# ── PHASE 0: 인트로 ──
if st.session_state.phase == "intro":
    st.title("🎭 AI 공동 창작 실험")
    st.markdown(f"""
이 실험은 **AI와 함께 글을 작성하는 과정**을 연구하기 위한 것입니다.

- 소요 시간: 약 **10분**
- **1가지 과제**를 AI와 함께 수행합니다
- AI와 **{FIXED_CYCLES}회** 대화를 진행합니다
- 모든 응답은 연구 목적으로만 익명 활용됩니다
    """)
    if st.button("실험 시작하기", use_container_width=True, type="primary"):
        st.session_state.phase = "consent"; st.rerun()

# ── PHASE 0.5: IRB 동의서 ──
elif st.session_state.phase == "consent":
    st.title("📄 연구 참여 안내 및 동의서")
    st.markdown("""
안녕하세요.

본 연구는 생성형 인공지능(Generative AI)과 인간의 상호작용 과정에서 나타나는 판단 방식, 사고 과정, 그리고 인지적 특성(예: 근거 평가, 관점 전환, 판단 점검 등)을 분석하기 위한 학술 연구입니다.

특히 본 연구는 인간–AI 협업 과정에서 나타나는 정보 해석 방식, 판단의 변화, 그리고 메타인지적 사고 과정을 이해하고, 이를 통해 보다 효과적인 인간–AI 상호작용 구조를 탐색하는 것을 목적으로 합니다.

---

**✔ 연구 목적**

본 연구의 목적은 생성형 AI와의 상호작용 과정에서 나타나는 개인의 판단 방식, 근거 기반 사고, 메타인지(자기 사고 점검 및 조절 능력)의 변화를 실증적으로 분석하는 것입니다.

**✔ 연구 절차**

본 설문은 약 5~10분 정도 소요됩니다. 일부 문항에서는 자신의 생각을 설명하거나, 판단의 근거를 되돌아보거나, 다른 가능성을 고려하는 질문이 포함될 수 있습니다.
이는 특정 정답을 요구하는 것이 아니라, 개인의 자연스러운 사고 과정을 이해하기 위한 것입니다.

**✔ 예상 위험 및 불편**

본 연구는 일상적인 설문 수준의 최소 위험(minimal risk) 연구입니다. 다만 일부 문항에서 자신의 판단을 재검토하는 과정에서 경미한 인지적 부담 또는 일시적인 불편감을 느낄 수 있습니다.
이러한 경우 언제든지 응답을 중단할 수 있습니다.

**✔ 참여의 자발성**

본 연구 참여는 전적으로 자발적입니다.
참여하지 않거나, 참여 도중 언제든지 중단할 수 있으며, 이에 따른 어떠한 불이익도 발생하지 않습니다.

**✔ 개인정보 보호 및 익명성**

본 설문은 완전 익명으로 진행되며, 개인을 식별할 수 있는 정보는 수집하지 않습니다.
모든 응답은 통계적으로 처리되며, 연구 목적 외에는 사용되지 않습니다.
연구 결과는 학술 논문 및 학회 발표 등에 활용될 수 있으나, 개인을 식별할 수 있는 정보는 공개되지 않습니다.

**✔ 자료 보관 및 폐기**

수집된 자료는 연구책임자의 책임 하에 안전하게 관리됩니다.
연구 종료 후 일정 기간 보관 후 완전히 폐기됩니다.

**✔ 문의처**

본 연구와 관련하여 문의사항이 있으신 경우 아래로 연락해 주시기 바랍니다.

- 연구책임자: 권오병 교수
- 소속: 경희대학교 경영학과, 빅데이터응용학과, 차세대정보기술연구센터(CAITECH)
- 이메일: obkwon@khu.ac.kr

또한 연구 참여와 관련된 권리 보호에 관한 문의는 소속 기관의 생명윤리심의위원회(IRB)로 문의하실 수 있습니다.
경희대학교 서울캠퍼스 생명윤리심의위원회 02-961-2342

---

**✔ 연구 참여 동의**

아래 문항에 응답함으로써, 귀하는 위 내용을 충분히 이해하였으며 자발적으로 연구 참여에 동의한 것으로 간주됩니다.
    """)

    consent = st.radio(
        "연구 참여에 동의하십니까?",
        ["선택해 주세요", "동의함", "동의하지 않음"],
        index=0, key="irb_consent"
    )

    if consent == "동의함":
        if st.button("다음으로 진행", use_container_width=True, type="primary"):
            st.session_state.phase = "pre_survey"; st.rerun()
    elif consent == "동의하지 않음":
        st.warning("연구 참여에 동의하지 않으셨습니다. 참여해 주셔서 감사합니다.")
        st.markdown("브라우저를 닫으시면 됩니다.")

    render_withdraw_button("consent")

# ── PHASE 1: 사전 설문 (첨부 설문 PDF 기준) ──
elif st.session_state.phase == "pre_survey":
    st.title("📋 사전 설문")
    st.caption("모든 정보는 익명 처리됩니다.")
    with st.form("pre_form"):
        st.markdown("#### 기본 정보")
        col1, col2 = st.columns(2)
        gender = col1.selectbox("성별", ["남성","여성","기타","응답 거부"])
        age_group = col2.selectbox("연령대", ["20대","30대","40대","50대 이상"])
        # 포함기준(논문 4.1: 최근 6개월 내 GenAI 사용 경험) 스크리닝 — 설문 PDF 외 유지
        ai_6mo = st.selectbox("최근 6개월 내 생성형 AI(ChatGPT, Claude 등) 사용 경험이 있습니까?", ["예","아니오"])

        st.markdown("#### 다음은 귀하의 일반적인 성향에 대한 질문입니다.")

        st.markdown("**다음은 근거 기반 판단 성향에 대한 질문입니다.**")
        ebr1 = likert7("ebr1", "나는 판단 시 근거를 중요하게 고려한다.")
        ebr2 = likert7("ebr2", "나는 데이터를 기반으로 결론을 내리려 한다.")
        ebr3 = likert7("ebr3", "나는 주장에 대한 객관적 증거를 확인한다.")
        ebr4 = likert7("ebr4", "나는 직관보다 근거를 우선시한다.")
        st.write("") # 시각적 분리

        st.markdown("**다음은 AI 환각 인식에 대한 질문입니다.**")
        aha1 = likert7("aha1", "일반적으로 AI는 틀린 정보를 생성할 수 있다고 생각한다.")
        aha2 = likert7("aha2", "일반적으로 AI의 결과를 그대로 신뢰하지 않는다.")
        aha3 = likert7("aha3", "AI의 출력은 대체로 검증이 필요하다고 생각한다.")
        aha4 = likert7("aha4", "AI는 사실과 다른 내용을 만들 수 있다고 본다.")
        st.write("") # 시각적 분리

        st.markdown("**다음은 과신 성향에 대한 질문입니다.**")  # 설문 PDF 기준 5문항
        oc1 = likert7("oc1", "나는 자신의 이상과 원칙에 대한 충성이 '개방적 사고'보다 더 중요하다고 생각한다.")
        oc2 = likert7("oc2", "나는 사람들을 나를 지지하는 사람과 그렇지 않은 사람으로 구분하는 경향이 있다.")
        oc3 = likert7("oc3", "자신의 생각을 바꾸는 것은 약함의 표시라고 생각한다.")
        oc4 = likert7("oc4", "나는 의사결정을 할 때, 그것이 논리적으로 타당한지보다 '옳다고 느끼는 것'이 더 중요하다.")
        oc5 = likert7("oc5", "내가 내리려는 결론에 반하는 증거는 크게 고려하지 않는 경향이 있다.")
        st.write("") # 시각적 분리

        st.markdown("**다음은 정서 중시 성향에 대한 질문입니다.**")  # 설문 PDF 기준 4문항
        emo1 = likert7("emo1", "감정은 그 사람의 삶의 방향을 정하는데 영향을 준다.")
        emo2 = likert7("emo2", "인간의 다양한 감정은 삶을 더욱 흥미롭게 만든다.")
        emo3 = likert7("emo3", "나는 감정을 느끼는 것이 건강하다고 믿는다.")
        emo4 = likert7("emo4", "나는 감정을 통해 배운다.")
        st.write("") # 시각적 분리

        st.markdown("#### 다음은 실험 시작 전 귀하의 현재 상태에 대한 질문입니다.")

        st.markdown("**다음은 인지적 거리두기에 대한 질문입니다.**")
        psd1 = likert7("psd1", "나는 내 생각을 제3자의 시각에서 바라볼 수 있다.")
        psd2 = likert7("psd2", "나는 내 판단에 거리감을 두고 평가할 수 있다.")
        psd3 = likert7("psd3", "나는 한걸음 물러나 감정과 판단을 분리하려 노력할 수 있다.")
        psd4 = likert7("psd4", "나는 내 생각을 객관적으로 검토할 수 있다.")
        st.write("") # 시각적 분리

        st.markdown("**다음은 현실 모니터링에 대한 질문입니다.**")
        psm1 = likert7("psm1", "나는 내가 획득한 정보가 어디에서 왔는지 확인하려고 한다.")
        psm2 = likert7("psm2", "나는 사실과 막연한 추론을 구분하려고 한다.")
        psm3 = likert7("psm3", "나는 사실과 상상을 구별할 수 있다.")
        psm4 = likert7("psm4", "나는 내 판단의 근거가 되는 정보의 출처를 돌아본다.")
        st.write("") # 시각적 분리

        st.markdown("**다음은 반사실적 사고에 대한 질문입니다.**")
        pcf1 = likert7("pcf1", "나는 늘 내 생각이 틀릴 수 있다고 본다.")
        pcf2 = likert7("pcf2", "정답은 나의 원래의 생각과 다를 수 있다.")
        pcf3 = likert7("pcf3", "나는 다양한 해석을 시도한다.")
        pcf4 = likert7("pcf4", "나는 하나의 결론에 쉽게 확신하는 오류가 없다.")
        st.write("") # 시각적 분리

        st.markdown("**다음은 지적 겸손에 대한 질문입니다.**")
        pih1 = likert7("pih1", "나는 많은 경우에 다른 의견에 대해서 배워야 함을 안다.")
        pih2 = likert7("pih2", "나는 어떤 의견을 가지려고할 때 종종 내 생각이 틀릴 수도 있음을 안다.")
        pih3 = likert7("pih3", "나는 합당한 이유가 있다면 내 견해를 수정할 의향을 가진다.")
        pih4 = likert7("pih4", "나는 비록 몇몇 부분은 동의하지 않더라도 다른 이의 의견을 귀담아 들으려는 의지가 있다.")
        st.write("") # 시각적 분리

        st.markdown("#### 추가 문항")
        llm_halluc = likert7("llm_halluc", "LLM은 환각이 심하다고 보십니까?")

        submitted = st.form_submit_button("다음으로", use_container_width=True, type="primary")

    if submitted:
        if ai_6mo == "아니오":
            st.warning("본 실험은 최근 6개월 내 생성형 AI 사용 경험이 있는 분을 대상으로 합니다. 참여에 감사드립니다.")
            st.stop()

        auto_id = "P_" + uuid.uuid4().hex[:8].upper()
        st.session_state.participant_id = auto_id
        st.session_state.pre_survey_data = {
            "gender": gender, "age_group": age_group,
            "ebr": {"ebr1":ebr1,"ebr2":ebr2,"ebr3":ebr3,"ebr4":ebr4},
            "ebr_mean": round((ebr1+ebr2+ebr3+ebr4)/4, 2),
            "aha": {"aha1":aha1,"aha2":aha2,"aha3":aha3,"aha4":aha4},
            "aha_mean": round((aha1+aha2+aha3+aha4)/4, 2),
            "oc": {"oc1":oc1,"oc2":oc2,"oc3":oc3,"oc4":oc4,"oc5":oc5},
            "oc_mean": round((oc1+oc2+oc3+oc4+oc5)/5, 2),
            "emo": {"emo1":emo1,"emo2":emo2,"emo3":emo3,"emo4":emo4},
            "emo_mean": round((emo1+emo2+emo3+emo4)/4, 2),
            # 메타인지 baseline (현재형)
            "psd": {"psd1":psd1,"psd2":psd2,"psd3":psd3,"psd4":psd4},
            "psd_mean": round((psd1+psd2+psd3+psd4)/4, 2),
            "psm": {"psm1":psm1,"psm2":psm2,"psm3":psm3,"psm4":psm4},
            "psm_mean": round((psm1+psm2+psm3+psm4)/4, 2),
            "pcf": {"pcf1":pcf1,"pcf2":pcf2,"pcf3":pcf3,"pcf4":pcf4},
            "pcf_mean": round((pcf1+pcf2+pcf3+pcf4)/4, 2),
            "pih": {"pih1":pih1,"pih2":pih2,"pih3":pih3,"pih4":pih4},
            "pih_mean": round((pih1+pih2+pih3+pih4)/4, 2),
            "llm_halluc": llm_halluc,
        }

        # 무작위 셀 배정
        cell = assign_random_cell()
        if cell is None:
            st.error("모든 실험 셀이 가득 찼습니다. 참여에 감사드립니다.")
            st.stop()
        info = CELLS[cell]
        st.session_state.cell = cell
        st.session_state.group = info["group"]
        st.session_state.task_key = info["task"]
        st.session_state.engine = BMDMEngine()
        st.session_state.phase = "task"
        st.rerun()

    render_withdraw_button("pre_survey")

# ── PHASE 2: 과제 수행 ──
elif st.session_state.phase == "task":
    run_task_phase()

# ── PHASE 3: 사후 설문 (첨부 설문 PDF 기준) ──
elif st.session_state.phase == "post_survey":
    st.title("📋 사후 설문")
    st.caption("AI와의 대화 경험을 돌아보며 응답해 주세요.")

    with st.form("post_form"):

        # ── 과제 유형 인식 (참가자가 지각한 과제 성격) ──
        perceived_task = st.radio(
            "귀하가 설문한 과제는 다음의 어떤 과제에 해당한다고 생각하십니까?",
            ["분석적 과제", "창의적 과제"],
            captions=[
                "주어진 정보와 근거를 바탕으로 논리적으로 분석하여 그럴듯한 결론을 끌어내는 일",
                "직관을 충분히 활용하여 남들이 쉽게 내기 어려운 새롭고 독창적인 아이디어를 만들어내는 일",
            ],
            index=None,
            key="perceived_task",
        )
        st.divider()

        # ── (연구자 주석) 조작점검 문항 — 참가자에게는 구성개념/목적 비노출
        st.markdown("#### 다음은 본 실험 후 귀하의 경험에 대한 질문입니다.")
        st.markdown("**다음은 시스템에 의한 소외 효과 경험에 대한 질문입니다.**")
        bae1 = likert7("bae1", "이 시스템은 내 생각을 낯설게 바라보게 하였다.")
        bae2 = likert7("bae2", "이 시스템은 나의 판단을 한 발 떨어져서 보게 했다.")
        bae3 = likert7("bae3", "이 시스템은 내 사고를 비판적으로 검토하게 했다.")
        bae4 = likert7("bae4", "이 시스템은 나의 자동적 반응을 멈추고 생각하게 했다.")

        st.divider()

        # ── 메타인지 — 전반
        st.markdown("**다음은 메타인지에 대한 질문입니다.**")
        mc1 = likert7("mc1", "시스템과 대화하면서 나는 내가 하려는 목표에 잘 부합해가고 있는지 점검할 수 있었다.")
        mc2 = likert7("mc2", "시스템과 대화하면서 나는 내 생각의 오류를 인식하고 수정하기 위해 내 지적 노력을 수반했다.")
        mc3 = likert7("mc3", "시스템과 대화하면서 스스로 나의 문제를 해결하기 위한 여러 옵션을 구사하는지 점검하게 되었다.")
        mc4 = likert7("mc4", "시스템과 대화하면서 나는 내 생각이 맞는지 스스로 자문할 수 있었다.")

        st.divider()

        # ── 환각지수 자기보고

        st.markdown("**다음은 확신 대비 근거 부족에 대한 질문입니다.**")
        #st.markdown("**확신 대비 근거 부족**")
        lr1 = likert7("lr1", "실험하는 동안 나에게 충분한 근거 없이도 결론을 내리는 경우가 있음을 알게 되었다.")
        lr2 = likert7("lr2", "실험하는 동안 내가 근거가 부족해도 확신을 가짐을 알게 되었다.")
        lr3 = likert7("lr3", "실험하는 동안 나의 주장에 대한 근거를 명확히 제시하지 못하는 경우가 있었다.")
        lr4 = likert7("lr4", "실험하는 동안 나는 설명 없이 결론을 내리는 경우가 있었다.")
        lr5 = likert7("lr5", "실험하는 동안 내 판단의 근거를 충분히 점검하지 않은 적이 있다.")
        st.write("") # 시각적 분리

        st.markdown("**다음은 출처 모호성에 대한 질문입니다.**")
        lf1 = likert7("lf1", "실험하는 동안 나는 정보의 출처를 명확히 인식하지 못할 때가 있었다.")
        lf2 = likert7("lf2", "실험하는 동안 나는 어디서 얻은 정보인지 혼동하기도 했다.")
        lf3 = likert7("lf3", "실험하는 동안 나는 출처를 확인하지 않는 경우가 있었다.")
        lf4 = likert7("lf4", "실험하는 동안 나는 정보의 신뢰성을 검증하지 않는 경우가 있었다.")
        st.write("") # 시각적 분리

        st.markdown("**다음은 정서 기반 판단 성향에 대한 질문입니다.**")
        ah1 = likert7("ah1", "실험하는 동안 감정에 따라 판단이 달라진 적이 있다.")
        ah2 = likert7("ah2", "실험하는 동안 내가 좋아하는 정보에 더 확신을 가진 것을 알게 되었다.")
        ah3 = likert7("ah3", "실험하는 동안 내가 감정적으로 판단하는 경우가 있음을 알게 되었다.")
        ah4 = likert7("ah4", "실험하는 동안 나는 기분이 판단에 영향을 주는 것을 알게 되었다.")
        st.write("") # 시각적 분리

        st.markdown("**다음은 비일관된 판단에 대한 질문입니다.**")
        ic1 = likert7("ic1", "실험하는 동안 나는 상황에 따라 판단이 달라지기도 함을 알게 되었다.")
        ic2 = likert7("ic2", "실험하는 동안 나는 이전 판단과 다른 결론을 내리기도 함을 알게 되었다.")
        ic3 = likert7("ic3", "실험하는 동안 나는 일관된 기준을 유지하기 어려움을 알게 되었다.")
        ic4 = likert7("ic4", "실험하는 동안 나는 판단 기준이 변하였다.")
        st.write("") # 시각적 분리

        st.divider()
        st.markdown("#### 다음은 실험을 마친 후 귀하의 현재 상태에 대한 질문입니다.")

        # ── 인지적 거리두기 (설문 PDF 기준 4문항)
        st.markdown("**다음은 인지적 거리두기에 대한 질문입니다.**")
        sd1 = likert7("sd1", "시스템을 사용하면서 나는 내 생각을 제3자의 시각에서 바라볼 수 있었다.")
        sd2 = likert7("sd2", "시스템을 사용하면서 나는 내 판단에 거리감을 두고 평가할 수 있었다.")
        sd3 = likert7("sd3", "시스템을 사용하면서 나는 한걸음 물러나 감정과 판단을 분리하려 노력할 수 있었다.")
        sd4 = likert7("sd4", "시스템을 사용하면서 나는 내 생각을 객관적으로 검토할 수 있었다.")
        st.write("") # 시각적 분리

        

        # ── 현실 모니터링 (sm3 문구 설문 PDF 반영)
        st.markdown("**다음은 현실 모니터링에 대한 질문입니다.**")
        sm1 = likert7("sm1", "시스템을 사용하면서 나는 내가 획득한 정보가 어디에서 왔는지 확인하려고 하였다.")
        sm2 = likert7("sm2", "시스템을 사용하면서 나는 사실과 막연한 추론을 구분하려고 했다.")
        sm3 = likert7("sm3", "시스템을 사용하면서 나는 사실과 상상을 구별하려는 생각을 가지게 되었다.")
        sm4 = likert7("sm4", "시스템을 사용하면서 나는 내 판단의 근거가 되는 정보의 출처를 돌아보았다.")
        st.write("") # 시각적 분리


        # ── 반사실적 사고 (cf1 문구 설문 PDF 반영)
        st.markdown("**다음은 반사실적 사고에 대한 질문입니다.**")
        cf1 = likert7("cf1", "시스템을 사용하면서 내 생각이 틀릴 수 있음을 알게 되었다.")
        cf2 = likert7("cf2", "시스템을 사용하면서 정답은 나의 원래의 생각과 다를 수 있음을 생각해볼 수 있었다.")
        cf3 = likert7("cf3", "시스템을 사용하면서 나는 다양한 해석을 시도할 수 있었다.")
        cf4 = likert7("cf4", "시스템을 사용하면서 나는 하나의 결론에 쉽게 확신하는 오류를 줄일 수 있었다.")
        st.write("") # 시각적 분리


        # ── 지적 겸손
        st.markdown("**다음은 지적 겸손에 대한 질문입니다.**")
        ih1 = likert7("ih1", "시스템을 사용하면서 나는 많은 경우에 다른 의견에 대해서 배워야 함을 알게 되었다.")
        ih2 = likert7("ih2", "시스템을 사용하면서 나는 어떤 의견을 가지려고할 때 종종 내 생각이 틀릴 수도 있음을 알게 되었다.")
        ih3 = likert7("ih3", "시스템을 사용하면서 나는 합당한 이유가 있다면 내 견해를 수정할 의향을 가지게 되었다.")
        ih4 = likert7("ih4", "시스템을 사용하면서 나는 비록 몇몇 부분은 동의하지 않더라도 다른 이의 의견을 귀담아 들으려는 의지가 생겼다.")


        st.divider()

        # ── 공동창출 의향
        st.markdown("#### 다음은 오늘 GenAI 시스템 사용 후 소감입니다.")
        st.markdown("**다음은 GenAI와의 가치 공동 창출 의향에 대한 질문입니다.**")
        ci1 = likert7("ci1", "나는 GenAI를 활용하여 나의 목적에 맞는 결과를 만들기 위해 적극적으로 상호작용할 의향이 있다.")
        ci2 = likert7("ci2", "나는 GenAI의 작동 방식을 이해하기 위해 지속적으로 시도하고 학습하려 한다.")
        ci3 = likert7("ci3", "나는 더 나은 결과를 위해 GenAI에 추가적인 정보(맥락, 요구사항 등)를 제공할 의향이 있다.")
        ci4 = likert7("ci4", "나는 GenAI가 생성한 결과를 나의 아이디어에 맞게 수정하고 개선하려 한다.")
        ci5 = likert7("ci5", "나는 더 나은 결과를 얻기 위해 시간과 노력을 투자할 의향이 있다.")
        ci6 = likert7("ci6", "나는 GenAI와 협력하여 결과물을 공동으로 발전시키려 한다.")
        ci7 = likert7("ci7", "나는 GenAI를 활용하여 나의 문제 해결 과정에 적극적으로 참여하려 한다.")

        # ── 공동창출 효과
        st.markdown("**다음은 귀하의 GenAI와의 공동 가치 창출 효과입니다.**")
        ce1 = likert7("ce1", "나는 GenAI로부터 결과 개선을 위한 유용한 피드백을 제공받았다.")
        ce2 = likert7("ce2", "나는 GenAI를 활용하여 나의 생각을 더욱 명확히 표현할 수 있었다.")
        ce3 = likert7("ce3", "나는 GenAI를 통해 문제 해결 방안을 스스로 탐색할 수 있었다.")
        ce4 = likert7("ce4", "나는 GenAI가 제공하는 결과에 적극적으로 반응할 수 있었다.")
        ce5 = likert7("ce5", "나는 GenAI와의 상호작용을 통해 더욱 좋은 해결책을 만들어냈다.")
        ce6 = likert7("ce6", "나는 GenAI 결과의 도움으로 반복적으로 수정 보완하며 발전시킬 수 있었다.")
        ce7 = likert7("ce7", "나는 GenAI를 활용하여 이전보다 더 나은 결과를 만들어냈다.")

        st.divider()

        # ── 결과물 + 확신도(보조) + 성찰
        st.markdown("**최종 결과물 및 소감**")
        task_key = st.session_state.get("task_key","creative")
        post_conf = st.slider("최종 결과물이 얼마나 완벽하다고 생각하시나요? (%)", 0, 100, 50, key="post_conf")  # 보조 탐색지표
        final_title = st.text_input(TASK_INFO[task_key]["final_label"], key="final_title")
        final_reason = st.text_area("최종 판단 이유", height=80, key="final_reason")
        # 창의성(Index of Creativity)은 전문가 2인 평가가 정식 측정 — 아래 자기보고는 보조 참고용
        cr_orig = likert7("cr_orig", "결과물이 독창적이다.")
        cr_use  = likert7("cr_use",  "결과물이 유용하다.")
        cr_fit  = likert7("cr_fit",  "결과물이 과제에 적합하다.")

        st.divider()
        st.markdown("#### 전체 경험")
        overall_sat = likert7("overall_sat", "AI와의 전체 대화 과정에 만족한다.")
        reflection = st.text_area("AI와 대화하면서 본인의 생각이나 관점에 변화가 있었다면 자유롭게 서술해주세요.", height=80)

        submitted = st.form_submit_button("최종 제출 완료", use_container_width=True, type="primary")

    if submitted:
        state = st.session_state.get("t_state", ConversationState())
        pre_conf = st.session_state.get("t_pre_confidence", 50)

        result = {
            "participant_id": st.session_state.get("participant_id",""),
            "experiment_design": {
                "cell": st.session_state.get("cell",""),
                "group": st.session_state.get("group",""),
                "task_type": st.session_state.get("task_key",""),
                "design": "2x2 between-subjects",
                "fixed_cycles": FIXED_CYCLES,
                "api_model": CLAUDE_MODEL,
            },
            "pre_survey": st.session_state.get("pre_survey_data",{}),
            "timestamp": now_kst_str(),

            # 사후설문 데이터
            "post_survey": {
                "perceived_task_type": perceived_task,
                "manipulation_check": {
                    "bae": {"bae1":bae1,"bae2":bae2,"bae3":bae3,"bae4":bae4},
                    "bae_mean": round((bae1+bae2+bae3+bae4)/4, 2)},
                "hallucination_self_report": {
                    "unsupported_claim": {"lr1":lr1,"lr2":lr2,"lr3":lr3,"lr4":lr4,"lr5":lr5},
                    "lr_mean": round((lr1+lr2+lr3+lr4+lr5)/5, 2),
                    "source_ambiguity": {"lf1":lf1,"lf2":lf2,"lf3":lf3,"lf4":lf4},
                    "lf_mean": round((lf1+lf2+lf3+lf4)/4, 2),
                    "affect_heuristic": {"ah1":ah1,"ah2":ah2,"ah3":ah3,"ah4":ah4},
                    "ah_mean": round((ah1+ah2+ah3+ah4)/4, 2),
                    "inconsistency": {"ic1":ic1,"ic2":ic2,"ic3":ic3,"ic4":ic4},
                    "ic_mean": round((ic1+ic2+ic3+ic4)/4, 2),
                },
                "metacognition": {
                    "general": {"mc1":mc1,"mc2":mc2,"mc3":mc3,"mc4":mc4},
                    "mc_mean": round((mc1+mc2+mc3+mc4)/4, 2),
                    "cognitive_distancing": {"sd1":sd1,"sd2":sd2,"sd3":sd3,"sd4":sd4},
                    "sd_mean": round((sd1+sd2+sd3+sd4)/4, 2),
                    "source_monitoring": {"sm1":sm1,"sm2":sm2,"sm3":sm3,"sm4":sm4},
                    "sm_mean": round((sm1+sm2+sm3+sm4)/4, 2),
                    "counterfactual": {"cf1":cf1,"cf2":cf2,"cf3":cf3,"cf4":cf4},
                    "cf_mean": round((cf1+cf2+cf3+cf4)/4, 2),
                    "intellectual_humility": {"ih1":ih1,"ih2":ih2,"ih3":ih3,"ih4":ih4},
                    "ih_mean": round((ih1+ih2+ih3+ih4)/4, 2),
                },
                "co_creation_intention": {"ci1":ci1,"ci2":ci2,"ci3":ci3,"ci4":ci4,"ci5":ci5,"ci6":ci6,"ci7":ci7},
                "ci_mean": round((ci1+ci2+ci3+ci4+ci5+ci6+ci7)/7, 2),
                "co_creation_effect": {"ce1":ce1,"ce2":ce2,"ce3":ce3,"ce4":ce4,"ce5":ce5,"ce6":ce6,"ce7":ce7},
                "ce_mean": round((ce1+ce2+ce3+ce4+ce5+ce6+ce7)/7, 2),
                "creativity": {"originality":cr_orig,"usefulness":cr_use,"fit":cr_fit},  # 보조 자기보고
                "creativity_index": round((cr_orig+cr_use+cr_fit)/3, 2),
                "confidence": {"pre":pre_conf,"post":post_conf,"change":post_conf-pre_conf},  # 보조 탐색지표
                "final_title": final_title,
                "final_reason": final_reason,
                "overall_satisfaction": overall_sat,
                "reflection": reflection,
            },

            # 알고리즘 산출 데이터
            "algorithm_data": {
                "initial_input": st.session_state.get("t_initial_input",""),
                "initial_hi": st.session_state.get("t_initial_metrics",{}),
                "final_hi": state.hallucination_metrics,
                "hi_change": round(
                    st.session_state.get("t_initial_metrics",{}).get("Hallucination_Index",0)
                    - state.hallucination_metrics.get("Hallucination_Index",0), 3),
                "meta_cognitive_activation": state.meta_cognitive_activation,
                "total_cycles": state.cycle_count,
                "transcript": st.session_state.get("t_transcript",[]),
            },
        }

        os.makedirs("results", exist_ok=True)
        fname = f"results/{st.session_state.participant_id}_{st.session_state.get('cell','X')}_{now_kst().strftime('%Y%m%d_%H%M%S')}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        # ★ 로컬 CSV 누적 저장 — 구글시트와 동일한 표준 순서(CANONICAL_COLUMNS)로 통일.
        #    기존 파일이 구버전 헤더면 칼럼명 기준으로 무손실 재작성한다.
        import csv
        csv_path = "results/all_results_korean.csv"
        flat = _row_dict_from_result(result)

        old_rows, old_header = [], []
        if os.path.exists(csv_path):
            try:
                with open(csv_path, "r", newline="", encoding="utf-8-sig") as cf:
                    rdr = csv.DictReader(cf)
                    old_header = rdr.fieldnames or []
                    old_rows = list(rdr)
            except Exception:
                old_rows, old_header = [], []

        # 헤더 = 표준 칼럼 + (flat의 신규 키) + (구헤더에만 있던 키) → 무손실
        csv_columns = _desired_header(flat, old_header)
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as cf:
            writer = csv.DictWriter(cf, fieldnames=csv_columns, extrasaction="ignore")
            writer.writeheader()
            for r in old_rows:                                  # 과거 행 보존(칼럼명 기준)
                writer.writerow({k: r.get(k, "") for k in csv_columns})
            writer.writerow(flat)                               # 이번 응답 추가

        # ★ Google Sheets 저장 — 사전설문 포함 전체 문항을 '설문 제시 순서'대로 기록.
        #    기존 데이터가 구버전 헤더이면 칼럼명 기준으로 무손실 재배치 후 추가한다.
        try:
            _save_row_to_gsheet(result)
        except Exception:
            pass  # Google Sheets 실패해도 로컬 JSON/CSV는 이미 저장됨

        st.session_state.saved_result = result
        st.session_state.phase = "done"
        st.rerun()

    render_withdraw_button("post_survey")

# ── PHASE 4: 완료 ──
elif st.session_state.phase == "done":
    st.title("✅ 실험 완료!")
    st.success("과제를 완료해주셔서 진심으로 감사드립니다.")
    st.markdown("실험이 정상적으로 저장되었습니다.\n\n궁금하신 사항이 있으시면 연구 담당자에게 문의해 주세요.")

# ── PHASE 5: 참여 종료 (중도 철회) ──
elif st.session_state.phase == "withdrawn":
    st.title("🚪 참여가 종료되었습니다")
    st.info("연구 참여를 중단하셨습니다. 참여해 주셔서 감사합니다.")
    st.markdown("""
참여 도중 언제든지 중단할 수 있으며, 이에 따른 어떠한 불이익도 발생하지 않습니다.

궁금하신 사항이 있으시면 연구 담당자에게 문의해 주세요.

- 연구담당자: 권상지 연구원
- 소속: 경희대학교 경영학과, 차세대정보기술연구센터(CAITECH)
- 이메일: aaaitaaa@khu.ac.kr
    """)
