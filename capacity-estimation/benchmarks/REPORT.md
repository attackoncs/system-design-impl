# 第3章验证记录

2026-10-21，Python3.13.9，Windows11。**9项测试通过，0.314秒**；包含真实CLI子进程。验证的是估算算术与输入规则，不是系统吞吐。

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
python -m capacity_estimation.cli estimate --scenario examples/social-media.json
python -m capacity_estimation.cli estimate --scenario examples/read-heavy.json
python -m capacity_estimation.cli estimate --scenario examples/read-heavy.json --sweep peak_factor=2,5,10
python -m capacity_estimation.cli units "1 MiB"
python -m capacity_estimation.cli availability 99.99
```

| 验证项 | 结果 |
|---|---|
| 原书媒体示例 | 1.5亿DAU，3亿写/日，30TB/日，五年54.75PB逻辑媒体 |
| MB / MiB | 1000000 / 1048576字节，严格区分 |
| 2000B逻辑×0.5压缩×3副本×1.2索引 | 3600B物理量 |
| 字节转比特 | 100B每秒载荷对应800bits/s |
| 节点上取整和备用 | 1.01倍容量需求向上取2台，再加1台备用 |
| 99.99%预算 | 日8.64秒、30天月259.2秒、365天年3153.6秒 |
| 不合法输入 | float/bool、NaN、越界比例、零利用率、分数副本、未知字段和极端零指数均拒绝 |
| 敏感性 | 峰值因子2/4/8独立变化，需求单调增长，原输入不变 |
| CLI | 单位换算与JSON场景/敏感性输出可解析 |

已保存实际命令输出：[social-media-result.json](social-media-result.json)、[read-heavy-result.json](read-heavy-result.json)、[sensitivity-result.json](sensitivity-result.json)。

读多写少示例：总峰值QPS约3645.83，保留逻辑量110621280000000B，加入压缩/三副本/索引后238941964800000B；缓存11212800000B；按给定单机2000QPS、70%利用率、1.5安全系数计算4台服务节点＋1台备用。这里的单机QPS是场景假设，**未经过硬件实测**。

循环小数使用80位有效数字，不要求有限Decimal表达式在任意外部精度下逐位相等，敏感性测试使用严格误差界。没有以原书旧延迟数值、当前云SLA或价格作为真实系统常量，也没有对未部署系统宣称性能或可用性达标。
