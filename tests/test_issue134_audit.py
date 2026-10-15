"""#134 검증 보고서의 수치가 현재 데이터에서 그대로 나오는지 검사한다.

`docs/data_validation/` 의 보고서는 `LOCAL_NEW` 수치(VDS 결측, 진입·진출, DB 현황,
온도 보간 등)를 본문에 적어 두고 결론의 근거로 쓴다. 그 수치가 가공 파이프라인을
고친 뒤에도 유효한지는 **사람이 문서를 다시 읽어야** 알 수 있었다.

`scripts/audit_issue134_readonly.py` 가 그 대조를 수행하므로, 여기서는 그것을
pytest 안에서 돌린다. 데이터를 고쳐 숫자가 달라지면 **이 테스트가 먼저 터진다.**

> 문서가 틀렸다는 뜻일 수도 있고 데이터가 틀렸다는 뜻일 수도 있다. 둘 중 하나는
> 고쳐야 한다는 것만 확실하다.

원본 가공본과 기본 DB 는 Git 이 추적하지 않으므로, 없으면 건너뛴다
(`test_geometry.py` 와 같은 규약).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audit_issue134_readonly.py"

#: 감사가 읽는 가공본. 하나라도 없으면 이 머신에서는 검증할 수 없다.
NEEDED = [
    ROOT / "data" / "processed" / "traffic_gyeongbu.parquet",
    ROOT / "data" / "processed" / "speed_gyeongbu.parquet",
    ROOT / "data" / "processed" / "conzone_gyeongbu.parquet",
    ROOT / "data" / "processed" / "lanes_gyeongbu.parquet",
    ROOT / "data" / "processed" / "gyeongbu_route.json",
    ROOT / "data" / "processed" / "entry_exit_gyeongbu_down_seollal.csv",
    ROOT / "data" / "processed" / "entry_exit_gyeongbu_up_seollal.csv",
]


def _load_audit():
    """스크립트를 모듈로 불러온다 (`scripts/` 는 패키지가 아니다)."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location("_audit134", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(ROOT / "scripts"))


def _audit_or_skip():
    missing = [p.name for p in NEEDED if not p.exists()]
    if missing:
        pytest.skip(f"가공본이 없어 생략: {', '.join(missing)}")

    audit = _load_audit()
    from evdt.paths import default_db_path

    if not Path(default_db_path()).exists():
        pytest.skip("기본 DB 가 없어 생략 (schema.sql 로 재생성 필요)")

    a = audit.Audit()
    audit.audit_vds(a)
    audit.audit_conzone_lanes(a)
    audit.audit_entry_exit(a)
    audit.audit_db(a)
    audit.audit_route(a)  # IC54·휴게소37·총연장. raw_dir 은 머신마다 달라 검사하지 않는다
    audit.audit_config(a)
    return a


def test_report_numbers_still_hold():
    """보고서에 적힌 수치가 현재 데이터에서 재현된다."""
    a = _audit_or_skip()

    assert a.rows, "검사가 하나도 수행되지 않았다 — 감사 함수가 비어 있다"
    drift = [f"{name}: 실제 {got} ≠ 문서 {want}" for name, got, want, ok in a.rows if not ok]
    assert not drift, (
        "데이터와 docs/data_validation/ 의 수치가 어긋났다. 둘 중 하나가 틀렸다:\n  "
        + "\n  ".join(drift)
    )


def test_audit_actually_fails_on_drift():
    """감사가 **틀린 값을 잡아내는지** 확인한다.

    안 터지는 검사는 쓸모가 없다. #112·#123 에서 "가드는 걸렸는데 통과라고 찍은"
    보고 버그를 두 번 만들었다. 그래서 가드 자체를 검사한다.
    """
    a = _audit_or_skip()

    a.check("의도적 불일치", 1, 2)
    assert a.failed, "틀린 값을 넣었는데 감사가 통과로 처리했다"
    assert a.report(verbose=False) == 1, "불일치가 있는데 종료코드가 0 이다"
