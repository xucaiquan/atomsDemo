"""验收探针：A5 同项目两轮增量 + 旧功能保留；A7 源码/版本一致性；新会话恢复。

对线上 https://keegan.pub.atoms.world 执行，只创建归属于本探针匿名身份的临时项目。
"""
import hashlib
import json
import re
import sys
import time

import httpx

BASE = "https://keegan.pub.atoms.world"
ANON = "X-Atoms-Anon"
COOKIE = "atoms_anon"
POLL = 5.0


def log(*a):
    print(*a, flush=True)


# 计算器场景的算子控件。它们必须出现在**按钮文本**里才算数：裸子串判定是空转的
# ——任何 HTML 都含 `-`（`<!DOCTYPE html>`）、`.`、`/`（闭合标签），
# `"-" in html` 恒为真，旧功能是否保留根本判不出来。
ARITH_BUTTONS = {"+", "-", "×", "*", "÷", "/", "=", ".", "AC", "C", "±", "%"}

_BUTTON_RE = re.compile(r"<button\b[^>]*>(.*?)</button>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def buttons(html: str) -> set[str]:
    """页面里所有 <button> 的可见文本（去标签、去空白、去 HTML 实体）。"""
    out: set[str] = set()
    for raw in _BUTTON_RE.findall(html):
        text = _TAG_RE.sub("", raw).replace("&plusmn;", "±").replace("&times;", "×")
        text = " ".join(text.split())
        if text:
            out.add(text)
    return out


def check(failures: list[str], label: str, ok: bool, detail: str = "") -> bool:
    """记录一条判定：日志里可见，同时失败会进 failures 影响退出码。"""
    log(f"[{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label if not detail else f"{label}（{detail}）")
    return ok


def probe() -> int:
    results: dict[str, object] = {}
    failures: list[str] = []
    anon_key: str | None = None

    with httpx.Client(base_url=BASE, timeout=60.0) as c:
        def h() -> dict[str, str]:
            return {ANON: anon_key} if anon_key else {}

        r = c.get("/api/v1/atoms/projects", headers=h())
        body = r.json()
        anon_key = body.get("anon_key") or anon_key
        cookie_val = c.cookies.get(COOKIE)
        log(f"[identity] http={r.status_code} anon={anon_key} cookie={cookie_val} 一致={anon_key == cookie_val}")
        results["anon_key"] = anon_key
        results["anon_cookie_match"] = (anon_key == cookie_val)

        # 新会话恢复：全新 Client（无 cookie），只带 header，应能看见同一批项目
        with httpx.Client(base_url=BASE, timeout=60.0) as fresh:
            r2 = fresh.get("/api/v1/atoms/projects", headers={ANON: anon_key or ""})
            log(f"[新会话恢复] 全新 client（无 cookie，仅 header） http={r2.status_code} "
                f"项目数={len(r2.json().get('projects', []))}")
            results["new_session_via_header"] = r2.status_code
        # 全新 Client 且不带 header → 应看不见（隔离）
        with httpx.Client(base_url=BASE, timeout=60.0) as anon2:
            r3 = anon2.get("/api/v1/atoms/projects")
            titles = [p.get("title") for p in r3.json().get("projects", [])]
            log(f"[隔离] 全新身份 http={r3.status_code} 项目数={len(titles)} 标题={titles}")
            results["new_identity_sees_probe_project"] = any("验收" in (t or "") for t in titles)

        r = c.post("/api/v1/atoms/projects", json={"title": "两轮增量探针"}, headers=h())
        pid = r.json()["public_id"]
        log(f"[project] {pid}")
        results["project"] = pid

        def gen(prompt: str) -> int:
            r = c.post(f"/api/v1/atoms/projects/{pid}/generate",
                       json={"prompt": prompt}, headers=h())
            log(f"[generate] http={r.status_code} {r.text[:160]}")
            if r.status_code != 202:
                raise SystemExit(f"generate 未被受理: {r.text[:200]}")
            return r.json()["version_seq"]

        def wait(seq: int, budget: float = 1500.0) -> dict:
            t0, last = time.time(), None
            while time.time() - t0 < budget:
                snap = c.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}/steps",
                             headers=h()).json()
                if snap.get("status") != last:
                    last = snap.get("status")
                    log(f"   [{time.time()-t0:7.1f}s] {last} "
                        f"{[s['status'] for s in snap.get('steps', [])]}")
                if last in ("succeeded", "failed", "cancelled"):
                    snap["_elapsed"] = time.time() - t0
                    return snap
                time.sleep(POLL)
            return {"status": "TIMEOUT", "_elapsed": time.time() - t0}

        def ver(seq: int) -> dict:
            return c.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}", headers=h()).json()

        P1 = ("做一个计算器，支持加减乘除，有数字键盘和显示屏，"
              "支持小数点与正负号，键盘也能输入，有 AC 清除键")
        P2 = "继续刚刚的需求，再加一个历史记录区域，能回看最近几次计算"

        log("\n=== 轮次 1：从零创建 ===")
        s1 = wait(gen(P1))
        log(f"[A5-1] {s1['status']} {s1['_elapsed']:.1f}s err={s1.get('error')} type={s1.get('error_type')}")
        if s1["status"] != "succeeded":
            log("[!] 轮次 1 未成功，终止")
            results["round1"] = s1["status"]
            # 早退同样是失败：TIMEOUT / failed / cancelled 都不是「A5 通过」。
            # （旧实现这里只在 round2 == "failed" 时返回 1，轮次 1 超时反而 exit 0。）
            failures.append(f"轮次 1 未成功：{s1['status']}")
            return _dump(results, failures)
        v1 = ver(1); h1 = v1["html"]
        log(f"[A5-1] len={len(h1)} sha={v1['html_sha256'][:12]}…")
        results["r1"] = {"len": len(h1), "sha": v1["html_sha256"], "elapsed": s1["_elapsed"]}

        log("\n=== 轮次 2：指代型增量 ===")
        s2 = wait(gen(P2))
        log(f"[A5-2] {s2['status']} {s2['_elapsed']:.1f}s err={s2.get('error')} type={s2.get('error_type')}")
        if s2["status"] != "succeeded":
            log("[!] 轮次 2 未成功")
            results["round2"] = s2["status"]
            failures.append(f"轮次 2 未成功：{s2['status']}")
            return _dump(results, failures)
        v2 = ver(2); h2 = v2["html"]
        log(f"[A5-2] len={len(h2)} sha={v2['html_sha256'][:12]}…")
        results["r2"] = {"len": len(h2), "sha": v2["html_sha256"], "elapsed": s2["_elapsed"]}

        log("\n--- A5 断言（产物级：看第二轮页面里留下了什么，不看注入了什么） ---")
        # ① 长度比：第二轮不得比第一轮大幅缩水
        ratio = len(h2) / max(len(h1), 1)
        check(failures, "A5 长度比 seq2/seq1 >= 0.80",
              ratio >= 0.8, f"实际 {ratio:.2f}（{len(h1)} → {len(h2)}）")
        results["ratio"] = ratio

        # ② 旧功能保留：第一轮的**算子控件**必须在第二轮的按钮集合里仍然存在。
        #    用按钮文本而不是裸子串——后者任何 HTML 都满足，等于没测。
        b1, b2 = buttons(h1), buttons(h2)
        ops1 = {t for t in b1 if t in ARITH_BUTTONS}
        log(f"[A5] 轮次1 算子按钮 = {sorted(ops1)}")
        log(f"[A5] 轮次2 按钮总数 = {len(b2)}")
        check(failures, "A5 第一轮确实产出了可识别的算子按钮",
              len(ops1) >= 4, f"只有 {sorted(ops1)}")
        lost = sorted(ops1 - b2)
        check(failures, "A5 旧功能保留（第一轮算子按钮第二轮仍在）",
              not lost, f"丢失 {lost}" if lost else f"全部保留 {sorted(ops1)}")
        results["ops_round1"] = sorted(ops1)
        results["ops_lost"] = lost

        # ③ 新增能力：第二轮要求的历史记录区域必须真的落地
        check(failures, "A5 新增能力「历史」出现在第二轮产物",
              "历史" in h2)
        results["added_history"] = "历史" in h2

        log("\n--- A7 断言（源码/版本一致性、文档完整性、旧版本不被覆写） ---")
        for tag, v, hh in (("v1", v1, h1), ("v2", v2, h2)):
            sha_ok = hashlib.sha256(hh.encode()).hexdigest() == v["html_sha256"]
            ends = hh.rstrip().endswith("</html>")
            bodies = len(re.findall(r"<body", hh, re.I))
            doctypes = len(re.findall(r"<!DOCTYPE", hh, re.I))
            log(f"[A7] {tag} len={len(hh)} sha自洽={sha_ok} 结尾</html>={ends} "
                f"body={bodies} doctype={doctypes}")
            check(failures, f"A7 {tag} html_sha256 与 html 自洽", sha_ok)
            # 摘要自洽：以 </html> 收尾（截断产物绝不放过）
            check(failures, f"A7 {tag} 以 </html> 结尾", ends)
            # 文档完整性：不得是两篇文档拼接或缺失骨架
            check(failures, f"A7 {tag} 恰好一个 <body> 与一个 <!DOCTYPE>",
                  bodies == 1 and doctypes == 1, f"body={bodies} doctype={doctypes}")

        # 关键：第二轮之后，第一轮版本必须原样可回取（逐字节不变）
        v1_again = ver(1)
        sha_same = v1_again["html_sha256"] == v1["html_sha256"]
        byte_same = v1_again["html"] == h1
        log(f"[A7] 第二轮后回取 v1：sha 未变={sha_same} html 逐字节未变={byte_same}")
        check(failures, "A7 第二轮后回取 v1 摘要未变", sha_same)
        check(failures, "A7 第二轮后回取 v1 html 逐字节不变", byte_same,
              "" if byte_same else f"长度 {len(h1)} → {len(v1_again['html'] or '')}")
        results["v1_immutable"] = sha_same and byte_same

        # 归属隔离：此刻项目里已有两版真实 HTML，是最值得验证「别的身份看不到」的时刻。
        # 开头那次隔离检查发生在项目创建之前，且用「验收」匹配标题——而本项目标题是
        # 「两轮增量探针」，所以它无论是否泄漏都恒为 False，是空转的。这里按 public_id
        # 判定：新身份既不能在列表里看到它，直接取详情也必须 404。
        with httpx.Client(base_url=BASE, timeout=60.0) as other:
            seen = [p.get("public_id") for p in other.get("/api/v1/atoms/projects").json().get("projects", [])]
            leak_in_list = pid in seen
            other_detail = other.get(f"/api/v1/atoms/projects/{pid}")
            check(failures, "隔离 新身份的项目列表不含本探针项目", not leak_in_list)
            check(failures, "隔离 新身份直接取本项目详情返回 404",
                  other_detail.status_code == 404, f"实际 {other_detail.status_code}")
            results["isolation"] = {"leak_in_list": leak_in_list,
                                    "detail_status": other_detail.status_code}

        # 新会话恢复（真实语义）：换新 client，带上 anon，应能取回同一项目的两轮版本与 html
        with httpx.Client(base_url=BASE, timeout=60.0) as fresh:
            det = fresh.get(f"/api/v1/atoms/projects/{pid}", headers={ANON: anon_key or ""}).json()
            vs = [(v["seq"], v["status"]) for v in det.get("versions", [])]
            got = fresh.get(f"/api/v1/atoms/projects/{pid}/versions/2",
                            headers={ANON: anon_key or ""}).json()
            log(f"[新会话恢复] 项目详情 versions={vs} messages={len(det.get('messages', []))} 条")
            log(f"[新会话恢复] 新 client 取回 v2 len={len(got.get('html') or '')} "
                f"sha一致={got.get('html_sha256') == v2['html_sha256']}")
            results["new_session_versions"] = vs
            results["new_session_sha_ok"] = (got.get("html_sha256") == v2["html_sha256"])
            check(failures, "新会话（仅 header）能取回两轮版本",
                  [s for s, _ in vs] == [1, 2], f"versions={vs}")
            check(failures, "新会话取回的 v2 与创建会话摘要一致",
                  results["new_session_sha_ok"])

    log(f"\n[probe project] {pid}")
    return _dump(results, failures)


def _dump(results: dict, failures: list[str]) -> int:
    """落盘证据并给出退出码：任何一条判定失败即非零退出。"""

    log("=== SUMMARY_JSON ===")
    log(json.dumps(results, ensure_ascii=False, indent=2))
    if failures:
        log(f"\n=== FAILURES ({len(failures)}) ===")
        for item in failures:
            log(f"  - {item}")
        return 1
    log("\n=== 全部判定通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(probe())
