"""Read a repository into structural observations and engineering history.

The scanner does not execute project code. It parses syntax, manifests, and
an optional ``.twin/history.jsonl`` event log. Git history is included only
when the directory is itself the git toplevel, so a nested sample is not
mixed with a parent repository.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.settings import HISTORY_WINDOW_DAYS, MAX_FILE_BYTES, MAX_FILES

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "coverage",
    ".idea",
    ".vscode",
    "data",
}

LANG_BY_SUFFIX = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
}

SECURITY_RULES = [
    (
        "hardcoded-secret",
        re.compile(
            r"(?i)\b(api[_-]?token|secret|password|api[_-]?key)\b\s*=\s*['\"][^'\"]+['\"]"
        ),
    ),
    (
        "sql-injection",
        re.compile(r"(?i)f(['\"])\s*(select|insert|update|delete)\b"),
    ),
    (
        "unverified-tls",
        re.compile(r"verify\s*=\s*False"),
    ),
    (
        "dangerous-exec",
        re.compile(r"\b(eval|exec)\s*\("),
    ),
]

PERF_RULES = [
    ("unbounded-loop", re.compile(r"while\s+True\b")),
    ("blocking-sleep", re.compile(r"time\.sleep\s*\(")),
    ("select-star", re.compile(r"(?i)\bselect\s+\*\s+from\b")),
]

HEALTH_RE = re.compile(r"(?i)(/health\b|def\s+health\b|HEALTHCHECK\b)")
TODO_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")
DYNAMIC_PY_RE = re.compile(r"importlib\.import_module\s*\(|__import__\s*\(")
DYNAMIC_JS_RE = re.compile(r"(?<![\w.])import\s*\(")
JS_IMPORT_RE = re.compile(
    r"""(?:import|export)\s+(?:[^'"\n]+?\s+from\s+)?['\"]([^'\"]+)['\"]|require\(\s*['\"]([^'\"]+)['\"]\s*\)"""
)
GO_IMPORT_RE = re.compile(r'^\s*(?:[\w.]+\s+)?"([^"]+)"', re.MULTILINE)


class _Complexity(ast.NodeVisitor):
    def __init__(self) -> None:
        self.score = 1

    def _bump(self, node: ast.AST, amount: int = 1) -> None:
        self.score += amount
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self._bump(node)

    def visit_For(self, node: ast.For) -> None:
        self._bump(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._bump(node)

    def visit_While(self, node: ast.While) -> None:
        self._bump(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._bump(node)

    def visit_With(self, node: ast.With) -> None:
        self._bump(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._bump(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        self._bump(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._bump(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self._bump(node, max(1, len(node.values) - 1))

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._bump(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._bump(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._bump(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._bump(node)


def scan_repository(root: Path, now: datetime | None = None) -> dict:
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a directory")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    warnings: list[str] = []
    files = _walk(root, warnings)
    modules: list[dict] = []
    dependencies: set[str] = set()
    packages: list[dict] = []
    has_health = False

    for path in files:
        rel = path.relative_to(root).as_posix()
        name = path.name
        if name == "requirements.txt":
            specs = _requirement_specs(path, rel)
            dependencies.update(item["name"] for item in specs)
            packages.extend(specs)
            continue
        if name == "package.json":
            specs = _package_json_specs(path, rel)
            dependencies.update(item["name"] for item in specs)
            packages.extend(specs)
            continue
        if name == "pubspec.yaml":
            specs = _pubspec_packages(path, rel)
            dependencies.update(item["name"] for item in specs)
            packages.extend(specs)
            continue
        if name == "Dockerfile" or name.startswith("Dockerfile"):
            text = _read(path)
            evidence = []
            if re.search(r"(?im)^USER\s+root\b", text) or not re.search(r"(?im)^USER\s+\S+", text):
                line = _line_of(text, r"(?im)^USER\s+root\b") or 1
                evidence.append(
                    {
                        "path": rel,
                        "line": line,
                        "kind": "container-root",
                        "text": "Container runs as root or never sets USER.",
                    }
                )
            if HEALTH_RE.search(text):
                has_health = True
            modules.append(_module(rel, "dockerfile", "config", text, 0, evidence))
            continue

        language = LANG_BY_SUFFIX.get(path.suffix)
        if not language:
            continue
        text = _read(path)
        if HEALTH_RE.search(text):
            has_health = True
        role = _role(rel)
        evidence = [] if role == "test" else _smells(rel, text)
        complexity = _python_complexity(text) if language == "python" else _rough_complexity(text)
        module = _module(rel, language, role, text, complexity, evidence)
        module["raw_imports"] = _raw_imports(rel, language, text)
        module["import_specs"] = len(module["raw_imports"])
        module["dynamic_imports"] = _dynamic_count(language, text)
        modules.append(module)

    index = _python_index(modules)
    known = {module["path"] for module in modules}
    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for module in modules:
        resolved, external, unresolved = _resolve(module, index, known)
        module["imports"] = resolved
        module["external_imports"] = external
        module["unresolved_imports"] = unresolved
        module.pop("raw_imports", None)
        for target in resolved:
            key = (module["path"], target)
            if key in seen or key[0] == key[1]:
                continue
            seen.add(key)
            edges.append({"source": module["path"], "target": target})

    edges.sort(key=lambda e: (e["source"], e["target"]))
    modules.sort(key=lambda m: m["path"])
    events = _history(root, moment) + _git_history(root, moment)
    events.sort(key=lambda e: e["age_days"])

    return {
        "root": str(root),
        "name": root.name,
        "description": _description(root),
        "scanned_at": moment.astimezone(timezone.utc).isoformat(),
        "modules": modules,
        "edges": edges,
        "events": events,
        "dependencies": sorted(dependencies),
        "packages": packages,
        "signals": {"has_health": has_health},
        "warnings": warnings,
    }


def _module(rel: str, language: str, role: str, text: str, complexity: int, evidence: list[dict]) -> dict:
    return {
        "path": rel,
        "language": language,
        "service": _service(rel),
        "layer": _layer(rel),
        "role": role,
        "loc": _loc(text),
        "complexity": complexity,
        "todos": len(TODO_RE.findall(text)),
        "evidence": evidence,
        "imports": [],
        "external_imports": [],
    }


def _walk(root: Path, warnings: list[str]) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS and not name.startswith("."))
        # Keep the hidden .twin directory so history can live there, but do not
        # treat it as source. os.walk was told to skip dotted names, so read
        # history separately.
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            found.append(Path(dirpath) / name)
            if len(found) >= MAX_FILES:
                warnings.append(f"Scan stopped after {MAX_FILES} files.")
                return found
    return found


def _read(path: Path) -> str:
    data = path.read_bytes()[:MAX_FILE_BYTES]
    if b"\0" in data[:1024]:
        return ""
    return data.decode("utf-8", errors="replace")


def _loc(text: str) -> int:
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        count += 1
    return count


def _role(rel: str) -> str:
    parts = Path(rel).parts
    name = Path(rel).name
    if "tests" in parts or "test" in parts or name.startswith("test_") or name.endswith("_test.py"):
        return "test"
    return "code"


def _service(rel: str) -> str:
    parts = Path(rel).parts
    if not parts:
        return "root"
    if parts[0] == "clients":
        return "client"
    if parts[0] == "deploy":
        return "deploy"
    if parts[0] in {"services", "libs"} and len(parts) > 1:
        return parts[1]
    return parts[0]


def _layer(rel: str) -> str:
    return Path(rel).parts[0]


def _python_complexity(text: str) -> int:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _rough_complexity(text)
    visitor = _Complexity()
    visitor.visit(tree)
    return visitor.score


def _rough_complexity(text: str) -> int:
    keywords = len(re.findall(r"\b(if|for|while|case|catch|elif|else)\b", text))
    return 1 + keywords


def _smells(rel: str, text: str) -> list[dict]:
    evidence = []
    for kind, pattern in SECURITY_RULES + PERF_RULES:
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            snippet = text.splitlines()[line - 1].strip()[:180]
            evidence.append({"path": rel, "line": line, "kind": kind, "text": snippet})
    return evidence


def _line_of(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text)
    if not match:
        return None
    return text[: match.start()].count("\n") + 1


def _python_index(modules: list[dict]) -> dict[str, str]:
    index = {}
    for module in modules:
        if module["language"] != "python":
            continue
        dotted = Path(module["path"]).with_suffix("").as_posix().replace("/", ".")
        index[dotted] = module["path"]
        if dotted.endswith(".__init__"):
            index[dotted[: -len(".__init__")]] = module["path"]
    return index


def _raw_imports(rel: str, language: str, text: str) -> list[str]:
    if language == "python":
        return _python_imports(rel, text)
    if language in {"javascript", "typescript"}:
        return _js_imports(rel, text)
    if language == "go":
        return _go_imports(text)
    return []


def _dynamic_count(language: str, text: str) -> int:
    if language == "python":
        return len(DYNAMIC_PY_RE.findall(text))
    if language in {"javascript", "typescript"}:
        return len(DYNAMIC_JS_RE.findall(text))
    return 0


def _python_imports(rel: str, text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    package = Path(rel).with_suffix("").parts[:-1]
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    found.append(node.module)
                continue
            base = list(package)
            up = node.level - 1
            if up:
                base = base[:-up] if up <= len(base) else []
            extra = node.module.split(".") if node.module else []
            dotted = ".".join([*base, *extra])
            if dotted:
                found.append(dotted)
    return found


def _js_imports(rel: str, text: str) -> list[str]:
    found = []
    for match in JS_IMPORT_RE.finditer(text):
        spec = match.group(1) or match.group(2)
        if not spec:
            continue
        if spec.startswith("."):
            normalized = os.path.normpath(str(Path(rel).parent / spec)).replace("\\", "/")
            found.append(f"file:{normalized}")
        else:
            found.append(f"pkg:{spec}")
    return found


def _go_imports(text: str) -> list[str]:
    return [match.group(1) for match in GO_IMPORT_RE.finditer(text)]


def _resolve(module: dict, index: dict[str, str], known: set[str]) -> tuple[list[str], list[str], list[str]]:
    local: list[str] = []
    external: list[str] = []
    unresolved: list[str] = []
    language = module["language"]
    tops = {key.split(".")[0] for key in index}
    for spec in module.get("raw_imports", []):
        if not spec:
            continue
        if language == "python":
            target = _lookup_dotted(spec, index)
            if target:
                local.append(target)
            elif spec.split(".")[0] in tops:
                unresolved.append(spec)
            elif not _stdlib(spec):
                external.append(spec.split(".")[0])
        elif language in {"javascript", "typescript"}:
            if spec.startswith("file:"):
                target = _lookup_path(spec[5:], known)
                if target:
                    local.append(target)
                else:
                    unresolved.append(spec[5:])
            elif spec.startswith("pkg:"):
                external.append(spec[4:].split("/")[0])
        elif language == "go" and not _stdlib(spec):
            external.append(spec.split("/")[0])
    return sorted(set(local)), sorted(set(external)), sorted(set(unresolved))


def _lookup_dotted(spec: str, index: dict[str, str]) -> str | None:
    if spec in index:
        return index[spec]
    parts = spec.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in index:
            return index[candidate]
        parts.pop()
    return None


def _lookup_path(spec: str, known: set[str]) -> str | None:
    candidates = [spec]
    for ext in (".ts", ".tsx", ".js", ".jsx", ".py"):
        candidates.append(spec + ext)
        candidates.append(spec.rstrip("/") + "/index" + ext)
    for candidate in candidates:
        if candidate in known:
            return candidate
    return None


def _stdlib(spec: str) -> bool:
    root = spec.split(".")[0]
    return root in sys.stdlib_module_names or root in {"app", "std"}


def _requirements(path: Path) -> set[str]:
    return {item["name"] for item in _requirement_specs(path, path.name)}


def _requirement_specs(path: Path, manifest: str) -> list[dict]:
    specs = []
    for line in _read(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name = re.split(r"[<>=!\[]", line, maxsplit=1)[0].strip()
        version = ""
        match = re.search(r"(?:==|>=|<=|~=)\s*([^,\s;]+)", line)
        if match:
            version = match.group(1)
        if name:
            specs.append({"name": name, "version": version, "manifest": manifest, "plugins": []})
    return specs


def _package_json(path: Path) -> set[str]:
    return {item["name"] for item in _package_json_specs(path, path.name)}


def _package_json_specs(path: Path, manifest: str) -> list[dict]:
    try:
        data = json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return []
    specs = []
    for key in ("dependencies", "devDependencies"):
        for name, version in (data.get(key) or {}).items():
            specs.append(
                {
                    "name": str(name),
                    "version": version if isinstance(version, str) else "",
                    "manifest": manifest,
                    "plugins": [],
                }
            )
    return specs


def _pubspec_packages(path: Path, manifest: str) -> list[dict]:
    """Dependencies and native plugin bindings from a Flutter pubspec.

    The parser is line-oriented. It keeps package names under dependencies and
    the android package declared under flutter.plugin.platforms.
    """
    root_name = ""
    deps: list[tuple[str, str]] = []
    plugins: list[str] = []
    section: list[str] = []
    for raw_line in _read(path).splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        key, _, value = raw_line.strip().partition(":")
        key = key.strip()
        value = value.strip()
        section = section[: indent // 2]
        section.append(key)
        if section == ["name"] and value:
            root_name = value.strip("\"'")
        if len(section) == 2 and section[0] in {"dependencies", "dev_dependencies"} and key not in {"sdk", "flutter"}:
            version = value.strip("\"'")
            if version in {"", "sdk"}:
                version = ""
            deps.append((key, version))
        if "platforms" in section and key == "package" and value:
            plugins.append(value.strip("\"'"))
    specs = [
        {"name": name, "version": version, "manifest": manifest, "plugins": []}
        for name, version in deps
        if name != "flutter"
    ]
    if root_name and plugins:
        specs.append({"name": root_name, "version": "", "manifest": manifest, "plugins": plugins})
    return specs


def _description(root: Path) -> str:
    readme = root / "README.md"
    if not readme.exists():
        return ""
    lines: list[str] = []
    for line in _read(readme).splitlines():
        if line.startswith("#"):
            continue
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line.strip())
    return " ".join(lines)[:500]


def _history(root: Path, now: datetime) -> list[dict]:
    path = root / ".twin" / "history.jsonl"
    if not path.exists():
        path = root / "twin.history.jsonl"
    if not path.exists():
        return []
    events = []
    for line_no, line in enumerate(_read(path).splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            events.append(_normalize_event(json.loads(line), now))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return events


def event_from_payload(obj: dict, now: datetime | None = None) -> dict:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return _normalize_event(obj, moment)


def _normalize_event(obj: dict, now: datetime) -> dict:
    if "age_days" in obj and obj["age_days"] is not None:
        age = float(obj["age_days"])
        ts = now - timedelta(days=age)
    else:
        raw = str(obj["ts"]).replace("Z", "+00:00")
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (now - ts).total_seconds() / 86400
    files: list[str] = []
    file_stats: dict[str, dict[str, int]] = {}
    raw_files = obj.get("files") or {}
    if isinstance(raw_files, dict):
        for rel, stats in raw_files.items():
            rel = str(rel)
            files.append(rel)
            if isinstance(stats, list) and len(stats) >= 2:
                file_stats[rel] = {"add": int(stats[0]), "del": int(stats[1])}
            elif isinstance(stats, dict):
                file_stats[rel] = {"add": int(stats.get("add", 0)), "del": int(stats.get("del", 0))}
    elif isinstance(raw_files, list):
        files.extend(str(item) for item in raw_files)
    if obj.get("file"):
        files.append(str(obj["file"]))
    summary = str(obj.get("summary") or obj.get("message") or obj.get("type") or "event")
    return {
        "ts": ts.astimezone(timezone.utc).isoformat(),
        "age_days": age,
        "clock": "age" if "age_days" in obj and obj["age_days"] is not None else "ts",
        "type": str(obj.get("type", "note")),
        "service": obj.get("service"),
        "files": files,
        "summary": summary,
        "data": {
            "author": obj.get("author"),
            "severity": obj.get("severity"),
            "result": obj.get("result"),
            "hours": obj.get("hours"),
            "file_stats": file_stats,
            "package": obj.get("package"),
            "version": obj.get("version"),
            "prior": obj.get("prior"),
            "manifest": obj.get("manifest"),
            "tests": [str(item) for item in obj.get("tests") or []],
            "id": None if obj.get("id") is None else str(obj.get("id")),
            "status": obj.get("status"),
            "merged_age": obj.get("merged_age"),
            "plugin": obj.get("plugin"),
        },
    }


def _git_history(root: Path, now: datetime) -> list[dict]:
    if not _own_git_repo(root):
        return []
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "log",
                "--numstat",
                "--date=iso-strict",
                "--pretty=format:---%an%x09%aI%x09%s",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    events = []
    author = ""
    ts: datetime | None = None
    summary = ""
    stats: dict[str, dict[str, int]] = {}

    def flush() -> None:
        nonlocal author, ts, summary, stats
        if ts is None:
            return
        age = (now - ts).total_seconds() / 86400
        if 0 <= age <= HISTORY_WINDOW_DAYS and stats:
            events.append(
                {
                    "ts": ts.astimezone(timezone.utc).isoformat(),
                    "age_days": age,
                    "clock": "ts",
                    "type": "commit",
                    "service": None,
                    "files": list(stats),
                    "summary": summary,
                    "data": {
                        "author": author,
                        "severity": None,
                        "result": None,
                        "hours": None,
                        "file_stats": stats,
                    },
                }
            )
        author, ts, summary, stats = "", None, "", {}

    for line in proc.stdout.splitlines():
        if line.startswith("---"):
            flush()
            parts = line[3:].split("\t", 2)
            if len(parts) < 3:
                continue
            author, raw_ts, summary = parts
            try:
                ts = datetime.fromisoformat(raw_ts)
            except ValueError:
                ts = None
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            continue
        columns = line.split("\t")
        if len(columns) != 3 or ts is None:
            continue
        added, deleted, rel = columns
        stats[rel] = {
            "add": 0 if added == "-" else int(added or 0),
            "del": 0 if deleted == "-" else int(deleted or 0),
        }
    flush()
    return events


def _own_git_repo(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    try:
        return Path(proc.stdout.strip()).resolve() == root.resolve()
    except OSError:
        return False
