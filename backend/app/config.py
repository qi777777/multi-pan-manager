import os
import secrets
from pydantic_settings import BaseSettings
from functools import lru_cache
from .core.logger import logger

class Settings(BaseSettings):
    """应用配置"""
    
    # 应用配置
    APP_NAME: str = "多网盘协同管理工具"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    
    # 数据库配置
    # 智能默认值：自动指向 app/data 目录下的数据库文件
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./app/data/pan_manager.db")
    
    # 加密密钥
    # 优先从环境变量读取，若未定义则动态生成一个（仅本次运行有效，建议用户在 .env 中固定）
    SECRET_KEY: str = os.getenv("SECRET_KEY", secrets.token_urlsafe(32))
    
    # CORS 配置
    CORS_ORIGINS: list = ["*"]
    
    class Config:
        # 统一加载项目根目录下的 .env 文件
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

# 自动确保数据目录存在
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

# 记录启动信息
logger.info(f"🚀 [CONFIG] 统一配置已激活")
logger.info(f"📦 [CONFIG] 数据库路径: {settings.DATABASE_URL.split('///')[-1]}")
if "SECRET_KEY" not in os.environ:
    logger.warning("⚠️  [CONFIG] 未检测到 SECRET_KEY 环境变量，已使用临时密钥启动。建议在 .env 中固定密钥以防数据解密失败。")

