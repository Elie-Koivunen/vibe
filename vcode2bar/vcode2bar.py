#!/usr/bin/env python3
"""
vcode2bar - Finnish virtual barcode (virtuaaliviivakoodi) <-> bank barcode.

Single-file tool with GUI and CLI:
  * validate + decode a 54-digit virtual barcode (versions 4 and 5)
  * render the Code 128 set C bank barcode per Finanssiala Pankkiviivakoodi-opas v5.3
    (PNG 600 dpi, SVG, print-ready A4 PDF)
  * read barcodes back from images (photos, scans, screenshots) and invoice PDFs
  * collect codes and export CSV (Excel-Nordic or standard)
  * GUI in English (default), Finnish, Swedish and Norwegian

Usage:
    python vcode2bar.py                                  # GUI
    python vcode2bar.py CODE [-o name] [--format png|svg|both] [--verify]
    python vcode2bar.py CODE1 CODE2 --csv out.csv [--excel]
    python vcode2bar.py --read photo.jpg invoice.pdf [--csv out.csv]
    python vcode2bar.py --deps                           # show optional components
    python vcode2bar.py --selftest [RUNS]                # built-in test suite

Requires: pip install python-barcode pillow
Optional: pyzbar (+ zbar library) for reading images / read-back check, pypdfium2 for PDFs.
GUI uses tkinter (bundled on Windows/macOS; Debian/Ubuntu: apt install python3-tk).
License: MIT
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import functools
import io
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

__version__ = "1.2.0"

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    sys.exit("Pillow is required: pip install pillow")

try:  # GUI is optional - the CLI works without tkinter
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from PIL import ImageTk
except Exception:  # noqa: BLE001
    tk = filedialog = messagebox = ttk = ImageTk = None

try:
    from pyzbar.pyzbar import decode as _zbar_decode
except Exception:  # noqa: BLE001  pyzbar or libzbar missing
    _zbar_decode = None


# =====================================================================================
# Core: parsing, validation, rendering, CSV, reading images/PDFs
# =====================================================================================
class VirtualBarcodeError(ValueError):
    pass


@dataclass
class VirtualBarcode:
    raw: str
    version: int
    iban: str
    amount: Decimal
    reference: str
    due_date: date | None

    def describe(self) -> str:
        iban_fmt = " ".join(self.iban[i:i + 4] for i in range(0, len(self.iban), 4))
        amt = "ei annettu" if self.amount == 0 else f"{self.amount:.2f} EUR".replace(".", ",")
        due = self.due_date.strftime("%d.%m.%Y") if self.due_date else "ei annettu"
        return (f"Versio:    {self.version}\n"
                f"Tili:      {iban_fmt}\n"
                f"Summa:     {amt}\n"
                f"Viite:     {self.reference}\n"
                f"Eräpäivä:  {due}")


def _iban_ok(iban: str) -> bool:
    r = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in r)) % 97 == 1


def _fi_ref_check_digit(base: str) -> int:
    weights = (7, 3, 1)
    s = sum(int(d) * weights[i % 3] for i, d in enumerate(reversed(base)))
    return (10 - s % 10) % 10


def _rf_ok(rf: str) -> bool:
    r = rf[4:] + rf[:4]
    return int("".join(str(int(c, 36)) for c in r)) % 97 == 1


def parse(code: str) -> VirtualBarcode:
    """Validate and decode a virtual barcode. Raises VirtualBarcodeError."""
    s = re.sub(r"[\s\-]", "", code)
    if not s.isdigit():
        raise VirtualBarcodeError("Code must contain digits only.")
    if len(s) != 54:
        raise VirtualBarcodeError(f"Code must be 54 digits, got {len(s)}.")
    version = int(s[0])
    if version not in (4, 5):
        raise VirtualBarcodeError(f"Unsupported version {version} (only 4 and 5 are valid).")

    iban = "FI" + s[1:17]
    if not _iban_ok(iban):
        raise VirtualBarcodeError(f"IBAN checksum failed: {iban}")

    amount = Decimal(int(s[17:23])) + Decimal(int(s[23:25])) / 100

    if version == 4:
        if s[25:28] != "000":
            raise VirtualBarcodeError("Reserved field (pos 26-28) must be 000 in version 4.")
        ref = s[28:48].lstrip("0")
        if not 4 <= len(ref) <= 20:
            raise VirtualBarcodeError("Reference must be 4-20 digits.")
        if _fi_ref_check_digit(ref[:-1]) != int(ref[-1]):
            raise VirtualBarcodeError(f"Reference check digit failed: {ref}")
        reference = ref
    else:
        check = s[25:27]
        body = s[27:48].lstrip("0")
        if check == "00" and not body:
            # Spec allows zeros when the RF reference contains letters (not encodable)
            reference = "(RF-viite ei numeerinen - katso lasku)"
        else:
            if not body:
                raise VirtualBarcodeError("RF reference body is empty.")
            reference = f"RF{check}{body}"
            if not _rf_ok(reference):
                raise VirtualBarcodeError(f"RF reference checksum failed: {reference}")

    d = s[48:54]
    due = None
    if d != "000000":
        try:
            due = date(2000 + int(d[0:2]), int(d[2:4]), int(d[4:6]))
        except ValueError:
            raise VirtualBarcodeError(f"Invalid due date field: {d}")

    return VirtualBarcode(s, version, iban, amount, reference, due)


# ---------------------------------------------------------------- building codes (form / --create)
class FieldError(VirtualBarcodeError):
    """Validation error tied to one input field: iban, amount, reference or due."""
    def __init__(self, field_name: str, msg: str):
        super().__init__(msg)
        self.field = field_name


MAX_AMOUNT = Decimal("999999.99")


def parse_amount(text) -> Decimal:
    """'1 234,56' / '1234.56' / '12,5 €' / '' (open amount) -> Decimal."""
    if isinstance(text, (int, float, Decimal)):
        s = str(text)
    else:
        s = re.sub(r"[\s\u00a0€]|EUR", "", str(text or ""), flags=re.I)
    if s == "":
        return Decimal("0")
    if "," in s and "." in s:            # 1.234,56 or 1,234.56 -> last separator is the decimal one
        dec_sep = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec_sep == "," else ",", "")
    s = s.replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", s):
        raise FieldError("amount", f"Invalid amount: {text!r} (use e.g. 1234,56; max 2 decimals)")
    v = Decimal(s)
    if v > MAX_AMOUNT:
        raise FieldError("amount", f"Amount {v} exceeds the barcode maximum 999999.99")
    return v


def parse_due(text) -> date | None:
    """'' -> None; accepts dd.mm.yyyy, d.m.yyyy, yyyy-mm-dd, dd.mm.yy or a date object."""
    if text is None or isinstance(text, date):
        d = text
    else:
        s = str(text).strip()
        if not s:
            return None
        d = None
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y"):
            try:
                d = datetime.strptime(s, fmt).date(); break
            except ValueError:
                continue
        if d is None:
            raise FieldError("due", f"Invalid due date: {text!r} (use dd.mm.yyyy)")
    if d is not None and not (2000 <= d.year <= 2099):
        raise FieldError("due", "Due date must be between 2000 and 2099")
    return d


def fi_reference(base: str) -> str:
    """Append the Finnish check digit (weights 7-3-1) to a 3-19 digit base."""
    b = re.sub(r"\s", "", str(base)).lstrip("0")
    if not re.fullmatch(r"\d{3,19}", b):
        raise FieldError("reference", "Reference base must be 3-19 digits")
    return b + str(_fi_ref_check_digit(b))


def rf_reference(body: str) -> str:
    """ISO 11649 creditor reference: 'RF' + 2 check digits + body (e.g. a Finnish reference)."""
    b = re.sub(r"\s", "", str(body)).upper()
    if b.startswith("RF"):
        raise FieldError("reference", "Already an RF reference")
    if not re.fullmatch(r"[0-9A-Z]{1,21}", b):
        raise FieldError("reference", "RF body must be 1-21 letters/digits")
    chk = 98 - int("".join(str(int(c, 36)) for c in b + "RF00")) % 97
    return f"RF{chk:02d}{b}"


def normalize_iban(iban: str) -> str:
    s = re.sub(r"[\s\-]", "", str(iban or "")).upper()
    if not re.fullmatch(r"FI\d{16}", s):
        raise FieldError("iban", "Account must be a Finnish IBAN: FI + 16 digits "
                                 "(bank barcodes only exist for Finnish accounts)")
    if not _iban_ok(s):
        raise FieldError("iban", f"IBAN checksum failed: {s}")
    return s


def normalize_reference(ref: str) -> str:
    s = re.sub(r"[\s\-]", "", str(ref or "")).upper()
    if not s:
        raise FieldError("reference", "Reference is required")
    if s.startswith("RF"):
        body = s[4:].lstrip("0") or "0"
        if not re.fullmatch(r"RF\d\d\d+", s):
            raise FieldError("reference", "RF reference must be numeric to fit in a bank barcode")
        if len(body) > 21:
            raise FieldError("reference", "RF reference body is longer than 21 digits")
        if not _rf_ok(s):
            raise FieldError("reference", f"RF reference checksum failed: {s}")
        return f"RF{s[2:4]}{body}"
    s = s.lstrip("0")
    if not re.fullmatch(r"\d{4,20}", s):
        raise FieldError("reference", "Finnish reference must be 4-20 digits including the check digit")
    if _fi_ref_check_digit(s[:-1]) != int(s[-1]):
        raise FieldError("reference", f"Reference check digit is wrong: {s} "
                                      f"(for base {s[:-1]} it is {_fi_ref_check_digit(s[:-1])})")
    return s


def build_code(iban, amount, reference, due=None) -> str:
    """Create a 54-digit virtual barcode from payment details. Version 4 for Finnish
    references, 5 for RF references. Raises FieldError naming the offending field."""
    ib = normalize_iban(iban)
    amt = parse_amount(amount)
    ref = normalize_reference(reference)
    d = parse_due(due)
    euros, cents = int(amt), int((amt * 100) % 100)
    dd = d.strftime("%y%m%d") if d else "000000"
    if ref.startswith("RF"):
        code = f"5{ib[2:]}{euros:06d}{cents:02d}{ref[2:4]}{ref[4:].zfill(21)}{dd}"
    else:
        code = f"4{ib[2:]}{euros:06d}{cents:02d}000{ref.zfill(20)}{dd}"
    vb = parse(code)                                    # defensive round-trip
    assert (vb.iban, vb.amount, vb.reference, vb.due_date) == (ib, amt, ref, d), "round-trip mismatch"
    return code


SPEC_OPTS = dict(module_width=0.30, module_height=12.0, quiet_zone=20.0,
                 text_distance=3, dpi=600)


def render_image(vb: VirtualBarcode, with_text: bool = False):
    """Render to an in-memory PIL image at 600 dpi (spec-compliant size)."""
    from barcode import Code128
    from barcode.writer import ImageWriter
    bc = Code128(vb.raw, writer=ImageWriter())
    if bc._build()[0] != 105:
        raise VirtualBarcodeError("Encoder did not select Code 128 set C.")
    opts = dict(SPEC_OPTS, write_text=with_text, font_size=8 if with_text else 0)
    return bc.render(opts)


# Lookahead => overlapping matches: a stray digit before the code can't swallow it.
CODE_RE = re.compile(r"(?<!\d)(?=([45](?:[\s\-]*\d){53})(?!\d))")


def iter_codes(text: str):
    """Yield every valid VirtualBarcode found in free text (deduplicated, in order)."""
    seen = set()
    for m in CODE_RE.finditer(text):
        cand = re.sub(r"[\s\-]", "", m.group(1))
        if cand in seen:
            continue
        seen.add(cand)
        try:
            yield parse(cand)
        except VirtualBarcodeError:
            continue


def extract_code(text: str) -> str | None:
    """Find a 54-digit virtual barcode inside arbitrary pasted text
    (e.g. a whole invoice or e-mail). Spaces/dashes inside the code are allowed."""
    return next((vb.raw for vb in iter_codes(text)), None)


# ---------------------------------------------------------------- CSV export
CSV_FIELDS = ["source", "code", "version", "reference_type", "iban", "amount_eur",
              "reference", "due_date", "days_overdue", "decoded_at"]


def to_record(vb: VirtualBarcode, source: str = "", today: date | None = None) -> dict:
    today = today or date.today()
    overdue = (today - vb.due_date).days if vb.due_date else ""
    return {
        "source": source,
        "code": vb.raw,
        "version": vb.version,
        "reference_type": "RF" if vb.version == 5 else "FI",
        "iban": vb.iban,
        "amount_eur": f"{vb.amount:.2f}",
        "reference": vb.reference,
        "due_date": vb.due_date.isoformat() if vb.due_date else "",
        "days_overdue": max(overdue, 0) if overdue != "" else "",
        "decoded_at": datetime.now().isoformat(timespec="seconds"),
    }


def _csv_safe(v) -> str:
    """Neutralise spreadsheet formula injection (e.g. a file name starting with '=')."""
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def write_csv(records: list[dict], path: str | Path, excel_fi: bool = False) -> Path:
    """excel_fi=True: ';' separator, decimal comma, UTF-8 BOM - opens cleanly in Finnish Excel.
    Codes are written as text (=\"...\") in Excel mode so the 54 digits don't become 4.6E+53."""
    path = Path(path)
    with open(path, "w", newline="", encoding="utf-8-sig" if excel_fi else "utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, delimiter=";" if excel_fi else ",")
        w.writeheader()
        for r in records:
            row = {k: _csv_safe(r.get(k, "")) for k in CSV_FIELDS}
            if excel_fi:
                row["amount_eur"] = row["amount_eur"].replace(".", ",")
                row["code"] = f'="{r["code"]}"'
            w.writerow(row)
    return path


# ---------------------------------------------------------------- reading images / PDFs
@dataclass
class ScanResult:
    source: str
    codes: list = field(default_factory=list)        # list[VirtualBarcode]
    rejected: list = field(default_factory=list)     # barcode/text found but not a valid code
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return f"{self.source}: {self.error}"
        parts = [f"{len(self.codes)} code(s)"]
        if self.rejected:
            parts.append(f"{len(self.rejected)} other barcode(s) ignored")
        return f"{self.source}: " + ", ".join(parts)


def _zbar():
    try:
        from pyzbar.pyzbar import decode, ZBarSymbol
        return decode, ZBarSymbol
    except Exception as e:  # noqa: BLE001  (ImportError or missing libzbar)
        raise VirtualBarcodeError(
            "Barcode reading needs pyzbar + zbar: pip install pyzbar "
            "(macOS: brew install zbar, Debian/Ubuntu: apt install libzbar0)") from e


def _variants(img):
    """Yield progressively more aggressive preprocessing of a photo/scan."""
    from PIL import Image, ImageFilter, ImageOps
    g = ImageOps.exif_transpose(img).convert("L")
    yield g
    ac = ImageOps.autocontrast(g, cutoff=2)
    yield ac
    if max(g.size) < 1600:
        yield ac.resize((g.width * 2, g.height * 2), Image.LANCZOS)
    if max(g.size) > 4000:
        yield ac.resize((g.width // 2, g.height // 2), Image.LANCZOS)
    yield ac.filter(ImageFilter.SHARPEN)
    for k in (3, 5):                                    # dust / scanner speckle / fax noise
        med_img = ImageOps.autocontrast(g.filter(ImageFilter.MedianFilter(k)), cutoff=2)
        yield med_img
        if max(g.size) < 1600:
            yield med_img.resize((g.width * 2, g.height * 2), Image.LANCZOS)
    hist = ac.histogram(); total = sum(hist); acc = 0; med = 128
    for i, h in enumerate(hist):
        acc += h
        if acc >= total / 2:
            med = i; break
    yield ac.point(lambda p: 255 if p > med else 0)
    for angle in (90, 270, 180):
        yield ac.rotate(angle, expand=True)
    for angle in (-4, 4, -8, 8, -12, 12, -20, 20, -30, 30):   # tilted photos
        yield ac.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC)


def read_image(img, source: str = "image") -> ScanResult:
    """Decode every Code 128 in a PIL image; validate each as a virtual barcode."""
    decode, ZBarSymbol = _zbar()
    res = ScanResult(source)
    seen = set()
    for v in _variants(img):
        for sym in decode(v, symbols=[ZBarSymbol.CODE128]):
            data = sym.data.decode("ascii", "replace")
            if data in seen:
                continue
            seen.add(data)
            try:
                res.codes.append(parse(data))
            except VirtualBarcodeError as e:
                res.rejected.append(f"{data[:60]} ({e})")
        if res.codes:
            break
    if not res.codes and not res.rejected:
        res.error = "no barcode found"
    return res


def read_file(path: str | Path) -> ScanResult:
    """Read an image (png/jpg/...) or a PDF invoice. For PDFs, both the text layer
    (printed virtual code) and the rendered pages (barcode graphic) are checked."""
    path = Path(path)
    res = ScanResult(path.name)
    try:
        if path.suffix.lower() == ".pdf":
            try:
                import pypdfium2 as pdfium
            except ImportError:
                res.error = "PDF support needs: pip install pypdfium2"; return res
            pdf = pdfium.PdfDocument(str(path))
            found: dict[str, VirtualBarcode] = {}
            try:
                for i in range(len(pdf)):
                    page = pdf[i]
                    try:
                        tp = page.get_textpage()
                        try:
                            text = tp.get_text_range()
                        finally:
                            tp.close()
                        for vb in iter_codes(text):
                            found.setdefault(vb.raw, vb)
                        bitmap = page.render(scale=300 / 72)
                        try:
                            pil = bitmap.to_pil().copy()
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
                    try:
                        r = read_image(pil, path.name)
                        for vb in r.codes:
                            found.setdefault(vb.raw, vb)
                        res.rejected += r.rejected
                    except VirtualBarcodeError as e:     # no zbar: text layer still works
                        if not found:
                            res.error = str(e)
            finally:
                pdf.close()
            res.codes = list(found.values())
            if res.codes:
                res.error = ""
            elif not res.error and not res.rejected:
                res.error = "no virtual barcode found (text or barcode)"
            return res
        from PIL import Image, UnidentifiedImageError
        try:
            img = Image.open(path); img.load()
        except (UnidentifiedImageError, OSError) as e:
            res.error = f"not a readable image ({e.__class__.__name__})"; return res
        r = read_image(img, path.name)
        return r
    except VirtualBarcodeError as e:
        res.error = str(e); return res


def render(vb: VirtualBarcode, out_stem: str | Path, formats=("png", "svg"),
           with_text: bool = False) -> list[Path]:
    """Render as Code 128 set C. Returns paths written."""
    from barcode import Code128
    from barcode.writer import ImageWriter, SVGWriter

    # Finanssiala Pankkiviivakoodi-opas v5.3: width 70-105 mm (bars only),
    # height 10-12.7 mm, 20 mm quiet zones, no human-readable text by the symbol.
    # 54 digits -> 27 set-C chars: 11 + 27*11 + 11 + 13 = 332 modules * 0.30 mm = 99.6 mm.
    opts = dict(SPEC_OPTS, write_text=with_text, font_size=8 if with_text else 0)
    written = []
    for fmt in formats:
        writer = ImageWriter() if fmt == "png" else SVGWriter()
        bc = Code128(vb.raw, writer=writer)
        # 54 digits must encode as pure set C (start code 105) - sanity check
        if bc._build()[0] != 105:
            raise VirtualBarcodeError("Encoder did not select Code 128 set C.")
        written.append(Path(bc.save(str(out_stem), opts)))
    return written


def code128c_checksum(digits: str) -> int:
    """Tarkiste 2 per spec: (105 + sum(weight_i * pair_i)) mod 103."""
    pairs = [int(digits[i:i + 2]) for i in range(0, len(digits), 2)]
    return (105 + sum(w * v for w, v in enumerate(pairs, 1))) % 103


def verify_png(png: Path, expected: str) -> bool:
    """Decode the PNG back with zbar and compare."""
    from PIL import Image
    from pyzbar.pyzbar import decode
    res = decode(Image.open(png))
    return len(res) == 1 and res[0].type == "CODE128" and res[0].data.decode() == expected





# ---------------------------------------------------------------- settings + components
def _config_path() -> Path:
    """%APPDATA%\\vcode2bar on Windows, ~/Library/Application Support on macOS,
    $XDG_CONFIG_HOME or ~/.config/vcode2bar on Linux and WSL."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "vcode2bar" / "settings.json"


CONFIG_PATH = _config_path()
LEGACY_CONFIG_PATH = Path.home() / ".vcode2bar.json"      # v1.0 location, read as fallback


def load_config() -> dict:
    for path in (CONFIG_PATH, LEGACY_CONFIG_PATH):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- Windows Subsystem for Linux
def _detect_wsl() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False


IS_WSL = _detect_wsl()
_WIN_CWD = "/mnt/c" if Path("/mnt/c").is_dir() else None   # Windows tools dislike a Linux (UNC) cwd


def wsl_to_windows_path(p) -> str:
    return subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True,
                          check=True, timeout=10).stdout.strip()


def wsl_to_linux_path(p) -> str:
    return subprocess.run(["wslpath", "-u", str(p)], capture_output=True, text=True,
                          check=True, timeout=10).stdout.strip()


@functools.lru_cache(maxsize=None)
def wsl_windows_env(var: str) -> str | None:
    """A Windows environment variable (e.g. USERPROFILE) as a Linux path, or None."""
    try:
        out = subprocess.run(["cmd.exe", "/c", f"echo %{var}%"], capture_output=True, text=True,
                             timeout=10, cwd=_WIN_CWD).stdout.strip().splitlines()
        val = out[-1].strip() if out else ""
        if not val or val == f"%{var}%":
            return None
        return wsl_to_linux_path(val) or None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def wsl_default_dir() -> Path | None:
    """Windows Downloads (or profile) folder, so saved files are easy to find from Windows."""
    prof = wsl_windows_env("USERPROFILE")
    if not prof:
        return None
    for cand in (Path(prof) / "Downloads", Path(prof)):
        if cand.is_dir():
            return cand
    return None


def wsl_open(path) -> None:
    """Open a file with its Windows default application (PDF viewer for printing)."""
    if shutil.which("wslview"):
        subprocess.Popen(["wslview", str(path)])
    else:
        subprocess.Popen(["explorer.exe", wsl_to_windows_path(path)], cwd=_WIN_CWD)


_PS_CLIPBOARD = (
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
    "$i=[System.Windows.Forms.Clipboard]::GetImage();"
    "if($i -ne $null){$i.Save('{PATH}',[System.Drawing.Imaging.ImageFormat]::Png);'IMAGE';exit 0};"
    "$f=[System.Windows.Forms.Clipboard]::GetFileDropList();"
    "if($f.Count -gt 0){foreach($x in $f){'FILE:'+$x};exit 0};"
    "exit 3")


def wsl_clipboard():
    """Read the *Windows* clipboard through PowerShell interop. WSLg's own clipboard sync hands
    images to Linux only as an old BMP variant and is known to be unreliable.
    Returns ("image", PIL.Image) | ("files", [linux paths]) | None."""
    ps = shutil.which("powershell.exe")
    if not ps:
        return None
    tmp = Path(tempfile.gettempdir()) / f"vcode2bar_clip_{os.getpid()}.png"
    tmp.unlink(missing_ok=True)
    script = _PS_CLIPBOARD.replace("{PATH}", wsl_to_windows_path(tmp).replace("'", "''"))
    r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
                       capture_output=True, text=True, timeout=30, cwd=_WIN_CWD)
    if r.returncode != 0:
        return None
    lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
    if "IMAGE" in lines and tmp.exists():
        img = Image.open(tmp); img.load()
        tmp.unlink(missing_ok=True)
        return ("image", img)
    files = [wsl_to_linux_path(x[5:]) for x in lines if x.startswith("FILE:")]
    return ("files", files) if files else None


def component_status() -> list[tuple[str, bool, str]]:
    """(component, available, detail/install hint) for the optional parts."""
    out = []
    try:
        import barcode as _bc
        out.append(("python-barcode", True, getattr(_bc, "version", "")))
    except ImportError:
        out.append(("python-barcode", False, "pip install python-barcode"))
    import PIL
    out.append(("Pillow", True, PIL.__version__))
    out.append(("tkinter (GUI)", tk is not None,
                "" if tk else "Debian/Ubuntu: apt install python3-tk"))
    out.append(("pyzbar + zbar (read images, read-back check)", _zbar_decode is not None,
                "" if _zbar_decode else "pip install pyzbar  (macOS: brew install zbar; "
                                        "Ubuntu 24.04: apt install libzbar0t64, 22.04: libzbar0)"))
    import importlib.util
    has_pdf = importlib.util.find_spec("pypdfium2") is not None
    out.append(("pypdfium2 (read PDF invoices)", has_pdf, "" if has_pdf else "pip install pypdfium2"))
    if sys.platform in ("win32", "darwin"):
        out.append(("clipboard images", True, "built in"))
    elif IS_WSL:
        ps = shutil.which("powershell.exe")
        out.append(("clipboard images (WSL: Windows clipboard via PowerShell)", bool(ps),
                    "" if ps else "Windows interop disabled? check [interop] in /etc/wsl.conf"))
        view = shutil.which("wslview") or shutil.which("explorer.exe")
        out.append(("print / open PDF (WSL: Windows default app)", bool(view),
                    view or "Windows interop disabled? check [interop] in /etc/wsl.conf"))
    else:
        tool = shutil.which("wl-paste") or shutil.which("xclip")
        out.append(("clipboard images", bool(tool), tool or "apt install xclip (X11) or wl-clipboard (Wayland)"))
    return out


# =====================================================================================
# User-interface strings (English is the default; Finnish, Swedish, Norwegian Bokmål)
# =====================================================================================
LANGUAGES = {"en": "English", "fi": "Suomi", "sv": "Svenska", "nb": "Norsk (bokmål)"}

T = {'en': {'title': 'vcode2bar – virtual barcode ↔ barcode',
        'paste_hint': 'Paste the virtual barcode (or the whole invoice text):',
        'paste': 'Paste',
        'clear': 'Clear',
        'digits': '{n}/54 digits',
        'empty': 'Waiting for input…',
        'found': 'code found in pasted text',
        'ok': '✓ Valid virtual barcode',
        'notfound': '✗ No valid 54-digit code found',
        'version': 'Version',
        'account': 'Account (IBAN)',
        'amount': 'Amount',
        'reference': 'Reference',
        'due': 'Due date',
        'code': 'Code',
        'none': 'not given',
        'overdue': '  ⚠ overdue ({d} days)',
        'today': '  ⚠ due today',
        'copy': 'Copy',
        'copied': 'Copied: {w}',
        'show_text': 'Print digits under bars (spec: off)',
        'save_png': 'Save PNG',
        'save_svg': 'Save SVG',
        'save_pdf': 'Save PDF (A4)',
        'print_': 'Print…',
        'verify_ok': '✓ Read-back check: image decodes to the same code',
        'verify_fail': '✗ Read-back check FAILED – do not use this image',
        'verify_na': 'Read-back check unavailable (pip install pyzbar)',
        'saved': 'Saved: {p}',
        'save_err': 'Could not save',
        'nothing': 'Nothing to save yet.',
        'pdf_head': 'Payment details',
        'pdf_note': 'Barcode per Finanssiala Pankkiviivakoodi-opas v5.3',
        'rf_nonnum': 'RF reference is not numeric – see invoice',
        'amount_open': 'open (payer decides)',
        'load': 'Open image/PDF…',
        'paste_img': 'Paste image',
        'tab_main': 'Barcode',
        'tab_list': 'List ({n})',
        'add': 'Add to list',
        'csv_one': 'Export CSV…',
        'csv_all': 'Export list to CSV…',
        'remove': 'Remove',
        'clear_list': 'Clear list',
        'to_editor': 'Show in editor',
        'csv_fmt': 'CSV format',
        'fmt_excel': 'Excel ;',
        'fmt_std': 'Standard ,',
        'manual': 'typed/pasted',
        'added': 'Added to list ({n})',
        'dup': 'Already in list',
        'no_img': 'No image on the clipboard',
        'img_err': 'Could not read clipboard image: {e}',
        'read_title': 'Read result',
        'from_src': '  —  from {s}',
        'list_empty': 'List is empty.',
        'exported': 'Exported {n} row(s): {p}',
        'col_source': 'Source',
        'col_iban': 'IBAN',
        'col_amount': 'Amount',
        'col_ref': 'Reference',
        'col_due': 'Due',
        'img_files': 'Images and PDFs',
        'm_file': 'File',
        'm_edit': 'Edit',
        'm_view': 'View',
        'm_lang': 'Language',
        'm_help': 'Help',
        'exit': 'Exit',
        'paste_code': 'Paste code',
        'copy_iban': 'Copy IBAN',
        'copy_amount': 'Copy amount',
        'copy_ref': 'Copy reference',
        'copy_code': 'Copy code',
        'view_main': 'Barcode tab',
        'view_list': 'List tab',
        'guide': 'User guide',
        'shortcuts': 'Keyboard shortcuts',
        'format_info': 'Virtual barcode format',
        'deps': 'Check components',
        'about': 'About vcode2bar',
        'close': 'Close',
        'ok_mark': 'installed',
        'missing': 'missing',
        'guide_text': '1. Paste the 54-digit virtual barcode – or a whole invoice text or e-mail – into the '
                      'input field. The code is found and validated automatically.\n'
                      '\n'
                      '2. The decoded fields and the barcode appear immediately. An amber due date means the '
                      'invoice is overdue or due today.\n'
                      '\n'
                      '3. Save the barcode as PNG, SVG or a print-ready A4 PDF, or print it (File menu).\n'
                      '\n'
                      '4. To read an existing barcode: File → Open image/PDF (photos, scans, screenshots, '
                      'invoice PDFs), or copy an image and use File → Paste image.\n'
                      '\n'
                      "5. Collect codes on the List tab and export them to CSV. The Excel format uses ';' "
                      'and a decimal comma so it opens cleanly in Nordic Excel.\n'
                      '\n'
                      '6. To create a code yourself use the Create tab (File → New from form…): enter the '
                      "IBAN, amount, reference and due date. 'Add check digit' turns a reference base into a "
                      "valid Finnish reference, 'Convert to RF' makes an RF reference. 'Show barcode' opens "
                      'the result on the Barcode tab for saving or printing.\n'
                      '\n'
                      "The barcode follows the Finnish banks' specification (Finanssiala, "
                      'Pankkiviivakoodi-opas v5.3): Code 128 set C, no digits printed under the bars. '
                      'Everything runs locally – no data is sent anywhere.',
        'shortcut_rows': [('Ctrl+N', 'New from form'),
                          ('Ctrl+O', 'Open image/PDF'),
                          ('Ctrl+Shift+V', 'Paste image'),
                          ('Ctrl+S', 'Save PNG'),
                          ('Ctrl+Shift+S', 'Save SVG'),
                          ('Ctrl+P', 'Print'),
                          ('Ctrl+L', 'Add to list'),
                          ('Ctrl+E', 'Export list to CSV'),
                          ('Ctrl+1 / 2 / 3', 'Barcode / Create / List tab'),
                          ('Esc', 'Clear input'),
                          ('F1', 'User guide'),
                          ('Ctrl+Q', 'Exit')],
        'format_rows': [('1', 'Version: 4 = Finnish reference, 5 = RF reference'),
                        ('2–17', "Account: IBAN without 'FI' (16 digits)"),
                        ('18–23', 'Euros (6 digits)'),
                        ('24–25', 'Cents (2 digits)'),
                        ('26–28', 'v4: reserved, always 000'),
                        ('29–48', 'v4: reference, zero-padded (20 digits)'),
                        ('26–27', 'v5: RF check digits'),
                        ('28–48', 'v5: RF reference body, zero-padded (21 digits)'),
                        ('49–54', 'Due date YYMMDD (000000 = not given)')],
        'format_note': 'Amount 000000 00 = the payer enters the amount. Checks performed: IBAN mod 97, '
                       'reference check digit (7-3-1 or RF mod 97), valid date, Code 128 checksum.',
        'about_text': 'Finnish virtual barcode ↔ bank barcode (Code 128 set C).\n'
                      '\n'
                      'Specification: Finanssiala – Pankkiviivakoodi-opas v5.3\n'
                      'License: MIT',
        'tab_create': 'Create',
        'create_intro': 'Fill in the payment details – the virtual barcode is created as you type.',
        'f_iban': 'Account (IBAN)',
        'f_amount': 'Amount (€)',
        'f_ref': 'Reference',
        'f_due': 'Due date',
        'h_amount': 'empty = payer enters the amount',
        'h_due': 'dd.mm.yyyy, empty = none',
        'h_ref': 'Finnish or RF reference',
        'btn_check': 'Add check digit',
        'btn_rf': 'Convert to RF',
        'f_result': 'Virtual barcode',
        'show_bar': 'Show barcode',
        'clear_form': 'Clear form',
        'form_ok': '✓ Virtual barcode created',
        'form_wait': 'Fill in the account and reference',
        'err_iban': 'Enter a valid Finnish IBAN (FI + 16 digits).',
        'err_amount': 'Amount must be 0 – 999 999,99 with at most 2 decimals.',
        'err_reference': 'Enter a valid Finnish reference (4–20 digits incl. check digit) or a numeric RF '
                         'reference.',
        'err_due': 'Due date must be dd.mm.yyyy between 2000 and 2099.',
        'err_base': 'Type 3–19 digits first; the check digit is added.',
        'render_err': '✗ Could not render the barcode: {e}',
        'verify_err': 'Read-back check error: {e}',
        'new_form': 'New from form…',
        'unexpected': 'Unexpected error'},
 'fi': {'title': 'vcode2bar – virtuaaliviivakoodi ↔ viivakoodi',
        'paste_hint': 'Liitä virtuaaliviivakoodi (tai koko laskun teksti):',
        'paste': 'Liitä',
        'clear': 'Tyhjennä',
        'digits': '{n}/54 numeroa',
        'empty': 'Odotetaan syötettä…',
        'found': 'koodi löytyi liitetystä tekstistä',
        'ok': '✓ Kelvollinen virtuaaliviivakoodi',
        'notfound': '✗ Kelvollista 54-numeroista koodia ei löytynyt',
        'version': 'Versio',
        'account': 'Tili (IBAN)',
        'amount': 'Summa',
        'reference': 'Viite',
        'due': 'Eräpäivä',
        'code': 'Koodi',
        'none': 'ei annettu',
        'overdue': '  ⚠ myöhässä ({d} pv)',
        'today': '  ⚠ erääntyy tänään',
        'copy': 'Kopioi',
        'copied': 'Kopioitu: {w}',
        'show_text': 'Numerot viivojen alle (standardi: ei)',
        'save_png': 'Tallenna PNG',
        'save_svg': 'Tallenna SVG',
        'save_pdf': 'Tallenna PDF (A4)',
        'print_': 'Tulosta…',
        'verify_ok': '✓ Takaisinluku: kuva lukee samaksi koodiksi',
        'verify_fail': '✗ Takaisinluku EPÄONNISTUI – älä käytä kuvaa',
        'verify_na': 'Takaisinluku ei käytettävissä (pip install pyzbar)',
        'saved': 'Tallennettu: {p}',
        'save_err': 'Tallennus epäonnistui',
        'nothing': 'Ei vielä tallennettavaa.',
        'pdf_head': 'Maksutiedot',
        'pdf_note': 'Viivakoodi Finanssialan Pankkiviivakoodi-oppaan v5.3 mukaan',
        'rf_nonnum': 'RF-viite ei numeerinen – katso lasku',
        'amount_open': 'avoin (maksaja päättää)',
        'load': 'Avaa kuva/PDF…',
        'paste_img': 'Liitä kuva',
        'tab_main': 'Viivakoodi',
        'tab_list': 'Lista ({n})',
        'add': 'Lisää listaan',
        'csv_one': 'Vie CSV…',
        'csv_all': 'Vie lista CSV:ksi…',
        'remove': 'Poista',
        'clear_list': 'Tyhjennä lista',
        'to_editor': 'Näytä editorissa',
        'csv_fmt': 'CSV-muoto',
        'fmt_excel': 'Excel ;',
        'fmt_std': 'Standardi ,',
        'manual': 'kirjoitettu/liitetty',
        'added': 'Lisätty listaan ({n})',
        'dup': 'On jo listalla',
        'no_img': 'Leikepöydällä ei ole kuvaa',
        'img_err': 'Leikepöydän kuvaa ei voitu lukea: {e}',
        'read_title': 'Lukutulos',
        'from_src': '  —  lähde: {s}',
        'list_empty': 'Lista on tyhjä.',
        'exported': 'Viety {n} riviä: {p}',
        'col_source': 'Lähde',
        'col_iban': 'IBAN',
        'col_amount': 'Summa',
        'col_ref': 'Viite',
        'col_due': 'Eräpäivä',
        'img_files': 'Kuvat ja PDF:t',
        'm_file': 'Tiedosto',
        'm_edit': 'Muokkaa',
        'm_view': 'Näytä',
        'm_lang': 'Kieli (Language)',
        'm_help': 'Ohje',
        'exit': 'Lopeta',
        'paste_code': 'Liitä koodi',
        'copy_iban': 'Kopioi IBAN',
        'copy_amount': 'Kopioi summa',
        'copy_ref': 'Kopioi viite',
        'copy_code': 'Kopioi koodi',
        'view_main': 'Viivakoodi-välilehti',
        'view_list': 'Lista-välilehti',
        'guide': 'Käyttöohje',
        'shortcuts': 'Pikanäppäimet',
        'format_info': 'Virtuaaliviivakoodin rakenne',
        'deps': 'Tarkista komponentit',
        'about': 'Tietoja vcode2barista',
        'close': 'Sulje',
        'ok_mark': 'asennettu',
        'missing': 'puuttuu',
        'guide_text': '1. Liitä 54-numeroinen virtuaaliviivakoodi – tai koko laskun teksti tai sähköposti – '
                      'syöttökenttään. Koodi etsitään ja tarkistetaan automaattisesti.\n'
                      '\n'
                      '2. Puretut tiedot ja viivakoodi näkyvät heti. Oranssi eräpäivä tarkoittaa, että lasku '
                      'on myöhässä tai erääntyy tänään.\n'
                      '\n'
                      '3. Tallenna viivakoodi PNG-, SVG- tai tulostusvalmiina A4-PDF-tiedostona tai tulosta '
                      'se (Tiedosto-valikko).\n'
                      '\n'
                      '4. Olemassa olevan viivakoodin lukeminen: Tiedosto → Avaa kuva/PDF (valokuvat, '
                      'skannaukset, kuvakaappaukset, lasku-PDF:t) tai kopioi kuva ja valitse Tiedosto → '
                      'Liitä kuva.\n'
                      '\n'
                      '5. Kerää koodit Lista-välilehdelle ja vie ne CSV-tiedostoon. Excel-muoto käyttää '
                      "';'-erotinta ja desimaalipilkkua, joten se aukeaa suoraan suomenkielisessä "
                      'Excelissä.\n'
                      '\n'
                      '6. Oman koodin voit luoda Luo-välilehdellä (Tiedosto → Uusi lomakkeelta…): anna IBAN, '
                      "summa, viite ja eräpäivä. 'Lisää tarkiste' tekee viitteen perusosasta kelvollisen "
                      "viitteen, 'Muunna RF-viitteeksi' tekee RF-viitteen. 'Näytä viivakoodi' avaa tuloksen "
                      'Viivakoodi-välilehdelle tallennettavaksi tai tulostettavaksi.\n'
                      '\n'
                      'Viivakoodi noudattaa pankkien määritystä (Finanssiala, Pankkiviivakoodi-opas v5.3): '
                      'Code 128 C-merkistö, ei numeroita viivojen alla. Kaikki toimii paikallisesti – '
                      'tietoja ei lähetetä minnekään.',
        'shortcut_rows': [('Ctrl+N', 'Uusi lomakkeelta'),
                          ('Ctrl+O', 'Avaa kuva/PDF'),
                          ('Ctrl+Shift+V', 'Liitä kuva'),
                          ('Ctrl+S', 'Tallenna PNG'),
                          ('Ctrl+Shift+S', 'Tallenna SVG'),
                          ('Ctrl+P', 'Tulosta'),
                          ('Ctrl+L', 'Lisää listaan'),
                          ('Ctrl+E', 'Vie lista CSV:ksi'),
                          ('Ctrl+1 / 2 / 3', 'Viivakoodi- / Luo- / Lista-välilehti'),
                          ('Esc', 'Tyhjennä syöte'),
                          ('F1', 'Käyttöohje'),
                          ('Ctrl+Q', 'Lopeta')],
        'format_rows': [('1', 'Versio: 4 = kotimainen viite, 5 = RF-viite'),
                        ('2–17', "Tili: IBAN ilman 'FI' (16 numeroa)"),
                        ('18–23', 'Eurot (6 numeroa)'),
                        ('24–25', 'Sentit (2 numeroa)'),
                        ('26–28', 'v4: varalla, aina 000'),
                        ('29–48', 'v4: viite etunollin (20 numeroa)'),
                        ('26–27', 'v5: RF-tarkiste'),
                        ('28–48', 'v5: RF-viitteen runko etunollin (21 numeroa)'),
                        ('49–54', 'Eräpäivä VVKKPP (000000 = ei annettu)')],
        'format_note': 'Summa 000000 00 = maksaja syöttää summan. Tarkistukset: IBAN mod 97, viitteen '
                       'tarkiste (7-3-1 tai RF mod 97), kelvollinen päivämäärä, Code 128 -tarkiste.',
        'about_text': 'Virtuaaliviivakoodi ↔ pankkiviivakoodi (Code 128, C-merkistö).\n'
                      '\n'
                      'Määritys: Finanssiala – Pankkiviivakoodi-opas v5.3\n'
                      'Lisenssi: MIT',
        'tab_create': 'Luo',
        'create_intro': 'Täytä maksutiedot – virtuaaliviivakoodi muodostuu kirjoittaessasi.',
        'f_iban': 'Tili (IBAN)',
        'f_amount': 'Summa (€)',
        'f_ref': 'Viite',
        'f_due': 'Eräpäivä',
        'h_amount': 'tyhjä = maksaja syöttää summan',
        'h_due': 'pp.kk.vvvv, tyhjä = ei eräpäivää',
        'h_ref': 'kotimainen tai RF-viite',
        'btn_check': 'Lisää tarkiste',
        'btn_rf': 'Muunna RF-viitteeksi',
        'f_result': 'Virtuaaliviivakoodi',
        'show_bar': 'Näytä viivakoodi',
        'clear_form': 'Tyhjennä lomake',
        'form_ok': '✓ Virtuaaliviivakoodi muodostettu',
        'form_wait': 'Täytä tili ja viite',
        'err_iban': 'Anna kelvollinen suomalainen IBAN (FI + 16 numeroa).',
        'err_amount': 'Summan on oltava 0 – 999 999,99, enintään 2 desimaalia.',
        'err_reference': 'Anna kelvollinen kotimainen viite (4–20 numeroa tarkisteineen) tai numeerinen '
                         'RF-viite.',
        'err_due': 'Eräpäivän on oltava muotoa pp.kk.vvvv vuosilta 2000–2099.',
        'err_base': 'Kirjoita ensin 3–19 numeroa; tarkiste lisätään.',
        'render_err': '✗ Viivakoodin piirtäminen epäonnistui: {e}',
        'verify_err': 'Takaisinlukuvirhe: {e}',
        'new_form': 'Uusi lomakkeelta…',
        'unexpected': 'Odottamaton virhe'},
 'sv': {'title': 'vcode2bar – virtuell streckkod ↔ streckkod',
        'paste_hint': 'Klistra in den virtuella streckkoden (eller hela fakturatexten):',
        'paste': 'Klistra in',
        'clear': 'Rensa',
        'digits': '{n}/54 siffror',
        'empty': 'Väntar på inmatning…',
        'found': 'koden hittades i den inklistrade texten',
        'ok': '✓ Giltig virtuell streckkod',
        'notfound': '✗ Ingen giltig 54-siffrig kod hittades',
        'version': 'Version',
        'account': 'Konto (IBAN)',
        'amount': 'Belopp',
        'reference': 'Referens',
        'due': 'Förfallodag',
        'code': 'Kod',
        'none': 'inte angiven',
        'overdue': '  ⚠ försenad ({d} dagar)',
        'today': '  ⚠ förfaller i dag',
        'copy': 'Kopiera',
        'copied': 'Kopierat: {w}',
        'show_text': 'Siffror under strecken (standard: av)',
        'save_png': 'Spara PNG',
        'save_svg': 'Spara SVG',
        'save_pdf': 'Spara PDF (A4)',
        'print_': 'Skriv ut…',
        'verify_ok': '✓ Kontrolläsning: bilden avkodas till samma kod',
        'verify_fail': '✗ Kontrolläsning MISSLYCKADES – använd inte bilden',
        'verify_na': 'Kontrolläsning ej tillgänglig (pip install pyzbar)',
        'saved': 'Sparat: {p}',
        'save_err': 'Kunde inte spara',
        'nothing': 'Inget att spara ännu.',
        'pdf_head': 'Betalningsuppgifter',
        'pdf_note': 'Streckkod enligt Finanssiala Pankkiviivakoodi-opas v5.3',
        'rf_nonnum': 'RF-referensen är inte numerisk – se fakturan',
        'amount_open': 'öppet (betalaren anger)',
        'load': 'Öppna bild/PDF…',
        'paste_img': 'Klistra in bild',
        'tab_main': 'Streckkod',
        'tab_list': 'Lista ({n})',
        'add': 'Lägg till i listan',
        'csv_one': 'Exportera CSV…',
        'csv_all': 'Exportera listan till CSV…',
        'remove': 'Ta bort',
        'clear_list': 'Töm listan',
        'to_editor': 'Visa i redigeraren',
        'csv_fmt': 'CSV-format',
        'fmt_excel': 'Excel ;',
        'fmt_std': 'Standard ,',
        'manual': 'inskriven/inklistrad',
        'added': 'Tillagd i listan ({n})',
        'dup': 'Finns redan i listan',
        'no_img': 'Ingen bild i urklipp',
        'img_err': 'Kunde inte läsa bilden i urklipp: {e}',
        'read_title': 'Läsresultat',
        'from_src': '  —  från {s}',
        'list_empty': 'Listan är tom.',
        'exported': 'Exporterade {n} rad(er): {p}',
        'col_source': 'Källa',
        'col_iban': 'IBAN',
        'col_amount': 'Belopp',
        'col_ref': 'Referens',
        'col_due': 'Förfaller',
        'img_files': 'Bilder och PDF-filer',
        'm_file': 'Arkiv',
        'm_edit': 'Redigera',
        'm_view': 'Visa',
        'm_lang': 'Språk (Language)',
        'm_help': 'Hjälp',
        'exit': 'Avsluta',
        'paste_code': 'Klistra in kod',
        'copy_iban': 'Kopiera IBAN',
        'copy_amount': 'Kopiera belopp',
        'copy_ref': 'Kopiera referens',
        'copy_code': 'Kopiera kod',
        'view_main': 'Fliken Streckkod',
        'view_list': 'Fliken Lista',
        'guide': 'Användarguide',
        'shortcuts': 'Kortkommandon',
        'format_info': 'Den virtuella streckkodens format',
        'deps': 'Kontrollera komponenter',
        'about': 'Om vcode2bar',
        'close': 'Stäng',
        'ok_mark': 'installerad',
        'missing': 'saknas',
        'guide_text': '1. Klistra in den 54-siffriga virtuella streckkoden – eller hela fakturatexten eller '
                      'ett e-postmeddelande – i inmatningsfältet. Koden hittas och kontrolleras '
                      'automatiskt.\n'
                      '\n'
                      '2. De avkodade uppgifterna och streckkoden visas direkt. En orange förfallodag '
                      'betyder att fakturan är försenad eller förfaller i dag.\n'
                      '\n'
                      '3. Spara streckkoden som PNG, SVG eller utskriftsfärdig A4-PDF, eller skriv ut den '
                      '(menyn Arkiv).\n'
                      '\n'
                      '4. Läsa en befintlig streckkod: Arkiv → Öppna bild/PDF (foton, skanningar, '
                      'skärmbilder, faktura-PDF:er), eller kopiera en bild och välj Arkiv → Klistra in '
                      'bild.\n'
                      '\n'
                      '5. Samla koder på fliken Lista och exportera dem till CSV. Excel-formatet använder '
                      "';' och decimalkomma så att filen öppnas korrekt i nordisk Excel.\n"
                      '\n'
                      '6. Skapa en egen kod på fliken Skapa (Arkiv → Ny från formulär…): ange IBAN, belopp, '
                      "referens och förfallodag. 'Lägg till kontrollsiffra' gör en referensstomme till en "
                      "giltig finsk referens, 'Konvertera till RF' skapar en RF-referens. 'Visa streckkod' "
                      'öppnar resultatet på fliken Streckkod för att sparas eller skrivas ut.\n'
                      '\n'
                      'Streckkoden följer de finländska bankernas specifikation (Finanssiala, '
                      'Pankkiviivakoodi-opas v5.3): Code 128 teckenuppsättning C, inga siffror under '
                      'strecken. Allt körs lokalt – inga uppgifter skickas någonstans.',
        'shortcut_rows': [('Ctrl+N', 'Ny från formulär'),
                          ('Ctrl+O', 'Öppna bild/PDF'),
                          ('Ctrl+Shift+V', 'Klistra in bild'),
                          ('Ctrl+S', 'Spara PNG'),
                          ('Ctrl+Shift+S', 'Spara SVG'),
                          ('Ctrl+P', 'Skriv ut'),
                          ('Ctrl+L', 'Lägg till i listan'),
                          ('Ctrl+E', 'Exportera listan till CSV'),
                          ('Ctrl+1 / 2 / 3', 'Fliken Streckkod / Skapa / Lista'),
                          ('Esc', 'Rensa inmatningen'),
                          ('F1', 'Användarguide'),
                          ('Ctrl+Q', 'Avsluta')],
        'format_rows': [('1', 'Version: 4 = finsk referens, 5 = RF-referens'),
                        ('2–17', "Konto: IBAN utan 'FI' (16 siffror)"),
                        ('18–23', 'Euro (6 siffror)'),
                        ('24–25', 'Cent (2 siffror)'),
                        ('26–28', 'v4: reserverat, alltid 000'),
                        ('29–48', 'v4: referens med inledande nollor (20 siffror)'),
                        ('26–27', 'v5: RF-kontrollsiffror'),
                        ('28–48', 'v5: RF-referensens stomme med inledande nollor (21 siffror)'),
                        ('49–54', 'Förfallodag ÅÅMMDD (000000 = inte angiven)')],
        'format_note': 'Belopp 000000 00 = betalaren anger beloppet. Kontroller: IBAN mod 97, referensens '
                       'kontrollsiffra (7-3-1 eller RF mod 97), giltigt datum, Code 128-kontrollsumma.',
        'about_text': 'Finsk virtuell streckkod ↔ bankstreckkod (Code 128, teckenuppsättning C).\n'
                      '\n'
                      'Specifikation: Finanssiala – Pankkiviivakoodi-opas v5.3\n'
                      'Licens: MIT',
        'tab_create': 'Skapa',
        'create_intro': 'Fyll i betalningsuppgifterna – den virtuella streckkoden skapas medan du skriver.',
        'f_iban': 'Konto (IBAN)',
        'f_amount': 'Belopp (€)',
        'f_ref': 'Referens',
        'f_due': 'Förfallodag',
        'h_amount': 'tomt = betalaren anger beloppet',
        'h_due': 'dd.mm.åååå, tomt = ingen',
        'h_ref': 'finsk referens eller RF-referens',
        'btn_check': 'Lägg till kontrollsiffra',
        'btn_rf': 'Konvertera till RF',
        'f_result': 'Virtuell streckkod',
        'show_bar': 'Visa streckkod',
        'clear_form': 'Töm formuläret',
        'form_ok': '✓ Virtuell streckkod skapad',
        'form_wait': 'Fyll i konto och referens',
        'err_iban': 'Ange ett giltigt finländskt IBAN (FI + 16 siffror).',
        'err_amount': 'Beloppet måste vara 0 – 999 999,99 med högst 2 decimaler.',
        'err_reference': 'Ange en giltig finsk referens (4–20 siffror inkl. kontrollsiffra) eller en '
                         'numerisk RF-referens.',
        'err_due': 'Förfallodagen måste vara dd.mm.åååå mellan 2000 och 2099.',
        'err_base': 'Skriv först 3–19 siffror; kontrollsiffran läggs till.',
        'render_err': '✗ Kunde inte rita streckkoden: {e}',
        'verify_err': 'Fel vid kontrolläsning: {e}',
        'new_form': 'Ny från formulär…',
        'unexpected': 'Oväntat fel'},
 'nb': {'title': 'vcode2bar – virtuell strekkode ↔ strekkode',
        'paste_hint': 'Lim inn den virtuelle strekkoden (eller hele fakturateksten):',
        'paste': 'Lim inn',
        'clear': 'Tøm',
        'digits': '{n}/54 sifre',
        'empty': 'Venter på inndata…',
        'found': 'koden ble funnet i den innlimte teksten',
        'ok': '✓ Gyldig virtuell strekkode',
        'notfound': '✗ Fant ingen gyldig 54-sifret kode',
        'version': 'Versjon',
        'account': 'Konto (IBAN)',
        'amount': 'Beløp',
        'reference': 'Referanse',
        'due': 'Forfallsdato',
        'code': 'Kode',
        'none': 'ikke oppgitt',
        'overdue': '  ⚠ forfalt ({d} dager)',
        'today': '  ⚠ forfaller i dag',
        'copy': 'Kopier',
        'copied': 'Kopiert: {w}',
        'show_text': 'Sifre under strekene (standard: av)',
        'save_png': 'Lagre PNG',
        'save_svg': 'Lagre SVG',
        'save_pdf': 'Lagre PDF (A4)',
        'print_': 'Skriv ut…',
        'verify_ok': '✓ Kontrollesing: bildet dekodes til samme kode',
        'verify_fail': '✗ Kontrollesing MISLYKTES – ikke bruk bildet',
        'verify_na': 'Kontrollesing ikke tilgjengelig (pip install pyzbar)',
        'saved': 'Lagret: {p}',
        'save_err': 'Kunne ikke lagre',
        'nothing': 'Ingenting å lagre ennå.',
        'pdf_head': 'Betalingsopplysninger',
        'pdf_note': 'Strekkode etter Finanssiala Pankkiviivakoodi-opas v5.3',
        'rf_nonnum': 'RF-referansen er ikke numerisk – se fakturaen',
        'amount_open': 'åpent (betaleren angir)',
        'load': 'Åpne bilde/PDF…',
        'paste_img': 'Lim inn bilde',
        'tab_main': 'Strekkode',
        'tab_list': 'Liste ({n})',
        'add': 'Legg til i listen',
        'csv_one': 'Eksporter CSV…',
        'csv_all': 'Eksporter listen til CSV…',
        'remove': 'Fjern',
        'clear_list': 'Tøm listen',
        'to_editor': 'Vis i redigeringsfeltet',
        'csv_fmt': 'CSV-format',
        'fmt_excel': 'Excel ;',
        'fmt_std': 'Standard ,',
        'manual': 'skrevet/limt inn',
        'added': 'Lagt til i listen ({n})',
        'dup': 'Finnes allerede i listen',
        'no_img': 'Ingen bilde på utklippstavlen',
        'img_err': 'Kunne ikke lese bildet på utklippstavlen: {e}',
        'read_title': 'Leseresultat',
        'from_src': '  —  fra {s}',
        'list_empty': 'Listen er tom.',
        'exported': 'Eksporterte {n} rad(er): {p}',
        'col_source': 'Kilde',
        'col_iban': 'IBAN',
        'col_amount': 'Beløp',
        'col_ref': 'Referanse',
        'col_due': 'Forfall',
        'img_files': 'Bilder og PDF-filer',
        'm_file': 'Fil',
        'm_edit': 'Rediger',
        'm_view': 'Vis',
        'm_lang': 'Språk (Language)',
        'm_help': 'Hjelp',
        'exit': 'Avslutt',
        'paste_code': 'Lim inn kode',
        'copy_iban': 'Kopier IBAN',
        'copy_amount': 'Kopier beløp',
        'copy_ref': 'Kopier referanse',
        'copy_code': 'Kopier kode',
        'view_main': 'Fanen Strekkode',
        'view_list': 'Fanen Liste',
        'guide': 'Brukerveiledning',
        'shortcuts': 'Hurtigtaster',
        'format_info': 'Formatet til den virtuelle strekkoden',
        'deps': 'Kontroller komponenter',
        'about': 'Om vcode2bar',
        'close': 'Lukk',
        'ok_mark': 'installert',
        'missing': 'mangler',
        'guide_text': '1. Lim inn den 54-sifrede virtuelle strekkoden – eller hele fakturateksten eller en '
                      'e-post – i inndatafeltet. Koden blir funnet og kontrollert automatisk.\n'
                      '\n'
                      '2. De dekodede opplysningene og strekkoden vises med en gang. En oransje forfallsdato '
                      'betyr at fakturaen er forfalt eller forfaller i dag.\n'
                      '\n'
                      '3. Lagre strekkoden som PNG, SVG eller utskriftsklar A4-PDF, eller skriv den ut '
                      '(Fil-menyen).\n'
                      '\n'
                      '4. Lese en eksisterende strekkode: Fil → Åpne bilde/PDF (bilder, skanninger, '
                      'skjermbilder, faktura-PDF-er), eller kopier et bilde og velg Fil → Lim inn bilde.\n'
                      '\n'
                      "5. Samle koder i fanen Liste og eksporter dem til CSV. Excel-formatet bruker ';' og "
                      'desimalkomma slik at filen åpnes riktig i nordisk Excel.\n'
                      '\n'
                      '6. Lag en egen kode i fanen Opprett (Fil → Ny fra skjema…): oppgi IBAN, beløp, '
                      "referanse og forfallsdato. 'Legg til kontrollsiffer' gjør en referansestamme til en "
                      "gyldig finsk referanse, 'Konverter til RF' lager en RF-referanse. 'Vis strekkode' "
                      'åpner resultatet i fanen Strekkode for lagring eller utskrift.\n'
                      '\n'
                      'Strekkoden følger de finske bankenes spesifikasjon (Finanssiala, '
                      'Pankkiviivakoodi-opas v5.3): Code 128 tegnsett C, ingen sifre under strekene. Alt '
                      'kjører lokalt – ingen opplysninger sendes noe sted.',
        'shortcut_rows': [('Ctrl+N', 'Ny fra skjema'),
                          ('Ctrl+O', 'Åpne bilde/PDF'),
                          ('Ctrl+Shift+V', 'Lim inn bilde'),
                          ('Ctrl+S', 'Lagre PNG'),
                          ('Ctrl+Shift+S', 'Lagre SVG'),
                          ('Ctrl+P', 'Skriv ut'),
                          ('Ctrl+L', 'Legg til i listen'),
                          ('Ctrl+E', 'Eksporter listen til CSV'),
                          ('Ctrl+1 / 2 / 3', 'Fanen Strekkode / Opprett / Liste'),
                          ('Esc', 'Tøm inndata'),
                          ('F1', 'Brukerveiledning'),
                          ('Ctrl+Q', 'Avslutt')],
        'format_rows': [('1', 'Versjon: 4 = finsk referanse, 5 = RF-referanse'),
                        ('2–17', "Konto: IBAN uten 'FI' (16 sifre)"),
                        ('18–23', 'Euro (6 sifre)'),
                        ('24–25', 'Cent (2 sifre)'),
                        ('26–28', 'v4: reservert, alltid 000'),
                        ('29–48', 'v4: referanse med innledende nuller (20 sifre)'),
                        ('26–27', 'v5: RF-kontrollsifre'),
                        ('28–48', 'v5: RF-referansens kropp med innledende nuller (21 sifre)'),
                        ('49–54', 'Forfallsdato ÅÅMMDD (000000 = ikke oppgitt)')],
        'format_note': 'Beløp 000000 00 = betaleren angir beløpet. Kontroller: IBAN mod 97, referansens '
                       'kontrollsiffer (7-3-1 eller RF mod 97), gyldig dato, Code 128-kontrollsum.',
        'about_text': 'Finsk virtuell strekkode ↔ bankstrekkode (Code 128, tegnsett C).\n'
                      '\n'
                      'Spesifikasjon: Finanssiala – Pankkiviivakoodi-opas v5.3\n'
                      'Lisens: MIT',
        'tab_create': 'Opprett',
        'create_intro': 'Fyll inn betalingsopplysningene – den virtuelle strekkoden lages mens du skriver.',
        'f_iban': 'Konto (IBAN)',
        'f_amount': 'Beløp (€)',
        'f_ref': 'Referanse',
        'f_due': 'Forfallsdato',
        'h_amount': 'tomt = betaleren angir beløpet',
        'h_due': 'dd.mm.åååå, tomt = ingen',
        'h_ref': 'finsk referanse eller RF-referanse',
        'btn_check': 'Legg til kontrollsiffer',
        'btn_rf': 'Konverter til RF',
        'f_result': 'Virtuell strekkode',
        'show_bar': 'Vis strekkode',
        'clear_form': 'Tøm skjemaet',
        'form_ok': '✓ Virtuell strekkode opprettet',
        'form_wait': 'Fyll inn konto og referanse',
        'err_iban': 'Oppgi et gyldig finsk IBAN (FI + 16 sifre).',
        'err_amount': 'Beløpet må være 0 – 999 999,99 med høyst 2 desimaler.',
        'err_reference': 'Oppgi en gyldig finsk referanse (4–20 sifre inkl. kontrollsiffer) eller en '
                         'numerisk RF-referanse.',
        'err_due': 'Forfallsdatoen må være dd.mm.åååå mellom 2000 og 2099.',
        'err_base': 'Skriv først 3–19 sifre; kontrollsifferet legges til.',
        'render_err': '✗ Kunne ikke tegne strekkoden: {e}',
        'verify_err': 'Feil ved kontrollesing: {e}',
        'new_form': 'Ny fra skjema…',
        'unexpected': 'Uventet feil'}}


FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]


def _glyph(font, c: str) -> bytes:
    im = Image.new("L", (128, 128), 0)
    ImageDraw.Draw(im).text((8, 8), c, font=font, fill=255)
    return im.tobytes()


def _font_supports(font, chars: str = "äöÅøæ€") -> bool:
    """True if every char has a real glyph, i.e. is non-empty and differs from the missing-glyph box."""
    try:
        notdef, blank = _glyph(font, "\U0010FFFD"), bytes(128 * 128)
        return all(_glyph(font, c) not in (notdef, blank) for c in chars)
    except Exception:  # noqa: BLE001
        return False


def pdf_font(size_px: int):
    """(font, needs_ascii). Prefer a system TrueType font with Finnish letters and €."""
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                f = ImageFont.truetype(path, size_px)
            except OSError:
                continue
            if _font_supports(f):
                return f, False
    try:
        return ImageFont.load_default(size=size_px), True
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default(), True


def ascii_safe(s: str) -> str:
    return (s.replace("€", "EUR").replace("ä", "a").replace("Ä", "A").replace("ö", "o")
             .replace("Ö", "O").replace("å", "a").replace("Å", "A").replace("ø", "o").replace("Ø", "O")
             .replace("æ", "ae").replace("Æ", "AE").replace("–", "-").replace("⚠", "!"))


FIELDS = ("version", "account", "amount", "reference", "due", "code")
GREEN, RED, AMBER, GREY = "#1a7f37", "#c62828", "#b26a00", "#666666"


def fmt_amount(vb: VirtualBarcode, lang: str) -> str:
    if vb.amount == 0:
        return T[lang]["amount_open"]
    s = f"{vb.amount:,.2f}"
    s = s.replace(",", " ").replace(".", ",")  # 1 234,56
    return f"{s} €"


def fmt_ref(vb: VirtualBarcode) -> str:
    r = vb.reference
    if r.startswith("RF"):
        return " ".join(r[i:i + 4] for i in range(0, len(r), 4))
    # Finnish reference: groups of 5 from the right
    out = []
    while r:
        out.insert(0, r[-5:]); r = r[:-5]
    return " ".join(out)


def fmt_iban(iban: str) -> str:
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def make_pdf_page(vb: VirtualBarcode, lang: str = "en") -> Image.Image:
    """A4 @600dpi: payment details on top, spec-size barcode below (not directly under text)."""
    t, dpi = T[lang], 600
    mm = lambda x: int(round(x / 25.4 * dpi))  # noqa: E731
    page = Image.new("RGB", (mm(210), mm(297)), "white")
    d = ImageDraw.Draw(page)
    f_big, ascii_only = pdf_font(mm(6))
    f, _ = pdf_font(mm(4.2))
    f_small, _ = pdf_font(mm(3.2))
    f_tiny, _ = pdf_font(mm(2.8))
    tx = ascii_safe if ascii_only else (lambda x: x)
    y = mm(25)
    d.text((mm(25), y), tx(t["pdf_head"]), font=f_big, fill="black"); y += mm(12)
    rows = [(t["account"], fmt_iban(vb.iban)), (t["amount"], fmt_amount(vb, lang)),
            (t["reference"], fmt_ref(vb) if vb.reference.isalnum() else t["rf_nonnum"]),
            (t["due"], vb.due_date.strftime("%d.%m.%Y") if vb.due_date else t["none"])]
    for k, v in rows:
        d.text((mm(25), y), tx(k), font=f, fill="#444444")
        d.text((mm(75), y), tx(v), font=f, fill="black"); y += mm(7.5)
    d.text((mm(25), y + mm(2)), vb.raw, font=f_small, fill="#444444")
    bar = render_image(vb, with_text=False).convert("RGB")  # quiet zones included
    page.paste(bar, ((page.width - bar.width) // 2, y + mm(15)))
    d.text((mm(25), mm(280)), tx(t["pdf_note"]), font=f_tiny, fill="#888888")
    return page


class _Actions:
    """Mixed into App: reading images/PDFs, list management, CSV export."""

    # ----- create from form -----
    FORM_FIELDS = ("iban", "amount", "reference", "due")
    FORM_HINTS = {"iban": None, "amount": "h_amount", "reference": "h_ref", "due": "h_due"}

    def _build_create_tab(self):
        f = self.create_tab
        mono = ("Consolas", 11) if sys.platform == "win32" else ("DejaVu Sans Mono", 11)
        self.w_cintro = ttk.Label(f, foreground=GREY, wraplength=760)
        self.w_cintro.grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 8))
        self.fv = {k: tk.StringVar() for k in self.FORM_FIELDS}
        self.cl, self.ce, self.ch = {}, {}, {}
        for i, k in enumerate(self.FORM_FIELDS, start=1):
            self.cl[k] = ttk.Label(f, width=16)
            self.cl[k].grid(row=i, column=0, sticky="w", padx=12, pady=4)
            self.ce[k] = ttk.Entry(f, textvariable=self.fv[k], font=mono, width=30)
            self.ce[k].grid(row=i, column=1, sticky="w", pady=4)
            self.ch[k] = tk.Label(f, anchor="w", fg=GREY, wraplength=380, justify="left")
            self.ch[k].grid(row=i, column=2, sticky="w", padx=10)
            self.fv[k].trace_add("write", lambda *_: self._form_changed())
        rb = ttk.Frame(f); rb.grid(row=5, column=1, columnspan=2, sticky="w", pady=(0, 8))
        self.b_ccheck = ttk.Button(rb, command=self.form_add_check); self.b_ccheck.pack(side="left")
        self.b_crf = ttk.Button(rb, command=self.form_to_rf); self.b_crf.pack(side="left", padx=6)
        ttk.Separator(f).grid(row=6, column=0, columnspan=3, sticky="we", padx=12, pady=8)
        self.w_cres_lbl = ttk.Label(f); self.w_cres_lbl.grid(row=7, column=0, sticky="w", padx=12)
        self.cresult = tk.StringVar()
        rf = ttk.Frame(f); rf.grid(row=7, column=1, columnspan=2, sticky="w")
        self.e_cres = ttk.Entry(rf, textvariable=self.cresult, font=mono, width=56, state="readonly")
        self.e_cres.pack(side="left")
        self.b_ccopy = ttk.Button(rf, width=8, command=self.form_copy); self.b_ccopy.pack(side="left", padx=6)
        self.w_cstatus = tk.Label(f, anchor="w", font=("TkDefaultFont", 10, "bold"))
        self.w_cstatus.grid(row=8, column=0, columnspan=3, sticky="we", padx=12, pady=8)
        bb = ttk.Frame(f); bb.grid(row=9, column=0, columnspan=3, sticky="w", padx=12, pady=4)
        self.b_cshow = ttk.Button(bb, command=self.form_show); self.b_cshow.pack(side="left")
        self.b_cadd = ttk.Button(bb, command=self.form_add_to_list); self.b_cadd.pack(side="left", padx=6)
        self.b_cclear = ttk.Button(bb, command=self.form_clear); self.b_cclear.pack(side="left")
        self.c_prev = tk.Label(f, bg="white", relief="solid", borderwidth=1, height=6)
        self.c_prev.grid(row=10, column=0, columnspan=3, sticky="nsew", padx=12, pady=(8, 12))
        self.c_prev.bind("<Configure>", lambda e: self._form_preview())
        f.columnconfigure(2, weight=1); f.rowconfigure(10, weight=1)
        self.form_code = None
        self._cphoto = None

    def _form_changed(self):
        if not hasattr(self, "w_cstatus"):
            return
        t = T[self.lang]
        vals = {k: self.fv[k].get().strip() for k in self.FORM_FIELDS}
        checks = {"iban": normalize_iban, "amount": parse_amount,
                  "reference": normalize_reference, "due": parse_due}
        errors = {}
        for k, fn in checks.items():
            if vals[k]:
                try:
                    fn(vals[k])
                except VirtualBarcodeError:
                    errors[k] = t["err_" + k]
        for k in self.FORM_FIELDS:
            if k in errors:
                self.ch[k].config(text=errors[k], fg=RED)
            else:
                hint = self.FORM_HINTS[k]
                self.ch[k].config(text=t[hint] if hint else "", fg=GREY)
        self.form_code = None
        if errors:
            self.w_cstatus.config(text="✗ " + next(iter(errors.values())), fg=RED)
        elif not (vals["iban"] and vals["reference"]):
            self.w_cstatus.config(text=t["form_wait"], fg=GREY)
        else:
            try:
                self.form_code = build_code(vals["iban"], vals["amount"], vals["reference"], vals["due"])
                self.w_cstatus.config(text=t["form_ok"], fg=GREEN)
            except VirtualBarcodeError as e:          # should not happen after per-field checks
                self.w_cstatus.config(text=f"✗ {e}", fg=RED)
        self.cresult.set(self.form_code or "")
        self._form_preview()
        st = "normal" if self.form_code else "disabled"
        for b in (self.b_ccopy, self.b_cshow, self.b_cadd):
            b.config(state=st)

    def _form_preview(self):
        """Live barcode preview on the Create tab (same renderer as the saved files)."""
        if not getattr(self, "form_code", None):
            self.c_prev.config(image=""); self._cphoto = None; return
        try:
            im = render_image(parse(self.form_code), with_text=self.show_text.get())
            im.thumbnail((max(self.c_prev.winfo_width() - 20, 300), max(self.c_prev.winfo_height() - 20, 50)),
                         Image.LANCZOS)
            self._cphoto = ImageTk.PhotoImage(im)
            self.c_prev.config(image=self._cphoto)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._cphoto = None
            self.c_prev.config(image="")
            self.w_cstatus.config(text=T[self.lang]["render_err"].format(e=f"{type(e).__name__}: {e}"), fg=RED)

    def new_form(self):
        self.nb.select(self.create_tab)
        self.ce["iban"].focus_set()

    def form_add_check(self):
        try:
            self.fv["reference"].set(fi_reference(self.fv["reference"].get()))
        except VirtualBarcodeError:
            self.w_cstatus.config(text="✗ " + T[self.lang]["err_base"], fg=RED)

    def form_to_rf(self):
        ref = self.fv["reference"].get()
        try:
            norm = normalize_reference(ref)
            if not norm.startswith("RF"):
                rf = rf_reference(norm)
                self.fv["reference"].set(" ".join(rf[i:i + 4] for i in range(0, len(rf), 4)))
        except VirtualBarcodeError:
            self.w_cstatus.config(text="✗ " + T[self.lang]["err_reference"], fg=RED)

    def form_copy(self):
        if self.form_code:
            self.root.clipboard_clear(); self.root.clipboard_append(self.form_code)
            self.w_msg.config(text=T[self.lang]["copied"].format(w=self.form_code))

    def form_show(self):
        if self.form_code:
            self.loaded_from[self.form_code] = T[self.lang]["tab_create"]
            self.set_input(self.form_code)
            self.nb.select(self.main)

    def form_add_to_list(self):
        if self.form_code:
            self._add(parse(self.form_code), T[self.lang]["tab_create"])

    def form_clear(self):
        for v in self.fv.values():
            v.set("")
        self.ce["iban"].focus_set()

    # ----- reading -----
    def load_dialog(self):
        t = T[self.lang]
        paths = filedialog.askopenfilenames(
            parent=self.root, initialdir=self.last_dir, title=t["load"],
            filetypes=[(t["img_files"], "*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp *.pdf"),
                       ("*", "*")])
        if paths:
            self.last_dir = Path(paths[0]).parent
            self.load_files(paths)

    def load_files(self, paths) -> list:
        results = [read_file(p) for p in paths]
        self._handle_results(results)
        return results

    def paste_image(self):
        t = T[self.lang]
        data, err = None, None
        if IS_WSL:                                   # ask Windows directly first
            try:
                got = wsl_clipboard()
            except Exception as e:  # noqa: BLE001
                got, err = None, e
            if got and got[0] == "files":
                return self.load_files([p for p in got[1] if Path(p).is_file()])
            if got:
                data = got[1]
        if data is None:
            try:
                from PIL import ImageGrab
                data = ImageGrab.grabclipboard()
            except Exception as e:  # noqa: BLE001  (Linux needs xclip or wl-paste)
                err = err or e
        if data is None and err is not None:
            messagebox.showwarning(t["read_title"], t["img_err"].format(e=err), parent=self.root)
            return None
        if isinstance(data, list):                       # files copied in Explorer/Finder
            return self.load_files([p for p in data if Path(p).is_file()])
        if data is None:
            self.w_msg.config(text=t["no_img"]); return None
        try:
            res = read_image(data, "clipboard")
        except VirtualBarcodeError as e:
            res = ScanResult("clipboard", error=str(e))
        self._handle_results([res])
        return res

    def _handle_results(self, results):
        t = T[self.lang]
        first = None
        for r in results:
            for vb in r.codes:
                self.loaded_from[vb.raw] = r.source
                self._add(vb, r.source, quiet=True)
                first = first or vb
        if first:
            self.set_input(first.raw)
        lines = [r.summary() for r in results]
        for r in results:
            lines += [f"   ignored: {x}" for x in r.rejected[:3]]
        self.w_msg.config(text=" | ".join(x for x in lines if not x.startswith("   ")))
        if not first or any(r.error for r in results):
            (messagebox.showinfo if first else messagebox.showwarning)(
                t["read_title"], "\n".join(lines), parent=self.root)

    # ----- list -----
    def _add(self, vb, source, quiet=False) -> bool:
        if any(r[0].raw == vb.raw for r in self.records):
            if not quiet:
                self.w_msg.config(text=T[self.lang]["dup"])
            return False
        self.records.append((vb, source))
        self._refresh_list()
        if not quiet:
            self.w_msg.config(text=T[self.lang]["added"].format(n=len(self.records)))
        return True

    def add_current(self):
        if self.vb:
            self._add(self.vb, self.loaded_from.get(self.vb.raw, T[self.lang]["manual"]))

    def _refresh_list(self):
        if not hasattr(self, "tree"):
            return
        self.tree.delete(*self.tree.get_children())
        for i, (vb, src) in enumerate(self.records):
            self.tree.insert("", "end", iid=str(i), values=(
                src, fmt_iban(vb.iban), fmt_amount(vb, self.lang), fmt_ref(vb),
                vb.due_date.strftime("%d.%m.%Y") if vb.due_date else "", vb.raw))
        self.nb.tab(self.list_tab, text=T[self.lang]["tab_list"].format(n=len(self.records)))
        self._autosize_columns()

    def _autosize_columns(self):
        """Fit each column to its widest cell (heading included) so nothing is truncated."""
        import tkinter.font as tkfont
        body = tkfont.nametofont("TkDefaultFont")
        head = tkfont.nametofont("TkHeadingFont")
        for c in self.tree["columns"]:
            w = head.measure(self.tree.heading(c, "text")) + 24
            for iid in self.tree.get_children():
                w = max(w, body.measure(str(self.tree.set(iid, c))) + 20)
            self.tree.column(c, width=w)

    def list_to_editor(self):
        sel = self.tree.selection()
        if sel:
            self.set_input(self.records[int(sel[0])][0].raw)
            self.nb.select(self.main)

    def list_remove(self):
        for i in sorted((int(x) for x in self.tree.selection()), reverse=True):
            del self.records[i]
        self._refresh_list()

    def list_clear(self):
        self.records.clear(); self._refresh_list()

    # ----- CSV -----
    def _csv(self, rows, path=None):
        t = T[self.lang]
        if not rows:
            self.w_msg.config(text=t["list_empty"]); return None
        if path is None:
            name = self.default_name() if len(rows) == 1 and self.vb else "viivakoodit"
            path = filedialog.asksaveasfilename(
                parent=self.root, initialdir=self.last_dir, initialfile=f"{name}.csv",
                defaultextension=".csv", filetypes=[("CSV", "*.csv")])
            if not path:
                return None
        try:
            p = write_csv([to_record(vb, src) for vb, src in rows], path,
                            excel_fi=self.csv_fmt.get() == "excel")
        except OSError as e:
            messagebox.showerror(t["save_err"], str(e), parent=self.root); return None
        self.last_dir = p.parent
        self.w_msg.config(text=t["exported"].format(n=len(rows), p=p.name))
        return p

    def export_current_csv(self, path=None):
        if self.vb:
            return self._csv([(self.vb, self.loaded_from.get(self.vb.raw, T[self.lang]["manual"]))], path)
        return None

    def export_list_csv(self, path=None):
        return self._csv(self.records, path)


class App(_Actions):
    def __init__(self, root: tk.Tk, initial: str | None = None, lang: str | None = None,
                 persist: bool = True):
        self.persist = persist
        self.cfg = load_config() if persist else {}
        lang = lang or self.cfg.get("lang") or "en"
        if lang not in T:
            lang = "en"
        self.root, self.lang = root, lang
        self.vb: VirtualBarcode | None = None
        self.img: Image.Image | None = None
        self._photo = None
        self._after = None
        start = self.cfg.get("last_dir") or ((wsl_default_dir() if IS_WSL else None) or Path.home())
        self.last_dir = Path(start)
        if not self.last_dir.is_dir():
            self.last_dir = Path.home()
        self.menubar = None
        self._menu_code_items: list[tuple] = []
        self.show_text = tk.BooleanVar(value=False)
        self.lang_var = tk.StringVar(value=lang)
        self.records: list[tuple[VirtualBarcode, str]] = []   # (vb, source) for the list tab
        self.loaded_from: dict[str, str] = {}                      # raw code -> file it was read from
        self.csv_fmt = tk.StringVar(value=self.cfg.get("csv_fmt", "excel"))
        root.report_callback_exception = self._on_tk_error
        self._build()
        self._apply_lang()
        if initial:
            self.set_input(initial)
        else:
            self._try_clipboard_prefill()
        self.update()

    # ---------- UI ----------
    def _build(self):
        root = self.root
        root.minsize(820, 660)
        self.nb = ttk.Notebook(root); self.nb.pack(fill="both", expand=True)
        r = self.main = ttk.Frame(self.nb)
        self.create_tab = ttk.Frame(self.nb)
        self.list_tab = ttk.Frame(self.nb)
        self.nb.add(self.main); self.nb.add(self.create_tab); self.nb.add(self.list_tab)
        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass
        mono = ("Consolas", 12) if sys.platform == "win32" else ("DejaVu Sans Mono", 11)
        pad = dict(padx=10, pady=4)

        top = ttk.Frame(r); top.pack(fill="x", **pad)
        self.w_hint = ttk.Label(top); self.w_hint.pack(side="left")
        # language is chosen from the menu; rebuild after the menu callback has returned
        self.lang_var.trace_add("write", lambda *_: self.root.after_idle(self._apply_lang))

        self.txt = tk.Text(r, height=3, wrap="word", font=mono, undo=True,
                           relief="solid", borderwidth=1)
        self.txt.pack(fill="x", **pad)
        self.txt.bind("<<Modified>>", self._on_modified)

        row = ttk.Frame(r); row.pack(fill="x", **pad)
        self.b_paste = ttk.Button(row, command=self.paste_clipboard); self.b_paste.pack(side="left")
        self.b_load = ttk.Button(row, command=self.load_dialog); self.b_load.pack(side="left", padx=(6, 0))
        self.b_pimg = ttk.Button(row, command=self.paste_image); self.b_pimg.pack(side="left", padx=(6, 0))
        self.b_clear = ttk.Button(row, command=self.clear); self.b_clear.pack(side="left", padx=6)
        self.w_count = ttk.Label(row, foreground=GREY); self.w_count.pack(side="left", padx=8)
        self.w_status = tk.Label(r, anchor="w", font=("TkDefaultFont", 10, "bold"))
        self.w_status.pack(fill="x", **pad)

        det = ttk.LabelFrame(r); det.pack(fill="x", **pad)
        self.det = det
        self.lbl, self.val, self.cpy = {}, {}, {}
        for i, f in enumerate(FIELDS):
            self.lbl[f] = ttk.Label(det, width=15)
            self.lbl[f].grid(row=i, column=0, sticky="w", padx=6, pady=1)
            self.val[f] = tk.Label(det, anchor="w", font=mono if f in ("account", "reference", "code") else None)
            self.val[f].grid(row=i, column=1, sticky="we", padx=6)
            if f in ("account", "amount", "reference", "code"):
                self.cpy[f] = ttk.Button(det, width=8, command=lambda f=f: self.copy_field(f))
                self.cpy[f].grid(row=i, column=2, padx=6)
        det.columnconfigure(1, weight=1)

        self.prev = tk.Label(r, bg="white", relief="solid", borderwidth=1, height=8)
        self.prev.pack(fill="both", expand=True, **pad)
        self.prev.bind("<Configure>", lambda e: self._show_preview())

        opt = ttk.Frame(r); opt.pack(fill="x", **pad)
        self.c_text = ttk.Checkbutton(opt, variable=self.show_text, command=self.update)
        self.c_text.pack(side="left")
        self.w_verify = tk.Label(opt, anchor="e"); self.w_verify.pack(side="right")

        btn = ttk.Frame(r); btn.pack(fill="x", padx=10, pady=(4, 10))
        self.b_png = ttk.Button(btn, command=lambda: self.save("png"))
        self.b_svg = ttk.Button(btn, command=lambda: self.save("svg"))
        self.b_pdf = ttk.Button(btn, command=lambda: self.save("pdf"))
        self.b_print = ttk.Button(btn, command=self.print_pdf)
        self.b_add = ttk.Button(btn, command=self.add_current)
        self.b_csv1 = ttk.Button(btn, command=self.export_current_csv)
        for b in (self.b_png, self.b_svg, self.b_pdf, self.b_print, self.b_add, self.b_csv1):
            b.pack(side="left", padx=(0, 6))
        self.w_msg = ttk.Label(r, foreground=GREY, anchor="w"); self.w_msg.pack(fill="x", padx=10, pady=(0, 8))

        self._build_list_tab()
        self._build_create_tab()
        mod = "Command" if sys.platform == "darwin" else "Control"
        for key, fn in [("o", self.load_dialog), ("V", self.paste_image),
                        ("s", lambda: self.save("png")), ("S", lambda: self.save("svg")),
                        ("p", self.print_pdf), ("l", self.add_current), ("e", self.export_list_csv),
                        ("q", self.close), ("n", self.new_form),
                        ("Key-1", lambda: self.nb.select(self.main)),
                        ("Key-2", lambda: self.nb.select(self.create_tab)),
                        ("Key-3", lambda: self.nb.select(self.list_tab))]:
            self._shortcut(f"<{mod}-{key}>", fn)
        self._shortcut("<F1>", self.show_guide)
        self._shortcut("<Escape>", self.clear)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def _shortcut(self, seq, fn):
        def handler(_e=None):
            fn()
            return "break"
        self.root.bind(seq, handler)
        self.txt.bind(seq, handler)      # widget binding runs first; "break" stops Tk's text defaults
        self.txt.focus_set()

    def _build_list_tab(self):
        f = self.list_tab
        f.configure(width=1, height=1)
        f.grid_propagate(False)       # window size follows the main tab; wide list scrolls instead
        cols = ("source", "iban", "amount", "ref", "due", "code")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", selectmode="extended", height=14)
        widths = dict(source=110, iban=165, amount=85, ref=165, due=85, code=180)
        for c in cols:
            self.tree.column(c, width=widths[c], minwidth=40, anchor="e" if c == "amount" else "w", stretch=False)
        sb = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(f, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=(10, 0))
        sb.grid(row=0, column=1, sticky="ns", pady=(10, 0), padx=(0, 10))
        hsb.grid(row=2, column=0, sticky="we", padx=(10, 0), pady=(0, 6))
        f.rowconfigure(0, weight=1); f.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda e: self.list_to_editor())
        self.tree.bind("<Delete>", lambda e: self.list_remove())
        bar = ttk.Frame(f); bar.grid(row=3, column=0, columnspan=2, sticky="we", padx=10, pady=(0, 10))
        self.b_edit = ttk.Button(bar, command=self.list_to_editor)
        self.b_rm = ttk.Button(bar, command=self.list_remove)
        self.b_clr = ttk.Button(bar, command=self.list_clear)
        for b in (self.b_edit, self.b_rm, self.b_clr):
            b.pack(side="left", padx=(0, 6))
        self.b_csv = ttk.Button(bar, command=self.export_list_csv); self.b_csv.pack(side="right")
        self.fmt_box = ttk.Combobox(bar, state="readonly", width=14)
        self.fmt_box.pack(side="right", padx=6)
        self.fmt_box.bind("<<ComboboxSelected>>", lambda e: (
            self.csv_fmt.set(("excel", "std")[self.fmt_box.current()]), self._save_cfg()))
        self.w_fmt = ttk.Label(bar); self.w_fmt.pack(side="right")

    # ---------- settings ----------
    def _save_cfg(self):
        if not self.persist:
            return
        self.cfg.update(lang=self.lang_var.get(), csv_fmt=self.csv_fmt.get(), last_dir=str(self.last_dir))
        save_config(self.cfg)

    # ---------- menu ----------
    def _build_menu(self):
        t = T[self.lang]
        acc = "Cmd" if sys.platform == "darwin" else "Ctrl"
        mb = tk.Menu(self.root, tearoff=False)
        self._menu_code_items = []

        def add(menu, label, cmd, key=None, needs=None):
            menu.add_command(label=label, command=cmd, accelerator=key.replace("Ctrl", acc) if key else None)
            if needs:
                self._menu_code_items.append((menu, menu.index("end"), needs))

        m = tk.Menu(mb, tearoff=False)
        add(m, t["new_form"], self.new_form, "Ctrl+N")
        m.add_separator()
        add(m, t["load"], self.load_dialog, "Ctrl+O")
        add(m, t["paste_img"], self.paste_image, "Ctrl+Shift+V")
        m.add_separator()
        add(m, t["save_png"], lambda: self.save("png"), "Ctrl+S", "img")
        add(m, t["save_svg"], lambda: self.save("svg"), "Ctrl+Shift+S", "img")
        add(m, t["save_pdf"], lambda: self.save("pdf"), None, "img")
        add(m, t["print_"], self.print_pdf, "Ctrl+P", "img")
        m.add_separator()
        add(m, t["csv_one"], self.export_current_csv, None, "code")
        add(m, t["csv_all"], self.export_list_csv, "Ctrl+E")
        m.add_separator()
        add(m, t["exit"], self.close, "Ctrl+Q")
        mb.add_cascade(label=t["m_file"], menu=m)

        m = tk.Menu(mb, tearoff=False)
        add(m, t["paste_code"], self.paste_clipboard)
        add(m, t["clear"], self.clear, "Esc")
        m.add_separator()
        add(m, t["copy_iban"], lambda: self.copy_field("account"), None, "code")
        add(m, t["copy_amount"], lambda: self.copy_field("amount"), None, "code")
        add(m, t["copy_ref"], lambda: self.copy_field("reference"), None, "code")
        add(m, t["copy_code"], lambda: self.copy_field("code"), None, "code")
        m.add_separator()
        add(m, t["add"], self.add_current, "Ctrl+L", "code")
        mb.add_cascade(label=t["m_edit"], menu=m)

        m = tk.Menu(mb, tearoff=False)
        add(m, t["view_main"], lambda: self.nb.select(self.main), "Ctrl+1")
        add(m, t["tab_create"], lambda: self.nb.select(self.create_tab), "Ctrl+2")
        add(m, t["view_list"], lambda: self.nb.select(self.list_tab), "Ctrl+3")
        m.add_separator()
        m.add_checkbutton(label=t["show_text"], variable=self.show_text, command=self.update)
        mb.add_cascade(label=t["m_view"], menu=m)

        m = tk.Menu(mb, tearoff=False)
        for code, name in LANGUAGES.items():
            m.add_radiobutton(label=name, value=code, variable=self.lang_var)
        mb.add_cascade(label=t["m_lang"], menu=m)

        m = tk.Menu(mb, tearoff=False)
        add(m, t["guide"], self.show_guide, "F1")
        add(m, t["shortcuts"], self.show_shortcuts)
        add(m, t["format_info"], self.show_format)
        m.add_separator()
        add(m, t["deps"], self.show_components)
        add(m, t["about"], self.show_about)
        mb.add_cascade(label=t["m_help"], menu=m)

        old, self.menubar = self.menubar, mb
        self.root.config(menu=mb)
        if old is not None:
            old.destroy()
        self.menus = {k: mb.nametowidget(mb.entrycget(i, "menu"))
                      for i, k in enumerate(("file", "edit", "view", "lang", "help"))}
        if hasattr(self, "b_png"):
            self._sync_states()

    # ---------- help windows ----------
    def _text_window(self, title, text, mono=False, width=78, height=22):
        w = tk.Toplevel(self.root)
        w.title(title); w.transient(self.root)
        font = ("Consolas", 10) if sys.platform == "win32" else ("DejaVu Sans Mono", 10)
        box = tk.Text(w, wrap="word", width=width, height=height, padx=12, pady=10, relief="flat",
                      font=font if mono else None)
        sb = ttk.Scrollbar(w, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=sb.set)
        box.insert("1.0", text); box.config(state="disabled")
        box.grid(row=0, column=0, sticky="nsew"); sb.grid(row=0, column=1, sticky="ns")
        ttk.Button(w, text=T[self.lang]["close"], command=w.destroy).grid(
            row=1, column=0, columnspan=2, pady=8)
        w.rowconfigure(0, weight=1); w.columnconfigure(0, weight=1)
        w.bind("<Escape>", lambda e: w.destroy())
        self.last_help = (w, text)
        return w

    def show_guide(self):
        t = T[self.lang]
        return self._text_window(t["guide"], t["guide_text"])

    def show_shortcuts(self):
        t = T[self.lang]
        acc = "Cmd" if sys.platform == "darwin" else "Ctrl"
        rows = [(k.replace("Ctrl", acc), v) for k, v in t["shortcut_rows"]]
        w = max(len(k) for k, _ in rows) + 3
        return self._text_window(t["shortcuts"], "\n".join(f"{k:<{w}}{v}" for k, v in rows),
                                 mono=True, width=60, height=len(rows) + 2)

    def show_format(self):
        t = T[self.lang]
        body = "\n".join(f"{p:<8}{d}" for p, d in t["format_rows"])
        ex = USER
        body += f"\n\n{t['format_note']}\n\n{ex[0]} {ex[1:17]} {ex[17:23]} {ex[23:25]} {ex[25:28]} " \
                f"{ex[28:48]} {ex[48:]}"
        return self._text_window(t["format_info"], body, mono=True, width=82, height=18)

    def show_components(self):
        t = T[self.lang]
        lines = []
        for name, ok, detail in component_status():
            mark = "✓" if ok else "✗"
            lines.append(f"{mark} {name}: {t['ok_mark'] if ok else t['missing']}"
                         + (f"  ({detail})" if detail else ""))
        return self._text_window(t["deps"], "\n".join(lines), width=90, height=len(lines) + 2)

    def show_about(self):
        t = T[self.lang]
        import barcode as _bc
        import PIL
        text = (f"vcode2bar {__version__}\n\n{t['about_text']}\n\n"
                f"{platform.system()} {'(WSL) ' if IS_WSL else ''}· Python {platform.python_version()} · Pillow {PIL.__version__} · "
                f"python-barcode {getattr(_bc, 'version', '?')}")
        return self._text_window(t["about"], text, width=64, height=10)

    def _apply_lang(self):
        if not self.root.winfo_exists():
            return
        self.lang = self.lang_var.get() if self.lang_var.get() in T else "en"
        t = T[self.lang]
        self._build_menu()
        self._save_cfg()
        self.root.title(t["title"])
        self.w_hint.config(text=t["paste_hint"])
        self.b_paste.config(text=t["paste"]); self.b_clear.config(text=t["clear"])
        for f in FIELDS:
            self.lbl[f].config(text=t[f] + ":")
        for b in self.cpy.values():
            b.config(text=t["copy"])
        self.c_text.config(text=t["show_text"])
        self.b_png.config(text=t["save_png"]); self.b_svg.config(text=t["save_svg"])
        self.b_pdf.config(text=t["save_pdf"]); self.b_print.config(text=t["print_"])
        self.b_load.config(text=t["load"]); self.b_pimg.config(text=t["paste_img"])
        self.b_add.config(text=t["add"]); self.b_csv1.config(text=t["csv_one"])
        self.b_edit.config(text=t["to_editor"]); self.b_rm.config(text=t["remove"])
        self.b_clr.config(text=t["clear_list"]); self.b_csv.config(text=t["csv_all"])
        self.w_fmt.config(text=t["csv_fmt"] + ":")
        self.fmt_box.config(values=(t["fmt_excel"], t["fmt_std"]))
        self.fmt_box.current(0 if self.csv_fmt.get() == "excel" else 1)
        for c, k in zip(("source", "iban", "amount", "ref", "due", "code"),
                        ("col_source", "col_iban", "col_amount", "col_ref", "col_due", "code")):
            self.tree.heading(c, text=t[k])
        self.nb.tab(self.main, text=t["tab_main"])
        self.nb.tab(self.create_tab, text=t["tab_create"])
        if hasattr(self, "fv"):
            self.w_cintro.config(text=t["create_intro"])
            for k, lab in (("iban", "f_iban"), ("amount", "f_amount"), ("reference", "f_ref"), ("due", "f_due")):
                self.cl[k].config(text=t[lab] + ":")
            self.w_cres_lbl.config(text=t["f_result"] + ":")
            self.b_ccopy.config(text=t["copy"]); self.b_ccheck.config(text=t["btn_check"])
            self.b_crf.config(text=t["btn_rf"]); self.b_cshow.config(text=t["show_bar"])
            self.b_cadd.config(text=t["add"]); self.b_cclear.config(text=t["clear_form"])
            self._form_changed()
        self._refresh_list()
        if hasattr(self, "txt"):
            self.update()

    # ---------- input ----------
    def get_input(self) -> str:
        return self.txt.get("1.0", "end-1c")

    def set_input(self, s: str):
        self.txt.delete("1.0", "end"); self.txt.insert("1.0", s)
        self.update()

    def _on_modified(self, _e=None):
        if self.txt.edit_modified():
            self.txt.edit_modified(False)
            if self._after:
                self.root.after_cancel(self._after)
            self._after = self.root.after(200, self._debounced)  # debounce typing

    def _debounced(self):
        self._after = None
        self.update()

    def close(self):
        """Cancel pending callbacks before destroying (avoids Tcl 'invalid command' errors)."""
        self._save_cfg()
        if self._after:
            self.root.after_cancel(self._after); self._after = None
        self.root.destroy()

    def paste_clipboard(self):
        try:
            self.set_input(self.root.clipboard_get())
        except tk.TclError:
            pass

    def _try_clipboard_prefill(self):
        try:
            clip = self.root.clipboard_get()
        except tk.TclError:
            return
        if extract_code(clip):
            self.set_input(clip)

    def clear(self):
        self.set_input("")
        self.txt.focus_set()

    # ---------- core ----------
    def update(self):
        t = T[self.lang]
        raw = self.get_input()
        digits = re.sub(r"[\s\-]", "", raw)
        self.w_count.config(text=t["digits"].format(n=len(re.sub(r"\D", "", raw))))
        self.vb, self.img = None, None
        err = None
        if not raw.strip():
            status, color = t["empty"], GREY
        else:
            code = digits if digits.isdigit() and len(digits) == 54 else extract_code(raw)
            try:
                self.vb = parse(code if code else digits)
                status, color = t["ok"], GREEN
                if self.vb.raw in self.loaded_from:
                    status += t["from_src"].format(s=self.loaded_from[self.vb.raw])
            except VirtualBarcodeError as e:
                err = str(e)
                status, color = (t["notfound"] if not (digits.isdigit()) else "✗ " + err), RED
        n_digits = len(re.sub(r"\D", "", raw))
        if self.vb and n_digits != 54:
            self.w_count.config(text=t["found"])
        self.w_status.config(text=status, fg=color)
        self._fill_details()
        self.render_error = None
        if self.vb:
            try:
                self.img = render_image(self.vb, with_text=self.show_text.get())
            except Exception as e:  # noqa: BLE001  - show it instead of dying silently
                self._render_failed(e)
            else:
                self._verify()
        else:
            self.w_verify.config(text="")
        try:
            self._show_preview()
        except Exception as e:  # noqa: BLE001
            self._render_failed(e)
        self._sync_states()
        return err

    def _render_failed(self, e: Exception):
        traceback.print_exc()
        self.img, self._photo = None, None
        self.render_error = f"{type(e).__name__}: {e}"
        self.prev.config(image="", text="")
        self.w_verify.config(text=T[self.lang]["render_err"].format(e=self.render_error), fg=RED)

    def _sync_states(self):
        code_state = "normal" if self.vb else "disabled"
        img_state = "normal" if self.vb and self.img is not None else "disabled"
        for b in (self.b_png, self.b_svg, self.b_pdf, self.b_print):
            b.config(state=img_state)
        for b in (self.b_add, self.b_csv1, *self.cpy.values()):
            b.config(state=code_state)
        for menu, idx, needs in self._menu_code_items:
            menu.entryconfigure(idx, state=img_state if needs == "img" else code_state)

    def _on_tk_error(self, exc, val, tb):
        traceback.print_exception(exc, val, tb)
        try:
            messagebox.showerror(T[self.lang]["unexpected"],
                                 f"{exc.__name__}: {val}\n\n(full details were printed to the terminal)",
                                 parent=self.root)
        except Exception:  # noqa: BLE001
            pass

    def _fill_details(self):
        t, vb = T[self.lang], self.vb
        if not vb:
            for f in FIELDS:
                self.val[f].config(text="", fg="black")
            return
        vals = {
            "version": f"{vb.version} ({'RF' if vb.version == 5 else 'FI'})",
            "account": fmt_iban(vb.iban),
            "amount": fmt_amount(vb, self.lang),
            "reference": fmt_ref(vb) if vb.reference.isalnum() else t["rf_nonnum"],
            "code": vb.raw,
        }
        due_fg = "black"
        if vb.due_date:
            d = (date.today() - vb.due_date).days
            vals["due"] = vb.due_date.strftime("%d.%m.%Y")
            if d > 0:
                vals["due"] += t["overdue"].format(d=d); due_fg = AMBER
            elif d == 0:
                vals["due"] += t["today"]; due_fg = AMBER
        else:
            vals["due"] = t["none"]
        for f in FIELDS:
            self.val[f].config(text=vals[f], fg=due_fg if f == "due" else "black")

    def _verify(self):
        t = T[self.lang]
        if _zbar_decode is None:
            self.w_verify.config(text=t["verify_na"], fg=GREY); return
        try:
            res = _zbar_decode(self.img.convert("L"))
        except Exception as e:  # noqa: BLE001  (e.g. broken zbar install)
            traceback.print_exc()
            self.verified = False
            self.w_verify.config(text=t["verify_err"].format(e=f"{type(e).__name__}: {e}"), fg=AMBER)
            return
        ok = len(res) == 1 and res[0].type == "CODE128" and res[0].data.decode() == self.vb.raw
        self.w_verify.config(text=t["verify_ok"] if ok else t["verify_fail"], fg=GREEN if ok else RED)
        self.verified = ok

    def _show_preview(self):
        if not self.img:
            self.prev.config(image="", text=""); self._photo = None; return
        w = max(self.prev.winfo_width() - 20, 300)
        h = max(self.prev.winfo_height() - 20, 60)
        im = self.img.copy()
        im.thumbnail((w, h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(im)
        self.prev.config(image=self._photo)

    # ---------- actions ----------
    def copy_field(self, f):
        vb = self.vb
        if not vb:
            return
        value = {"account": vb.iban, "amount": f"{vb.amount:.2f}".replace(".", ","),
                 "reference": vb.reference, "code": vb.raw}[f]
        self.root.clipboard_clear(); self.root.clipboard_append(value)
        self.w_msg.config(text=T[self.lang]["copied"].format(w=value))

    def default_name(self) -> str:
        vb = self.vb
        ref = re.sub(r"\W", "", vb.reference)[:24]
        due = vb.due_date.strftime("%Y-%m-%d") if vb.due_date else "no-due"
        return f"viivakoodi_{ref}_{due}"

    def save(self, fmt: str, path: str | None = None) -> Path | None:
        t = T[self.lang]
        if not self.vb:
            self.w_msg.config(text=t["nothing"]); return None
        if path is None:
            path = filedialog.asksaveasfilename(
                parent=self.root, initialdir=self.last_dir, initialfile=f"{self.default_name()}.{fmt}",
                defaultextension=f".{fmt}", filetypes=[(fmt.upper(), f"*.{fmt}")])
            if not path:
                return None
        p = Path(path)
        try:
            if fmt == "png":
                self.img.save(p, dpi=(600, 600))
            elif fmt == "svg":
                render(self.vb, p.with_suffix(""), ("svg",), with_text=self.show_text.get())
            elif fmt == "pdf":
                self.make_pdf_page().save(p, "PDF", resolution=600)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(t["save_err"], str(e), parent=self.root); return None
        self.last_dir = p.parent
        self.w_msg.config(text=t["saved"].format(p=p.name))
        return p

    def make_pdf_page(self) -> Image.Image:
        return make_pdf_page(self.vb, self.lang)

    def print_pdf(self):
        if not self.vb:
            return
        p = Path(tempfile.gettempdir()) / f"{self.default_name()}_{datetime.now():%H%M%S%f}.pdf"
        if not self.save("pdf", str(p)):
            return
        try:
            if IS_WSL:
                wsl_open(p)      # Windows PDF viewer - WSL has none of its own
            elif sys.platform == "win32":
                os.startfile(p)  # opens default viewer; print from there
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as e:  # noqa: BLE001
            messagebox.showinfo("PDF", f"{p}\n\n{e}", parent=self.root)




def run_gui(initial: str | None = None, lang: str | None = None, persist: bool = True) -> int:
    if tk is None:
        print("The GUI needs tkinter (Debian/Ubuntu: apt install python3-tk). "
              "Use the command line instead: python vcode2bar.py --help", file=sys.stderr)
        return 4
    if sys.platform == "win32":                  # crisp UI on high-DPI screens instead of bitmap scaling
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001  (older Windows)
            pass
    try:
        root = tk.Tk()
    except tk.TclError as e:
        hint = ("On WSL the GUI needs WSLg (Windows 11, or Windows 10 with current updates); "
                "alternatively run vcode2bar natively on Windows." if IS_WSL
                else "Run it from a desktop session, or use the command line.")
        print(f"Cannot open the GUI window: {e}\n{hint}\n"
              "The command line works without a display: python vcode2bar.py --help", file=sys.stderr)
        return 4
    App(root, initial=initial, lang=lang, persist=persist)
    root.mainloop()
    return 0



# =====================================================================================
# Built-in test suite:  python vcode2bar.py --selftest [RUNS]
# =====================================================================================
USER = "460143230002086030000500000000000000000000040905260908"


def _mini_pdf(path, lines):
    """Write a minimal one-page text-only PDF (no extra dependency needed for tests)."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    body = "BT /F1 12 Tf 50 800 Td 16 TL " + " ".join(f"({esc(x)}) Tj T*" for x in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            f"<< /Length {len(body)} >>\nstream\n{body}\nendstream"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out)); out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    Path(path).write_bytes(out)


# Official test invoices, Finanssiala Pankkiviivakoodi-opas v5.3 ch. 13.1/13.2:
# (pairs as printed, IBAN, amount, reference, due, expected Tarkiste 2)
OFFICIAL = [
 ("4 7 94 40 5 2 02 00 3 6 08 20 04 8 8 31 50 0 0 00 0 0 08 6 8 51 62 5 9 6 1 98 9 7 10 0 6 12","FI7944052020036082","4883.15","868516259619897",date(2010,6,12),40),
 ("4 5 81 01 7 1 00 00 0 0 12 20 00 4 8 29 90 0 0 00 0 0 05 5 9 58 22 4 3 2 9 46 7 1 12 0 1 31","FI5810171000000122","482.99","559582243294671",date(2012,1,31),55),
 ("4 0 25 00 0 4 64 00 0 1 30 20 00 6 9 38 00 0 0 0 0 0 6 98 7 5 67 20 8 3 4 3 53 6 4 11 0 7 24","FI0250004640001302","693.80","69875672083435364",date(2011,7,24),14),
 ("4 1 56 60 1 0 00 15 3 0 64 10 07 4 4 45 40 0 0 00 0 0 77 5 8 47 47 9 0 6 4 74 8 9 19 1 2 19","FI1566010001530641","7444.54","7758474790647489",date(2019,12,19),63),
 ("4 1 68 00 0 1 40 00 5 0 26 70 00 9 3 58 50 0 0 00 0 7 87 7 7 67 96 5 6 6 2 86 8 7 00 0 0 00","FI1680001400050267","935.85","78777679656628687",None,30),
 ("47 3 3 1 3 13 0 0 1 0 0 0 05 8 0 00 00 0 0 0 0 00 0 0 00 0 0 00 0 0 0 0 00 86 8 6 2 4 13 0 8 0 9","FI7331313001000058","0.00","868624",date(2013,8,9),68),
 ("48 33 30 10 00 11 00 77 51 50 00 02 00 00 92 12 53 74 25 25 39 89 77 37 16 05 25","FI8333010001100775","150000.20","92125374252539897737",date(2016,5,25),45),
 ("43 9 3 6 3 63 0 0 2 0 9 2 49 2 0 00 00 1 0 3 0 00 0 0 00 0 0 00 0 0 0 5 90 73 8 3 9 0 23 0 3 11","FI3936363002092492","1.03","590738390",date(2023,3,11),9),
 ("49 2 3 9 3 90 0 0 1 0 0 3 39 1 0 00 00 0 0 2 0 00 0 0 00 0 0 00 0 0 0 0 01 35 7 9 1 4 99 1 2 24","FI9239390001003391","0.02","1357914",date(2099,12,24),30),
 ("57 94 40 52 02 00 36 08 20 04 88 31 50 90 00 00 08 68 51 62 59 61 98 97 10 06 12","FI7944052020036082","4883.15","RF098685162596198 97".replace(" ",""),date(2010,6,12),74),
 ("5 5 81 01 7 1 00 00 0 0 12 20 00 4 8 29 90 6 0 00 0 0 05 5 9 58 22 4 3 2 9 46 7 1 10 0 1 31","FI5810171000000122","482.99","RF06559582243294671",date(2010,1,31),31),
 ("50 25 00 04 64 00 01 30 20 00 69 38 06 10 00 00 00 00 69 87 56 72 08 39 11 07 24","FI0250004640001302","693.80","RF61698756720839",date(2011,7,24),34),  # doc prints 59: erratum from 2019 example swap; spec algorithm + python-barcode both give 34
 ("5 1 56 60 1 0 00 15 3 0 64 10 07 4 4 45 48 4 0 00 0 0 77 5 8 47 47 9 0 6 4 74 8 9 19 1 2 19","FI1566010001530641","7444.54","RF847758474790647489",date(2019,12,19),16),
 ("5 1 68 00 0 1 40 00 5 0 26 70 00 9 3 58 56 0 0 00 0 7 87 7 7 67 96 5 6 6 2 86 8 7 00 0 0 00","FI1680001400050267","935.85","RF6078777679656628687",None,15),
 ("57 3 3 1 3 13 0 0 1 0 0 0 05 8 0 00 00 0 0 0 1 00 0 0 00 0 0 00 0 0 0 0 00 86 8 6 2 4 13 0 8 09","FI7331313001000058","0.00","RF10868624",date(2013,8,9),91),
 ("58 3 3 3 0 10 0 0 1 1 0 0 77 5 1 50 00 0 2 0 7 10 9 2 12 5 3 74 2 5 2 5 39 89 7 7 3 7 16 0 5 25","FI8333010001100775","150000.20","RF7192125374252539897737",date(2016,5,25),80),
 ("53 93 63 63 00 20 92 49 20 00 00 10 36 60 00 00 00 00 00 05 90 73 83 90 23 03 11","FI3936363002092492","1.03","RF66590738390",date(2023,3,11),10),
 ("59 2 3 9 3 90 0 0 1 0 0 3 39 1 0 00 00 0 0 2 9 50 0 0 00 0 0 00 0 0 0 0 01 35 7 9 1 4 99 1 2 24","FI9239390001003391","0.02","RF951357914",date(2099,12,24),33),
]
# Independent implementations (Ruby gem docs, PyPI package docs)
THIRD_PARTY = [
 ("437159030000007760000122500000000000000000011112161221","FI3715903000000776","12.25","11112",date(2016,12,21)),
 ("449500094200287300001002000000000000001234567907201212","FI4950009420028730","100.20","1234567907",date(2020,12,12)),  # PyPI README says 2022 but its own output encodes 201212
]
USER_CASE = ("460143230002086030000500000000000000000000040905260908","FI6014323000208603","50.00","40905",date(2026,9,8))

NEGATIVE = {
 "length 53": "46014323000208603000050000000000000000000004090526090",
 "letters": "46014323000208603000050000000000000000000004090526090X",
 "version 2": "260143230002086030000500000000000000000000040905260908",
 "IBAN typo": "460143230002086130000500000000000000000000040905260908",
 "ref check digit": "460143230002086030000500000000000000000000040906260908",
 "reserved != 000": "460143230002086030000500010000000000000000040905260908",
 "bad date 13th month": "460143230002086030000500000000000000000000040905261308",
 "RF checksum": "57944052020036082004883159100000868516259619897100612",
}

RF = OFFICIAL[9][0].replace(" ", "")  # official v5 test invoice 1


def fi_ref(base):  # independent re-implementation for generator
    s = sum(int(d) * (7, 3, 1)[i % 3] for i, d in enumerate(reversed(base)))
    return base + str((10 - s % 10) % 10)

def mk_iban(rng):
    bban = "".join(rng.choice("0123456789") for _ in range(14))
    chk = 98 - int("".join(str(int(c, 36)) for c in bban + "FI00")) % 97
    return f"FI{chk:02d}{bban}"

def mk_rf(num):
    chk = 98 - int(num + "271500") % 97
    return f"RF{chk:02d}{num}"

def random_case(rng):
    iban = mk_iban(rng)
    cents = rng.randint(0, 99999999)
    due = None if rng.random() < .15 else date(rng.randint(2000, 2099), rng.randint(1, 12), rng.randint(1, 28))
    dd = due.strftime("%y%m%d") if due else "000000"
    if rng.random() < .5:
        ref = fi_ref(str(rng.randint(100, 10**19 - 1)))
        code = f"4{iban[2:]}{cents:08d}000{ref.zfill(20)}{dd}"
    else:
        ref = mk_rf(str(rng.randint(1, 10**21 - 1)))
        code = f"5{iban[2:]}{cents:08d}{ref[2:4]}{ref[4:].zfill(21)}{dd}"
    return code, iban, Decimal(cents) / 100, ref, due

def check_fields(vb, iban, amt, ref, due):
    assert vb.iban == iban, (vb.iban, iban)
    assert vb.amount == Decimal(amt), (vb.amount, amt)
    assert vb.reference == ref, (vb.reference, ref)
    assert vb.due_date == due, (vb.due_date, due)

def roundtrip(code, d):
    vb = parse(code)
    paths = render(vb, d / code, ("png", "svg"))
    png = next(p for p in paths if p.suffix == ".png")
    assert verify_png(png, code), f"decode mismatch {code}"
    assert Path(d / f"{code}.svg").stat().st_size > 1000
    return vb

def _t_core(seed):
    rng = random.Random(seed); n = 0
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        for pairs, iban, amt, ref, due, chk in OFFICIAL:
            code = pairs.replace(" ", "")
            assert len(code) == 54, (pairs, len(code))
            assert code128c_checksum(code) == chk, (code, code128c_checksum(code), chk)
            from barcode import Code128
            c = Code128(code); enc = c._build()
            assert enc[0] == 105 and len(enc) == 28, "must be pure set C: START C + 27 pairs"
            assert c._calculate_checksum(list(enc)) == chk, ("library checksum", chk)
            check_fields(roundtrip(code, d), iban, amt, ref, due); n += 1
        for code, iban, amt, ref, due in THIRD_PARTY + [USER_CASE]:
            check_fields(roundtrip(code, d), iban, amt, ref, due); n += 1
        for name, code in NEGATIVE.items():
            try: parse(code); raise AssertionError(f"accepted invalid: {name}")
            except VirtualBarcodeError: n += 1
        # spaces/dashes accepted; all-zero RF field accepted per spec
        sp = " ".join(USER_CASE[0][i:i+4] for i in range(0, 54, 4)).replace(" ", "-", 3)
        assert parse(sp).raw == USER_CASE[0]; n += 1
        parse("5" + "7944052020036082" + "00488315" + "0"*23 + "100612"); n += 1
        # CLI end-to-end
        rc = main([USER_CASE[0], "-o", str(d / "cli"), "--verify"]); assert rc == 0; n += 1
        rc = main(["123", "--info-only"]); assert rc == 2; n += 1
        for _ in range(300):
            code, iban, amt, ref, due = random_case(rng)
            assert len(code) == 54
            check_fields(roundtrip(code, d), iban, amt, ref, due); n += 1
    return n



def _t_degrade(im, kind, rng):
    im = im.convert("L")
    return {
        "rot90": lambda: im.rotate(90, expand=True),
        "tilt": lambda: im.rotate(rng.choice((-12, -6, 6, 12)), expand=True, fillcolor=255, resample=Image.BICUBIC),
        "small": lambda: im.resize((im.width // 5, im.height // 5), Image.LANCZOS),
        "noise": lambda: _noise(im.resize((im.width // 3, im.height // 3)), rng),
        "clean": lambda: im,
    }[kind]()

def _noise(im, rng):
    px = im.load()
    for _ in range(im.width * im.height // 12):
        px[rng.randrange(im.width), rng.randrange(im.height)] = rng.choice((0, 255))
    return im

def _t_read(seed):
    rng = random.Random(seed); n = 0
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        vbs = [parse(USER), parse(RF)] + [parse(random_case(rng)[0]) for _ in range(3)]
        recs = [to_record(vb, f"src{i}") for i, vb in enumerate(vbs)]
        # --- CSV standard
        p = write_csv(recs, d / "std.csv")
        rows = list(csv.DictReader(open(p, encoding="utf-8")))
        assert len(rows) == 5 and list(rows[0]) == CSV_FIELDS; n += 1
        assert rows[0]["code"] == USER and rows[0]["amount_eur"] == "50.00" and rows[0]["iban"] == "FI6014323000208603"; n += 1
        assert rows[0]["reference"] == "40905" and rows[0]["due_date"] == "2026-09-08" and rows[0]["reference_type"] == "FI"; n += 1
        assert rows[1]["reference"] == "RF09868516259619897" and rows[1]["reference_type"] == "RF"; n += 1
        for r, vb in zip(rows, vbs):   # every row round-trips back to the same code
            assert parse(r["code"]).raw == vb.raw and r["amount_eur"] == f"{vb.amount:.2f}"; n += 1
        # --- CSV Excel-FI
        raw = (d / "x.csv"); write_csv(recs, raw, excel_fi=True)
        b = raw.read_bytes(); assert b[:3] == b"\xef\xbb\xbf"; n += 1
        rows = list(csv.DictReader(io.StringIO(b.decode("utf-8-sig")), delimiter=";"))
        assert rows[0]["amount_eur"] == "50,00" and rows[0]["code"] == f'="{USER}"'; n += 1
        # --- formula injection guard
        write_csv([to_record(vbs[0], "=HYPERLINK(\"x\")")], d / "inj.csv")
        assert list(csv.DictReader(open(d / "inj.csv")))[0]["source"].startswith("'="); n += 1
        # --- images: PNG/JPEG files + degradations
        for vb in vbs:
            img = render_image(vb)
            for kind in ("clean", "rot90", "tilt", "small", "noise"):
                r = read_image(_t_degrade(img, kind, rng), kind)
                assert [c.raw for c in r.codes] == [vb.raw], (kind, r.summary()); n += 1
            jp = d / "p.jpg"; img.convert("L").resize((img.width // 3, img.height // 3)).save(jp, quality=40)
            assert [c.raw for c in read_file(jp).codes] == [vb.raw]; n += 1
        # two bank barcodes in one image
        a, bimg = render_image(vbs[0]).convert("L"), render_image(vbs[1]).convert("L")
        both = Image.new("L", (a.width, a.height + bimg.height + 200), 255); both.paste(a, (0, 0)); both.paste(bimg, (0, a.height + 200))
        assert {c.raw for c in read_image(both).codes} == {vbs[0].raw, vbs[1].raw}; n += 1
        # non-bank Code128 -> rejected, not accepted
        from barcode import Code128
        from barcode.writer import ImageWriter
        other = Code128("INVOICE-2024-0042", writer=ImageWriter()).render()
        r = read_image(other); assert not r.codes and r.rejected and "INVOICE" in r.rejected[0]; n += 1
        # a 54-digit Code128 with a bad IBAN checksum -> rejected
        badcode = USER[:5] + ("0" if USER[5] != "0" else "1") + USER[6:]
        r = read_image(Code128(badcode, writer=ImageWriter()).render())
        assert not r.codes and "IBAN" in r.rejected[0]; n += 1
        # blank image / text file / corrupt image
        r = read_image(Image.new("RGB", (800, 300), "white")); assert r.error == "no barcode found"; n += 1
        (d / "a.txt").write_text("hello"); assert "not a readable image" in read_file(d / "a.txt").error; n += 1
        (d / "bad.png").write_bytes(b"\x89PNG garbage"); assert read_file(d / "bad.png").error; n += 1
        # --- PDFs: image-only (barcode graphic), text-only (printed code), both, neither
        render_image(vbs[2]).convert("RGB").save(d / "img.pdf", "PDF", resolution=600)
        assert [c.raw for c in read_file(d / "img.pdf").codes] == [vbs[2].raw]; n += 1
        grouped = " ".join(vbs[3].raw[i:i + 4] for i in range(0, 54, 4))
        _mini_pdf(d / "txt.pdf", ["Lasku 1043", "Puh 040 1234567", "Virtuaaliviivakoodi:", grouped])
        assert [c.raw for c in read_file(d / "txt.pdf").codes] == [vbs[3].raw]; n += 1
        _mini_pdf(d / "none.pdf", ["Nothing to pay here", "Tel 0401234567"])
        assert read_file(d / "none.pdf").error; n += 1
        # --- text extraction: stray digits before/around the code must not hide it
        g = " ".join(vbs[0].raw[i:i + 4] for i in range(0, 54, 4))
        for pre in ("Summa 45 ", "Asiakas 4 ", "Rivi 5 - ", "4 5 4 5 ", "Tel 0401234567\n"):
            assert extract_code(pre + g + " 12 kiitos") == vbs[0].raw, pre; n += 1
        assert [v.raw for v in iter_codes(f"A: {vbs[0].raw} B: {vbs[1].raw} A again {vbs[0].raw}")] == [vbs[0].raw, vbs[1].raw]; n += 1
        # --- CLI
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert main(["--read", str(d / "img.pdf"), str(d / "txt.pdf"), "--csv", str(d / "cli.csv")]) == 0
            assert main([USER, RF, "--csv", str(d / "cli2.csv"), "--excel"]) == 0
            assert main(["--read", str(d / "none.pdf")]) == 5
            assert main([USER, "123", "--csv", str(d / "cli3.csv")]) == 2   # bad arg flagged, good one kept
        codes = [r["code"] for r in csv.DictReader(open(d / "cli.csv"))]
        assert codes == [vbs[2].raw, vbs[3].raw]; n += 1
        assert len(list(csv.DictReader(open(d / "cli2.csv", encoding="utf-8-sig"), delimiter=";"))) == 2; n += 1
        assert [r["code"] for r in csv.DictReader(open(d / "cli3.csv"))] == [USER]; n += 1
    return n



def dec(img):
    from pyzbar.pyzbar import decode
    r = decode(img.convert("L")); return r[0].data.decode() if len(r) == 1 else None

def pump(root):
    for _ in range(5): root.update(); root.update_idletasks()

def _t_gui(seed):
    n = 0; rng = random.Random(seed)
    root = tk.Tk(); root.geometry("900x700")
    root.clipboard_clear(); root.clipboard_append("Invoice 1043\nCode: " + " ".join(USER[i:i+4] for i in range(0,54,4)))
    app = App(root, lang="en", persist=False); pump(root)
    # 1 clipboard prefill at startup (whole invoice text)
    assert app.vb and app.vb.raw == USER, "clipboard prefill"; n += 1
    assert app.w_count.cget("text") == "code found in pasted text"; n += 1
    # 2 fields + overdue
    assert app.val["account"].cget("text") == "FI60 1432 3000 2086 03"; n += 1
    assert app.val["amount"].cget("text") == "50,00 €"; n += 1
    assert app.val["reference"].cget("text") == "40905"; n += 1
    assert "overdue" in app.val["due"].cget("text") and app.val["due"].cget("fg") == AMBER; n += 1
    # 3 preview shown + read-back ok
    assert app._photo is not None and app._photo.width() > 300; n += 1
    assert app.verified and "✓" in app.w_verify.cget("text"); n += 1
    # 4 invalid inputs disable buttons with a reason
    for bad, frag in [(USER[:40], "54 digits"), (USER[:5] + "9" + USER[6:], "IBAN"),
                      ("hello world", "No valid"), (USER[:-7] + "6260908", "check digit")]:
        app.set_input(bad); pump(root)
        assert app.vb is None and str(app.b_png.cget("state")) == "disabled", bad
        assert frag in app.w_status.cget("text"), (bad, app.w_status.cget("text")); n += 1
    # 5 empty
    app.clear(); pump(root); assert app.vb is None and "Waiting" in app.w_status.cget("text"); n += 1
    # 6 typing path (<<Modified>> + debounce)
    app.txt.insert("1.0", RF); pump(root); root.after(300); root.update(); pump(root)
    assert app.vb and app.vb.reference == "RF09868516259619897", app.w_status.cget("text"); n += 1
    assert app.val["reference"].cget("text") == "RF09 8685 1625 9619 897"; n += 1
    assert app.val["due"].cget("fg") == AMBER; n += 1  # 2010, overdue
    # 7 language switch
    app.lang_var.set("fi"); pump(root)
    assert app.b_png.cget("text") == "Tallenna PNG" and app.lbl["due"].cget("text") == "Eräpäivä:"; n += 1
    assert "Kelvollinen" in app.w_status.cget("text"); n += 1
    app.lang_var.set("en"); pump(root)
    # 8 copy buttons -> clipboard
    app.copy_field("reference"); assert root.clipboard_get() == "RF09868516259619897"; n += 1
    app.copy_field("account"); assert root.clipboard_get() == "FI7944052020036082"; n += 1
    app.copy_field("amount"); assert root.clipboard_get() == "4883,15"; n += 1
    # 9 saves: PNG, SVG, PDF + decode
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        p = app.save("png", str(d / "a.png")); assert dec(Image.open(p)) == RF; n += 1
        assert Image.open(p).info["dpi"][0] >= 599; n += 1
        p = app.save("svg", str(d / "a.svg")); assert p.exists() and "<svg" in p.read_text(); n += 1
        p = app.save("pdf", str(d / "a.pdf")); assert p.read_bytes()[:4] == b"%PDF"; n += 1
        page = app.make_pdf_page(); assert page.size == (4961, 7016); n += 1   # A4 @600dpi
        assert dec(page) == RF; n += 1  # barcode on the printable page decodes
        # 9b font handling: detection must reject the builtin font and accept a full font
        from PIL import ImageFont
        assert not _font_supports(ImageFont.load_default(size=40)); n += 1
        f, ascii_only = pdf_font(40)
        if not ascii_only: assert _font_supports(f); n += 1
        saved = FONT_CANDIDATES[:]; FONT_CANDIDATES[:] = []      # simulate no system fonts
        assert pdf_font(40)[1] is True and ascii_safe("Eräpäivä 5,00 €") == "Erapaiva 5,00 EUR"
        assert dec(app.make_pdf_page()) == RF; n += 1
        FONT_CANDIDATES[:] = saved
        # 10 show-digits toggle still decodes
        app.show_text.set(True); app.update(); pump(root)
        assert dec(app.img) == RF and app.verified; n += 1
        app.show_text.set(False); app.update()
        # 11 due today / no due date / open amount
        td = date.today().strftime("%y%m%d")
        app.set_input(USER[:-6] + td); pump(root); assert "today" in app.val["due"].cget("text"); n += 1
        app.set_input(USER[:17] + "00000000" + USER[25:48] + "000000"); pump(root)
        assert app.val["due"].cget("text") == "not given" and "open" in app.val["amount"].cget("text"); n += 1
        # 12 random round-trips through the GUI path
        for _ in range(40):
            code, iban, amt, ref, due = random_case(rng)
            app.set_input(code if rng.random() < .5 else f"Pay this: {' '.join(code[i:i+3] for i in range(0,54,3))} thanks")
            pump(root)
            assert app.vb and app.vb.raw == code and app.verified, code
            assert dec(Image.open(app.save("png", str(d / "r.png")))) == code; n += 1
    n += gui_read_csv_tests(app, root, rng)
    n += gui_menu_tests(app, root)
    n += gui_create_tests(app, root, rng)
    n += gui_robustness_tests(app, root)
    n += gui_wsl_tests(app, root)
    app.close()
    return n


def gui_read_csv_tests(app, root, rng):
    import csv
    from PIL import ImageGrab
    n = 0
    shown = []
    messagebox.showinfo = lambda *a, **k: shown.append(("info", a))
    messagebox.showwarning = lambda *a, **k: shown.append(("warn", a))
    app.lang_var.set("en"); app.list_clear(); pump(root)
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        vb_a, vb_b = parse(USER), parse(RF)
        render_image(vb_a).convert("L").rotate(90, expand=True).save(d / "photo.jpg", quality=60)
        render_image(vb_b).convert("RGB").save(d / "inv.pdf", "PDF", resolution=600)
        Image.new("RGB", (600, 200), "white").save(d / "empty.png")
        # 1 load two files -> both in list, first shown in editor with source
        app.load_files([str(d / "photo.jpg"), str(d / "inv.pdf")]); pump(root)
        assert [r[0].raw for r in app.records] == [USER, RF] and app.vb.raw == USER; n += 1
        assert "from photo.jpg" in app.w_status.cget("text"); n += 1
        assert app.nb.tab(app.list_tab, "text") == "List (2)"; n += 1
        assert not shown; n += 1                             # success -> no popup
        # 2 file without barcode -> warning popup, list unchanged
        app.load_files([str(d / "empty.png")]); pump(root)
        assert shown and shown[-1][0] == "warn" and "no barcode" in shown[-1][1][1]; n += 1
        assert len(app.records) == 2; n += 1
        # 3 duplicates are not added twice
        app.set_input(USER); pump(root); app.add_current()
        assert len(app.records) == 2 and "Already" in app.w_msg.cget("text"); n += 1
        # 4 typed code -> added with source "typed/pasted"
        code3 = random_case(rng)[0]; app.set_input(code3); pump(root); app.add_current()
        assert app.records[-1] == (parse(code3), "typed/pasted") or (app.records[-1][0].raw == code3 and app.records[-1][1] == "typed/pasted"); n += 1
        # 5 clipboard image paste (monkeypatched grab)
        orig = ImageGrab.grabclipboard
        code4 = random_case(rng)[0]
        ImageGrab.grabclipboard = lambda: render_image(parse(code4)).resize((700, 100))
        app.paste_image(); pump(root)
        assert app.vb.raw == code4 and app.records[-1][1] == "clipboard"; n += 1
        ImageGrab.grabclipboard = lambda: [str(d / "inv.pdf")]          # files copied in Explorer
        app.paste_image(); pump(root); assert app.vb.raw == RF; n += 1
        ImageGrab.grabclipboard = lambda: None
        app.paste_image(); pump(root); assert "No image" in app.w_msg.cget("text"); n += 1
        def boom(): raise RuntimeError("xclip missing")
        ImageGrab.grabclipboard = boom
        app.paste_image(); pump(root); assert "xclip" in shown[-1][1][1]; n += 1
        ImageGrab.grabclipboard = orig
        # 6 list -> editor, remove
        app.tree.selection_set("1"); app.list_to_editor(); pump(root); assert app.vb.raw == RF; n += 1
        app.tree.selection_set("0"); app.list_remove(); pump(root)
        assert [r[0].raw for r in app.records][0] == RF and len(app.records) == 3; n += 1
        # 7 CSV export: list (Excel-FI default) and current (standard)
        p = app.export_list_csv(str(d / "list.csv"))
        rows = list(csv.DictReader(open(p, encoding="utf-8-sig"), delimiter=";"))
        assert [r["code"].strip('="') for r in rows] == [r[0].raw for r in app.records]; n += 1
        assert rows[0]["source"] == "inv.pdf" and rows[0]["amount_eur"] == "4883,15"; n += 1
        app.fmt_box.current(1); app.fmt_box.event_generate("<<ComboboxSelected>>"); pump(root)
        assert app.csv_fmt.get() == "std"; n += 1
        app.set_input(USER); pump(root)
        p = app.export_current_csv(str(d / "one.csv"))
        rows = list(csv.DictReader(open(p)))
        assert len(rows) == 1 and rows[0]["code"] == USER and rows[0]["amount_eur"] == "50.00"; n += 1
        assert rows[0]["source"] == "photo.jpg"; n += 1
        # 8 Finnish UI for new widgets
        app.lang_var.set("fi"); pump(root)
        assert app.b_load.cget("text") == "Avaa kuva/PDF…" and app.nb.tab(app.list_tab, "text") == "Lista (3)"; n += 1
        assert app.tree.heading("due", "text") == "Eräpäivä"; n += 1
        app.lang_var.set("en"); pump(root)
        # 9 clear list, export empty -> message, no file
        app.list_clear(); assert app.export_list_csv(str(d / "e.csv")) is None and not (d / "e.csv").exists(); n += 1
    return n



def gui_menu_tests(app, root):
    """Menu bar, 4 languages, shortcuts, help windows, settings persistence."""
    n = 0
    tmp = Path(tempfile.mkdtemp())
    orig = (filedialog.asksaveasfilename, filedialog.askopenfilenames)
    try:
        app.set_input(USER); pump(root)
        # 1 every language: menu titles, widgets, window title, language menu entries
        for lang in ("fi", "sv", "nb", "en"):
            app.lang_var.set(lang); pump(root)
            t = T[lang]
            tops = [app.menubar.entrycget(i, "label") for i in range(app.menubar.index("end") + 1)]
            assert tops == [t["m_file"], t["m_edit"], t["m_view"], t["m_lang"], t["m_help"]], (lang, tops); n += 1
            assert app.root.title() == t["title"] and app.b_png.cget("text") == t["save_png"]; n += 1
            assert app.nb.tab(app.main, "text") == t["tab_main"] and app.lbl["due"].cget("text") == t["due"] + ":"; n += 1
            lm = app.menus["lang"]
            assert [lm.entrycget(i, "label") for i in range(4)] == list(LANGUAGES.values()); n += 1
            assert app.menus["file"].entrycget(0, "label") == t["new_form"]; n += 1
            assert app.menus["file"].entrycget(2, "label") == t["load"]; n += 1
        # 2 choosing a language from the menu (radiobutton invoke)
        app.menus["lang"].invoke(2); pump(root)
        assert app.lang == "sv" and app.b_load.cget("text") == "Öppna bild/PDF…"; n += 1
        app.menus["lang"].invoke(0); pump(root); assert app.lang == "en"; n += 1
        # 3 code-dependent menu items follow validity
        fm = app.menus["file"]
        png_idx = next(i for i in range(fm.index("end") + 1)
                       if fm.type(i) == "command" and fm.entrycget(i, "label") == T["en"]["save_png"])
        assert fm.entrycget(png_idx, "state") == "normal"; n += 1
        app.clear(); pump(root); assert fm.entrycget(png_idx, "state") == "disabled"; n += 1
        app.set_input(USER); pump(root); assert fm.entrycget(png_idx, "state") == "normal"; n += 1
        # 4 menu commands work (dialogs stubbed)
        filedialog.asksaveasfilename = lambda **k: str(tmp / ("m." + k["defaultextension"].lstrip(".")))
        fm.invoke(png_idx); assert dec(Image.open(tmp / "m.png")) == USER; n += 1
        idx = next(i for i in range(fm.index("end") + 1)
                   if fm.type(i) == "command" and fm.entrycget(i, "label") == T["en"]["csv_one"])
        fm.invoke(idx); assert USER in (tmp / "m.csv").read_text(encoding="utf-8-sig"); n += 1
        em = app.menus["edit"]
        idx = next(i for i in range(em.index("end") + 1)
                   if em.type(i) == "command" and em.entrycget(i, "label") == "Copy IBAN")
        em.invoke(idx); assert root.clipboard_get() == "FI6014323000208603"; n += 1
        vm = app.menus["view"]; vm.invoke(2); pump(root)
        assert app.nb.select() == str(app.list_tab); vm.invoke(1); pump(root)
        assert app.nb.select() == str(app.create_tab); vm.invoke(0); pump(root); n += 1
        # 5 help windows, every language
        for lang in ("en", "fi", "sv", "nb"):
            app.lang_var.set(lang); pump(root)
            hm = app.menus["help"]            # menus are rebuilt on language change
            for i, must in [(0, T[lang]["guide_text"][:20]), (1, "Ctrl+O"), (2, "49–54"), (4, "pyzbar"),
                            (5, "vcode2bar " + __version__)]:
                hm.invoke(i); pump(root)
                w, text = app.last_help
                assert must in text and w.winfo_exists(), (lang, i); n += 1
                w.destroy()
        app.lang_var.set("en"); pump(root)
        # 6 shortcuts inside the text box: action runs, Tk's Emacs keys do NOT edit the text
        filedialog.askopenfilenames = lambda **k: ()           # user cancels
        filedialog.asksaveasfilename = lambda **k: ""
        app.set_input(USER); pump(root); app.txt.focus_force(); pump(root)
        for key in ("<Control-o>", "<Control-e>", "<Control-l>", "<Control-2>", "<Control-1>"):
            app.txt.event_generate(key); pump(root)
            assert app.get_input() == USER, (key, repr(app.get_input())); n += 1
        before = len(app.records); app.list_clear(); app.txt.event_generate("<Control-l>"); pump(root)
        assert len(app.records) == 1 and before >= 0; n += 1
        app.txt.event_generate("<F1>"); pump(root)
        assert app.last_help[1] == T["en"]["guide_text"]; app.last_help[0].destroy(); n += 1
        app.txt.event_generate("<Escape>"); pump(root); assert app.get_input() == "" and app.vb is None; n += 1
        # 7 settings: default English (even with a Finnish locale), language remembered
        g = globals(); saved_cfg, saved_lang = (g["CONFIG_PATH"], g["LEGACY_CONFIG_PATH"]), os.environ.get("LANG")
        g["CONFIG_PATH"] = tmp / "sub" / "cfg.json"; g["LEGACY_CONFIG_PATH"] = tmp / "none.json"
        os.environ["LANG"] = "fi_FI.UTF-8"
        try:
            r2 = tk.Tk(); a2 = App(r2); pump(r2)
            assert a2.lang == "en"; n += 1
            a2.lang_var.set("nb"); pump(r2); a2.close()
            assert json.loads((tmp / "sub" / "cfg.json").read_text())["lang"] == "nb"; n += 1   # dir created
            r3 = tk.Tk(); a3 = App(r3); pump(r3)
            assert a3.lang == "nb" and a3.b_png.cget("text") == "Lagre PNG"; n += 1
            a3.close()
            (tmp / "sub" / "cfg.json").write_text("{broken")              # corrupt file -> defaults
            r4 = tk.Tk(); a4 = App(r4); pump(r4); assert a4.lang == "en"; a4.close(); n += 1
        finally:
            g["CONFIG_PATH"], g["LEGACY_CONFIG_PATH"] = saved_cfg
            if saved_lang is None:
                os.environ.pop("LANG", None)
            else:
                os.environ["LANG"] = saved_lang
        # 8 PDF page renders Nordic letters with a real font, and decodes
        f, ascii_only = pdf_font(40)
        assert ascii_only or _font_supports(f, "øæåäö€"); n += 1
        app.set_input(USER); app.lang_var.set("nb"); pump(root)
        assert dec(app.make_pdf_page()) == USER; n += 1
        assert ascii_safe("Beløp Æ") == "Belop AE"; n += 1
        app.lang_var.set("en"); pump(root)
    finally:
        filedialog.asksaveasfilename, filedialog.askopenfilenames = orig
        shutil.rmtree(tmp, ignore_errors=True)
    return n


def _cli(args, stdin_text=None):
    """Run main() capturing stdout/stderr; returns (rc, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    if stdin_text is not None:
        sys.stdin = io.StringIO(stdin_text)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = main(args)
            except SystemExit as e:
                rc = e.code
    finally:
        sys.stdin = old_stdin
    return rc, out.getvalue(), err.getvalue()


def _t_create(seed):
    """Builder (inverse of parse), reference helpers, input parsing, CLI switches."""
    rng = random.Random(seed); n = 0
    # 1 all 18 official test invoices rebuilt from their printed fields, in varied input styles
    for pairs, iban, amt, ref, due, _chk in OFFICIAL:
        code = pairs.replace(" ", "")
        ib = rng.choice([iban, " ".join(iban[i:i + 4] for i in range(0, 18, 4)), iban.lower()])
        a = rng.choice([amt, amt.replace(".", ","), f"{Decimal(amt):,.2f}".replace(",", " ").replace(".", ",") + " €"])
        if Decimal(amt) == 0:
            a = rng.choice(["", "0", "0,00"])
        r = rng.choice([ref, " ".join(ref[i:i + 4] for i in range(0, len(ref), 4)), ref.lower()])
        d = "" if due is None else rng.choice([due.strftime("%d.%m.%Y"), due.isoformat(), due])
        assert build_code(ib, a, r, d) == code, (code, ib, a, r, d); n += 1
    # 2 random round trips: build(parse(code).fields) == code
    for _ in range(150):
        code, iban, amt, ref, due = random_case(rng)
        assert build_code(iban, amt, ref, due) == code; n += 1
    # 3 reference helpers produce references that validate
    for _ in range(40):
        base = str(rng.randint(100, 10**19 - 1))
        fi = fi_reference(base)
        assert normalize_reference(fi) == fi and fi.startswith(base); n += 1
        rf = rf_reference(fi); assert _rf_ok(rf) and rf.endswith(fi); n += 1
    assert fi_reference("4090") == "40905" and rf_reference("40905") == "RF1140905"; n += 1
    # 4 amount / date parsing
    for txt, want in [("1 234,56", "1234.56"), ("1.234,56", "1234.56"), ("1,234.56", "1234.56"),
                      ("12,5 €", "12.5"), ("EUR 7", "7"), ("", "0"), ("999999,99", "999999.99")]:
        assert parse_amount(txt) == Decimal(want), txt; n += 1
    for txt, want in [("08.09.2026", date(2026, 9, 8)), ("2026-09-08", date(2026, 9, 8)),
                      ("8.9.2026", date(2026, 9, 8)), ("08.09.26", date(2026, 9, 8)), ("", None)]:
        assert parse_due(txt) == want, txt; n += 1
    # 5 every bad input names the right field
    good = dict(iban="FI6014323000208603", amount="50", reference="40905", due="08.09.2026")
    bad = [("iban", "FI6014323000208604"), ("iban", "SE4550000000058398257466"), ("iban", "FI60"),
           ("amount", "1000000"), ("amount", "12,345"), ("amount", "-5"), ("amount", "abc"),
           ("reference", "40906"), ("reference", "123"), ("reference", "RF18ABC123"),
           ("reference", "RF00123"), ("reference", "1" * 21), ("reference", ""),
           ("due", "31.02.2026"), ("due", "01.01.1999"), ("due", "tomorrow")]
    for fld, val in bad:
        kw = dict(good, **{fld: val})
        try:
            build_code(kw["iban"], kw["amount"], kw["reference"], kw["due"])
            raise AssertionError(f"accepted {fld}={val}")
        except FieldError as e:
            assert e.field == fld, (fld, val, e.field); n += 1
    # 6 CLI switches
    with tempfile.TemporaryDirectory() as t:
        d = Path(t); cwd = os.getcwd(); os.chdir(d)
        try:
            args = ["--create", "--iban", "FI79 4405 2020 0360 82", "--amount", "4883,15",
                    "--ref", "8685 1625 9619 897", "--due", "12.06.2010"]
            rc, out, _ = _cli(args + ["--code-only"])
            assert rc == 0 and out.strip() == OFFICIAL[0][0].replace(" ", ""); n += 1
            rc, out, _ = _cli(args + ["-o", "x", "--format", "all", "--verify"])
            assert rc == 0 and all((d / f"x.{e}").exists() for e in ("png", "svg", "pdf")); n += 1
            assert verify_png(d / "x.png", OFFICIAL[0][0].replace(" ", "")); n += 1
            assert (d / "x.pdf").read_bytes()[:4] == b"%PDF"; n += 1
            rc, out, _ = _cli(args + ["--json"])
            j = json.loads(out); assert rc == 0 and j[0]["reference"] == "868516259619897" and j[0]["source"] == "created"; n += 1
            assert not list(d.glob("viivakoodi*")); n += 1          # --json/--create write no images unasked
            rc, out, _ = _cli(["--ref-fi", "4090"]); assert (rc, out.strip()) == (0, "40905"); n += 1
            rc, out, _ = _cli(["--ref-rf", "40905"]); assert (rc, out.strip()) == (0, "RF1140905"); n += 1
            rc, out, _ = _cli(["--ref-fi", "12"]); assert rc == 2; n += 1
            rc, _, err = _cli(["--create", "--iban", "FI6014323000208604", "--ref", "40905"])
            assert rc == 2 and "(iban)" in err; n += 1
            rc, _, _ = _cli(["--create", "--iban", "FI6014323000208603"]); assert rc == 2; n += 1   # --ref missing
            g = " ".join(USER[i:i + 4] for i in range(0, 54, 4))
            rc, out, _ = _cli(["--decode", "-", "--json"], stdin_text=f"Lasku 45 puh 0401234567\n{g}\nkiitos")
            assert rc == 0 and [r["code"] for r in json.loads(out)] == [USER]; n += 1
            rc, _, _ = _cli(["--decode", "-"], stdin_text="nothing here"); assert rc == 2; n += 1
            rc, out, _ = _cli(["--decode", USER, RF])
            assert rc == 0 and "RF09868516259619897" in out and not list(d.glob("viivakoodi*")); n += 1
            rc, _, _ = _cli([USER]); assert rc == 0 and (d / "viivakoodi.png").exists(); n += 1   # old default kept
            rc, _, _ = _cli([RF, "--format", "pdf", "-o", "p", "--lang", "fi"]); assert (d / "p.pdf").exists(); n += 1
            rc, _, _ = _cli([USER, RF, "-o", "multi", "--format", "png"])
            assert (d / "multi_1.png").exists() and (d / "multi_2.png").exists(); n += 1
            rc, _, _ = _cli(["--create", "--iban", "FI6014323000208603", "--ref", "RF1140905",
                             "--csv", "c.csv"])
            assert rc == 0 and "RF1140905" in (d / "c.csv").read_text(); n += 1
        finally:
            os.chdir(cwd)
    return n


def gui_create_tests(app, root, rng):
    n = 0
    app.lang_var.set("en"); pump(root)
    app.txt.focus_force(); app.txt.event_generate("<Control-n>"); pump(root)
    assert app.nb.select() == str(app.create_tab); n += 1
    fv = app.fv
    # 1 empty form waits, buttons disabled
    app.form_clear(); pump(root)
    assert app.cresult.get() == "" and str(app.b_cshow.cget("state")) == "disabled"; n += 1
    assert app.w_cstatus.cget("text") == T["en"]["form_wait"]; n += 1
    # 2 filling the fields builds the code live
    fv["iban"].set("FI60 1432 3000 2086 03"); fv["amount"].set("50,00")
    fv["reference"].set("4090"); fv["due"].set("08.09.2026"); pump(root)
    assert app.form_code is None and app.ch["reference"].cget("fg") == RED; n += 1     # 4090 lacks check digit
    app.form_add_check(); pump(root)
    assert fv["reference"].get() == "40905" and app.form_code == USER and app.cresult.get() == USER; n += 1
    assert app.w_cstatus.cget("fg") == GREEN and str(app.b_cshow.cget("state")) == "normal"; n += 1
    assert app._cphoto is not None and app._cphoto.width() > 250; n += 1                   # live preview
    assert dec(ImageTk.getimage(app._cphoto).resize((app._cphoto.width() * 3, app._cphoto.height() * 3))) == USER; n += 1
    # 3 convert to RF -> version 5, still valid
    app.form_to_rf(); pump(root)
    assert fv["reference"].get().replace(" ", "") == "RF1140905" and app.form_code.startswith("5"); n += 1
    assert parse(app.form_code).reference == "RF1140905"; n += 1
    app.form_to_rf(); pump(root); assert fv["reference"].get().replace(" ", "") == "RF1140905"; n += 1  # idempotent
    # 4 field errors are shown next to the field and translated
    fv["iban"].set("FI60 1432 3000 2086 04"); pump(root)
    assert app.form_code is None and app.ch["iban"].cget("text") == T["en"]["err_iban"]; n += 1
    assert app._cphoto is None; n += 1                                                       # preview cleared
    app.lang_var.set("sv"); pump(root)
    assert app.ch["iban"].cget("text") == T["sv"]["err_iban"] and app.cl["iban"].cget("text") == "Konto (IBAN):"; n += 1
    assert app.nb.tab(app.create_tab, "text") == "Skapa"; n += 1
    app.lang_var.set("en"); pump(root)
    fv["iban"].set("FI6014323000208603"); fv["amount"].set("1234567"); pump(root)
    assert app.ch["amount"].cget("fg") == RED and app.form_code is None; n += 1
    fv["amount"].set(""); fv["due"].set("31.02.2026"); pump(root)
    assert app.ch["due"].cget("fg") == RED; n += 1
    fv["due"].set(""); pump(root)
    assert app.form_code and parse(app.form_code).amount == 0 and parse(app.form_code).due_date is None; n += 1
    # 5 add check digit on garbage -> message, no change
    fv["reference"].set("12"); app.form_add_check(); pump(root)
    assert fv["reference"].get() == "12" and T["en"]["err_base"] in app.w_cstatus.cget("text"); n += 1
    # 6 show barcode -> Barcode tab, rendered and verified; add to list; copy
    fv["amount"].set("50"); fv["reference"].set("40905"); fv["due"].set("2026-09-08"); pump(root)
    app.form_copy(); assert root.clipboard_get() == USER; n += 1
    app.form_show(); pump(root)
    assert app.nb.select() == str(app.main) and app.vb.raw == USER and app.verified; n += 1
    assert "from Create" in app.w_status.cget("text"); n += 1
    app.list_clear(); app.form_add_to_list(); pump(root)
    assert app.records[-1][0].raw == USER and app.records[-1][1] == "Create"; n += 1
    # 7 random valid forms -> codes identical to independently generated ones
    for _ in range(15):
        code, iban, amt, ref, due = random_case(rng)
        fv["iban"].set(iban); fv["amount"].set(str(amt).replace(".", ","))
        fv["reference"].set(ref); fv["due"].set(due.strftime("%d.%m.%Y") if due else ""); pump(root)
        assert app.form_code == code, (code, app.form_code); n += 1
    app.form_clear(); pump(root)
    return n


def gui_robustness_tests(app, root):
    """A failing render / preview / zbar must be reported in the window, not leave it half-updated."""
    n = 0
    g = globals()
    shown = []
    orig = (g["render_image"], g["_zbar_decode"], messagebox.showerror, App._show_preview)
    try:
        app.lang_var.set("en"); pump(root)
        messagebox.showerror = lambda *a, **k: shown.append(a)
        def broken(*a, **k):
            raise OSError("cannot open resource")
        g["render_image"] = broken
        with contextlib.redirect_stderr(io.StringIO()):
            app.set_input(USER); pump(root)
        assert app.vb is not None and app.img is None; n += 1
        assert "Could not render" in app.w_verify.cget("text") and "cannot open resource" in app.w_verify.cget("text"); n += 1
        assert str(app.b_png.cget("state")) == "disabled" and str(app.b_add.cget("state")) == "normal"; n += 1
        assert str(app.cpy["account"].cget("state")) == "normal"; n += 1
        g["render_image"] = orig[0]
        # preview (ImageTk) failure
        App._show_preview = lambda self: (_ for _ in ()).throw(RuntimeError("no _imagingtk"))
        with contextlib.redirect_stderr(io.StringIO()):
            app.update(); pump(root)
        assert "no _imagingtk" in app.w_verify.cget("text") and str(app.b_png.cget("state")) == "disabled"; n += 1
        App._show_preview = orig[3]
        # zbar failure at decode time -> amber note, barcode still usable
        def zbar_boom(img):
            raise OSError("libzbar.so.0: cannot open shared object file")
        g["_zbar_decode"] = zbar_boom
        with contextlib.redirect_stderr(io.StringIO()):
            app.update(); pump(root)
        assert "libzbar" in app.w_verify.cget("text") and app.w_verify.cget("fg") == AMBER; n += 1
        assert str(app.b_png.cget("state")) == "normal" and app._photo is not None; n += 1
        g["_zbar_decode"] = orig[1]
        # any other callback error -> error dialog instead of silence
        with contextlib.redirect_stderr(io.StringIO()):
            root.report_callback_exception(ValueError, ValueError("boom"), None)
        assert shown and "boom" in shown[-1][1]; n += 1
        app.update(); pump(root); assert app.verified; n += 1
    finally:
        g["render_image"], g["_zbar_decode"], messagebox.showerror, App._show_preview = orig
    return n


def gui_wsl_tests(app, root):
    """WSL integration logic with stubbed Windows tools (cannot run real Windows here)."""
    n = 0
    g = globals()
    calls, popens = [], []
    tmp = Path(tempfile.mkdtemp())
    winroot = tmp / "Users" / "elie"; (winroot / "Downloads").mkdir(parents=True)
    pdf_src = tmp / "invoice.pdf"
    render_image(parse(RF)).convert("RGB").save(pdf_src, "PDF", resolution=600)
    state = {"clip": "image"}
    saved = (g["IS_WSL"], subprocess.run, subprocess.Popen, shutil.which, messagebox.showwarning)

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "wslpath" and cmd[1] == "-w":
            return R(0, "\\\\wsl.localhost\\Ubuntu" + cmd[2].replace("/", "\\") + "\n")
        if cmd[0] == "wslpath" and cmd[1] == "-u":
            return R(0, str(winroot) + "\n" if "USERPROFILE" in cmd[2] or cmd[2] == "C:\\Users\\elie"
                     else str(pdf_src) + "\n")
        if cmd[0] == "cmd.exe":
            return R(0, "C:\\Users\\elie\r\n")
        if cmd[0].endswith("powershell.exe"):
            script = cmd[-1]
            assert "-STA" in cmd and "GetImage" in script and "GetFileDropList" in script
            win = re.search(r"Save\('([^']+)'", script).group(1)
            lin = win.replace("\\\\wsl.localhost\\Ubuntu", "").replace("\\", "/")
            if state["clip"] == "image":
                render_image(parse(USER)).convert("L").resize((1100, 110)).save(lin)
                return R(0, "IMAGE\r\n")
            if state["clip"] == "files":
                return R(0, "FILE:C:\\Users\\elie\\invoice.pdf\r\n")
            return R(3, "")
        raise AssertionError(cmd)

    try:
        g["IS_WSL"] = True
        subprocess.run = fake_run
        subprocess.Popen = lambda cmd, **kw: popens.append(cmd)
        shutil.which = lambda name: f"/mnt/c/Windows/{name}" if name in ("powershell.exe", "explorer.exe") else None
        messagebox.showwarning = lambda *a, **k: None
        wsl_windows_env.cache_clear()
        # 1 detection
        old = os.environ.get("WSL_DISTRO_NAME"); os.environ["WSL_DISTRO_NAME"] = "Ubuntu"
        assert _detect_wsl() or not sys.platform.startswith("linux"); n += 1
        if old is None:
            os.environ.pop("WSL_DISTRO_NAME")
        else:
            os.environ["WSL_DISTRO_NAME"] = old
        # 2 Windows Downloads becomes the default folder
        assert wsl_default_dir() == winroot / "Downloads"; n += 1
        # 3 clipboard image straight from Windows
        app.lang_var.set("en"); pump(root); app.list_clear(); app.clear(); pump(root)
        state["clip"] = "image"; app.paste_image(); pump(root)
        assert app.vb and app.vb.raw == USER and app.records[-1][1] == "clipboard"; n += 1
        # 4 invoice file copied in Windows Explorer
        state["clip"] = "files"; app.paste_image(); pump(root)
        assert app.vb.raw == RF and app.records[-1][1] == "invoice.pdf"; n += 1
        # 5 empty Windows clipboard -> falls back to ImageGrab (patched: nothing there)
        from PIL import ImageGrab
        orig_grab = ImageGrab.grabclipboard
        ImageGrab.grabclipboard = lambda: None
        state["clip"] = "none"; app.paste_image(); pump(root)
        assert "No image" in app.w_msg.cget("text"); n += 1
        ImageGrab.grabclipboard = orig_grab
        # 6 printing opens the Windows default app
        app.set_input(USER); pump(root); popens.clear()
        app.print_pdf()
        assert popens and popens[-1][0] == "explorer.exe" and popens[-1][1].startswith("\\\\wsl.localhost"); n += 1
        shutil.which = lambda name: "/usr/bin/wslview" if name == "wslview" else None
        app.print_pdf(); assert popens[-1][0] == "wslview"; n += 1
        # 7 component report mentions the WSL routes
        shutil.which = lambda name: None
        names = [c[0] for c in component_status()]
        assert any("WSL" in x and "clipboard" in x for x in names) and any("print" in x for x in names); n += 1
    finally:
        (g["IS_WSL"], subprocess.run, subprocess.Popen, shutil.which, messagebox.showwarning) = saved
        wsl_windows_env.cache_clear()
        shutil.rmtree(tmp, ignore_errors=True)
    return n


def _t_platform(seed):
    """Cross-platform behaviour that can be checked on any OS."""
    n = 0
    g = globals()
    # config locations per OS
    saved = (sys.platform, os.environ.get("APPDATA"), os.environ.get("XDG_CONFIG_HOME"))
    try:
        sys.platform = "win32"; os.environ["APPDATA"] = str(Path("C:/Users/x/AppData/Roaming"))
        assert _config_path() == Path("C:/Users/x/AppData/Roaming") / "vcode2bar" / "settings.json"; n += 1
        sys.platform = "linux"; os.environ["XDG_CONFIG_HOME"] = "/cfg"
        assert _config_path() == Path("/cfg/vcode2bar/settings.json"); n += 1
        os.environ.pop("XDG_CONFIG_HOME")
        assert _config_path() == Path.home() / ".config" / "vcode2bar" / "settings.json"; n += 1
        sys.platform = "darwin"
        assert _config_path().parts[-3:] == ("Application Support", "vcode2bar", "settings.json"); n += 1
    finally:
        sys.platform = saved[0]
        for k, v in (("APPDATA", saved[1]), ("XDG_CONFIG_HOME", saved[2])):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    # legacy config is still read when the new one does not exist yet
    with tempfile.TemporaryDirectory() as t:
        d = Path(t); old = (g["CONFIG_PATH"], g["LEGACY_CONFIG_PATH"])
        try:
            g["CONFIG_PATH"], g["LEGACY_CONFIG_PATH"] = d / "new" / "s.json", d / "old.json"
            (d / "old.json").write_text('{"lang": "sv"}')
            assert load_config()["lang"] == "sv"; n += 1
            save_config({"lang": "fi"}); assert load_config()["lang"] == "fi"; n += 1
        finally:
            g["CONFIG_PATH"], g["LEGACY_CONFIG_PATH"] = old
    # console that cannot encode ✓ / ä / € (e.g. Windows cp1252 or ascii pipe) must not crash
    for enc in ("cp1252", "ascii"):
        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding=enc, errors="strict")
        so, se = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = stream
        try:
            rc = main(["--decode", USER])
            print("✓ ↔ €")
            stream.flush()
        finally:
            sys.stdout, sys.stderr = so, se
        out = buf.getvalue()
        assert rc == 0 and b"FI60 1432 3000 2086 03" in out, (enc, out[:80]); n += 1
        if enc == "ascii":
            assert b"Er?p?iv?" in out and b"? ? ?" in out; n += 1     # replaced, not crashed
    # GUI without a display -> friendly message, exit 4 (simulated)
    if tk is not None:
        orig_tk = tk.Tk
        def no_display(*a, **k):
            raise tk.TclError("no display name and no $DISPLAY environment variable")
        tk.Tk = no_display
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                rc = run_gui(persist=False)
        finally:
            tk.Tk = orig_tk
        assert rc == 4 and "command line works without a display" in err.getvalue(); n += 1
    # source stays compatible with Python 3.9 grammar (README minimum)
    import ast
    ast.parse(Path(__file__).read_text(encoding="utf-8"), feature_version=(3, 9)); n += 1
    return n


def selftest(runs: int = 1) -> int:
    """Run the built-in test suites RUNS times each. Needs pyzbar+zbar and pypdfium2;
    the GUI suite also needs a display."""
    missing = [n for n, ok, _ in component_status() if not ok and not n.startswith("clipboard")]
    if missing:
        print("Self-test needs all components; missing: " + ", ".join(missing), file=sys.stderr)
        return 1
    suites = [("core", _t_core, 7919), ("create", _t_create, 3571), ("read/csv", _t_read, 6131),
              ("platform", _t_platform, 1)]
    gui_ok = False
    try:
        r = tk.Tk(); r.destroy(); gui_ok = True
    except Exception as e:  # noqa: BLE001
        print(f"GUI suite skipped (no display: {e})")
    if gui_ok:
        suites.append(("gui", _t_gui, 104729))
    total = 0
    for name, fn, mult in suites:
        for i in range(1, runs + 1):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                n = fn(i * mult)
            total += n
            print(f"{name:9} run {i:2d}/{runs}: PASS ({n} checks)", flush=True)
    print(f"ALL PASS - {total} checks")
    return 0




CLI_EPILOG = """examples:
  vcode2bar.py                                           open the GUI
  vcode2bar.py CODE                                      decode + write viivakoodi.png/.svg
  vcode2bar.py CODE -o lasku --format pdf                A4 PDF with payment details + barcode
  vcode2bar.py --decode CODE [CODE ...] [--json]         decode only (no images)
  vcode2bar.py --create --iban "FI79 4405 2020 0360 82" --amount 4883,15 \\
               --ref 868516259619897 --due 12.06.2010    print the virtual barcode
  vcode2bar.py --create ... -o lasku --format all        ...and write PNG/SVG/PDF
  vcode2bar.py --create ... --code-only                  print only the 54 digits (scripts)
  vcode2bar.py --ref-fi 4090                             Finnish reference with check digit
  vcode2bar.py --ref-rf 40905                            convert to RF creditor reference
  vcode2bar.py --read photo.jpg invoice.pdf --csv out.csv
  cat invoice.txt | vcode2bar.py --decode -              find codes in text from stdin
  vcode2bar.py --deps | --selftest [RUNS] | --version
"""


def _emit(records, as_json):
    if as_json:
        print(json.dumps([to_record(vb, src) for vb, src in records], ensure_ascii=False, indent=2))
        return
    for vb, src in records:
        if len(records) > 1 or src not in ("argument", "created"):
            print(f"--- {src}")
        print(vb.describe())
        if vb.due_date and vb.due_date < date.today():
            print("HUOM: eräpäivä on jo mennyt.")


def _safe_console():
    """Windows consoles/pipes may use cp1252 etc.; replace unprintable characters instead of crashing."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _safe_console()
    p = argparse.ArgumentParser(
        prog="vcode2bar.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Finnish virtual barcode (virtuaaliviivakoodi) <-> bank barcode. "
                    "Without arguments the GUI opens.", epilog=CLI_EPILOG)
    p.add_argument("code", nargs="*", help="virtual barcode(s); '-' reads text from stdin")
    g = p.add_argument_group("modes")
    g.add_argument("--gui", action="store_true", help="open the GUI (optionally pre-filled with CODE)")
    g.add_argument("--decode", "--info-only", dest="decode", action="store_true",
                   help="decode/validate only, write no images")
    g.add_argument("--create", action="store_true", help="build a virtual barcode from --iban/--amount/--ref/--due")
    g.add_argument("--read", nargs="+", metavar="FILE", help="read barcodes from image/PDF files")
    g.add_argument("--ref-fi", metavar="BASE", help="print a Finnish reference: BASE + check digit")
    g.add_argument("--ref-rf", metavar="REF", help="print the RF creditor reference for REF")
    c = p.add_argument_group("create")
    c.add_argument("--iban", help="Finnish IBAN, spaces allowed")
    c.add_argument("--amount", default="", help="amount, e.g. 1234,56 (empty/0 = payer enters it)")
    c.add_argument("--ref", help="Finnish reference or numeric RF reference")
    c.add_argument("--due", default="", help="due date dd.mm.yyyy or yyyy-mm-dd (optional)")
    c.add_argument("--code-only", action="store_true", help="print only the 54-digit code")
    o = p.add_argument_group("output")
    o.add_argument("-o", "--output", help="output file stem (default: viivakoodi)")
    o.add_argument("--format", choices=["png", "svg", "pdf", "both", "all"],
                   help="png, svg, pdf, both (png+svg, default) or all")
    o.add_argument("--text", action="store_true", help="print digits under the bars (spec says not to)")
    o.add_argument("--verify", action="store_true", help="decode the written PNG back and compare")
    o.add_argument("--csv", metavar="FILE", help="write decoded fields to CSV")
    o.add_argument("--excel", action="store_true", help="CSV for Nordic Excel: ';' + decimal comma + BOM")
    o.add_argument("--json", action="store_true", help="machine-readable JSON output")
    o.add_argument("--lang", choices=sorted(T), help="GUI language / PDF text language (default en)")
    m = p.add_argument_group("other")
    m.add_argument("--deps", action="store_true", help="show which optional components are installed")
    m.add_argument("--selftest", nargs="?", const=1, type=int, metavar="RUNS",
                   help="run the built-in test suite RUNS times")
    m.add_argument("--version", action="version", version=f"vcode2bar {__version__}")
    a = p.parse_args(argv)

    if a.selftest:
        return selftest(a.selftest)
    if a.deps:
        for name, ok, detail in component_status():
            print(f"{'OK ' if ok else '-- '} {name}" + (f"  ({detail})" if detail else ""))
        return 0
    try:
        if a.ref_fi:
            print(fi_reference(a.ref_fi)); return 0
        if a.ref_rf:
            print(rf_reference(normalize_reference(a.ref_rf))); return 0
    except VirtualBarcodeError as e:
        print(f"ERROR: {e}", file=sys.stderr); return 2
    if a.gui or not (a.code or a.read or a.create):
        return run_gui(a.code[0] if a.code else None, lang=a.lang)

    records, rc = [], 0
    if a.create:
        missing = [f"--{n}" for n in ("iban", "ref") if not getattr(a, n)]
        if missing:
            p.error("--create needs " + " and ".join(missing))
        try:
            records.append((parse(build_code(a.iban, a.amount, a.ref, a.due)), "created"))
        except FieldError as e:
            print(f"ERROR ({e.field}): {e}", file=sys.stderr); return 2
        if a.code_only:
            print(records[0][0].raw)
    for c_arg in a.code:
        if c_arg == "-":
            found = list(iter_codes(sys.stdin.read()))
            if not found:
                print("ERROR: no valid virtual barcode found on stdin", file=sys.stderr); rc = 2
            records += [(vb, "stdin") for vb in found]
            continue
        try:
            records.append((parse(c_arg), "argument"))
        except VirtualBarcodeError as e:
            print(f"ERROR: {e}", file=sys.stderr); rc = 2
    for fpath in a.read or []:
        r = read_file(fpath)
        print(r.summary(), file=sys.stderr)
        for rej in r.rejected:
            print(f"  ignored: {rej}", file=sys.stderr)
        if r.error and not r.codes:
            rc = rc or 5
        records += [(vb, r.source) for vb in r.codes]

    if not a.code_only:
        _emit(records, a.json)
    if a.csv:
        write_csv([to_record(vb, src) for vb, src in records], a.csv, excel_fi=a.excel)
        print(f"CSV: {a.csv} ({len(records)} rows)", file=sys.stderr if a.json or a.code_only else sys.stdout)

    # images: explicit -o/--format always; a single plain CODE argument keeps the old default
    wants_images = bool(a.output or a.format or a.verify) or (
        len(records) == 1 and records[0][1] == "argument" and not (a.decode or a.csv or a.json or a.read))
    if not wants_images or a.decode:
        return rc
    fmt = a.format or "both"
    formats = {"png": ("png",), "svg": ("svg",), "pdf": ("pdf",), "both": ("png", "svg"),
               "all": ("png", "svg", "pdf")}[fmt]
    if a.verify and "png" not in formats:
        formats += ("png",)
    stem = a.output or "viivakoodi"
    info = sys.stderr if (a.json or a.code_only) else sys.stdout
    for i, (vb, _src) in enumerate(records):
        st = stem if len(records) == 1 else f"{stem}_{i + 1}"
        paths = render(vb, st, tuple(f for f in formats if f != "pdf"), with_text=a.text)
        if "pdf" in formats:
            pdf = Path(f"{st}.pdf"); make_pdf_page(vb, a.lang or "en").save(pdf, "PDF", resolution=600)
            paths.append(pdf)
        for pth in paths:
            print(f"Tallennettu: {pth}", file=info)
        if a.verify:
            png = next(x for x in paths if x.suffix == ".png")
            ok = verify_png(png, vb.raw)
            print("Tarkistus: OK - kuva lukee takaisin samaksi koodiksi" if ok
                  else "Tarkistus: EPÄONNISTUI", file=info)
            if not ok:
                rc = 3
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:            # e.g. `vcode2bar.py --decode CODE --json | head`
        sys.stderr.close()
        sys.exit(0)
