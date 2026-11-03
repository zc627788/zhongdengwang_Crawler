# -*- coding: utf-8 -*-
"""
中登网登录密码摘要工具
对应前端:
  - /common/md5/md5.js -> hex_md5(password)
  - /common/sm3/sm3.js -> sm3(password) (标准 GM/T 0004-2012 SM3)
"""

import hashlib
from cryptography.hazmat.primitives import hashes


def calc_md5(plain_text: str) -> str:
    """计算 32 位小写 MD5 十六进制摘要"""
    return hashlib.md5(plain_text.encode("utf-8")).hexdigest().lower()


def calc_sm3(plain_text: str) -> str:
    """计算 64 位小写国密 SM3 十六进制摘要"""
    digest = hashes.Hash(hashes.SM3())
    digest.update(plain_text.encode("utf-8"))
    return digest.finalize().hex().lower()


def build_password_digests(password: str) -> dict:
    """生成 loginCheck 所需的双密码摘要字段"""
    return {
        "passWord": calc_md5(password),
        "passWordSm3": calc_sm3(password),
    }

