#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键打包 Windows 可执行程序 (ZhongdengCrawler.exe) 构建脚本

用法：
  1. 默认高速目录版 (推荐，秒开且包含 _internal 依赖目录):
     python build_exe.py
  2. 单文件便携版:
     python build_exe.py --onefile
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="使用 PyInstaller 打包 ZhongdengCrawler 桌面客户端")
    parser.add_argument("--onefile", action="store_true", help="打包为单个独立 .exe 文件（默认为秒开的 onedir 目录模式）")
    args = parser.parse_args()

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        "ZhongdengCrawler",
        "--onefile" if args.onefile else "--onedir",
        "--collect-all",
        "ddddocr",
        "--collect-all",
        "onnxruntime",
        "--collect-all",
        "curl_cffi",
        "--hidden-import",
        "PIL._tkinter_finder",
        str(PROJECT_ROOT / "gui_app.py"),
    ]

    print("🚀 正在调用 PyInstaller 构建 ZhongdengCrawler.exe ...")
    print("   命令:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)

    target_dir = (PROJECT_ROOT / "dist") if args.onefile else (PROJECT_ROOT / "dist" / "ZhongdengCrawler")
    cfg_out = target_dir / "config"
    cfg_out.mkdir(parents=True, exist_ok=True)
    (target_dir / "output").mkdir(parents=True, exist_ok=True)

    for fname in ("account.example.json", "targets.example.txt"):
        src = PROJECT_ROOT / "config" / fname
        if src.exists():
            shutil.copy2(src, cfg_out / fname)

    exe_path = target_dir / "ZhongdengCrawler.exe"
    print("\n" + "═" * 64)
    print(f"✅ 打包成功！可执行文件路径:\n   {exe_path}")
    print("═" * 64)


if __name__ == "__main__":
    main()
