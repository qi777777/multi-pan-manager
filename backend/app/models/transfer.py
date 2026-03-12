from sqlalchemy import Column, Integer, String, Text, DateTime, SmallInteger, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship, backref
from ..database import Base


class TransferTask(Base):
    """转存任务模型"""
    __tablename__ = "transfer_tasks"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    source_url = Column(String(1000), nullable=False, comment="源链接")
    source_type = Column(SmallInteger, nullable=False, comment="源网盘类型")
    source_code = Column(String(50), comment="提取码")
    target_account_id = Column(Integer, ForeignKey("disk_accounts.id"), nullable=False, comment="目标账户ID")
    storage_path = Column(String(500), default="0", comment="存储路径")
    parent_task_id = Column(Integer, ForeignKey("transfer_tasks.id"), nullable=True, comment="父任务ID（链式转存）")
    chain_status = Column(String(200), nullable=True, comment="链式状态描述")
    status = Column(SmallInteger, default=0, comment="状态: 0=待处理,1=进行中,2=成功,3=失败")
    result_share_url = Column(String(1000), comment="转存后的分享链接")
    result_fid = Column(Text, comment="转存后的文件ID")
    result_title = Column(String(500), comment="资源标题")
    error_message = Column(Text, comment="错误信息")
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime)
    
    # 关联
    target_account = relationship("DiskAccount", backref="transfer_tasks")
    children = relationship("TransferTask", 
                           foreign_keys=[parent_task_id],
                           lazy="select")
    
    # 状态映射
    STATUS_PENDING = 0
    STATUS_RUNNING = 1
    STATUS_SUCCESS = 2
    STATUS_FAILED = 3
    
    STATUS_MAP = {
        0: "待处理",
        1: "进行中",
        2: "成功",
        3: "失败"
    }
    
    @property
    def status_name(self):
        return self.STATUS_MAP.get(self.status, "未知")

