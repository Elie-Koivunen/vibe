# bm4vlc – version archive

Nothing in this project is ever deleted. Before a new version replaces the working copy,
the previous version is kept here unchanged.

| Folder | Version | Contents | Git tag |
|---|---|---|---|
| [`v0.1.0/`](v0.1.0/) | 0.1.0 | `bm4vlc-0.1.0.zip`: the whole `bm4vlc/` folder exactly as it was at tag `bm4vlc-v0.1.0` (commit `3fb1a6b`), made with `git archive`. Note: its files sit one folder deeper than intended (`bm4vlc/bm4vlc/...`); kept as made. | `bm4vlc-v0.1.0` |
| [`v0.2.0/`](v0.2.0/) | 0.2.0 | `bm4vlc-0.2.0.zip`: the `bm4vlc/` folder at tag `bm4vlc-v0.2.0` (commit `e595105`), without `archive/` | `bm4vlc-v0.2.0` |
| [`v0.3.0/`](v0.3.0/) | 0.3.0 | `bm4vlc-0.3.0.zip`: the `bm4vlc/` folder at tag `bm4vlc-v0.3.0` (commit `575d81f`), without `archive/`. Its release build failed in CI (Linux), so no GitHub Release was published for it; 0.3.1 is the first packaged release. | `bm4vlc-v0.3.0` |

Every version is also tagged in git (`git tag -l "bm4vlc-v*"`), so
`git checkout bm4vlc-v0.2.0 -- bm4vlc/` restores it as it was.

## Policy for future versions

1. Zip the current `bm4vlc/` project folder (excluding `archive/`) as
   `archive/v<OLD_VERSION>/bm4vlc-<OLD_VERSION>.zip`:
   `git archive --format=zip --prefix=bm4vlc/ -o bm4vlc/archive/v<OLD>/bm4vlc-<OLD>.zip bm4vlc-v<OLD>:bm4vlc ":(exclude)archive"`
2. Add a row to the table above.
3. Make the changes, bump `version` in `pyproject.toml` and `bookmark_studio/__init__.py`,
   and add a `CHANGELOG.md` entry.
4. Commit, then tag the result as `bm4vlc-v<NEW_VERSION>`.

If you move or rename a file, use `git mv` so its history carries over. Don't remove files.
