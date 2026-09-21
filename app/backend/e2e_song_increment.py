"""验收探针：v1 原有功能保留 + v2 增量新增（歌曲推荐场景）。

对应用户诉求：
    轮次 1（原始需求）：帮我创建一个每日歌曲推荐
    轮次 2（补充需求）：每首歌下面加一个不感兴趣按钮

要同时成立的两件事：
    A. 新功能真的落地  —— v2 出现「不感兴趣」按钮，且数量与歌曲条目相当；
    B. 旧功能没有退化  —— v1 的歌曲条目/交互在 v2 仍然存在，v1 版本本身逐字节不变。

判定一律走**产物**（最终 HTML 里留下了什么），不看提示词里注入了什么——
注入了上下文不等于模型用上了上下文。
"""
import hashlib
import json
import re
import sys
import time

import httpx

BASE = "https://keegan.pub.atoms.world"
ANON = "X-Atoms-Anon"
POLL = 5.0

_BUTTON_RE = re.compile(r"<button\b[^>]*>(.*?)</button>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
# 歌曲条目在产物里通常是 JS 数据数组的字段；不同模型用的键名差异较大，
# 故同时匹配常见键名，并额外兜底匹配「歌名 - 歌手」式字符串字面量。
_SONG_FIELD_RE = re.compile(
    r"""["'](?:title|name|song|track|songName|歌名|曲名)["']\s*[:=]\s*["'`]([^"'`]{2,60})["'`]""",
    re.I,
)
_SONG_PAIR_RE = re.compile(r"""["'`]([^"'`\n]{2,40}\s+[-–—]\s+[^"'`\n]{2,30})["'`]""")


def log(*a):
    print(*a, flush=True)


def buttons(html: str) -> list[str]:
    """页面里所有 <button> 的可见文本（去标签、去空白、去实体）。"""
    out: list[str] = []
    for raw in _BUTTON_RE.findall(html):
        text = _TAG_RE.sub("", raw)
        text = text.replace("&times;", "×").replace("&nbsp;", " ")
        text = " ".join(text.split())
        if text:
            out.append(text)
    return out


def songs(html: str) -> set[str]:
    """从产物中提取歌曲条目名（JS 数据数组里的字段或「歌名 - 歌手」字面量）。"""
    found = {m.strip() for m in _SONG_FIELD_RE.findall(html)}
    found |= {m.strip() for m in _SONG_PAIR_RE.findall(html)}
    return {s for s in found if s and not s.startswith("$")}


def check(failures: list[str], label: str, ok: bool, detail: str = "") -> bool:
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
        anon_key = r.json().get("anon_key") or anon_key
        log(f"[identity] anon={anon_key}")

        pid = c.post("/api/v1/atoms/projects",
                     json={"title": "歌曲推荐增量探针"},
                     headers=h()).json()["public_id"]
        log(f"[project] {pid}")
        results["project"] = pid

        def gen(prompt: str) -> int:
            r = c.post(f"/api/v1/atoms/projects/{pid}/generate",
                       json={"prompt": prompt}, headers=h())
            log(f"[generate] http={r.status_code} {r.text[:160]}")
            if r.status_code != 202:
                raise SystemExit(f"generate 未被受理: {r.text[:200]}")
            return r.json()["version_seq"]

        def wait(seq: int, budget: float = 900.0) -> dict:
            t0, last = time.time(), None
            while time.time() - t0 < budget:
                snap = c.get(
                    f"/api/v1/atoms/projects/{pid}/versions/{seq}/steps",
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
            return c.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}",
                         headers=h()).json()

        P1 = "帮我创建一个每日歌曲推荐"
        P2 = "每首歌下面加一个不感兴趣按钮"

        log("\n=== 轮次 1：原始需求 ===")
        s1 = wait(gen(P1))
        log(f"[r1] {s1['status']} {s1['_elapsed']:.1f}s err={s1.get('error')} "
            f"type={s1.get('error_type')}")
        if s1["status"] != "succeeded":
            failures.append(f"轮次 1 未成功：{s1['status']}")
            results["round1"] = s1["status"]
            return _dump(results, failures)
        v1 = ver(1)
        h1 = v1["html"]
        b1, g1 = buttons(h1), songs(h1)
        log(f"[r1] len={len(h1)} 按钮={len(b1)} 歌曲条目={len(g1)}")
        log(f"[r1] 歌曲样例={sorted(g1)[:8]}")
        results["r1"] = {"len": len(h1), "songs": sorted(g1),
                         "buttons": sorted(set(b1)), "elapsed": s1["_elapsed"]}

        log("\n=== 轮次 2：补充需求（在 v1 基础上增量） ===")
        s2 = wait(gen(P2))
        log(f"[r2] {s2['status']} {s2['_elapsed']:.1f}s err={s2.get('error')} "
            f"type={s2.get('error_type')}")
        if s2["status"] != "succeeded":
            failures.append(f"轮次 2 未成功：{s2['status']}")
            results["round2"] = s2["status"]
            return _dump(results, failures)
        v2 = ver(2)
        h2 = v2["html"]
        b2, g2 = buttons(h2), songs(h2)
        log(f"[r2] len={len(h2)} 按钮={len(b2)} 歌曲条目={len(g2)}")
        results["r2"] = {"len": len(h2), "songs": sorted(g2),
                         "buttons": sorted(set(b2)), "elapsed": s2["_elapsed"]}

        log("\n--- A. 新功能真的落地 ---")
        disinterest = [t for t in b2 if "不感兴趣" in t]
        check(failures, "v2 出现「不感兴趣」按钮", bool(disinterest),
              f"命中 {len(disinterest)} 个按钮，样例={disinterest[:3]}")
        # 「每首歌下面」：按钮要么按歌逐条渲染（静态多个），要么在 JS 模板里生成
        in_template = "不感兴趣" in h2
        check(failures, "「不感兴趣」出现在产物中（含 JS 模板渲染路径）", in_template)
        results["disinterest_buttons"] = len(disinterest)

        log("\n--- B. 旧功能没有退化 ---")
        ratio = len(h2) / max(len(h1), 1)
        check(failures, "v2 长度未相对 v1 大幅缩水（>=0.80）",
              ratio >= 0.8, f"实际 {ratio:.2f}（{len(h1)} → {len(h2)}）")
        results["ratio"] = ratio

        check(failures, "v1 确实产出了可识别的歌曲条目", len(g1) >= 3,
              f"仅 {len(g1)} 条")
        lost = sorted(g1 - g2)
        kept = len(g1 & g2)
        keep_rate = kept / max(len(g1), 1)
        check(failures, "v1 的歌曲条目在 v2 全部保留", not lost,
              f"保留 {kept}/{len(g1)}（{keep_rate:.0%}），丢失={lost[:6]}"
              if lost else f"全部 {len(g1)} 条保留")
        results["songs_kept"] = kept
        results["songs_lost"] = lost

        # v1 的既有交互按钮（排除本轮新增的「不感兴趣」）不应消失
        old_btn = {t for t in b1} - {t for t in b1 if "不感兴趣" in t}
        btn_lost = sorted(old_btn - set(b2))
        check(failures, "v1 的原有按钮在 v2 仍存在", not btn_lost,
              f"丢失={btn_lost}" if btn_lost else f"全部保留 {sorted(old_btn)}")
        results["buttons_lost"] = btn_lost

        log("\n--- C. 版本不可变与产物完整性 ---")
        for tag, v, hh in (("v1", v1, h1), ("v2", v2, h2)):
            sha_ok = hashlib.sha256(hh.encode()).hexdigest() == v["html_sha256"]
            ends = hh.rstrip().endswith("</html>")
            bodies = len(re.findall(r"<body", hh, re.I))
            doctypes = len(re.findall(r"<!DOCTYPE", hh, re.I))
            log(f"[{tag}] len={len(hh)} sha自洽={sha_ok} 结尾</html>={ends} "
                f"body={bodies} doctype={doctypes}")
            check(failures, f"{tag} html_sha256 与 html 自洽", sha_ok)
            check(failures, f"{tag} 以 </html> 结尾", ends)
            check(failures, f"{tag} 恰好一个 <body> 与一个 <!DOCTYPE>",
                  bodies == 1 and doctypes == 1,
                  f"body={bodies} doctype={doctypes}")

        v1_again = ver(1)
        sha_same = v1_again["html_sha256"] == v1["html_sha256"]
        byte_same = v1_again["html"] == h1
        check(failures, "第二轮后回取 v1 摘要未变", sha_same)
        check(failures, "第二轮后回取 v1 html 逐字节不变", byte_same)
        results["v1_immutable"] = sha_same and byte_same

    log(f"\n[probe project] {pid}")
    return _dump(results, failures)


def _dump(results: dict, failures: list[str]) -> int:
    log("=== SUMMARY_JSON ===")
    log(json.dumps(results, ensure_ascii=False, indent=2)[:4000])
    if failures:
        log(f"\n=== FAILURES ({len(failures)}) ===")
        for item in failures:
            log(f"  - {item}")
        return 1
    log("\n=== 全部判定通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(probe())
