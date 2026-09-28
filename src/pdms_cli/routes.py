"""API routes of a PDMS repo, read from its Terraform API Gateway definition.

``infra/infra_auto/environments/<env>/api_rsc_*.tf`` chains ``api_gw_rsc`` modules (``parent_id`` + ``path_part``) into
resource paths, ``api_gw_meth`` modules bind an ``http_method`` on a resource to a Lambda module, and the Lambda module's
``lambda_path`` points at ``backend/<domain>/<service>/``. Parsing takes a few seconds, so the result is cached until
a ``.tf`` file changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from .instances import state_dir

REF = re.compile(r"module\.([A-Za-z0-9_-]+)(?:\[\d+\])?\.")
CACHE_VERSION = 1


@dataclass(frozen=True)
class Route:
    method: str
    path: str  # e.g. /api/v1/leads/tp/{entity_id}
    service: str  # relative to the backend folder, e.g. lead/lead-tp-list

    @property
    def segments(self) -> list[str]:
        return [s for s in self.path.split("/") if s]


def terraform_dir(repo_root: Path, env: str) -> Path:
    return repo_root / "infra" / "infra_auto" / "environments" / env


def _unquote(value: object) -> str:
    text = str(value).strip()
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] == '"' else text


def _ref(value: object) -> str | None:
    match = REF.search(str(value))
    return match.group(1) if match else None


def _parse_file(path: str) -> list[tuple[str, dict]]:
    import hcl2

    try:
        data = hcl2.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001 - a broken file only loses its own routes
        return []
    modules = []
    for block in data.get("module", []):
        for name, body in block.items():
            modules.append((_unquote(name), {k: v for k, v in body.items() if not k.startswith("__")}))
    return modules


def _fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.tf")):
        stat = path.stat()
        digest.update(f"{path.name}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def parse_routes(repo_root: Path, env: str = "dev") -> list[Route]:
    directory = terraform_dir(repo_root, env)
    files = [str(p) for p in sorted(directory.glob("*.tf"))]
    if len(files) < 40:  # starting worker processes costs more than parsing a handful of files
        parsed = [_parse_file(f) for f in files]
    else:
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
            parsed = list(pool.map(_parse_file, files, chunksize=8))
    modules: dict[str, tuple[str, dict]] = {}
    for file, file_modules in zip(files, parsed):
        for name, body in file_modules:
            modules[name] = (file, body)

    def resource_path(name: str, seen: frozenset = frozenset()) -> str | None:
        if name not in modules or name in seen:
            return ""  # the API root (or a reference we cannot follow)
        _, body = modules[name]
        if "path_part" not in body:
            return ""
        parent = _ref(body.get("parent_id", ""))
        prefix = resource_path(parent, seen | {name}) if parent else ""
        return f"{prefix}/{_unquote(body['path_part'])}"

    backend = (repo_root / "backend").resolve()
    routes = set()
    for _, body in modules.values():
        if "http_method" not in body:
            continue
        resource, lambda_module = _ref(body.get("resource_id", "")), _ref(body.get("integration_lambda_invoke_arn", ""))
        if not resource or not lambda_module or lambda_module not in modules:
            continue
        lambda_file, lambda_body = modules[lambda_module]
        if "lambda_path" not in lambda_body:
            continue
        service_dir = (Path(lambda_file).parent / _unquote(lambda_body["lambda_path"])).resolve()
        try:
            service = str(service_dir.relative_to(backend))
        except ValueError:
            continue
        path = resource_path(resource)
        if path:
            routes.add(Route(_unquote(body["http_method"]).upper(), path, service))
    return sorted(routes, key=lambda r: (r.path, r.method))


def load_routes(repo_root: Path, env: str = "dev") -> list[Route]:
    """Routes of ``repo_root``, from the cache when no ``.tf`` file changed."""
    directory = terraform_dir(repo_root, env)
    if not directory.is_dir():
        return []
    cache = state_dir() / "routes" / (hashlib.sha1(f"{repo_root.resolve()}|{env}".encode()).hexdigest() + ".json")
    fingerprint = _fingerprint(directory)
    try:
        data = json.loads(cache.read_text())
        if data.get("version") == CACHE_VERSION and data.get("fingerprint") == fingerprint:
            return [Route(**r) for r in data["routes"]]
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        pass
    routes = parse_routes(repo_root, env)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"version": CACHE_VERSION, "fingerprint": fingerprint, "routes": [asdict(r) for r in routes]}))
    return routes


def _specificity(route: Route) -> tuple:
    # API Gateway prefers literal segments over {param} and {param+}, segment by segment.
    return tuple(2 if s.endswith("+}") else 1 if s.startswith("{") else 0 for s in route.segments)


def _matches(route: Route, segments: list[str]) -> bool:
    template = route.segments
    for i, part in enumerate(template):
        if part.startswith("{") and part.endswith("+}"):
            return len(segments) > i
        if i >= len(segments):
            return False
        if not (part.startswith("{") and part.endswith("}")) and part != segments[i]:
            return False
    return len(template) == len(segments)


def match(routes: list[Route], method: str, path: str) -> Route | None:
    segments = [s for s in path.split("?", 1)[0].split("/") if s]
    candidates = [r for r in routes if r.method in (method.upper(), "ANY") and _matches(r, segments)]
    return min(candidates, key=_specificity) if candidates else None
