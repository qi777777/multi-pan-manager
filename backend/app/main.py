import asyncio
import os
from .core.logger import logger
try:
    import uvloop
    # 强制禁用 uvloop，使用标准 asyncio 策略
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    logger.warning("⚠️  [App] Detected uvloop, forced asyncio.DefaultEventLoopPolicy()")
except ImportError:
    logger.info("✅  [App] uvloop not installed, using default asyncio")

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from .core.limiter import limiter
from .config import settings
from .database import engine, Base
from .api import accounts, files, transfer, shares, cross_transfer, system, auth
from .models import User, DiskAccount, TransferTask, Share, CrossTransferTask  # 导入模型以注册到 Base
from .core.auth import get_current_user, get_password_hash
from sqlalchemy.orm import Session

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时创建数据库表
    Base.metadata.create_all(bind=engine)
    
    # 初始化管理员账户
    from .database import SessionLocal
    db: Session = SessionLocal()
    try:
        admin_user = db.query(User).filter(User.username == "admin").first()
        if not admin_user:
            logger.info("➕ [App] 正在创建默认管理员账户...")
            new_admin = User(
                username="admin",
                # 由于前端页面在发送前会对密码进行 SHA-256 预哈希，因此这里对应的“明文”应为该哈希值
                # SHA256("admin123") = 240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9
                hashed_password=get_password_hash("240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9"),
                full_name="管理员"
            )
            db.add(new_admin)
            db.commit()
            logger.info("✅ [App] 默认管理员账户创建成功 (admin/admin123)")
    except Exception as e:
        logger.error(f"❌ [App] 初始化管理员失败: {e}")
    finally:
        db.close()
    
    yield
    # 关闭时的清理工作（如果需要）


# 应用初始化
app = FastAPI(
    title="多网盘协同管理工具",
    description="支持夸克、百度、迅雷、UC等网盘的自动化管理与文件互传",
    version="2.0.0",
    lifespan=lifespan
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(auth.router, prefix="/api/auth", tags=["认证管理"])
app.include_router(accounts.router, prefix="/api/accounts", tags=["账户管理"], dependencies=[Depends(get_current_user)])
app.include_router(files.router, prefix="/api/files", tags=["文件管理"], dependencies=[Depends(get_current_user)])
app.include_router(transfer.router, prefix="/api/transfer", tags=["资源转存"], dependencies=[Depends(get_current_user)])
app.include_router(shares.router, prefix="/api/shares", tags=["分享管理"], dependencies=[Depends(get_current_user)])
app.include_router(cross_transfer.router, prefix="/api/cross-transfer", tags=["跨网盘互传"], dependencies=[Depends(get_current_user)])
app.include_router(system.router, prefix="/api/system", tags=["系统管理"], dependencies=[Depends(get_current_user)])


@app.get("/")
async def root():
    return {"message": "多网盘协同管理工具 API", "version": settings.APP_VERSION}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}

