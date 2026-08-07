"""Repository Map — maintains symbol index and file type classification for V5."""

import ast
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        self.map_path = self.project_root / ".ai" / "repository_map.json"
        self._map: dict[str, Any] = self._load()

    def _load(self) -> dict:
        if self.map_path.exists():
            try:
                return json.loads(self.map_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to load repo map: {e}")
        return self._empty_map()

    def _save(self):
        self.map_path.parent.mkdir(parents=True, exist_ok=True)
        self.map_path.write_text(json.dumps(self._map, indent=2, default=str))

    def _empty_map(self) -> dict:
        return {
            "generated_at": None,
            "files": [],
            "symbols": [],
            "categories": {},
        }

    def _classify_file(self, file_path: Path) -> str:
        """Classify a file into a category."""
        rel = str(file_path.relative_to(self.project_root))
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
            tree = ast.parse(file_path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    symbols.append({
                        "name": node.name,
                        "type": "class",
                        "line": node.lineno,
                        "file": str(file_path.relative_to(self.project_root)),
                    })
                elif isinstance(node, ast.FunctionDef):
                    symbols.append({
                        "name": node.name,
                        "type": "function",
                        "line": node.lineno,
                        "file": str(file_path.relative_to(self.project_root)),
                    })
        except (SyntaxError, OSError) as e:
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
            rel = str(f.relative_to(self.project_root))

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
        return [s for s in self._map.get("symbols", []) if s["file"] == file_path]

    def find_symbol(self, name: str) -> list[dict]:
        """Find a symbol by name across the project."""
        return [s for s in self._map.get("symbols", []) if name.lower() in s["name"].lower()]

    def get_category_files(self, category: str) -> list[str]:
        """Get all files in a category."""
        return self._map.get("categories", {}).get(category, {}).get("files", [])

    def get_context_summary(self, max_files: int = 30) -> str:
        """Get a condensed summary of the repo map for prompt injection."""
        categories = self._map.get("categories", {})
        symbols = self._map.get("symbols", [])

        lines = [f"Repository: {self.project_root.name}"]
        lines.append(f"Total files: {len(self._map.get('files', []))}")
        lines.append(f"Total symbols: {len(symbols)}")

        for cat, info in categories.items():
            lines.append(f"  {cat}: {info.get('count', 0)} files")

        if symbols:
            lines.append(f"\nTop-level symbols ({min(len(symbols), 20)} shown):")
            for s in symbols[:20]:
                lines.append(f"  {s['type']} {s['name']} ({s['file']}:{s['line']})")

        return "\n".join(lines)

    @property
    def is_loaded(self) -> bool:
        return bool(self._map.get("symbols"))