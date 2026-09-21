"""阶段 3 超时根因复现：用「贪吃蛇」载荷实测单次调用耗时。

探针已证实计算器的阶段 3 耗时 105.0s，逼近 STAGE_TIMEOUT=120s。
本脚本用更复杂的贪吃蛇需求复现越线，并记录是否出现网关 524。
"""
import sys
import time

import httpx

sys.path.insert(0, ".")
from services import prompts  # noqa: E402

BASE = "https://keegan.pub.atoms.world"
PROMPT = "生成一个贪吃蛇小游戏"
ANALYSIS_RAW = """{
  "app_name": "贪吃蛇",
  "features": ["方向键控制蛇移动", "吃到食物变长并加分", "撞墙或撞到自身游戏结束",
               "实时分数与最高分展示", "开始/暂停/重新开始"],
  "notes": "经典单页贪吃蛇小游戏，打开即玩。"
}"""
DESIGN_RAW = """{
  "layout": "单页居中布局：顶部标题与分数栏，中部 Canvas 游戏区，底部操作说明与按钮",
  "components": ["分数栏", "Canvas 画布", "开始/暂停按钮", "游戏结束遮罩"]
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
        r = httpx.post(f"{BASE}/api/v1/aihub/gentxt", json=payload, timeout=300.0)
    except Exception as exc:  # noqa: BLE001
        print(f"[{label}] 传输层异常 after {time.time()-t0:.1f}s: "
              f"{type(exc).__name__}: {exc}")
        return
    dt = time.time() - t0
    body = r.text
    print(f"[{label}] http={r.status_code} elapsed={dt:.1f}s chars={len(body)}")
    print(f"    超过 STAGE_TIMEOUT(120s)? {'是' if dt > 120 else '否'}")
    print(f"    body[:200]={body[:200]!r}")


def main() -> int:
    call("stage3-snake", "deepseek-v4-pro", prompts.CODE_SYSTEM,
         prompts.build_code_user(PROMPT, ANALYSIS_RAW, DESIGN_RAW, None, []),
         16384)
    return 0


if __name__ == "__main__":
    sys.exit(main())
