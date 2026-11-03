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
        m = re.search(r"(\d)\s*([+\-*])\s*(\d)", normalized)
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
            pred = self._ocr.classification(
                img_bytes, color_filter_colors=[target_color]
            ).strip()
            return (pred if pred else None), clean_tips, f"color:{target_color}"

        if "计算结果" in clean_tips:
            raw_pred = self._ocr.classification(img_bytes).strip()
            calc_ans = self.eval_arithmetic(raw_pred)
            return calc_ans, clean_tips, f"arithmetic(raw={raw_pred!r})"

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
