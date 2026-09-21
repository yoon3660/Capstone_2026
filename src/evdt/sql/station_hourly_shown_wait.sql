-- 휴게소별 · 시각(시)별 평균 "표시 대기" — 그 시각에 도착했다면 기다렸을 시간 (스냅샷 wait_min)
-- S0(UE) 가 보고 고르는 값이다. 실제 대기(station_hourly_wait)와 비교하면 표시가 얼마나 빗나갔는지 보인다.
-- 스냅샷은 격자로 찍히므로 차가 없는 시간대도 행이 있다 (값 0).
SELECT run_id, entity_id AS station_id, CAST(FLOOR(t_min / 60) AS INTEGER) AS hour, COUNT(*) AS n_snap, AVG(value) AS mean_shown_wait_min FROM snapshot WHERE entity_type = 'station' AND state = 'wait_min' GROUP BY ALL ORDER BY ALL
