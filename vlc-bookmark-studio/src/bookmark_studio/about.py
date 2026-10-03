"""Who owns and made VLC Bookmark Studio, where it lives, and its license (Help menu)."""
from __future__ import annotations

import sys
from pathlib import Path

OWNER = "Elie Koivunen"
DEVELOPERS = "Elie Koivunen and Claude (Anthropic)"
REPOSITORY_URL = "https://github.com/Elie-Koivunen/vibe/tree/main/vlc-bookmark-studio"
RELEASES_URL = "https://github.com/Elie-Koivunen/vibe/releases"
COPYRIGHT = f"Copyright © 2026 {OWNER}. All rights reserved."
LICENSE_SUMMARY = (
    "Proprietary pre-release software: you may install and run it to evaluate and test "
    "it; any other use -- copying, changing or sharing it -- needs the owner's written "
    "permission. The final version will be released under an open-source license. "
    "Versions 0.1.0-0.8.0 remain under the GNU GPL v3."
)


def _license_candidates() -> list[Path]:
    here = Path(__file__).resolve().parent
    candidates = [here / "resources" / "LICENSE", here / "resources" / "LICENSE.txt"]
    frozen_dir = getattr(sys, "_MEIPASS", None)
    if frozen_dir:  # a packaged build: next to the program too
        candidates += [Path(frozen_dir) / "bookmark_studio" / "resources" / "LICENSE",
                       Path(sys.executable).resolve().parent / "LICENSE.txt"]
    candidates.append(here.parents[1] / "LICENSE")  # running from the source tree
    return candidates


def license_text() -> str:
    """The full license (the LICENSE file); its summary if the file isn't to be found."""
    for candidate in _license_candidates():
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        except OSError:
            continue
    return f"{COPYRIGHT}\n\n{LICENSE_SUMMARY}\n\n{REPOSITORY_URL}\n"
