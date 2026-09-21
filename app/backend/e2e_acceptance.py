"""线上验收探针（只读性检查 + 真实端到端生成）。

对 https://keegan.pub.atoms.world 执行：
  A4 两类需求：从零创建（计算器）
  A5 两轮增量：第二轮指代型短句，核对第一轮功能是否保留
  A7 源码/Preview 一致性：GET /versions/{seq} 的 html 与 html_sha256 自洽

仅创建归属于本探针匿名身份的临时项目，不触碰既有数据。
"""
import hashlib
import json
import re
import sys
import time

import httpx

BASE = "https://keegan.pub.atoms.world"
ANON_HEADER = "X-Atoms-Anon"
POLL_INTERVAL = 5.0


def log(*a):
    print(*a, flush=True)


def main() -> int:
    jar: dict[str, str] = {}
    anon_key: str | None = None

    def headers() -> dict[str, str]:
        return {ANON_HEADER: anon_key} if anon_key else {}

    with httpx.Client(base_url=BASE, timeout=60.0) as c:
        # ---- 建立匿名身份 ----
        r = c.get("/api/v1/atoms/projects", headers=headers())
        body = r.json()
        anon_key = body.get("anon_key") or anon_key
        log(f"[identity] status={r.status_code} anon_key={anon_key} "
            f"cookie_set={'atoms_anon' in c.cookies}")

        # ---- 建项目 ----
        r = c.post("/api/v1/atoms/projects", json={"title": "验收探针"}, headers=headers())
        pid = r.json()["public_id"]
        log(f"[project] {pid}")

        def generate(prompt: str) -> int:
            r = c.post(f"/api/v1/atoms/projects/{pid}/generate",
                       json={"prompt": prompt}, headers=headers())
            log(f"[generate] http={r.status_code} body={r.text[:200]}")
            if r.status_code != 202:
                raise SystemExit("generate 未被受理")
            return r.json()["version_seq"]

        def poll(seq: int, budget_s: float = 420.0) -> dict:
            t0 = time.time()
            last = None
            while time.time() - t0 < budget_s:
                r = c.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}/steps",
                          headers=headers())
                snap = r.json()
                if snap.get("status") != last:
                    last = snap.get("status")
                    log(f"  [{time.time()-t0:6.1f}s] status={last} "
                        f"steps={[s['status'] for s in snap.get('steps', [])]}")
                if snap.get("status") in ("succeeded", "failed", "cancelled"):
                    snap["_elapsed"] = time.time() - t0
                    return snap
                time.sleep(POLL_INTERVAL)
            return {"status": "TIMEOUT", "_elapsed": time.time() - t0}

        def fetch(seq: int) -> dict:
            r = c.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}", headers=headers())
            return r.json()

        # ================= 轮次 1：从零创建 =================
        log("\n=== 轮次 1：从零创建（计算器） ===")
        s1 = generate("做一个计算器，支持加减乘除，有数字键盘和显示屏，"
                      "支持小数点与正负号，键盘也能输入")
        snap1 = poll(s1)
        log(f"[round1] status={snap1['status']} elapsed={snap1['_elapsed']:.1f}s "
            f"error={snap1.get('error')} error_type={snap1.get('error_type')}")
        if snap1["status"] != "succeeded":
            log("!! 轮次 1 未成功，后续断言跳过")
            return 1

        v1 = fetch(s1)
        h1 = v1["html"]
        log(f"[round1] seq={v1['seq']} len={len(h1)} sha256={v1['html_sha256'][:16]}…")
        # A7：后端 sha256 必须等于客户端对同一 html 的计算
        local_sha = hashlib.sha256(h1.encode()).hexdigest()
        log(f"[A7] sha256 自洽={'OK' if local_sha == v1['html_sha256'] else 'MISMATCH'}")
        log(f"[A7] 以 </html> 结尾={h1.rstrip().endswith('</html>')} "
            f"<body 计数={len(re.findall(r'<body', h1, re.I))} "
            f"doctype 计数={len(re.findall(r'<!DOCTYPE', h1, re.I))}")
        # 第一轮功能特征串
        feats1 = [w for w in ("+", "-", "*", "/") if w in h1]
        log(f"[round1] 运算符出现={feats1}")

        # ================= 轮次 2：指代型增量 =================
        log("\n=== 轮次 2：指代型增量（继续刚刚的需求） ===")
        s2 = generate("继续刚刚的需求，再加一个历史记录区域，能回看最近几次计算")
        snap2 = poll(s2)
        log(f"[round2] status={snap2['status']} elapsed={snap2['_elapsed']:.1f}s "
            f"error={snap2.get('error')} error_type={snap2.get('error_type')}")
        if snap2["status"] != "succeeded":
            log("!! 轮次 2 未成功")
            return 1

        v2 = fetch(s2)
        h2 = v2["html"]
        log(f"[round2] seq={v2['seq']} len={len(h2)} sha256={v2['html_sha256'][:16]}…")

        # A5：增量而非推倒重来
        ratio = len(h2) / max(len(h1), 1)
        log(f"[A5] 长度比 round2/round1 = {ratio:.2f}（期望 >= 0.8）")
        # 旧功能保留：第一轮的运算符 / 关键结构是否仍在
        kept = [w for w in ("+", "-", "*", "/") if w in h2]
        log(f"[A5] 第二轮仍含运算符={kept}")
        log(f"[A5] 第二轮新增「历史」字样={'历史' in h2}")

        # ---- 项目详情：核对两轮版本都在 ----
        r = c.get(f"/api/v1/atoms/projects/{pid}", headers=headers())
        vs = r.json()["versions"]
        log(f"\n[detail] 版本数={len(vs)} "
            f"{[(v['seq'], v['status']) for v in vs]}")
        log(f"[detail] messages={len(r.json()['messages'])} 条")

        log(f"\n[probe project] {pid}  ← 可手动打开核对")
    return 0


if __name__ == "__main__":
    sys.exit(main())
