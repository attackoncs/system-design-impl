"""Build the combined reading edition from the verified chapter translations."""

from __future__ import annotations

import argparse
from pathlib import Path
import re

from verify import FILES, READING_FILES

HERE = Path(__file__).resolve().parent
COMBINED = HERE / "后续章节中文版.md"

# Only the reading edition repairs extraction damage. The per-chapter files
# retain line-by-line correspondence with the English manuscript.
REPAIRS_22 = {
    (228, 237): """> 译校说明：原稿表12的列与换行发生错位，以下按可辨认内容重排。

| 对比项 | 仅存储原始数据 | 仅存储聚合数据 |
|---|---|---|
| 优点 | 完整数据集；支持数据筛选与重新计算 | 数据集较小；查询速度快 |
| 缺点 | 数据存储量巨大；查询速度慢 | 信息损失：这是派生数据，例如10条记录可能聚合为1条记录 |""",
    (
        440,
        447,
    ): """> 译校说明：原稿表17缺少部分单元格，且性能指标断行。以下仅重排已有文字；“原稿缺项”表示底本未提供，未据常识补写。

| 比较项 | 服务（在线系统） | 批处理系统（离线系统） | 流处理系统（近实时系统） |
|---|---|---|---|
| 响应性 | 快速响应客户端 | 不需要向客户端返回响应 | 原稿缺项 |
| 输入 | 用户请求 | 有限大小的有界输入，数据量较大 | 原稿缺项 |
| 输出 | 返回给客户端的响应 | 物化视图、聚合指标等 | 原稿缺项 |
| 性能衡量指标 | 可用性、延迟 | 吞吐量 | 吞吐量、延迟 |
| 示例 | 在线购物 | MapReduce | 原稿缺项 |""",
    (
        495,
        498,
    ): """> 译校说明：原稿表18的“优点／缺点”表头与实际内容不对应，也未给出缺点项。以下按可见内容整理，不补写缺失信息。

| 时间基准 | 原稿列出的特点 |
|---|---|
| 事件时间 | 聚合结果更准确，因为客户端确切知道广告被点击的时刻 |
| 处理时间 | 服务器时间戳更可靠 |""",
}
REPAIRS_QPS = {
    17: (
        59,
        65,
        """> 译校说明：原稿的QPS单列表格发生断行，以下将原有条目放回表格，公式保持原样。

| 计算 QPS |
|---|
| 每天的秒数为24 \\* 60 \\* 60 = 86,400，计算时向上近似为10^5。**本书始终以10^5表示每天秒数。** |
| 假设每位用户每天执行5次搜索。 |
| Search QPS = 100 million \\* 5 / 10^5 = 5,000 |""",
    ),
    18: (
        72,
        80,
        """> 译校说明：原稿的QPS单列表格发生断行，以下仅重排已有估算。

| 计算 QPS |
|---|
| 日活跃用户1亿。 |
| 并发用户数：10% \\* 1亿 = 1000万。 |
| 用户每30秒报告一次位置。 |
| 位置更新 QPS = 1000万 / 30 ≈ 334,000。 |""",
    ),
}


def reading_text(number, text):
    if number in REPAIRS_QPS:
        start, end, replacement = REPAIRS_QPS[number]
        lines = text.splitlines(keepends=True)
        lines[start - 1 : end] = [replacement + "\n"]
        return "".join(lines)
    if number == 22:
        lines = text.splitlines(keepends=True)
        for (start, end), replacement in sorted(REPAIRS_22.items(), reverse=True):
            lines[start - 1 : end] = [replacement + "\n"]
        text = "".join(lines)
    return text


def demote_headings(text, levels=1):
    result = []
    fenced = False
    for line in text.splitlines(keepends=True):
        if re.match(r"^\s*(`{3,}|~{3,})", line):
            fenced = not fenced
        if not fenced and re.match(r"^#{1,6} ", line):
            line = "#" * levels + line
        result.append(line)
    return "".join(result)


def build(repair_tables=True, include_payment=False):
    selected = FILES if include_payment else READING_FILES
    parts = [
        """# 《System Design Interview》后续章节中文译文

根据纸书目录核对，本阅读版从本地英文稿第17章起补齐纸书缺失的章节：17—20、22—26、28—29，另附第30章学习资料。保留英文底本编号，按章号顺序阅读。

纸书已有第21章“指标监控与告警系统”和第27章“支付系统”，默认合集跳过这两章；前次第27章单独译文仍保留为参考。

阅读导航、逐章文件及校验方法见 [目录说明](README.md)；[术语表](GLOSSARY.zh-CN.md) 和 [译校说明](EDITORIAL-NOTES.zh-CN.md) 也附于本文末尾。

原图、代码及参考链接沿用底本；图注、图片替代说明和正文已经翻译。图内的英文标注仍保留原样，可结合术语表阅读。少数源稿缺项与技术疑点单独标注，未将其当作可直接运行的代码或已核实的事实补全。

## 目录

"""
    ]
    if include_payment:
        parts.append("本次生成额外包含前次第27章支付系统译文，供完整对照。\n\n")
    for index, (number, filename) in enumerate(selected.items(), 1):
        title = (
            (HERE / filename)
            .read_text(encoding="utf-8")
            .splitlines()[0]
            .removeprefix("# ")
        )
        anchor = title.lower().replace(" ", "-")
        parts.append(f"{index}. [{title}](#{anchor})\n")
    parts.append("\n")
    for number, filename in selected.items():
        text = (HERE / filename).read_text(encoding="utf-8")
        if repair_tables:
            text = reading_text(number, text)
        parts.extend(["\n---\n\n", demote_headings(text)])
    parts.append("\n---\n\n## 译者阅读辅助（不属于英文原文）\n\n")
    for filename in ("GLOSSARY.zh-CN.md", "EDITORIAL-NOTES.zh-CN.md"):
        parts.append(demote_headings((HERE / filename).read_text(encoding="utf-8"), 2))
        parts.append("\n")
    COMBINED.write_text("".join(parts), encoding="utf-8")
    return COMBINED


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-table-repairs", action="store_true")
    parser.add_argument("--include-payment-reference", action="store_true")
    args = parser.parse_args()
    result = build(not args.no_table_repairs, args.include_payment_reference)
    print(f"Built {result.name}: {result.stat().st_size} bytes")
