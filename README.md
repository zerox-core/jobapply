# jobapply — 秋招网申自动化助手

从飞书多维表格拉取职位 → AI 评估匹配度 → Playwright 驱动浏览器预填网申表单 → **你本人核对后手动点提交**。

## 安全底线

- **程序永远不自动提交网申**。AI 只负责把表单填好并暂停，提交按钮永远由人点。
- 所有个人数据（简历、职位池、填表报告、登录态、API key）**只存在你本机**，仓库里不含任何个人信息。

## 功能

- **职位池**：从飞书多维表格导入职位（链接、公司、岗位、描述），AI 逐条评估匹配度与推荐理由
- **简历管理**：上传 PDF / Word 简历，LLM 结构化为档案（教育 / 实习 / 项目 / 校园经历 / 技能），支持人工补充
- **AI 预填**：Playwright 打开网申页面，LLM 识别表单字段并用简历档案自动填写，支持上传简历附件
- **登录态共享**：面板内所有职位链接走内置通道，自动复用自动化浏览器里已登录的会话；也可通过 CDP 直接接管你的日常浏览器
- **对话面板**：与职位助手多轮对话，让它筛职位、答疑、批量操作
- **填表报告**：每次预填生成报告（填了哪些字段、哪些需要人工补），可追溯

## 快速开始

环境：Windows + Python 3.10+

```bat
git clone https://github.com/zerox-core/jobapply.git
cd jobapply
pip install -r requirements.txt
playwright install chromium
copy config.example.yaml config.yaml
```

然后编辑 `config.yaml`：

1. 把简历 PDF 放进 `resume\` 目录，改 `resume_attachment` 指向它
2. 配置 LLM（二选一）：
   - 有 llm_hub：把 `hub_config` 指向你的 `data.json`
   - 没有：直接填 `base_url` / `api_key` / `model`（OpenAI 兼容接口均可）

启动：

```bat
start.bat
```

浏览器自动打开 http://127.0.0.1:8899 ，在「设置」页填飞书多维表格的 app token 即可开始导入职位。

## 目录说明

| 路径 | 内容 | 是否入库 |
| --- | --- | --- |
| `server.py` / `jobapply/` / `static/` | 程序本体 | 是 |
| `tests/` / `run_tests.py` | 测试 | 是 |
| `config.example.yaml` | 配置模板 | 是 |
| `config.yaml` | 你的真实配置 | **否**（gitignore） |
| `data/` | 简历档案、职位池、填表报告 | **否**（gitignore） |
| `resume/` | 简历 PDF | **否**（gitignore） |
| `browser_profile/` | 自动化浏览器登录态 | **否**（gitignore） |

## 给朋友用

克隆后按「快速开始」配好自己的 `config.yaml` 和简历即可。你们之间不会共享任何数据——每个人的职位池、简历、登录态都在各自电脑上。

## 运行测试

```bat
py run_tests.py
```
