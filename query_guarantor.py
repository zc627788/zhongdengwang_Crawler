#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中登网 - 第二步：按担保人（机构名称）批量轮询查询与分页附件压缩包下载入口

支持输入方式：
  1. 默认读取 config/targets.txt（支持每行一个、英文逗号或中文逗号分隔，也可指定 .csv 文件）
  2. 命令行直接传参：python query_guarantor.py --targets "TCL华星光电技术有限公司,珠海格力电器股份有限公司"
  3. 指定自定义列表文件：python query_guarantor.py --file my_companies.csv

自动会话保障 & 断点继承：
  - 启动前自动在线核验 config/session.json 是否过期
  - 当输入企业列表与采集参数内容未变时，自动继承已完成企业及半途中断的分页进度（加 --force 可强制重采）
  - 每次执行完毕或中断后自动输出 output/批量任务执行报告.csv，清晰说明成功/继承/失败原因
"""

import argparse
import random
import sys
from pathlib import Path

from main import load_account
from utils.auth import ZhongdengAuthClient
from utils.guarantor_query import (
    GuarantorQueryScraper,
    TaskStoppedError,
    classify_error_reason,
    compute_task_signature,
    parse_target_list,
    write_batch_report_csv,
)
from utils.paths import ensure_runtime_dirs, get_app_root

BASE_DIR = get_app_root()
CONFIG_DIR, DEFAULT_OUTPUT_DIR = ensure_runtime_dirs(BASE_DIR)
DEFAULT_TARGETS_FILE = CONFIG_DIR / "targets.txt"


def ensure_authenticated(client: ZhongdengAuthClient, sms_code: str = "", auto_send_only: bool = False) -> dict:
    """确保开始采集前拥有合法有效的登录会话"""
    if sms_code:
        print("🔐 正在提交手机短信动态码完成登录...")
        return client.complete_sms_step(sms_code)

    saved = client.load_valid_session()
    if saved:
        print(f"✅ 登录态检查通过！当前在线账号: {saved.get('username')} (保存于 {saved.get('saved_at')})")
        return saved

    print("⚠️ 当前本地登录态不存在或已过期，正在启动自动预登录并发送手机验证码...")
    username, password = load_account()

    if auto_send_only or not sys.stdin.isatty():
        res = client.send_sms_step(
            username=username,
            password=password,
            allow_manual_fallback=False,
            ask_before_send=False,
        )
        if res["status"] == "done":
            return res["session"]
        print("\n📩 已触发手机短信动态码！请收到后执行:")
        print("   python query_guarantor.py --code <你的6位动态码>")
        sys.exit(0)

    return client.login(username=username, password=password)


def main():
    parser = argparse.ArgumentParser(description="中登网 - 按担保人名称批量轮询采集与附件下载工具")
    parser.add_argument("--targets", type=str, default="", help="机构名称列表（支持英文/中文逗号或换行分隔）")
    parser.add_argument("--file", type=str, default=str(DEFAULT_TARGETS_FILE), help="机构名称列表文件路径 (.txt / .csv)")
    parser.add_argument("--output", type=str, default=str(DEFAULT_OUTPUT_DIR), help="输出根目录 (默认 ./output)")
    parser.add_argument("--reason", type=str, default="01", help="查询原因编码 (默认 '01' 第一个选项)")
    parser.add_argument("--code", type=str, default="", help="若刚触发过登录短信，传入6位短信动态码完成登录并立即开始查询")
    parser.add_argument("--auto-send-sms", action="store_true", help="非交互模式下若登录过期则自动发短信后暂停等待 --code")
    parser.add_argument("--no-download", action="store_true", help="仅导出 CSV 表格与分类统计，跳过下载每页 PDF 压缩包")
    parser.add_argument("--force", action="store_true", help="忽略本地断点缓存，强制全量重新采集")
    args = parser.parse_args()

    print("═" * 68)
    print("  动产融资统一登记公示系统 - 按担保人查询与分页附件采集工具")
    print("═" * 68)

    # 1. 解析目标企业列表
    if args.targets.strip():
        targets = parse_target_list(args.targets)
    else:
        fpath = Path(args.file)
        if not fpath.exists():
            print(f"⚠️ 未找到目标列表文件: {fpath}，请先创建或使用 --targets 指定企业名称。")
            sys.exit(1)
        targets = parse_target_list(fpath.read_text(encoding="utf-8"))

    if not targets:
        print("⚠️ 解析到的目标机构名称列表为空，请检查输入内容！")
        sys.exit(1)

    print(f"📋 本次待轮询机构共 {len(targets)} 家: {targets}")

    # 2. 检查/续期登录会话
    auth_client = ZhongdengAuthClient(CONFIG_DIR)
    saved_session = ensure_authenticated(
        auth_client,
        sms_code=args.code,
        auto_send_only=args.auto_send_sms,
    )

    # 3. 初始化采集器与内容未变断点继承校验
    output_root = Path(args.output)
    download_zip = not args.no_download
    scraper = GuarantorQueryScraper(
        auth_client=auth_client,
        output_root=output_root,
        page_size=10,
        min_delay=2.5,
        max_delay=5.5,
    )
    scraper.bind_session(saved_session)

    signature = compute_task_signature(targets, query_reason=args.reason, download_zip=download_zip)
    same_content = scraper.checkpoint.prepare_for_task(
        targets=targets,
        signature=signature,
        allow_inherit=not args.force,
    )
    effective_inherit = (not args.force) and same_content
    if effective_inherit:
        print("♻️  检测到任务输入内容未发生变化，已启用历史断点与本地文件继承模式！")
    elif args.force:
        print("🔄 已指定 --force，本次将忽略历史断点全量重新采集。")

    batch_summary = []
    try:
        for idx, org_name in enumerate(targets, start=1):
            print(f"\n🚀 [总进度 {idx}/{len(targets)}] 正在处理: {org_name}")
            try:
                res = scraper.process_company(
                    org_name=org_name,
                    query_reason=args.reason,
                    download_zip=download_zip,
                    allow_inherit=effective_inherit,
                )
                status_lbl = "已继承完成" if res.get("inherited") else "已完成"
                batch_summary.append({"orgName": org_name, "status": "ok", "status_label": status_lbl, **res})
            except PermissionError as exc:
                print(f"\n⚠️ 运行途中登录态过期: {exc}")
                saved_session = ensure_authenticated(auth_client, auto_send_only=args.auto_send_sms)
                scraper.bind_session(saved_session)
                res = scraper.process_company(
                    org_name=org_name,
                    query_reason=args.reason,
                    download_zip=download_zip,
                    allow_inherit=True,
                )
                batch_summary.append({"orgName": org_name, "status": "ok", "status_label": "已完成", **res})
            except (KeyboardInterrupt, TaskStoppedError) as exc:
                reason = "用户主动中断任务（已完成进度已保存，内容不变时可直接续跑）"
                print(f"\n⏹️ {reason}: {exc}")
                batch_summary.append({
                    "orgName": org_name,
                    "status": "stopped",
                    "status_label": "已中断",
                    "note": reason,
                })
                break
            except Exception as exc:
                reason = classify_error_reason(exc)
                scraper.checkpoint.mark_company_failed(org_name, reason)
                print(f"\n❌ 企业 [{org_name}] 处理失败: {reason}")
                batch_summary.append({
                    "orgName": org_name,
                    "status": "error",
                    "status_label": "失败",
                    "note": reason,
                })

            if idx < len(targets) and not batch_summary[-1].get("inherited"):
                rest = random.uniform(6.0, 11.0)
                print(f"\n⏳ 已完成第 {idx} 家企业，拟人停顿 {rest:.1f}s 后查询下一家...")
                scraper.controller.sleep(rest)
    except KeyboardInterrupt:
        print("\n⏹️ 捕获到 Ctrl+C 中断信号，正在保存执行报告...")

    report_csv = output_root / "批量任务执行报告.csv"
    write_batch_report_csv(report_csv, batch_summary)

    print("\n" + "═" * 68)
    print("  ✅ 批量轮询任务执行结束！")
    for item in batch_summary:
        if item["status"] == "ok":
            tag = "继承跳过" if item.get("inherited") else "采集落盘"
            print(f"   • [{tag}] {item['orgName']}: 共 {item.get('total', 0)} 笔登记 ({item.get('pages', 0)} 页)")
        else:
            print(f"   • [{item.get('status_label', '异常')}] {item['orgName']}: {item.get('note')}")
    print(f"  📑 批次状态与失败说明报告: {report_csv}")
    print("═" * 68)


if __name__ == "__main__":
    main()
