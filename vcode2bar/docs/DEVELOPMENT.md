# vcode2bar – developer guide

vcode2bar is deliberately **one Python file** (`vcode2bar.py`). You can copy it anywhere and run it
with no install step, and it runs the same on Windows, macOS, Linux and WSL. This guide explains how
the file is organised, how the test suite works, and how to change things safely.

- [Code map](#code-map)
- [Data flow](#data-flow)
- [The specification in code](#the-specification-in-code)
- [Tests](#tests)
- [Adding or changing UI text / a language](#adding-or-changing-ui-text--a-language)
- [Continuous integration](#continuous-integration)
- [Packaging](#packaging)
- [Releasing a new version (archive policy)](#releasing-a-new-version-archive-policy)

---

## Code map

`vcode2bar.py` is organised in sections, top to bottom. Search for the function names below.

| Section | Main names | Purpose |
|---|---|---|
| Parsing & validation | `VirtualBarcode`, `parse()`, `_iban_ok`, `_fi_ref_check_digit`, `_rf_ok` | 54 digits → validated fields. Raises `VirtualBarcodeError`. |
| Building codes | `build_code()`, `normalize_iban`, `normalize_reference`, `parse_amount`, `parse_due`, `fi_reference`, `rf_reference`, `FieldError` | The inverse of `parse()`. Every error names the offending field (`FieldError.field`) so the GUI and CLI can point at it. `build_code` round-trips its result through `parse` as a guard. |
| Rendering | `SPEC_OPTS`, `render_image()`, `render()`, `code128c_checksum`, `verify_png` | Code 128 set C via python-barcode. PNG/SVG writers plus in-memory PIL images. |
| Finding codes in text | `CODE_RE`, `iter_codes()`, `extract_code()` | Overlapping-lookahead regex, so a stray digit before the code can't hide it. Spaces/dashes inside allowed. |
| CSV export | `CSV_FIELDS`, `to_record()`, `write_csv()`, `_csv_safe` | Standard and Excel-Nordic dialects, with a formula-injection guard. |
| Batch import | `BATCH_COLUMNS`, `BatchRow`, `read_batch_csv()`, `_header_key` | CSV → one `BatchRow` per data row (valid `vb` or `error` + `field` + `line`). Shared by the GUI import and `--batch`. |
| Reading images/PDFs | `READ_EXTS`, `expand_inputs()`, `ScanResult`, `_variants()`, `read_image()`, `read_file()` | zbar decoding with progressively heavier preprocessing (autocontrast, scaling, median filters, threshold, rotations, tilts). Stops at the first variant that yields a code. PDFs use the text layer and page render at 300 dpi. |
| Settings & components | `_config_path`, `load_config`, `save_config`, `component_status()` | Per-OS settings file. `--deps` / *Check components* report. |
| WSL integration | `IS_WSL`, `wsl_open`, `wsl_clipboard`, `wsl_default_dir`, `wsl_windows_env` | Printing, clipboard and default folder via Windows interop. |
| UI strings | `LANGUAGES`, `T` | One dict per language, identical keys (enforced by a test). |
| PDF page & formatting | `make_pdf_page`, `pdf_font`, `ascii_safe`, `fmt_amount`, `fmt_ref`, `fmt_iban` | A4 page at 600 dpi. Falls back to ASCII if no system font has Nordic glyphs. |
| GUI | `_Actions` (mixin: create tab, reading, CSV import, list, CSV export), `App` (window, menus, help windows, input/preview), `run_gui()` | Tkinter + ttk (`clam` theme). |
| Self-test | `selftest()`, `_t_core`, `_t_create`, `_t_read`, `_t_batch`, `_t_platform`, `_t_gui` + `gui_*_tests` | See [Tests](#tests). |
| CLI | `main()`, `_emit`, `_cli_lang`, `cli()`, `gui_main()` | argparse front end. `cli`/`gui_main` are the packaged entry points. |

Optional imports are handled once at the top: without tkinter the CLI still works, without pyzbar
reading and the read-back check are disabled, and without pypdfium2 PDFs are disabled.

## Data flow

```text
 text / e-mail ──► iter_codes ─┐
 54 digits ────────────────────┤
 photo / scan / PDF ─► read_file ─► read_image (zbar) ─┤
 payment details ─► build_code ────────────────────────┼──► parse() ──► VirtualBarcode
 CSV rows ─► read_batch_csv (build_code / parse) ──────┘                   │
                                                                          ├─► render_image / render ─► PNG · SVG
                                                                          ├─► make_pdf_page ─► A4 PDF / print
                                                                          ├─► to_record ─► CSV · JSON
                                                                          └─► describe(lang) ─► console
```

Everything converges on `parse()`: a `VirtualBarcode` object always holds a fully validated code.

## The specification in code

Source: Finanssiala, *Pankkiviivakoodi-opas* v5.3.

| Rule | Where |
|---|---|
| 54 digits, version 4 (Finnish reference) or 5 (RF reference) | `parse` |
| IBAN mod-97 on `FI` + 16 digits | `_iban_ok`, `normalize_iban` |
| Amount 6 + 2 digits; all zeros = payer enters the amount | `parse`, `parse_amount` (`MAX_AMOUNT = 999999.99`) |
| v4: `000` + 20-digit reference; Finnish 7-3-1 check digit | `parse`, `_fi_ref_check_digit` |
| v5: 2 RF check digits + 21-digit body; ISO 11649 mod-97; all zeros allowed when the RF reference contains letters | `parse`, `_rf_ok`, `rf_reference` |
| Due date `YYMMDD`, `000000` = none, years 2000–2099 | `parse`, `parse_due` |
| Code 128 **set C** only (start code 105) | asserted in `render_image` / `render` |
| Module 0.30 mm → 332 modules = 99.6 mm (70–105 mm allowed); height 12 mm (10–12.7); 20 mm quiet zones; no text | `SPEC_OPTS` |

The spec's own test invoices (18 of them) are in `OFFICIAL`, with their printed Code 128 check
characters. One erratum was found: v5 test invoice 3 prints check character 59, but the spec's
algorithm (and python-barcode) give 34. This is left over from the 2019 change of example data.

## Tests

```bash
python vcode2bar.py --selftest          # every suite once
python vcode2bar.py --selftest 3        # three runs with different random seeds
xvfb-run -a python vcode2bar.py --selftest 2   # headless Linux: virtual display for the GUI suite
```

The self-test needs every component (`--deps` all OK, clipboard tools excepted). The GUI suite runs
only if a window can be opened. The tests use a temporary settings file, so they never touch yours.
A failing run doesn't stop the others: each failure is printed as `FAIL` with its traceback, the
remaining suites still run, and the summary line reads `FAILED: …` (exit code 1) instead of
`ALL PASS`.

| Suite | Checks per run | Covers |
|---|---|---|
| `core` | ≈333 | 18 official invoices (fields + Code 128 checksums + pure set C), third-party vectors, 8 invalid inputs, 300 random codes rendered as PNG + SVG and decoded back |
| `create` | ≈295 | All official invoices rebuilt from their printed fields in varied input styles, 150 random round trips, reference helpers, amount/date parsing, every invalid field mapped to the right `FieldError.field`, every CLI switch |
| `read/csv` | ≈60 | CSV in both dialects, injection guard, images rotated/tilted/scaled/noisy/JPEG, two codes in one image, non-bank barcodes rejected, image-only/text-only/empty PDFs, text extraction with stray digits, CLI `--read` |
| `batch` | ≈32 | Batch CSV in English/Finnish/Swedish/Norwegian headers, `,`/`;`/tab, UTF-8/BOM/cp1252, line numbers and fields of bad rows, re-import of exports, 60 random rows, CLI `--batch` with every output, `--read` with folders and wildcards, console languages and the saved-language fallback |
| `platform` | ≈15 | Settings path per OS, legacy settings, consoles that can't print `✓`/`€`, no display → exit 4, string-table completeness, Python 3.9 grammar |
| `gui` | ≈240 | Clipboard pre-fill, fields and overdue colours, preview and read-back, invalid input, typing debounce, all 4 languages for every menu, help window and widget, shortcuts inside the text box, settings persistence, saving PNG/SVG/PDF, the Create form, file/clipboard reading, list and CSV export, **CSV import**, error reporting when rendering/preview/zbar fail, WSL integration (with stubbed Windows tools) |

Random data comes from fixed seeds (`run × prime`), so a failure can be reproduced by running the
same number of runs. The generators (`random_case`, `mk_iban`, `mk_rf`, `fi_ref`) are independent
re-implementations, not calls into the code under test.

**Writing a test.** Add assertions to the matching suite function and count them (`n += 1`).
For the CLI use `_cli([...], stdin_text=None)`, which returns `(rc, stdout, stderr)`. For the GUI,
call `pump(root)` after every action, and stub dialogs (`filedialog.*`, `messagebox.*`) instead of
letting them open. Restore every monkeypatch in a `finally` block.

**Keyboard shortcuts in tests: use `press(app, root, "<Control-l>")`, never `event_generate` on a
key directly.** A synthetic key press only reaches a window that has keyboard focus, and the
operating system may refuse that: Windows' foreground lock on CI machines, or you working in
another window while the tests run. `press` sends the real key event when the input box has focus.
Otherwise it runs the handler that the key is bound to (from `App.shortcut_handlers`) and checks
that it returns `"break"`, which is what keeps Tk's own text-box bindings from firing. In 1.3.0,
direct `event_generate` calls made the GUI suite fail on GitHub's Windows runners and on a busy
desktop.

## Adding or changing UI text / a language

- All user-visible GUI and console strings live in `T[lang][key]`. Change a text in **all four**
  languages. The `platform` suite fails if a language is missing a key or has different shortcut rows.
- New language: add it to `LANGUAGES` and copy the `T['en']` block. The menu tests loop over
  `LANGUAGES` automatically. Error messages from the validation core are English only by design.
- Keyboard shortcuts appear in three places: the bindings in `App._build`, the menu accelerators
  in `_build_menu`, and `shortcut_rows` (the Help window). Keep them in sync.

## Continuous integration

`.github/workflows/vcode2bar-selftest.yml` (at the **repository root**, because GitHub only runs
workflows from there) runs on every branch push or PR that touches `vcode2bar/`. Tag pushes are
excluded, because GitHub ignores `paths` for tags and would otherwise run it for every project's tags.

- Windows, Ubuntu 22.04 and Ubuntu 24.04 × Python 3.9 and 3.12
- `pip install -r requirements.txt`, `--deps`, `--selftest 2` (Linux under `xvfb-run`)
- `pip install .` and a smoke test of the installed `vcode2bar` command, including `--batch` on the
  example file

**When it fails:** GitHub shows full job logs only to signed-in users. The workflow therefore also
posts the failing part of the self-test output as an error annotation and on the run's **Summary**
page, where anyone can read it, including through the API
(`GET /repos/Elie-Koivunen/vibe/check-runs/<job id>/annotations`).

## Packaging

`pyproject.toml` builds a single-module distribution (`py-modules = ["vcode2bar"]`). The version is
read from `__version__`. It provides two commands:

- `vcode2bar` → `vcode2bar:cli`
- `vcode2bar-gui` → `vcode2bar:gui_main` (a GUI script: no console window on Windows)

Optional extras: `pip install ".[read]"` also installs pyzbar and pypdfium2.

## Releasing a new version (archive policy)

Nothing is deleted. Old versions are archived, so every earlier state stays available:

1. Archive the current version: zip `vcode2bar/` (without `archive/`) into
   `archive/v<OLD>/vcode2bar-<OLD>.zip` and add a row to [`archive/README.md`](../archive/README.md).
2. Make the change and bump `__version__` in `vcode2bar.py`.
3. Add a section to [`CHANGELOG.md`](../CHANGELOG.md).
4. Run `python vcode2bar.py --selftest 2` (and let CI run on Windows + Linux).
5. Commit, then tag: `git tag -a vcode2bar-v<NEW> -m "vcode2bar <NEW>"`.
6. Use `git mv` for renames, never a delete. If a file must leave the working tree, it goes into
   `archive/` first.
