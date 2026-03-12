# 🤝 贡献指南 (Contributing Guide)

感谢您对 **多网盘协同管理系统** 感兴趣！我们欢迎所有形式的贡献，无论是修复 Bug、改进文档，还是提交新功能。

## 🛠 开发预览

### 环境要求
- Python 3.10+
- Node.js 18+
- Docker & Docker Compose (用于环境模拟)

### 本地启动
1. **Clone 仓库**:
   ```bash
   git clone https://github.com/qi777777/multi-pan-manager.git
   ```
2. **后端调试**:
   - 进入 `backend` 目录，创建虚拟环境并安装依赖。
   - 复制 `.env.example` 为 `.env` 并配置。
   - 运行 `python run.py`。
3. **前端调试**:
   - 进入 `frontend` 目录，安装依赖。
   - 运行 `npm run dev`。

## 📝 提交规范

### 分支管理
- 建议从 `main` 分支切出 `feature/xxx` 或 `fix/xxx` 分支进行开发。

### Commit 信息
请遵循简单的约定：
- `feat: [内容]` - 新功能
- `fix: [内容]` - 修复 Bug
- `docs: [内容]` - 文档更新
- `style: [内容]` - 仅涉及样式/排版修改
- `refactor: [内容]` - 代码重构

## 🐛 报告问题
请使用 GitHub 的 [Issues](https://github.com/qi777777/multi-pan-manager/issues) 页面提交您的发现。请尽可能详尽地描述复现步骤。

---
**让我们一起构建更好的私有云网关！**
