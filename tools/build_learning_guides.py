"""Publish authored Chinese learning guides with verified live source references.

Edit tools/learning_guides/<project>.md, then run this script.
--check verifies project coverage, source excerpts, links and generated content.
No chapter package is imported or executed.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).with_name("learning_guides.json")
TEMPLATES = Path(__file__).with_name("learning_guides")
TOKEN = re.compile(r"{{(symbol|file|excerpt):([^{}]+)}}")
START, END = "<!-- LEARNING_GUIDE_START -->", "<!-- LEARNING_GUIDE_END -->"


class SourceReferences:
    """Bind authored reading stops to actual definitions, including test methods."""

    def __init__(self, project):
        self.project = project.resolve()
        self.cache = {}

    def source(self, relative):
        path = (self.project / relative).resolve()
        if not path.is_relative_to(self.project) or not path.is_file():
            raise ValueError(f"Invalid source path: {relative}")
        if path not in self.cache:
            text = path.read_text(encoding="utf-8-sig")
            symbols = {}

            def visit(nodes, prefix=""):
                for node in nodes:
                    if isinstance(
                        node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        name = prefix + node.name
                        symbols[name] = node
                        visit(node.body, name + ".")
                    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                        targets = (
                            node.targets
                            if isinstance(node, ast.Assign)
                            else [node.target]
                        )
                        for target in targets:
                            if isinstance(target, ast.Name):
                                symbols[prefix + target.id] = node

            if path.suffix == ".py":
                visit(ast.parse(text).body)
            self.cache[path] = (text.splitlines(), symbols)
        return self.cache[path]

    def resolve(self, kind, value):
        parts = value.split(":")
        relative = parts[0]
        lines, symbols = self.source(relative)
        target = "../" + Path(relative).as_posix()
        if kind == "file":
            if len(parts) != 1:
                raise ValueError(f"Malformed file reference: {value}")
            return f"[{Path(relative).name}]({target})"
        expected = 2 if kind == "symbol" else 4
        if len(parts) != expected or parts[1] not in symbols:
            raise ValueError(f"Unknown or malformed {kind} reference: {value}")
        node = symbols[parts[1]]
        if kind == "symbol":
            return f"[{parts[1]}]({target}#L{node.lineno})"
        offset, count = int(parts[2]), int(parts[3])
        start = node.lineno - 1 + offset
        stop = start + count
        if offset < 0 or not 1 <= count <= 18 or stop > node.end_lineno:
            raise ValueError(
                f"Excerpt must stay inside its symbol and contain 1..18 lines: {value}"
            )
        code = textwrap.dedent("\n".join(lines[start:stop]))
        return (
            f"[{Path(relative).name}:{start + 1}]({target}#L{start + 1})\n\n"
            f"\u0060\u0060\u0060python\n{code}\n\u0060\u0060\u0060"
        )


def render_guide(item):
    template = TEMPLATES / (item["project"] + ".md")
    content = template.read_text(encoding="utf-8")
    tokens = list(TOKEN.finditer(content))
    if sum(m[1] == "symbol" for m in tokens) < 6:
        raise ValueError(f"{template.name}: needs six source reading references")
    if not any(m[1] == "excerpt" for m in tokens):
        raise ValueError(f"{template.name}: needs an explained source excerpt")
    if sum(m[1] == "symbol" and m[2].startswith("tests/") for m in tokens) < 2:
        raise ValueError(f"{template.name}: needs two explicit test references")
    diagrams = re.findall(
        r"\u0060\u0060\u0060mermaid\s*\n([\s\S]*?)\u0060\u0060\u0060", content
    )
    if not 2 <= len(diagrams) <= 4:
        raise ValueError(f"{template.name}: choose two to four focused diagrams")
    refs = SourceReferences(ROOT / item["project"])
    try:
        content = TOKEN.sub(lambda m: refs.resolve(m[1], m[2]), content)
    except (KeyError, ValueError, SyntaxError) as error:
        raise ValueError(f"{template.name}: {error}") from error
    if re.search(r"{{(?:symbol|file|excerpt):", content):
        raise ValueError(f"{template.name}: malformed unresolved reference")
    content += (
        "\n\n---\n\n"
        "[返回全仓库学习导航](../../docs/learning-guide_cn.md)"
        " · [项目运行说明](../README.md)\n\n"
        f"[导读原稿](../../tools/learning_guides/{item['project']}.md)。"
        "正文与图按当前实现编写；源码链接和摘录由生成器绑定到当前定义。"
        "修改行为后应同时复核案例、时序和解释，再重新生成；"
        "定义与链接校验不等同于业务回归测试。\n"
    )
    return content.rstrip() + "\n"


def readme_content(text, english=False):
    body = (
        "## Code reading guide\n\n"
        "[Learn through a concrete example: architecture, state changes and annotated source (Chinese)]"
        "(docs/learning-guide_cn.md)."
        if english
        else "## 从这里开始读代码\n\n"
        "[跟着一个案例读懂系统：架构、状态变化与关键代码详解](docs/learning-guide_cn.md)。"
    )
    block = f"{START}\n{body}\n{END}"
    if START in text:
        return re.sub(
            re.escape(START) + r".*?" + re.escape(END),
            lambda _: block,
            text,
            flags=re.S,
        )
    split = text.find("\n")
    return text[: split + 1] + "\n" + block + "\n" + text[split + 1 :]


def index(items):
    out = [
        "# 从一个案例读懂系统：27个子项目学习导航\n",
        "每份导读用一个具体案例串起四件事：系统要保存什么事实、各组件如何协作、"
        "一次操作在哪些函数中改变状态、失败后依靠什么恢复。"
        "先建立整体认识，再按编号跳进代码，最后用已存在的测试核对预期。\n",
        "## 对照GitHub上的实现\n",
        "[27个子项目的GitHub编码实现对照](github-implementations_cn.md)"
        "按章节列出仓库、主要语言、对应范围和学习重点，区分业务系统、算法库、核心组件及教学案例。\n",
        "## 选一条适合你的路线\n",
        "| 你想先掌握什么 | 推荐路线 | 第一轮的目标 |",
        "|---|---|---|",
        "| 从函数读到完整小系统 | 唯一ID → 短链接 → 限流器 | 能解释输入、核心算法、共享状态和返回结果 |",
        "| 缓存与异步业务 | 新闻动态 → 通知 → 聊天 → 消息队列 | 能区分受理、持久提交、后台完成与用户可见 |",
        "| 存储与恢复 | 一致性哈希 → 键值存储 → 文件同步 → 对象存储 | 能解释数据归属、版本、复制与重启恢复 |",
        "| 状态一致性与资金业务 | 酒店预订 → 支付 → 数字钱包 → 证券交易 | 能定位事务、幂等身份、状态推进和补偿 |",
        "| 数据处理与派生视图 | 自动补全 → 广告聚合 → 指标告警 → 排行榜 | 能区分原始事实、处理进度、快照和查询结果 |",
        "\n容量估算适合穿插学习：用它给设计列出假设、单位和资源预算，再回到具体实现判断瓶颈。\n",
        "## 一份导读怎样读\n",
        "1. **先读案例和整体图。** 暂时不点源码，先预测这次输入会产生哪些状态和输出。\n"
        "2. **沿数据归属表找状态。** 明确哪份记录是事实，哪份是缓存／投影，哪份只存在于进程内。\n"
        "3. **按编号打开源码。** 每站都有要回答的问题与输入／输出；先读指定函数和解释过的摘录，再决定是否展开辅助函数。\n"
        "4. **对照失败分支。** 找出成功的提交点，判断重试会不会重复改变状态、崩溃后能恢复什么。\n"
        "5. **核对测试和练习。** 每页提供明确测试入口、观察项和带答案自查。运行命令与依赖按对应 README，"
        "每个项目独立执行。\n",
        "读完一个项目的标志：你能拿案例的数据，从入口讲到结果；"
        "能指着具体函数说明一次关键状态提交；能预测一个重复、失败或并发场景的结果。\n",
        "## 按章节查找\n",
        "| 章节 | 导读 | 主要学习问题 |",
        "|---|---|---|",
    ]
    for item in sorted(items, key=lambda i: i["chapter"]):
        out.append(
            f"| {item['chapter']:02} | [{item['title']}](../{item['project']}/docs/learning-guide_cn.md)"
            f" | {item['purpose']} |"
        )
    out += [
        "\n## 图和代码如何保持一致\n",
        "导读原稿位于 \u0060tools/learning_guides/<project>.md\u0060，"
        "解释与图由阅读实际实现后编写，源码定义和摘录由工具校验。"
        "调用图只保留案例所需路径；后台任务、回调与替代后端会在文中说明。\n",
        "\u0060\u0060\u0060powershell\n"
        "python tools/build_learning_guides.py\n"
        "python tools/build_learning_guides.py --check\n"
        "\u0060\u0060\u0060\n",
        "\u0060--check\u0060 检查27个项目覆盖、真实源码／测试定义、摘录范围、本地链接和生成内容同步。"
        "它不能证明案例解释、动态调用或生产容量正确。"
        "继续深入代码时可用 CodeGraph 追踪调用，用 code-review-graph 查看影响范围；先确认索引与工作区代码同步。\n",
    ]
    return "\n".join(out).rstrip() + "\n"


def outputs_for(items):
    expected = {
        p.name
        for p in ROOT.iterdir()
        if (p / "src").is_dir() and (p / "tests").is_dir()
    }
    actual = {i["project"] for i in items}
    templates = {p.stem for p in TEMPLATES.glob("*.md") if p.name != "README.md"}
    if actual != expected or len(actual) != len(items) or templates != actual:
        raise ValueError(
            f"Project coverage mismatch: missing={expected - actual}, extra={actual - expected}, "
            f"missing templates={actual - templates}, extra templates={templates - actual}"
        )
    outputs = {
        ROOT / item["project"] / "docs/learning-guide_cn.md": render_guide(item)
        for item in items
    }
    outputs[ROOT / "docs/learning-guide_cn.md"] = index(items)
    for project in [ROOT] + [ROOT / item["project"] for item in items]:
        for name in ("README.md", "README_CN.md"):
            path = project / name
            if path.is_file():
                outputs[path] = readme_content(
                    path.read_text(encoding="utf-8"), name == "README.md"
                )
    return outputs


def verify_links(outputs):
    errors = []
    for path, content in outputs.items():
        # README text outside our managed navigation block belongs to other
        # documentation work. Validate the links this publisher owns.
        if path.name in ("README.md", "README_CN.md"):
            block = re.search(
                re.escape(START) + r".*?" + re.escape(END), content, flags=re.S
            )
            if block is None:
                errors.append(f"{path.relative_to(ROOT)}: missing guide navigation")
                continue
            content = block.group(0)
        for destination in re.findall(r"\]\(([^)]+)\)", content):
            if "://" in destination or destination.startswith("#"):
                continue
            file, _, anchor = destination.partition("#")
            target = (path.parent / file).resolve()
            if not target.exists() and target not in outputs:
                errors.append(f"{path.relative_to(ROOT)}: missing {destination}")
            elif anchor.startswith("L") and anchor[1:].isdigit() and target.is_file():
                lines = target.read_text(encoding="utf-8-sig").splitlines()
                if not 1 <= int(anchor[1:]) <= len(lines):
                    errors.append(
                        f"{path.relative_to(ROOT)}: invalid line {destination}"
                    )
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    items = json.loads(DATA.read_text(encoding="utf-8"))
    outputs = outputs_for(items)
    errors = verify_links(outputs)
    if errors:
        raise SystemExit("\n".join(errors))
    for path, content in outputs.items():
        content = content.rstrip() + "\n"
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if args.check and current != content:
            errors.append(f"{path.relative_to(ROOT)}: out of date")
        elif not args.check and current != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    if errors:
        raise SystemExit("\n".join(errors))
    print(
        f"{'Checked' if args.check else 'Published'} {len(items)} authored guides, index and README entries."
    )


if __name__ == "__main__":
    main()
