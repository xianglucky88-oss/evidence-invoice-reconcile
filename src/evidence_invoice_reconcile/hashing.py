"""证据哈希与规范化序列化。

哈希用于两个目的：

1. 原始文件指纹：OCR 文件、结构化开票接口报文各自计算 SHA-256，
   作为证据链锚点；指纹变化即触发新版本结论，不允许覆盖旧记录。
2. 规范化载荷指纹：对 JSON 数据按 key 排序、去空白后哈希，
   使输入顺序变化不改变指纹，而任何字段变化都会改变指纹。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def canonical_json(obj: Any) -> str:
    """键排序、无多余空白的确定性 JSON 文本（ensure_ascii=False）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(obj: Any) -> str:
    """任意可 JSON 化对象的规范化哈希。"""
    return sha256_text(canonical_json(obj))


def hash_evidence_refs(refs: Iterable[dict]) -> list:
    """为证据引用列表补算规范化哈希（证据本身已带 hash 时原样保留并校验格式）。"""
    out = []
    for ref in refs:
        item = dict(ref)
        digest = item.get("hash")
        if digest is None and "content" in item:
            digest = sha256_text(canonical_json(item["content"]))
        if digest is not None:
            digest = str(digest).lower()
            if len(digest) != 64:
                raise ValueError(f"证据哈希必须是 64 位十六进制: {item.get('source')!r}")
            int(digest, 16)  # 校验十六进制
            item["hash"] = digest
        out.append(item)
    return out
