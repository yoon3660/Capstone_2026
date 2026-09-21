"""실행 레지스트리 (T-04).

해결하는 문제
    발표 3주 전에 "이 히트맵 어느 설정으로 뽑은 거였지?" 를 반드시 묻게 된다.
    그때 답하려면 다섯 가지가 있어야 한다:
        scenario_id / stage / seed / participation / code_version(git SHA)
    이 다섯을 run 테이블이 들고 있고, UNIQUE 제약이 중복 실험을 막는다.

run_id 규칙 (결정적 — 같은 조건이면 언제나 같은 id)
    <scenario_id>__<stage>__p<참여율 3자리>__s<시드 4자리>
    예: seollal_2026_down_base__UE__p100__s0007

    타임스탬프를 넣지 않는 이유: 같은 조건을 두 번 돌리면 결과 폴더가 두 개
    생기고, 나중에 어느 쪽이 맞는지 판단해야 한다. 결정적 id 는 덮어쓰기를
    명시적 선택(overwrite=True)으로 만든다.

사용법
    cfg = ScenarioConfig.from_yaml("config/scenario_seollal_down.yaml")

    with RunContext.open(cfg, seed=7) as run:
        run.writer.append("charge_event", {...})
        run.kpi("total_social_cost_krw", 1_234_567.0, "KRW")
    # 정상 종료 → status=DONE, 예외 → status=FAILED + error_message 기록
"""

from __future__ import annotations

import json
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

import pandas as pd

from evdt.config import ScenarioConfig
from evdt.io.db import get_conn, upsert_df
from evdt.io.writers import ParquetRunWriter
from evdt.paths import PROJECT_ROOT, RUNS_DIR, default_db_path


class RunExistsError(RuntimeError):
    """같은 (scenario, stage, seed, participation) 실행이 이미 있다."""


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GitInfo:
    sha: str
    dirty: bool

    @property
    def label(self) -> str:
        return f"{self.sha}{'-dirty' if self.dirty else ''}"


def git_info(repo: Path | None = None) -> GitInfo:
    """현재 커밋 SHA 와 작업트리 변경 여부.

    git 이 없거나 리포가 아니면 sha='nogit' 을 돌려준다.
    (결과는 남되, 재현 불가라는 사실이 드러나야 한다.)
    """
    root = repo or PROJECT_ROOT
    if shutil.which("git") is None or not (root / ".git").exists():
        return GitInfo(sha="nogit", dirty=True)
    try:
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        return GitInfo(sha=sha or "nogit", dirty=bool(status))
    except (subprocess.SubprocessError, OSError):
        return GitInfo(sha="nogit", dirty=True)


# ---------------------------------------------------------------------------
# run_id
# ---------------------------------------------------------------------------
def make_run_id(scenario_id: str, stage: str, seed: int, participation: float) -> str:
    pct = int(round(participation * 100))
    return f"{scenario_id}__{stage}__p{pct:03d}__s{seed:04d}"


# ---------------------------------------------------------------------------
# 등록
# ---------------------------------------------------------------------------
def register_scenario(cfg: ScenarioConfig, *, db_path: str | Path | None = None) -> None:
    """scenario 테이블에 config 전문을 upsert 한다.

    corridor 행이 없으면 먼저 만들라는 에러가 난다 (FK). Sprint 1 초반에는
    scripts/seed_corridor.py 로 경부선 상·하행 2개를 넣어두면 된다.
    """
    row = cfg.scenario_row()
    with get_conn(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM corridor WHERE corridor_id = ?", (cfg.corridor_id,)
        ).fetchone()
        if exists is None:
            raise RunExistsError(
                f"corridor '{cfg.corridor_id}' 가 DB 에 없다. "
                f"먼저 `python scripts/seed_corridor.py` 를 실행할 것."
            )
        upsert_df(conn, "scenario", pd.DataFrame([row]))


def register_run(
    cfg: ScenarioConfig,
    *,
    seed: int | None = None,
    stage: str | None = None,
    participation: float | None = None,
    params: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
    runs_dir: Path | None = None,
    overwrite: bool = False,
) -> RunHandle:
    """run 을 등록하고 runs/<run_id>/ 를 만든다."""
    stage = stage or cfg.policy.stage
    seed = cfg.vehicles.seed if seed is None else seed
    participation = cfg.policy.participation if participation is None else participation

    register_scenario(cfg, db_path=db_path)

    run_id = make_run_id(cfg.scenario_id, stage, seed, participation)
    base = runs_dir or RUNS_DIR
    out_dir = base / run_id
    gi = git_info()
    db = Path(db_path) if db_path is not None else default_db_path()

    with get_conn(db) as conn:
        prior = conn.execute("SELECT run_id, status FROM run WHERE run_id = ?", (run_id,)).fetchone()
        if prior is not None:
            if not overwrite:
                raise RunExistsError(
                    f"이미 등록된 실행이다: {run_id} (status={prior['status']})\n"
                    f"  같은 조건을 다시 돌리려면 overwrite=True 를 주거나 seed 를 바꿀 것."
                )
            conn.execute("DELETE FROM run WHERE run_id = ?", (run_id,))  # run_kpi 는 CASCADE
        conn.execute(
            """
            INSERT INTO run (run_id, scenario_id, stage, seed, participation,
                             code_version, code_dirty, params_json, output_dir,
                             status, started_at, hostname, python_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?, ?)
            """,
            (
                run_id, cfg.scenario_id, stage, int(seed), float(participation),
                gi.sha, int(gi.dirty), json.dumps(params or {}, ensure_ascii=False),
                str(out_dir.relative_to(PROJECT_ROOT)) if _under(out_dir, PROJECT_ROOT) else str(out_dir),
                _now(), socket.gethostname(), platform.python_version(),
            ),
        )

    if overwrite and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # config 원문을 run 폴더에도 복사해 둔다. DB 가 없어도 폴더만 보고 조건을 알 수 있다.
    (out_dir / "config.yaml").write_text(cfg.raw_yaml, encoding="utf-8")

    handle = RunHandle(
        run_id=run_id,
        scenario_id=cfg.scenario_id,
        stage=stage,
        seed=int(seed),
        participation=float(participation),
        output_dir=out_dir,
        code_version=gi.label,
        db_path=db,
        config_hash=cfg.config_hash,
    )
    handle._write_meta()
    return handle


@dataclass(slots=True)
class RunHandle:
    """등록된 실행 하나. KPI 기록과 상태 전이를 담당."""

    run_id: str
    scenario_id: str
    stage: str
    seed: int
    participation: float
    output_dir: Path
    code_version: str
    db_path: Path
    config_hash: str

    # -- KPI ---------------------------------------------------------------
    def kpi(self, metric: str, value: float, unit: str = "") -> None:
        self.kpis({metric: (value, unit)})

    def kpis(self, items: dict[str, float | tuple[float, str]]) -> None:
        rows = []
        for metric, v in items.items():
            value, unit = v if isinstance(v, tuple) else (v, "")
            rows.append(
                {"run_id": self.run_id, "metric": metric, "value": float(value), "unit": unit}
            )
        if not rows:
            return
        with get_conn(self.db_path) as conn:
            upsert_df(conn, "run_kpi", pd.DataFrame(rows))

    # -- 상태 --------------------------------------------------------------
    def mark_done(self) -> None:
        self._set_status("DONE", None)

    def mark_failed(self, message: str) -> None:
        self._set_status("FAILED", message[:2000])

    def _set_status(self, status: str, message: str | None) -> None:
        with get_conn(self.db_path) as conn:
            conn.execute(
                "UPDATE run SET status = ?, error_message = ?, finished_at = ? WHERE run_id = ?",
                (status, message, _now(), self.run_id),
            )

    def _write_meta(self) -> None:
        meta = {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "stage": self.stage,
            "seed": self.seed,
            "participation": self.participation,
            "code_version": self.code_version,
            "config_hash": self.config_hash,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "hostname": socket.gethostname(),
            "started_at": _now(),
            "argv": sys.argv,
        }
        (self.output_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class RunContext:
    """run 등록 + Parquet writer + 상태 전이를 묶은 컨텍스트 매니저.

        with RunContext.open(cfg, seed=7) as run:
            run.writer.append("charge_event", {...})
            run.kpi("wait_p95_min", 18.3, "min")

    블록이 예외로 끝나면 run.status 가 FAILED 로 기록되고 예외는 그대로 전파된다.
    """

    def __init__(self, handle: RunHandle, writer: ParquetRunWriter) -> None:
        self.handle = handle
        self.writer = writer

    @classmethod
    def open(cls, cfg: ScenarioConfig, **kwargs: Any) -> RunContext:
        handle = register_run(cfg, **kwargs)
        writer = ParquetRunWriter(handle.output_dir, handle.run_id)
        return cls(handle, writer)

    # 자주 쓰는 것만 앞으로 끌어낸다
    @property
    def run_id(self) -> str:
        return self.handle.run_id

    @property
    def output_dir(self) -> Path:
        return self.handle.output_dir

    def kpi(self, metric: str, value: float, unit: str = "") -> None:
        self.handle.kpi(metric, value, unit)

    def kpis(self, items: dict[str, float | tuple[float, str]]) -> None:
        self.handle.kpis(items)

    def __enter__(self) -> RunContext:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        counts = self.writer.close()
        (self.output_dir / "row_counts.json").write_text(
            json.dumps(counts, indent=2), encoding="utf-8"
        )
        if exc is None:
            self.handle.mark_done()
        else:
            self.handle.mark_failed(f"{exc_type.__name__ if exc_type else 'Error'}: {exc}")


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
