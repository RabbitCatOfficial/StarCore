# Nebula Audit

前言：感谢大白AI对本项目的大力赞助

大白AI QQ群号：1083850789

大白AI官网：https://ai-api.db-kj.com/

使用我的邀请码注册 https://ai-api.db-kj.com/register?aff=mtdt

加入群聊找管理员即可领取5$额度

![dbai](dbai.jpg)

基于 `Python + Qt (PySide6)` 的桌面代码审计工具，从零实现，满足以下能力：

- 拖拽源码压缩包到聊天式界面，自动解压并发起 LLM 代码审计
- 强制使用流式输出展示审计结果
- 配置页支持一键测试模型连接
- 本地保存 Markdown 审计报告，并在软件内管理和预览
- 支持 OpenAI、DeepSeek、Qwen、Moonshot、SiliconFlow、OpenRouter、AI-DB-KJ、Ollama 和自定义接口配置
- 同时支持 `chat/completions`、`responses` 和 `Ollama api/chat` 三种接口协议
- 使用带科幻感但不过度炫光的 Qt 深色界面

## 运行

```powershell
cd nebula_audit_app
python -m pip install -r requirements.txt
python main.py
```

## 目录

- `main.py`: 启动入口
- `nebula_audit/config.py`: 应用配置和模型预设
- `nebula_audit/audit.py`: 压缩包处理、代码快照构建、审计提示词
- `nebula_audit/llm.py`: 流式模型客户端
- `nebula_audit/workers.py`: 后台线程任务
- `nebula_audit/ui.py`: Qt 图形界面
- `runtime_data/reports/`: 本地报告目录

## 说明

- 当前原生支持 `zip` 与 `tar/tar.gz/tgz` 压缩包。
- 审计快照不再注入本地漏洞线索或关键字命中，直接由模型基于源码自行梳理调用链和业务流。
- 如果第三方网关走 OpenAI `Responses API`，可在 `API 配置` 中将“接口协议”设为 `OpenAI Responses API`，接口路径通常填写 `responses`。
