"""pytest 引导：把 previz 根目录加进 sys.path，使 `import app` 可用。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
