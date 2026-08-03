"""Thin entry: streamlit run app.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from finchat.ui.chat import main

main()
