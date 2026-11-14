"""`obskit` command line. Currently one command: install the bundled Claude skill into a repo."""

import argparse
import shutil
import sys
from importlib import resources
from pathlib import Path

SKILL_NAME = "obs-integration"
BEGIN = "<!-- obskit:begin -->"
END = "<!-- obskit:end -->"

CLAUDE_MD_SNIPPET = f"""{BEGIN}
## Observability (svn-obskit)

This service reports to the OBS platform through `svn-obskit` (`import svn_obskit`).
When you add or change code for services, workers, jobs, workflows/steps, logging, tracing or metrics,
use the `obs-integration` skill (`.claude/skills/obs-integration/`): it explains how to classify what the
code does and which SDK call reports it. Configure via `OBSKIT_*` env vars; never hard-code the API key.
{END}
"""


def _skill_source() -> Path:
    return Path(str(resources.files("svn_obskit").joinpath("agent_skill", SKILL_NAME)))


def install_skill(target_root: Path, *, force: bool = False) -> Path:
    """Copy the skill to `<target_root>/skills/obs-integration`. Refuses to overwrite unless `force`."""
    dest = target_root / "skills" / SKILL_NAME
    if dest.exists():
        if not force:
            raise FileExistsError(f"{dest} already exists (use --force to replace it)")
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_skill_source(), dest)
    return dest


def update_claude_md(path: Path) -> str:
    """Add or refresh the marked snippet in CLAUDE.md, leaving the rest of the file untouched."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        new = head + CLAUDE_MD_SNIPPET.rstrip("\n") + tail
        action = "updated"
    else:
        new = text + ("\n" if text and not text.endswith("\n\n") else "") + CLAUDE_MD_SNIPPET
        action = "added to"
    path.write_text(new, encoding="utf-8")
    return action


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obskit")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init-claude", help="install the obs-integration Claude skill into this repo")
    p.add_argument("--path", default=".", help="repo root (default: current directory)")
    p.add_argument("--user", action="store_true", help="install into ~/.claude instead of the repo")
    p.add_argument("--force", action="store_true", help="replace an existing skill copy")
    p.add_argument("--no-claude-md", action="store_true", help="do not touch CLAUDE.md")
    args = parser.parse_args(argv)

    if args.user:
        claude_dir = Path.home() / ".claude"
    else:
        claude_dir = Path(args.path).resolve() / ".claude"
    try:
        dest = install_skill(claude_dir, force=args.force)
    except FileExistsError as exc:
        print(f"obskit: {exc}", file=sys.stderr)
        return 1
    print(f"installed skill: {dest}")
    if not args.user and not args.no_claude_md:
        md = Path(args.path).resolve() / "CLAUDE.md"
        print(f"CLAUDE.md {update_claude_md(md)}: {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
