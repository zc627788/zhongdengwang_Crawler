# -*- coding: utf-8 -*-
"""
中登网（动产融资统一登记公示系统）纯协议登录与会话持久化模块

完整协议链路（对齐 /template/script/login.js 与 /out/ SPA 拦截器）：
  1. POST /api/user/tool/token                  -> 获取 loginToken
  2. POST /api/user/tool/verifyCode             -> 获取图形验证码 Base64 与提示语 tips
  3. POST /api/user/loginController/loginCheck  -> 校验账号 + MD5/SM3双摘要密码 + 图形验证码
     - 若 loginStart == "0": 直接获得最终 authToken
     - 否则返回脱敏手机号 mbNumber 与临时令牌 loginTempToken
  4. POST /api/user/tool/sendDynamicCode        -> 触发手机短信动态码下发
  5. POST /api/user/loginController/login       -> 提交短信动态码，换取最终 authToken
  6. 持久化 authToken 至 config/session.json（后续所有业务接口通过 Header `accessToken: <authToken>` 鉴权）
"""

import json
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from curl_cffi import requests as cffi_requests

from .captcha import CaptchaSolver, prompt_manual_captcha
from .paths import DEFAULT_HEADERS
from .sign import build_password_digests

BASE_URL = "https://www.zhongdengwang.org.cn"


class ZhongdengAuthClient:
    def __init__(
        self,
        config_dir: Path,
        logger: Optional[Callable[[str], None]] = None,
        manual_captcha_fn: Optional[Callable[[Path, str], str]] = None,
    ):
        self.config_dir = config_dir
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.headers_file = config_dir / "headers.json"
        self.session_file = config_dir / "session.json"
        self.pending_file = config_dir / "login_pending.json"
        self.captcha_file = config_dir / "captcha.png"

        self._logger = logger
        self._manual_captcha_fn = manual_captcha_fn

        self.session = cffi_requests.Session(impersonate="chrome120")
        if self.headers_file.exists():
            try:
                default_headers = json.loads(self.headers_file.read_text(encoding="utf-8"))
            except Exception:
                default_headers = DEFAULT_HEADERS
        else:
            default_headers = DEFAULT_HEADERS
            self.headers_file.write_text(
                json.dumps(DEFAULT_HEADERS, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        self.session.headers.update(default_headers)
        self.solver = CaptchaSolver()
        self.log_dir = self.config_dir.parent / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _log(self, msg: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        log_file = self.log_dir / f"zhongdeng_{time.strftime('%Y%m%d')}.log"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg.lstrip()}\n")
        except Exception:
            pass
        if self._logger:
            self._logger(msg)
        else:
            print(msg, flush=True)

    def reset_connection_pool(self):
        """重建底层 curl_cffi Session 连接池（保留当前 headers 与 cookies），彻底消除长耗时下载后的半开死连接 (curl 56)"""
        old_headers = dict(self.session.headers)
        old_cookies = dict(self.session.cookies)
        try:
            self.session.close()
        except Exception:
            pass
        self.session = cffi_requests.Session(impersonate="chrome120")
        self.session.headers.update(old_headers)
        for k, v in old_cookies.items():
            self.session.cookies.set(k, v)

    def _request_json(self, method: str, path: str, max_retries: int = 3, **kwargs) -> dict:
        """带瞬态断连 (curl 56 / Connection reset / Timeout) 自动重连重试的 JSON 请求封装"""
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                return resp.json()
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    self.reset_connection_pool()
                    time.sleep(0.8 * attempt)
                else:
                    raise last_exc

    def check_session_valid(self, access_token: str) -> Tuple[bool, Optional[dict]]:
        """调用 /out/ SPA 的当前登录用户信息接口验证 accessToken 是否有效"""
        if not access_token:
            return False, None
        try:
            data = self._request_json(
                "POST",
                "/api/user/subsidiary/userLoginInfo",
                json={},
                headers={"accessToken": access_token},
                timeout=15,
            )
            if data.get("success"):
                return True, data.get("data")
            return False, data
        except Exception:
            return False, None

    def load_valid_session(self) -> Optional[dict]:
        """读取本地 config/session.json 并在线校验 accessToken 有效性"""
        if not self.session_file.exists():
            return None
        try:
            saved = json.loads(self.session_file.read_text(encoding="utf-8"))
        except Exception:
            return None

        token = saved.get("accessToken") or saved.get("authToken")
        ok, user_info = self.check_session_valid(token)
        if ok:
            saved["userInfo"] = user_info
            return saved
        return None

    def save_session(self, auth_token: str, username: str, user_info: Optional[dict] = None) -> dict:
        """持久化登录凭证至 config/session.json"""
        payload = {
            "username": username,
            "authToken": auth_token,
            "accessToken": auth_token,
            "headers_for_api": {
                "accessToken": auth_token,
                "Content-Type": "application/json",
            },
            "cookies": dict(self.session.cookies),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "userInfo": user_info,
        }
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.session_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if self.pending_file.exists():
            try:
                self.pending_file.unlink()
            except Exception:
                pass
        return payload

    def _get_login_token(self) -> str:
        res = self._request_json(
            "POST",
            "/api/user/tool/token",
            json={"type": "4"},
            timeout=15,
        )
        if not res.get("success") or not res.get("data", {}).get("token"):
            raise RuntimeError(f"获取 loginToken 失败: {res}")
        return res["data"]["token"]

    def _fetch_verify_code(self, login_token: str) -> Tuple[bytes, str]:
        res = self._request_json(
            "POST",
            "/api/user/tool/verifyCode",
            data={"type": "4", "token": login_token},
            timeout=15,
        )
        if not res.get("success") or not res.get("data", {}).get("url"):
            raise RuntimeError(f"获取图形校验码失败: {res}")
        data_uri = res["data"]["url"]
        tips = res["data"].get("tips", "")
        img_bytes = self.solver.save_base64_image(data_uri, self.captcha_file)
        return img_bytes, tips

    def send_sms_step(
        self,
        username: str,
        password: str,
        max_auto_attempts: int = 5,
        allow_manual_fallback: bool = True,
        ask_before_send: bool = False,
    ) -> dict:
        """
        阶段 1 & 2：过图形校验码 + 账密预检 + 触发下发手机短信动态码，
        并将待验证上下文保存至 config/login_pending.json。
        """
        self._log(f"⏳ [1/4] 初始化登录令牌 (账号: {username})...")
        login_token = self._get_login_token()
        pwd_digests = build_password_digests(password)

        check_data = None
        verified_code = ""
        auto_try = 0

        while True:
            img_bytes, html_tips = self._fetch_verify_code(login_token)
            auto_ans, clean_tips, mode = self.solver.try_auto_solve(img_bytes, html_tips)

            if auto_try < max_auto_attempts:
                auto_try += 1
                if auto_ans:
                    self._log(f"  🤖 [第 {auto_try}/{max_auto_attempts} 次自动识别] 模式={mode} | 提示='{clean_tips}' -> 识别结果: {auto_ans}")
                    verify_code = auto_ans
                else:
                    self._log(f"  🔄 [第 {auto_try}/{max_auto_attempts} 次自动识别] 模式={mode} 运算符置信度低，自动刷新换一张...")
                    time.sleep(0.5)
                    continue
            else:
                if self._manual_captcha_fn:
                    verify_code = self._manual_captcha_fn(self.captcha_file, clean_tips).strip()
                    if not verify_code:
                        raise RuntimeError("用户取消了图形校验码输入")
                elif allow_manual_fallback:
                    verify_code = prompt_manual_captcha(self.captcha_file, clean_tips)
                    if not verify_code:
                        continue
                else:
                    raise RuntimeError(f"连续 {max_auto_attempts} 次图形校验码自动识别未通过，请查看 {self.captcha_file}")

            payload = {
                **pwd_digests,
                "token": login_token,
                "type": "4",
                "userCode": username,
                "verifyCode": verify_code,
            }
            res = self._request_json(
                "POST",
                "/api/user/loginController/loginCheck",
                json=payload,
                timeout=15,
            )

            if res.get("success"):
                check_data = res.get("data") or {}
                verified_code = verify_code
                self._log("  ✅ 账号密码及图形校验码验证通过！")
                break

            msg = res.get("msg", "未知错误")
            self._log(f"  ⚠️  loginCheck 未通过: {msg}")
            if any(k in msg for k in ("密码", "不存在", "锁定", "冻结", "停用")):
                raise RuntimeError(f"登录终止（请检查账号密码）: {msg}")
            time.sleep(0.6)

        # 分支 A：无需短信验证直接下发正式 authToken (loginStart == "0")
        if str(check_data.get("loginStart")) == "0" and check_data.get("authToken"):
            auth_token = check_data["authToken"]
            _, user_info = self.check_session_valid(auth_token)
            saved = self.save_session(auth_token, username, user_info)
            self._log(f"✅ 登录完成（免短信分支），登录态已保存至: {self.session_file}")
            return {"status": "done", "session": saved}

        # 分支 B：标准手机号动态码二次验证
        mb_number = check_data.get("mbNumber") or ""
        login_temp_token = check_data.get("authToken") or ""
        if not mb_number:
            raise RuntimeError(f"账号需绑定手机号或返回异常状态，响应数据: {check_data}")

        self._log(f"📱 [2/4] 触发手机号二次验证 (脱敏号码: {mb_number})")
        if ask_before_send:
            confirm_send = input("  ⌨️  是否立即发送短信动态码到该手机？[Y/n]: ").strip().lower()
            if confirm_send in ("n", "no"):
                raise RuntimeError("用户取消发送短信动态码")

        send_payload = {
            "mbNumber": mb_number,
            "newMbNumber": "",
            "modelType": "string",
            "token": login_token,
            "userCode": username,
            "validataCode": verified_code,
            "verifyType": "4",
        }
        send_resp = self._request_json(
            "POST",
            "/api/user/tool/sendDynamicCode",
            json=send_payload,
            timeout=15,
        )
        if not send_resp.get("success"):
            raise RuntimeError(f"发送短信动态码失败: {send_resp.get('msg', send_resp)}")

        msg_text = send_resp.get("msg", "短信动态码已发送，请注意查收（120秒内有效）")
        self._log(f"  📩 短信下发成功: {msg_text}")

        pending = {
            "username": username,
            "mbNumber": mb_number,
            "loginToken": login_token,
            "loginTempToken": login_temp_token,
            "verifyCode": verified_code,
            "verifyType": "4",
            "cookies": dict(self.session.cookies),
            "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.pending_file.write_text(
            json.dumps(pending, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"status": "sms_sent", "pending": pending, "msg": msg_text}

    def complete_sms_step(self, dynamic_code: str) -> dict:
        """阶段 3 & 4：读取 config/login_pending.json 提交短信动态码，换取并保存最终 authToken"""
        if not self.pending_file.exists():
            raise RuntimeError("未找到待验证的短信会话，请先点击【验证账密并发短信】。")

        pending = json.loads(self.pending_file.read_text(encoding="utf-8"))
        for k, v in (pending.get("cookies") or {}).items():
            self.session.cookies.set(k, v)

        login_payload = {
            "dynamicCode": dynamic_code.strip(),
            "mbNumber": pending["mbNumber"],
            "token": pending["loginToken"],
            "loginTempToken": pending["loginTempToken"],
            "type": pending.get("verifyType", "4"),
            "userCode": pending["username"],
            "verifyCode": pending["verifyCode"],
        }
        final_resp = self._request_json(
            "POST",
            "/api/user/loginController/login",
            json=login_payload,
            timeout=15,
        )

        if not final_resp.get("success"):
            raise RuntimeError(f"动态码验证未通过: {final_resp.get('msg', final_resp)}")

        auth_token = (final_resp.get("data") or {}).get("authToken")
        if not auth_token:
            raise RuntimeError(f"登录响应成功但未找到 authToken: {final_resp}")

        self._log("  🎉 [4/4] 短信动态码校验成功，正在校验并持久化会话...")
        _, user_info = self.check_session_valid(auth_token)
        saved = self.save_session(auth_token, pending["username"], user_info)
        self._log(f"  ✅ 登录信息已完整保存至: {self.session_file}")
        return saved

    def login(self, username: str, password: str, max_auto_attempts: int = 5) -> dict:
        """交互式完整登录流程（CLI 模式）"""
        step_res = self.send_sms_step(
            username=username,
            password=password,
            max_auto_attempts=max_auto_attempts,
            allow_manual_fallback=True,
            ask_before_send=False,
        )
        if step_res["status"] == "done":
            return step_res["session"]

        while True:
            dynamic_code = input("\n⌨️  [3/4] 请输入手机收到的短信动态码 (输入 q 退出): ").strip()
            if dynamic_code.lower() == "q":
                raise RuntimeError("用户主动退出短信验证")
            if not dynamic_code:
                continue
            try:
                return self.complete_sms_step(dynamic_code)
            except Exception as exc:
                print(f"  ❌ {exc}，请重新输入。")
