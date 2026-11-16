사다리 결과 CSV 묶음
========================================================================

무대: 2026 추석 2026-09-25 경부선 UP — 혼잡 시나리오 (EV 15%, ⚠ 실측 비율 아님)
  scenario_id        chuseok_2026_up_congested
  config             config/scenario_chuseok_up_congested.yaml
  기간 · 방향         chuseok2026 · UP
  수요 프로파일       data/processed/issue146_chuseok/2026-09-25/UP/volume.csv
  기온               22.18°C
  EV 레이어          2026 추석 연휴 (chuseok2026) · 가정: EV 보급률 15% · 기회 충전 20% (도착 SoC<50%)
  우회 비용           120분 (산포 sigma 0)
  포기(renege)       True
  인내               uniform(shape 0.142857) ·
                     중앙값 70분
  통행시간           fixed · 80 km/h
  시드               [np.int64(1), np.int64(2), np.int64(3), np.int64(4), np.int64(5), np.int64(6), np.int64(7), np.int64(8), np.int64(9), np.int64(10)]
  커밋               4744dd2

파일
  ladder_seeds.csv     시드 × 칸 한 줄씩 — **원자료**. 다시 집계할 때 이걸 쓴다
  ladder_summary.csv   칸별 평균 · S0 대비 개선율 · 95% 신뢰구간 · 이긴 시드
  ladder_steps.csv     계단별(S0→S1, S1→S2 …) 짝지은 차이와 신뢰구간
  run_params.json      무대 설정을 기계가 읽을 수 있게

핵심 열
  total_social_cost_h   **총 사회적 비용** = 체류 + 이탈 비용 + 포기 전 대기.
                        칸 비교는 **이 열로만** 한다
  dwell_total_h         총 체류
  escape_cost_total_h   이탈 비용 합 (차마다 제 우회 비용을 더한 값)
  renege_wait_total_h   포기하기 전까지 기다린 시간 합
  n_escaped             이탈 합계 = balked + reneged + stranded
    _balked             줄이 길어서 애초에 안 섰다
    _reneged            줄에서 기다리다 인내가 끝나 떠났다
    _stranded           정책이 계획 불가인 곳으로 몰아넣었다
  bottleneck_slots      포화된 (휴게소 × 시간칸) 수
  served_mean           충전 마친 차 = n_ev_charging − n_escaped

신뢰구간 읽는 법
  시드별로 **짝지어** 차이를 내고 그 평균의 95% 구간이다.
  구간이 0 을 포함하면 **"동점"** 이라고 쓴다. "조금 낫다" 라고 쓰지 않는다.

========================================================================
⚠ 이 표로 하면 안 되는 것
========================================================================

1. **평균 대기로 칸을 비교하지 말 것.**
   S0 는 가장 긴 줄로 갈 차를 가장 많이 흘린다. 그래서 S0 의 평균 대기는
   **생존자 평균**이고, 더 많은 차를 붙잡은 칸보다 좋아 보인다.
   총 사회적 비용과 `served_mean` 으로만 비교한다.

2. **다른 인내·이탈 설정끼리 총비용을 비교하지 말 것.**
   설정이 바뀌면 충전 마친 차 수가 바뀐다 — 모집단이 다른 두 수를 견주는 것이
   된다. 이 묶음은 **한 설정 안에서만** 유효하다.

3. **'바닥–천장 격차 축소율(구간의 N%)' 을 쓰지 말 것.**
   바닥과 천장의 이탈 대수가 다르면 분모가 같은 모집단이 아니다. 지금 무대는
   모든 칸이 그 허용(5대)을 넘는다.

4. **민폐(한계 외부비용) 합계를 총비용과 섞지 말 것.**
   한계값은 더해지지 않는다. 합계가 총 체류의 몇 배로 나온다
   (`scripts/externality_rank.py` 머리말 참조).

5. **완전예측은 S2 의 천장이지 S4 의 천장이 아니다.**
   `runner.py` 에서 `PERFECT_ROLLING` 은 S2Policy 로 떨어진다 — **S2 구조 +
   완전한 미래**다. S4 가 그걸 넘어도 고장이 아니다.

========================================================================
다시 집계하려면 (pandas 만 있으면 된다)
========================================================================

    import pandas as pd
    t = pd.read_csv("ladder_seeds.csv")
    w = t.pivot(index="seed", columns="stage", values="total_social_cost_h")
    imp = (w["S0"] - w["S4"]) / w["S0"] * 100      # 시드별 개선율
    print(imp.mean(), imp.std(ddof=1) / len(imp) ** .5 * 1.96)
