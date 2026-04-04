"""
WSGI entry point for gunicorn
Runs startup() on import (preload), then serves Flask app
"""
from app import app, startup

# 啟動初始化（註冊 webhook、keep-alive 等）
startup()
