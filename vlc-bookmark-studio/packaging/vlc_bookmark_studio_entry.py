"""Entry script for the packaged builds (PyInstaller needs a file, not a module)."""
import sys

from bookmark_studio.bootstrap import main

if __name__ == "__main__":
    sys.exit(main())
