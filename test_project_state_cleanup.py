"""Project removal must reset generated state without deleting project files."""

import os
import subprocess

import pytest

import app.paths as app_paths


def test_project_state_cleanup_removes_local_and_fallback_state(
    tmp_path, monkeypatch,
):
    project = tmp_path / "Demo Project"
    project.mkdir()
    source = project / "main.py"
    source.write_text("print('keep')\n", encoding="utf-8")
    user_data = tmp_path / "user-data"
    monkeypatch.setattr(app_paths, "app_data_dir", lambda: user_data)
    local_state, fallback_state = app_paths.project_state_paths(project)
    (local_state / "skills" / "demo").mkdir(parents=True)
    (local_state / "repository_map.json").write_text("{}", encoding="utf-8")
    fallback_state.mkdir(parents=True)
    (fallback_state / "project.md").write_text("stale", encoding="utf-8")

    removed = app_paths.remove_project_state(project)

    assert removed == [local_state, fallback_state]
    assert not local_state.exists()
    assert not fallback_state.exists()
    assert source.read_text(encoding="utf-8") == "print('keep')\n"
    assert project.exists()


def test_project_state_cleanup_unlinks_ai_symlink_without_following_it(
    tmp_path, monkeypatch,
):
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external-state"
    external.mkdir()
    protected = external / "keep.txt"
    protected.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        app_paths, "app_data_dir", lambda: tmp_path / "user-data"
    )
    state_link = project / ".ai"
    try:
        state_link.symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    removed = app_paths.remove_project_state(project)

    assert removed == [state_link]
    assert not os.path.lexists(state_link)
    assert protected.read_text(encoding="utf-8") == "keep"


def test_project_state_cleanup_unregisters_generated_git_worktrees(
    tmp_path, monkeypatch,
):
    project = tmp_path / "repo"
    project.mkdir()
    monkeypatch.setattr(
        app_paths, "app_data_dir", lambda: tmp_path / "user-data"
    )

    def git(*arguments):
        return subprocess.run(
            ["git", *arguments], cwd=project, capture_output=True,
            text=True, check=True,
        )

    git("init", "-b", "main")
    git("config", "user.name", "RockCore Test")
    git("config", "user.email", "test@rockcore.local")
    (project / "README.md").write_text("demo\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-m", "baseline")
    worktree = project / ".ai" / "worktrees" / "ai-task"
    worktree.parent.mkdir(parents=True)
    git("worktree", "add", "-b", "ai/test", str(worktree))
    assert str(worktree) in git("worktree", "list", "--porcelain").stdout

    app_paths.remove_project_state(project)

    listing = git("worktree", "list", "--porcelain").stdout
    assert str(worktree) not in listing
    assert not (project / ".ai").exists()
