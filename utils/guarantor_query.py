# -*- coding: utf-8 -*-
"""
中登网 - 按担保人（机构名称）批量查询、表格导出、分类统计、断点继承与暂停/中断控制模块
"""

import csv
import hashlib
import json
import math
import random
import re
import threading
import time
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .auth import BASE_URL, ZhongdengAuthClient

SKIP_HEADERS = {"机构名称", "企业名称", "公司名称", "担保人名称", "名称", "name", "company", "orgname"}
ONLINE_REGNUM_QUERY_URL = "https://www.zhongdengwang.org.cn/out/?#/query/regnum/"


class TaskStoppedError(Exception):
    """用户主动点击停止/中断任务时抛出的受控异常"""


class TaskController:
    """线程安全的暂停 / 继续 / 停止控制器"""

    def __init__(self):
        self._run_gate = threading.Event()
        self._run_gate.set()  # 默认放行
        self._stop_flag = threading.Event()

    @property
    def is_paused(self) -> bool:
        return not self._run_gate.is_set() and not self._stop_flag.is_set()

    @property
    def is_stopped(self) -> bool:
        return self._stop_flag.is_set()

    def pause(self):
        self._run_gate.clear()

    def resume(self):
        self._run_gate.set()

    def stop(self):
        self._stop_flag.set()
        self._run_gate.set()  # 唤醒可能处于暂停等待中的工作线程以便立即退出

    def reset(self):
        self._stop_flag.clear()
        self._run_gate.set()

    def check(self):
        """检查是否暂停或停止（在每个网络步骤与下载分块中调用）"""
        while not self._run_gate.is_set():
            if self._stop_flag.is_set():
                raise TaskStoppedError("用户主动中断任务（已完成进度已自动保存，内容不变时可直接续跑）")
            time.sleep(0.1)
        if self._stop_flag.is_set():
            raise TaskStoppedError("用户主动中断任务（已完成进度已自动保存，内容不变时可直接续跑）")

    def sleep(self, seconds: float):
        """可被毫秒级暂停或中断的拟人化等待"""
        end_time = time.time() + max(0.0, seconds)
        while time.time() < end_time:
            self.check()
            remaining = end_time - time.time()
            if remaining <= 0:
                break
            time.sleep(min(0.1, remaining))


def classify_error_reason(exc: Exception) -> str:
    """将底层异常转化为清晰可读的中文失败/中断原因说明"""
    if isinstance(exc, TaskStoppedError):
        return str(exc)
    if isinstance(exc, PermissionError):
        return f"登录态过期(403): {exc}"
    msg = str(exc)
    low = msg.lower()
    if "timed out" in low or "timeout" in low or "curl: (28)" in low:
        return f"网络连接或下载超时（已保存断点，网络恢复后可直接续跑）: {msg[:120]}"
    if "curl:" in low or "connection" in low or "ssl" in low or "eof" in low:
        return f"网络连接断开或波动（已保存断点，可直接续跑）: {msg[:120]}"
    if "验证码" in msg or "校验码" in msg:
        return f"图形校验码未能通过: {msg}"
    if "formvalidate" in low or "表单" in msg or "名称" in msg:
        return f"查询条件被服务端拒绝: {msg}"
    return f"执行异常: {msg[:160]}"


def sanitize_folder_name(name: str) -> str:
    """清理 Windows 非法目录名字符"""
    return re.sub(r'[<>:"/\\|?*]', "_", name.strip())


def format_epoch_ms(ms_val) -> str:
    """将毫秒时间戳格式化为 YYYY-MM-DD HH:MM:SS"""
    if not ms_val:
        return ""
    try:
        return datetime.fromtimestamp(int(ms_val) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ms_val)


def parse_target_list(raw_text: str) -> List[str]:
    """
    解析目标机构名称列表：
    兼容换行分隔、英文逗号 ',' 分隔、中文逗号 '，' 或分号分隔，以及单列/首列 CSV。
    自动去重并保持原始顺序。
    """
    if not raw_text:
        return []
    text = raw_text.lstrip("\ufeff")
    parts = re.split(r"[\r\n,，;；]+", text)
    seen = set()
    result = []
    for p in parts:
        cleaned = p.strip().strip('"').strip("'").strip()
        cleaned = re.sub(r"\s+", "", cleaned)
        if not cleaned or cleaned.startswith("#"):
            continue
        if cleaned.lower() in SKIP_HEADERS or cleaned in SKIP_HEADERS:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def compute_task_signature(targets: List[str], query_reason: str = "01", download_zip: bool = True) -> str:
    """对输入内容计算 SHA256 指纹，用于判断任务输入内容是否未发生变化"""
    canonical = json.dumps(
        {"targets": targets, "reason": query_reason, "download_zip": bool(download_zip)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def decode_zip_filename(info: zipfile.ZipInfo) -> str:
    """兼容服务端 Java ZipOutputStream 生成的 GBK 文件名（未置 UTF-8 flag bit 11）"""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("gbk")
    except Exception:
        return info.filename


class CheckpointManager:
    """
    断点续传与进度继承管理器：
      - 当任务输入内容签名 (signature) 一致时，自动继承已完成企业及半途中断企业的已完成分页数据
      - 同时校验本地 CSV 与 附件/*.zip 物理文件完整性，防止文件被误删后假跳过
    """

    def __init__(self, checkpoint_file: Path):
        self.checkpoint_file = checkpoint_file
        self.state: dict = self._load()

    def _load(self) -> dict:
        if self.checkpoint_file.exists():
            try:
                return json.loads(self.checkpoint_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"signature": "", "targets": [], "companies": {}, "updated_at": ""}

    def save(self):
        self.state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_file.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def reset(self):
        self.state = {"signature": "", "targets": [], "companies": {}, "updated_at": ""}
        if self.checkpoint_file.exists():
            try:
                self.checkpoint_file.unlink()
            except Exception:
                pass

    def prepare_for_task(self, targets: List[str], signature: str, allow_inherit: bool = True) -> bool:
        """
        返回 True 表示命中历史相同内容断点，可继承进度；
        若输入内容已改变或禁用继承，则初始化新任务签名。
        """
        if allow_inherit and self.state.get("signature") == signature:
            return True

        self.state = {
            "signature": signature,
            "targets": targets,
            "companies": {},
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save()
        return False

    def get_company_state(self, org_name: str) -> dict:
        return self.state.setdefault("companies", {}).setdefault(
            org_name,
            {"status": "pending", "total": None, "pages": 0, "saved_pages": {}},
        )

    def update_company_page(self, org_name: str, total: int, total_pages: int, page_num: int, page_rows: List[dict]):
        c_state = self.get_company_state(org_name)
        c_state["status"] = "in_progress"
        c_state["total"] = total
        c_state["pages"] = total_pages
        c_state.setdefault("saved_pages", {})[str(page_num)] = page_rows
        self.save()

    def mark_company_completed(self, org_name: str, total: int, total_pages: int):
        c_state = self.get_company_state(org_name)
        c_state["status"] = "completed"
        c_state["total"] = total
        c_state["pages"] = total_pages
        c_state["saved_pages"] = {}  # 完整落盘 CSV 后释放临时行缓存减小 JSON 体积
        self.save()

    def mark_company_failed(self, org_name: str, reason: str):
        c_state = self.get_company_state(org_name)
        c_state["status"] = "failed"
        c_state["last_error"] = reason
        self.save()

    def is_company_intact_on_disk(
        self,
        org_name: str,
        output_root: Path,
        download_zip: bool,
    ) -> Optional[dict]:
        """校验某企业是否已在当前未变任务中完成且本地 CSV + 附件 ZIP 全部完好"""
        c_state = self.state.get("companies", {}).get(org_name)
        if not c_state or c_state.get("status") != "completed":
            return None

        safe_name = sanitize_folder_name(org_name)
        company_dir = output_root / safe_name
        main_csv = company_dir / f"{safe_name}.csv"
        cat_csv = company_dir / f"{safe_name}_分类汇总.csv"
        meta_json = company_dir / "查询摘要.json"
        attach_dir = company_dir / "附件"

        if not (main_csv.exists() and cat_csv.exists() and meta_json.exists()):
            return None

        try:
            meta = json.loads(meta_json.read_text(encoding="utf-8"))
            total = int(meta.get("total", 0))
            pages = int(meta.get("pages", 0))
            with open(main_csv, "r", encoding="utf-8-sig", newline="") as f:
                row_count = sum(1 for _ in csv.DictReader(f))
            if row_count != total:
                return None

            if download_zip and total > 0:
                for p in range(1, pages + 1):
                    s_seq = (p - 1) * 10 + 1
                    e_seq = min(p * 10, total)
                    zp = attach_dir / f"第{p:03d}页_{s_seq:03d}-{e_seq:03d}_登记证明文件.zip"
                    if not (zp.exists() and zp.stat().st_size > 100 and zipfile.is_zipfile(zp)):
                        return None
            return meta
        except Exception:
            return None


def write_batch_report_csv(report_path: Path, results: List[dict]):
    """将本批次所有企业的成功/继承/失败/中断状态汇总落盘，方便审计与排查失败原因"""
    fieldnames = ["序号", "企业名称", "执行状态", "登记总笔数", "总页数", "输出目录", "说明或失败原因", "更新时间"]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for idx, r in enumerate(results, start=1):
            writer.writerow({
                "序号": idx,
                "企业名称": r.get("orgName", ""),
                "执行状态": r.get("status_label", r.get("status", "")),
                "登记总笔数": r.get("total", ""),
                "总页数": r.get("pages", ""),
                "输出目录": r.get("company_dir", ""),
                "说明或失败原因": r.get("note", ""),
                "更新时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            })


class GuarantorQueryScraper:
    def __init__(
        self,
        auth_client: ZhongdengAuthClient,
        output_root: Path,
        page_size: int = 10,
        min_delay: float = 2.5,
        max_delay: float = 5.5,
        controller: Optional[TaskController] = None,
        checkpoint: Optional[CheckpointManager] = None,
        logger: Optional[Callable[[str], None]] = None,
        on_company_progress: Optional[Callable[[dict], None]] = None,
        on_download_progress: Optional[Callable[[int, int, str], None]] = None,
    ):
        self.auth = auth_client
        self.session = auth_client.session
        self.output_root = output_root
        self.page_size = page_size
        self.min_delay = min_delay
        self.max_delay = max_delay

        self.controller = controller or TaskController()
        self.checkpoint = checkpoint or CheckpointManager(auth_client.config_dir / "task_checkpoint.json")
        self._logger = logger
        self._on_company_progress = on_company_progress
        self._on_download_progress = on_download_progress

        self.access_token: str = ""
        self.user_info: dict = {}

    def _log(self, msg: str):
        if self._logger:
            self._logger(msg)
        else:
            print(msg)

    def _emit_company(self, **kwargs):
        if self._on_company_progress:
            self._on_company_progress(kwargs)

    def _emit_dl(self, downloaded: int, total: int, text: str):
        if self._on_download_progress:
            self._on_download_progress(downloaded, total, text)

    def _human_sleep(self, low: Optional[float] = None, high: Optional[float] = None, label: str = ""):
        self.controller.check()
        lo = low if low is not None else self.min_delay
        hi = high if high is not None else self.max_delay
        sec = random.uniform(lo, hi)
        if label:
            self._log(f"    ⏳ {label} (拟人停顿 {sec:.1f}s)...")
        self.controller.sleep(sec)

    def bind_session(self, saved_session: dict):
        self.access_token = saved_session["accessToken"]
        self.user_info = saved_session.get("userInfo") or {}

    def _headers(self) -> dict:
        return {
            "accessToken": self.access_token,
            "Referer": "https://www.zhongdengwang.org.cn/out/?",
        }

    def _post_json(self, path: str, payload: dict, timeout: int = 25) -> dict:
        self.controller.check()
        resp = self.session.post(
            f"{BASE_URL}{path}",
            json=payload,
            headers=self._headers(),
            timeout=timeout,
        )
        res = resp.json()
        if str(res.get("code")) == "403":
            raise PermissionError(res.get("msg") or "会话令牌已过期")
        return res

    def _submit_search_form(
        self,
        org_name: str,
        query_reason: str = "01",
        max_captcha_tries: int = 20,
        manual_after: int = 12,
    ) -> dict:
        tok_res = self._post_json("/api/user/tool/token", {"type": "5"})
        if not tok_res.get("success") or not tok_res.get("data", {}).get("token"):
            raise RuntimeError(f"获取查询 token 失败: {tok_res.get('msg', tok_res)}")
        q_token = tok_res["data"]["token"]

        user_cat = str(self.user_info.get("userCategory") or "20")
        user_status = str(self.user_info.get("userStatus") or "0")

        # 对齐前端 /out/assets/index-1b48539b.js：动态检测当前账号是否具备历史数据查询权限 (hasDataTypeRight)
        has_data_type_right = "0"
        try:
            sub_res = self.session.post(
                f"{BASE_URL}/api/user/roleRight/userMenuQueryByParentId",
                data={"nodeId": "out-004-001-000-000", "menuType": "0", "sysName": "对外用户系统"},
                headers=self._headers(),
                timeout=10,
            ).json()
            if sub_res.get("success") and sub_res.get("data"):
                if (sub_res["data"][0] or {}).get("menuUrl") == "/query/guarantor/his":
                    has_data_type_right = "1"
        except Exception:
            pass

        last_err_msg = ""
        submit_count = 0
        manual_fn = getattr(self.auth, "_manual_captcha_fn", None)
        for attempt in range(1, max_captcha_tries + 1):
            self.controller.check()
            vc_resp = self.session.post(
                f"{BASE_URL}/api/user/tool/verifyCode",
                data={"type": "5", "token": q_token},
                headers=self._headers(),
                timeout=15,
            ).json()
            if not vc_resp.get("success"):
                raise RuntimeError(f"获取查询验证码失败: {vc_resp.get('msg', vc_resp)}")

            captcha_path = self.auth.config_dir / "query_captcha.png"
            img_bytes = self.auth.solver.save_base64_image(vc_resp["data"]["url"], captcha_path)
            ans, clean_tips, mode = self.auth.solver.try_auto_solve(img_bytes, vc_resp["data"].get("tips", ""))

            # 自动识别多次仍失败时，GUI 模式下弹窗请用户人工输入
            if attempt > manual_after and manual_fn:
                self._log(f"    🙋 [查询校验码 {attempt}/{max_captcha_tries}] 自动识别多次未通过，请在弹窗中人工输入")
                ans = (manual_fn(captcha_path, clean_tips) or "").strip()
                mode = "manual"
                if not ans:
                    raise RuntimeError("用户取消了查询校验码人工输入")

            if not ans:
                self._log(f"    🔄 [查询校验码 {attempt}/{max_captcha_tries}] 识别置信度不足，换一张 ({mode})")
                self.controller.sleep(0.6)
                continue

            submit_count += 1
            self._log(f"    🤖 [查询校验码 {attempt}/{max_captcha_tries}] '{clean_tips}' -> {ans}")
            effective_reason = "14" if user_cat == "14" else query_reason
            form_payload = {
                "debtorType": "0",
                "queryConfirmationAgeUUID": "",
                "hasDataTypeRight": has_data_type_right,
                "dataType": "0",
                "userCategory": user_cat,
                "userStatus": user_status,
                "queryConfirmationNeed": "0",
                "token": q_token,
                "orgName": org_name,
                "queryReason": effective_reason,
                "verifyCode": ans,
            }
            if user_cat == "14":
                form_payload["otherQueryReason"] = "依法履职查询"
            val_res = self._post_json("/api/query/byDebtorName/formValidate", form_payload)
            if val_res.get("success"):
                val_data = val_res.get("data") or {}
                form_payload["currentQueryConditonUUID"] = val_data.get("currentQueryConditonUUID", "")
                if val_data.get("queryConfirmationAgeUUID"):
                    form_payload["queryConfirmationAgeUUID"] = val_data["queryConfirmationAgeUUID"]
                form_payload["token"] = ""
                return form_payload

            last_err_msg = val_res.get("msg") or "未知校验错误"
            self._log(f"    ⚠️ formValidate 未通过: {last_err_msg}")
            if any(k in last_err_msg for k in ("机构名称", "长度", "非法", "权限", "频繁")):
                raise RuntimeError(f"表单校验拒绝: {last_err_msg}")
            self.controller.sleep(1.0)

        raise RuntimeError(f"连续 {max_captcha_tries} 张查询验证码未通过 (提交 {submit_count} 次, 最后提示: {last_err_msg})")

    def _download_page_zip(
        self,
        page_payload: dict,
        save_path: Path,
        max_retries: int = 3,
        allow_inherit: bool = True,
    ) -> List[str]:
        save_path.parent.mkdir(parents=True, exist_ok=True)

        if allow_inherit and save_path.exists() and save_path.stat().st_size > 100 and zipfile.is_zipfile(save_path):
            with zipfile.ZipFile(save_path, "r") as zf:
                names = [decode_zip_filename(zi) for zi in zf.infolist()]
            sz = save_path.stat().st_size
            self._emit_dl(sz, sz, f"继承本地完好附件包: {save_path.name}")
            self._log(f"    ⏭️  附件压缩包已存在且完好，直接继承: {save_path.name} ({len(names)} 个文件)")
            return names

        temp_path = save_path.with_suffix(".zip.part")

        for retry in range(1, max_retries + 1):
            self.controller.check()
            try:
                dl1 = self._post_json("/api/query/byDebtorName/downloadRegInfoPDFOnCurrentPage", page_payload, timeout=45)
                if not dl1.get("success") or not (dl1.get("data") or {}).get("url"):
                    raise RuntimeError(f"下载票据申请失败: {dl1.get('msg', dl1)}")

                hop1_url = dl1["data"]["url"]
                if hop1_url.startswith("/"):
                    hop1_url = BASE_URL + hop1_url

                self.controller.check()
                dl2_resp = self.session.get(hop1_url, headers=self._headers(), timeout=30)
                ctype2 = dl2_resp.headers.get("content-type") or ""
                if "json" in ctype2.lower():
                    dl2 = dl2_resp.json()
                    if not dl2.get("success") or not (dl2.get("data") or {}).get("url"):
                        raise RuntimeError(f"文件流地址获取失败: {dl2.get('msg', dl2)}")
                    real_url = dl2["data"]["url"]
                    if real_url.startswith("/"):
                        real_url = BASE_URL + real_url
                else:
                    real_url = hop1_url

                t0 = time.time()
                bin_resp = self.session.get(
                    real_url,
                    headers=self._headers(),
                    stream=True,
                    timeout=600,
                )
                if bin_resp.status_code != 200:
                    raise RuntimeError(f"文件流 HTTP 状态异常 ({bin_resp.status_code})")

                total_bytes = int(bin_resp.headers.get("content-length") or 0)
                downloaded = 0
                last_print = 0.0

                with open(temp_path, "wb") as f_out:
                    for chunk in bin_resp.iter_content(chunk_size=65536):
                        self.controller.check()
                        if not chunk:
                            continue
                        f_out.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if total_bytes > 0:
                            pct = downloaded * 100.0 / total_bytes
                            self._emit_dl(
                                downloaded,
                                total_bytes,
                                f"{save_path.name}: {downloaded / 1048576:.2f} / {total_bytes / 1048576:.2f} MB ({pct:.1f}%)",
                            )
                        else:
                            self._emit_dl(downloaded, 0, f"{save_path.name}: {downloaded / 1048576:.2f} MB")

                        if now - last_print >= 3.0:
                            last_print = now
                            if total_bytes > 0:
                                pct = downloaded * 100.0 / total_bytes
                                self._log(f"      ⬇️ 下载进度: {downloaded / 1048576:.2f} / {total_bytes / 1048576:.2f} MB ({pct:.1f}%)")
                            else:
                                self._log(f"      ⬇️ 已下载: {downloaded / 1048576:.2f} MB...")

                if total_bytes > 0 and downloaded < total_bytes:
                    raise RuntimeError(f"下载流提前断开 ({downloaded}/{total_bytes} bytes)")

                if not zipfile.is_zipfile(temp_path):
                    snippet = temp_path.read_bytes()[:160]
                    raise RuntimeError(f"响应内容非有效 ZIP 压缩包: {snippet!r}")

                if save_path.exists():
                    save_path.unlink()
                temp_path.replace(save_path)

                elapsed = time.time() - t0
                with zipfile.ZipFile(save_path, "r") as zf:
                    names = [decode_zip_filename(zi) for zi in zf.infolist()]
                self._emit_dl(downloaded, downloaded or 1, f"下载完成: {save_path.name}")
                self._log(
                    f"    ✅ 压缩包保存成功: {save_path.name} "
                    f"({downloaded / 1048576:.2f} MB, 耗时 {elapsed:.1f}s, 含 {len(names)} 个文件)"
                )
                return names

            except (PermissionError, TaskStoppedError):
                raise
            except Exception as exc:
                self._log(f"    ⚠️ 第 {retry}/{max_retries} 次下载压缩包异常: {exc}")
                if retry < max_retries:
                    wait_s = 3.0 * retry
                    self._log(f"    ⏳ 等待 {wait_s:.0f}s 后自动重试该页下载...")
                    self.controller.sleep(wait_s)
                else:
                    raise RuntimeError(f"压缩包 {save_path.name} 连续 {max_retries} 次下载失败: {exc}") from exc
        return []

    @staticmethod
    def _match_files_in_zip(zip_names: List[str], sub_reg_no: str, main_reg_no: str) -> str:
        if not zip_names:
            return ""
        exact = [n for n in zip_names if sub_reg_no and sub_reg_no in n]
        if exact:
            return " | ".join(exact)
        fallback = [n for n in zip_names if main_reg_no and main_reg_no in n]
        return " | ".join(fallback)

    @staticmethod
    def _write_main_csv(csv_path: Path, rows: List[dict]):
        fieldnames = [
            "序号",
            "登记时间",
            "登记证明编号",
            "担保权人",
            "登记类型",
            "担保类型",
            "登记到期日",
            "主登记号",
            "本地附件压缩包",
            "包内对应证明文件",
            "在线核验链接",
        ]
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _write_category_csv(
        cat_csv_path: Path,
        org_name: str,
        total: int,
        official_categories: List[dict],
        rows: List[dict],
    ):
        fieldnames = ["企业名称", "统计维度", "分类名称", "笔数", "占比"]
        summary_rows = []

        for item in official_categories:
            cnt = int(item.get("subtotal") or 0)
            pct = f"{(cnt * 100.0 / total):.2f}%" if total > 0 else "0.00%"
            summary_rows.append({
                "企业名称": org_name,
                "统计维度": "担保类型(官方分类)",
                "分类名称": item.get("linkName", ""),
                "笔数": cnt,
                "占比": pct,
            })

        reg_type_counter = Counter(r["登记类型"] for r in rows if r.get("登记类型"))
        for rtype, cnt in reg_type_counter.most_common():
            pct = f"{(cnt * 100.0 / len(rows)):.2f}%" if rows else "0.00%"
            summary_rows.append({
                "企业名称": org_name,
                "统计维度": "登记类型",
                "分类名称": rtype,
                "笔数": cnt,
                "占比": pct,
            })

        pawnee_counter = Counter(r["担保权人"] for r in rows if r.get("担保权人"))
        for pname, cnt in pawnee_counter.most_common(20):
            pct = f"{(cnt * 100.0 / len(rows)):.2f}%" if rows else "0.00%"
            summary_rows.append({
                "企业名称": org_name,
                "统计维度": "担保权人(Top20)",
                "分类名称": pname,
                "笔数": cnt,
                "占比": pct,
            })

        with open(cat_csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(summary_rows)

    def process_company(
        self,
        org_name: str,
        query_reason: str = "01",
        download_zip: bool = True,
        allow_inherit: bool = True,
    ) -> dict:
        self.controller.check()
        safe_name = sanitize_folder_name(org_name)
        company_dir = self.output_root / safe_name
        attach_dir = company_dir / "附件"

        # 1. 企业级断点继承检查
        if allow_inherit:
            inherited_meta = self.checkpoint.is_company_intact_on_disk(
                org_name=org_name,
                output_root=self.output_root,
                download_zip=download_zip,
            )
            if inherited_meta is not None:
                tot = int(inherited_meta.get("total", 0))
                pgs = int(inherited_meta.get("pages", 0))
                self._log(f"\n⏭️  企业 [{org_name}] 内容未变且本地结果完好（共 {tot} 笔 / {pgs} 页），直接继承跳过！")
                self._emit_company(
                    org_name=org_name,
                    status="已继承完成",
                    total=tot,
                    page_progress=f"{pgs}/{pgs}",
                    note=f"内容未变，自动继承本地 {tot} 笔记录与 {pgs} 页压缩包",
                )
                return {
                    **inherited_meta,
                    "inherited": True,
                    "company_dir": str(company_dir),
                    "note": f"继承已完成结果 (共 {tot} 笔)",
                }

        company_dir.mkdir(parents=True, exist_ok=True)
        attach_dir.mkdir(parents=True, exist_ok=True)

        main_csv_path = company_dir / f"{safe_name}.csv"
        cat_csv_path = company_dir / f"{safe_name}_分类汇总.csv"
        meta_json_path = company_dir / "查询摘要.json"

        self._log("\n" + "━" * 64)
        self._log(f"🏢 开始查询企业: {org_name}")
        self._log(f"📂 输出目录: {company_dir}")
        self._log("━" * 64)
        self._emit_company(org_name=org_name, status="提交查询中", total="-", page_progress="0/0", note="正在通过图形校验码...")

        # 2. 提交查询表单
        query_ctx = self._submit_search_form(org_name=org_name, query_reason=query_reason)
        self._human_sleep(1.2, 2.5, "等待进入查询汇总页")

        # 3. 获取汇总信息 (summary)
        sum_res = self._post_json("/api/query/byDebtorName/summary", query_ctx)
        if not sum_res.get("success"):
            raise RuntimeError(f"获取查询汇总失败: {sum_res.get('msg', sum_res)}")

        sum_data = sum_res.get("data") or {}
        query_ctx["queryFlowNo"] = sum_data.get("queryFlowNo") or ""
        query_ctx["queryConfirmationFileNo"] = sum_data.get("queryConfirmationFileNo") or ""
        query_ctx["regExcelFileNo"] = sum_data.get("regExcelFileNo") or ""

        stats_vo = (sum_data.get("queryRegCategory") or {}).get("queryRegStatisticsVo") or {}
        total = int(stats_vo.get("total") or 0)
        cat_items = stats_vo.get("queryCategoryItemList") or []

        active_cats = [f"{c['linkName']}({c['subtotal']}笔)" for c in cat_items if int(c.get("subtotal") or 0) > 0]
        cat_summary_str = " | ".join(active_cats) if active_cats else "无有效登记"
        self._log(f"  📊 查询结果统计: 共 {total} 笔登记 ({cat_summary_str})")

        if total == 0:
            self._write_main_csv(main_csv_path, [])
            self._write_category_csv(cat_csv_path, org_name, 0, cat_items, [])
            summary_meta = {
                "orgName": org_name,
                "queryTime": (sum_data.get("queryReport") or {}).get("queryTime"),
                "queryFlowNo": query_ctx["queryFlowNo"],
                "regExcelFileNo": query_ctx["regExcelFileNo"],
                "total": 0,
                "pages": 0,
                "exportedRows": 0,
                "officialCategories": cat_items,
            }
            meta_json_path.write_text(json.dumps(summary_meta, ensure_ascii=False, indent=2), encoding="utf-8")
            self.checkpoint.mark_company_completed(org_name, 0, 0)
            self._emit_company(org_name=org_name, status="已完成", total=0, page_progress="0/0", note="该企业当前无登记记录 (0笔)")
            return {**summary_meta, "inherited": False, "company_dir": str(company_dir), "note": "无登记记录 (0笔)"}

        # 4. 打开详情页初始化调用 queryReport
        self._human_sleep(1.2, 2.5, "打开登记信息详情页")
        self._post_json("/api/query/byDebtorName/queryReport", query_ctx)

        total_pages = math.ceil(total / self.page_size)
        all_rows: List[dict] = []

        # 页级断点恢复：若上次在该企业中途断开且服务端总笔数未变，复用已采完页
        c_state = self.checkpoint.get_company_state(org_name)
        saved_pages: Dict[str, List[dict]] = {}
        if allow_inherit and c_state.get("total") == total and isinstance(c_state.get("saved_pages"), dict):
            saved_pages = c_state["saved_pages"]

        self._log(f"  📄 共 {total} 条记录，按每页 {self.page_size} 条分 {total_pages} 页轮询处理...")

        for page_num in range(1, total_pages + 1):
            self.controller.check()
            start_seq = (page_num - 1) * self.page_size + 1
            end_seq = min(page_num * self.page_size, total)
            zip_filename = f"第{page_num:03d}页_{start_seq:03d}-{end_seq:03d}_登记证明文件.zip"
            zip_save_path = attach_dir / zip_filename

            # 检查该单页是否可从断点继承
            cached_page_rows = saved_pages.get(str(page_num))
            zip_ok = (not download_zip) or (zip_save_path.exists() and zip_save_path.stat().st_size > 100 and zipfile.is_zipfile(zip_save_path))
            if cached_page_rows and zip_ok:
                self._log(f"  ⏭️  [第 {page_num}/{total_pages} 页] 命中页级断点且附件完好，直接继承 ({len(cached_page_rows)} 条)")
                all_rows.extend(cached_page_rows)
                self._write_main_csv(main_csv_path, all_rows)
                self._emit_company(
                    org_name=org_name,
                    status="断点续跑中",
                    total=total,
                    page_progress=f"{page_num}/{total_pages}",
                    note=f"已继承第 {page_num} 页 ({cat_summary_str})",
                )
                continue

            self._log(f"\n  ── [第 {page_num}/{total_pages} 页 | 序号 {start_seq}~{end_seq}] ──")
            self._emit_company(
                org_name=org_name,
                status="分页采集中",
                total=total,
                page_progress=f"{page_num}/{total_pages}",
                note=f"正在读取第 {page_num}/{total_pages} 页表格...",
            )

            page_payload = {
                **query_ctx,
                "total": total,
                "pageSize": self.page_size,
                "pageNum": page_num,
            }
            list_res = self._post_json("/api/query/byDebtorName/listRegInfo", page_payload)
            if not list_res.get("success"):
                raise RuntimeError(f"获取第 {page_num} 页列表失败: {list_res.get('msg', list_res)}")

            raw_list = (list_res.get("data") or {}).get("list") or []
            self._log(f"    📋 成功获取本页表格记录 {len(raw_list)} 条")

            zip_rel_path = ""
            zip_entries: List[str] = []
            if download_zip and raw_list:
                self._human_sleep(1.5, 3.2, "浏览本页列表后准备点击下载附件")
                self._emit_company(
                    org_name=org_name,
                    status="下载附件中",
                    total=total,
                    page_progress=f"{page_num}/{total_pages}",
                    note=f"正在下载第 {page_num}/{total_pages} 页附件包...",
                )
                zip_entries = self._download_page_zip(page_payload, zip_save_path, allow_inherit=allow_inherit)
                if zip_save_path.exists():
                    zip_rel_path = f"附件/{zip_filename}"

            page_rows: List[dict] = []
            for idx, item in enumerate(raw_list):
                seq_no = item.get("serialNumber") or str(start_seq + idx)
                sub_reg_no = str(item.get("subRegistrationNo") or "")
                main_reg_no = str(item.get("registrationNo") or "")
                matched_pdf = self._match_files_in_zip(zip_entries, sub_reg_no, main_reg_no)

                page_rows.append({
                    "序号": seq_no,
                    "登记时间": item.get("registrationInureDateFormat") or format_epoch_ms(item.get("registrationInureDate")),
                    "登记证明编号": sub_reg_no,
                    "担保权人": item.get("pawneeName") or "",
                    "登记类型": item.get("registrationType") or "",
                    "担保类型": item.get("businessType") or "",
                    "登记到期日": format_epoch_ms(item.get("registrationExpireDate"))[:10],
                    "主登记号": main_reg_no,
                    "本地附件压缩包": zip_rel_path,
                    "包内对应证明文件": matched_pdf,
                    "在线核验链接": ONLINE_REGNUM_QUERY_URL,
                })

            all_rows.extend(page_rows)
            self._write_main_csv(main_csv_path, all_rows)
            self.checkpoint.update_company_page(org_name, total, total_pages, page_num, page_rows)

            if page_num < total_pages:
                if page_num % 10 == 0:
                    self._human_sleep(12.0, 18.0, f"已完成 {page_num} 页，模拟人工阶段性休息")
                else:
                    self._human_sleep(self.min_delay, self.max_delay, "准备翻到下一页")

        # 5. 全部页完成后写入分类统计、查询摘要并标记企业完成
        self._write_category_csv(cat_csv_path, org_name, total, cat_items, all_rows)
        summary_meta = {
            "orgName": org_name,
            "queryTime": (sum_data.get("queryReport") or {}).get("queryTime"),
            "queryFlowNo": query_ctx["queryFlowNo"],
            "regExcelFileNo": query_ctx["regExcelFileNo"],
            "total": total,
            "pages": total_pages,
            "exportedRows": len(all_rows),
            "officialCategories": cat_items,
        }
        meta_json_path.write_text(json.dumps(summary_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self.checkpoint.mark_company_completed(org_name, total, total_pages)

        self._emit_company(
            org_name=org_name,
            status="已完成",
            total=total,
            page_progress=f"{total_pages}/{total_pages}",
            note=f"成功导出 {len(all_rows)} 条记录 | {cat_summary_str}",
        )
        self._log(f"\n  🎉 企业 [{org_name}] 全部处理完毕！共 {len(all_rows)} 条明细，{total_pages} 页压缩包。")
        return {**summary_meta, "inherited": False, "company_dir": str(company_dir), "note": cat_summary_str}
