"""
启动脚本 - 解决相对导入问题

使用方法:
    python run.py

或:
    uvicorn run:app --reload --host 0.0.0.0 --port 8000
"""

import sys
import os

# 将当前目录添加到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import app

if __name__ == "__main__":
    import uvicorn
    # 使用 import string 方式启动，支持 reload
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, reload_dirs=["."])
