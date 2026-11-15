"""clip-llm 0.2 Studio：统一的词典式桌面应用。

融合三份资产：
- ``gd_llm.py`` / clip-llm 的分类规则、9 套完整提示词、LaTeX/JSON 处理规则
  → ``spec/legacy_assets.json`` + 0.2 的 features/policy/prompts
- Chrome Math OCR 扩展的视觉识别流程 → ``vision.py``
- clip-llm 的剪贴板监听、缓存、设置与桌面集成 → ``desktop/`` 基座

两层 UI 都在**同一个主窗口的同一个标签页**里：
- 第一层 初判：内容预览、硬拦截、领域候选（可切换）、领域专属查询方式、
  其他领域推荐、自由输入、外部网页搜索；
- 第二层 结果：点击查询方式后组装提示词、请求大模型、精美 JSON 排版。

交互对齐常规词典：新查询覆盖当前标签、可复制内容再查询、可在新标签页打开。
"""
