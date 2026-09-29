# vcode2bar – version archive

Nothing in this project is ever deleted. Before a new version replaces the working copy,
the previous version is kept here unchanged, in the exact form it was delivered or released.

| Folder | Version | Contents | Git tag |
|---|---|---|---|
| [`v1.2.0/`](v1.2.0/) | 1.2.0 | `files.zip` – the original delivery: `vcode2bar.zip` (full project) + a loose copy of `vcode2bar.py` (byte-identical to the one inside the project zip) | `vcode2bar-v1.2.0` |
| [`v1.3.0/`](v1.3.0/) | 1.3.0 | `vcode2bar-1.3.0.zip` – the released project folder (without `archive/`) plus its CI workflow `.github/workflows/vcode2bar-selftest.yml`, exported with `git archive` from the tag | `vcode2bar-v1.3.0` |
| [`v1.3.1/`](v1.3.1/) | 1.3.1 | `vcode2bar-1.3.1.zip` – same layout as 1.3.0 | `vcode2bar-v1.3.1` |

Every version is also tagged in git (`git tag -l "vcode2bar-v*"`), so
`git checkout vcode2bar-v1.2.0 -- vcode2bar/` restores it as it was imported.

## Policy for future versions

1. Zip the current `vcode2bar/` project folder (excluding `archive/`) as
   `archive/v<OLD_VERSION>/vcode2bar-<OLD_VERSION>.zip`, or copy in the zip it came from.
2. Add a row to the table above.
3. Make the changes, bump `__version__` in `vcode2bar.py`, add a `CHANGELOG.md` entry.
4. Commit, then tag the result as `vcode2bar-v<NEW_VERSION>`.

If you move or rename a file, use `git mv` so its history carries over. Don't remove files.
