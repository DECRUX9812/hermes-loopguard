"""Classify the EFFECT of a shell command before it runs.

Pure, deterministic, offline. The tool gate can only add friction: it may
block a command or escalate it to the human approval gate, never remove one
of Hermes' own approvals (there is no "allow" directive).
"""
from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass

log = logging.getLogger(__name__)

CATEGORIES = ("read", "modify_allowed", "modify_other", "destructive", "network", "unclear")
SEVERITY = {"read": 0, "modify_allowed": 1, "modify_other": 2, "unclear": 3, "network": 4,
            "destructive": 5}

DEFAULT_POLICY = {"read": None, "modify_allowed": None, "modify_other": "approve",
                  "unclear": "approve", "network": "approve", "destructive": "block"}

READ_CMDS = {"ls", "cat", "head", "tail", "less", "grep", "rg", "wc", "echo", "printf", "pwd",
             "stat", "file", "which", "type", "ps", "df", "du", "date", "uname", "whoami", "id",
             "tree", "jq", "sort", "uniq", "cut", "tr", "diff", "cmp", "basename", "dirname",
             "realpath"}
MODIFY_CMDS = {"cp", "mv", "mkdir", "touch", "tee", "ln", "patch", "rmdir"}
NETWORK_CMDS = {"curl", "wget", "http", "httpie", "ssh", "scp", "sftp", "nc", "ncat", "telnet",
                "ftp", "gh", "aws", "gcloud", "az"}

_GIT_PLAIN_READ = ("status", "log", "diff", "show", "blame", "rev-parse", "ls-files", "describe")
_GIT_MODIFY = ("add", "merge", "tag", "cherry-pick", "revert", "apply", "am", "mv", "rm", "switch")

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DURATION_RE = re.compile(r"^\d+$|^\d+[smhd]$")
_PUNCT_OPS = {"|", "||", "&&", ";", "&"}
_WRITE_OPS = (">>", "&>", ">|", ">")


@dataclass
class ToolVerdict:
    category: str
    reasons: list[str]
    directive: dict | None


def _split_segments(command: str) -> list[list[str]] | None:
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        tokens = list(lex)
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _PUNCT_OPS:
            segments.append(current)
            current = []
        else:
            current.append(tok)
    segments.append(current)
    return [seg for seg in segments if seg]


def _strip_prefixes(tokens: list[str]) -> list[str] | None:
    """Drop VAR=value assignments and env/nice/timeout/command prefixes.

    Returns the remaining tokens, or None when sudo/doas makes the segment
    destructive outright.
    """
    while tokens:
        head = tokens[0]
        if _ASSIGN_RE.match(head):
            tokens = tokens[1:]
            continue
        if head in ("sudo", "doas"):
            return None
        if head in ("env", "nice", "command"):
            tokens = tokens[1:]
            if head == "env":
                while tokens and (_ASSIGN_RE.match(tokens[0]) or tokens[0].startswith("-")):
                    tokens = tokens[1:]
            elif head == "nice":
                while tokens and (tokens[0].startswith("-") or tokens[0].isdigit()):
                    tokens = tokens[1:]
            continue
        if head == "timeout":
            tokens = tokens[1:]
            if tokens and _DURATION_RE.match(tokens[0]):
                tokens = tokens[1:]
            continue
        return tokens
    return []


def _scan_redirects(tokens: list[str]) -> list[str]:
    targets: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _WRITE_OPS:
            prev = tokens[i - 1] if i > 0 else ""
            if prev.isdigit():  # fd redirect like 2> file
                i += 2
                continue
            if i + 1 < len(tokens):
                targets.append(tokens[i + 1])
                i += 2
                continue
        i += 1
    return targets


def _path_allowed(path: str, cwd: str, allowed: list[str]) -> bool:
    if path == "/dev/null":
        return True
    if os.path.isabs(path):
        resolved = os.path.realpath(path)
    else:
        resolved = os.path.realpath(os.path.join(cwd, path))
    return any(resolved == a or resolved.startswith(a + os.sep) for a in allowed)


def _modify_verdict(paths: list[str], *, allowed: list[str], cwd: str, reasons: list[str]):
    checked = paths or [cwd]
    outside = [p for p in checked if not _path_allowed(p, cwd, allowed)]
    if outside:
        return ("modify_other",
                reasons + [f"paths outside the allowed set: {', '.join(outside[:3])}"])
    return ("modify_allowed", reasons)


def _runner_verdict(cwd: str, allowed: list[str]):
    if _path_allowed(cwd, cwd, allowed):
        return ("modify_allowed", ["build/test runner, effect judged by cwd"])
    return ("modify_other", ["build/test runner runs outside the allowed paths"])


def _looks_remote(arg: str) -> bool:
    host, sep, _rest = arg.partition(":")
    return bool(sep) and host != "" and "/" not in host


def _rm_recursive_or_force(args: list[str]) -> bool:
    for a in args:
        if a == "--":
            break
        if a.startswith("--"):
            if a in ("--recursive", "--force"):
                return True
        elif a.startswith("-") and any(c in a[1:] for c in "rRf"):
            return True
    return False


def _recursive_flag(args: list[str]) -> bool:
    for a in args:
        if a == "--":
            break
        if a == "--recursive" or (a.startswith("-") and not a.startswith("--") and "R" in a[1:]):
            return True
    return False


def _classify_find(args: list[str], targets: list[str], *, allowed: list[str], cwd: str):
    if "-delete" in args:
        return ("destructive", ["find -delete removes matched files"])
    for flag in ("-exec", "-execdir", "-ok", "-okdir"):
        if flag in args:
            idx = args.index(flag)
            prog = os.path.basename(args[idx + 1]) if idx + 1 < len(args) else ""
            if prog in ("rm", "sh", "bash"):
                return ("destructive", [f"find {flag} runs {prog}"])
            positional = [t for t in args if not t.startswith("-")]
            return _modify_verdict(positional + targets, allowed=allowed, cwd=cwd,
                                   reasons=[f"find {flag} runs {prog or 'a command'}"])
    return ("read", [])


def _classify_git(args: list[str], targets: list[str], *, allowed: list[str], cwd: str):
    if not args:
        return ("unclear", ["git without a subcommand"])
    sub, rest = args[0], args[1:]
    positional = [t for t in rest if not t.startswith("-")]

    def modify(reasons: list[str]):
        return _modify_verdict(positional + targets, allowed=allowed, cwd=cwd, reasons=reasons)

    if sub in _GIT_PLAIN_READ:
        return ("read", [])
    if sub == "branch":
        if any(f in rest for f in ("-d", "-D", "-m", "-M", "--delete", "--move")):
            return ("destructive", ["git branch deletes or moves a branch"])
        return ("read", [])
    if sub == "remote":
        if not rest or rest[0] in ("-v", "--verbose"):
            return ("read", [])
        return modify(["git remote changes configuration"])
    if sub == "config":
        if "--get" in rest:
            return ("read", [])
        return modify(["git config writes configuration"])
    if sub == "push":
        force_flags = ("--force", "-f", "--force-with-lease", "--mirror", "--delete")
        if any(f in rest for f in force_flags) or any(t.startswith(":") for t in positional):
            return ("destructive", ["forced or deleting git push rewrites the remote"])
        return ("network", ["git push talks to a remote"])
    if sub in ("pull", "fetch", "clone", "ls-remote"):
        return ("network", [f"git {sub} talks to a remote"])
    if sub == "submodule":
        if "update" in rest:
            return ("network", ["git submodule update fetches from remotes"])
        return ("read", [])
    if sub == "reset":
        if "--hard" in rest:
            return ("destructive", ["git reset --hard discards uncommitted changes"])
        return modify(["git reset"])
    if sub == "clean":
        return ("destructive", ["git clean deletes untracked files"])
    if sub == "rebase":
        return ("destructive", ["git rebase rewrites commit history"])
    if sub == "commit":
        if "--amend" in rest:
            return ("destructive", ["git commit --amend rewrites the last commit"])
        return modify(["git commit"])
    if sub == "stash":
        if rest and rest[0] in ("drop", "clear"):
            return ("destructive", [f"git stash {rest[0]} discards stashes"])
        return modify(["git stash"])
    if sub == "checkout":
        if "-b" in rest or "-B" in rest:
            return modify(["git checkout -b creates a branch"])
        if "--" in rest or "." in rest:
            return ("destructive", ["git checkout discards working-tree changes"])
        return modify(["git checkout"])
    if sub == "restore":
        if "--staged" in rest:
            return modify(["git restore --staged unstages files"])
        return ("destructive", ["git restore discards working-tree changes"])
    if sub == "update-ref":
        if "-d" in rest:
            return ("destructive", ["git update-ref -d deletes a ref"])
        return modify(["git update-ref"])
    if sub == "reflog":
        if rest and rest[0] == "expire":
            return ("destructive", ["git reflog expire drops history"])
        return ("read", [])
    if sub == "gc":
        if "--prune=now" in rest:
            return ("destructive", ["git gc --prune=now drops unreachable objects"])
        return modify(["git gc"])
    if sub in ("filter-branch", "filter-repo"):
        return ("destructive", [f"git {sub} rewrites history"])
    if sub in _GIT_MODIFY:
        return modify([f"git {sub}"])
    return ("unclear", [f"unknown git subcommand: {sub}"])


def _classify_docker(args: list[str], *, allowed: list[str], cwd: str):
    sub = args[0] if args else ""
    if sub in ("rm", "rmi"):
        return ("destructive", [f"docker {sub} removes containers or images"])
    if sub == "volume" and len(args) > 1 and args[1] == "rm":
        return ("destructive", ["docker volume rm removes volumes"])
    if sub == "system" and len(args) > 1 and args[1] == "prune":
        return ("destructive", ["docker system prune deletes unused data"])
    if sub in ("push", "pull", "login"):
        return ("network", [f"docker {sub} talks to a registry"])
    if sub == "ps":
        return ("read", [])
    if sub == "build":
        positional = [t for t in args[1:] if not t.startswith("-")]
        return _modify_verdict(positional or [cwd], allowed=allowed, cwd=cwd,
                               reasons=["docker build"])
    return ("unclear", [f"unrecognized docker usage: {sub or '?'}"])


def _classify_segment(tokens: list[str], *, allowed: list[str], cwd: str):
    tokens = _strip_prefixes(tokens)
    if tokens is None:
        return ("destructive", ["uses sudo/doas"])
    if not tokens:
        return ("read", ["nothing but variable assignments"])
    cmd = os.path.basename(tokens[0])
    args = tokens[1:]
    targets = _scan_redirects(tokens)

    # Anything that runs arbitrary text as code is never classifiable.
    if cmd in ("eval", "exec", "xargs"):
        return ("unclear", [f"{cmd} executes arbitrary text as a command"])
    if cmd in ("bash", "sh") and "-c" in args:
        return ("unclear", [f"{cmd} -c executes arbitrary text as a command"])
    if cmd == "node" and "-e" in args:
        return ("unclear", ["node -e executes arbitrary code"])
    if cmd in ("python", "python3"):
        if "-c" in args:
            return ("unclear", ["python -c executes arbitrary code"])
        if "-m" in args:
            idx = args.index("-m")
            module = args[idx + 1] if idx + 1 < len(args) else ""
            if module == "pytest":
                return _runner_verdict(cwd, allowed)
            return ("unclear", [f"python -m {module or '?'} runs arbitrary code"])
        return ("unclear", ["python runs arbitrary code"])

    if cmd == "git":
        return _classify_git(args, targets, allowed=allowed, cwd=cwd)
    if cmd == "docker":
        return _classify_docker(args, allowed=allowed, cwd=cwd)

    if cmd in NETWORK_CMDS:
        return ("network", [f"{cmd} talks to the network"])
    if cmd == "rsync":
        positional = [t for t in args if not t.startswith("-")]
        if any(_looks_remote(t) for t in positional):
            return ("network", ["rsync to a remote host"])
        return _modify_verdict(positional + targets, allowed=allowed, cwd=cwd,
                               reasons=["rsync copies files"])
    if cmd in ("pip", "pip3"):
        if args and args[0] == "install":
            return ("network", ["pip install downloads packages"])
        if args and args[0] in ("freeze", "list", "show"):
            return ("read", [])
        return ("unclear", ["unrecognized pip usage"])
    if cmd == "uv":
        if args[:2] == ["pip", "install"] or (args and args[0] == "add"):
            return ("network", ["uv installs packages"])
        return ("unclear", ["unrecognized uv usage"])
    if cmd == "twine":
        if args and args[0] == "upload":
            return ("network", ["twine upload publishes a package"])
        return ("unclear", ["unrecognized twine usage"])
    if cmd in ("npm", "pnpm", "yarn"):
        if args and args[0] in ("install", "add", "publish"):
            return ("network", [f"{cmd} {args[0]} downloads or publishes packages"])
        if args and args[0] in ("test", "run"):
            return _runner_verdict(cwd, allowed)
        return ("unclear", [f"unrecognized {cmd} usage"])

    if cmd in ("pytest", "make", "tsc", "ruff", "mypy", "black"):
        return _runner_verdict(cwd, allowed)
    if cmd == "cargo":
        if args and args[0] in ("build", "test"):
            return _runner_verdict(cwd, allowed)
        if args and args[0] in ("install", "publish"):
            return ("network", [f"cargo {args[0]} downloads or publishes packages"])
        return ("unclear", ["unrecognized cargo usage"])
    if cmd == "go" and args and args[0] in ("build", "test"):
        return _runner_verdict(cwd, allowed)

    if cmd in ("shred", "dd", "truncate", "wipefs") or cmd.startswith("mkfs"):
        return ("destructive", [f"{cmd} overwrites data in place"])
    if cmd in ("pkill", "killall", "kill"):
        return ("destructive", [f"{cmd} terminates processes"])
    if cmd == "kubectl" and args and args[0] == "delete":
        return ("destructive", ["kubectl delete removes cluster resources"])
    if cmd == "terraform" and args and args[0] == "destroy":
        return ("destructive", ["terraform destroy tears down infrastructure"])
    if cmd == "systemctl":
        verb = args[0] if args else ""
        if verb in ("stop", "disable", "mask"):
            return ("destructive", [f"systemctl {verb} stops or disables a service"])
        if verb in ("status", "is-active", "is-enabled", "show", "list-units"):
            return ("read", [])
        return ("modify_other", [f"systemctl {verb or '?'} changes service state"])
    if cmd in ("chmod", "chown"):
        positional = [t for t in args if not t.startswith("-")]
        if _recursive_flag(args):
            return ("destructive", [f"{cmd} -R changes a whole tree at once"])
        return _modify_verdict(positional + targets, allowed=allowed, cwd=cwd, reasons=[cmd])
    if cmd == "rm":
        if _rm_recursive_or_force(args):
            return ("destructive", ["rm with -r/-f removes files irreversibly"])
        positional = [t for t in args if not t.startswith("-")]
        return _modify_verdict(positional + targets, allowed=allowed, cwd=cwd,
                               reasons=["rm deletes files"])
    if cmd == "find":
        return _classify_find(args, targets, allowed=allowed, cwd=cwd)
    if cmd == "sed":
        if any(a == "-i" or a.startswith("-i.") or a.startswith("--in-place") for a in args):
            positional = [t for t in args if not t.startswith("-")]
            return _modify_verdict(positional + targets, allowed=allowed, cwd=cwd,
                                   reasons=["sed -i edits files in place"])
        return ("read", [])
    if cmd == "awk":
        if any("system(" in a for a in args):
            return ("unclear", ["awk system() runs shell commands"])
        return ("read", [])
    if cmd == "hermes":
        if any(a in ("list", "show", "status", "stats") for a in args):
            return ("read", [])
        return ("unclear", ["unrecognized hermes usage"])
    if cmd in READ_CMDS:
        if targets:
            return _modify_verdict(targets, allowed=allowed, cwd=cwd,
                                   reasons=[f"redirects output to {', '.join(targets)}"])
        return ("read", [])
    if cmd in MODIFY_CMDS:
        positional = [t for t in args if not t.startswith("-")]
        return _modify_verdict(positional + targets, allowed=allowed, cwd=cwd, reasons=[cmd])
    return ("unclear", [f"unrecognized command: {cmd}"])


def classify_command(command: str, *, allowed_paths: list[str] | tuple = (),
                     cwd: str | None = None) -> tuple[str, list[str]]:
    """Classify the most severe effect of any segment of a compound command."""
    if cwd is None:
        cwd = os.getcwd()
    cwd = os.path.realpath(cwd)
    allowed = [os.path.realpath(str(p)) for p in allowed_paths] or [cwd]
    if "$(" in command or "`" in command:
        return ("unclear", ["command substitution: $(...) or backticks"])
    segments = _split_segments(command)
    if segments is None:
        return ("unclear", ["unparseable command (unbalanced quotes?)"])
    if not segments:
        return ("read", [])
    worst_cat, worst_reasons = "read", []
    for seg in segments:
        cat, reasons = _classify_segment(list(seg), allowed=allowed, cwd=cwd)
        if SEVERITY[cat] > SEVERITY[worst_cat]:
            worst_cat, worst_reasons = cat, reasons
    return (worst_cat, worst_reasons)


def directive_for(category: str, reasons: list[str], gate_cfg: dict) -> dict | None:
    """Map a category to a friction directive, or None when no friction applies.

    There is deliberately no "allow" directive: this hook can only block or
    escalate, never weaken Hermes' built-in approvals.
    """
    policy = dict(DEFAULT_POLICY)
    for key in ("network", "destructive", "unclear", "modify_other"):
        if key in (gate_cfg or {}):
            policy[key] = gate_cfg[key]
    action = policy.get(category)
    if action in (None, "allow"):
        return None
    joined = "; ".join(reasons) if reasons else category
    if action == "block":
        return {"action": "block", "message": f"LoopGuard blocked a {category} command: {joined}"}
    if action == "approve":
        return {"action": "approve",
                "message": f"LoopGuard: {category} command needs approval: {joined}"}
    return None


def evaluate_tool_call(tool_name: str, args: dict, gate_cfg: dict, *,
                       cwd: str | None = None) -> ToolVerdict:
    tools = (gate_cfg or {}).get("tools", ["terminal"])
    if tool_name in tools and isinstance(args, dict) and isinstance(args.get("command"), str):
        category, reasons = classify_command(args["command"],
                                             allowed_paths=(gate_cfg or {}).get("allowed_paths", ()),
                                             cwd=cwd)
        return ToolVerdict(category, reasons, directive_for(category, reasons, gate_cfg or {}))
    return ToolVerdict("read", ["not a shell tool"], None)


def make_pre_tool_call_hook(get_cfg, *, in_automation, store_factory=None, semantic=None):
    """Build a pre_tool_call hook. Never raises; never returns an allow directive."""

    def hook(tool_name=None, args=None, task_id=None, **kwargs):
        try:
            cfg = (get_cfg() or {}).get("tool_gate") or {}
            if not cfg.get("enabled"):
                return None
            if cfg.get("scope", "automations") == "automations" and not in_automation():
                return None
            verdict = evaluate_tool_call(tool_name, args, cfg)
            command = args.get("command") if isinstance(args, dict) else None
            if (verdict.category == "unclear" and cfg.get("semantic") and semantic
                    and isinstance(command, str)):
                try:
                    cat, prob = semantic(command)
                    if prob >= 0.8 and cat in CATEGORIES and cat != "unclear":
                        reasons = [f"semantic gate reclassified as {cat} (p={prob:.2f})"]
                        verdict = ToolVerdict(cat, reasons, directive_for(cat, reasons, cfg))
                except Exception:
                    log.debug("tool gate semantic reclassification failed", exc_info=True)
            directive = verdict.directive
            if directive is not None and store_factory is not None:
                try:
                    store_factory().log_decision(watcher="tool_gate", kind="tool",
                                                 outcome=directive.get("action"),
                                                 route=verdict.category,
                                                 note=(command or "")[:300])
                except Exception:
                    log.debug("tool gate decision log failed", exc_info=True)
            return directive
        except Exception:
            log.debug("tool gate hook failed", exc_info=True)
            return None

    return hook
