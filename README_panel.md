# BMDM 패널 연동판 (app_panel.py) 안내

## 본판(app.py)과의 차이
- 사전 설문, IRB 동의서, 사후 설문 화면 **제거** → 업체(엠브레인) 설문에서 수행
- 흐름: **인트로 → 과제(AI 대화 6회) → 최종 제출(제목·판단 이유·완벽도) → 완료(복귀 버튼)**
- 접속 URL의 `panel_id` 를 참가자 ID로 저장
- 완료/중도종료/정원마감 시 업체 복귀 링크로 `panel_id` 와 `status` 전달 (정원마감 접속도 상태코드와 함께 1행 기록)
- 4개 셀(A: BMDM+분석, B: BMDM+창의, C: 통제+분석, D: 통제+창의) 중 **시작 버튼을 누를 때 무작위 배정**, 참가자에게 조건 비노출
- 같은 `panel_id` 가 완료 후 다시 들어오면 과제를 다시 시키지 않고 완료 화면(복귀 버튼)으로 보냄
- 구글시트 탭: `panel_results`, `panel_cell_counts` (본판의 `results`, `cell_counts` 와 분리)
- 저장 칼럼 35개 (사전·사후 설문 칼럼 없음, `[시스템] 패널_ID`, `[시스템] 복귀_상태값` 추가)
- 중도 종료자도 `복귀_상태값 = 아웃코드` 로 1행 저장됨 (셀 카운트에는 미집계) → 업체 아웃 집계와 대조용

## 배포
1. GitHub 저장소에 `app_panel.py` 추가 (app.py 와 같은 폴더에 두어도 됨)
2. Streamlit Community Cloud 에서 **새 앱** 생성, Main file path = `app_panel.py`
3. Secrets 에 `secrets_panel.example.toml` 내용을 채워 붙이기 (복귀 주소·상태코드는 업체 확정값으로)
4. 배포 주소 확인 → 업체에 전달하는 참여 링크: `https://<앱주소>/?panel_id=[ID]`
5. 관리자 확인: 사이드바 비밀번호 입력 → "셀 직접 시작"으로 각 셀 점검. 완료 화면에서 생성된 복귀 URL이 그대로 표시됨.

## 업체에 전달할 내용 (예시 문안)
- 참여 링크 형식: `https://<앱주소>/?panel_id=[ID]`  ※ 기존 파라미터가 없으므로 `&` 가 아니라 `?` 로 시작
- 과제 내용: AI와 6회 대화하며 글을 작성하는 과제 1개, 약 10분, 한국어, PC·모바일 모두 가능
- 복귀 방식: 플랫폼 완료 화면에서 **"설문으로 돌아가기" 버튼**을 누르면 아래 주소로 이동
  (Streamlit 구조상 자동 이동은 불가 → 버튼 클릭 방식이 허용되어야 함)
  - 완료: `복귀주소?panel_id=[ID]&status=완료코드`
  - 중도 종료(참여 종료 버튼): `복귀주소?panel_id=[ID]&status=아웃코드`
  - 정원 마감으로 참여 불가: `복귀주소?panel_id=[ID]&status=쿼터아웃코드`  ← **이 코드도 받아야 함**
- 브라우저를 그냥 닫은 이탈자는 신호를 보낼 수 없음 → 업체 쪽에서 "미복귀 = 아웃" 처리 여부 확인
- 쿼터: 4개 조건 × 75명 = 300명. 발송은 나눠서(예: 50명 단위) 해 주면 서버 부하와 정원 초과를 줄일 수 있음

## 로컬 테스트
```bash
cd <app_panel.py 폴더>
mkdir -p .streamlit && cp secrets_panel.example.toml .streamlit/secrets.toml   # 값 채우기
streamlit run app_panel.py
# 브라우저: http://localhost:8501/?panel_id=TEST001
```
secrets 없이 실행해도 앱은 뜨지만 Claude 도입문장 생성과 구글시트 저장은 건너뛴다(로컬 JSON/CSV 는 저장됨).
