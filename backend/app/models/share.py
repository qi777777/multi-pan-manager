from sqlalchemy import Column, Integer, String, Text, DateTime, SmallInteger, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from ..database import Base


class Share(Base):
    """分享记录模型"""
    __tablename__ = "shares"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("disk_accounts.id"), nullable=False, comment="账户ID")
    share_id = Column(String(100), comment="网盘分享ID")
    share_url = Column(String(1000), nullable=False, comment="分享链接")
    title = Column(String(500), comment="资源标题")
    password = Column(String(50), comment="提取码")
    fid = Column(Text, comment="文件ID(JSON)")
    expired_at = Column(DateTime, comment="过期时间")
    status = Column(SmallInteger, default=1, comment="状态: 0=已失效,1=有效")
    expired_type = Column(SmallInteger, comment="时长类型: 1=永久, 2=7天, 3=1天, 4=30天")
    file_path = Column(String(1000), default="", comment="文件存储路径")
    created_at = Column(DateTime, server_default=func.now())
    
    # 关联
    account = relationship("DiskAccount", backref="shares")
