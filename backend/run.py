import uvicorn
import os

if __name__ == "__main__":
    # 确保在 backend 目录下运行
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
