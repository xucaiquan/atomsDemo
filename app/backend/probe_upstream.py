"""隔离实验：直接调用平台 aihub 的 gentxt，对比阶段1/阶段2 载荷。

目的：判定线上「阶段2 必挂」是**上游拒绝了阶段2的载荷**，还是**流水线自身代码**。
用与线上完全相同的 prompt 构造与模型选型。
"""
import sys
import time

import httpx

sys.path.insert(0, ".")
from services import prompts  # noqa: E402

BASE = "https://keegan.pub.atoms.world"
FAST_MODEL = "deepseek-v4-flash"

PROMPT = ("做一个计算器，支持加减乘除，有数字键盘和显示屏，"
          "支持小数点与正负号，键盘也能输入")
# 线上步骤1 的真实产出（从 /versions/1/steps 取回）
ANALYSIS_RAW = """{
  "app_name": "简易计算器",
  "features": ["显示屏实时显示当前输入与算式结果", "数字键盘 0-9、小数点与正负号切换",
               "加减乘除四则运算与等号求值", "清除键 AC 与退格键删除末位",
               "支持物理键盘数字、运算符与回车输入"],
  "notes": "面向日常快速算数的单页计算器，打开即用。"
}"""


def call(label: str, model: str, system: str, user: str, max_tokens: int) -> None:
    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "model": model,
        "max_tokens": max_tokens,
    }
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}/api/v1/aihub/gentxt", json=payload, timeout=180.0)
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] 传输层异常 after {time.time()-t0:.1f}s: "
              f"{type(exc).__name__}: {exc}")
        return
    dt = time.time() - t0
    print(f"[{label}] http={r.status_code} elapsed={dt:.1f}s "
          f"payload_chars={len(user)}")
    print(f"    body[:500]={r.text[:500]!r}")


def main() -> int:
    # 阶段 1 载荷（线上已验证成功）
    call("stage1", FAST_MODEL, prompts.ANALYZE_SYSTEM,
         prompts.build_analyze_user(PROMPT, None, []), 4096)
    # 阶段 2 载荷（线上必挂）
    call("stage2", FAST_MODEL, prompts.DESIGN_SYSTEM,
         prompts.build_design_user(PROMPT, ANALYSIS_RAW, None, []), 4096)
    # 阶段 3 载荷（从未被执行到）
    call("stage3", "deepseek-v4-pro", prompts.CODE_SYSTEM,
         prompts.build_code_user(PROMPT, ANALYSIS_RAW, "{}", None, []), 16384)
    return 0


if __name__ == "__main__":
    sys.exit(main())
