---
name: sp16-pull-request
description: Commit and open pull requests for the SP16 emulator. Covers the branch, commit style, which checks a change needs (unit tests with coverage, the end-to-end smoke test with its sped-up screencast, the hardware suite), and the PR description. Use when committing, opening or updating a pull request, or when asked for test evidence, coverage or a screencast.
---

# Commits and pull requests

Read [AGENTS.md](../../../AGENTS.md) and [CONTRIBUTING.md](../../../CONTRIBUTING.md) first. Their rules hold here:
no firmware or firmware-derived material, no mods, nothing from `build/` or `private/`.

## 1. Branch

- Work on a topic branch made from the current `origin/main`. If the change depends on an open pull request, branch
  from that PR's branch and open the new PR against it (a stacked PR).
- Never commit to `main`, never force-push a branch others use, never rewrite history: old commits stay; only the
  current code must be clean.

## 2. Commits

- **One theme per commit, and every commit passes `./sp16 test`.** When a working tree mixes themes, split it by
  hunk instead of making one large commit.
- **Message style of this repository:** an imperative subject of at most about 70 characters, no Conventional
  Commits prefix (an area such as `Panel:`, `Diagnostics:` or `clean:` is fine when it helps), a blank line, then a
  body that says why and what changed, in short paragraphs or `-` bullets. Name what you verified when it was more
  than the unit tests ("Verified with a fresh build and a boot of firmware 1.60").
- Agents add the co-author trailer their host asks for. Never put personal e-mail addresses, local paths or tokens
  in a message.
- Before committing, read the whole staged diff (`git diff --cached`) and run `git diff --check`.

## 3. Checks: what a change needs

| The change touches | Run | Put in the PR |
|---|---|---|
| anything | `./sp16 test`; with coverage.py installed, `./sp16 test --coverage` | test count; coverage total and the changed files' coverage |
| the app, the build or launch (`*.swift`, `native.py`), the guest (`guest-*.sh/py`, `pack-guest.sh`, `Dockerfile.native-base`), `hardware_stub.c`, the panel model, profiles | a research build (`make build-dev`) and `./sp16 smoke` | smoke verdict and duration; the screencast |
| the emulated hardware boundary (shim, bridge, panel, protocol) or profile measurements | `./sp16 hwsuite --smoke` (`--tier full` to verify a profile) | the suite's summary |
| Swift sources only, quickly | `cd emulator && xcrun swiftc -typecheck -parse-as-library <SWIFT_SOURCES from native.py>` | — |
| commands, options or docs | `./sp16 help <command>`, `make -n <target>`; update README, `docs/` and these skills | — |

Report failures and skipped checks as they are. CI (`.github/workflows/tests.yml`) runs the unit tests with coverage
on macOS and refuses committed firmware, keys and images; the smoke test and the hardware suite need firmware, so
they run only on a Mac with an import.

## 4. Smoke test and screencast

```sh
make build-dev                 # build/sp16-dev: research socket, WAV recording (once; --rebuild after code changes)
./sp16 smoke                   # about a minute; --speed 6, --no-video, --keep-frames, --firmware ID
```

It boots the selected firmware and checks a settled home screen, live lamps, a selector detent that changes the LCD,
pad presses and PLAY/STOP reaching the firmware, the mounted USB stick and a clean stop. It records the whole panel
through the control socket (no screen-recording permission) and writes `build/sp16-dev/smoke/<run id>/`:
`smoke-report.md`, `smoke-result.json`, `screencast.mp4` and `screencast.gif` (4x speed by default; MP4 and GIF need
ffmpeg, `brew install ffmpeg`; without it a GIF is made with Pillow). It never plays sound and stops only the session
it started. Rebuild the dev build after changing app or guest code: the smoke test runs what was built.

**Attaching the screencast:** `gh` cannot upload files to a pull request. Give the GIF or MP4 to the user (the PR
author drags it into the description or a comment on GitHub), or upload it through the GitHub web page in a browser
only after the user agrees. The screencast shows the firmware's own screen, like the screenshots in a diagnostics zip:
fine in a PR, never committed to the repository.

## 5. The pull request

- Title: what the change does, in the same style as a commit subject.
- Body (write it to a file and pass `--body-file`): **Summary** (what and why, grouped by theme), **Verification**
  (commands and results: tests and coverage, smoke verdict, hardware suite if run, real builds), **Screencast**
  (placeholder line where the author drops the file), **Limitations** (what was not run or not verified). Link the
  issue it fixes.
- `gh pr create --base <base> --head <branch> --title "<title>" --body-file <file>`; open a draft when the work is
  incomplete. Then check the URL, base and head with `gh pr view`, and look at CI once with `gh pr checks` (do not
  poll in a loop).
- For a review before merging, an agent can run its host's code-review tooling on the branch.
