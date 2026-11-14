from svn_obskit import cli


def test_init_claude_installs_skill_and_claude_md(tmp_path, capsys):
    assert cli.main(["init-claude", "--path", str(tmp_path)]) == 0
    skill = tmp_path / ".claude" / "skills" / "obs-integration"
    assert (skill / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: obs-integration")
    assert (skill / "recipes.md").exists()
    assert cli.BEGIN in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")


def test_init_claude_keeps_existing_claude_md_and_is_idempotent(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Project rules\n\nkeep me\n", encoding="utf-8")
    cli.main(["init-claude", "--path", str(tmp_path)])
    cli.main(["init-claude", "--path", str(tmp_path), "--force"])
    text = md.read_text(encoding="utf-8")
    assert "keep me" in text
    assert text.count(cli.BEGIN) == 1 and text.count(cli.END) == 1


def test_refuses_to_overwrite_without_force(tmp_path, capsys):
    cli.main(["init-claude", "--path", str(tmp_path)])
    assert cli.main(["init-claude", "--path", str(tmp_path)]) == 1
    assert "--force" in capsys.readouterr().err


def test_no_claude_md_flag(tmp_path):
    cli.main(["init-claude", "--path", str(tmp_path), "--no-claude-md"])
    assert not (tmp_path / "CLAUDE.md").exists()


def test_skill_uses_only_real_sdk_names():
    """Recipes must not drift from the SDK: every symbol they import has to exist."""
    import importlib

    for module, name in [
        ("svn_obskit", "Observability"), ("svn_obskit", "WorkflowTracker"), ("svn_obskit", "current_context"),
        ("svn_obskit", "use_context"), ("svn_obskit.middleware.fastapi", "ObservabilityMiddleware"),
        ("svn_obskit.core.correlation", "inject_headers"), ("svn_obskit.core.correlation", "extract_context"),
        ("svn_obskit.integrations.httpx", "client"), ("svn_obskit.tracing.decorators", "span"),
    ]:  # fmt: skip
        assert hasattr(importlib.import_module(module), name), f"{module}.{name}"
