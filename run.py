"""启动入口 - 直接在项目根目录运行"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "agents"))
os.chdir(str(ROOT / "agents"))  # 让 agents 内部的相对路径生效

import uvicorn

# 重新指向 api/main.py
sys.path.insert(0, str(ROOT / "api"))

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")
