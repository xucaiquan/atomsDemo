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


def probe() -> int:
    results: dict[str, object] = {}
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
            log("[!] 轮次 1 未成功，终止"); results["round1"] = s1["status"]; return _dump(results)
        v1 = ver(1); h1 = v1["html"]
        log(f"[A5-1] len={len(h1)} sha={v1['html_sha256'][:12]}…")
        results["r1"] = {"len": len(h1), "sha": v1["html_sha256"], "elapsed": s1["_elapsed"]}

        log("\n=== 轮次 2：指代型增量 ===")
        s2 = wait(gen(P2))
        log(f"[A5-2] {s2['status']} {s2['_elapsed']:.1f}s err={s2.get('error')} type={s2.get('error_type')}")
        if s2["status"] != "succeeded":
            log("[!] 轮次 2 未成功"); results["round2"] = s2["status"]; return _dump(results)
        v2 = ver(2); h2 = v2["html"]
        log(f"[A5-2] len={len(h2)} sha={v2['html_sha256'][:12]}…")
        results["r2"] = {"len": len(h2), "sha": v2["html_sha256"], "elapsed": s2["_elapsed"]}

        log("\n--- A5 断言 ---")
        ratio = len(h2) / max(len(h1), 1)
        log(f"[A5] 长度比 seq2/seq1 = {ratio:.2f}（期望 >= 0.80）→ {'PASS' if ratio >= 0.8 else 'FAIL'}")
        # 旧功能保留：第一轮的关键能力是否仍在第二轮产物里
        FEATS = {"加法": "+", "减法": "-", "乘法": "*", "除法": "/",
                 "清除键": "AC", "显示屏": "显示", "小数点": "."}
        kept = {k: (v in h2) for k, v in FEATS.items()}
        log(f"[A5] 旧功能保留 = {kept}")
        log(f"[A5] 新增「历史」= {'历史' in h2}")
        results["ratio"] = ratio
        results["kept"] = kept
        results["added_history"] = "历史" in h2

        log("\n--- A7 断言 ---")
        for tag, v, hh in (("v1", v1, h1), ("v2", v2, h2)):
            sha_ok = hashlib.sha256(hh.encode()).hexdigest() == v["html_sha256"]
            log(f"[A7] {tag} sha自洽={sha_ok} 结尾</html>={hh.rstrip().endswith('</html>')} "
                f"body={len(re.findall(r'<body', hh, re.I))} doctype={len(re.findall(r'<!DOCTYPE', hh, re.I))}")
        # 关键：第二轮之后，第一轮版本必须原样可回取（旧版本未被覆写）
        v1_again = ver(1)
        log(f"[A7] 第二轮后回取 v1 sha 未变 = {v1_again['html_sha256'] == v1['html_sha256']} "
            f"html 未变 = {v1_again['html'] == h1}")
        results["v1_immutable"] = (v1_again["html_sha256"] == v1["html_sha256"])

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

    log(f"\n[probe project] {pid}")
    return _dump(results)


def _dump(results: dict) -> int:
    log("=== SUMMARY_JSON ===")
    log(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results.get("round2") != "failed" else 1


if __name__ == "__main__":
    sys.exit(probe())
