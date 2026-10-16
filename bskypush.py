#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///
"""bskypush - git push, screenshot the CLI, post it to Bluesky.

One motion for shipping a CLI tool: push the code, and only if the push
succeeds, run the tool in a PTY, render its output as a terminal screenshot,
post it to Bluesky, and rotate the boost (un-repost the previous post for this
project, repost the new one).
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import pty
import re
import shutil
import struct
import subprocess
import sys
import termios
import tomllib
from pathlib import Path

import requests

CONFIG_NAME = ".bskypush.toml"
CACHE_DIR = ".bskypush"
MAX_IMAGE_BYTES = 2_000_000   # app.bsky.embed.images maxSize, raised from 1MB 2026-04-23
MAX_TEXT_CHARS = 300          # graphemes upstream; we count codepoints, see README

DEFAULT_TEXT = "{repo} {tag} - {subject}"
DEFAULT_COLUMNS = 90
DEFAULT_ROWS = 60
DEFAULT_PDS = "https://bsky.social"
DEFAULT_TERMSHOT_ARGS = ["--padding", "20", "--no-shadow"]

# Keys that must never appear in the repo-tracked config file.
CRED_KEYS = {
    "handle", "password", "app_password", "identifier",
    "bsky_handle", "bsky_app_password", "bsky_password",
}

CONFIG_TEMPLATE = '''\
# bskypush configuration. Committed to the repo - credentials never go here.

# The CLI invocation to screenshot. This is what "the latest build" means for
# this repo: whatever command shows the tool off in one screen of output.
command = "{command}"

# Post text template. Placeholders come from git:
#   {{repo}}  {{branch}}  {{sha}}  {{subject}}  {{tag}}  {{date}}
# URLs, #hashtags and @mentions in the result are detected and posted as
# clickable rich-text facets, so a repo link here works as a link.
text = "{text}"

# History key for the boost rotation, so bskypush knows which post is "the
# previous one for this project". Defaults to the repo name.
# project = "myproject"

# Terminal width used when capturing the command's output.
columns = {columns}

# Personal Data Server. Change only if you self-host.
# pds = "https://bsky.social"

# Alt text for the screenshot. Defaults to "Terminal output of: <command>".
# alt = "..."

[termshot]
args = {termshot_args}
'''

GITIGNORE_LINES = [CACHE_DIR + "/", ".env", "__pycache__/"]


class Die(Exception):
    """A user-facing error: printed as a message, not a traceback."""


def warn(msg: str) -> None:
    print(f"bskypush: {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# paths and state
# --------------------------------------------------------------------------- #

def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    d = root / "bskypush"
    d.mkdir(parents=True, exist_ok=True)
    return d


def history_path() -> Path:
    return state_dir() / "history.jsonl"


def append_history(rec: dict) -> None:
    with open(history_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_history() -> list[dict]:
    p = history_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def last_boosted(project: str) -> dict | None:
    """Newest prior post for this project that still carries a repost."""
    for rec in reversed(read_history()):
        if rec.get("project") == project and rec.get("repost_uri"):
            return rec
    return None


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #

def git_opt(*args: str, cwd: Path | None = None) -> str:
    """Run git, returning stdout on success and "" on any failure."""
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def repo_root() -> Path:
    out = git_opt("rev-parse", "--show-toplevel")
    if not out:
        raise Die("not inside a git repository (try `bskypush init`)")
    return Path(out)


def git_meta(root: Path, remote: str) -> dict:
    url = git_opt("remote", "get-url", remote, cwd=root)
    name = ""
    if url:
        name = url.rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
    return {
        "repo": name or root.name,
        "branch": git_opt("rev-parse", "--abbrev-ref", "HEAD", cwd=root),
        "sha": git_opt("rev-parse", "--short", "HEAD", cwd=root),
        "subject": git_opt("log", "-1", "--pretty=%s", cwd=root),
        "tag": git_opt("describe", "--tags", "--abbrev=0", cwd=root),
        "date": git_opt("log", "-1", "--pretty=%cs", cwd=root),
    }


# --------------------------------------------------------------------------- #
# config and credentials
# --------------------------------------------------------------------------- #

def load_config(root: Path) -> dict:
    path = root / CONFIG_NAME
    if not path.exists():
        raise Die(f"no {CONFIG_NAME} in {root} - run `bskypush init`")
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    for key in raw:
        if key.lower() in CRED_KEYS:
            raise Die(
                f"{CONFIG_NAME} contains the credential key '{key}'.\n"
                "  Credentials belong in ~/.config/bskypush/creds.env or the environment,\n"
                "  never in a file that gets committed."
            )

    cfg = {
        "command": raw.get("command"),
        "text": raw.get("text", DEFAULT_TEXT),
        "project": raw.get("project"),
        "columns": int(raw.get("columns", DEFAULT_COLUMNS)),
        "pds": str(raw.get("pds", DEFAULT_PDS)).rstrip("/"),
        "alt": raw.get("alt"),
        "termshot_args": [str(a) for a in raw.get("termshot", {}).get("args", DEFAULT_TERMSHOT_ARGS)],
    }
    if not cfg["command"]:
        raise Die(f"{CONFIG_NAME} must set `command` - the CLI invocation to screenshot")
    return cfg


def parse_env_file(path: Path) -> dict:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.startswith("export "):
            k = k[len("export "):]
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def creds_file() -> Path:
    return Path.home() / ".config" / "bskypush" / "creds.env"


def load_creds(root: Path | None = None) -> tuple[str, str]:
    """Process env, then ./.env, then ~/.config/bskypush/creds.env."""
    sources: list[dict] = [dict(os.environ)]
    if root is not None:
        sources.append(parse_env_file(root / ".env"))
    sources.append(parse_env_file(creds_file()))

    handle = password = ""
    for src in sources:
        handle = handle or src.get("BSKY_HANDLE", "")
        password = password or src.get("BSKY_APP_PASSWORD", "")

    if not handle or not password:
        missing = " and ".join(
            n for n, v in (("BSKY_HANDLE", handle), ("BSKY_APP_PASSWORD", password)) if not v
        )
        raise Die(
            f"missing {missing}.\n"
            "  In Bluesky: Settings -> Privacy and Security -> App Passwords -> create one,\n"
            f"  then write it to {creds_file()} (chmod 600):\n"
            "      BSKY_HANDLE=you.bsky.social\n"
            "      BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx"
        )
    return handle, password


# --------------------------------------------------------------------------- #
# capture and render
# --------------------------------------------------------------------------- #

def capture(command: str, columns: int, rows: int = DEFAULT_ROWS) -> tuple[bytes, int]:
    """Run command under a PTY with colors forced on, returning raw ANSI bytes.

    We capture rather than letting termshot execute the command: a CLI running
    in a renderer's child PTY can detect a reduced environment and drop color.
    """
    env = dict(os.environ)
    env.update({
        "FORCE_COLOR": "1",
        "CLICOLOR_FORCE": "1",
        "CLICOLOR": "1",
        "TERM": "xterm-256color",
        "COLUMNS": str(columns),
        "LINES": str(rows),
    })
    env.pop("NO_COLOR", None)

    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    except OSError:
        pass  # window size is a nicety, not a requirement

    proc = subprocess.Popen(
        command, shell=True, stdin=slave, stdout=slave, stderr=slave,
        env=env, close_fds=True,
    )
    os.close(slave)

    chunks: list[bytes] = []
    try:
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:
                break       # EIO: the child closed the far end
            if not data:
                break
            chunks.append(data)
    finally:
        os.close(master)

    return b"".join(chunks), proc.wait()


def render(ansi: bytes, command: str, out_png: Path, termshot_args: list[str],
           cache: Path, stem: str) -> Path:
    if not shutil.which("termshot"):
        raise Die(
            "termshot not found on PATH.\n"
            "  install:  yay -S termshot\n"
            "       or:  go install github.com/homeport/termshot/cmd/termshot@latest\n"
            "            (then make sure ~/go/bin is on your PATH)"
        )

    cache.mkdir(parents=True, exist_ok=True)
    ansi_path = cache / f"{stem}.ansi"
    # termshot can't know the command under --raw-read, so show it ourselves.
    ansi_path.write_bytes(f"$ {command}\r\n".encode() + ansi)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["termshot", "--raw-read", str(ansi_path), "--filename", str(out_png), *termshot_args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not out_png.exists():
        detail = (p.stderr or p.stdout).strip() or f"exit {p.returncode}"
        raise Die(f"termshot failed: {detail}")
    return ansi_path


def png_dimensions(path: Path) -> tuple[int, int] | None:
    """Width/height from the IHDR chunk, or None if it isn't where we expect.

    aspectRatio is optional in the lexicon, and wrong dimensions are worse than
    absent ones - so a surprising layout means we omit the field.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return (w, h) if w and h else None



# --------------------------------------------------------------------------- #
# rich text facets
# --------------------------------------------------------------------------- #
#
# Bluesky does not auto-link anything. A client renders a URL, #tag or @mention
# as clickable only if the record carries a matching facet, and facet offsets
# are *UTF-8 byte* offsets, not character offsets - so an em-dash or emoji
# earlier in the text shifts every span after it. We therefore run the regexes
# over the encoded bytes and never convert from character indices.

# Each pattern requires a leading delimiter so we do not match mid-token (the
# "#x" in a URL fragment, an email's "@"). A space is prepended to the text
# before matching so a span at offset 0 still has one, then subtracted back.
_URL_RE = re.compile(
    rb"[\s(]((?:https?://)(?:[a-zA-Z0-9][a-zA-Z0-9\-]*\.)+[a-zA-Z]{2,}(?::\d+)?(?:/[^\s]*)?)"
)
_TAG_RE = re.compile(rb"[\s(](#[^\s#]+)")
_MENTION_RE = re.compile(
    rb"[\s(](@(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,})"
)

_TRAILING = b".,;:!?'\")]}>"
MAX_TAG_CHARS = 64


def _trim_trailing(token: bytes) -> bytes:
    """Drop sentence punctuation that a regex greedily swallowed.

    A closing paren is kept when the token has an unmatched opening one, so
    wiki-style URLs ending in ")" survive.
    """
    while len(token) > 1 and token[-1] in _TRAILING:
        if token.endswith(b")") and token.count(b"(") >= token.count(b")"):
            break
        token = token[:-1]
    return token


def _spans(pattern: re.Pattern[bytes], text_bytes: bytes) -> list[tuple[int, int, bytes]]:
    out = []
    for m in pattern.finditer(b" " + text_bytes):
        token = _trim_trailing(m.group(1))
        start = m.start(1) - 1          # undo the prepended space
        out.append((start, start + len(token), token))
    return out


def detect_facets(text: str, resolve_handle=None) -> list[dict]:
    """Facets for the URLs, #tags and @mentions in text.

    Mentions need a handle -> DID lookup, so they are only emitted when a
    resolver is supplied; links and tags are entirely offline. Earlier facets
    win any overlap, so a "#fragment" inside a URL cannot become a tag.
    """
    text_bytes = text.encode("utf-8")
    facets: list[dict] = []
    claimed: list[tuple[int, int]] = []

    def claim(start: int, end: int) -> bool:
        if any(start < c_end and end > c_start for c_start, c_end in claimed):
            return False
        claimed.append((start, end))
        return True

    for start, end, token in _spans(_URL_RE, text_bytes):
        if claim(start, end):
            facets.append({
                "index": {"byteStart": start, "byteEnd": end},
                "features": [{
                    "$type": "app.bsky.richtext.facet#link",
                    "uri": token.decode("utf-8"),
                }],
            })

    for start, end, token in _spans(_TAG_RE, text_bytes):
        tag = token.decode("utf-8").lstrip("#")
        if not tag or len(tag) > MAX_TAG_CHARS or not claim(start, end):
            continue
        facets.append({
            "index": {"byteStart": start, "byteEnd": end},
            "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}],
        })

    if resolve_handle is not None:
        for start, end, token in _spans(_MENTION_RE, text_bytes):
            handle = token.decode("utf-8").lstrip("@")
            did = resolve_handle(handle)
            if not did or not claim(start, end):
                continue
            facets.append({
                "index": {"byteStart": start, "byteEnd": end},
                "features": [{"$type": "app.bsky.richtext.facet#mention", "did": did}],
            })

    facets.sort(key=lambda f: f["index"]["byteStart"])
    return facets


def describe_facets(facets: list[dict]) -> str:
    kinds: dict[str, int] = {}
    for f in facets:
        kind = f["features"][0]["$type"].rsplit("#", 1)[-1]
        kinds[kind] = kinds.get(kind, 0) + 1
    if not kinds:
        return "none"
    return ", ".join(f"{n} {k}" + ("s" if n > 1 else "") for k, n in sorted(kinds.items()))

# --------------------------------------------------------------------------- #
# Bluesky
# --------------------------------------------------------------------------- #

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def rkey_of(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


class Bsky:
    """The four XRPC calls this tool needs. No SDK, no token refresh.

    A fresh createSession per run is less code than caching and refreshing the
    JWT, and nowhere near the session rate limit at a few pushes a day.
    """

    def __init__(self, pds: str, handle: str, password: str):
        self.pds = pds
        self.handle = handle
        self._password = password
        self.did = ""
        self.jwt = ""
        self.http = requests.Session()

    def _url(self, nsid: str) -> str:
        return f"{self.pds}/xrpc/{nsid}"

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.jwt}"}

    @staticmethod
    def _check(r: requests.Response, what: str) -> None:
        if r.status_code < 400:
            return
        try:
            body = r.json()
            msg = body.get("message") or body.get("error") or r.text
        except ValueError:
            msg = r.text
        raise Die(f"{what} -> HTTP {r.status_code}: {str(msg)[:300]}")

    def login(self) -> dict:
        r = self.http.post(
            self._url("com.atproto.server.createSession"),
            json={"identifier": self.handle, "password": self._password}, timeout=30,
        )
        self._check(r, "createSession")
        d = r.json()
        self.jwt = d["accessJwt"]
        self.did = d["did"]
        self.handle = d.get("handle") or self.handle
        return d

    def resolve_handle(self, handle: str) -> str | None:
        """Handle -> DID for a mention facet. None if it does not resolve."""
        try:
            r = self.http.get(
                self._url("com.atproto.identity.resolveHandle"),
                params={"handle": handle}, headers=self._auth, timeout=15,
            )
        except requests.RequestException:
            return None
        if r.status_code >= 400:
            return None
        return r.json().get("did")

    def upload_blob(self, path: Path) -> dict:
        r = self.http.post(
            self._url("com.atproto.repo.uploadBlob"),
            data=path.read_bytes(),
            headers={**self._auth, "Content-Type": "image/png"},
            timeout=180,
        )
        self._check(r, "uploadBlob")
        return r.json()["blob"]   # response wraps the blob object

    def create_record(self, collection: str, record: dict) -> dict:
        r = self.http.post(
            self._url("com.atproto.repo.createRecord"),
            json={"repo": self.did, "collection": collection, "record": record},
            headers=self._auth, timeout=30,
        )
        self._check(r, f"createRecord({collection})")
        return r.json()

    # Phrasings a PDS uses for "that record isn't there". Deleting an already
    # deleted repost is the expected outcome of a half-finished rotation, so it
    # has to read as success - and the wording has varied across versions.
    _GONE = ("could not locate", "not found", "notfound", "recordnotfound")

    def delete_record(self, collection: str, rkey: str) -> bool:
        """True if deleted, False if it was already gone."""
        r = self.http.post(
            self._url("com.atproto.repo.deleteRecord"),
            json={"repo": self.did, "collection": collection, "rkey": rkey},
            headers=self._auth, timeout=30,
        )
        if r.status_code in (400, 404):
            body = r.text.lower().replace(" ", "")
            if any(phrase.replace(" ", "") in body for phrase in self._GONE):
                return False
        self._check(r, f"deleteRecord({collection})")
        return True

    def post_url(self, post_uri: str) -> str:
        return f"https://bsky.app/profile/{self.handle}/post/{rkey_of(post_uri)}"


# --------------------------------------------------------------------------- #
# shared pieces of the commands
# --------------------------------------------------------------------------- #

def build_text(template: str, meta: dict) -> str:
    try:
        text = template.format(**meta)
    except KeyError as e:
        raise Die(
            f"unknown placeholder {{{e.args[0]}}} in `text`. "
            f"Available: {', '.join(sorted(meta))}"
        ) from None
    # An empty {tag} on an untagged repo would otherwise leave double spaces.
    return " ".join(text.split())


def check_text(text: str) -> None:
    if not text:
        raise Die("post text is empty")
    if len(text) > MAX_TEXT_CHARS:
        raise Die(
            f"post text is {len(text)} characters, limit is {MAX_TEXT_CHARS}:\n  {text}"
        )


def check_image(png: Path) -> int:
    size = png.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise Die(
            f"screenshot is {size:,} bytes, limit is {MAX_IMAGE_BYTES:,} "
            f"({size / MAX_IMAGE_BYTES:.1f}x over).\n"
            f"  {png}\n"
            "  Shorten the captured output or narrow `columns`, then retry."
        )
    return size


def capture_and_render(root: Path, cfg: dict, stem: str, out: Path | None = None) -> tuple[Path, int]:
    cache = root / CACHE_DIR
    png = out if out is not None else cache / f"{stem}.png"

    ansi, rc = capture(cfg["command"], cfg["columns"])
    if rc != 0:
        warn(f"`{cfg['command']}` exited {rc} - screenshotting its output anyway")
    if not ansi.strip():
        raise Die(f"`{cfg['command']}` produced no output - nothing to screenshot")

    render(ansi, cfg["command"], png, cfg["termshot_args"], cache, stem)
    return png, check_image(png)


def display_path(path: Path, root: Path) -> str:
    """Paths inside the repo print relative - absolute ones swamp the output."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        raise Die("refusing to post without confirmation on a non-interactive stdin (pass --yes)")
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_init(args: argparse.Namespace) -> int:
    root = Path(git_opt("rev-parse", "--show-toplevel") or ".").resolve()
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        print(f"  initialized git repository in {root}")
        root = Path(git_opt("rev-parse", "--show-toplevel", cwd=root) or root)

    config = root / CONFIG_NAME
    if config.exists():
        print(f"  {CONFIG_NAME} already exists, leaving it alone")
    else:
        config.write_text(CONFIG_TEMPLATE.format(
            command=args.command or "echo 'set command in .bskypush.toml'",
            text=DEFAULT_TEXT,
            columns=DEFAULT_COLUMNS,
            termshot_args=json.dumps(DEFAULT_TERMSHOT_ARGS),
        ), encoding="utf-8")
        print(f"  wrote {config.relative_to(root)}")

    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    missing = [l for l in GITIGNORE_LINES if l not in existing]
    if missing:
        with open(gitignore, "a", encoding="utf-8") as f:
            if existing and existing[-1].strip():
                f.write("\n")
            f.write("# bskypush\n" + "\n".join(missing) + "\n")
        print(f"  added to .gitignore: {', '.join(missing)}")

    print("\nNext: set `command` in .bskypush.toml, then `bskypush doctor`.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"  [{'ok' if good else 'XX'}] {label}" + (f" - {detail}" if detail else ""))

    print("bskypush doctor\n")

    root = None
    try:
        root = repo_root()
        check("git repository", True, str(root))
    except Die as e:
        check("git repository", False, str(e).splitlines()[0])

    termshot = shutil.which("termshot")
    check("termshot", bool(termshot), termshot or "not on PATH")
    if not termshot:
        print("       install:  yay -S termshot")
        print("            or:  go install github.com/homeport/termshot/cmd/termshot@latest")
        print("                 (then add ~/go/bin to your PATH)")

    cfg = None
    if root:
        try:
            cfg = load_config(root)
            check("config", True, f"command = {cfg['command']!r}")
        except Die as e:
            check("config", False, str(e).splitlines()[0])

    handle = None
    try:
        handle, _ = load_creds(root)
        check("credentials", True, f"BSKY_HANDLE = {handle}")
    except Die as e:
        check("credentials", False, str(e).splitlines()[0])
        print(f"       expected in: {creds_file()}")

    if args.live:
        if not (handle and cfg):
            check("createSession", False, "skipped - need credentials and config first")
        else:
            try:
                h, p = load_creds(root)
                bsky = Bsky(cfg["pds"], h, p)
                session = bsky.login()
                check("createSession", True, f"{session.get('handle')} / {session['did']}")
            except Die as e:
                check("createSession", False, str(e).splitlines()[0])

    hist = read_history()
    print(f"\n  history: {len(hist)} post(s) in {history_path()}")
    print("\n" + ("all good" if ok else "some checks failed"))
    return 0 if ok else 1


def cmd_shot(args: argparse.Namespace) -> int:
    root = repo_root()
    cfg = load_config(root)
    stem = git_opt("rev-parse", "--short", "HEAD", cwd=root) or "worktree"
    out = Path(args.output).resolve() if args.output else None

    png, size = capture_and_render(root, cfg, stem, out)
    dims = png_dimensions(png)
    print(display_path(png, root))
    print(f"  {size:,} bytes" + (f", {dims[0]}x{dims[1]}" if dims else ", dimensions unknown"))
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    root = repo_root()
    cfg = load_config(root)

    remote = args.remote or "origin"
    meta = git_meta(root, remote)
    branch = args.branch or meta["branch"]
    if not branch or branch == "HEAD":
        raise Die("could not determine the current branch - pass --branch")

    project = cfg["project"] or meta["repo"]
    text = args.text or build_text(cfg["text"], meta)
    check_text(text)

    # Credentials are resolved before the push, so a missing app password fails
    # here rather than after the commits are already public.
    if not args.dry_run:
        handle, password = load_creds(root)

    if not args.dry_run and not args.no_push:
        print(f"-> git push {remote} {branch}")
        # --no-progress drops the per-object progress spam; the ref update
        # summary and every error still come through, and we print our own
        # progress line above it.
        push = ["git", "push", "--no-progress", remote, branch]
        if subprocess.run(push, cwd=root).returncode != 0:
            raise Die("git push failed - nothing captured, nothing posted")
        meta = git_meta(root, remote)   # refresh in case the push moved things

    stem = meta["sha"] or "worktree"
    print(f"-> capturing `{cfg['command']}`")
    png, size = capture_and_render(root, cfg, stem)
    dims = png_dimensions(png)
    alt = cfg["alt"] or f"Terminal output of: {cfg['command']}"

    previous = None if args.no_repost else last_boosted(project)
    # Links and tags resolve offline; mentions need a session, so the preview
    # reports only what can be known before logging in.
    preview_facets = detect_facets(text)

    print(f"\n  text:    {text}")
    print(f"  image:   {display_path(png, root)} ({size:,} bytes"
          + (f", {dims[0]}x{dims[1]})" if dims else ", dimensions unknown)"))
    print(f"  alt:     {alt}")
    print(f"  links:   {describe_facets(preview_facets)}")
    print(f"  project: {project}")
    if args.no_repost:
        print("  boost:   skipped (--no-repost)")
    elif previous:
        print(f"  boost:   repost this, un-repost {rkey_of(previous['post_uri'])}")
    else:
        print("  boost:   repost this (no previous post for this project)")

    if args.dry_run:
        print("\ndry run - nothing pushed, nothing posted")
        return 0

    if not args.yes and not confirm("\nPost to Bluesky?"):
        print("aborted")
        return 1

    bsky = Bsky(cfg["pds"], handle, password)
    bsky.login()

    blob = bsky.upload_blob(png)
    image: dict = {"alt": alt, "image": blob}
    if dims:
        image["aspectRatio"] = {"width": dims[0], "height": dims[1]}

    # Rebuilt with a resolver now that we have a session, so @mentions count.
    facets = detect_facets(text, resolve_handle=bsky.resolve_handle)
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": now_iso(),
        "embed": {"$type": "app.bsky.embed.images", "images": [image]},
    }
    if facets:
        record["facets"] = facets

    created = bsky.create_record("app.bsky.feed.post", record)
    url = bsky.post_url(created["uri"])
    print(f"\nposted: {url}")

    # Boost rotation is best-effort: the post already landed, which is the part
    # that matters. New boost goes up before the old one comes down, so a crash
    # mid-sequence leaves two boosted posts rather than none.
    repost_uri = None
    if not args.no_repost:
        try:
            repost_uri = bsky.create_record("app.bsky.feed.repost", {
                "$type": "app.bsky.feed.repost",
                "subject": {"uri": created["uri"], "cid": created["cid"]},
                "createdAt": now_iso(),
            })["uri"]
            print("  reposted")
        except Die as e:
            warn(f"repost failed (post is still up): {e}")

    append_history({
        "project": project,
        "post_uri": created["uri"],
        "post_cid": created["cid"],
        "repost_uri": repost_uri,
        "sha": meta["sha"],
        "at": now_iso(),
        "text": text,
        "facets": len(facets),
    })

    if previous and not args.no_repost:
        try:
            deleted = bsky.delete_record("app.bsky.feed.repost", rkey_of(previous["repost_uri"]))
            print(f"  un-reposted {rkey_of(previous['post_uri'])}"
                  + ("" if deleted else " (already gone)"))
        except Die as e:
            warn(f"un-repost of the previous post failed: {e}")

    return 0


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bskypush",
        description="git push, screenshot the CLI, post it to Bluesky.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="create .bskypush.toml and update .gitignore")
    pi.add_argument("--command", help="the CLI invocation to screenshot")
    pi.set_defaults(func=cmd_init)

    pd = sub.add_parser("doctor", help="check termshot, config, credentials, git")
    pd.add_argument("--live", action="store_true", help="also log in to Bluesky")
    pd.set_defaults(func=cmd_doctor)

    ps = sub.add_parser("shot", help="capture and render only, no network")
    ps.add_argument("-o", "--output", help="write the PNG here")
    ps.set_defaults(func=cmd_shot)

    pp = sub.add_parser("push", help="push, screenshot, post, rotate the boost")
    pp.add_argument("--text", help="post text, overriding the template")
    pp.add_argument("--dry-run", action="store_true", help="render and print, touch no network")
    pp.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    pp.add_argument("--no-push", action="store_true", help="skip git push, still post")
    pp.add_argument("--no-repost", action="store_true", help="skip the boost rotation")
    pp.add_argument("--remote", help="git remote (default: origin)")
    pp.add_argument("--branch", help="git branch (default: current)")
    pp.set_defaults(func=cmd_push)

    return p


def main(argv: list[str] | None = None) -> int:
    # git and termshot write straight to the terminal; without line buffering our
    # own progress lines land out of order whenever stdout is a pipe.
    sys.stdout.reconfigure(line_buffering=True)
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Die as e:
        print(f"bskypush: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
