# ☁️ 多网盘协同管理系统 (Multi-Pan Manager)

<p align="center">
  <img src="assets/banner.png" alt="Multi-Pan Manager Banner" width="800" />
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Status-Flagship-brightgreen?style=for-the-badge" />
  <img src="https://img.shields.io/badge/Architecture-x86__64%20%7C%20arm64-blue?style=for-the-badge" />
  <img src="https://img.shields.io/badge/Framework-FastAPI-009688?style=for-the-badge&logo=fastapi" />
  <img src="https://img.shields.io/badge/Frontend-React_v18-61DAFB?style=for-the-badge&logo=react" />
</p>

## 🌟 项目简介

**Multi-Pan Manager** 是一个专注于**多账号统一调度、跨盘高速互传、实时任务监控**的旗舰级个人云盘网关。它深度整合了各主流网盘的底层协议，解决了账号孤岛、转存繁琐、进度不可见等核心痛点。

> [!CAUTION]
> **使用须知与免责声明**
> 1. **严禁商用**：本系统仅供个人学习、研究及资源整理使用，**禁止任何形式的商业盈利行为**（包括但不限于通过本工具进行倒卖账号、付费代转资源等）。
> 2. **数据责任**：请遵守各网盘平台的服务协议。开发者对用户因违规操作导致的封号、数据丢失等后果不承担任何法律责任。
> 3. **版权保护**：请尊重正版资源，禁止利用本工具分发非法/侵权内容。

---

## 🔥 核心特性

*   **分片流式中转 (Chunk-Streaming)**：跨盘互传采用内存分片流技术，无须完整下载到服务器磁盘即可实现“即下即传”，极低磁盘占用且高效。
*   **多盘聚合管理**：单界面操作夸克、阿里、百度、UC、迅雷等主流网盘。
*   **极致目录上传**：支持文件夹递归上传，自动在云端重建复杂的本地目录结构。
*   **实时进度矩阵**：基于 SSE 协议，精准展示各网盘在传输不同文件时的真实进度。

---

## 📸 界面导览 (Screenshots)

<details open>
  <summary><b>账户管理 (多盘聚合视图)</b></summary>
  <p align="center">
    <!-- Replace this placeholder with the actual image path, e.g., assets/account.png -->
    <img src="assets/screenshots/account.png" alt="账户管理" width="800" />
  </p>
</details>

<details open>
  <summary><b>文件管理 (分片流式上传)</b></summary>
  <p align="center">
    <img src="assets/screenshots/file_manage.png" alt="文件管理" width="800" />
  </p>
</details>

<details open>
  <summary><b>网盘互传 (跨盘多点分发)</b></summary>
  <p align="center">
    <img src="assets/screenshots/cross_transfer.png" alt="网盘互传" width="800" />
  </p>
</details>

<details open>
  <summary><b>转存工具 (主流网盘资源链批量转存)</b></summary>
  <p align="center">
    <img src="assets/screenshots/transfer_tool.png" alt="转存工具" width="800" />
  </p>
</details>

<details open>
  <summary><b>分享管理 (聚合网盘分享链管控)</b></summary>
  <p align="center">
    <img src="assets/screenshots/share_manage.png" alt="分享管理" width="800" />
  </p>
</details>

---

## 🏗 环境要求

| 维度 | 最低配置 | 推荐配置 |
| :--- | :--- | :--- |
| **CPU** | 1 核 (x86_64 / ARM64) | 2 核+ |
| **内存** | 512MB (系统可用) | 2GB+ |
| **磁盘** | 100MB 基础占用 | 2GB+ (建议 SSD 提升 `temp_data` 性能) |
| **系统** | Linux / macOS / Windows / Docker | Docker (推荐) |

---

## 🚀 部署指南

### 方式一：Docker 部署 (生产环境推荐 🚀)
1. **下载项目**
   ```bash
   git clone https://github.com/qi777777/multi-pan-manager.git
   cd multi-pan-manager
   ```
2. **启动服务**
   ```bash
   docker-compose up -d
   ```
   *   **访问地址**：`http://localhost:3000`
   *   **预设账号/密码**：可在容器启动后访问管理界面查看。

---

### 方式二：手动部署 (开发调试模式)

#### 1. 后端环境 (Python 3.10+)

**Linux / macOS:**
```bash
cd backend
# 1. 创建并激活虚拟环境
python3 -m venv venv
source venv/bin/activate

# 2. 安装项目依赖
pip install -r requirements.txt

# 3. 配置文件初始化 (执行拷贝后，请编辑 .env 修改 SECRET_KEY)
cp .env.example .env 

# 4. 启动后端服务器
python3 run.py
```

**Windows:**
```powershell
cd backend
# 1. 创建并激活虚拟环境
python -m venv venv
.\venv\Scripts\activate

# 2. 安装项目依赖
pip install -r requirements.txt

# 3. 配置文件初始化 (执行拷贝后，请编辑 .env 修改 SECRET_KEY)
copy .env.example .env

# 4. 启动后端服务器
python run.py
```

#### 2. 前端环境 (Node.js 18+)
```bash
cd frontend
# 1. 安装核心框架及 UI 组件
npm install 

# 2. 启动前端开发服务器
npm run dev
```

---

## ⚙️ 配置文件说明 (.env)

系统通过 `backend/.env` 进行细粒度配置。**首次部署时必须执行以下步骤：**

1.  **激活模板**：执行 `cp backend/.env.example backend/.env` (或 Windows 下使用 `copy`)。
2.  **配置密钥**：修改 `SECRET_KEY`。保持默认值会导致登录态在多实例或重启后失效，且存在安全隐患。
3.  **调试模式**：生产环境中确保 `DEBUG=false`。

---

## ❓ 常见问题 (FAQ)

**Q: 为什么上传进度条上方显示的文件名不一样？**
A: 本系统支持真正的并发分发。不同网盘的接口响应速度不一，系统会实时反馈每个账号当前正在处理的真实文件名，确报进度“所见即所得”。

**Q: Nginx 反向代理下进度条不动？**
A: 请确保关闭了代理缓冲，否则 SSE 消息会被延迟拦截：
```nginx
proxy_buffering off;
proxy_read_timeout 3600s;
```

---

## 💖 致谢与开源参考 (Credits)

本项目的诞生离不开以下优秀项目的原理启发与协议分析方案：

*   **[LinkSwift](https://github.com/hmjz100/LinkSwift)**：网盘 API 逆向及多账户调度模型。
*   **[QuarkPan](https://github.com/lich0821/QuarkPan)**：夸克网盘协议层实现参考。
*   **[BaiduPCS-Py](https://github.com/PeterDing/BaiduPCS-Py)**：百度网盘文件路径转换逻辑参考。
*   **[Quark2Baidu](https://github.com/pjx1314/Quark2Baidu)**：秒传逻辑参考。
*   **[xinyue-search](https://github.com/675061370/xinyue-search)**：部分加密指纹提取参考。

