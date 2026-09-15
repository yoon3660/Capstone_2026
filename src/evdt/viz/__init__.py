"""시각화 — 스냅샷 스트림을 받아 그림으로.

    frames.py  Parquet 스냅샷 → 렌더러 입력 프레임
    plots.py   matplotlib 정적 그림 (Sprint 1: UE 대기 히트맵 — T-20)

⚠ 렌더러는 시뮬레이터를 임포트하지 않는다. 입력은 오직 스냅샷 스트림
   (t_min, entity_type, entity_id, lat, lon, state, value) 이다 (설계 규칙 4).
   그래야 렌더러를 바꿔도 시뮬레이터를 고치지 않는다.
"""
