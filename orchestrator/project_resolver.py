"""Deterministically identify the files that make up a project's runtime."""

from __future__ import annotations

import ast
import json
import re
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 used by current desktop builds
    import tomli as tomllib
from collections import defaultdict, deque
from html.parser import HTMLParser
from pathlib import Path


IGNORED_DIRS = {
    ".ai", ".git", ".hg", ".svn", ".venv", "venv", "__pycache__",
    "node_modules", "dist", "build", "coverage", ".next", ".nuxt",
}
SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx"}
JS_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx")
JS_IMPORT_RE = re.compile(
    r"(?:\bimport\s*(?:[^'\";]*?\s+from\s*)?|\bexport\s+[^'\";]*?\s+from\s*|"
    r"\brequire\s*\(|\bimport\s*\()\s*['\"]([^'\"]+)['\"]"
)
JS_DECLARATION_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:class|function|const|let|var)\s+"
    r"([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


class _HtmlReferences(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self.styles: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        values = {str(key).lower(): value for key, value in attrs}
        if tag.lower() == "script" and values.get("src"):
            self.scripts.append(str(values["src"]))
        if (
            tag.lower() == "link"
            and str(values.get("rel") or "").lower() == "stylesheet"
            and values.get("href")
        ):
            self.styles.append(str(values["href"]))


class ProjectResolver:
    """Build a portable active-surface snapshot without using a model."""

    def __init__(self, project_root: str | Path):
        self.root = Path(project_root).resolve()
        self.files = self._inventory()
        self.file_set = set(self.files)

    def resolve(self) -> dict:
        package = self._read_json("package.json")
        pyproject = self._read_toml("pyproject.toml")
        entrypoints = self._entrypoints(package, pyproject)
        runtime_groups = [
            {
                "entrypoint": item["path"],
                "kind": item["kind"],
                "files": sorted(self._runtime_closure([item])),
            }
            for item in entrypoints
        ]
        active = {
            path for group in runtime_groups for path in group["files"]
        }
        commands = self._commands(package, pyproject, entrypoints)
        duplicates = self._duplicate_js_symbols()
        runtime_sources = {
            path for path in self.files
            if Path(path).suffix.lower() in SOURCE_SUFFIXES | {".html", ".css"}
        }
        entry_kinds = {item["kind"] for item in entrypoints}
        browser_runtime = bool(entry_kinds & {"browser", "javascript"})
        legacy = sorted(
            path for path in (runtime_sources - active)
            if browser_runtime
            and Path(path).suffix.lower() in set(JS_SUFFIXES) | {".html", ".css"}
            and not self._is_test_file(path)
        ) if active else []
        support = sorted(
            path for path in self.files
            if path not in active and path not in legacy
            and (
                self._is_test_file(path)
                or Path(path).name.lower() in {
                    "readme", "readme.md", "package-lock.json", "pnpm-lock.yaml",
                    "yarn.lock", "requirements.txt", "pyproject.toml", "setup.cfg",
                    "tox.ini", "pytest.ini", "tsconfig.json", "vite.config.js",
                    "vite.config.ts", "webpack.config.js",
                }
            )
        )
        ambiguities: list[str] = []
        if self.files and not entrypoints:
            ambiguities.append("未找到可确定的运行入口；执行时应先确认启动文件。")
        browser_entries = [
            item for item in entrypoints if item["kind"] == "browser"
        ]
        if len(browser_entries) > 1:
            ambiguities.append(
                "检测到多个浏览器入口："
                + ", ".join(item["path"] for item in browser_entries[:8])
            )
        active_duplicates = [
            item for item in duplicates
            if len(set(item["files"]) & active) > 1
        ]
        if active_duplicates:
            ambiguities.append(
                "当前运行路径包含重复的顶层声明："
                + ", ".join(item["name"] for item in active_duplicates[:8])
            )

        confidence = 0.25
        if entrypoints:
            confidence = max(item["confidence"] for item in entrypoints)
        if ambiguities:
            confidence = max(0.2, confidence - min(0.3, 0.1 * len(ambiguities)))
        return {
            "version": 1,
            "project_root": str(self.root),
            "entrypoints": entrypoints,
            "active_files": sorted(active),
            "runtime_groups": runtime_groups,
            "support_files": support,
            "legacy_files": legacy,
            "duplicate_symbols": duplicates,
            "commands": commands,
            "ambiguities": ambiguities,
            "confidence": round(confidence, 2),
            "file_count": len(self.files),
        }

    def _inventory(self) -> list[str]:
        if not self.root.exists():
            return []
        result = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root)
            if any(part in IGNORED_DIRS for part in relative.parts):
                continue
            result.append(relative.as_posix())
        return sorted(result)

    def _entrypoints(self, package: dict, pyproject: dict) -> list[dict]:
        found: dict[str, dict] = {}

        def add(path: str, kind: str, confidence: float, source: str):
            normalized = self._existing_path(path)
            if not normalized:
                return
            current = found.get(normalized)
            item = {
                "path": normalized, "kind": kind,
                "confidence": confidence, "source": source,
            }
            if current is None or confidence > current["confidence"]:
                found[normalized] = item

        for candidate in ("index.html", "public/index.html", "site/index.html"):
            add(candidate, "browser", 0.98 if candidate == "index.html" else 0.9,
                "conventional browser entry")
        for field in ("main", "module", "browser"):
            value = package.get(field)
            if isinstance(value, str):
                add(value, "javascript", 0.96, f"package.json {field}")
        for script_name in ("start", "dev", "serve"):
            script = str((package.get("scripts") or {}).get(script_name) or "")
            for candidate in self._paths_from_command(script):
                add(candidate, "javascript", 0.9, f"package.json scripts.{script_name}")
        if not any(item["kind"] == "javascript" for item in found.values()):
            for candidate in (
                "src/main.tsx", "src/main.ts", "src/main.jsx", "src/main.js",
                "src/index.ts", "src/index.js", "server.js", "app.js",
            ):
                if candidate in self.file_set:
                    add(candidate, "javascript", 0.82,
                        "conventional JavaScript entry")
                    break

        scripts = ((pyproject.get("project") or {}).get("scripts") or {})
        for name, target in scripts.items():
            module = str(target).split(":", 1)[0].strip()
            add(module.replace(".", "/") + ".py", "python", 0.98,
                f"pyproject project.scripts.{name}")
        for candidate in ("app/main.py", "main.py", "src/main.py", "__main__.py"):
            add(candidate, "python", 0.94 if candidate == "app/main.py" else 0.88,
                "conventional Python entry")

        if not found:
            html_files = [path for path in self.files if path.endswith(".html")]
            if len(html_files) == 1:
                add(html_files[0], "browser", 0.82, "only HTML file")
        return sorted(found.values(), key=lambda item: (-item["confidence"], item["path"]))

    def _runtime_closure(self, entrypoints: list[dict]) -> set[str]:
        active: set[str] = set()
        queue = deque(item["path"] for item in entrypoints)
        while queue:
            relative = queue.popleft()
            if relative in active or relative not in self.file_set:
                continue
            active.add(relative)
            suffix = Path(relative).suffix.lower()
            references: list[str] = []
            if suffix == ".html":
                parser = _HtmlReferences()
                try:
                    parser.feed(self._read_text(relative))
                    references = parser.scripts + parser.styles
                except (OSError, UnicodeError):
                    references = []
            elif suffix in JS_SUFFIXES:
                references = JS_IMPORT_RE.findall(self._read_text(relative))
            elif suffix == ".py":
                references = self._python_imports(relative)
            for reference in references:
                resolved = self._resolve_reference(relative, reference)
                if resolved and resolved not in active:
                    queue.append(resolved)

        for metadata in ("package.json", "pyproject.toml"):
            if metadata in self.file_set and entrypoints:
                active.add(metadata)
        return active

    def _python_imports(self, relative: str) -> list[str]:
        try:
            tree = ast.parse(self._read_text(relative))
        except (SyntaxError, OSError, UnicodeError):
            return []
        references = []
        package = list(Path(relative).parent.parts)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                references.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = (
                    package[:max(0, len(package) - max(0, node.level - 1))]
                    if node.level else []
                )
                module = str(node.module or "").split(".") if node.module else []
                joined = "/".join(base + module)
                if joined:
                    references.append(joined)
        return references

    def _resolve_reference(self, source: str, reference: str) -> str | None:
        clean = str(reference or "").split("?", 1)[0].split("#", 1)[0]
        if not clean or clean.startswith(("http://", "https://", "//", "data:")):
            return None
        source_parent = Path(source).parent
        if clean.startswith("/"):
            base = Path(clean.lstrip("/"))
        elif Path(source).suffix.lower() == ".html":
            base = source_parent / clean
        elif clean.startswith("."):
            base = source_parent / clean
        elif "/" in clean and not clean.startswith("@"):
            base = Path(clean)
        else:
            base = Path(clean.replace(".", "/"))
        candidates = [base]
        if not base.suffix:
            candidates.extend(Path(str(base) + suffix) for suffix in JS_SUFFIXES)
            candidates.extend(Path(str(base / "index") + suffix) for suffix in JS_SUFFIXES)
            candidates.extend((Path(str(base) + ".py"), base / "__init__.py"))
        for candidate in candidates:
            normalized = Path(candidate).as_posix().lstrip("./")
            if normalized in self.file_set:
                return normalized
        return None

    def _duplicate_js_symbols(self) -> list[dict]:
        declarations: dict[str, set[str]] = defaultdict(set)
        for relative in self.files:
            if Path(relative).suffix.lower() not in JS_SUFFIXES:
                continue
            try:
                for name in JS_DECLARATION_RE.findall(self._read_text(relative)):
                    declarations[name].add(relative)
            except (OSError, UnicodeError):
                continue
        return [
            {"name": name, "files": sorted(files)}
            for name, files in sorted(declarations.items()) if len(files) > 1
        ]

    def _commands(self, package: dict, pyproject: dict,
                  entrypoints: list[dict]) -> dict:
        scripts = package.get("scripts") or {}
        commands = {
            "start": next((f"npm run {name}" for name in ("dev", "start", "serve")
                           if scripts.get(name)), ""),
            "test": "npm test" if scripts.get("test") else "",
            "syntax": "",
        }
        if scripts.get("build"):
            commands["build"] = "npm run build"
        python_entries = [item["path"] for item in entrypoints if item["kind"] == "python"]
        if python_entries:
            commands["start"] = commands["start"] or f"python {python_entries[0]}"
            commands["syntax"] = "python -m compileall -q ."
            if any(path.startswith("test_") or "/test_" in path for path in self.files):
                commands["test"] = commands["test"] or "python -m pytest -q"
        elif any(Path(path).suffix.lower() in JS_SUFFIXES for path in self.files):
            commands["syntax"] = str(scripts.get("build") and "npm run build" or "")
        browser_entries = [item["path"] for item in entrypoints if item["kind"] == "browser"]
        if browser_entries and not commands["start"]:
            commands["start"] = "python -m http.server 8000"
            commands["browser_entry"] = browser_entries[0]
        return {key: value for key, value in commands.items() if value}

    def _existing_path(self, value: str) -> str | None:
        normalized = str(value or "").replace("\\", "/").lstrip("./")
        return normalized if normalized in self.file_set else None

    @staticmethod
    def _paths_from_command(command: str) -> list[str]:
        return re.findall(r"[A-Za-z0-9_./-]+\.(?:js|mjs|cjs|ts|jsx|tsx|py|html)", command)

    @staticmethod
    def _is_test_file(relative: str) -> bool:
        path = Path(relative)
        lowered_parts = {part.lower() for part in path.parts}
        stem = path.stem.lower()
        return bool(
            lowered_parts & {"test", "tests", "spec", "specs", "__tests__"}
            or stem.startswith("test_") or stem.endswith(("_test", ".test", ".spec"))
        )

    def _read_text(self, relative: str) -> str:
        path = self.root / relative
        # Import and declaration discovery does not benefit from loading large
        # generated bundles into memory. Entrypoints normally appear near the top.
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(2 * 1024 * 1024)

    def _read_json(self, relative: str) -> dict:
        try:
            value = json.loads(self._read_text(relative))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _read_toml(self, relative: str) -> dict:
        try:
            with (self.root / relative).open("rb") as handle:
                value = tomllib.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, tomllib.TOMLDecodeError):
            return {}
