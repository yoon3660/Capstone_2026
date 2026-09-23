"""셀이 코리도 전체(0 ~ 415.058 km)를 빈틈·겹침 없이 덮는지 확인한다 (#51).

    python scripts/check_cells.py

셀은 차로 프로파일(표준노드링크 SHP)이 있는 기기에서만 만들 수 있어서, 각자 DB 가 다를 수 있다.
노선을 415.058 km 로 바로잡은 뒤에도 셀이 옛 노선(392.978 km) 기준이면 뒤쪽 22 km 가 비거나
셀 위치가 어긋난다. 이 스크립트는 DB 의 cell 테이블만 읽는다 (SHP 불필요).

검사
    0. corridor.length_km 가 중심선 기준 415.058 km 다 (DB 전체가 옛 노선이면 1~4 는 모두 통과한다)
    1. 방향마다 첫 셀이 0 km 에서 시작하고, 마지막 셀이 corridor.length_km 에서 끝난다
    2. 이웃 셀 사이에 빈틈·겹침이 없다 (앞 셀 끝 = 뒤 셀 시작)
    3. 셀 길이가 CFL 하한 이상이다 (config/flow_params.yaml 의 dt_min 기준)
    4. 휴게소마다 cell_id 가 있고, 휴게소 기점거리가 그 셀 안에 있다
하나라도 틀리면 종료 코드 1.
"""

from __future__ import annotations

import sys

from _bootstrap import ROOT  # noqa: E402

from evdt.io.db import get_conn  # noqa: E402
from evdt.io.route import EXPECTED_ROUTE_KM, ROUTE_LENGTH_TOL_KM  # noqa: E402
from evdt.paths import default_db_path  # noqa: E402

TOL_KM = 0.01


def main() -> int:
    problems: list[str] = []

    with get_conn(default_db_path(), readonly=True) as conn:
        corridors = conn.execute("SELECT corridor_id, length_km FROM corridor ORDER BY corridor_id").fetchall()

        for corridor_id, length_km in corridors:
            # 셀은 corridor.length_km 를 기준으로 검사한다. 그 값 자체가 옛 노선이면
            # 셀이 아무리 맞아떨어져도 전부 틀린 자리다 (#51).
            if abs(length_km - EXPECTED_ROUTE_KM) > ROUTE_LENGTH_TOL_KM:
                problems.append(
                    f"{corridor_id}: 코리도 길이 {length_km:.3f} km ≠ 중심선 기준 {EXPECTED_ROUTE_KM} km. "
                    "DB 가 옛 노선으로 만들어졌다. build_route.py 부터 다시 만들 것 (io/route.py REBUILD_STEPS)"
                )

            cells = conn.execute(
                "SELECT seq, offset_km_start, offset_km_end, length_km, v_free_kmh, w_back_kmh"
                " FROM cell WHERE corridor_id = ? ORDER BY seq",
                (corridor_id,),
            ).fetchall()

            if not cells:
                problems.append(f"{corridor_id}: 셀이 없다 (seed_cells.py --write)")
                continue

            first, last = cells[0], cells[-1]
            shortest = min(c[3] for c in cells)
            print(f"{corridor_id}: 셀 {len(cells)}개 · {first[1]:.3f} ~ {last[2]:.3f} km "
                  f"(코리도 {length_km:.3f} km) · 가장 짧은 셀 {shortest:.3f} km")

            if abs(first[1]) > TOL_KM:
                problems.append(f"{corridor_id}: 첫 셀이 {first[1]:.3f} km 에서 시작 (0 이어야 함)")
            if abs(last[2] - length_km) > TOL_KM:
                problems.append(
                    f"{corridor_id}: 마지막 셀이 {last[2]:.3f} km 에서 끝남 (코리도 {length_km:.3f} km). "
                    "옛 노선으로 만든 셀이면 seed_cells.py --replace 로 다시 만들 것"
                )

            for a, b in zip(cells, cells[1:], strict=False):
                if abs(a[2] - b[1]) > TOL_KM:
                    kind = "빈틈" if b[1] > a[2] else "겹침"
                    problems.append(f"{corridor_id}: 셀 {a[0]}→{b[0]} {kind} ({a[2]:.3f} → {b[1]:.3f} km)")

            stations = conn.execute(
                "SELECT s.name, s.offset_km, s.cell_id, c.offset_km_start, c.offset_km_end"
                " FROM station s LEFT JOIN cell c ON c.cell_id = s.cell_id WHERE s.corridor_id = ?",
                (corridor_id,),
            ).fetchall()

            for name, km, cell_id, lo, hi in stations:
                if cell_id is None:
                    problems.append(f"{corridor_id}: {name} 에 cell_id 가 없다")
                elif not (lo - TOL_KM <= km <= hi + TOL_KM):
                    problems.append(f"{corridor_id}: {name} ({km:.3f} km) 가 자기 셀 [{lo:.3f}, {hi:.3f}] 밖")

    try:
        import yaml

        dt_min = float(yaml.safe_load((ROOT / "config" / "flow_params.yaml").read_text(encoding="utf-8"))["dt_min"])
        with get_conn(default_db_path(), readonly=True) as conn:
            worst = conn.execute(
                "SELECT corridor_id, seq, length_km, MAX(v_free_kmh, w_back_kmh) * ? / 60.0 AS cfl"
                " FROM cell WHERE length_km < MAX(v_free_kmh, w_back_kmh) * ? / 60.0 - 1e-9",
                (dt_min, dt_min),
            ).fetchall()
        for corridor_id, seq, length_km, cfl in worst:
            problems.append(f"{corridor_id}: 셀 {seq} 길이 {length_km:.3f} km < CFL 하한 {cfl:.3f} km")
    except (OSError, KeyError) as exc:
        problems.append(f"CFL 검사를 못 했다: {exc}")

    if problems:
        print("\n[문제]")
        for p in problems:
            print("  -", p)
        return 1

    print("\n[OK] 셀이 코리도 전체를 빈틈 없이 덮고, 휴게소가 모두 제 셀 안에 있다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
