#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中登网（动产融资统一登记公示系统）纯协议登录与登录态管理入口

用法：
  1. 完整交互登录:   python main.py
  2. 仅触发发短信:   python main.py --send-only
  3. 提交短信动态码: python main.py --code 123456
  4. 强制重新登录:   python main.py --force
"""

import argparse
import json
import sys
from pathlib import Path

from utils.auth import ZhongdengAuthClient
from utils.paths import ensure_runtime_dirs, get_app_root

BASE_DIR = get_app_root()
CONFIG_DIR, _ = ensure_runtime_dirs(BASE_DIR)
ACCOUNT_FILE = CONFIG_DIR / "account.json"


def load_account() -> tuple[str, str]:
    if not ACCOUNT_FILE.exists():
        ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACCOUNT_FILE.write_text(
            json.dumps({"username": "", "password": ""}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"⚠️ 已自动创建账号配置文件: {ACCOUNT_FILE}，请填入 username 和 password 后重试。")
        sys.exit(1)

    data = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        print(f"⚠️ 请先在 {ACCOUNT_FILE} 中完整填写 username 与 password！")
        sys.exit(1)

    return username, password


def main():
    parser = argparse.ArgumentParser(description="中登网纯协议登录与会话持久化工具")
    parser.add_argument("--force", action="store_true", help="忽略本地已有 session.json，强制重新登录")
    parser.add_argument("--send-only", action="store_true", help="过图形校验码并下发手机短信动态码后暂停")
    parser.add_argument("--code", type=str, default="", help="直接提交收到的手机短信动态码完成登录")
    args = parser.parse_args()

    print("═" * 60)
    print("  动产融资统一登记公示系统 (zhongdengwang.org.cn) 登录工具")
    print("═" * 60)

    client = ZhongdengAuthClient(CONFIG_DIR)

    if args.code:
        try:
            saved = client.complete_sms_step(args.code)
            print("\n" + "─" * 60)
            print(f"🔑 最终 accessToken: {saved['accessToken'][:40]}...")
            if saved.get("userInfo"):
                print(f"📋 当前登录用户详情: {json.dumps(saved['userInfo'], ensure_ascii=False)}")
            return
        except Exception as exc:
            print(f"\n❌ 提交短信验证码失败: {exc}")
            sys.exit(1)

    if not args.force and not args.send_only:
        existing = client.load_valid_session()
        if existing:
            print(f"✅ 检测到本地有效登录态 (保存时间: {existing.get('saved_at')})")
            print(f"👤 登录账号: {existing.get('username')}")
            print(f"🔑 accessToken: {existing.get('accessToken')[:36]}...")
            if existing.get("userInfo"):
                print(f"📋 用户信息摘要: {json.dumps(existing['userInfo'], ensure_ascii=False)[:200]}")
            return

    username, password = load_account()
    try:
        if args.send_only:
            res = client.send_sms_step(
                username=username,
                password=password,
                allow_manual_fallback=False,
                ask_before_send=False,
            )
            if res["status"] == "sms_sent":
                print(f"\n⏳ 待验证状态已保存至 config/login_pending.json，收到短信后请提供验证码或运行: python main.py --code <动态码>")
            return

        saved = client.login(username=username, password=password)
        print("\n" + "─" * 60)
        print(f"🔑 最终 accessToken: {saved['accessToken'][:40]}...")
        if saved.get("userInfo"):
            print(f"📋 当前登录用户详情: {json.dumps(saved['userInfo'], ensure_ascii=False)}")
        print("💡 后续业务请求只需在 Header 中携带: {'accessToken': session['accessToken']}")
    except Exception as exc:
        print(f"\n❌ 登录未完成: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
