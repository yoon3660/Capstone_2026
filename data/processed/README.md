# data/processed — 전처리 산출물

`data/raw/` 를 가공해 시뮬레이션이 바로 읽을 수 있는 형태로 만든 파일.
**원본에서 스크립트로 언제든 재생성 가능해야 한다.** 손으로 고친 파일을 여기 두지 말 것.

## 시나리오 config 가 기대하는 파일

| 파일 | 만드는 티켓 | 쓰는 곳 |
|---|---|---|
| `volume_gyeongbu_down_seollal.csv` | T-07 | `config/scenario_seollal_down.yaml` |
| `volume_gyeongbu_up_seollal.csv` | T-07 | `config/scenario_seollal_up.yaml` |
| `volume_gyeongbu_down_weekend.csv` | T-07 | `config/scenario_weekend_base.yaml` |

## 교통량 프로파일 형식 (T-07 산출물, T-12 입력)

```csv
hour,section_id,offset_km_start,offset_km_end,volume_veh_h
0,gyeongbu_d_001,0.0,12.4,1820
1,gyeongbu_d_001,0.0,12.4,1240
...
```

- `hour` 는 0~23. 시뮬레이션 시각(분)으로는 `hour * 60`.
- `volume_veh_h` 는 **전체 차량** 수다. EV 비중은 config 의 `demand.ev_share` 로 곱한다.
- 비동질 포아송 과정의 도착률 λ(t) 가 여기서 나온다 (T-12).
