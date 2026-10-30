from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_cloud_build_only_builds_and_releases_copy_the_main_image():
    build = yaml.safe_load((ROOT / "cloudbuild.yaml").read_text())
    assert [step["id"] for step in build["steps"]] == ["build"]
    assert not any("kubectl" in step["name"] for step in build["steps"])
    script = build["steps"][0]["args"][-1]
    assert "image=us-central1-docker.pkg.dev/${PROJECT_ID}/crucible-api/crucible-api\n" in script
    assert '--tag "$$image:${COMMIT_SHA}"' in script and '--tag "$$image:main"' in script
    release = yaml.safe_load((ROOT / "cloudbuild.release.yaml").read_text())
    copy = release["steps"][0]["args"][-1]
    assert "docker build" not in copy
    assert 'gcloud container images add-tag --quiet "${_BUILDS}@$$digest"' in copy
    assert release["substitutions"]["_BUILDS"].endswith("/crucible-api/crucible-api")
