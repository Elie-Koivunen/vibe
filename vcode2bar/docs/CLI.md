# vcode2bar – command-line reference

Everything the GUI does is also available from the command line, and the command line works without
a display (SSH, servers, WSL without WSLg, CI).

```text
python vcode2bar.py [CODE ...] [mode] [create options] [output options]
```

On Windows use `py vcode2bar.py …`. After `pip install .`, the command is just `vcode2bar …`.
With **no arguments** (or `--gui`) the GUI opens.

- [Modes](#modes)
- [Options](#options)
- [Recipes](#recipes)
- [Batch CSV format](#batch-csv-format)
- [Output formats](#output-formats)
- [Exit codes](#exit-codes)
- [Language of console output](#language-of-console-output)

---

## Modes

You can combine modes: codes from arguments, `--create`, `--read` and `--batch` are collected
into one list, and the output options apply to all of them.

| Mode | What it does |
|---|---|
| `CODE [CODE …]` | Validate and decode one or more 54-digit virtual barcodes. Spaces and dashes inside a code are allowed. A single plain `CODE` with no other options also writes `viivakoodi.png` + `viivakoodi.svg` (the 1.0 default). |
| `-` (as a CODE) | Read **text** from stdin (an invoice, an e-mail, …) and extract every valid code in it. |
| `--decode` (alias `--info-only`) | Decode and print only. Never writes images. |
| `--create --iban … --ref … [--amount …] [--due …]` | Build a virtual barcode from payment details. |
| `--read FILE/FOLDER/PATTERN …` | Read barcodes from photos, scans, screenshots and invoice PDFs. A folder gives its images/PDFs (not recursive). A wildcard like `"*.jpg"` is expanded by vcode2bar itself, so it also works in `cmd.exe` and PowerShell. |
| `--batch FILE.csv` | One virtual barcode per CSV row. See [Batch CSV format](#batch-csv-format). |
| `--ref-fi BASE` | Print a Finnish reference: `BASE` (3–19 digits) + the 7-3-1 check digit. |
| `--ref-rf REF` | Print the RF creditor reference (ISO 11649) for a Finnish reference. |
| `--gui` | Open the GUI, optionally pre-filled with the first `CODE`. |
| `--deps` | Show which optional components are installed, with install hints. |
| `--selftest [RUNS]` | Run the built-in test suite (see [DEVELOPMENT.md](DEVELOPMENT.md#tests)). |
| `--version`, `--help` | |

## Options

**Create** (used with `--create`)

| Option | Meaning |
|---|---|
| `--iban IBAN` | Finnish IBAN, spaces allowed (`FI79 4405 2020 0360 82`). Required. Bank barcodes exist only for Finnish accounts. |
| `--ref REF` | Finnish reference (4–20 digits, check digit included) or numeric RF reference (`RF09 8685 …`). Required. A Finnish reference gives a **version 4** code, an RF reference a **version 5** code. |
| `--amount AMOUNT` | `1234,56`, `1 234,56`, `1.234,56`, `1,234.56`, `12,5 €`… Max `999999,99`, two decimals. Empty or `0` = the payer enters the amount. |
| `--due DATE` | `dd.mm.yyyy`, `d.m.yyyy`, `yyyy-mm-dd`, `dd.mm.yy` or `dd/mm/yyyy`, between 2000 and 2099. Optional. |
| `--code-only` | Print only the 54 digits, one per line (also works with `--batch`, `--read`, codes). |

**Output**

| Option | Meaning |
|---|---|
| `-o STEM`, `--output STEM` | Write images as `STEM.png` / `.svg` / `.pdf`. With several codes: `STEM_1.png`, `STEM_2.png`, … |
| `--format png\|svg\|pdf\|both\|all` | `both` = png + svg (default), `all` = png + svg + A4 PDF. |
| `--text` | Print the digits under the bars. The bank specification says **not** to; off by default. |
| `--verify` | Decode every written PNG again with zbar and compare it with the code (exit 3 on mismatch). Adds PNG to the formats if needed. |
| `--json` | Print decoded fields as JSON (same fields as CSV). Informational messages go to stderr. |
| `--csv FILE` | Also write the decoded fields to a CSV file. |
| `--excel` | CSV for Nordic Excel: `;` separator, decimal comma, UTF-8 BOM, and codes as `="…"` text so the 54 digits don't turn into `4.6E+53`. |
| `--lang en\|fi\|sv\|nb` | Language of console text and of the PDF page. See [below](#language-of-console-output). |

## Recipes

```bash
# decode (English or Finnish summary)
python vcode2bar.py --decode 479440520200360820048831500000000868516259619897100612
python vcode2bar.py --decode 479440520200360820048831500000000868516259619897100612 --lang fi

# find every code in an invoice text / e-mail
cat invoice.txt | python vcode2bar.py --decode -
python vcode2bar.py --decode - --json < mail.eml

# create a code, then the printable A4 PDF with a read-back check
python vcode2bar.py --create --iban "FI79 4405 2020 0360 82" --amount 4883,15 \
                   --ref 868516259619897 --due 12.06.2010 --code-only
python vcode2bar.py --create --iban "FI79 4405 2020 0360 82" --amount 4883,15 \
                   --ref 868516259619897 --due 12.06.2010 -o lasku --format all --verify

# reference helpers
python vcode2bar.py --ref-fi 4090            # 40905
python vcode2bar.py --ref-rf 40905           # RF1140905

# read barcodes back from files, a folder, or a wildcard; collect into an Excel CSV
python vcode2bar.py --read photo.jpg invoice.pdf --csv found.csv --excel
python vcode2bar.py --read scans/ "mail/*.pdf" --json

# batch: a spreadsheet of payments -> codes, CSV, and one print-ready PDF per row
python vcode2bar.py --batch docs/examples/batch_invoices.csv
python vcode2bar.py --batch invoices.csv --csv codes.csv --excel
python vcode2bar.py --batch invoices.csv -o lasku --format pdf --lang fi   # lasku_1.pdf, lasku_2.pdf, …
```

## Batch CSV format

One payment per row, with a header row. Two shapes are accepted:

**Payment details.** `iban` and `reference` are required; `amount`, `due` and `source` are optional:

```csv
name,iban,amount,reference,due
Rent October,FI02 5000 4640 0013 02,"850,00",40905,01.10.2026
Electricity,FI79 4405 2020 0360 82,63.40,RF09 8685 1625 9619 897,2026-10-15
Donation (payer chooses amount),FI58 1017 1000 0001 22,,559582243294671,
```

**Ready-made codes.** A `code` column (other columns are then ignored except `source`/`name`).
Every CSV that vcode2bar exports has this column, so exported lists can be imported again.

Rules:

- **Header names** are matched without regard to case, accents, spaces or punctuation. The first
  matching column wins, and extra columns are ignored:

  | Field | Accepted headers |
  |---|---|
  | code | `code`, `virtual_barcode`, `virtuaaliviivakoodi`, `virtuell streckkod`, `virtuell strekkode` |
  | iban | `iban`, `account`, `tili`, `tilinumero`, `konto`, `kontonummer` |
  | amount | `amount`, `amount_eur`, `sum`, `summa`, `belopp`, `beløp` |
  | reference | `reference`, `ref`, `viite`, `viitenumero`, `referens`, `referanse` |
  | due | `due`, `due_date`, `eräpäivä`, `förfallodag`, `förfallodatum`, `forfallsdato` |
  | source | `source`, `name`, `label`, `invoice`, `nimi`, `lasku`, `namn`, `faktura`, `navn` |

- **Separator** `,`, `;` or tab is detected from the header line. With `,` separators, quote amounts
  that use a decimal comma (`"850,00"`), as spreadsheet programs do.
- **Encoding**: UTF-8 (with or without BOM) or Windows-1252 (older Excel "CSV (semicolon delimited)").
- **Values** use the same rules as `--create`. Spaces inside IBANs and references are fine.
- **Source label**: the `source`/`name` value, else `file.csv:LINE`. It appears in the list, the
  CSV/JSON `source` field and the console output.
- **Blank lines** are skipped.
- **Errors**: a row that fails validation is reported as
  `ERROR: file.csv line 5 (iban): IBAN checksum failed: …` on stderr. The other rows are still
  processed, and the exit code is 2. The example file has one deliberately broken row to show this.

## Output formats

**Console** (default): one block per code. With several codes each block starts with `--- source`.

```text
Version:         4 (FI)
Account (IBAN):  FI79 4405 2020 0360 82
Amount:          4 883,15 €
Reference:       868516259619897
Due date:        12.06.2010
NOTE: the due date has passed (5953 days ago).
```

**JSON / CSV fields** (identical in both; the stable interface for scripts):

| Field | Example | Notes |
|---|---|---|
| `source` | `argument`, `created`, `stdin`, `photo.jpg`, `Rent October` | where the code came from |
| `code` | `4794…0612` | 54 digits (`="…"` in `--excel` CSV) |
| `version` | `4` | 4 = Finnish reference, 5 = RF reference |
| `reference_type` | `FI` / `RF` | |
| `iban` | `FI7944052020036082` | no spaces |
| `amount_eur` | `4883.15` | decimal comma in `--excel` CSV; `0.00` = payer enters the amount |
| `reference` | `868516259619897` / `RF09868516259619897` | |
| `due_date` | `2010-06-12` | ISO date, empty if not given |
| `days_overdue` | `5953` | 0 if not overdue, empty if no due date |
| `decoded_at` | `2026-09-29T10:15:02` | local time |

Spreadsheet formula injection is neutralised: a value starting with `=`, `+`, `-`, `@`, tab or CR
gets a leading `'`.

**Images** follow the Finanssiala *Pankkiviivakoodi-opas* v5.3. The barcode is Code 128 set C,
0.30 mm modules (99.6 mm of bars), 12 mm high, with 20 mm quiet zones and no digits under the bars.
PNG is written at 600 dpi and SVG as vectors. The PDF is an A4 page with the payment details on
top and the full-size barcode below.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | `--selftest`: required components missing |
| 2 | Invalid input: a bad code, `--create` field or batch row, a missing/unusable batch file, or no code found on stdin |
| 3 | `--verify`: an image did not decode back to the same code, **don't use it** |
| 4 | GUI requested but unavailable (no tkinter or no display) |
| 5 | `--read`: a file had no readable virtual barcode |

When several things go wrong, the first code set wins, except that 3 overrides.

## Language of console output

Console labels, the overdue note and the saved/read-back messages come in English, Finnish, Swedish
or Norwegian. The language is chosen in this order:

1. `--lang xx`
2. the language last chosen in the GUI (the settings file, see the [user guide](USER_GUIDE.md#settings))
3. English

Error messages about invalid input are always in English, and `--json`/`--csv` output is
language-independent. Scripts should use those.
