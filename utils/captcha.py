# -*- coding: utf-8 -*-
"""
中登网双形态图形校验码识别与人工兜底模块
支持：
  1. 指定颜色文字验证码（基于 ddddocr 1.6+ 内置 HSV ColorFilter）
  2. 算术加减乘验证码（正则安全解析，不使用 eval）
  3. 识别不确定时自动落盘 config/captcha.png 转人工输入兜底
"""

import base64
import os
import re
from pathlib import Path
from typing import Optional, Tuple

import ddddocr

ALLOWED_DDD_COLORS = {
    "red", "blue", "green", "yellow", "orange",
    "purple", "cyan", "black", "white", "gray"
}

CN_COLOR_MAP = {
    "红": "red",
    "蓝": "blue",
    "绿": "green",
    "黄": "yellow",
    "橙": "orange",
    "紫": "purple",
    "青": "cyan",
    "黑": "black",
    "白": "white",
    "灰": "gray",
}


class CaptchaSolver:
    def __init__(self):
        self._ocr = ddddocr.DdddOcr(show_ad=False)
        try:
            self._ocr_beta = ddddocr.DdddOcr(show_ad=False, beta=True)
        except Exception:
            self._ocr_beta = self._ocr

    @staticmethod
    def _crop_arith_left(img_bytes: bytes) -> bytes:
        """裁掉算术图右侧约 1/3 的 '=?' 区域，并把高饱和彩色噪点抹成背景色"""
        try:
            import cv2
            import numpy as np

            bgr = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                return img_bytes
            crop = bgr[:, : int(bgr.shape[1] * 0.67)].copy()
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            bg = np.median(crop.reshape(-1, 3), axis=0).astype(np.uint8)
            crop[hsv[:, :, 1] > 80] = bg
            ok, enc = cv2.imencode(".png", crop)
            return enc.tobytes() if ok else img_bytes
        except Exception:
            return img_bytes

    _OP_MAP = {
        "+": "+", "t": "+", "T": "+", "f": "+", "F": "+", "十": "+", "÷": "+",
        "-": "-", "一": "-", "_": "-", "~": "-", "—": "-",
        "x": "*", "X": "*", "×": "*", "*": "*", ">": "*", "<": "*", "k": "*", "K": "*", "y": "*", "Y": "*",
    }

    @classmethod
    def _op_from_text(cls, text: str) -> Optional[str]:
        """提取【首位数字】与【第二个数字】之间的运算符字符（忽略末尾 '=' 被误识的 '-'）"""
        m = re.match(r"^\D*(\d)(\D+)(\d)", text or "")
        if not m:
            return None
        for ch in m.group(2):
            if ch in cls._OP_MAP:
                return cls._OP_MAP[ch]
        return None

    def solve_arithmetic(self, img_bytes: bytes) -> Tuple[Optional[str], str]:
        """
        中登网算术题固定为【1位数字 运算符(+ - ×) 1位数字 =?】（基于线上真值统计）：
          - 裁掉 '=?' 后识别首尾两位数字
          - 运算符：'+' 常识别为 '+'/'t'/'f'；'×' 常识别为 'x'/'>' 或在两数之间多出一个 '7'；'-' 常直接丢失
          - 多引擎投票确定运算符；运算符丢失时：a < b 视为乘法（结果不为负），否则视为减法
        """
        crop = self._crop_arith_left(img_bytes)
        raw_std = self._ocr.classification(crop).strip()
        raw_beta = self._ocr_beta.classification(crop).strip()
        raw_full = self._ocr.classification(img_bytes).strip()
        debug = f"std={raw_std!r},beta={raw_beta!r},full={raw_full!r}"

        # 1. 首尾数字
        digits = None
        for cand in (raw_std, raw_beta, raw_full):
            ds = [c for c in cand if c.isdigit()]
            if cand is raw_full:
                ds = ds[:3] if len(ds) >= 3 else ds  # 完整图末尾 '?' 常被识别为 2
                if len(ds) == 3 and not re.match(r"^\D*\d7\d", cand):
                    ds = ds[:2]
            if len(ds) >= 2:
                digits = (int(ds[0]), int(ds[-1]))
                break
        if not digits:
            return None, debug
        a, b = digits

        # 2. 运算符投票 —— 只在高置信时作答，否则返回 None 让调用方直接换一张（换图不消耗提交次数）
        #    线上真值统计：'+' / '×'(x > Y 或两数间多出 '7') 识别可靠；显式 '-' 常是被干扰的 '×'，放弃作答；
        #    运算符完全丢失时：a < b 为 '×'，否则为 '-'
        votes = {op for op in (self._op_from_text(raw_std), self._op_from_text(raw_full), self._op_from_text(raw_beta)) if op}
        if votes == {"+"}:
            return str(a + b), debug + ",op=+"
        if votes == {"*"}:
            return str(a * b), debug + ",op=*"
        if not votes:
            if any(re.match(r"^\D*\d7\d", c) for c in (raw_std, raw_full)) or a < b:
                return str(a * b), debug + ",op=*?"
            return str(a - b), debug + ",op=-?"
        return None, debug

    @staticmethod
    def clean_cjk(text: str) -> str:
        return re.sub(r"[^\u4e00-\u9fa5]", "", text or "")

    @staticmethod
    def strip_html(html_tips: str) -> str:
        """去除 HTML 标签，提取纯文本提示"""
        return re.sub(r"<[^>]+>", "", html_tips or "").strip()

    @staticmethod
    def save_base64_image(data_uri: str, save_path: Path) -> bytes:
        """解码 data:image/png;base64,... 并保存到本地文件"""
        b64_part = data_uri.split(",", 1)[1] if "," in data_uri else data_uri
        img_bytes = base64.b64decode(b64_part)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(img_bytes)
        return img_bytes

    @classmethod
    def detect_target_color(cls, html_tips: str) -> Optional[str]:
        """从 verifyTips HTML 中提取目标颜色名称（英文预设名）"""
        if not html_tips:
            return None

        # 优先匹配 <font color='blue'> 属性
        m_attr = re.search(r"color\s*=\s*['\"]?([a-zA-Z]+)", html_tips)
        if m_attr:
            col = m_attr.group(1).lower()
            if col in ALLOWED_DDD_COLORS:
                return col

        # 兜底匹配中文颜色描述（如“蓝色文字”、“红色文字”）
        clean_text = cls.strip_html(html_tips)
        for cn_key, en_val in CN_COLOR_MAP.items():
            if cn_key in clean_text:
                return en_val

        return None

    @staticmethod
    def eval_arithmetic(raw_ocr: str) -> Optional[str]:
        """安全解析个位数加减乘算术表达式（去除末尾 '=? ' 误识字符干扰），禁止裸 eval"""
        if not raw_ocr:
            return None
        normalized = (
            raw_ocr.replace("加", "+")
            .replace("减", "-")
            .replace("乘", "*")
            .replace("×", "*")
            .replace("x", "*")
            .replace("X", "*")
        )
        # 中登网算术验证码固定为【1位数字 运算符 1位数字 =?】，限制 \d 避免把末尾 '?' 误识的 '2' 拼进第二个数
        m = re.fullmatch(r"\s*(\d)\s*([+\-*])\s*(\d)\s*\d?\s*", normalized) or re.search(r"(\d)\s*([+\-*])\s*(\d)", normalized)
        if not m:
            return None
        a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
        if op == "+":
            return str(a + b)
        if op == "-":
            return str(a - b)
        if op == "*":
            return str(a * b)
        return None

    def try_auto_solve(self, img_bytes: bytes, html_tips: str) -> Tuple[Optional[str], str, str]:
        """
        尝试自动识别验证码。
        返回: (answer_or_None, clean_tips, mode)
          mode 取值: 'color:<color>' | 'arithmetic' | 'general'
        """
        clean_tips = self.strip_html(html_tips)
        target_color = self.detect_target_color(html_tips)

        if target_color:
            pred_beta = self.clean_cjk(self._ocr_beta.classification(img_bytes, color_filter_colors=[target_color]))
            pred_std = self.clean_cjk(self._ocr.classification(img_bytes, color_filter_colors=[target_color]))
            # 中登网颜色字固定 2 个汉字；优先使用对旋转汉字准确率更高的 beta 模型 (common.onnx)
            for pred in (pred_beta, pred_std):
                if len(pred) == 2:
                    return pred, clean_tips, f"color:{target_color}"
            return None, clean_tips, f"color:{target_color}(beta={pred_beta!r},std={pred_std!r})"

        if "计算结果" in clean_tips:
            calc_ans, dbg = self.solve_arithmetic(img_bytes)
            return calc_ans, clean_tips, f"arithmetic({dbg})"

        pred = self._ocr.classification(img_bytes).strip()
        return (pred if pred else None), clean_tips, "general"


def prompt_manual_captcha(captcha_path: Path, clean_tips: str, auto_open: bool = True) -> str:
    """当自动识别置信度不足时，打开本地图片并由人工输入校验码"""
    print(f"\n  🖼️  验证码已保存至: {captcha_path}")
    print(f"  📌 页面提示: {clean_tips}")
    if auto_open and os.name == "nt" and captcha_path.exists():
        try:
            os.startfile(str(captcha_path))
        except Exception:
            pass
    return input("  ⌨️  请查看图片并输入图形校验码 (直接回车则刷新一张): ").strip()
