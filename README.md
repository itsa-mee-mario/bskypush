# bskypush

Ship a CLI tool in one motion: `git push`, screenshot the tool, post it to Bluesky.

## Why a wrapper command instead of a git hook

Git has no client-side `post-push` hook, and a `pre-push` hook runs *before* the
push — so it can happily announce a push that then fails. `bskypush push` runs the
push itself and only continues if it exited 0. A failed push posts nothing.

## Install

Needs [`termshot`](https://github.com/homeport/termshot) on `PATH`:

```sh
yay -S termshot                                      # Arch / Manjaro (AUR)
go install github.com/homeport/termshot/cmd/termshot@latest       # anywhere with Go
```

Then either run the script directly — it declares its own dependencies, so
[`uv`](https://docs.astral.sh/uv/) handles the rest:

```sh
uv run bskypush.py doctor
```

…or install it as a command:

```sh
uv tool install --editable .    # --editable so source edits take effect at once
bskypush doctor
```

`--editable` keeps the command pointed at this checkout, so moving or deleting
the directory breaks it; drop the flag for an independent copy.

## Credentials

Create an **app password** in Bluesky: Settings → Privacy and Security → App
Passwords. Never your account password — app passwords are individually
revocable and can't change your email or delete your account.

```sh
mkdir -p ~/.config/bskypush
cat > ~/.config/bskypush/creds.env <<'CREDS'
BSKY_HANDLE=you.bsky.social
BSKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
CREDS
chmod 600 ~/.config/bskypush/creds.env
```

Read order: process environment → `./.env` (gitignored) →
`~/.config/bskypush/creds.env`. Credentials are never read from `.bskypush.toml`,
which is a committed file — putting them there is a hard error.

Bluesky's API is free: no developer portal, no OAuth app registration, no
billing. That is the reason this posts to Bluesky and not X — X retired its free
tier in September 2026 and now bills per post from prepaid credits, $0.015 each
and $0.20 if the post contains a link.

## Setup

```sh
cd ~/code/mytool
bskypush init --command "python -m mytool --demo"
bskypush doctor --live
```

`init` writes `.bskypush.toml`, adds `.bskypush/` and `.env` to `.gitignore`, and
runs `git init` if needed. It never overwrites an existing config.

## Configuration — `.bskypush.toml`

```toml
command = "python -m mytool --demo"   # what "the latest build" looks like
text    = "{repo} {tag} - {subject}"  # post template
columns = 90                          # terminal width for the capture
# project = "mytool"                  # history key, defaults to the repo name
# pds     = "https://bsky.social"     # change only if you self-host
# alt     = "..."                     # defaults to "Terminal output of: <command>"

[termshot]
args = ["--padding", "20", "--no-shadow"]
```

Template placeholders, all from git: `{repo}` `{branch}` `{sha}` `{subject}`
`{tag}` `{date}`. `{tag}` is empty on an untagged repo rather than an error.

URLs, `#hashtags` and `@mentions` in the final text are posted as clickable
rich-text facets. Bluesky doesn't auto-link anything — a client only renders a
link if the record carries a facet with matching byte offsets, which bskypush
computes.

## Commands

| Command | What it does |
|---|---|
| `bskypush init` | Write `.bskypush.toml`, update `.gitignore`, `git init` if needed |
| `bskypush doctor [--live]` | Check termshot, config, credentials, git; `--live` logs in |
| `bskypush shot [-o PATH]` | Capture and render only. No network. |
| `bskypush push` | Push, screenshot, post, rotate the boost |

`push` flags: `--text` (override the template), `--dry-run` (render and print,
zero network calls), `--yes` (skip the confirmation), `--no-push` (post without
pushing), `--no-repost` (skip the boost rotation), `--remote`, `--branch`.

## How the screenshot is taken

bskypush runs your command under its own PTY with `FORCE_COLOR`, `CLICOLOR_FORCE`
and `TERM=xterm-256color` set, captures the raw ANSI, and hands that to
`termshot --raw-read`. It does *not* let termshot execute the command: a CLI
running inside a renderer's child PTY can detect a reduced environment and drop
its colors, which is exactly what you don't want in a screenshot.

Artifacts land in `.bskypush/` (gitignored) next to the repo, so a stray PNG can
never end up in a commit.

**Glyph gotcha.** termshot embeds its own font and has no flag to change it. The
`go install` build renders box-drawing and symbol characters (`✔`, `✖`, `▸`) as
tofu boxes. If your CLI leans on those, use the AUR package — `yay -S termshot`
ships a font patch — or keep the demo output to ASCII.

## Limits, checked before anything is uploaded

- **Text**: 300 characters. Bluesky counts graphemes; bskypush counts codepoints,
  which over-counts emoji slightly and so only ever rejects a little early.
- **Image**: 2,000,000 bytes. Oversized screenshots fail with the actual byte
  count rather than being rejected mid-upload.

## Post history

`~/.local/state/bskypush/history.jsonl`, one line per post — project, post URI
and CID, repost URI, commit SHA, text. Nothing is written into your repo.

The repost record is a *separate* record from the post with its own rkey, which
is why the repost URI is stored: un-reposting later needs that rkey, not the
post's. Un-reposting never deletes the old post — it only stops boosting it.

Boost ordering is deliberate: post → repost the new one → un-repost the old one.
If something fails midway you end up with two boosted posts (harmless, fixable)
rather than none. Both repost calls are best-effort: if they fail, the post is
already up and bskypush warns instead of erroring.
