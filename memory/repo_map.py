"""Repository Map — maintains symbol index and file type classification for V5."""

import ast
import json
import logging
import re
import tokenize
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.paths import project_state_dir

logger = logging.getLogger(__name__)

# File type classification
FILE_CATEGORIES = {
    "source": {
        "extensions": {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".cpp", ".c", ".h"},
        "description": "Source code files",
    },
    "config": {
        "extensions": {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"},
        "description": "Configuration files",
    },
    "markup": {
        "extensions": {".md", ".html", ".css", ".scss", ".less", ".xml", ".svg"},
        "description": "Markup and styles",
    },
    "data": {
        "extensions": {".csv", ".tsv", ".sql", ".db", ".sqlite"},
        "description": "Data files",
    },
    "test": {
        "extensions": set(),
        "patterns": [r"test_", r"_test", r"spec_", r"_spec", r"conftest"],
        "description": "Test files (detected by name pattern)",
    },
}


class RepoMap:
    """Maintains a symbol index and file type classification for the project.

    The map is stored as .ai/repository_map.json and updated incrementally.
    """

    def __init__(self, project_root: str,
                 state_dir: str | Path | None = None):
        self.project_root = Path(project_root).resolve()
        self.state_dir = Path(state_dir) if state_dir else project_state_dir(
            self.project_root
        )
        self.map_path = self.state_dir / "repository_map.json"
        self._map: dict[str, Any] = self._load()

    def _load(self) -> dict:
        if self.map_path.exists():
            try:
                loaded = json.loads(self.map_path.read_text(encoding="utf-8"))
                return self._normalize_stored_paths(loaded)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load repo map: {e}")
        return self._empty_map()

    @staticmethod
    def _portable_path(value: str | Path) -> str:
        """Serialize project-relative paths identically on every platform."""
        return str(value or "").replace("\\", "/")

    @classmethod
    def _normalize_stored_paths(cls, data: dict) -> dict:
        """Migrate repository maps previously persisted with Windows slashes."""
        if not isinstance(data, dict):
            return data
        for file_info in data.get("files", []):
            if isinstance(file_info, dict):
                file_info["path"] = cls._portable_path(file_info.get("path", ""))
        for symbol in data.get("symbols", []):
            if isinstance(symbol, dict):
                symbol["file"] = cls._portable_path(symbol.get("file", ""))
        for category in (data.get("categories") or {}).values():
            if isinstance(category, dict):
                category["files"] = [
                    cls._portable_path(path)
                    for path in (category.get("files") or [])
                ]
        return data

    def _save(self):
        try:
            self.map_path.parent.mkdir(parents=True, exist_ok=True)
            self.map_path.write_text(
                json.dumps(
                    self._map, indent=2, ensure_ascii=False, default=str
                ),
                encoding="utf-8",
            )
        except OSError as error:
            # The in-memory map remains usable if a drive becomes read-only.
            logger.warning("Failed to save repo map %s: %s", self.map_path, error)

    def _empty_map(self) -> dict:
        return {
            "generated_at": None,
            "files": [],
            "symbols": [],
            "categories": {},
        }

    def _classify_file(self, file_path: Path) -> str:
        """Classify a file into a category."""
        ext = file_path.suffix.lower()

        # Check test patterns first
        for cat_name, cat_info in FILE_CATEGORIES.items():
            if cat_name == "test":
                for pattern in cat_info["patterns"]:
                    if re.search(pattern, file_path.stem):
                        return "test"
            elif ext in cat_info["extensions"]:
                return cat_name

        return "other"

    def _extract_symbols(self, file_path: Path) -> list[dict]:
        """Extract symbols from a Python file using AST."""
        symbols = []
        if file_path.suffix != ".py":
            return symbols

        try:
            # tokenize.open honors Python encoding declarations and handles
            # non-ASCII Windows source paths/content correctly.
            with tokenize.open(file_path) as source:
                tree = ast.parse(source.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    symbols.append({
                        "name": node.name,
                        "type": "class",
                        "line": node.lineno,
                        "file": file_path.relative_to(self.project_root).as_posix(),
                    })
                elif isinstance(node, ast.FunctionDef):
                    symbols.append({
                        "name": node.name,
                        "type": "function",
                        "line": node.lineno,
                        "file": file_path.relative_to(self.project_root).as_posix(),
                    })
        except (SyntaxError, UnicodeError, OSError) as e:
            logger.debug(f"Could not parse {file_path}: {e}")

        return symbols

    def update(self):
        """Scan the project and rebuild the repository map."""
        files = []
        all_symbols = []
        categories: dict[str, list[str]] = {}

        for f in self.project_root.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(self.project_root).as_posix()

            # Skip hidden dirs and common non-project dirs
            parts = Path(rel).parts
            if any(p.startswith(".") for p in parts if p != "."):
                continue
            if any(p in parts for p in {"__pycache__", "node_modules", "venv", ".venv", ".git"}):
                continue

            cat = self._classify_file(f)
            categories.setdefault(cat, []).append(rel)
            symbols = self._extract_symbols(f)
            all_symbols.extend(symbols)

            files.append({
                "path": rel,
                "category": cat,
                "symbols": len(symbols),
            })

        self._map = {
            "generated_at": str(datetime.now(timezone.utc)),
            "files": files,
            "symbols": all_symbols,
            "categories": {k: {"count": len(v), "files": v[:50]} for k, v in categories.items()},
        }
        self._save()
        logger.info(f"RepoMap updated: {len(files)} files, {len(all_symbols)} symbols")

    def get_symbols_for_file(self, file_path: str) -> list[dict]:
        """Get all symbols defined in a specific file."""
        normalized = self._portable_path(file_path)
        return [
            symbol for symbol in self._map.get("symbols", [])
            if self._portable_path(symbol.get("file", "")) == normalized
        ]

    def find_symbol(self, name: str) -> list[dict]:
        """Find a symbol by name across the project."""
        return [s for s in self._map.get("symbols", []) if name.lower() in s["name"].lower()]

    def get_category_files(self, category: str) -> list[str]:
        """Get all files in a category."""
        return self._map.get("categories", {}).get(category, {}).get("files", [])

    def get_context_summary(self, max_files: int = 30) -> str:
        """Get a condensed summary of the repo map for prompt injection."""
        categories = self._map.get("categories", {})
        files = self._map.get("files", [])
        symbols = self._map.get("symbols", [])

        lines = [f"Repository: {self.project_root.name}"]
        lines.append(f"Total files: {len(files)}")
        lines.append(f"Total symbols: {len(symbols)}")

        for cat, info in categories.items():
            lines.append(f"  {cat}: {info.get('count', 0)} files")

        if files:
            shown_files = files[:max_files]
            lines.append(f"\nProject files ({len(shown_files)} shown):")
            for file_info in shown_files:
                lines.append(
                    f"  {file_info.get('path', '')} "
                    f"[{file_info.get('category', 'other')}]"
                )

        if symbols:
            lines.append(f"\nTop-level symbols ({min(len(symbols), 20)} shown):")
            for s in symbols[:20]:
                lines.append(f"  {s['type']} {s['name']} ({s['file']}:{s['line']})")

        return "\n".join(lines)

    @property
    def is_loaded(self) -> bool:
        # Static HTML/CSS/data projects legitimately have no extractable Python
        # symbols. Their file inventory is still valuable planning context.
        return bool(self._map.get("files"))
