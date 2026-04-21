# ============================================================
# BMDM 실험 플랫폼 (Streamlit + Claude API)
#
# ★ 2×2 between-subjects factorial design
#   셀A: BMDM + 분석적 과제  |  셀B: BMDM + 창의적 과제
#   셀C: 통제 + 분석적 과제  |  셀D: 통제 + 창의적 과제
#   참가자 1명 = 1개 셀, 1개 과제만 수행 (셀당 최대 30명)
#   FIXED_CYCLES = 5회 대화
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
FIXED_CYCLES   = 5
HOST_PASSWORD  = st.secrets.get("HOST_PASSWORD", "bmdm2025admin")
MAX_PER_CELL   = 30

CELLS = {
    "A": {"group": "experimental", "task": "factual",  "label": "BMDM + 분석적 과제"},
    "B": {"group": "experimental", "task": "creative", "label": "BMDM + 창의적 과제"},
    "C": {"group": "control",      "task": "factual",  "label": "통제 + 분석적 과제"},
    "D": {"group": "control",      "task": "creative", "label": "통제 + 창의적 과제"},
}

# ============================================================
# Google Sheets 연결 (Streamlit Community Cloud용)
# ============================================================
GSHEET_NAME = st.secrets.get("GSHEET_NAME", "BMDM_Results")

@st.cache_resource
def _get_gspread_client():
    """Google Sheets 클라이언트 초기화 (서비스 계정 인증)."""
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
        st.warning(f"Google Sheets 연결 실패 — 로컬 파일로 저장합니다. ({e})")
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
            return spreadsheet.add_worksheet(title=sheet_name, rows=200, cols=50)
    except Exception as e:
        return None

# ============================================================
# 셀 인원 관리 (Google Sheets 우선, 로컬 JSON 폴백)
# ============================================================
CELL_COUNT_FILE = "cell_counts.json"

def _load_cell_counts():
    # Google Sheets 우선
    ws = _get_worksheet("cell_counts")
    if ws:
        try:
            data = ws.get_all_records()
            if data:
                return {row["cell"]: int(row["count"]) for row in data}
            else:
                # 초기화: 헤더 + 4개 셀 행 생성
                ws.update("A1:B1", [["cell", "count"]])
                rows = [[k, 0] for k in CELLS]
                ws.update(f"A2:B{len(rows)+1}", rows)
                return {k: 0 for k in CELLS}
        except:
            pass
    # 폴백: 로컬 JSON
    if os.path.exists(CELL_COUNT_FILE):
        with open(CELL_COUNT_FILE, "r") as f:
            return json.load(f)
    return {k: 0 for k in CELLS}

def _save_cell_counts(counts):
    # Google Sheets 우선
    ws = _get_worksheet("cell_counts")
    if ws:
        try:
            rows = [["cell", "count"]] + [[k, v] for k, v in counts.items()]
            ws.update(f"A1:B{len(rows)}", rows)
        except:
            pass
    # 로컬도 항상 저장 (폴백)
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
# 실험집단: 필수 프롬프트 + Claude API 도입 문장
# ============================================================
MANDATORY_PROMPTS = {
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
# 각 사이클별 "의도(intent)" 정의 — 논문의 '결과 개선형' 프롬프트를 5단계로 세분화
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


# 금지 패턴 (LLM이 규칙을 어기면 폴백)
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
    # 평서문으로 끝나는지 (~다/요/오 등)
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

    # LLM에 전달할 요청 메시지 — 템플릿을 '거의 그대로' 쓰도록 강하게 고정
    user_msg = f"""[의도] {intent}
[기본 문장] "{base_sentence}"

위 '기본 문장'을 거의 그대로 사용하되, 자연스럽게 한 번만 다시 표현하여 1문장으로 출력하세요.
질문·검증·성찰 유도 표현은 절대 금지입니다. 평서문 1문장만."""

    # 최대 2회 재시도 (LLM이 규칙 위반 시 폴백)
    for _ in range(2):
        out = call_claude_api(CTRL_SYSTEM_STRICT, user_msg, max_tokens=120)
        if out:
            # 첫 줄만 취하고 따옴표/번호 제거
            line = out.strip().split("\n")[0].strip()
            line = re.sub(r'^["\'\d\.\)\s\-]+', '', line).strip()
            line = line.strip('"').strip("'").strip()
            if _validate_ctrl_output(line):
                return [line]

    # 폴백: 검증 실패 시 기본 문장 그대로 사용
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
    # fallback
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
        "label": "창의적·개방형 과제 — 달 탐사선 명명",
        "instruction": """**[과제] 달 탐사선 명명 및 상징 설명 작성**

우리 기관이 새로운 달 탐사선을 개발했습니다.
탐사선의 **이름을 제안하고, 그 이름이 가지는 상징성과
대중을 설득할 수 있는 창의적인 이유**를 작성해주세요.

*독창성·상징성·설득력이 요구됩니다.*""",
        "final_label": "최종 결정한 달 탐사선 이름",
        "confidence_q": "방금 제안하신 이름과 설명이 얼마나 완벽하다고 생각하시나요?",
    },
    "factual": {
        "label": "분석적·사실기반 과제 — AI 규제 정책 분석",
        "instruction": """**[과제] AI 데이터 수집 규제 정책 분석**

최근 정부가 AI 데이터 수집을 엄격히 제한하는 규제안을 발표했습니다.
이 규제가 **국내 스타트업 생태계에 미칠 영향을 분석하고,
본인의 판단을 명확한 근거와 함께 서술**해주세요.

*검증 가능한 사실과 근거 제시가 요구됩니다.*""",
        "final_label": "최종 도출한 핵심 분석 (한 줄 요약)",
        "confidence_q": "방금 작성하신 분석이 얼마나 완벽하다고 생각하시나요?",
    }
}

# ============================================================
# BMDM 엔진
# ============================================================
class BMDMEngine:
    ALL_STRATEGIES = [
        "Externalize","Origin_Source_Differentiation","Counter_Position",
        "Evidence_Calibration","Probability_Framing"]
    STEP_LABELS = {
        "Externalize":"외재화(Externalization)",
        "Origin_Source_Differentiation":"기원–출처 분리(Origin–Source Differentiation)",
        "Counter_Position":"대안 관점 유도(Counter-Position Induction)",
        "Evidence_Calibration":"근거 재구성(Evidence Calibration)",
        "Probability_Framing":"확률적 재구성(Probabilistic Reframing)",
        "Control_Supportive":"AI 피드백"}

    def Extract_Claims(self, text):
        t = text.strip()
        return [Claim(content=t, certainty=self._est_cert(t), affect_intensity=self._est_affect(t),
                       evidence_status=self._est_evidence(t), source_ambiguity=self._est_source(t))]

    def Select_Next_Strategy(self, used_modes):
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
        """Claim 파라미터 점진 갱신 — 양 집단 공통 로직 + 실험집단 전략 보너스.

        논문 3.5절 "조건 차이는 오직 프롬프팅 방식에만 있다"와 일치시키기 위해,
        양 집단 모두 동일한 공통 갱신을 적용하고, BMDM 전략의 순효과는 실험집단의
        추가 조정(_apply_strategy_bonus)에서만 발생시킨다.
        """
        # (1) 공통 갱신: 모든 집단에 적용되는 텍스트 기반 점진 조정
        self._apply_common_update(claim, resp)
        # (2) 전략 보너스: 실험집단에서만 BMDM 전략별 추가 조정
        if group == "experimental":
            self._apply_strategy_bonus(claim, mode, resp)

    def _apply_common_update(self, claim, resp):
        """양 집단 공통: 응답 텍스트 기반 보수적 파라미터 갱신 (카테고리당 ±0.04)."""
        t = resp.lower()
        STEP = 0.04

        # Evidence: 근거·자료 제시 키워드 → 증거 상태 증가
        if any(k in t for k in ["데이터","근거","통계","연구","조사","실험","자료","보고서","문헌"]):
            claim.evidence_status = min(1.0, claim.evidence_status + STEP)

        # Source Ambiguity: 출처 명시 표현 → 모호성 감소
        if any(k in t for k in ["에 따르면","보고서","발표","출처","인용","논문"]):
            claim.source_ambiguity = max(0.0, claim.source_ambiguity - STEP)

        # Certainty: 완화 표현(내림) vs 강화 표현(올림) — 상호 배타
        if any(k in t for k in ["아마","가능","추정","것 같","일지","어쩌면","모르","불확실"]):
            claim.certainty = max(0.0, claim.certainty - STEP)
        elif any(k in t for k in ["반드시","완벽","확실","절대","틀림없","분명","유일"]):
            claim.certainty = min(1.0, claim.certainty + STEP)

        # Affect Intensity: 정서 키워드(올림) vs 객관 키워드(내림) — 상호 배타
        if any(k in t for k in ["자부심","위상","애착","감동","상징","명예","희망"]):
            claim.affect_intensity = min(1.0, claim.affect_intensity + STEP)
        elif any(k in t for k in ["객관","중립","냉정","사실적"]):
            claim.affect_intensity = max(0.0, claim.affect_intensity - STEP)

    def _apply_strategy_bonus(self, claim, mode, resp):
        """실험집단 전용: BMDM 전략별 추가 조정 (기존 Update_Claim 로직 유지)."""
        t = resp.lower()
        if mode == "Externalize" and any(k in t for k in ["외부","다를 수","다르게"]):
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
            nums = re.findall(r'(\d+)\s*%', h.get("user_response", "") if isinstance(h, dict) else "")
            if nums:
                vals.append(int(nums[-1]) / 100)
        if len(vals) < 2:
            return 0.05
        return min(0.30, abs(vals[0] - vals[-1]) * 0.5)

# ============================================================
# 과제 실행 (참가자: 수치 비노출)
# ============================================================
def run_task_phase():
    engine = st.session_state.engine
    group  = st.session_state.group
    task_key = st.session_state.task_key
    task = TASK_INFO[task_key]
    prefix = "t_"

    st.title(f"📝 {task['label']}")

    # [A] 초기 입력
    if not st.session_state.get(prefix+"initial_input"):
        st.markdown(task["instruction"])
        with st.form("initial_form"):
            user_input = st.text_area("당신의 주장과 이유를 자세히 작성하세요", height=150,
                                       placeholder="자유롭게 작성해주세요...")
            st.markdown("---")
            st.caption(task["confidence_q"])
            pre_conf = st.slider("완벽도 (0~100%)", 0, 100, 50, key="pre_conf")
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
                mode = engine.Select_Next_Strategy([])
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
                        st.caption(f"📊 선택한 확률: {turn['probability_slider']}%")
                    st.write(turn["user_response"])

        if not is_done:
            cur_mode = st.session_state[prefix+"cur_mode"]
            is_prob_framing = (cur_mode == "Probability_Framing" and group == "experimental")

            with st.chat_message("assistant"):
                for p in st.session_state[prefix+"cur_prompts"]:
                    st.markdown(p)
                if is_prob_framing:
                    st.markdown("**아래 슬라이더로 확률을 선택해 주세요.**")

            with st.form(f"chat_{cycle_done}", clear_on_submit=True):
                remaining = FIXED_CYCLES - cycle_done

                # ★ 확률적 재구성 전략일 때 확률 슬라이더 표시
                prob_val = None
                if is_prob_framing:
                    prob_val = st.slider(
                        "이 주장이 옳을 확률 (0~100%)", 0, 100, 50,
                        key=f"{prefix}prob_slider",
                        help="현재 주장이 옳다고 생각하는 확률을 선택하세요."
                    )

                user_resp = st.text_area(f"응답을 입력하세요 ({remaining}회 남음):", height=100)
                sent = st.form_submit_button("전송")

            if sent and user_resp.strip():
                cur_mode = st.session_state[prefix+"cur_mode"]

                # 확률 슬라이더 값을 응답에 반영
                final_resp = user_resp
                if prob_val is not None:
                    final_resp = f"확률: {prob_val}%. {user_resp}"
                    claim.certainty = prob_val / 100.0

                # 공통: 양 집단 모두 동일한 Claim 갱신 로직을 사용 (논문 3.5절 정합)
                engine.Update_Claim(claim, cur_mode, final_resp, group=group)
                # 실험집단 전용: 메타인지 활성화 측정 (BMDM 내부 모니터링 — 논문 3.1절 표1)
                if group == "experimental":
                    state.meta_cognitive_activation = evaluate_meta_cognitive(
                        cur_mode, final_resp, state.meta_cognitive_activation)
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
                        nxt = engine.Select_Next_Strategy(used)
                        st.session_state[prefix+"cur_mode"] = nxt
                        st.session_state[prefix+"cur_prompts"] = generate_experimental_prompt(nxt, user_resp, transcript, task_key)
                st.rerun()

        if is_done:
            st.success("과제가 완료되었습니다. 수고하셨습니다!")
            if st.button("사후 설문으로 이동 →", use_container_width=True, type="primary"):
                st.session_state.phase = "post_survey"
                st.rerun()

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
                _setup_task_at_cycle(cyc)
                st.session_state.phase = "task"
            st.rerun()

        # 초기화
        st.markdown("")
        if st.button("🔄 전체 초기화", key="host_reset", use_container_width=True):
            keep = {"host_auth"}
            for k in list(st.session_state.keys()):
                if k not in keep: del st.session_state[k]
            st.session_state.phase = "intro"; st.rerun()

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
            csv_path = "results/all_results.csv"
            if files:
                def get_korean_name(key_path):
                    # 소문자로 변환하여 경로 상관없이 단어(키워드)로 매핑
                    key_str = str(key_path).lower()
                    
                    if "creativity" in key_str and "fit" in key_str: return "[결과] 적합성"
                    if "creativity" in key_str and "original" in key_str: return "[결과] 독창성"
                    if "creativity" in key_str and "useful" in key_str: return "[결과] 유용성"
                    if "creativity_index" in key_str: return "[결과] 창의성 종합지수"
                    if "confidence" in key_str and "pre" in key_str: return "[실험] 사전 확신도"
                    if "confidence" in key_str and "post" in key_str: return "[실험] 사후 확신도"
                    if "confidence" in key_str and "change" in key_str: return "[분석] 확신도 변화량"
                    
                    if "nfc1" in key_str: return "[사전_NFC1] 복잡한 문제 선호"
                    if "nfc2" in key_str: return "[사전_NFC2] 깊은 사고 선호"
                    if "nfc3" in key_str: return "[사전_NFC3] 사고 과정 즐김"
                    if "nfc_mean" in key_str: return "[사전_NFC_평균]"
                    
                    if "task_order" in key_str: return "[시스템] 과제_수행_순서"
                    if "fixed_cycles" in key_str: return "[시스템] 고정_대화_턴수"
                    if "design" in key_str: return "[시스템] 실험_설계"
                    if "reflection" in key_str: return "[최종] 주관적 성찰 기록"
                    if "participant_id" in key_str: return "[시스템] 참가자_ID"
                    if "timestamp" in key_str: return "[시스템] 참여_일시"
                    if "cell" in key_str: return "[시스템] 배정_셀"
                    if "group" in key_str: return "[시스템] 실험집단"
                    if "task_type" in key_str: return "[시스템] 과제유형"
                    if "api_model" in key_str: return "[시스템] 사용_모델"
                    if "total_cycles" in key_str: return "[시스템] 진행된_대화_턴수"
                    if "hi_change" in key_str: return "[분석] 환각지수 감소량"
                    if "transcript" in key_str: return "[로그] 대화 전체 기록"
                    if "initial_input" in key_str: return "[과제] 최초 주장 내용"
                    if "final_title" in key_str: return "[최종] 결과물 제목"
                    if "final_reason" in key_str: return "[최종] 판단 이유"
                    if "overall_satisfaction" in key_str: return "[결과] 전체 대화 만족도"
                    if "gender" in key_str: return "[사전] 성별"
                    if "age_group" in key_str: return "[사전] 연령대"
                    if "ai_experience" in key_str: return "[사전] AI 경험"

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
                    
                    if "cognitive_distance" in key_str: return "[알고리즘_메타인지] 인지적 거리두기"
                    if "counterfactual" in key_str and "simulation" in key_str: return "[알고리즘_메타인지] 반사실적 사고"
                    if "epistemic_humility" in key_str: return "[알고리즘_메타인지] 지적 겸손"
                    if "reality_monitoring" in key_str: return "[알고리즘_메타인지] 현실 모니터링"

                    last_part = key_str.split('.')[-1]
                    SURVEY_MAP = {
                        "ebr1": "[사전_근거1] 근거 중요성", "ebr2": "[사전_근거2] 데이터 기반", "ebr3": "[사전_근거3] 증거 확인", "ebr4": "[사전_근거4] 직관보다 근거", "ebr_mean": "[사전_근거_평균]",
                        "aha1": "[사전_AI환각인식1] AI 틀린 정보", "aha2": "[사전_AI환각인식2] 무조건 신뢰 안함", "aha3": "[사전_AI환각인식3] 검증 필요", "aha4": "[사전_AI환각인식4] 사실과 다른 내용", "aha_mean": "[사전_AI환각인식_평균]",
                        "oc1": "[사전_과신편향1] 이상/원칙 충성", "oc2": "[사전_과신편향2] 지지자 구분", "oc3": "[사전_과신편향3] 생각변화는 약함", "oc4": "[사전_과신편향4] 논리보다 느낌", "oc5": "[사전_과신편향5] 반대증거 무시", "oc6": "[사전_과신편향6] 내 판단 믿음", "oc_mean": "[사전_과신편향_평균]",
                        "emo1": "[사전_정서중시1] 삶 방향 영향", "emo2": "[사전_정서중시2] 삶을 흥미롭게", "emo3": "[사전_정서중시3] 느끼는것이 건강", "emo4": "[사전_정서중시4] 감정 통해 배움", "emo5": "[사전_정서중시5] 판단에 영향", "emo_mean": "[사전_정서중시_평균]",
                        "bae1": "[사후_소외1] 낯설게 바라봄", "bae2": "[사후_소외2] 떨어져서 봄", "bae3": "[사후_소외3] 비판적 검토", "bae4": "[사후_소외4] 자동적반응 멈춤", "bae_mean": "[사후] 소외효과 조작점검 평균",
                        "mc1": "[사후_메타1] 목표 부합 점검", "mc2": "[사후_메타2] 오류 인식", "mc3": "[사후_메타3] 옵션 점검", "mc4": "[사후_메타4] 타당성 자문", "mc_mean": "[사후] 메타인지(전반) 평균",
                        "sd1": "[사후_거리1] 제3자 시각", "sd2": "[사후_거리2] 거리감 유지", "sd3": "[사후_거리3] 감정/판단 분리", "sd4": "[사후_거리4] 객관적 검토", "sd5": "[사후_거리5] 일시성 인지", "sd_mean": "[사후] 메타인지(거리두기) 평균",
                        "sm1": "[사후_출처1] 출처 확인", "sm2": "[사후_출처2] 사실/추론 구분", "sm3": "[사후_출처3] 사실/상상 구별", "sm4": "[사후_출처4] 근거출처 성찰", "sm_mean": "[사후] 메타인지(출처모니터링) 평균",
                        "cf1": "[사후_반사실1] 다른 가능성", "cf2": "[사후_반사실2] 다른 정답 인지", "cf3": "[사후_반사실3] 다양한 해석", "cf4": "[사후_반사실4] 확신오류 감소", "cf_mean": "[사후] 메타인지(반사실적사고) 평균",
                        "ih1": "[사후_지적겸손1] 학습 필요성", "ih2": "[사후_지적겸손2] 오류 인정", "ih3": "[사후_지적겸손3] 수정 의향", "ih4": "[사후_지적겸손4] 경청 의지", "ih_mean": "[사후] 메타인지(지적겸손) 평균",
                        "lr1": "[사후_환각_근거부족1] 결론 인지", "lr2": "[사후_환각_근거부족2] 확신 인지", "lr3": "[사후_환각_근거부족3] 제시 미흡", "lr4": "[사후_환각_근거부족4] 설명 없음", "lr5": "[사후_환각_근거부족5] 점검 미흡", "lr_mean": "[사후] 자기보고 환각(근거부족) 평균",
                        "lf1": "[사후_환각_출처모호1] 불명확", "lf2": "[사후_환각_출처모호2] 혼동", "lf3": "[사후_환각_출처모호3] 미확인", "lf4": "[사후_환각_출처모호4] 검증 미흡", "lf_mean": "[사후] 자기보고 환각(출처모호) 평균",
                        "ah1": "[사후_환각_정서판단1] 판단 변화", "ah2": "[사후_환각_정서판단2] 선호 확신", "ah3": "[사후_환각_정서판단3] 판단 인지", "ah4": "[사후_환각_정서판단4] 기분 영향", "ah_mean": "[사후] 자기보고 환각(정서판단) 평균",
                        "ic1": "[사후_환각_비일관성1] 판단 변화", "ic2": "[사후_환각_비일관성2] 다른 결론", "ic3": "[사후_환각_비일관성3] 기준 유지", "ic4": "[사후_환각_비일관성4] 기준 변화", "ic_mean": "[사후] 자기보고 환각(비일관성) 평균",
                        "ci1": "[사후_공동창출의향1] 적극 상호작용", "ci2": "[사후_공동창출의향2] 학습 시도", "ci3": "[사후_공동창출의향3] 추가정보 제공", "ci4": "[사후_공동창출의향4] 결과 수정개선", "ci5": "[사후_공동창출의향5] 시간노력 투자", "ci6": "[사후_공동창출의향6] 공동 발전", "ci7": "[사후_공동창출의향7] 문제해결 참여", "ci_mean": "[사후] 공동창출 의향 평균",
                        "ce1": "[사후_공동창출효과1] 유용한 피드백", "ce2": "[사후_공동창출효과2] 명확한 표현", "ce3": "[사후_공동창출효과3] 방안 탐색", "ce4": "[사후_공동창출효과4] 적극 반응", "ce5": "[사후_공동창출효과5] 좋은 해결책", "ce6": "[사후_공동창출효과6] 반복 수정발전", "ce7": "[사후_공동창출효과7] 더 나은 결과", "ce_mean": "[사후] 공동창출 효과 평균"
                    }
                    if last_part in SURVEY_MAP: return SURVEY_MAP[last_part]
                    return f"[미확인] {last_part}"

                # =========================================================
                # 🎯 여기입니다! 가나다순을 폐기하고 완벽한 시간 흐름순으로 강제 고정
                # =========================================================
                ORDERED_COLUMNS = [
                    # [1] 인트로 & 시스템
                    "[시스템] 참가자_ID", "[시스템] 참여_일시", "[시스템] 배정_셀", "[시스템] 실험집단", "[시스템] 과제유형", "[시스템] 사용_모델", "[시스템] 과제_수행_순서",
                    # [2] 사전 설문
                    "[사전] 성별", "[사전] 연령대", "[사전] AI 경험", "[사전_NFC_평균]", "[사전_근거_평균]", "[사전_AI환각인식_평균]", "[사전_과신편향_평균]", "[사전_정서중시_평균]",
                    # [3] 실험 과제
                    "[과제] 최초 주장 내용", "[실험] 사전 확신도", "[알고리즘] 초기 환각지수(HI)",
                    # [4] 사후 설문
                    "[사후] 소외효과 조작점검 평균", "[실험] 사후 확신도", 
                    "[사후] 메타인지(전반) 평균", "[사후] 메타인지(거리두기) 평균", "[사후] 메타인지(출처모니터링) 평균", "[사후] 메타인지(반사실적사고) 평균", "[사후] 메타인지(지적겸손) 평균",
                    "[사후] 자기보고 환각(근거부족) 평균", "[사후] 자기보고 환각(출처모호) 평균", "[사후] 자기보고 환각(정서판단) 평균", "[사후] 자기보고 환각(비일관성) 평균",
                    "[사후] 공동창출 의향 평균", "[사후] 공동창출 효과 평균",
                    # [5] 완료 & 산출물
                    "[최종] 결과물 제목", "[최종] 판단 이유", "[최종] 주관적 성찰 기록", "[결과] 독창성", "[결과] 유용성", "[결과] 적합성", "[결과] 창의성 종합지수", "[결과] 전체 대화 만족도",
                    # [6] 기타 분석 및 로그
                    "[분석] 환각지수 감소량", "[분석] 확신도 변화량", "[알고리즘] 최종 환각지수(HI)",
                    "[알고리즘_초기] 확신-근거 불일치", "[알고리즘_초기] 정서 기반 판단", "[알고리즘_초기] 출처 모호성", "[알고리즘_초기] 확신 대비 근거 부족", "[알고리즘_초기] 비일관성",
                    "[알고리즘_최종] 확신-근거 불일치", "[알고리즘_최종] 정서 기반 판단", "[알고리즘_최종] 출처 모호성", "[알고리즘_최종] 확신 대비 근거 부족", "[알고리즘_최종] 비일관성",
                    "[알고리즘_메타인지] 인지적 거리두기", "[알고리즘_메타인지] 반사실적 사고", "[알고리즘_메타인지] 지적 겸손", "[알고리즘_메타인지] 현실 모니터링",
                    "[시스템] 진행된_대화_턴수", "[시스템] 고정_대화_턴수", "[시스템] 실험_설계", "[로그] 대화 전체 기록"
                ]

                def _flatten_all(d, prefix=""):
                    items = {}
                    for k, v in d.items():
                        full_path = f"{prefix}{k}" if prefix else k
                        if isinstance(v, dict):
                            items.update(_flatten_all(v, full_path + "."))
                        else:
                            mapped_key = get_korean_name(full_path)
                            items[mapped_key] = json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
                    return items

                all_r = [json.load(open(fp, "r", encoding="utf-8")) for fp in files]
                processed_data = [_flatten_all(r) for r in all_r]

                if processed_data:
                    import io, csv
                    output = io.StringIO()
                    
                    # 엑셀에 적을 데이터 안에 존재하는 키를 모두 뽑음
                    present_keys_in_data = list(set(k for row in processed_data for k in row.keys()))
                    
                    # 🎯 알파벳 정렬(sorted)을 빼고, 위에서 선언한 ORDERED_COLUMNS 순서대로만 담음
                    final_columns = [k for k in ORDERED_COLUMNS if k in present_keys_in_data]
                    
                    # 혹시 정의되지 않은 문항(예: ebr1 개별 문항 등)이 섞여있다면 맨 뒤에 붙임
                    extra_columns = sorted([k for k in present_keys_in_data if k not in final_columns])
                    final_columns.extend(extra_columns)
                    
                    writer = csv.DictWriter(output, fieldnames=final_columns)
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

# 참여 종료 버튼 (사이드바 — 진행 중인 단계에서만 표시)
if st.session_state.get("phase") in ("pre_survey", "task", "post_survey", "consent"):
    with st.sidebar:
        st.markdown("---")
        if st.button("🚪 참여 종료", key="withdraw_btn", use_container_width=True):
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
- 소속: 경희대학교 경영학과
- 이메일: obkwon@khu.ac.kr

또한 연구 참여와 관련된 권리 보호에 관한 문의는 소속 기관의 생명윤리심의위원회(IRB)로 문의하실 수 있습니다.

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

# ── PHASE 1: 사전 설문 (표3 조절변수) ──
elif st.session_state.phase == "pre_survey":
    st.title("📋 사전 설문")
    st.caption("모든 정보는 익명 처리됩니다.")
    with st.form("pre_form"):
        st.markdown("#### 1. 기본 정보")
        col1, col2 = st.columns(2)
        gender = col1.selectbox("성별", ["남성","여성","기타","응답 거부"])
        age_group = col2.selectbox("연령대", ["20대","30대","40대","50대 이상"])
        ai_exp = st.selectbox("생성형 AI (ChatGPT, Claude 등) 사용 경험",
                              ["없음","가끔 사용 (월 1~2회)","자주 사용 (주 1~2회)","매일 사용"])
        ai_6mo = st.selectbox("최근 6개월 내 생성형 AI 사용 경험이 있습니까?", ["예","아니오"])

        st.markdown("#### 2. 평소 사고 및 판단 습관 (1=전혀 그렇지 않다 / 7=매우 그렇다)")

        st.markdown("**근거 기반 판단 성향(System 2 Thinking)**")
        ebr1 = likert7("ebr1", "나는 판단 시 근거를 중요하게 고려한다.")
        ebr2 = likert7("ebr2", "나는 데이터를 기반으로 결론을 내리려 한다.")
        ebr3 = likert7("ebr3", "나는 주장에 대한 객관적 증거를 확인한다.")
        ebr4 = likert7("ebr4", "나는 직관보다 근거를 우선시한다.")

        st.markdown("**AI 환각 인식(Hallucination proneness on AI)**")
        aha1 = likert7("aha1", "일반적으로 AI는 틀린 정보를 생성할 수 있다고 생각한다.")
        aha2 = likert7("aha2", "일반적으로 AI의 결과를 그대로 신뢰하지 않는다.")
        aha3 = likert7("aha3", "AI의 출력은 대체로 검증이 필요하다고 생각한다.")
        aha4 = likert7("aha4", "AI는 사실과 다른 내용을 만들 수 있다고 본다.")

        st.markdown("**과신 편향(Overconfidence)**")
        oc1 = likert7("oc1", "나는 자신의 이상과 원칙에 대한 충성이 '개방적 사고'보다 더 중요하다고 생각한다.")
        oc2 = likert7("oc2", "나는 사람들을 나를 지지하는 사람과 그렇지 않은 사람으로 구분하는 경향이 있다.")
        oc3 = likert7("oc3", "자신의 생각을 바꾸는 것은 약함의 표시라고 생각한다.")
        oc4 = likert7("oc4", "나는 의사결정을 할 때, 그것이 논리적으로 타당한지보다 '옳다고 느끼는 것'이 더 중요하다.")
        oc5 = likert7("oc5", "내가 내리려는 결론에 반하는 증거는 크게 고려하지 않는 경향이 있다.")
        oc6 = likert7("oc6", "증거가 내 판단에 반하더라도 나의 판단을 믿는 것이 중요하다.")

        st.markdown("**정서 중시 성향(Beliefs about the Functionality of Emotion)**")
        emo1 = likert7("emo1", "감정은 그 사람의 삶의 방향을 정하는데 영향을 준다.")
        emo2 = likert7("emo2", "인간의 다양한 감정은 삶을 더욱 흥미롭게 만든다.")
        emo3 = likert7("emo3", "나는 감정을 느끼는 것이 건강하다고 믿는다.")
        emo4 = likert7("emo4", "나는 감정을 통해 배운다.")
        emo5 = likert7("emo5", "나는 나의 감정이 판단에 영향을 준다고 느낀다.")

        submitted = st.form_submit_button("다음으로", use_container_width=True, type="primary")

    if submitted:
        if ai_6mo == "아니오":
            st.warning("본 실험은 최근 6개월 내 생성형 AI 사용 경험이 있는 분을 대상으로 합니다. 참여에 감사드립니다.")
            st.stop()

        auto_id = "P_" + uuid.uuid4().hex[:8].upper()
        st.session_state.participant_id = auto_id
        st.session_state.pre_survey_data = {
            "gender": gender, "age_group": age_group, "ai_experience": ai_exp,
            "ebr": {"ebr1":ebr1,"ebr2":ebr2,"ebr3":ebr3,"ebr4":ebr4},
            "ebr_mean": round((ebr1+ebr2+ebr3+ebr4)/4, 2),
            "aha": {"aha1":aha1,"aha2":aha2,"aha3":aha3,"aha4":aha4},
            "aha_mean": round((aha1+aha2+aha3+aha4)/4, 2),
            "oc": {"oc1":oc1,"oc2":oc2,"oc3":oc3,"oc4":oc4,"oc5":oc5,"oc6":oc6},
            "oc_mean": round((oc1+oc2+oc3+oc4+oc5+oc6)/6, 2),
            "emo": {"emo1":emo1,"emo2":emo2,"emo3":emo3,"emo4":emo4,"emo5":emo5},
            "emo_mean": round((emo1+emo2+emo3+emo4+emo5)/5, 2),
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

# ── PHASE 2: 과제 수행 ──
elif st.session_state.phase == "task":
    run_task_phase()

# ── PHASE 3: 사후 설문 (표3 전체) ──
elif st.session_state.phase == "post_survey":
    st.title("📋 사후 설문")
    st.caption("AI와의 대화 경험을 돌아보며 응답해 주세요.")

    with st.form("post_form"):

        # ── 인지된 브레히트적 소외효과(Brechtian Alienation Effect) — 조작점검
        st.markdown("**인지된 브레히트적 소외효과(Brechtian Alienation Effect)**")
        st.caption("조작점검(manipulation check)")
        bae1 = likert7("bae1", "이 시스템은 내 생각을 낯설게 바라보게 하였다.")
        bae2 = likert7("bae2", "이 시스템은 나의 판단을 한 발 떨어져서 보게 했다.")
        bae3 = likert7("bae3", "이 시스템은 내 사고를 비판적으로 검토하게 했다.")
        bae4 = likert7("bae4", "이 시스템은 나의 자동적 반응을 멈추고 생각하게 했다.")

        st.divider()

        # ── 메타인지(meta-cognition) — 전반
        st.markdown("**메타인지(meta-cognition)**")
        mc1 = likert7("mc1", "시스템과 대화하면서 나는 내가 하려는 목표에 잘 부합해가고 있는지 점검할 수 있었다.")
        mc2 = likert7("mc2", "시스템과 대화하면서 나는 내 생각의 오류를 인식하고 수정하기 위해 내 지적 노력을 수반했다.")
        mc3 = likert7("mc3", "시스템과 대화하면서 스스로 나의 문제를 해결하기 위한 여러 옵션을 구사하는지 점검하게 되었다.")
        mc4 = likert7("mc4", "시스템과 대화하면서 나는 내 생각이 맞는지 스스로 자문할 수 있었다.")

        # ── 인지적 거리두기(self-distancing)
        st.markdown("**인지적 거리두기(self-distancing)**")
        sd1 = likert7("sd1", "시스템을 사용하면서 나는 내 생각을 제3자의 시각에서 바라볼 수 있었다.")
        sd2 = likert7("sd2", "시스템을 사용하면서 나는 내 판단에 거리감을 두고 평가할 수 있었다.")
        sd3 = likert7("sd3", "시스템을 사용하면서 나는 한걸음 물러나 감정과 판단을 분리하려 노력할 수 있었다.")
        sd4 = likert7("sd4", "시스템을 사용하면서 나는 내 생각을 객관적으로 검토할 수 있었다.")
        sd5 = likert7("sd5", "시스템을 사용하면서 나는 내 판단이 일시적일 수 있다고 생각할 수 있었다.")

        # ── 현실 모니터링(source monitoring)
        st.markdown("**현실 모니터링(source monitoring)**")
        sm1 = likert7("sm1", "시스템을 사용하면서 나는 내가 획득한 정보가 어디에서 왔는지 확인하려고 하였다.")
        sm2 = likert7("sm2", "시스템을 사용하면서 나는 사실과 막연한 추론을 구분하려고 했다.")
        sm3 = likert7("sm3", "시스템을 사용하면서 나는 사실과 상상을 구별할 수 있었다.")
        sm4 = likert7("sm4", "시스템을 사용하면서 나는 내 판단의 근거가 되는 정보의 출처를 돌아보았다.")

        # ── 반사실적 사고(counterfactual thinking)
        st.markdown("**반사실적 사고(counterfactual thinking)**")
        cf1 = likert7("cf1", "시스템을 사용하면서 나는 내 생각 외 다른 가능성을 고려할 수 있었다.")
        cf2 = likert7("cf2", "시스템을 사용하면서 정답은 나의 원래의 생각과 다를 수 있음을 생각해볼 수 있었다.")
        cf3 = likert7("cf3", "시스템을 사용하면서 나는 다양한 해석을 시도할 수 있었다.")
        cf4 = likert7("cf4", "시스템을 사용하면서 나는 하나의 결론에 쉽게 확신하는 오류를 줄일 수 있었다.")

        # ── 지적 겸손(intellectual humility)
        st.markdown("**지적 겸손(intellectual humility)**")
        ih1 = likert7("ih1", "시스템을 사용하면서 나는 많은 경우에 다른 의견에 대해서 배워야 함을 알게 되었다.")
        ih2 = likert7("ih2", "시스템을 사용하면서 나는 어떤 의견을 가지려고할 때 종종 내 생각이 틀릴 수도 있음을 알게 되었다.")
        ih3 = likert7("ih3", "시스템을 사용하면서 나는 합당한 이유가 있다면 내 견해를 수정할 의향을 가지게 되었다.")
        ih4 = likert7("ih4", "시스템을 사용하면서 나는 비록 몇몇 부분은 동의하지 않더라도 다른 이의 의견을 귀담아 들으려는 의지가 생겼다.")

        st.divider()

        # ── 환각지수(Hallucination Index) 자기보고
        st.markdown("#### 환각지수(Hallucination Index) — 자기보고")

        # ── 확신 대비 근거 부족(lack of reasoning)
        st.markdown("**확신 대비 근거 부족(lack of reasoning)**")
        lr1 = likert7("lr1", "실험하는 동안 나에게 충분한 근거 없이도 결론을 내리는 경우가 있음을 알게 되었다.")
        lr2 = likert7("lr2", "실험하는 동안 내가 근거가 부족해도 확신을 가짐을 알게 되었다.")
        lr3 = likert7("lr3", "실험하는 동안 나의 주장에 대한 근거를 명확히 제시하지 못하는 경우가 있었다.")
        lr4 = likert7("lr4", "실험하는 동안 나는 설명 없이 결론을 내리는 경우가 있었다.")
        lr5 = likert7("lr5", "실험하는 동안 내 판단의 근거를 충분히 점검하지 않은 적이 있다.")

        # ── 출처 모호성(Lack of Faithfulness)
        st.markdown("**출처 모호성(Lack of Faithfulness)**")
        lf1 = likert7("lf1", "실험하는 동안 나는 정보의 출처를 명확히 인식하지 못할 때가 있었다.")
        lf2 = likert7("lf2", "실험하는 동안 나는 어디서 얻은 정보인지 혼동하기도 했다.")
        lf3 = likert7("lf3", "실험하는 동안 나는 출처를 확인하지 않는 경우가 있었다.")
        lf4 = likert7("lf4", "실험하는 동안 나는 정보의 신뢰성을 검증하지 않는 경우가 있었다.")

        # ── 정서 기반 판단(Affect Heuristic)
        st.markdown("**정서 기반 판단(Affect Heuristic)**")
        ah1 = likert7("ah1", "실험하는 동안 감정에 따라 판단이 달라진 적이 있다.")
        ah2 = likert7("ah2", "실험하는 동안 내가 좋아하는 정보에 더 확신을 가진 것을 알게 되었다.")
        ah3 = likert7("ah3", "실험하는 동안 내가 감정적으로 판단하는 경우가 있음을 알게 되었다.")
        ah4 = likert7("ah4", "실험하는 동안 나는 기분이 판단에 영향을 주는 것을 알게 되었다.")

        # ── 비일관된 판단(Inconsistency)
        st.markdown("**비일관된 판단(Inconsistency)**")
        ic1 = likert7("ic1", "실험하는 동안 나는 상황에 따라 판단이 달라지기도 함을 알게 되었다.")
        ic2 = likert7("ic2", "실험하는 동안 나는 이전 판단과 다른 결론을 내리기도 함을 알게 되었다.")
        ic3 = likert7("ic3", "실험하는 동안 나는 일관된 기준을 유지하기 어려움을 알게 되었다.")
        ic4 = likert7("ic4", "실험하는 동안 나는 판단 기준이 변하였다.")

        st.divider()

        # ── 공동창출 의향
        st.markdown("**공동창출 의향**")
        ci1 = likert7("ci1", "나는 GenAI를 활용하여 나의 목적에 맞는 결과를 만들기 위해 적극적으로 상호작용할 의향이 있다.")
        ci2 = likert7("ci2", "나는 GenAI의 작동 방식을 이해하기 위해 지속적으로 시도하고 학습하려 한다.")
        ci3 = likert7("ci3", "나는 더 나은 결과를 위해 GenAI에 추가적인 정보(맥락, 요구사항 등)를 제공할 의향이 있다.")
        ci4 = likert7("ci4", "나는 GenAI가 생성한 결과를 나의 아이디어에 맞게 수정하고 개선하려 한다.")
        ci5 = likert7("ci5", "나는 더 나은 결과를 얻기 위해 시간과 노력을 투자할 의향이 있다.")
        ci6 = likert7("ci6", "나는 GenAI와 협력하여 결과물을 공동으로 발전시키려 한다.")
        ci7 = likert7("ci7", "나는 GenAI를 활용하여 나의 문제 해결 과정에 적극적으로 참여하려 한다.")

        # ── 공동창출 효과
        st.markdown("**공동창출 효과**")
        ce1 = likert7("ce1", "나는 GenAI로부터 결과 개선을 위한 유용한 피드백을 제공받았다.")
        ce2 = likert7("ce2", "나는 GenAI를 활용하여 나의 생각을 더욱 명확히 표현할 수 있었다.")
        ce3 = likert7("ce3", "나는 GenAI를 통해 문제 해결 방안을 스스로 탐색할 수 있었다.")
        ce4 = likert7("ce4", "나는 GenAI가 제공하는 결과에 적극적으로 반응할 수 있었다.")
        ce5 = likert7("ce5", "나는 GenAI와의 상호작용을 통해 더욱 좋은 해결책을 만들어냈다.")
        ce6 = likert7("ce6", "나는 GenAI 결과의 도움으로 반복적으로 수정 보완하며 발전시킬 수 있었다.")
        ce7 = likert7("ce7", "나는 GenAI를 활용하여 이전보다 더 나은 결과를 만들어냈다.")

        st.divider()

        # ── 창의성 지수(Index of Creativity) + 확신도 + 성찰
        st.markdown("**창의성 지수(Index of Creativity)**")
        task_key = st.session_state.get("task_key","creative")
        post_conf = st.slider("최종 결과물이 얼마나 완벽하다고 생각하시나요? (%)", 0, 100, 50, key="post_conf")
        final_title = st.text_input(TASK_INFO[task_key]["final_label"], key="final_title")
        final_reason = st.text_area("최종 판단 이유", height=80, key="final_reason")
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
            "timestamp": datetime.datetime.now().isoformat(),

            # 사후설문 데이터
            "post_survey": {
                "manipulation_check": {
                    "bae": {"bae1":bae1,"bae2":bae2,"bae3":bae3,"bae4":bae4},
                    "bae_mean": round((bae1+bae2+bae3+bae4)/4, 2)},
                "metacognition": {
                    "general": {"mc1":mc1,"mc2":mc2,"mc3":mc3,"mc4":mc4},
                    "mc_mean": round((mc1+mc2+mc3+mc4)/4, 2),
                    "cognitive_distancing": {"sd1":sd1,"sd2":sd2,"sd3":sd3,"sd4":sd4,"sd5":sd5},
                    "sd_mean": round((sd1+sd2+sd3+sd4+sd5)/5, 2),
                    "source_monitoring": {"sm1":sm1,"sm2":sm2,"sm3":sm3,"sm4":sm4},
                    "sm_mean": round((sm1+sm2+sm3+sm4)/4, 2),
                    "counterfactual": {"cf1":cf1,"cf2":cf2,"cf3":cf3,"cf4":cf4},
                    "cf_mean": round((cf1+cf2+cf3+cf4)/4, 2),
                    "intellectual_humility": {"ih1":ih1,"ih2":ih2,"ih3":ih3,"ih4":ih4},
                    "ih_mean": round((ih1+ih2+ih3+ih4)/4, 2),
                },
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
                "co_creation_intention": {"ci1":ci1,"ci2":ci2,"ci3":ci3,"ci4":ci4,"ci5":ci5,"ci6":ci6,"ci7":ci7},
                "ci_mean": round((ci1+ci2+ci3+ci4+ci5+ci6+ci7)/7, 2),
                "co_creation_effect": {"ce1":ce1,"ce2":ce2,"ce3":ce3,"ce4":ce4,"ce5":ce5,"ce6":ce6,"ce7":ce7},
                "ce_mean": round((ce1+ce2+ce3+ce4+ce5+ce6+ce7)/7, 2),
                "creativity": {"originality":cr_orig,"usefulness":cr_use,"fit":cr_fit},
                "creativity_index": round((cr_orig+cr_use+cr_fit)/3, 2),
                "confidence": {"pre":pre_conf,"post":post_conf,"change":post_conf-pre_conf},
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
        fname = f"results/{st.session_state.participant_id}_{st.session_state.get('cell','X')}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

# ★ CSV 누적 저장 (실험 흐름순 정렬 및 한글 매핑 완결판)
        import csv
        csv_path = "results/all_results_korean.csv" 
        
        # 1. 원본 경로 -> 한글 변수명 매핑 (빠짐없이 수록)
        RAW_MAPPING = {
            # [1] 인트로 & 시스템 정보
            "participant_id": "[시스템] 참가자_ID",
            "timestamp": "[시스템] 참여_일시",
            "experiment_design.cell": "[시스템] 배정_셀",
            "experiment_design.group": "[시스템] 실험집단",
            "experiment_design.task_type": "[시스템] 과제유형",
            "experiment_design.api_model": "[시스템] 사용_모델",
            "experiment_design.design": "[시스템] 실험_설계",
            "task_order": "[시스템] 과제_수행_순서",
            "fixed_cycles": "[시스템] 고정_대화_턴수",

            # [2] 사전 설문 (기본 정보 및 심리 척도)
            "pre_survey.gender": "[사전] 성별",
            "pre_survey.age_group": "[사전] 연령대",
            "pre_survey.ai_experience": "[사전] AI 경험",
            "nfc1": "[사전_NFC1] 복잡한 문제 선호",
            "nfc2": "[사전_NFC2] 깊은 사고 선호",
            "nfc3": "[사전_NFC3] 사고 과정 즐김",
            "nfc_mean": "[사전_NFC_평균]",
            "pre_survey.ebr.ebr1": "[사전_근거1] 근거 중요성",
            "pre_survey.ebr.ebr2": "[사전_근거2] 데이터 기반",
            "pre_survey.ebr.ebr3": "[사전_근거3] 증거 확인",
            "pre_survey.ebr.ebr4": "[사전_근거4] 직관보다 근거",
            "pre_survey.ebr_mean": "[사전_근거_평균]",
            "pre_survey.aha_mean": "[사전_AI환각인식_평균]",
            "pre_survey.oc_mean": "[사전_과신편향_평균]",
            "pre_survey.emo_mean": "[사전_정서중시_평균]",

            # [3] 과제 및 실험 개입 시작
            "algorithm_data.initial_input": "[과제] 최초 주장 내용",
            "pre_confidence": "[실험] 사전 확신도",
            "algorithm_data.initial_hi.Hallucination_Index": "[알고리즘] 초기 환각지수(HI)",

            # [4] 사후 설문 (반응 및 측정)
            "post_survey.manipulation_check.bae_mean": "[사후] 소외효과 조작점검 평균",
            "post_confidence": "[실험] 사후 확신도",
            "post_survey.metacognition.mc_mean": "[사후] 메타인지(전반) 평균",
            "post_survey.metacognition.sd_mean": "[사후] 메타인지(거리두기) 평균",
            "post_survey.metacognition.sm_mean": "[사후] 메타인지(출처모니터링) 평균",
            "post_survey.metacognition.cf_mean": "[사후] 메타인지(반사실적사고) 평균",
            "post_survey.metacognition.ih_mean": "[사후] 메타인지(지적겸손) 평균",
            "post_survey.hallucination_self_report.lr_mean": "[사후] 자기보고 환각(근거부족) 평균",
            "post_survey.hallucination_self_report.lf_mean": "[사후] 자기보고 환각(출처모호) 평균",
            "post_survey.hallucination_self_report.ah_mean": "[사후] 자기보고 환각(정서판단) 평균",
            "post_survey.hallucination_self_report.ic_mean": "[사후] 자기보고 환각(비일관성) 평균",
            "post_survey.ci_mean": "[사후] 공동창출 의향 평균",
            "post_survey.ce_mean": "[사후] 공동창출 효과 평균",

            # [5] 완료 및 최종 결과
            "post_survey.final_title": "[최종] 결과물 제목",
            "post_survey.final_reason": "[최종] 판단 이유",
            "reflection_text": "[최종] 주관적 성찰 기록",
            "creativity_originality": "[결과] 독창성",
            "creativity_usefulness": "[결과] 유용성",
            "creativity_fit": "[결과] 적합성",
            "post_survey.creativity_index": "[결과] 창의성 종합지수",

            # [6] 나머지 분석 지표 및 로그 (뒤로 배치)
            "algorithm_data.hi_change": "[분석] 환각지수 감소량",
            "post_survey.confidence.change": "[분석] 확신도 변화량",
            "algorithm_data.final_hi.Hallucination_Index": "[알고리즘] 최종 환각지수(HI)",
            "algorithm_data.transcript": "[로그] 대화 전체 기록"
        }

        # 2. 📊 엑셀 칼럼 나열 순서 정의 (요청하신 순서대로 배치)
        COLUMN_ORDER = [
            "[시스템] 참가자_ID", "[시스템] 참여_일시", "[시스템] 배정_셀", "[시스템] 실험집단", "[시스템] 과제유형", "[시스템] 사용_모델",
            "[사전] 성별", "[사전] 연령대", "[사전] AI 경험", "[사전_NFC_평균]", "[사전_근거_평균]", "[사전_AI환각인식_평균]", "[사전_과신편향_평균]", "[사전_정서중시_평균]",
            "[과제] 최초 주장 내용", "[시스템] 과제_수행_순서", "[실험] 사전 확신도", "[알고리즘] 초기 환각지수(HI)",
            "[사후] 소외효과 조작점검 평균", "[실험] 사후 확신도", "[사후] 메타인지(전반) 평균", "[사후] 메타인지(거리두기) 평균", "[사후] 메타인지(출처모니터링) 평균", "[사후] 메타인지(반사실적사고) 평균", "[사후] 메타인지(지적겸손) 평균",
            "[사후] 자기보고 환각(근거부족) 평균", "[사후] 자기보고 환각(출처모호) 평균", "[사후] 자기보고 환각(정서판단) 평균", "[사후] 자기보고 환각(비일관성) 평균",
            "[사후] 공동창출 의향 평균", "[사후] 공동창출 효과 평균",
            "[최종] 결과물 제목", "[최종] 판단 이유", "[최종] 주관적 성찰 기록", "[결과] 독창성", "[결과] 유용성", "[결과] 적합성", "[결과] 창의성 종합지수",
            "[분석] 환각지수 감소량", "[분석] 확신도 변화량", "[알고리즘] 최종 환각지수(HI)", "[로그] 대화 전체 기록"
        ]

        def _flatten_and_map(d, prefix=""):
            items = {}
            for k, v in d.items():
                full_path = f"{prefix}{k}" if prefix else k
                if isinstance(v, dict):
                    items.update(_flatten_and_map(v, full_path + "."))
                else:
                    # 1순위: 전체 경로 매핑 / 2순위: 단어 매핑 / 3순위: 원래 키
                    mapped_key = RAW_MAPPING.get(full_path, RAW_MAPPING.get(full_path.split('.')[-1], full_path))
                    items[mapped_key] = json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
            return items

        flat = _flatten_and_map(result)
        file_exists = os.path.exists(csv_path)
        with open(csv_path, "a", newline="", encoding="utf-8-sig") as cf:
            # COLUMN_ORDER에 정의된 순서대로만 저장하며, 정의되지 않은 변수는 무시하거나 뒤에 추가 가능
            writer = csv.DictWriter(cf, fieldnames=COLUMN_ORDER, extrasaction='ignore')
            if not file_exists:
                writer.writeheader()
            writer.writerow(flat)

        # ★ Google Sheets에도 저장 (클라우드 배포 시 데이터 보존)
        try:
            ws = _get_worksheet("results")
            if ws:
                existing = ws.get_all_values()
                if not existing:
                    ws.append_row(COLUMN_ORDER)
                row = [str(flat.get(col, "")) for col in COLUMN_ORDER]
                ws.append_row(row)
        except Exception as e:
            pass  # Google Sheets 실패해도 로컬 CSV는 이미 저장됨

        st.session_state.saved_result = result
        st.session_state.phase = "done"
        st.rerun()

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

- 연구책임자: 권오병 교수
- 소속: 경희대학교 경영학과
- 이메일: obkwon@khu.ac.kr
    """)
