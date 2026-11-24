#!/usr/bin/env python3
"""本地启动入口：``python run.py`` 然后浏览器打开 http://127.0.0.1:8000"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rivernet.server import main  # noqa: E402

if __name__ == "__main__":
    main()
