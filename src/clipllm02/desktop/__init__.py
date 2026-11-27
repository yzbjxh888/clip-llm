"""clip-llm 0.2 桌面层（PySide6）。

前台进程运行，不注册 systemd、不启动后台服务。依赖 PySide6 为可选：
    uv pip install --python .venv/bin/python PySide6
不装也能使用纯 CLI 核心（clipllm02 / serve）。

模块（classic 应用已删除，以下均为 Studio 共用）：
- gateway.py          可选真实模型调用；无 API Key 时只显示本地请求预览
- window_base.py      Studio 主窗口基类（依赖 x11_state.py）
- clipboard_worker.py Studio 剪贴板读取线程
"""
