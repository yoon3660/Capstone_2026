"""`scripts/run_mnl_world.py` 의 구조 검사 — 긴 실행을 **한 시드가 죽일 수 없다**.

이 파일은 시뮬레이션을 돌리지 않는다. 소스를 읽어서 "이 자리에 이 보호막이
있는가" 만 본다. 한 시드가 13분씩 걸리고 10시드를 한 번에 거는 스크립트라,
중간에 터지면 그때까지 쓴 CSV 는 남지만 **뒤의 시드는 통째로 날아간다.**
"""

from __future__ import annotations

import ast
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_mnl_world.py"


def _tree() -> ast.Module:
    return ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))


def test_a_seed_whose_ue_does_not_converge_does_not_kill_the_run() -> None:
    """★ `UENotConverged` 는 **잡아서 그 시드만 건너뛴다.**

    UE 는 시드에 따라 수렴하지 못한다 — `ue.escape_cost_of` 의 설명대로 이탈
    비용이 한 점에 몰리면 gap 이 5~6% 에서 추세 없이 흔들린다. 설계된 실패고,
    `solve_ue` 가 일부러 던진다 (조용히 넘어가면 "수렴 안 한 배정" 이 균형
    결과처럼 쓰인다).

    2026-11-13, `--seeds 1-10 --classic --ue` 가 **시드 5 에서 그 예외로
    죽었다.** 시드 6~10 은 시작도 못 했고, 52분이 날아갔다. UE 는 참고
    끝점일 뿐이고 같은 시드의 S0·엔진·천장 칸은 멀쩡했다 — 그 하나 때문에
    나머지를 잃을 이유가 없다.

    그래서 **그 시드만 UE 없이 두고 넘어간다.** 대신 조용하면 안 된다:
    빠진 시드를 끝에서 밝히고, 요약은 `closed_to_ue_*_n_seeds` 로 실제로 쓴
    시드 수를 적는다 (`.mean()` 이 NaN 을 조용히 빼기 때문이다).
    """

    main = next(n for n in ast.walk(_tree())
                if isinstance(n, ast.FunctionDef) and n.name == "main")

    guarded = [
        t for t in ast.walk(main)
        if isinstance(t, ast.Try)
        and any("run_ue_cell" == getattr(getattr(c, "func", None), "id", None)
                for c in ast.walk(t) if isinstance(c, ast.Call))
    ]
    assert guarded, (
        "`run_ue_cell` 호출이 try 안에 없다 — 수렴 실패 한 번에 뒤의 시드가 "
        "전부 날아간다")

    caught = {ast.unparse(h.type) for t in guarded for h in t.handlers if h.type}
    assert "UENotConverged" in caught, (
        f"잡는 예외가 {caught or '{}'} 다 — `UENotConverged` 를 이름으로 잡아야 "
        "한다 (맨 `except Exception` 은 진짜 고장까지 삼킨다)")
    assert "Exception" not in caught and "BaseException" not in caught, (
        "맨 `except Exception` 으로 삼키면 안 된다 — UE 수렴 실패만 봐준다")


def test_the_summary_says_how_many_seeds_the_ue_metric_used() -> None:
    """UE 가 빠진 시드는 `closed_to_ue` 에서 NaN 이고 `.mean()` 이 **조용히 뺀다.**

    표에는 `n_seeds` 가 하나뿐이라, 그 칸만 n 이 다른 것을 아무도 모른다.
    그래서 실제로 쓴 시드 수를 따로 적는다.
    """

    src = SCRIPT.read_text(encoding="utf-8")
    assert "closed_to_ue{sfx}_n_seeds" in src, (
        "`closed_to_ue_*_n_seeds` 를 요약에 안 적는다 — UE 가 빠진 시드를 "
        "숨긴 채 평균만 보여주게 된다")

    show = next(n for n in ast.walk(_tree())
                if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "SHOW" for t in n.targets))
    assert "closed_to_ue_n_seeds" in ast.literal_eval(show.value), (
        "`SHOW` 에 `closed_to_ue_n_seeds` 가 없다 — CSV 에만 있고 화면에는 "
        "안 뜨면 못 본다")


def test_the_ladder_refuses_a_participation_it_cannot_honor(tmp_path) -> None:
    """★ 사다리는 참여율을 **run_id 에만** 쓴다 — 그래서 받으면 안 된다.

    `runner.run_once` 안에서 `cfg.policy.participation` 이 쓰이는 곳은
    `make_run_id` 하나뿐이다. 물리에는 한 줄도 닿지 않는다. 그래서 config 에
    `participation: 0.6` 을 적으면 **이름만 다른, 내용이 똑같은 run** 이 나온다.

    그걸 결과로 읽으면 "참여율 60% 에서도 효과가 같다" 는 **완전히 틀린 결론**이
    조용히 나온다. 발표 §8-4 가 바로 그 질문이라 특히 위험하다 (2026-11-14).

    참여율은 `engine.participation.ParticipationPolicy` 에 있고 그것을 쓰는 것은
    `scripts/run_mnl_world.py --p` 뿐이다. 그쪽으로 보낸다.
    """

    import dataclasses

    import pytest

    from evdt.runner import load_config, run_once

    cfg = load_config("config/scenario_chuseok_up_congested.yaml", stage="S0")
    cfg = dataclasses.replace(
        cfg, policy=dataclasses.replace(cfg.policy, participation=0.6))

    # `runs_dir=tmp_path` — 가드가 쓰기 전에 멈추지만, 정적 검사
    # (`test_no_test_writes_into_the_real_runs_dir`) 는 호출만 보고 걸러낸다.
    # 그 검사가 옳다: 가드가 나중에 느슨해지면 이 테스트가 진짜 run 을 덮는다
    with pytest.raises(ValueError, match="참여율"):
        run_once(cfg, seed=1, overwrite=True, log=lambda *_: None,
                 runs_dir=tmp_path)
