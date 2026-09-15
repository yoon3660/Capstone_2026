"""시나리오 config 로더와 검증 (T-03).

왜 로딩 시점에 검증하는가
    ev_share = 1.5 같은 값은 시뮬레이션이 3시간 돈 뒤에 이상한 숫자로 나타난다.
    그때는 원인을 찾기 어렵다. 잘못된 값은 **YAML 을 읽는 순간** 터져야 하고,
    에러 메시지는 어느 키가 왜 틀렸는지 정확히 말해야 한다.

사용법
    from evdt.config import ScenarioConfig

    cfg = ScenarioConfig.from_yaml("config/scenario_seollal_down.yaml")
    print(cfg.scenario_id, cfg.demand.ev_share, cfg.config_hash)

한 번에 모든 오류를 모아서 보고한다. 하나 고치고 다시 돌리면 다음 오류가
나오는 식이면 팀원이 지친다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# run.stage 의 허용값 (schema.sql 의 CHECK 와 반드시 일치해야 한다)
VALID_STAGES: tuple[str, ...] = (
    "UE", "S0", "S1", "S2", "S3", "S4",
    "MEC", "PAPER", "PERFECT_ROLLING", "ORACLE",
)
VALID_DAY_TYPES: tuple[str, ...] = (
    "SEOLLAL_PEAK", "CHUSEOK_PEAK", "WEEKEND_BASE", "WEEKDAY_BASE",
)
VALID_DIRECTIONS: tuple[str, ...] = ("UP", "DOWN")
VALID_DISCIPLINES: tuple[str, ...] = ("FIFO",)
VALID_CHARGER_SELECT: tuple[str, ...] = ("MAX_POWER_IDLE",)


class ConfigError(ValueError):
    """config 가 잘못됐을 때. 메시지에 문제 목록이 전부 담긴다."""


class _Errors:
    """검증 오류 수집기. 한 번에 다 모아서 보고한다."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.items: list[str] = []

    def add(self, path: str, message: str) -> None:
        self.items.append(f"{path}: {message}")

    def require(self, data: dict[str, Any], path: str, key: str) -> Any:
        if key not in data:
            self.add(f"{path}.{key}" if path else key, "필수 키가 없다")
            return None
        return data[key]

    def number(
        self,
        value: Any,
        path: str,
        *,
        lo: float | None = None,
        hi: float | None = None,
        lo_exclusive: bool = False,
        integer: bool = False,
    ) -> float | int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.add(path, f"숫자여야 한다 (받은 값: {value!r})")
            return None
        if integer and not float(value).is_integer():
            self.add(path, f"정수여야 한다 (받은 값: {value!r})")
            return None
        if lo is not None:
            if lo_exclusive and value <= lo:
                self.add(path, f"{lo} 보다 커야 한다 (받은 값: {value})")
                return None
            if not lo_exclusive and value < lo:
                self.add(path, f"{lo} 이상이어야 한다 (받은 값: {value})")
                return None
        if hi is not None and value > hi:
            self.add(path, f"{hi} 이하여야 한다 (받은 값: {value})")
            return None
        return int(value) if integer else float(value)

    def choice(self, value: Any, path: str, allowed: tuple[str, ...]) -> str | None:
        if value is None:
            return None
        if value not in allowed:
            self.add(path, f"{list(allowed)} 중 하나여야 한다 (받은 값: {value!r})")
            return None
        return str(value)

    def text(self, value: Any, path: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            self.add(path, f"비어 있지 않은 문자열이어야 한다 (받은 값: {value!r})")
            return None
        return value

    def section(self, data: dict[str, Any], key: str) -> dict[str, Any]:
        value = data.get(key)
        if value is None:
            self.add(key, "필수 섹션이 없다")
            return {}
        if not isinstance(value, dict):
            self.add(key, f"매핑(딕셔너리)이어야 한다 (받은 값: {type(value).__name__})")
            return {}
        return value

    def raise_if_any(self) -> None:
        if self.items:
            lines = "\n".join(f"  - {m}" for m in self.items)
            raise ConfigError(f"config 검증 실패: {self.source}\n{lines}")


# ---------------------------------------------------------------------------
# 섹션 dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TimeConfig:
    start_min: int
    end_min: int
    dt_min: int          # 재계획 주기 Δt (§4 매 Δt 루프)

    @property
    def duration_min(self) -> int:
        return self.end_min - self.start_min


@dataclass(frozen=True, slots=True)
class DemandConfig:
    volume_profile: str        # 시간대별 교통량 CSV (T-07 산출물)
    demand_multiplier: float
    ev_share: float
    charge_prob: float         # Rupnik 규칙의 충전확률 (0.95)
    safety_buffer_km: float    # Rupnik 규칙의 안전버퍼 (30km)
    low_soc_threshold: float   # 이 SoC 아래면 무조건 충전 (0.2)


@dataclass(frozen=True, slots=True)
class SocBeta:
    a: float
    b: float
    lo: float
    hi: float


@dataclass(frozen=True, slots=True)
class VehiclesConfig:
    seed: int
    soc_beta: SocBeta
    target_soc_cap: float      # 목표 SoC 상한 0.8 (§2.3)


@dataclass(frozen=True, slots=True)
class EnvironmentConfig:
    temp_c: float


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    stage: str
    participation: float
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QueueConfig:
    discipline: str
    charger_select: str


@dataclass(frozen=True, slots=True)
class OutputConfig:
    write_snapshots: bool
    snapshot_every_min: int


@dataclass(frozen=True, slots=True)
class ScenarioConfig:
    scenario_id: str
    label: str
    corridor_id: str
    direction: str
    day_type: str

    time: TimeConfig
    demand: DemandConfig
    vehicles: VehiclesConfig
    environment: EnvironmentConfig
    policy: PolicyConfig
    queue: QueueConfig
    output: OutputConfig

    source_path: str
    raw_yaml: str
    config_hash: str

    # -- 생성 ---------------------------------------------------------------
    @classmethod
    def from_yaml(cls, path: str | Path) -> ScenarioConfig:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"config 파일이 없다: {p}")
        text = p.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"YAML 문법 오류: {p}\n  {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"config 최상위가 매핑이 아니다: {p}")
        return cls._build(data, source_path=str(p), raw_yaml=text)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str = "<dict>") -> ScenarioConfig:
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=True)
        return cls._build(data, source_path=source, raw_yaml=text)

    @classmethod
    def _build(cls, data: dict[str, Any], *, source_path: str, raw_yaml: str) -> ScenarioConfig:
        e = _Errors(source_path)

        scenario_id = e.text(e.require(data, "", "scenario_id"), "scenario_id")
        label = e.text(e.require(data, "", "label"), "label")
        corridor_id = e.text(e.require(data, "", "corridor_id"), "corridor_id")
        direction = e.choice(e.require(data, "", "direction"), "direction", VALID_DIRECTIONS)
        day_type = e.choice(e.require(data, "", "day_type"), "day_type", VALID_DAY_TYPES)

        # time -------------------------------------------------------------
        t = e.section(data, "time")
        start_min = e.number(e.require(t, "time", "start_min"), "time.start_min", lo=0, integer=True)
        end_min = e.number(e.require(t, "time", "end_min"), "time.end_min", lo=0, integer=True)
        dt_min = e.number(e.require(t, "time", "dt_min"), "time.dt_min", lo=0, lo_exclusive=True, integer=True)
        if start_min is not None and end_min is not None and end_min <= start_min:
            e.add("time.end_min", f"start_min({start_min}) 보다 커야 한다 (받은 값: {end_min})")
        if dt_min is not None and start_min is not None and end_min is not None:
            if dt_min > end_min - start_min:
                e.add("time.dt_min", "재계획 주기가 시뮬레이션 전체 길이보다 길다")

        # demand -----------------------------------------------------------
        d = e.section(data, "demand")
        volume_profile = e.text(e.require(d, "demand", "volume_profile"), "demand.volume_profile")
        demand_multiplier = e.number(
            e.require(d, "demand", "demand_multiplier"), "demand.demand_multiplier",
            lo=0, lo_exclusive=True,
        )
        ev_share = e.number(e.require(d, "demand", "ev_share"), "demand.ev_share", lo=0.0, hi=1.0)
        charge_prob = e.number(d.get("charge_prob", 0.95), "demand.charge_prob", lo=0.0, hi=1.0)
        safety_buffer_km = e.number(
            d.get("safety_buffer_km", 30.0), "demand.safety_buffer_km", lo=0.0
        )
        low_soc_threshold = e.number(
            d.get("low_soc_threshold", 0.2), "demand.low_soc_threshold", lo=0.0, hi=1.0
        )

        # vehicles ---------------------------------------------------------
        v = e.section(data, "vehicles")
        seed = e.number(e.require(v, "vehicles", "seed"), "vehicles.seed", lo=0, integer=True)
        beta_raw = v.get("soc_beta")
        if not isinstance(beta_raw, dict):
            e.add("vehicles.soc_beta", "a / b / lo / hi 를 담은 매핑이어야 한다")
            beta_raw = {}
        beta_a = e.number(beta_raw.get("a", 2.0), "vehicles.soc_beta.a", lo=0, lo_exclusive=True)
        beta_b = e.number(beta_raw.get("b", 5.0), "vehicles.soc_beta.b", lo=0, lo_exclusive=True)
        beta_lo = e.number(beta_raw.get("lo", 0.10), "vehicles.soc_beta.lo", lo=0.0, hi=1.0)
        beta_hi = e.number(beta_raw.get("hi", 0.95), "vehicles.soc_beta.hi", lo=0.0, hi=1.0)
        if beta_lo is not None and beta_hi is not None and beta_hi <= beta_lo:
            e.add("vehicles.soc_beta.hi", f"lo({beta_lo}) 보다 커야 한다 (받은 값: {beta_hi})")
        target_soc_cap = e.number(
            v.get("target_soc_cap", 0.8), "vehicles.target_soc_cap", lo=0.0, hi=1.0, lo_exclusive=True
        )

        # environment ------------------------------------------------------
        env = e.section(data, "environment")
        temp_c = e.number(e.require(env, "environment", "temp_c"), "environment.temp_c", lo=-40.0, hi=50.0)

        # policy -----------------------------------------------------------
        p = e.section(data, "policy")
        stage = e.choice(e.require(p, "policy", "stage"), "policy.stage", VALID_STAGES)
        participation = e.number(
            p.get("participation", 1.0), "policy.participation", lo=0.0, hi=1.0
        )
        params = p.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            e.add("policy.params", "매핑이어야 한다")
            params = {}
        if stage in ("UE", "S0") and participation not in (None, 1.0):
            e.add(
                "policy.participation",
                f"{stage} 는 조정을 하지 않으므로 참여율 개념이 없다. 1.0 이어야 한다 "
                f"(받은 값: {participation})",
            )

        # queue ------------------------------------------------------------
        q = data.get("queue") or {}
        if not isinstance(q, dict):
            e.add("queue", "매핑이어야 한다")
            q = {}
        discipline = e.choice(q.get("discipline", "FIFO"), "queue.discipline", VALID_DISCIPLINES)
        charger_select = e.choice(
            q.get("charger_select", "MAX_POWER_IDLE"), "queue.charger_select", VALID_CHARGER_SELECT
        )

        # output -----------------------------------------------------------
        o = data.get("output") or {}
        if not isinstance(o, dict):
            e.add("output", "매핑이어야 한다")
            o = {}
        write_snapshots = o.get("write_snapshots", True)
        if not isinstance(write_snapshots, bool):
            e.add("output.write_snapshots", f"true/false 여야 한다 (받은 값: {write_snapshots!r})")
            write_snapshots = True
        snapshot_every_min = e.number(
            o.get("snapshot_every_min", 5), "output.snapshot_every_min",
            lo=0, lo_exclusive=True, integer=True,
        )

        # 알 수 없는 최상위 키 — 오타를 조용히 넘기지 않는다
        known_top = {
            "scenario_id", "label", "corridor_id", "direction", "day_type",
            "time", "demand", "vehicles", "environment", "policy", "queue", "output",
        }
        for key in data:
            if key not in known_top:
                e.add(key, "알 수 없는 최상위 키다 (오타인지 확인)")

        e.raise_if_any()

        return cls(
            scenario_id=scenario_id,                       # type: ignore[arg-type]
            label=label,                                   # type: ignore[arg-type]
            corridor_id=corridor_id,                       # type: ignore[arg-type]
            direction=direction,                           # type: ignore[arg-type]
            day_type=day_type,                             # type: ignore[arg-type]
            time=TimeConfig(start_min, end_min, dt_min),   # type: ignore[arg-type]
            demand=DemandConfig(
                volume_profile=volume_profile,             # type: ignore[arg-type]
                demand_multiplier=demand_multiplier,       # type: ignore[arg-type]
                ev_share=ev_share,                         # type: ignore[arg-type]
                charge_prob=charge_prob,                   # type: ignore[arg-type]
                safety_buffer_km=safety_buffer_km,         # type: ignore[arg-type]
                low_soc_threshold=low_soc_threshold,       # type: ignore[arg-type]
            ),
            vehicles=VehiclesConfig(
                seed=seed,                                 # type: ignore[arg-type]
                soc_beta=SocBeta(beta_a, beta_b, beta_lo, beta_hi),  # type: ignore[arg-type]
                target_soc_cap=target_soc_cap,             # type: ignore[arg-type]
            ),
            environment=EnvironmentConfig(temp_c),         # type: ignore[arg-type]
            policy=PolicyConfig(stage, participation, dict(params)),  # type: ignore[arg-type]
            queue=QueueConfig(discipline, charger_select), # type: ignore[arg-type]
            output=OutputConfig(write_snapshots, snapshot_every_min),  # type: ignore[arg-type]
            source_path=source_path,
            raw_yaml=raw_yaml,
            config_hash=hashlib.sha256(raw_yaml.encode("utf-8")).hexdigest()[:16],
        )

    # -- 입력 파일 -----------------------------------------------------------
    def missing_inputs(self, root: Path | None = None) -> list[str]:
        """config 가 가리키는 입력 파일 중 아직 없는 것.

        로딩 시점에 검사하지 않는 이유: Sprint 1 초반에는 T-07 산출물이 아직
        없는데도 config 자체는 읽을 수 있어야 한다. 대신 러너가 시뮬레이션을
        시작하기 직전에 이 메서드로 확인한다.
        """
        base = root or Path.cwd()
        candidates = [self.demand.volume_profile]
        return [c for c in candidates if not (base / c).is_file() and not Path(c).is_file()]

    # -- DB 연동 -------------------------------------------------------------
    def scenario_row(self) -> dict[str, Any]:
        """scenario 테이블에 넣을 한 행."""
        return {
            "scenario_id": self.scenario_id,
            "corridor_id": self.corridor_id,
            "label": self.label,
            "day_type": self.day_type,
            "sim_start_min": self.time.start_min,
            "sim_end_min": self.time.end_min,
            "ev_share": self.demand.ev_share,
            "demand_multiplier": self.demand.demand_multiplier,
            "temp_c": self.environment.temp_c,
            "config_path": self.source_path,
            "config_yaml": self.raw_yaml,
            "config_hash": self.config_hash,
        }


def load_scenario(path: str | Path) -> ScenarioConfig:
    """ScenarioConfig.from_yaml 의 짧은 이름."""
    return ScenarioConfig.from_yaml(path)
