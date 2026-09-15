"""디지털 트윈 — CTM 본선 흐름 + 휴게소 충전 Queue (SimPy DES).

Sprint 1 에서 채워질 모듈:
    queue_rule.py  큐 규칙 단일 모듈            (T-15) ⭐ 가장 중요한 설계 티켓
    stations.py    충전소/충전기 런타임 상태     (T-16)
    sim.py         SimPy 시뮬레이션 루프        (T-16)
    dwell.py       충전 점유시간 계산기          (T-14)
Sprint 2:
    ctm.py         Cell Transmission Model      (§9.5 q_max 함정 주의)

⚠ 이 패키지는 evdt.engine 을 임포트하지 않는다 (설계 규칙 2).
   시뮬레이터는 evdt.interfaces.Policy 객체를 주입받을 뿐,
   어떤 스테이지가 도는지 모른다. 이것이 공정 비교의 물리적 보장이다.
"""
