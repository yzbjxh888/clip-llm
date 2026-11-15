"""clip-llm 0.2 桌面层（PySide6）。

前台进程运行，不注册 systemd、不启动后台服务。依赖 PySide6 为可选：
    uv pip install --python .venv/bin/python PySide6
不装也能使用纯 CLI 核心（clipllm02 / serve）。

模块：
- app.py             DesktopApp：托盘、剪贴板轮询、generation、请求调度
- menu_window.py     MenuWindow：五区意图菜单
- result_window.py   ResultWindow：只读结果卡片 + 右键菜单
- settings_window.py SettingsWindow：可插拔设置（草稿事务 + 热重载）
- gateway.py         可选真实模型调用；无 API Key 时只显示本地请求预览
"""
