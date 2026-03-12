from .accounts import router as accounts_router
from .files import router as files_router
from .transfer import router as transfer_router
from .shares import router as shares_router

# 导出所有路由模块
__all__ = ["accounts", "files", "transfer", "shares"]
