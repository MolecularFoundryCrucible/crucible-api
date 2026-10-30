import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("state", ["missing", "enabled", "suspended", "error"])
def test_cleanup_deploy_preserves_activation(tmp_path, state):
    manifest = yaml.safe_load((ROOT / "k8s-conf/hub-auth-cleanup-staging.yaml").read_text())
    stub = tmp_path / "kubectl"
    stub.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ["TEST_ROOT"])
if args[:2] == ["set", "image"]:
    document = json.loads(os.environ["TEST_MANIFEST"])
    image = next(arg.split("=", 1)[1] for arg in args if arg.startswith("cleanup="))
    document["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"] = image
    print(json.dumps(document))
elif args[0] == "get":
    if os.environ["TEST_STATE"] == "error":
        sys.exit(1)
    if os.environ["TEST_STATE"] != "missing":
        print("cronjob.batch/crucible-hub-auth-cleanup-staging")
elif "--local" in args:
    document = json.loads(pathlib.Path(args[args.index("-f") + 1]).read_text())
    assert json.loads(args[args.index("-p") + 1]) == [{"op": "remove", "path": "/spec/suspend"}]
    del document["spec"]["suspend"]
    print(json.dumps(document))
else:
    option = "-f" if args[0] == "create" else "--patch-file"
    document = json.loads(pathlib.Path(args[args.index(option) + 1]).read_text())
    (root / "write.json").write_text(json.dumps({"operation": args[0], "document": document}))
''')
    stub.chmod(0o755)
    image = "example.test/crucible-api-staging:abc1234"
    result = subprocess.run(
        ["bash", "scripts/deploy_hub_auth_cleanup.sh", "staging", image],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "TEST_ROOT": str(tmp_path),
             "TEST_MANIFEST": json.dumps(manifest), "TEST_STATE": state},
        capture_output=True, text=True,
    )
    if state == "error":
        assert result.returncode != 0
        assert not (tmp_path / "write.json").exists()
        return
    assert result.returncode == 0, result.stderr
    written = json.loads((tmp_path / "write.json").read_text())
    spec = written["document"]["spec"]
    assert spec["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"] == image
    if state == "missing":
        assert written["operation"] == "create"
        assert spec["suspend"] is True
    else:
        assert written["operation"] == "patch"
        assert "suspend" not in spec


def test_cloud_build_deploys_only_main_and_uses_same_release_image_for_cleanup():
    build = yaml.safe_load((ROOT / "cloudbuild.yaml").read_text())
    step = next(step for step in build["steps"] if step.get("id") == "deploy-main")
    assert "if [ '${BRANCH_NAME}' != main ]; then" in step["args"][-1]
    assert [s for s in build["steps"] if "kubectl" in s["name"]] == [step]
    assert step["entrypoint"] == "bash"
    assert "gcloud container clusters get-credentials" in step["args"][-1]
    assert "bash scripts/deploy_hub_auth_cleanup.sh '${BRANCH_NAME}' '${_IMAGE_PATH}:${SHORT_SHA}'" in step["args"][-1]
