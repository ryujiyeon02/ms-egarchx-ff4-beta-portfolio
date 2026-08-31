# 합성 샘플 데이터

공개 저장소에는 원본 가격·시가총액·거래량·요인 데이터가 포함되지 않습니다. 이 폴더의 CSV는 입력 열 구조를 설명하기 위해 만든 합성 데이터이며, README에 공개된 백테스트 결과에는 사용하지 않았습니다.

```bash
python scripts/generate_sample_data.py
```

| 샘플 | 원본 입력의 논리 구조 |
| --- | --- |
| `prices_sample.csv` | `Date`와 종목별 수정주가 열 |
| `market_cap_sample.csv` | `Date`와 종목별 시가총액 열 |
| `kospi200_volume_sample.csv` | `Date`, `KOSPI 200` 거래량 |
| `ff4_factors_sample.csv` | `Date`, `KOSPI200`, `HML`, `SMB`, `MOM`, `CD(91)` |

전략을 재실행하려면 적법하게 확보한 데이터를 동일한 열 구조로 준비한 뒤 `src/ai_beta_reweight_backtest.py`의 파일명 상수에 연결해야 합니다.
