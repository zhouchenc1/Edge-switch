# Python 回归测试

此目录保留旧版 Python 桌面、配置兼容和远端 Agent 的回归测试。新版 Tauri 仍复用部分远端 Python 逻辑，因此这些测试仍需维护。

在项目根目录运行（Windows，Python 需包含可用的 Tcl/Tk）：

```powershell
python -m pip install -r requirements.txt pytest
python -m pytest -q
```

单独运行某个文件：

```powershell
python -m pytest -q tests/test_auth.py
```

`pytest.ini` 将默认收集范围限制在此目录，避免误收集 `build/` 等目录中的临时验证脚本。

| 范围 | 测试文件 |
| --- | --- |
| 供应商配置、协议适配与凭据隔离 | `test_api_services.py`、`test_provider_tools.py` |
| SSH 授权、官方账号与凭据导入 | `test_auth.py`、`test_official_import.py` |
| 远端工具复用、编辑器与 VS Code 引导 | `test_shared_tools.py`、`test_editor_runtime.py`、`test_ide_extension.py`、`test_vscode_bootstrap.py` |
| Codex 账号切换后的历史与 Linux 迁移 | `test_codex_history.py`（真实 symlink、SQLite WAL；Linux 专属检查在 Windows 跳过） |
| 配置修复、兼容、连接生命周期与综合行为 | `test_repair.py`、`test_lifecycle.py`、`test_reopen.py`、`test_workbench.py` |
| 旧版 Tk 界面、工作环境与后台任务 | `test_official_card.py`、`test_environments.py`、`test_background.py`、`test_workflow.py` |

部分综合测试也会创建 Tk 窗口。若报 `Can't find a usable init.tcl`，应修复本机 Python 的 Tcl/Tk 环境，而不是删除测试或跳过断言。

新版前端测试位于 `src/`，使用 `npm test`；Rust 测试使用 `cargo test --manifest-path src-tauri/Cargo.toml --locked --lib`。

Linux 历史迁移检查无需桌面或第三方依赖：`python -m unittest discover -s tests -p test_codex_history.py -v`。
