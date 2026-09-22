"""源码 / 预览一致性：三方对照（FR-033 / SC-007 / research.md R-10）。

R-10 点名的问题是**自证**：用同一个公式复算同一个响应体，只能发现「响应体被
改动」，发现不了「前端展示的其实不是这份数据」或「两个视图取自不同来源」。
本文件的对照关系是：

① 后端 ``GET /versions/{seq}`` 返回的 ``html`` 与 ``html_sha256``；
② 验证方**独立**计算的摘要——既不复用被测函数 ``_html_sha256``，也不复用它的
   写法；另有一条**已知答案**（KAT）把字节层钉死；
③ 前端展示条与代码视图所依据的**同一数据源**——用功能性断言证明全平台只有这
   一个 ``html`` 来源，再用源码级守卫证明前端不自行重算摘要。

三者相等因此不是「碰巧算得一样」，而是「只有一个来源，且该来源自洽」。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from core.database import db_manager
from fakes import make_html
from models.versions import Versions

# ---------------------------------------------------------------- ② 已知答案（KAT）
#
# 下面这个页面与它的摘要都在本仓库之外算好，测试只做等值断言。KAT 的意义在于钉死
# 「html 字段与摘要字段对应的字节完全一致」——含换行与编码。若测试两边都用同一个
# 公式现算，换行或编码漂移会一起漂移而对不上号（R-10 说的自证）。
# 页面刻意使用纯 ASCII：让字节层断言不受任何终端/文件编码往返影响。
KNOWN_HTML = (
    "<!DOCTYPE html>\n"
    "<html><head><title>atoms-consistency</title></head>"
    "<body><h1>atoms</h1></body></html>\n"
)
KNOWN_HTML_SHA256 = "fd200c46647cef6f93aa3eeff0caab45796da9daf3b176fc4dd0131adb8abdfa"


def _independent_sha256(text: str) -> str:
    """验证方自算摘要，**不**调用 ``routers.atoms._html_sha256``。

    是一条另写的实现（``hashlib.new`` + 非对齐分块 ``update``），并且算的是
    **响应体里实际返回的那个字符串**——因此它能发现「摘要字段对应的不是这份
    html」，而不是复述产出方的结论。分块步长取 7 这类非对齐值：对齐分块会在
    边界处恰好掩盖拼接顺序类的问题。
    """
    digest = hashlib.new("sha256")
    raw = text.encode("utf-8")
    step = 7
    for i in range(0, len(raw), step):
        digest.update(raw[i : i + step])
    return digest.hexdigest()


# ---------------------------------------------------------------- 夹具与工具


async def _create_project(client) -> str:
    response = await client.post("/api/v1/atoms/projects", json={"title": "一致性"})
    return response.json()["public_id"]


async def _get_version(client, pid: str, seq: int) -> dict:
    response = await client.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}")
    assert response.status_code == 200, response.text
    return response.json()


async def _seed_versions(pid: str, *, html: str, marker: str) -> None:
    """直接落一个成功版本，避免依赖生成路径（生成路径另有用例覆盖）。"""
    async with db_manager.session() as session:
        session.add(
            Versions(
                project_public_id=pid,
                seq=1,
                prompt=f"seed {marker}",
                html=html,
                status="succeeded",
                duration_ms=10,
            )
        )
        await session.commit()


def _mentions(node, marker: str) -> bool:
    """递归判断响应体里是否有任何字段（含键名）携带该标记。"""
    if isinstance(node, str):
        return marker in node
    if isinstance(node, dict):
        return any(_mentions(k, marker) or _mentions(v, marker) for k, v in node.items())
    if isinstance(node, list):
        return any(_mentions(item, marker) for item in node)
    return False


# ---------------------------------------------------------------- ①② 摘要与内容自洽


async def test_known_answer_pins_html_bytes_and_digest(client):
    """① vs ②(KAT)：返回的 html 与摘要字段必须对应同一段**确定字节**。"""
    pid = await _create_project(client)
    await _seed_versions(pid, html=KNOWN_HTML, marker="kat")

    detail = await _get_version(client, pid, 1)

    assert detail["html"] == KNOWN_HTML
    assert detail["html_sha256"] == KNOWN_HTML_SHA256
    # KAT 与独立实现也必须互相印证，否则说明常量本身算错了
    assert _independent_sha256(KNOWN_HTML) == KNOWN_HTML_SHA256


async def test_digest_matches_independent_computation_on_generated_page(
    client, inject_fake_ai
):
    """走**真实生成路径**产出含非 ASCII 的页面，再独立复算摘要。

    非 ASCII 是有意选的：它能暴露「摘要按 latin-1/UTF-16 而不是 UTF-8 编码」
    这类只在中文页面上发作的问题，而 ASCII KAT 覆盖不到。
    """
    pid = await _create_project(client)
    inject_fake_ai(_fake_hub(make_html("计算器v1")))

    accepted = await client.post(
        f"/api/v1/atoms/projects/{pid}/generate", json={"prompt": "做一个计算器"}
    )
    assert accepted.status_code == 202, accepted.text
    seq = accepted.json()["version_seq"]

    detail = await _get_version(client, pid, seq)

    assert detail["status"] == "succeeded"
    assert detail["html"], "成功版本必须返回非空 html"
    # 中文确实进了产物，否则这条用例退化成了 ASCII 场景
    assert "计算器v1" in detail["html"]
    assert detail["html_sha256"] == _independent_sha256(detail["html"])
    assert len(detail["html_sha256"]) == 64


# ---------------------------------------------------------------- ③ 单一数据源


def _project_scoped_get_paths(pid: str) -> list[str]:
    """枚举 atoms 路由中所有可针对本项目构造的 GET 路径。

    这里是**遍历路由表**而不是手写端点清单：手写清单在新增接口时会静默漏检，
    而「某个新接口顺手带上 html」正是 CLAUDE.md 里「仅此接口返回 html」要防的
    退化——遍历之后，新接口一泄漏就会立刻失败。
    """
    from routers import atoms as atoms_router

    paths = []
    for route in atoms_router.router.routes:
        if "GET" not in getattr(route, "methods", set()):
            continue
        path = route.path.replace("{public_id}", pid).replace("{seq}", "1")
        if "{" in path:
            continue  # 还有其它路径参数，无法针对本项目构造
        paths.append(path)
    return paths


async def test_only_version_detail_exposes_html(client):
    """③ 功能性一半：全平台只有 ``GET /versions/{seq}`` 能拿到 html。

    两个视图不可能「取自不同来源」——因为不存在第二个来源。这条断言把 R-10 的
    「同一数据源」从口头约定变成可执行的契约。
    """
    pid = await _create_project(client)
    marker = "MARKER-SINGLE-SOURCE-7f3a"
    await _seed_versions(pid, html=make_html(marker), marker="single")

    detail_path = f"/api/v1/atoms/projects/{pid}/versions/1"
    paths = _project_scoped_get_paths(pid)
    assert detail_path in paths, "前置条件：路由表里应能找到版本详情接口"
    # 不能空转：若枚举出的路径只剩详情接口本身，下面的循环就什么都没检查
    assert len(paths) >= 3, f"枚举到的 GET 路径过少，断言会退化成空转：{paths}"

    for path in paths:
        response = await client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        if path == detail_path:
            assert marker in response.json()["html"], "前置条件：详情接口确实返回了这段 html"
        else:
            assert not _mentions(
                response.json(), marker
            ), f"{path} 也暴露了 html 内容——出现了第二个数据源"


def _frontend_src() -> Path:
    # tests/ -> backend/ -> app/
    return Path(__file__).resolve().parents[2] / "frontend" / "src"


def test_frontend_views_read_html_and_digest_from_one_response():
    """③ 源码级一半：前端不自行重算摘要，两个视图读同一份状态。

    功能性断言只能证明后端只有一个来源；「前端展示条与代码视图都取自该来源」
    需要在源码上确认。这里守的是**会真正导致分叉**的那件事：前端自己算摘要。
    只要它算，展示条和代码视图就可能各显示一份，而后端测试永远看不见。
    """
    index_src = (_frontend_src() / "pages" / "Index.tsx").read_text(encoding="utf-8")
    code_viewer_src = (_frontend_src() / "components" / "CodeViewer.tsx").read_text(
        encoding="utf-8"
    )
    all_src = "\n".join(
        p.read_text(encoding="utf-8") for p in _frontend_src().rglob("*.ts*")
    )

    # (a) 前端不得有任何本地摘要计算——这是唯一能让两个视图分叉的写法
    assert "crypto.subtle" not in all_src, "前端出现了本地摘要计算（crypto.subtle）"
    assert "createHash" not in all_src, "前端出现了本地摘要计算（createHash）"
    assert not re.search(r"\bsha256\s*\(", all_src), "前端出现了本地 sha256 计算调用"

    # (b) 展示条所用的摘要只能来自接口字段，不能来自任何本地表达式
    assignments = re.findall(r"setHtmlSha256\(([^)]*)\)", index_src)
    assert assignments, "Index.tsx 里没有 setHtmlSha256 赋值，前置条件不成立"
    for expr in assignments:
        if expr.strip().strip("'\"") == "":
            continue  # 切换项目/出错时的清空，不是数据来源
        assert "html_sha256" in expr, f"摘要不是取自接口字段：setHtmlSha256({expr})"

    # (c) 展示条与代码视图消费的是同一份状态
    assert "SHA {htmlSha256" in index_src, "展示条没有渲染接口下发的摘要"
    code_viewer_usage = re.search(r"<CodeViewer\b[^>]*/>", index_src, re.S)
    assert code_viewer_usage, "没有找到 CodeViewer 的使用处"
    usage = code_viewer_usage.group(0)
    assert "html={html}" in usage, "代码视图拿到的不是展示用的 html"
    assert "sha256={htmlSha256}" in usage, "代码视图拿到的不是接口下发的摘要"
    assert "sha256" in code_viewer_src, "CodeViewer 没有接收摘要 prop"


# ---------------------------------------------------------------- 多版本切换（v3 → v1）


async def test_switching_back_to_v1_returns_v1_content_and_digest(
    client, inject_fake_ai
):
    """同一需求迭代出 v1/v2/v3 后，切回任一历史版本必须拿到**那一版**的内容。

    这是「从 v3 切到 v1，源码和预览都展示 v1」的后端保证。前端的预览与源码共用
    同一份 `html` 状态（见 test_frontend_views_read_html_and_digest_from_one_source
    的源码级断言），因此只要版本详情接口按 seq 返回各自的内容与摘要，两个视图就
    不可能分叉——切换正确性完全落在这个接口上。

    断言不止「v1 内容对」，还要求三版**互不相同**：若三版内容恰好一样，上面那条
    断言会在接口把 seq 完全忽略、永远返回最新版的情况下依然通过，退化成空转。
    """
    pid = await _create_project(client)

    seqs = []
    for round_no, marker in enumerate(("第一版内容", "第二版内容", "第三版内容"), 1):
        inject_fake_ai(_fake_hub(make_html(marker)))
        accepted = await client.post(
            f"/api/v1/atoms/projects/{pid}/generate",
            json={"prompt": f"第 {round_no} 轮需求"},
        )
        assert accepted.status_code == 202, accepted.text
        seqs.append(accepted.json()["version_seq"])

    assert seqs == [1, 2, 3], f"迭代应产出连续递增的版本号，实际 {seqs}"

    details = {seq: await _get_version(client, pid, seq) for seq in seqs}

    # 三版内容与摘要两两不同，否则下面的「切回 v1」断言不具鉴别力
    htmls = {seq: d["html"] for seq, d in details.items()}
    shas = {seq: d["html_sha256"] for seq, d in details.items()}
    assert len(set(htmls.values())) == 3, "三个版本内容相同，切换断言会退化成空转"
    assert len(set(shas.values())) == 3, "三个版本摘要相同，切换断言会退化成空转"

    # 切回 v1：拿到的必须是 v1 的正文，且不含后续版本的任何标记
    v1 = details[1]
    assert "第一版内容" in v1["html"]
    assert "第二版内容" not in v1["html"]
    assert "第三版内容" not in v1["html"]
    assert v1["seq"] == 1
    # 摘要与正文自洽（独立复算），保证展示条标识的就是这份 v1 正文
    assert v1["html_sha256"] == _independent_sha256(v1["html"])

    # 反向再确认一次：v3 仍是 v3，切换没有污染其它版本
    assert "第三版内容" in details[3]["html"]
    assert details[3]["html_sha256"] == _independent_sha256(details[3]["html"])


async def test_iterating_does_not_mutate_earlier_versions(client, inject_fake_ai):
    """新版本产出后，历史版本的 html 与摘要必须逐字节不变（版本不可变）。

    切换能用的前提是历史版本**还在且没被改写**。若迭代时就地覆盖了旧行，
    「切到 v1」拿到的会是被改写过的 v1，界面上看不出任何异常。
    """
    pid = await _create_project(client)

    inject_fake_ai(_fake_hub(make_html("原始版本")))
    first = await client.post(
        f"/api/v1/atoms/projects/{pid}/generate", json={"prompt": "初版"}
    )
    assert first.status_code == 202, first.text
    before = await _get_version(client, pid, first.json()["version_seq"])

    inject_fake_ai(_fake_hub(make_html("迭代版本")))
    second = await client.post(
        f"/api/v1/atoms/projects/{pid}/generate", json={"prompt": "加个按钮"}
    )
    assert second.status_code == 202, second.text
    assert second.json()["version_seq"] != first.json()["version_seq"]

    after = await _get_version(client, pid, first.json()["version_seq"])
    assert after["html"] == before["html"], "迭代后 v1 的 html 被改写了"
    assert after["html_sha256"] == before["html_sha256"], "迭代后 v1 的摘要变了"


# ---------------------------------------------------------------- 局部工具


def _fake_hub(html: str):
    """真实生成路径需要一个 FakeAIHub；延迟导入避免与 fakes 的模块级设置抢跑。"""
    from fakes import ANALYSIS_JSON, DESIGN_JSON, FakeAIHub

    return FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, html])
