#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键打包 Windows 可执行程序 (ZhongdengCrawler.exe) 与免安装分发压缩包 (.zip) 构建脚本

用法：
  1. 默认高速目录版 + 自动生成免安装分发压缩包 dist/ZhongdengCrawler_Windows_x64.zip (推荐):
     python build_exe.py
  2. 仅重新清理配置并打包分发压缩包 (无需重跑 PyInstaller 编译):
     python build_exe.py --zip-only
  3. 单文件便携版:
     python build_exe.py --onefile
"""

import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

USER_GUIDE_TXT = """================================================================
  中登网 · 动产融资担保人批量查询与证明附件采集系统 (免安装版)
================================================================

【一、运行方式】
  1. 请先将压缩包【完整解压】到任意文件夹（切勿直接在压缩包内双击运行，且不要单独移动 ZhongdengCrawler.exe 离开 _internal 文件夹）。
  2. 双击运行「ZhongdengCrawler.exe」即可打开可视化桌面界面（无需安装 Python 或浏览器）。

【二、四步使用流程】
  1. 登录鉴权：在界面上方输入您的中登网登录名与密码 -> 点击「1. 验证账密并发短信」-> 填入手机收到的 6 位短信动态码 -> 点击「2. 提交验证码登录」。
     （登录凭证自动保存在同级 config/session.json，会话未过期前重开软件免重复登录）
  2. 导入名单：在中间文本框直接粘贴企业法定全称（支持换行、英文逗号 , 、中文逗号 ， 分隔），或点击「导入 TXT / CSV 文件」。
  3. 开始采集：点击「开始批量采集」，界面实时展示总体进度、单页 ZIP 压缩包下载进度以及每家企业的详细说明或失败原因。
  4. 暂停/中断与续跑：运行途中可随时点击「暂停任务」或「停止 / 中断」；只要待查企业名单内容不变，再次点击开始会自动继承已完成企业与已下载分页。

【三、输出文件位置】
  默认保存在本目录的「output/」文件夹下：
  - output/<企业名称>/<企业名称>.csv            （登记明细总表，Excel 直接打开无乱码）
  - output/<企业名称>/<企业名称>_分类汇总.csv   （担保类型 / 登记类型 / 担保权人分类占比）
  - output/<企业名称>/附件/*.zip                （每页 10 条记录对应的原始 PDF/XLSX 证明文件压缩包）
  - output/批量任务执行报告.csv                 （全批次成功 / 继承 / 失败原因汇总报告）
"""


def sanitize_and_prepare_release_dir(target_dir: Path):
    """确保分发目录绝不包含任何个人账密、历史登录态或采集数据"""
    cfg_out = target_dir / "config"
    out_dir = target_dir / "output"

    if cfg_out.exists():
        shutil.rmtree(cfg_out)
    if out_dir.exists():
        shutil.rmtree(out_dir)

    cfg_out.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 写入空白账号配置，绝不携带构建者个人账密
    (cfg_out / "account.json").write_text(
        json.dumps({"username": "", "password": ""}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    for fname in ("account.example.json", "targets.example.txt"):
        src = PROJECT_ROOT / "config" / fname
        if src.exists():
            shutil.copy2(src, cfg_out / fname)

    (target_dir / "使用说明.txt").write_text(USER_GUIDE_TXT, encoding="utf-8")


def create_release_zip(folder_to_zip: Path, zip_save_path: Path):
    """将 dist/ZhongdengCrawler 完整打包为保留顶层文件夹的免安装 .zip 压缩包"""
    if zip_save_path.exists():
        zip_save_path.unlink()

    print(f"📦 正在生成免安装分发压缩包: {zip_save_path} ...", flush=True)
    parent = folder_to_zip.parent
    with zipfile.ZipFile(zip_save_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for item in sorted(folder_to_zip.rglob("*")):
            arcname = item.relative_to(parent).as_posix()
            if item.is_dir():
                zf.writestr(arcname + "/", "")
            else:
                zf.write(item, arcname)

    size_mb = zip_save_path.stat().st_size / 1048576
    print(f"✅ 分发压缩包生成完毕！体积: {size_mb:.2f} MB -> {zip_save_path}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="使用 PyInstaller 打包 ZhongdengCrawler 桌面客户端与分发压缩包")
    parser.add_argument("--onefile", action="store_true", help="打包为单个独立 .exe 文件（默认为秒开的 onedir 目录模式）")
    parser.add_argument("--zip-only", action="store_true", help="跳过重新编译，直接脱敏清理现有 dist/ZhongdengCrawler 并重新生成 .zip")
    args = parser.parse_args()

    if not args.zip_only:
        cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--noupx",
            "--windowed",
            "--name",
            "ZhongdengCrawler",
            "--onefile" if args.onefile else "--onedir",
            "--collect-data",
            "ddddocr",
            "--collect-data",
            "certifi",
            "--collect-binaries",
            "onnxruntime",
            "--collect-data",
            "onnxruntime",
            "--collect-binaries",
            "curl_cffi",
            "--collect-data",
            "curl_cffi",
            "--hidden-import",
            "PIL._tkinter_finder",
            "--hidden-import",
            "onnxruntime.capi._pybind_state",
            "--exclude-module",
            "pytest",
            "--exclude-module",
            "matplotlib",
            "--exclude-module",
            "pandas",
            "--exclude-module",
            "scipy",
            "--exclude-module",
            "torch",
            str(PROJECT_ROOT / "gui_app.py"),
        ]
        print("🚀 正在调用 PyInstaller 快速构建 ZhongdengCrawler.exe ...", flush=True)
        subprocess.run(cmd, cwd=str(PROJECT_ROOT), check=True)

    target_dir = (PROJECT_ROOT / "dist") if args.onefile else (PROJECT_ROOT / "dist" / "ZhongdengCrawler")
    exe_path = target_dir / "ZhongdengCrawler.exe"
    if not exe_path.exists():
        raise FileNotFoundError(f"未找到可执行文件: {exe_path}，请先运行不带 --zip-only 的构建！")

    sanitize_and_prepare_release_dir(target_dir)

    if not args.onefile:
        zip_path = PROJECT_ROOT / "dist" / "ZhongdengCrawler_Windows_x64.zip"
        create_release_zip(target_dir, zip_path)

    print("\n" + "═" * 68, flush=True)
    print(f"✅ 构建与隐私清理完成！", flush=True)
    print(f"   • 本地可执行文件: {exe_path}", flush=True)
    if not args.onefile:
        print(f"   • 对外分发压缩包: {PROJECT_ROOT / 'dist' / 'ZhongdengCrawler_Windows_x64.zip'}", flush=True)
    print("═" * 68, flush=True)


if __name__ == "__main__":
    main()
