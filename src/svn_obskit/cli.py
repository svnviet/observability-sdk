"""`obskit` command line: install the bundled Claude skills into a repo; publish a service profile to OBS."""

import argparse
import json
import shutil
import sys
from importlib import resources
from pathlib import Path

SKILL_NAME = "obs-integration"
SKILLS = (SKILL_NAME, "obs-service-profile")
BEGIN = "<!-- obskit:begin -->"
END = "<!-- obskit:end -->"

CLAUDE_MD_SNIPPET = f"""{BEGIN}
## Observability (svn-obskit)

This service reports to the OBS platform through `svn-obskit` (`import svn_obskit`).
When you add or change code for services, workers, jobs, workflows/steps, logging, tracing or metrics,
use the `obs-integration` skill (`.claude/skills/obs-integration/`): it explains how to classify what the
code does and which SDK call reports it. To describe this service's features and capabilities on OBS (its
detail page), use the `obs-service-profile` skill. Configure via `OBSKIT_*` env vars; never hard-code the API key.
{END}
"""


def _skill_source(name: str = SKILL_NAME) -> Path:
    return Path(str(resources.files("svn_obskit").joinpath("agent_skill", name)))


def install_skill(target_root: Path, *, force: bool = False) -> Path:
    """Copy every bundled skill to `<target_root>/skills/<name>`; return the obs-integration path.

    Refuses to overwrite an existing copy unless `force`; nothing is copied if any target exists.
    """
    dests = {name: target_root / "skills" / name for name in SKILLS}
    existing = [d for d in dests.values() if d.exists()]
    if existing and not force:
        raise FileExistsError(f"{existing[0]} already exists (use --force to replace it)")
    for name, dest in dests.items():
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(_skill_source(name), dest)
    return dests[SKILL_NAME]


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


def _profile_publish(args) -> int:
    from svn_obskit.profile import ProfileError, build_profile, publish_profile

    try:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        if args.dry_run:
            build_profile(data)
            print("profile is valid (dry run, nothing sent)")
            return 0
        publish_profile(args.service, data)
    except (OSError, json.JSONDecodeError, ProfileError) as exc:
        print(f"obskit: {exc}", file=sys.stderr)
        return 1
    print(f"profile published for {args.service}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obskit")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init-claude", help="install the obs-integration Claude skill into this repo")
    p.add_argument("--path", default=".", help="repo root (default: current directory)")
    p.add_argument("--user", action="store_true", help="install into ~/.claude instead of the repo")
    p.add_argument("--force", action="store_true", help="replace an existing skill copy")
    p.add_argument("--no-claude-md", action="store_true", help="do not touch CLAUDE.md")
    prof = sub.add_parser("profile", help="describe this service's features and capabilities on OBS")
    prof_sub = prof.add_subparsers(dest="profile_command", required=True)
    pub = prof_sub.add_parser(
        "publish", help="validate a profile JSON file and store it on the service's OBS page"
    )
    pub.add_argument("file", help="profile JSON written by the obs-service-profile skill")
    pub.add_argument("--service", required=True, help="service_name exactly as the service registers it")
    pub.add_argument("--dry-run", action="store_true", help="only validate; send nothing")
    args = parser.parse_args(argv)
    if args.command == "profile":
        return _profile_publish(args)

    if args.user:
        claude_dir = Path.home() / ".claude"
    else:
        claude_dir = Path(args.path).resolve() / ".claude"
    try:
        dest = install_skill(claude_dir, force=args.force)
    except FileExistsError as exc:
        print(f"obskit: {exc}", file=sys.stderr)
        return 1
    print(f"installed skills: {dest.parent} ({', '.join(SKILLS)})")
    if not args.user and not args.no_claude_md:
        md = Path(args.path).resolve() / "CLAUDE.md"
        print(f"CLAUDE.md {update_claude_md(md)}: {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
