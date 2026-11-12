"""선택 분배 적합도 — 모델 충전 시작을 실측 충전 시작과 **비중으로** 비교한다 (#168).

## 왜 비중(TVD)인가

추석 실측은 ME 장비의 충전 시작뿐이고, 모델은 모든 운영사를 포함한다 (#162: 총량
+86%). 총량 차이는 **수요 생성·모집단** 문제이지 운전자가 어디를 고르느냐의 문제가
아니다. MNL 이 고칠 수 있는 것은 **어디로 나뉘는가**뿐이므로, 보정 목적은 총량을
지운 비중 차이로 둔다.

    TVD = ½ Σ_k |p_obs(k) − p_model(k)|      0 = 분배가 같다 · 1 = 겹치는 곳이 없다

총량 편향과 WAPE 는 **보고만** 한다 (판정에 쓰지 않는다).

## 비교 집합

`#162` 와 같다. 실측이 있는 (휴게소, 방향) 28곳 안에서만 비교한다. 미관측 7곳은
실측 0 이 아니라 N/A 다 — 모델 쪽에서도 뺀다 (`docs/data_validation/issue162_20261108`).

이 모듈은 DataFrame 만 받는다 (`tests/test_import_boundaries.py`: eval 은 engine 을
임포트하지 않는다).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import pandas as pd

STATION_KEYS: tuple[str, ...] = ("direction", "station_name")


def counts(df: pd.DataFrame, keys: Sequence[str]) -> pd.Series:
    """키별 건수. 빈 표면 빈 Series."""

    if df.empty:
        return pd.Series(dtype=float)
    return df.groupby(list(keys)).size().astype(float)


def tvd(obs: pd.Series, model: pd.Series) -> float:
    """두 건수 분포의 total variation distance. 한쪽이 비면 nan."""

    if obs.sum() <= 0 or model.sum() <= 0:
        return float("nan")
    idx = obs.index.union(model.index)
    p = obs.reindex(idx, fill_value=0.0) / obs.sum()
    q = model.reindex(idx, fill_value=0.0) / model.sum()
    return float(0.5 * (p - q).abs().sum())


def wape(obs: pd.Series, model: pd.Series) -> float:
    """Σ|모델 − 실측| / Σ실측 × 100. **건수** 비교라 총량 차이가 그대로 들어간다."""

    if obs.sum() <= 0:
        return float("nan")
    idx = obs.index.union(model.index)
    o = obs.reindex(idx, fill_value=0.0)
    m = model.reindex(idx, fill_value=0.0)
    return float((m - o).abs().sum() / o.sum() * 100.0)


def restrict_to_observed(model: pd.DataFrame, observed: pd.DataFrame) -> pd.DataFrame:
    """모델 이벤트를 실측이 있는 (방향, 휴게소) 로만 남긴다."""

    keys = set(zip(observed["direction"], observed["station_name"], strict=True))
    mask = [(d, s) in keys for d, s in zip(model["direction"], model["station_name"],
                                          strict=True)]
    return model.loc[mask]


def fit_metrics(observed: pd.DataFrame, model: pd.DataFrame, *,
                directions: Iterable[str] = ("BOTH", "DOWN", "UP"),
                dates: Iterable[str] | None = None) -> list[dict]:
    """실측·모델 충전 시작 표 → 지표 행들.

    두 표 모두 `direction, station_name, date, hour` 열이 있어야 한다. 모델은
    `restrict_to_observed` 를 거친 뒤 넘긴다.

    `dates` 를 주면 날짜별 행도 만든다 (하루 빼고 보정할 때 쓴다).
    """

    out: list[dict] = []
    slices: list[tuple[str, str | None]] = [(d, None) for d in directions]
    if dates is not None:
        slices += [(d, day) for day in dates for d in directions]

    for direction, day in slices:
        o, m = observed, model
        if direction != "BOTH":
            o, m = o[o["direction"] == direction], m[m["direction"] == direction]
        if day is not None:
            o, m = o[o["date"] == day], m[m["date"] == day]
        st_o, st_m = counts(o, STATION_KEYS), counts(m, STATION_KEYS)
        sh_o = counts(o, (*STATION_KEYS, "date", "hour"))
        sh_m = counts(m, (*STATION_KEYS, "date", "hour"))
        h_o, h_m = counts(o, ("date", "hour")), counts(m, ("date", "hour"))
        out.append({
            "direction": direction, "date": day or "ALL",
            "n_observed": int(len(o)), "n_model": int(len(m)),
            "total_bias_pct": ((len(m) - len(o)) / len(o) * 100.0) if len(o) else float("nan"),
            "station_tvd": tvd(st_o, st_m),
            "station_hour_tvd": tvd(sh_o, sh_m),
            "hour_tvd": tvd(h_o, h_m),
            "station_wape_pct": wape(st_o, st_m),
        })
    return out
