# 中文代码导读原稿

这里每个 Markdown 文件对应一个子项目。原稿承担教学解释：具体案例、状态归属、关键取舍、逐站源码阅读、故障推演和带答案自查。生成器只绑定源码与摘录，不替代人工理解。

发布与同步检查：

```powershell
python tools/build_learning_guides.py
python tools/build_learning_guides.py --check
```

引用格式（路径相对于子项目根目录）：

- `{{symbol:src/package/module.py:Class.method}}`：链接到当前定义，可同样引用测试函数或测试类方法。
- `{{file:src/package/module.py}}`：文件链接。
- `{{excerpt:src/package/module.py:Class.method:5:12}}`：从定义行起偏移5行，摘录12行真实源码。摘录不得超出定义范围，最多18行，正文解释摘录中的关键判断和状态变化。

每份原稿选择2—4张图，确保图中每条关键路径都能在源码中定位。宏观图说明组件职责、数据和同步／异步边界；微观说明输入、分支、修改的状态及输出。对具体配置讲解，区分可替换后端和模拟外部服务。

检查不执行子项目代码；修改业务语义后应复核案例预期与相关测试。不要依靠重新生成来自动确认时序和解释仍然正确。
