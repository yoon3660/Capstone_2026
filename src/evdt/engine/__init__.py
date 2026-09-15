"""배정 엔진 — 본 프로젝트의 기여 부분.

채워질 모듈:
    policy.py    스테이지별 Policy 구현 (UE, S0~S4, MEC, ORACLE)   (T-18 ~)
    ledger.py    예약 원장 (§5.4)                                   Sprint 2
    mec.py       한계 외부비용 계산 (§6.1)                           Sprint 3
    forecast.py  미래 도착 예측                                      Sprint 3
    milp.py      Rolling Horizon MILP (PuLP + HiGHS)                Sprint 3

engine 은 world 를 임포트해도 된다 (반대 방향만 금지). 다만 큐 규칙은
world.queue_rule 의 **같은 함수**를 호출해야 한다 (설계 규칙 1).
두 곳에 따로 구현하면 원장이 틀린 미래를 예측하고 외부비용이 전부 오염된다.
"""
