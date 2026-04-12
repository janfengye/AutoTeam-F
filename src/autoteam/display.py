"""自动设置虚拟显示器（无头服务器）— 在 import 时执行"""

import logging
import os

logger = logging.getLogger(__name__)

if not os.environ.get("DISPLAY"):
    try:
        from xvfbwrapper import Xvfb

        _vdisplay = Xvfb(width=1280, height=800)
        _vdisplay.start()
    except ImportError:
        os.system("Xvfb :99 -screen 0 1280x800x24 &")
        os.environ["DISPLAY"] = ":99"
