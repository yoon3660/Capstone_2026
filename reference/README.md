# 못 박아 둔 기준선

`scripts/baseline.py --pin` 이 쓰고 `--check` 가 비교한다.

```bash
python scripts/baseline.py --check      # 돌려서 차이만 본다
python scripts/baseline.py --pin        # 지금 결과를 새 기준으로
```

**결과 파일이 아니라 계약이다.** 여기가 움직이면 무언가 바뀐 것이고, 그게 의도한
것인지 답할 수 있어야 한다. 움직였다고 반드시 나쁜 것은 아니다 — 버그를 고치면
움직인다. 도구는 "움직였다/안 움직였다" 만 말하고, 옳고 그름은 사람이 판단한다.

## 무엇으로 찍었나

`config/scenario_seollal_{down,up}.yaml` 그대로 · **가정 레이어 없음** ·
출발 SoC low · 시드 20개. 기준선은 **재현 그 자체**이므로 아무것도 얹지 않는다.

⚠ 이 값들은 근거 없는 잠정값 여럿 위에 있다 (EV 비중 #67 · 이탈 비용 #64 ·
출발 SoC #55 · 기온 3차). 확정되면 다시 못 박아야 한다.
자세한 것은 `docs/dev_log.md` §7.
