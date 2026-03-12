from sqlalchemy import Column, Integer, String, Text, DateTime, SmallInteger
from sqlalchemy.sql import func
from ..database import Base


class DiskAccount(Base):
    """网盘账户模型"""
    __tablename__ = "disk_accounts"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, comment="账户名称")
    type = Column(SmallInteger, nullable=False, comment="网盘类型: 0=夸克,1=阿里,2=百度,3=UC,4=迅雷")
    credentials = Column(Text, nullable=False, comment="加密后的凭证(Token/Cookie)")
    storage_path = Column(String(500), comment="默认存储路径")
    storage_path_temp = Column(String(500), comment="临时资源路径")
    status = Column(SmallInteger, default=1, comment="状态: 0=禁用,1=正常,2=凭证过期")
    last_check_at = Column(DateTime, comment="最后检测时间")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())
    
    # Token缓存字段（避免频繁刷新token）
    cached_token = Column(Text, comment="缓存的访问令牌")
    token_expires_at = Column(Integer, comment="令牌过期时间戳")
    config = Column(Text, default="{}", comment="扩展配置(JSON)")
    
    # 网盘类型映射
    DISK_TYPES = {
        0: "夸克网盘",
        1: "阿里云盘",
        2: "百度网盘",
        3: "UC网盘",
        4: "迅雷云盘"
    }
    
    @property
    def type_name(self):
        return self.DISK_TYPES.get(self.type, "未知")
