from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    from galgame_news.desktop.app import main

    raise SystemExit(main())
