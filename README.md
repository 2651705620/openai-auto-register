# 官方已大开杀戒，此仓库已失效，可自己修改跳过限制，仅供学习

> 支持 **纯 API 协议注册**（推荐）和 **浏览器自动化注册**（旧版）两种模式。  
> 账号格式： `邮箱----密码----client_id----refresh_token`。
> 域名邮箱自动注册可能存在bug，目前无法发现，测试500并发把测试的域名都玩黑了，域名邮箱收不到验证码，无法继续测试。
> 有问题可以直接丢给ai，因在上学，提Issues不一定能及时看见。
> 项目仅供参考学习，请勿滥用！！！
---

## 目录结构

```
openai-auto-register/
├── api-register-go/      ← ⚡ 高并发：Go 版，支持数十账号同时注册，资源占用极低
├── api-register-py/      ← 🛡️ 高稳定：Python 版，逻辑清晰，出错重试完善，适合稳跑
└── browser-register-py/  ← 旧版浏览器自动化（Playwright，速度较慢，几乎不可用了）
```

### 如何选择？

| | ⚡ Go 版（api-register-go） | 🛡️ Python 版（api-register-py） |
|---|---|---|
| **核心优势** | **高并发**，goroutine 轻量调度 | **高稳定**，异常处理完善，重试健壮 |
| 并发性能 | 极高，50+ 并发无压力 | 中等，线程池 10~20 并发 |
| 稳定性 | 良好 | ✅ 更强，滑动窗口失败率保护 |
| 运行环境 | 无需安装，直接运行 exe | 需要 Python 3.10+ |
| 适用场景 | 大批量账号、追求速度 | 账号稳定性、调试排查 |
| XOAUTH2 | ✅ | ✅ |
| 域名邮箱 | ✅ | ✅ |
| Web 控制台 | ✅ | ✅ |

> 💡 **推荐策略**：首选 Go 版跑量，遇到批量失败时切换 Python 版排查问题。
---

## api-register-go（推荐）

**Go 高并发纯 API 版**，内置 Web 控制台，无需安装任何环境，直接运行 `.exe`。

### 特性
- 🚀 高并发，goroutine 调度，支持自定义并发数
- 🔐 Outlook 账号支持 **XOAUTH2** 和密码两种 IMAP 认证
- 📧 内置集成 IMAP 服务，支持域名邮箱 catch-all
- 🌐 内置 Web 控制台（端口 `8899`），实时 SSE 日志
- 🔄 支持 **注册模式** 和 **登录刷新 Token 模式**

### 使用方法

1. 进入 `api-register-go/` 目录
2. 双击运行 `register.exe`（或命令行执行）
3. 浏览器打开 `http://localhost:8899`
4. 在界面中填入账号列表，选择参数，点击开始

### 账号格式

```
# Outlook 账号（仅密码）
DeannaSmith1590@outlook.com----password123

# Outlook 账号（XOAUTH2，优先使用）
DeannaSmith1590@outlook.com----password123----client_id----refresh_token

# 域名邮箱（需配置 IMAP 服务）
user@yourdomain.com----password123
```

### 结果输出

注册成功的账号保存在 `tokens/` 目录，每个账号一个 JSON 文件：

```json
{
  "email": "example@outlook.com",
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": "2025-12-31T00:00:00Z"
}
```

### 从源码编译

需要 Go 1.21+：

```bash
cd api-register-go
go build -o register.exe .
```

---

## api-register-py

**Python 纯 API 版**，功能与 Go 版一致，Web 控制台同样运行在 `8899` 端口。

### 使用方法

```bash
cd api-register-py
pip install -r requirements.txt
python web_server.py
# 浏览器打开 http://localhost:8899
```

或者直接双击 `start.bat`（Windows）。

### 依赖

- `curl_cffi >= 0.7.0`（自带浏览器 TLS 指纹，无需安装 Playwright）
- Python 3.10+

---

## browser-register-py（旧版）

**Playwright 浏览器自动化版**，通过控制真实浏览器完成注册，兼容性强但速度慢。

### 使用方法

```bash
cd browser-register-py
pip install -r requirements.txt
playwright install chromium
python main.py
```

或双击 `start.bat`。

---

## 配置说明

`config.template.json` 为配置模板，复制为 `config.json` 并填写后使用：

```json
{
  "proxy": "http://127.0.0.1:7890",
  "imap": {
    "host": "mail.yourdomain.com",
    "port": 993,
    "username": "catchall@yourdomain.com",
    "password": "your_imap_password",
    "use_tls": true
  }
}
```
## 遇到问题

**Q：`ModuleNotFoundError: No module named 'xxx'`，但 `pip install` 显示已安装**  
A：多 Python 版本冲突，`pip` 装在了别的版本里。改用以下命令确保装到同一个 Python：
```bash
python -m pip install -r requirements.txt
```

**Q：Outlook 账号 IMAP 登录失败 / `LOGIN failed`**  
A：微软已关闭基础密码认证，需使用 XOAUTH2。账号格式改为：
```
邮箱----密码----client_id----refresh_token
```

**Q：一直在等待验证码，没有收到**  
A：检查以下几点：
- Outlook 账号：确认 `refresh_token` 有效，或检查邮件是否进了垃圾箱
- 域名邮箱：确认 IMAP 配置中的 `host`/`port`/`username`/`password` 正确，`use_tls` 按端口设置（993→true，143→false）
- 尝试减少并发数，避免 OpenAI 触发限流

**Q：注册成功但 Token 文件在哪里？**  
A：在各版本目录下的 `tokens/` 文件夹，每个账号对应一个 `邮箱.json` 文件。

**Q：浏览器版（browser-register-py）报 Playwright 错误**  
A：需要单独安装浏览器内核：
```bash
python -m playwright install chromium
```

---

## 注意事项
- 📋 Outlook 账号需要在 Microsoft 开发者平台申请 `client_id` 并获取 `refresh_token` 方可使用 XOAUTH2

---

## License

MIT
