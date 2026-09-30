# VLC Bookmark Studio – version archive

Nothing in this project is ever deleted. Before a new version replaces the working copy,
the previous version is kept here unchanged.

| Folder | Version | Contents | Git tag |
|---|---|---|---|
| [`v0.1.0/`](v0.1.0/) | 0.1.0 | `bm4vlc-0.1.0.zip`: the whole `bm4vlc/` folder exactly as it was at tag `bm4vlc-v0.1.0` (commit `3fb1a6b`), made with `git archive`. Note: its files sit one folder deeper than intended (`bm4vlc/bm4vlc/...`); kept as made. | `bm4vlc-v0.1.0` |
| [`v0.2.0/`](v0.2.0/) | 0.2.0 | `bm4vlc-0.2.0.zip`: the `bm4vlc/` folder at tag `bm4vlc-v0.2.0` (commit `e595105`), without `archive/` | `bm4vlc-v0.2.0` |
| [`v0.3.0/`](v0.3.0/) | 0.3.0 | `bm4vlc-0.3.0.zip`: the `bm4vlc/` folder at tag `bm4vlc-v0.3.0` (commit `575d81f`), without `archive/`. Its release build failed in CI (Linux), so no GitHub Release was published for it; 0.3.1 is the first packaged release. | `bm4vlc-v0.3.0` |
| [`v0.3.1/`](v0.3.1/) | 0.3.1 | `bm4vlc-0.3.1.zip`: the `bm4vlc/` folder at tag `bm4vlc-v0.3.1` (commit `8b59497`), without `archive/`. The last version under the name bm4vlc; 0.4.0 renamed the project to VLC Bookmark Studio (`vlc-bookmark-studio/`). | `bm4vlc-v0.3.1` |
| [`v0.4.0/`](v0.4.0/) | 0.4.0 | `vlc-bookmark-studio-0.4.0.zip`: the `vlc-bookmark-studio/` folder at tag `vlc-bookmark-studio-v0.4.0` (commit `760dff2`), without `archive/`. The first version named VLC Bookmark Studio. | `vlc-bookmark-studio-v0.4.0` |

Every version is also tagged in git (`git tag -l "bm4vlc-v*" "vlc-bookmark-studio-v*"`), so
`git checkout bm4vlc-v0.2.0 -- bm4vlc/` restores 0.2.0 as it was (under its old folder name).

## Policy for future versions

Up to 0.3.1 the project was called bm4vlc: those zips are named `bm4vlc-<version>.zip`,
their files sit under `bm4vlc/`, and their tags are `bm4vlc-v<version>`. From 0.4.0 the
project folder is `vlc-bookmark-studio/` and the tags are `vlc-bookmark-studio-v<version>`.

1. Zip the current project folder (excluding `archive/`) as
   `archive/v<OLD_VERSION>/vlc-bookmark-studio-<OLD_VERSION>.zip`:
   `git archive --format=zip --prefix=vlc-bookmark-studio/ -o vlc-bookmark-studio/archive/v<OLD>/vlc-bookmark-studio-<OLD>.zip vlc-bookmark-studio-v<OLD>:vlc-bookmark-studio ":(exclude)archive"`
2. Add a row to the table above.
3. Make the changes, bump `version` in `pyproject.toml` and `bookmark_studio/__init__.py`,
   and add a `CHANGELOG.md` entry.
4. Commit, then tag the result as `vlc-bookmark-studio-v<NEW_VERSION>`.

If you move or rename a file, use `git mv` so its history carries over. Don't remove files.
