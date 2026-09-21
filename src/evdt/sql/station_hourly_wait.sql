-- 휴게소별 · 도착 시각(시)별 평균 대기 — 실제로 기다린 시간 (설계문서 T-17 완료 기준)
-- 차가 한 대도 오지 않은 시간대는 행이 없다 (0 이 아니라 "관측 없음").
SELECT run_id, station_id, CAST(FLOOR(t_arrive_min / 60) AS INTEGER) AS hour, COUNT(*) AS n_ev, AVG(wait_min) AS mean_wait_min FROM charge_event GROUP BY ALL ORDER BY ALL
