"""生产发布脚本在首个变更前检查目标，并等待所有工作负载。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _run(tmp_path: Path, *, fail_preflight: bool = False) -> tuple[int, list[str]]:
    kubectl = tmp_path / "kubectl"
    log = tmp_path / "calls"
    kubectl.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$MOCK_LOG"\n'
        'if [[ "$MOCK_FAIL" == 1 && "$3" == get ]]; then exit 1; fi\n'
        'if [[ "$*" == *"containers[0].name"* ]]; then echo "${4#deployment/}"; fi\n'
        'if [[ "$*" == *"spec.replicas"* ]]; then echo 1; fi\n',
        encoding="utf-8",
    )
    kubectl.chmod(0o755)
    script = Path(__file__).resolve().parents[2] / "scripts" / "deploy_k8s.sh"
    result = subprocess.run(
        ["bash", str(script), "ghcr.io/example/lkm-service:" + "a" * 40, "lkm"],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "MOCK_LOG": str(log),
            "MOCK_FAIL": "1" if fail_preflight else "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, log.read_text(encoding="utf-8").splitlines()


def test_deploy_updates_and_waits_for_all_targets(tmp_path: Path) -> None:
    rc, calls = _run(tmp_path)
    assert rc == 0
    updates = [call for call in calls if " set image " in call]
    rollouts = [call for call in calls if " rollout status " in call]
    assert len(updates) == len(rollouts) == 15
    assert any(
        "deployment/backend backend=ghcr.io/example/lkm-service:" in c for c in updates
    )
    assert any("deployment/prefect-worker" in c for c in updates)


def test_deploy_preflight_failure_makes_no_changes(tmp_path: Path) -> None:
    rc, calls = _run(tmp_path, fail_preflight=True)
    assert rc != 0
    assert not any(" set image " in call for call in calls)
