import argparse
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

POD_SPEC_PATHS = {
    "Pod": ("spec",),
    "Deployment": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "ReplicaSet": ("spec", "template", "spec"),
    "Job": ("spec", "template", "spec"),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
}
# Read by the image outside the settings groups, so not reported as unknown.
OTHER_VARIABLES = frozenset({
    "ALEMBIC_PRODUCTION_DATABASE", "API_VERSION", "AUTH_CACHE_MAXSIZE", "AUTH_CACHE_TTL", "BRANCH_NAME",
    "CRUCIBLE_SECRETS_DIR", "DB_CONF_FILE", "FORWARDED_ALLOW_IPS", "GITHASH", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT", "MAX_UPLOAD_BYTES", "MULTIPART_DATASET_BYTES", "MULTIPART_DATASET_SESSIONS",
    "MULTIPART_OWNER_BYTES", "MULTIPART_OWNER_SESSIONS", "MULTIPART_SIGNING_INTERVAL_SECONDS",
    "MULTIPART_UPLOAD_BUCKET", "MULTIPART_UPLOAD_ENABLED", "MULTIPART_UPLOAD_PROJECT", "MULTIPART_UPLOAD_SIGNER",
    "PORT", "PYTHONUNBUFFERED", "THUMBNAIL_LARGE_LOOKUP_WARNING_SIZE", "THUMBNAIL_MAX_SOURCE_BYTES",
    "THUMBNAIL_MAX_SOURCE_PIXELS", "WEB_CONCURRENCY",
})
# Settings the server no longer reads, with what replaced them.
RETIRED_VARIABLES = {
    name: "Google storage uses Application Default Credentials" for name in ("GCS_SA", "GCS_SA_FILE", "GCS_SA_VERSION")
}
HIDDEN = "unchecked"
DEFAULT_SECRETS_DIR = "/run/secrets"
GKE_SECRET_MANAGER_DRIVER = "secrets-store-gke.csi.k8s.io"


@dataclass
class Container:
    """One container's view of its settings: its environment and the files mounted for it. A value
    the manifests cannot show (a Kubernetes Secret, a field reference) is the placeholder HIDDEN."""
    workload: str
    name: str
    environment: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    secret_manager_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_documents(sources: list[str]) -> list[dict]:
    """Kubernetes objects from YAML files, or standard input for "-"; `kind: List` is flattened."""
    documents = []
    for source in sources:
        text = sys.stdin.read() if source == "-" else Path(source).read_text()
        for document in yaml.safe_load_all(text):
            if not isinstance(document, dict):
                continue
            if document.get("kind") == "List":
                documents.extend(item for item in document.get("items") or [] if isinstance(item, dict))
            else:
                documents.append(document)
    return documents


def _key(document: dict) -> tuple[str, str]:
    return document.get("kind", ""), document.get("metadata", {}).get("name", "")


def _dig(document: dict, path: tuple[str, ...]):
    for step in path:
        document = (document or {}).get(step)
    return document


def _object_keys(document: dict | None) -> list[str] | None:
    if document is None:
        return None
    return sorted({*(document.get("data") or {}), *(document.get("stringData") or {}), *(document.get("binaryData") or {})})


def _volume_files(volume: dict, mount_path: str, objects: dict, container: Container) -> None:
    """Record the files a volume mount provides: their path and, for Secret Manager, where they come from."""
    if "secret" in volume:
        secret = volume["secret"]
        items = secret.get("items")
        keys = [item["path"] for item in items] if items else _object_keys(objects.get(("Secret", secret.get("secretName", ""))))
        if keys is None:
            container.warnings.append(f"volume {volume['name']} mounts Secret {secret.get('secretName')}, which is not in the input, so its files are unknown")
            return
        for key in keys:
            container.files[str(PurePosixPath(mount_path) / key)] = HIDDEN
    elif "csi" in volume and volume["csi"].get("driver") == GKE_SECRET_MANAGER_DRIVER:
        class_name = (volume["csi"].get("volumeAttributes") or {}).get("secretProviderClass", "")
        provider = objects.get(("SecretProviderClass", class_name))
        if provider is None:
            container.errors.append(f"volume {volume['name']} uses SecretProviderClass {class_name}, which is not in the input")
            return
        for entry in yaml.safe_load((provider.get("spec", {}).get("parameters") or {}).get("secrets") or "[]") or []:
            container.files[str(PurePosixPath(mount_path) / entry["path"])] = HIDDEN
            container.secret_manager_paths.append(entry["resourceName"])
    elif "configMap" in volume:
        config_map = objects.get(("ConfigMap", volume["configMap"].get("name", "")))
        for key, value in ((config_map or {}).get("data") or {}).items():
            container.files[str(PurePosixPath(mount_path) / key)] = str(value)


def _resolve_environment(spec: dict, objects: dict, container: Container) -> None:
    for source in spec.get("envFrom") or []:
        prefix = source.get("prefix", "")
        if "configMapRef" in source:
            config_map = objects.get(("ConfigMap", source["configMapRef"].get("name", "")))
            if config_map is None:
                if not source["configMapRef"].get("optional"):
                    container.errors.append(f"envFrom ConfigMap {source['configMapRef'].get('name')} is not in the input")
                continue
            for key, value in (config_map.get("data") or {}).items():
                container.environment[prefix + key] = str(value)
        elif "secretRef" in source:
            keys = _object_keys(objects.get(("Secret", source["secretRef"].get("name", ""))))
            if keys is None:
                container.warnings.append(f"envFrom Secret {source['secretRef'].get('name')} is not in the input, so the variables it sets are unknown")
                continue
            for key in keys:
                container.environment[prefix + key] = HIDDEN
    for entry in spec.get("env") or []:
        name = entry["name"]
        if "value" in entry:
            container.environment[name] = str(entry["value"])
            continue
        source = entry.get("valueFrom") or {}
        if "configMapKeyRef" in source:
            reference = source["configMapKeyRef"]
            data = (objects.get(("ConfigMap", reference.get("name", ""))) or {}).get("data") or {}
            if reference.get("key") in data:
                container.environment[name] = str(data[reference["key"]])
            elif not reference.get("optional"):
                container.errors.append(f"{name} refers to key {reference.get('key')} of ConfigMap {reference.get('name')}, which the input does not have")
        elif source:
            container.environment[name] = HIDDEN
        else:
            container.environment[name] = ""


def find_containers(documents: list[dict]) -> list[Container]:
    objects = {_key(document): document for document in documents}
    containers = []
    for document in documents:
        path = POD_SPEC_PATHS.get(document.get("kind", ""))
        pod = _dig(document, path) if path else None
        if not pod:
            continue
        volumes = {volume["name"]: volume for volume in pod.get("volumes") or []}
        for spec in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            container = Container(workload="/".join(_key(document)), name=spec.get("name", ""))
            _resolve_environment(spec, objects, container)
            for mount in spec.get("volumeMounts") or []:
                volume = volumes.get(mount.get("name"))
                if volume is not None:
                    _volume_files(volume, mount["mountPath"], objects, container)
            containers.append(container)
    return containers


@contextmanager
def _as_container(container: Container):
    """Make the process see exactly the container's environment and secret files while the settings are read."""
    from app import config

    saved_environment, saved_directory, saved_loaded = dict(os.environ), os.getcwd(), set(config._loaded_env_files)
    with tempfile.TemporaryDirectory() as scratch:
        secrets_dir = PurePosixPath(container.environment.get("CRUCIBLE_SECRETS_DIR") or DEFAULT_SECRETS_DIR)
        local_secrets = Path(scratch, "secrets")
        local_secrets.mkdir()
        environment = dict(container.environment)
        for path, content in container.files.items():
            if PurePosixPath(path).parent == secrets_dir:
                Path(local_secrets, PurePosixPath(path).name).write_text(content)
        for name, value in container.environment.items():
            if name.endswith("_FILE") and value and value in container.files:
                local = Path(scratch, "pointed", value.lstrip("/"))
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_text(container.files[value])
                environment[name] = str(local)
        environment["CRUCIBLE_SECRETS_DIR"] = str(local_secrets)
        try:
            os.environ.clear()
            os.environ.update(environment)
            os.chdir(scratch)
            config._loaded_env_files.clear()
            config.get_settings.cache_clear()
            yield
        finally:
            os.environ.clear()
            os.environ.update(saved_environment)
            os.chdir(saved_directory)
            config._loaded_env_files.clear()
            config._loaded_env_files.update(saved_loaded)
            config.get_settings.cache_clear()


def _unknown_variables(environment: dict[str, str]) -> list[str]:
    from app import config

    known = set(OTHER_VARIABLES) | set(RETIRED_VARIABLES)
    for group in config.GROUPS.values():
        for name in map(group.env_name, group.model_fields):
            known |= {name, f"{name}_FILE"}
    known |= {f"{name}_VERSION" for name in config._SECRETS}
    return sorted(name for name in environment if name.upper() not in known)


def _pointed_files(container: Container) -> list[str]:
    from app import config

    names = {group.env_name(field) for group in config.GROUPS.values() for field in group.model_fields}
    return [
        f"{name} points to {value}, which no volume in the pod provides"
        for name, value in sorted(container.environment.items())
        if name.upper().endswith("_FILE") and name.upper()[:-5] in names and value and value != HIDDEN and value not in container.files
    ]


def _secret_versions(names: list[str]) -> list[str]:
    """Well-formed Secret Manager paths of the named secrets that are not given another way."""
    from app import config

    versions = [config.optional_env(f"{name}_VERSION") for name in names if config.configured_secret(name) is None]
    return [version for version in versions if version and config._SECRET_VERSION.fullmatch(version)]


def verify_secret_versions(paths: list[str]) -> list[str]:
    """Errors for Secret Manager versions that do not exist or are not enabled, from their metadata
    only (secretmanager.versions.get); no secret value is read."""
    from google.cloud import secretmanager

    errors = []
    with secretmanager.SecretManagerServiceClient() as client:
        for path in sorted(set(paths)):
            try:
                version = client.get_secret_version(request={"name": path}, timeout=10)
            except Exception as error:
                errors.append(f"{path}: {error.__class__.__name__}")
                continue
            if version.state.name != "ENABLED":
                errors.append(f"{path}: version is {version.state.name}")
    return errors


def check_container(container: Container, profile: str, verify_secrets: bool = False) -> Report:
    """Would this container's program start with its settings? `profile` is "server" (the API server:
    the full startup check) or "database" (a database-only tool such as the Hub sign-in cleanup job)."""
    from app import config

    report = Report(errors=list(container.errors), warnings=list(container.warnings))
    report.errors += _pointed_files(container)
    if profile == "server":
        report.warnings += [f"{name} is not a Crucible setting" for name in _unknown_variables(container.environment)]
        report.warnings += [f"{name} is no longer read ({reason}); remove it" for name, reason in RETIRED_VARIABLES.items() if name in container.environment]
    paths = list(container.secret_manager_paths)
    with _as_container(container):
        malformed = [name for name in config._SECRETS if (config.optional_env(f"{name}_VERSION") or "").strip() and not config._SECRET_VERSION.fullmatch(config.optional_env(f"{name}_VERSION"))]
        report.errors += [f"{name}_VERSION must be a full projects/<project>/secrets/<secret>/versions/<number or latest> path" for name in malformed]
        try:
            settings = config.get_settings()
            if profile == "server":
                config.check_settings()
                if settings.hub.enabled and settings.hub.orcid_issuer != settings.api.orcid_issuer:
                    report.warnings.append(f"HUB_AUTH_ORCID_ISSUER ({settings.hub.orcid_issuer}) and ORCID_ISSUER ({settings.api.orcid_issuer}) differ, so a person signing in through both is refused")
                names = config.secrets_in_use()
            else:
                missing = settings.db.missing("server", "username", "database", "password")
                if missing:
                    raise config.ConfigurationError(f"Missing required settings: {', '.join(missing)}")
                names = ["PGDB_PW"]
            paths += _secret_versions(names)
        except config.ConfigurationError as error:
            report.errors.append(str(error))
    if verify_secrets and paths and not report.errors:
        report.errors += verify_secret_versions(paths)
    return report


def check_documents(documents: list[dict], servers: list[str], databases: list[str], verify_secrets: bool = False) -> list[tuple[Container, str, Report]]:
    containers = find_containers(documents)
    results = []
    for profile, names in (("server", servers), ("database", databases)):
        for name in names:
            matches = [container for container in containers if container.name == name]
            if not matches:
                found = ", ".join(sorted({container.name for container in containers})) or "none"
                results.append((Container(workload="-", name=name), profile, Report(errors=[f"no container named {name} in the input (found: {found})"])))
            for container in matches:
                results.append((container, profile, check_container(container, profile, verify_secrets)))
    return results


def register(groups: argparse._SubParsersAction) -> None:
    commands = groups.add_parser("settings", help="Check deployment settings").add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check",
        help="Check that containers in rendered Kubernetes manifests would start with their settings",
        description="Reads Kubernetes YAML (for example `kubectl kustomize overlays/staging`), resolves each named container's environment from its ConfigMaps and mounted secret files, and runs the same checks the program runs at startup. Values held in Kubernetes Secrets are treated as given but not checked. Exits 1 when any check fails.",
    )
    check.add_argument("sources", nargs="+", metavar="FILE", help="YAML file, or - for standard input")
    check.add_argument("--server", action="append", default=[], metavar="CONTAINER", help="A container running the API server (repeatable)")
    check.add_argument("--database", action="append", default=[], metavar="CONTAINER", help="A container running a database-only tool, such as the Hub sign-in cleanup job (repeatable)")
    check.add_argument("--verify-secrets", action="store_true", help="Also confirm with Secret Manager that every referenced secret version exists and is enabled, without reading values (needs secretmanager.versions.get)")
    check.set_defaults(handler=_run_check)


def _run_check(args: argparse.Namespace) -> int:
    if not args.server and not args.database:
        print("Name the containers to check with --server or --database.", file=sys.stderr)
        return 2
    results = check_documents(load_documents(args.sources), args.server, args.database, args.verify_secrets)
    failed = False
    for container, profile, report in results:
        status = "fails" if report.errors else "ok"
        print(f"{container.workload} {container.name} ({profile}): {status}")
        for message in report.errors:
            print(f"  error: {message}")
        for message in report.warnings:
            print(f"  warning: {message}")
        failed = failed or bool(report.errors)
    return 1 if failed else 0
