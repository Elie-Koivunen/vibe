# Changelog – vcode2bar

All notable changes. Versions follow [semantic versioning](https://semver.org/).
Every released version is archived unchanged under [`archive/`](archive/) and tagged
`vcode2bar-v<version>` in git.

## 1.3.0 – 2026-09-29

First version published in the [`vibe`](https://github.com/Elie-Koivunen/vibe) repository.
Imported from the 1.2.0 delivery (archived in [`archive/v1.2.0/`](archive/v1.2.0/), tag `vcode2bar-v1.2.0`).

### Added
- **Batch CSV – many invoices at once.**
  - GUI: *File → Import CSV…* (`Ctrl+I`) adds every valid row of a spreadsheet to the List tab.
    Bad rows are listed with line number and field, and don't stop the others.
  - CLI: `--batch FILE.csv` works with all the existing outputs: `--json`, `--csv`, `--code-only`,
    and `-o STEM --format png|svg|pdf|all` for one image or PDF per row.
  - Accepts either a `code` column or `iban` + `reference` (+ optional `amount`, `due`, `source`/`name`)
    columns. Header names can be English, Finnish, Swedish or Norwegian (`tili`, `summa`, `viite`,
    `eräpäivä`, `belopp`, `beløp`, …).
  - Detects `,` `;` or tab separators and UTF-8 (with or without BOM) or Windows-1252 encoding.
  - vcode2bar's own CSV exports (standard and Excel) import back unchanged.
  - Example: [`docs/examples/batch_invoices.csv`](docs/examples/batch_invoices.csv).
- **`--read` accepts folders and wildcards.** `--read scans/` reads every image/PDF in the folder, and
  `--read "mail/*.pdf"` works even on Windows, where the shell doesn't expand `*`.
- **Localized console output.** `--decode` output, the overdue note, and the "saved" and read-back
  messages follow `--lang`, else the language last chosen in the GUI, else English. In 1.2.0 they
  were always Finnish.
- **Packaging.** A `pyproject.toml`, so `pip install .` / `pipx install .` gives `vcode2bar` (CLI) and
  `vcode2bar-gui` (starts without a console window on Windows). Running the single file as
  `python vcode2bar.py` still works as before.
- **Windows launcher** `launch_vcode2bar.bat` (double-click to open the GUI; passes arguments through).
- Hourglass cursor while images, PDFs or CSV files are being read.
- Documentation: [user guide](docs/USER_GUIDE.md), [command-line reference](docs/CLI.md),
  [developer guide](docs/DEVELOPMENT.md), this changelog, and new screenshots.
- Tests: a new `batch` suite (CSV import, `--batch`, folder/wildcard `--read`, console languages), a GUI
  import test, and a check that every language has every string.

### Fixed
- **Keyboard focus after closing a help window.** Closing *User guide*, *Keyboard shortcuts*, etc.
  left no widget with keyboard focus under some window managers (seen on WSLg). `Esc` and the
  `Ctrl` shortcuts did nothing until you clicked the main window, and the 1.2.0 GUI self-test failed
  there. Focus now returns to the input box.
- The read-back check (`verify_png`, used by `--verify`) left the PNG file open. On Windows an open
  handle blocks deleting or overwriting the file.
- The self-test no longer reads or writes your real settings file. A saved language no longer changes
  test results, and running the tests no longer resets your settings.
- `App.verified` and `App.render_error` are initialised at start-up (they only existed after the
  first render).

### Changed
- The CI workflow moved from `vcode2bar/.github/workflows/selftest.yml` (GitHub ignores workflows in
  subfolders) to the repository root as `.github/workflows/vcode2bar-selftest.yml`. It runs only when
  `vcode2bar/` changes, and now also tests `pip install .` and the installed command.
- `.gitattributes` keeps LF line endings in this folder, so one checkout works on Windows and under
  WSL.
- Console summary layout: labels are aligned and localized, the version shows `4 (FI)` / `5 (RF)`,
  and an open amount reads "open (payer decides)" instead of "ei annettu".
  Scripts should use `--json`, which is unchanged.

## 1.2.0 – 2026-09-28

The original delivery (see [`archive/v1.2.0/`](archive/v1.2.0/)): GUI with Barcode / Create / List tabs
in four languages, CLI with `--decode`, `--create`, `--read`, `--ref-fi`, `--ref-rf`, CSV/JSON export,
PNG/SVG/PDF output, reading from images and PDFs, WSL integration, and the built-in self-test.
