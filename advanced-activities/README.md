# 추가 심화 활동

이 폴더는 `MS와 EGARCH-X로 변동성 레짐 분석 및 FF4_beta_포트폴리오` repo의 확장 실험을 모아두는 공간입니다.

## 포함 파일

| 파일 | 설명 |
| --- | --- |
| `메인전략_ParticipationStabilityTop30_Weekly_EGARCH7030.ipynb` | Participation-Stability Top30 주간 리밸런싱 전략에 EGARCH 기반 시장 비중 조절을 결합한 메인 전략 심화 노트북 |

## 전략 요약

- `EGARCH-X`로 시장 변동성 레짐을 판단합니다.
- 위험 신호는 `5거래일 연속` 확인된 뒤에만 반영합니다.
- 시장 노출은 `100 / 70 / 30`으로 조절합니다.
- 종목은 `Participation-Stability Score` 기준 Top30을 사용합니다.
- 주간 리밸런싱과 동일가중 포트폴리오를 기준으로 성과, turnover, 거래비용 반영 순성과를 확인합니다.

## 실행에 필요한 추가 데이터

이 노트북은 아래 파일을 같은 실행 디렉터리에서 찾도록 작성되어 있습니다.

| 파일 | 비고 |
| --- | --- |
| `1997_2026_코스피코스닥_수정주가_비영업일제외.csv` | 개별 종목 가격 데이터 |
| `1997_2026_KOSPI200, SMB, HML, MOM_종가_91CD_일별_비영업일제외.csv` | FF4 요인과 CD91 금리 |
| `garch_roll_params.csv` | EGARCH 롤링 추정 결과 |
| `ff4_beta_store.parquet` 또는 `ff4_beta_store.pkl.gz` | FF4 beta 저장 파일 |
| `gemini_ai_score_cache_5y.csv` | 선택적으로 사용하는 Gemini 점수 캐시 |

`ff4_beta_store.parquet`와 `ff4_beta_store.pkl.gz`는 100MB를 넘는 중간 산출물이어서 GitHub 일반 파일로는 포함하지 않았습니다.
