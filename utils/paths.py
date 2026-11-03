# -*- coding: utf-8 -*-
"""
统一路径解析工具：兼容 Python 源码直接运行与 PyInstaller 打包后的 .exe 运行环境。
确保 config/ 与 output/ 始终位于可执行文件（或项目根目录）同级，保证会话与断点持久化。
"""

import json
import sys
from pathlib import Path

DEFAULT_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.zhongdengwang.org.cn",
    "Referer": "https://www.zhongdengwang.org.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}


def get_app_root() -> Path:
    """获取应用运行根目录（.exe 所在目录或源码项目根目录）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def ensure_runtime_dirs(root: Path | None = None) -> tuple[Path, Path]:
    """确保 config/ 与 output/ 目录及基础 headers.json 存在"""
    base = root or get_app_root()
    config_dir = base / "config"
    output_dir = base / "output"
    config_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    headers_file = config_dir / "headers.json"
    if not headers_file.exists():
        headers_file.write_text(
            json.dumps(DEFAULT_HEADERS, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return config_dir, output_dir
