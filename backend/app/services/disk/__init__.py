from typing import Dict, Any
from .base import BaseDiskService
from .quark import QuarkDiskService
from .alipan import AlipanDiskService
from .baidu import BaiduDiskService
from .uc import UcDiskService
from .xunlei import XunleiDiskService
from ...core.logger import logger


def get_disk_service(disk_type: int, credentials: str, config: Dict[str, Any] = None) -> BaseDiskService:
    """
    获取网盘服务实例
    
    Args:
        disk_type: 网盘类型 (0=夸克, 1=阿里, 2=百度, 3=UC, 4=迅雷)
        credentials: 凭证 (Cookie/Token)
        config: 配置项
    
    Returns:
        BaseDiskService 实例
    """
    services = {
        0: QuarkDiskService,
        1: AlipanDiskService,
        2: BaiduDiskService,
        3: UcDiskService,
        4: XunleiDiskService
    }
    
    service_class = services.get(disk_type)
    if not service_class:
        raise ValueError(f"不支持的网盘类型: {disk_type}")
    
    logger.debug(f"[DISK] 初始化服务实例: type={disk_type}, class={service_class.__name__}")
    return service_class(credentials, config)


__all__ = [
    "BaseDiskService",
    "QuarkDiskService",
    "AlipanDiskService",
    "BaiduDiskService",
    "UcDiskService", 
    "XunleiDiskService",
    "get_disk_service"
]
