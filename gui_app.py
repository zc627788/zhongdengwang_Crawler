#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中登网（动产融资统一登记公示系统）桌面可视化采集终端 (GUI / EXE 入口)

核心特性：
  1. 登录与会话闭环：自动检测本地 session.json 有效性；支持输入账密 -> 自动过图形码发短信 -> 填入短信码登录（含人工图形码弹窗兜底）
  2. 多格式目标导入：支持粘贴换行、英文逗号(,)、中文逗号(，)分隔的企业名称，或一键导入 .txt / .csv 名单（兼容 UTF-8 / GBK）
  3. 实时进度与失败归因：双进度条（总企业进度 + 单页附件流式下载 MB/% 进度）+ 状态明细表格（明确展示每家企业的成功/继承/失败具体原因）
  4. 暂停 / 继续 / 中断 & 内容不变自动继承：毫秒级响应暂停与中断；当输入列表与采集配置未变时自动继承已完成企业及半途分页进度
"""

import argparse
import ctypes
import json
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import List, Optional

from PIL import Image, ImageTk

from utils.auth import ZhongdengAuthClient
from utils.guarantor_query import (
    CheckpointManager,
    GuarantorQueryScraper,
    TaskController,
    TaskStoppedError,
    classify_error_reason,
    compute_task_signature,
    parse_target_list,
    sanitize_folder_name,
    write_batch_report_csv,
)
from utils.paths import ensure_runtime_dirs, get_app_root
from utils.sign import build_password_digests

# 开启 Windows 高分屏 DPI 感知，防止打包成 exe 后字体模糊
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

QUERY_REASON_OPTIONS = [
    ("01 - 担保业务展业前查询 (推荐默认)", "01"),
    ("02 - 担保业务存续期管理查询", "02"),
    ("03 - 其他法定查询事由", "03"),
]


def read_text_smart(file_path: Path) -> str:
    """兼容 UTF-8-SIG / UTF-8 / GB18030 读取本地 TXT 或 CSV"""
    raw = file_path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


class ZhongdengCrawlerGUI:
    def __init__(self, root: tk.Tk, skip_auto_session_check: bool = False):
        self.root = root
        self.root.title("中登网 · 动产融资担保人批量查询与证明附件采集系统 v1.0")
        self.root.geometry("1160x840")
        self.root.minsize(1020, 740)

        self.app_root = get_app_root()
        self.config_dir, self.default_output_dir = ensure_runtime_dirs(self.app_root)
        self.account_file = self.config_dir / "account.json"
        self.targets_file = self.config_dir / "targets.txt"
        self.checkpoint_file = self.config_dir / "task_checkpoint.json"

        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.controller = TaskController()
        self.checkpoint_mgr = CheckpointManager(self.checkpoint_file)

        self.auth_client = ZhongdengAuthClient(
            config_dir=self.config_dir,
            logger=self.enqueue_log,
            manual_captcha_fn=self.prompt_captcha_modal_threadsafe,
        )

        self.current_session: Optional[dict] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.sms_countdown_left: int = 0
        self.row_id_by_org: dict[str, str] = {}

        self._setup_styles()
        self._build_layout()
        self._load_local_configs()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._poll_ui_queue)
        # 启动后异步核验本地登录态
        if not skip_auto_session_check:
            threading.Thread(target=self._bg_check_session, daemon=True).start()

    # ───────────────────────── UI 样式与布局构建 ─────────────────────────

    def _setup_styles(self):
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")

        default_font = ("Microsoft YaHei UI", 10)
        bold_font = ("Microsoft YaHei UI", 10, "bold")
        style.configure(".", font=default_font)
        style.configure("TLabelframe.Label", font=bold_font, foreground="#1e3a8a")
        style.configure("Primary.TButton", font=bold_font, padding=(12, 5))
        style.configure("Action.TButton", padding=(10, 5))
        style.configure("Treeview", rowheight=28, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    def _build_layout(self):
        outer = ttk.Frame(self.root, padding=(12, 10, 12, 10))
        outer.pack(fill=tk.BOTH, expand=True)

        # 1. 顶部标题与会话状态栏
        header = ttk.Frame(outer)
        header.pack(fill=tk.X, pady=(0, 8))

        title_lbl = tk.Label(
            header,
            text="🏛️ 动产融资统一登记公示系统 · 批量查询与证明附件采集终端",
            font=("Microsoft YaHei UI", 13, "bold"),
            fg="#991b1b",
        )
        title_lbl.pack(side=tk.LEFT)

        self.session_badge = tk.Label(
            header,
            text=" ● 正在检测本地登录会话... ",
            font=("Microsoft YaHei UI", 9, "bold"),
            bg="#fef3c7",
            fg="#92400e",
            padx=10,
            pady=4,
            relief=tk.FLAT,
        )
        self.session_badge.pack(side=tk.RIGHT)

        # 2. 登录与短信验证区
        auth_box = ttk.LabelFrame(outer, text=" 🔐 第一步：账号登录与短信动态码鉴权（会话未过期时自动复用） ", padding=(10, 8))
        auth_box.pack(fill=tk.X, pady=(0, 8))

        row1 = ttk.Frame(auth_box)
        row1.pack(fill=tk.X)

        ttk.Label(row1, text="登录名:").pack(side=tk.LEFT)
        self.username_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.username_var, width=16).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(row1, text="密码:").pack(side=tk.LEFT)
        self.password_var = tk.StringVar()
        self.pwd_entry = ttk.Entry(row1, textvariable=self.password_var, show="●", width=16)
        self.pwd_entry.pack(side=tk.LEFT, padx=(4, 6))

        self.show_pwd_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row1,
            text="显示",
            variable=self.show_pwd_var,
            command=lambda: self.pwd_entry.config(show="" if self.show_pwd_var.get() else "●"),
        ).pack(side=tk.LEFT, padx=(0, 8))

        self.save_acc_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="记住账密", variable=self.save_acc_var).pack(side=tk.LEFT, padx=(0, 12))

        self.btn_send_sms = ttk.Button(
            row1,
            text="1. 验证账密并发短信",
            style="Action.TButton",
            command=self._on_click_send_sms,
        )
        self.btn_send_sms.pack(side=tk.LEFT, padx=(0, 14))

        ttk.Separator(row1, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        ttk.Label(row1, text="短信动态码:").pack(side=tk.LEFT, padx=(6, 4))
        self.sms_code_var = tk.StringVar()
        self.sms_entry = ttk.Entry(row1, textvariable=self.sms_code_var, width=10)
        self.sms_entry.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_verify_sms = ttk.Button(
            row1,
            text="2. 提交验证码登录",
            style="Primary.TButton",
            command=self._on_click_verify_sms,
        )
        self.btn_verify_sms.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_check_sess = ttk.Button(
            row1,
            text="🔄 检测会话",
            command=lambda: threading.Thread(target=self._bg_check_session, daemon=True).start(),
        )
        self.btn_check_sess.pack(side=tk.RIGHT)

        # 3. 目标输入与任务参数区
        input_box = ttk.LabelFrame(
            outer,
            text=" 📋 第二步：导入或输入待查询企业名单（支持换行、英文逗号 , 、中文逗号 ， 分隔） ",
            padding=(10, 8),
        )
        input_box.pack(fill=tk.X, pady=(0, 8))

        toolbar = ttk.Frame(input_box)
        toolbar.pack(fill=tk.X, pady=(0, 6))

        ttk.Button(toolbar, text="📂 导入 TXT / CSV 文件", command=self._on_import_file).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="💾 保存为默认名单", command=self._on_save_targets).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="🧹 清空名单", command=self._on_clear_targets).pack(side=tk.LEFT, padx=(0, 12))

        self.parsed_count_lbl = ttk.Label(
            toolbar,
            text="已识别有效去重企业：0 家",
            foreground="#1d4ed8",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.parsed_count_lbl.pack(side=tk.LEFT, padx=6)

        ttk.Button(toolbar, text="📁 打开输出目录", command=self._open_output_root).pack(side=tk.RIGHT)

        self.targets_text = scrolledtext.ScrolledText(
            input_box,
            height=4,
            font=("Microsoft YaHei UI", 10),
            wrap=tk.WORD,
        )
        self.targets_text.pack(fill=tk.X, pady=(0, 6))
        self.targets_text.bind("<KeyRelease>", lambda _e: self._update_parsed_count())

        opt_row = ttk.Frame(input_box)
        opt_row.pack(fill=tk.X)

        ttk.Label(opt_row, text="查询原因:").pack(side=tk.LEFT)
        self.reason_var = tk.StringVar(value=QUERY_REASON_OPTIONS[0][0])
        reason_cb = ttk.Combobox(
            opt_row,
            textvariable=self.reason_var,
            values=[x[0] for x in QUERY_REASON_OPTIONS],
            state="readonly",
            width=28,
        )
        reason_cb.pack(side=tk.LEFT, padx=(4, 14))

        self.download_zip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt_row,
            text="下载每页证明文件压缩包(.zip)",
            variable=self.download_zip_var,
        ).pack(side=tk.LEFT, padx=(0, 14))

        self.inherit_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt_row,
            text="内容不变时继承已完成进度(断点续传)",
            variable=self.inherit_var,
        ).pack(side=tk.LEFT, padx=(0, 14))

        ttk.Label(opt_row, text="保存目录:").pack(side=tk.LEFT)
        self.output_dir_var = tk.StringVar(value=str(self.default_output_dir))
        ttk.Entry(opt_row, textvariable=self.output_dir_var, width=26).pack(side=tk.LEFT, padx=(4, 4), fill=tk.X, expand=True)
        ttk.Button(opt_row, text="选择...", width=7, command=self._on_pick_output_dir).pack(side=tk.LEFT)

        # 4. 执行控制、进度条、状态表格与日志区
        exec_box = ttk.LabelFrame(
            outer,
            text=" 🚀 第三步：批量轮询控制、实时进度监控与失败原因追踪（双击表格行可直接打开对应企业文件夹） ",
            padding=(10, 8),
        )
        exec_box.pack(fill=tk.BOTH, expand=True)

        ctrl_row = ttk.Frame(exec_box)
        ctrl_row.pack(fill=tk.X, pady=(0, 8))

        self.btn_start = ttk.Button(
            ctrl_row,
            text="▶ 开始批量采集",
            style="Primary.TButton",
            command=self._on_click_start,
        )
        self.btn_start.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_pause = ttk.Button(
            ctrl_row,
            text="⏸ 暂停任务",
            style="Action.TButton",
            state=tk.DISABLED,
            command=self._on_click_pause_resume,
        )
        self.btn_pause.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_stop = ttk.Button(
            ctrl_row,
            text="⏹ 停止 / 中断",
            style="Action.TButton",
            state=tk.DISABLED,
            command=self._on_click_stop,
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 12))

        self.btn_reset_cp = ttk.Button(
            ctrl_row,
            text="🔄 重置历史断点",
            command=self._on_click_reset_checkpoint,
        )
        self.btn_reset_cp.pack(side=tk.LEFT)

        self.task_state_lbl = ttk.Label(
            ctrl_row,
            text="当前状态：就绪",
            font=("Microsoft YaHei UI", 9, "bold"),
            foreground="#374151",
        )
        self.task_state_lbl.pack(side=tk.RIGHT)

        # 双进度条
        prog_frame = ttk.Frame(exec_box)
        prog_frame.pack(fill=tk.X, pady=(0, 8))
        prog_frame.columnconfigure(1, weight=1)
        prog_frame.columnconfigure(3, weight=1)

        ttk.Label(prog_frame, text="总体企业进度:").grid(row=0, column=0, sticky=tk.W)
        self.overall_prog = ttk.Progressbar(prog_frame, orient=tk.HORIZONTAL, mode="determinate", maximum=100)
        self.overall_prog.grid(row=0, column=1, sticky=tk.EW, padx=(6, 12))
        self.overall_lbl = ttk.Label(prog_frame, text="0 / 0 (0%)", width=16)
        self.overall_lbl.grid(row=0, column=2, sticky=tk.W)

        ttk.Label(prog_frame, text="当前页附件下载:").grid(row=1, column=0, sticky=tk.W, pady=(4, 0))
        self.dl_prog = ttk.Progressbar(prog_frame, orient=tk.HORIZONTAL, mode="determinate", maximum=100)
        self.dl_prog.grid(row=1, column=1, sticky=tk.EW, padx=(6, 12), pady=(4, 0))
        self.dl_lbl = ttk.Label(prog_frame, text="等待开始...", width=40)
        self.dl_lbl.grid(row=1, column=2, columnspan=2, sticky=tk.W, pady=(4, 0))

        # 上下分栏：上方企业状态表，下方实时滚动日志
        paned = ttk.PanedWindow(exec_box, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)

        table_frame = ttk.Frame(paned)
        cols = ("seq", "org", "status", "total", "pages", "note")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=8)
        self.tree.heading("seq", text="序号")
        self.tree.heading("org", text="企业名称 (担保人)")
        self.tree.heading("status", text="执行状态")
        self.tree.heading("total", text="登记笔数")
        self.tree.heading("pages", text="分页进度")
        self.tree.heading("note", text="详细说明 / 失败与中断原因")

        self.tree.column("seq", width=55, anchor=tk.CENTER, stretch=False)
        self.tree.column("org", width=260, anchor=tk.W, stretch=False)
        self.tree.column("status", width=110, anchor=tk.CENTER, stretch=False)
        self.tree.column("total", width=85, anchor=tk.CENTER, stretch=False)
        self.tree.column("pages", width=90, anchor=tk.CENTER, stretch=False)
        self.tree.column("note", width=480, anchor=tk.W, stretch=True)

        self.tree.tag_configure("done", background="#ecfdf5", foreground="#065f46")
        self.tree.tag_configure("inherited", background="#eff6ff", foreground="#1e40af")
        self.tree.tag_configure("running", background="#fffbeb", foreground="#92400e")
        self.tree.tag_configure("failed", background="#fef2f2", foreground="#991b1b")
        self.tree.tag_configure("stopped", background="#f3f4f6", foreground="#374151")

        y_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=y_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", self._on_double_click_row)
        paned.add(table_frame, weight=3)

        log_frame = ttk.Frame(paned)
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=8,
            font=("Consolas", 9),
            bg="#0f172a",
            fg="#e2e8f0",
            insertbackground="#e2e8f0",
            state=tk.DISABLED,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        paned.add(log_frame, weight=2)

    # ───────────────────────── 本地配置加载与辅助 ─────────────────────────

    def _load_local_configs(self):
        if self.account_file.exists():
            try:
                acc = json.loads(self.account_file.read_text(encoding="utf-8"))
                self.username_var.set(acc.get("username", ""))
                self.password_var.set(acc.get("password", ""))
            except Exception:
                pass

        if self.targets_file.exists():
            try:
                txt = read_text_smart(self.targets_file)
                self.targets_text.delete("1.0", tk.END)
                self.targets_text.insert("1.0", txt.strip())
            except Exception:
                pass
        else:
            self.targets_text.insert("1.0", "TCL华星光电技术有限公司")
        self._update_parsed_count()

    def _save_account_if_needed(self):
        if not self.save_acc_var.get():
            return
        payload = {
            "username": self.username_var.get().strip(),
            "password": self.password_var.get(),
        }
        self.account_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _get_selected_reason_code(self) -> str:
        val = self.reason_var.get()
        for label, code in QUERY_REASON_OPTIONS:
            if val == label:
                return code
        return "01"

    def _get_parsed_targets(self) -> List[str]:
        raw = self.targets_text.get("1.0", tk.END)
        return parse_target_list(raw)

    def _update_parsed_count(self):
        targets = self._get_parsed_targets()
        self.parsed_count_lbl.config(text=f"已识别有效去重企业：{len(targets)} 家")

    # ───────────────────────── 跨线程 UI 消息队列 ─────────────────────────

    def enqueue_log(self, msg: str):
        self.ui_queue.put(("log", msg))

    def _append_log_ui(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        clean = msg.lstrip("\n")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{ts}] {clean}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _set_session_badge_ui(self, kind: str, text: str):
        palette = {
            "ok": ("#dcfce7", "#166534"),
            "sms": ("#dbeafe", "#1e40af"),
            "warn": ("#fee2e2", "#991b1b"),
            "checking": ("#fef3c7", "#92400e"),
        }
        bg, fg = palette.get(kind, ("#f3f4f6", "#374151"))
        self.session_badge.config(text=f" {text} ", bg=bg, fg=fg)

    def _poll_ui_queue(self):
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log_ui(item[1])
                elif kind == "badge":
                    self._set_session_badge_ui(item[1], item[2])
                elif kind == "sms_sent_ok":
                    self._start_sms_countdown(120)
                    self.sms_entry.focus_set()
                elif kind == "enable_send_btn":
                    if self.sms_countdown_left <= 0:
                        self.btn_send_sms.config(state=tk.NORMAL, text="1. 验证账密并发短信")
                elif kind == "enable_verify_btn":
                    self.btn_verify_sms.config(state=tk.NORMAL)
                elif kind == "company_progress":
                    self._update_company_row_ui(item[1])
                elif kind == "overall_progress":
                    done_cnt, total_cnt = item[1], item[2]
                    pct = (done_cnt * 100.0 / total_cnt) if total_cnt > 0 else 0.0
                    self.overall_prog["value"] = pct
                    self.overall_lbl.config(text=f"{done_cnt} / {total_cnt} ({pct:.0f}%)")
                elif kind == "dl_progress":
                    dl_bytes, tot_bytes, label_txt = item[1], item[2], item[3]
                    if tot_bytes > 0:
                        self.dl_prog["value"] = min(100.0, dl_bytes * 100.0 / tot_bytes)
                    self.dl_lbl.config(text=label_txt[:52])
                elif kind == "task_finished":
                    self._on_task_finished_ui(item[1])
                elif kind == "alert_error":
                    messagebox.showerror(item[1], item[2], parent=self.root)
                elif kind == "alert_info":
                    messagebox.showinfo(item[1], item[2], parent=self.root)
                elif kind == "manual_captcha":
                    img_path, tips, res_holder, evt = item[1], item[2], item[3], item[4]
                    res_holder["ans"] = self._show_captcha_dialog(img_path, tips)
                    evt.set()
        except queue.Empty:
            pass
        self.root.after(80, self._poll_ui_queue)

    # ───────────────────────── 人工图形码兜底弹窗 ─────────────────────────

    def prompt_captcha_modal_threadsafe(self, img_path: Path, clean_tips: str) -> str:
        holder = {"ans": ""}
        evt = threading.Event()
        self.ui_queue.put(("manual_captcha", img_path, clean_tips, holder, evt))
        evt.wait()
        return holder["ans"]

    def _show_captcha_dialog(self, img_path: Path, clean_tips: str) -> str:
        dlg = tk.Toplevel(self.root)
        dlg.title("人工输入图形校验码")
        dlg.geometry("380x230")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text=clean_tips or "请输入下图中的校验码：", font=("Microsoft YaHei UI", 10, "bold")).pack(pady=(12, 8))

        try:
            pil_img = Image.open(img_path).resize((200, 68), Image.Resampling.LANCZOS)
            tk_img = ImageTk.PhotoImage(pil_img)
            img_lbl = ttk.Label(dlg, image=tk_img)
            img_lbl.image = tk_img
            img_lbl.pack(pady=4)
        except Exception:
            ttk.Label(dlg, text=f"图片位置: {img_path}").pack(pady=4)

        ans_var = tk.StringVar()
        ent = ttk.Entry(dlg, textvariable=ans_var, width=18, font=("Microsoft YaHei UI", 11))
        ent.pack(pady=8)
        ent.focus_set()

        result = {"val": ""}

        def _ok(_event=None):
            result["val"] = ans_var.get().strip()
            dlg.destroy()

        ent.bind("<Return>", _ok)
        ttk.Button(dlg, text="确定提交", style="Primary.TButton", command=_ok).pack(pady=4)
        self.root.wait_window(dlg)
        return result["val"]

    # ───────────────────────── 登录与短信验证事件 ─────────────────────────

    def _bg_check_session(self):
        self.ui_queue.put(("badge", "checking", "● 正在校验本地登录会话..."))
        saved = self.auth_client.load_valid_session()
        if saved:
            self.current_session = saved
            uname = saved.get("username") or "未知用户"
            saved_at = saved.get("saved_at") or ""
            self.ui_queue.put(("badge", "ok", f"● 会话在线有效 | 账号: {uname} ({saved_at})"))
            self.enqueue_log(f"✅ 本地登录态在线核验有效（账号: {uname}），可直接开始批量查询！")
        else:
            self.current_session = None
            if self.auth_client.pending_file.exists():
                self.ui_queue.put(("badge", "sms", "● 待输入短信动态码完成登录"))
                self.enqueue_log("📩 检测到待验证的短信会话，请输入手机收到的动态码并点击【2. 提交验证码登录】。")
            else:
                self.ui_queue.put(("badge", "warn", "● 未登录或会话已过期，请先发短信登录"))
                self.enqueue_log("⚠️ 本地登录会话不存在或已过期，请确认账号密码后点击【1. 验证账密并发短信】。")

    def _start_sms_countdown(self, seconds: int):
        self.sms_countdown_left = seconds
        self._tick_sms_countdown()

    def _tick_sms_countdown(self):
        if self.sms_countdown_left > 0:
            self.btn_send_sms.config(state=tk.DISABLED, text=f"短信已发 ({self.sms_countdown_left}s)")
            self.sms_countdown_left -= 1
            self.root.after(1000, self._tick_sms_countdown)
        else:
            self.btn_send_sms.config(state=tk.NORMAL, text="1. 验证账密并发短信")

    def _on_click_send_sms(self):
        username = self.username_var.get().strip()
        password = self.password_var.get()
        if not username or not password:
            messagebox.showwarning("提示", "请先填写登录名与密码！", parent=self.root)
            return

        self._save_account_if_needed()
        self.btn_send_sms.config(state=tk.DISABLED, text="正在过码发短信...")

        def _work():
            try:
                res = self.auth_client.send_sms_step(
                    username=username,
                    password=password,
                    max_auto_attempts=5,
                    allow_manual_fallback=True,
                    ask_before_send=False,
                )
                if res["status"] == "done":
                    self.current_session = res["session"]
                    self.ui_queue.put(("badge", "ok", f"● 会话在线有效 | 账号: {username}"))
                    self.ui_queue.put(("alert_info", "登录成功", "账号已完成免短信登录，会话已保存！"))
                else:
                    mb = res["pending"].get("mbNumber", "")
                    self.ui_queue.put(("badge", "sms", f"● 短信已发至 {mb}，请填入动态码"))
                    self.ui_queue.put(("sms_sent_ok",))
            except Exception as exc:
                self.enqueue_log(f"❌ 发送短信验证失败: {exc}")
                self.ui_queue.put(("alert_error", "发送短信失败", str(exc)))
            finally:
                self.ui_queue.put(("enable_send_btn",))

        threading.Thread(target=_work, daemon=True).start()

    def _on_click_verify_sms(self):
        code = self.sms_code_var.get().strip()
        if not code:
            messagebox.showwarning("提示", "请输入手机收到的 6 位短信动态码！", parent=self.root)
            return

        self.btn_verify_sms.config(state=tk.DISABLED)

        def _work():
            try:
                saved = self.auth_client.complete_sms_step(code)
                self.current_session = saved
                uname = saved.get("username", "")
                self.ui_queue.put(("badge", "ok", f"● 会话在线有效 | 账号: {uname} ({saved.get('saved_at')})"))
                self.ui_queue.put(("alert_info", "登录成功", f"账号 [{uname}] 登录成功，凭证已持久化至 config/session.json！"))
            except Exception as exc:
                self.enqueue_log(f"❌ 短信动态码校验失败: {exc}")
                self.ui_queue.put(("alert_error", "验证码登录失败", str(exc)))
            finally:
                self.ui_queue.put(("enable_verify_btn",))

        threading.Thread(target=_work, daemon=True).start()

    # ───────────────────────── 文件导入与目录操作 ─────────────────────────

    def _on_import_file(self):
        fpath = filedialog.askopenfilename(
            parent=self.root,
            title="选择企业名单文件 (.txt / .csv)",
            filetypes=[("文本或表格名单", "*.txt *.csv"), ("所有文件", "*.*")],
        )
        if not fpath:
            return
        try:
            content = read_text_smart(Path(fpath))
            targets = parse_target_list(content)
            self.targets_text.delete("1.0", tk.END)
            self.targets_text.insert("1.0", "\n".join(targets))
            self._update_parsed_count()
            self.enqueue_log(f"📂 已从 [{Path(fpath).name}] 导入并去重解析出 {len(targets)} 家企业。")
        except Exception as exc:
            messagebox.showerror("导入失败", f"读取文件异常: {exc}", parent=self.root)

    def _on_save_targets(self):
        targets = self._get_parsed_targets()
        self.targets_file.write_text("\n".join(targets) + ("\n" if targets else ""), encoding="utf-8")
        self.enqueue_log(f"💾 已将 {len(targets)} 家企业保存至默认名单: {self.targets_file}")
        messagebox.showinfo("保存成功", f"已保存 {len(targets)} 家企业到 config/targets.txt", parent=self.root)

    def _on_clear_targets(self):
        self.targets_text.delete("1.0", tk.END)
        self._update_parsed_count()

    def _on_pick_output_dir(self):
        chosen = filedialog.askdirectory(parent=self.root, initialdir=self.output_dir_var.get(), title="选择输出保存目录")
        if chosen:
            self.output_dir_var.set(chosen)

    def _open_output_root(self):
        out = Path(self.output_dir_var.get())
        out.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(out))

    def _on_double_click_row(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        if not vals or len(vals) < 2:
            return
        org_name = vals[1]
        c_dir = Path(self.output_dir_var.get()) / sanitize_folder_name(org_name)
        target_open = c_dir if c_dir.exists() else Path(self.output_dir_var.get())
        if target_open.exists() and sys.platform == "win32":
            os.startfile(str(target_open))

    # ───────────────────────── 任务执行、暂停、中断与断点继承 ─────────────────────────

    def _on_click_reset_checkpoint(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("提示", "请先停止当前运行中的任务再重置断点！", parent=self.root)
            return
        if messagebox.askyesno("确认重置断点", "确定要清空本地断点记录 (task_checkpoint.json) 吗？\n（已导出的 CSV 和 ZIP 文件不会被删除）", parent=self.root):
            self.checkpoint_mgr.reset()
            self.enqueue_log("🔄 已清空本地历史断点缓存，下次运行将重新采集。")

    def _populate_initial_table(self, targets: List[str]):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.row_id_by_org.clear()
        for idx, org in enumerate(targets, start=1):
            iid = self.tree.insert("", tk.END, values=(idx, org, "排队等待", "-", "-", "等待轮询..."))
            self.row_id_by_org[org] = iid

    def _update_company_row_ui(self, payload: dict):
        org = payload.get("org_name", "")
        iid = self.row_id_by_org.get(org)
        if not iid:
            return
        cur_vals = list(self.tree.item(iid, "values"))
        status = payload.get("status", cur_vals[2])
        total = payload.get("total", cur_vals[3])
        pages = payload.get("page_progress", cur_vals[4])
        note = payload.get("note", cur_vals[5])

        tag = "running"
        if status == "已完成":
            tag = "done"
        elif status == "已继承完成":
            tag = "inherited"
        elif "失败" in status or "过期" in status:
            tag = "failed"
        elif "中断" in status or "暂停" in status:
            tag = "stopped"

        self.tree.item(iid, values=(cur_vals[0], org, status, total, pages, note), tags=(tag,))
        self.tree.see(iid)

    def _on_click_start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return

        targets = self._get_parsed_targets()
        if not targets:
            messagebox.showwarning("提示", "请先输入或导入至少 1 家企业法定名称！", parent=self.root)
            return

        output_root = Path(self.output_dir_var.get().strip() or str(self.default_output_dir))
        reason_code = self._get_selected_reason_code()
        download_zip = self.download_zip_var.get()
        user_allow_inherit = self.inherit_var.get()

        # 保存当前企业列表以便下次启动直接回显
        self.targets_file.write_text("\n".join(targets) + "\n", encoding="utf-8")

        self.controller.reset()
        self._populate_initial_table(targets)
        self.overall_prog["value"] = 0
        self.overall_lbl.config(text=f"0 / {len(targets)} (0%)")
        self.dl_prog["value"] = 0
        self.dl_lbl.config(text="准备开始...")

        self.btn_start.config(state=tk.DISABLED)
        self.btn_pause.config(state=tk.NORMAL, text="⏸ 暂停任务")
        self.btn_stop.config(state=tk.NORMAL)
        self.task_state_lbl.config(text="当前状态：🟢 正在运行中...", foreground="#15803d")

        self.worker_thread = threading.Thread(
            target=self._bg_run_batch,
            args=(targets, output_root, reason_code, download_zip, user_allow_inherit),
            daemon=True,
        )
        self.worker_thread.start()

    def _on_click_pause_resume(self):
        if not (self.worker_thread and self.worker_thread.is_alive()):
            return
        if not self.controller.is_paused:
            self.controller.pause()
            self.btn_pause.config(text="▶ 继续运行")
            self.task_state_lbl.config(text="当前状态：⏸ 已暂停（可随时继续或停止）", foreground="#b45309")
            self.enqueue_log("⏸ 任务已暂停，当前进度已锁定保护；点击【▶ 继续运行】可无缝恢复。")
        else:
            self.controller.resume()
            self.btn_pause.config(text="⏸ 暂停任务")
            self.task_state_lbl.config(text="当前状态：🟢 正在运行中...", foreground="#15803d")
            self.enqueue_log("▶ 任务已恢复继续执行！")

    def _on_click_stop(self):
        if not (self.worker_thread and self.worker_thread.is_alive()):
            return
        self.controller.stop()
        self.btn_pause.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.DISABLED)
        self.task_state_lbl.config(text="当前状态：⏹ 正在安全中断并保存断点...", foreground="#991b1b")
        self.enqueue_log("⏹ 已发送中断指令，正在安全落盘已完成进度...")

    def _bg_run_batch(
        self,
        targets: List[str],
        output_root: Path,
        reason_code: str,
        download_zip: bool,
        user_allow_inherit: bool,
    ):
        # 1. 启动前在线核验登录会话
        self.enqueue_log("🔍 正在核验中登网在线登录会话有效性...")
        saved = self.auth_client.load_valid_session()
        if not saved:
            self.current_session = None
            self.ui_queue.put(("badge", "warn", "● 会话已过期，请先发短信重新登录"))
            self.enqueue_log("❌ 当前登录会话不存在或已过期！请先在上方完成短信验证码登录后再点击开始。")
            self.ui_queue.put(("alert_error", "需要登录", "当前登录态未建立或已过期！\n请先在上方点击【1. 验证账密并发短信】并填入短信动态码完成登录。"))
            self.ui_queue.put(("task_finished", "需要重新登录"))
            return

        self.current_session = saved
        uname = saved.get("username", "")
        self.ui_queue.put(("badge", "ok", f"● 会话在线有效 | 账号: {uname} ({saved.get('saved_at')})"))

        # 2. 初始化采集器与内容未变断点校验
        scraper = GuarantorQueryScraper(
            auth_client=self.auth_client,
            output_root=output_root,
            page_size=10,
            min_delay=2.5,
            max_delay=5.5,
            controller=self.controller,
            checkpoint=self.checkpoint_mgr,
            logger=self.enqueue_log,
            on_company_progress=lambda d: self.ui_queue.put(("company_progress", d)),
            on_download_progress=lambda b, t, txt: self.ui_queue.put(("dl_progress", b, t, txt)),
        )
        scraper.bind_session(saved)

        sig = compute_task_signature(targets, query_reason=reason_code, download_zip=download_zip)
        same_content = self.checkpoint_mgr.prepare_for_task(
            targets=targets,
            signature=sig,
            allow_inherit=user_allow_inherit,
        )
        effective_inherit = user_allow_inherit and same_content
        if effective_inherit:
            self.enqueue_log("♻️ 检测到本次任务内容与上次一致，已激活断点继承（已完成企业与已完成分页将自动跳过）！")
        elif user_allow_inherit:
            self.enqueue_log("🆕 检测到输入企业名单或参数为新内容，已建立新任务断点档案。")

        results_summary = []
        done_count = 0
        final_banner = "全部采集完成"

        for idx, org_name in enumerate(targets, start=1):
            if self.controller.is_stopped:
                final_banner = "已手动停止（断点已保存）"
                break

            try:
                res = scraper.process_company(
                    org_name=org_name,
                    query_reason=reason_code,
                    download_zip=download_zip,
                    allow_inherit=effective_inherit,
                )
                done_count += 1
                self.ui_queue.put(("overall_progress", done_count, len(targets)))
                status_lbl = "已继承完成" if res.get("inherited") else "已完成"
                results_summary.append({
                    "orgName": org_name,
                    "status": "ok",
                    "status_label": status_lbl,
                    **res,
                })
            except TaskStoppedError as exc:
                reason = classify_error_reason(exc)
                self.ui_queue.put((
                    "company_progress",
                    {"org_name": org_name, "status": "已中断", "note": reason},
                ))
                results_summary.append({
                    "orgName": org_name,
                    "status": "stopped",
                    "status_label": "已中断",
                    "note": reason,
                })
                final_banner = "已中断（内容不变时可直接续跑）"
                break
            except PermissionError as exc:
                reason = classify_error_reason(exc)
                self.checkpoint_mgr.mark_company_failed(org_name, reason)
                self.ui_queue.put(("badge", "warn", "● 运行途中会话过期，请重新短信登录"))
                self.ui_queue.put((
                    "company_progress",
                    {"org_name": org_name, "status": "会话过期", "note": reason},
                ))
                results_summary.append({
                    "orgName": org_name,
                    "status": "expired",
                    "status_label": "会话过期",
                    "note": reason,
                })
                self.ui_queue.put(("alert_error", "会话已过期", "中登网登录令牌已过期！\n请重新发送短信动态码登录后，保持名单不变直接点击【开始批量采集】续跑。"))
                final_banner = "因登录过期暂停（重新登录后可续跑）"
                break
            except Exception as exc:
                reason = classify_error_reason(exc)
                self.checkpoint_mgr.mark_company_failed(org_name, reason)
                done_count += 1
                self.ui_queue.put(("overall_progress", done_count, len(targets)))
                self.enqueue_log(f"❌ 企业 [{org_name}] 采集失败 -> {reason}")
                self.ui_queue.put((
                    "company_progress",
                    {"org_name": org_name, "status": "采集失败", "note": reason},
                ))
                results_summary.append({
                    "orgName": org_name,
                    "status": "error",
                    "status_label": "采集失败",
                    "note": reason,
                })

            # 企业间拟人化间隔（若刚才是秒级继承则无需等待）
            if idx < len(targets) and not (results_summary and results_summary[-1].get("inherited")):
                try:
                    rest = random.uniform(6.0, 11.0)
                    self.enqueue_log(f"⏳ 已处理完 [{org_name}]，拟人休息 {rest:.1f}s 后切换下一家...")
                    self.controller.sleep(rest)
                except TaskStoppedError as exc:
                    final_banner = "已中断（内容不变时可直接续跑）"
                    self.enqueue_log(f"⏹ {exc}")
                    break

        report_path = output_root / "批量任务执行报告.csv"
        try:
            write_batch_report_csv(report_path, results_summary)
            self.enqueue_log(f"📑 批次执行与失败说明报告已保存: {report_path}")
        except Exception as exc:
            self.enqueue_log(f"⚠️ 写入执行报告失败: {exc}")

        self.ui_queue.put(("task_finished", final_banner))

    def _on_task_finished_ui(self, summary_text: str):
        self.btn_start.config(state=tk.NORMAL)
        self.btn_pause.config(state=tk.DISABLED, text="⏸ 暂停任务")
        self.btn_stop.config(state=tk.DISABLED)
        self.task_state_lbl.config(text=f"当前状态：{summary_text}", foreground="#1e3a8a")

    def _on_close(self):
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("确认退出", "当前仍有采集任务正在运行，退出将安全中断并保存已完成断点，确定退出吗？", parent=self.root):
                return
            self.controller.stop()
        self.root.destroy()


def run_self_test() -> int:
    """供源码与 PyInstaller 打包后的 .exe 执行端到端自检"""
    app_root = get_app_root()
    cfg_dir, out_dir = ensure_runtime_dirs(app_root)

    # 1. 密码双摘要签名校验 (MD5 + 国密 SM3)
    digests = build_password_digests("Test123456!")
    assert len(digests["passWord"]) == 32 and len(digests["passWordSm3"]) == 64

    # 2. 多分隔符解析校验 (换行 + 英文逗号 + 中文逗号 + 表头过滤 + 去重)
    parsed = parse_target_list("机构名称\nTCL华星光电技术有限公司, 珠海格力电器股份有限公司，美的集团股份有限公司\nTCL华星光电技术有限公司")
    assert parsed == ["TCL华星光电技术有限公司", "珠海格力电器股份有限公司", "美的集团股份有限公司"], f"Unexpected: {parsed}"

    # 3. 验证码 ONNX 引擎与 TLS 客户端加载校验
    auth = ZhongdengAuthClient(cfg_dir)
    assert auth.solver is not None

    # 4. 控制器暂停/继续/中断与内容不变断点继承闭环校验
    ctrl = TaskController()
    ctrl.pause()
    assert ctrl.is_paused
    ctrl.resume()
    assert not ctrl.is_paused
    ctrl.stop()
    stopped_raised = False
    try:
        ctrl.check()
    except TaskStoppedError:
        stopped_raised = True
    assert stopped_raised

    tmp_cp_file = cfg_dir / "_selftest_checkpoint.json"
    cp = CheckpointManager(tmp_cp_file)
    cp.reset()
    sig1 = compute_task_signature(parsed, "01", True)
    assert cp.prepare_for_task(parsed, sig1, allow_inherit=True) is False
    cp.update_company_page("TCL华星光电技术有限公司", 17, 2, 1, [{"序号": "1"}])
    # 重新加载，输入内容不变 -> 必须命中继承！
    cp_reloaded = CheckpointManager(tmp_cp_file)
    assert cp_reloaded.prepare_for_task(parsed, sig1, allow_inherit=True) is True
    assert "1" in cp_reloaded.get_company_state("TCL华星光电技术有限公司")["saved_pages"]
    # 输入内容改变 -> 必须重置不继承旧任务！
    sig2 = compute_task_signature(["另一家新公司"], "01", True)
    assert cp_reloaded.prepare_for_task(["另一家新公司"], sig2, allow_inherit=True) is False
    assert cp_reloaded.get_company_state("TCL华星光电技术有限公司")["saved_pages"] == {}
    cp_reloaded.reset()

    # 5. GUI 组件实例化冒烟校验
    root = tk.Tk()
    root.withdraw()
    app = ZhongdengCrawlerGUI(root, skip_auto_session_check=True)
    root.update_idletasks()
    root.destroy()

    report = {
        "ok": True,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "app_root": str(app_root),
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (cfg_dir / "_selftest_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description="中登网桌面可视化采集终端")
    parser.add_argument("--self-test", action="store_true", help="运行打包与核心模块端到端自检并退出")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(run_self_test())

    root = tk.Tk()
    ZhongdengCrawlerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
