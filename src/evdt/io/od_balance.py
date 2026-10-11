"""실측 OD 를 VDS 주변분포에 맞춰 균형화한다 — IPF / Furness (#111).

## 무엇을 고치나

`tcs_od_gyeongbu.parquet` 은 **양 끝이 모두 경부선 영업소인 통행만** 담는다. 한쪽 끝이
코리도 밖인 통행은 **구조적으로 빠져 있고**, #100 에서 그게 도로 사용량의 약 31% 로
드러났다.

    평균 통행거리      총 통행거리
    옛 수요 (exit_share)   59.5 km    22.05백만 대·km
    OD 수요 (#99)          37.9 km    14.06백만 대·km   ← 31% 부족
    VDS 가 요구            53.6 km    19.86백만 대·km

목적지가 **너무 일찍 빠지는** 것도 같은 뿌리다 — VDS 진출 분포와의 총변동거리가 0.397.

## 이미 갖고 있는 것을 안 쓰고 있었다

`entry_exit_profile` 은 **전체 차량** 기준이라 진입·진출 주변분포가 완전하다.
지금까지 진입만 쓰고 **진출을 버렸다.**

그리고 진입·진출 합의 차이가 곧 **코리도 끝까지 통과하는 교통량**이다
(하행 30,807대 8.3% · 상행 83,556대 21.6%). 옛 위험률 모델은 *"끝까지 살아남으면
코리도 끝"* 으로 이걸 보존했는데, OD 모델에는 그 몫이 없다.

## 방법 — IPF (Furness)

씨앗의 **구조**는 유지하면서 행·열 합만 목표에 맞춘다. 모수가 없다.

| | |
|---|---|
| 씨앗 | TCS OD 의 기점×종점 결합 (#99 가 얻은 것) |
| 행 제약 | VDS 진입 대수 |
| 열 제약 | VDS 진출 대수 **+ 코리도 끝(잔차)** |
| 제한 | 종점은 기점보다 하류 |

> **#99 를 되돌리는 것이 아니다.** 조건부 구조는 씨앗으로 남고 주변분포만 실측에
> 맞춘다. 내부 통행의 모양은 유지되고 전체 평균 거리가 올라간다.

## ⚠ 바닥(floor)이 필요한 이유

IPF 는 **씨앗이 0 인 칸에 질량을 못 옮긴다.** 0 에 무엇을 곱해도 0 이다. 그런데 TCS 가
못 본 외부 통행이 바로 그 0 칸에 있다. 그래서 하류 칸에 **거리 감쇠 바닥**을 깔아
둔다 — 작게 깔아 씨앗의 구조를 덮지 않으면서, 주변분포가 요구하면 채워질 수 있게.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: 바닥의 세기. 씨앗 총량 대비 비율이다. 크면 TCS 구조가 묻히고, 작으면 IPF 가
#: 외부 통행을 채울 자리를 못 찾는다. 0.02 는 씨앗을 거의 안 건드리면서
#: 모든 하류 칸에 길을 열어 두는 값이다.
FLOOR_SHARE = 0.02

#: 거리 감쇠 지수. 바닥을 균등하게 깔면 **먼 칸이 과대**해진다 — 통행은 가까울수록
#: 많다. 중력모형의 관행대로 거리의 거듭제곱으로 줄인다.
FLOOR_DECAY = 1.5

MAX_ITER = 200
TOL = 1e-9


@dataclass(frozen=True)
class Balanced:
    """균형화 결과."""

    #: (기점 수 × 종점 수). 종점의 마지막 칸은 **코리도 끝**이다
    matrix: np.ndarray
    origins: np.ndarray
    dests: np.ndarray
    iterations: int
    #: 행·열 합이 목표에서 벗어난 최대 상대오차
    residual: float

    def rows(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """0 이 아닌 칸을 (기점, 종점, 대수) 로 편다."""
        i, j = np.nonzero(self.matrix > 0)
        return self.origins[i], self.dests[j], self.matrix[i, j]


def _floor(origins: np.ndarray, dests: np.ndarray, total: float) -> np.ndarray:
    """하류 칸에 깔 거리 감쇠 바닥."""
    d = dests[None, :] - origins[:, None]
    ok = d > 0
    w = np.zeros_like(d, dtype=float)
    w[ok] = np.power(d[ok], -FLOOR_DECAY)
    s = w.sum()
    return w * (total * FLOOR_SHARE / s) if s > 0 else w


def balance(
    seed: np.ndarray,
    origins: np.ndarray,
    dests: np.ndarray,
    entry: np.ndarray,
    exit_: np.ndarray,
) -> Balanced:
    """씨앗을 진입·진출 주변분포에 맞춘다.

    `dests` 의 **마지막 칸이 코리도 끝**이고, `exit_` 의 마지막 값이 그 칸의 목표다
    (진입 합 − 진출 합, 즉 끝까지 통과하는 교통량).

    행 합과 열 합이 같아야 풀린다 — 다르면 거부한다. 조용히 한쪽에 맞추면 총량이
    말없이 바뀐다.
    """

    entry = np.asarray(entry, dtype=float)
    exit_ = np.asarray(exit_, dtype=float)

    if (entry < 0).any() or (exit_ < 0).any():
        raise ValueError("\n[중단] 주변분포에 음수가 있다.")

    gap = abs(entry.sum() - exit_.sum())
    if gap > max(1e-6, 1e-9 * entry.sum()):
        raise ValueError(
            f"\n[중단] 진입 합 {entry.sum():,.1f} 과 진출 합 {exit_.sum():,.1f} 이 다르다 "
            f"(차이 {gap:,.1f}).\n"
            "  코리도 끝 칸이 잔차를 받도록 열 목표를 만들었는지 확인하라 (#111)."
        )

    m = np.asarray(seed, dtype=float).copy()
    if (m < 0).any():
        raise ValueError("\n[중단] 씨앗에 음수가 있다.")

    m += _floor(origins, dests, float(entry.sum()))
    # 상류로 가는 칸은 통행이 아니다 — 바닥을 깔고 나서 다시 0 으로 눌러 둔다
    m[dests[None, :] <= origins[:, None]] = 0.0

    # 주변분포가 대수를 요구하는데 그 줄이 통째로 0 이면 IPF 가 발산한다
    for name, need, got in (("기점", entry, m.sum(axis=1)), ("종점", exit_, m.sum(axis=0))):
        bad = (need > 0) & (got <= 0)
        if bad.any():
            which = (origins if name == "기점" else dests)[bad]
            raise ValueError(
                f"\n[중단] {name} {np.round(which, 3).tolist()} 에 대수가 필요한데 "
                "갈 수 있는 칸이 없다.\n"
                "  하류 제약과 바닥을 확인하라 (#111)."
            )

    iters = 0
    for step in range(1, MAX_ITER + 1):
        iters = step
        r = m.sum(axis=1)
        m *= np.divide(entry, r, out=np.zeros_like(r), where=r > 0)[:, None]
        c = m.sum(axis=0)
        m *= np.divide(exit_, c, out=np.zeros_like(c), where=c > 0)[None, :]

        err = max(
            float(np.max(np.abs(m.sum(axis=1) - entry))),
            float(np.max(np.abs(m.sum(axis=0) - exit_))),
        )
        if err <= TOL * max(1.0, float(entry.sum())):
            break

    scale = max(1.0, float(entry.sum()))
    residual = max(
        float(np.max(np.abs(m.sum(axis=1) - entry))),
        float(np.max(np.abs(m.sum(axis=0) - exit_))),
    ) / scale

    return Balanced(matrix=m, origins=np.asarray(origins, dtype=float),
                    dests=np.asarray(dests, dtype=float), iterations=iters, residual=residual)


def from_profile(od, entry_offsets, entry_veh, exit_offsets, exit_veh, end_km: float):
    """`OdDestinations` 와 VDS 주변분포로 균형화된 OD 를 만든다.

    종점 격자는 **VDS 진출 경계 + 코리도 끝**이다. 끝 칸의 목표는 진입 합에서 진출 합을
    뺀 **잔차** — 그게 코리도를 끝까지 통과하는 교통량이다.
    """

    origins = np.asarray(entry_offsets, dtype=float)
    entry = np.asarray(entry_veh, dtype=float)
    ex_off = np.asarray(exit_offsets, dtype=float)
    ex_veh = np.asarray(exit_veh, dtype=float)

    through = float(entry.sum() - ex_veh.sum())
    if through < 0:
        raise ValueError(
            f"\n[중단] 진출 합이 진입 합보다 크다 (초과 {-through:,.0f}대).\n"
            "  코리도 끝 칸이 음수 목표를 받게 된다 — 주변분포를 확인하라 (#111)."
        )

    dests = np.append(ex_off, float(end_km))
    exit_ = np.append(ex_veh, through)

    # 씨앗: 기점마다 OD 의 조건부 분포를 종점 격자에 올린다
    seed = np.zeros((len(origins), len(dests)), dtype=float)
    idx, _ = od.snap(origins)
    for r, (i, n) in enumerate(zip(idx, entry, strict=True)):
        if n <= 0:
            continue
        probs = np.diff(np.concatenate([[0.0], od.cdfs[i]]))
        j = np.abs(od.dests[i][:, None] - dests[None, :]).argmin(axis=1)
        np.add.at(seed[r], j, n * probs)

    return balance(seed, origins, dests, entry, exit_)
