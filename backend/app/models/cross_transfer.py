"""跨网盘转存任务模型"""
from sqlalchemy import Column, Integer, String, Text, DateTime, SmallInteger, ForeignKey, BigInteger
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from ..database import Base


class CrossTransferTask(Base):
    """跨网盘转存任务"""
    __tablename__ = "cross_transfer_tasks"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    source_account_id = Column(Integer, ForeignKey("disk_accounts.id"), nullable=False, comment="源账户ID")
    source_fid = Column(String(500), nullable=False, comment="源文件ID")
    source_file_name = Column(String(500), comment="源文件名")
    source_file_size = Column(BigInteger, default=0, comment="文件大小")
    source_file_md5 = Column(String(64), comment="文件MD5")
    target_account_id = Column(Integer, ForeignKey("disk_accounts.id"), nullable=False, comment="目标账户ID")
    target_path = Column(String(500), default="/", comment="目标路径")
    status = Column(SmallInteger, default=0, comment="状态: 0=待处理,1=进行中,2=成功,3=失败")
    transfer_type = Column(SmallInteger, default=2, comment="传输类型: 0=秒传,1=普通上传,2=流式传输")
    result_fid = Column(String(500), comment="目标文件ID")
    result_path = Column(String(1000), comment="目标文件完整路径")
    error_message = Column(Text, comment="错误信息")
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime)
    
    # 新增：进度跟踪字段
    progress = Column(SmallInteger, default=0, comment="进度百分比 0-100")
    current_step = Column(String(100), comment="当前步骤描述")
    
    # 新增：文件夹传输支持
    parent_task_id = Column(Integer, ForeignKey("cross_transfer_tasks.id"), nullable=True, comment="父任务ID（文件夹传输）")
    is_folder = Column(SmallInteger, default=0, comment="是否为文件夹任务: 0=否, 1=是（父任务）")
    total_files = Column(Integer, default=0, comment="总文件数（仅父任务）")
    completed_files = Column(Integer, default=0, comment="已完成文件数（仅父任务）")
    source_folder_path = Column(String(1000), comment="源文件夹路径")
    
    # 新增：三层结构支持（多目标文件夹传输）
    master_task_id = Column(Integer, ForeignKey("cross_transfer_tasks.id"), nullable=True, comment="顶层主任务ID（多目标文件夹）")
    is_master = Column(SmallInteger, default=0, comment="是否为顶层主任务: 0=否, 1=是")
    total_targets = Column(Integer, default=0, comment="目标数量（仅主任务）")
    completed_targets = Column(Integer, default=0, comment="已完成目标数（仅主任务）")
    
    # 关联
    source_account = relationship("DiskAccount", foreign_keys=[source_account_id], backref="source_transfers")
    target_account = relationship("DiskAccount", foreign_keys=[target_account_id], backref="target_transfers")
    
    # 父子任务关联
    parent_task = relationship("CrossTransferTask", remote_side=[id], foreign_keys=[parent_task_id], backref="child_tasks")
    
    # 主任务关联
    master_task = relationship("CrossTransferTask", remote_side=[id], foreign_keys=[master_task_id], backref="target_tasks")

    
    # 状态常量
    STATUS_PENDING = 0
    STATUS_RUNNING = 1
    STATUS_SUCCESS = 2
    STATUS_FAILED = 3
    STATUS_PAUSED = 4
    STATUS_PARTIAL_SUCCESS = 5  # 部分成功
    STATUS_CANCELLED = 6        # 已取消
    
    STATUS_MAP = {
        0: "待处理",
        1: "进行中",
        2: "成功",
        3: "失败",
        4: "已暂停",
        5: "部分成功",
        6: "已取消"
    }
    
    # 传输类型常量
    TRANSFER_RAPID = 0
    TRANSFER_NORMAL = 1
    TRANSFER_STREAM = 2  # 流式传输（下载后上传）
    TRANSFER_CHAIN = 3   # 链式转存触发的互传（不在互传页面显示）
    
    @property
    def status_name(self):
        return self.STATUS_MAP.get(self.status, "未知")
    
    @property
    def source_account_type(self):
        return self.source_account.type if self.source_account else None
    
    @property
    def source_account_name(self):
        return self.source_account.name if self.source_account else None
    
    @property
    def target_account_type(self):
        return self.target_account.type if self.target_account else None
    
    @property
    def target_account_name(self):
        return self.target_account.name if self.target_account else None

    def to_dict(self):
        """序列化为字典"""
        return {
            "id": self.id,
            "source_account_id": self.source_account_id,
            "source_fid": self.source_fid,
            "source_file_name": self.source_file_name,
            "source_file_size": self.source_file_size,
            "target_account_id": self.target_account_id,
            "target_path": self.target_path,
            "status": self.status,
            "status_name": self.status_name,
            "transfer_type": self.transfer_type,
            "result_path": self.result_path,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "source_account_type": self.source_account_type,
            "source_account_name": self.source_account_name,
            "target_account_type": self.target_account_type,
            "target_account_name": self.target_account_name,
            "progress": self.progress,
            "current_step": self.current_step,
            "is_folder": self.is_folder,
            "total_files": self.total_files,
            "completed_files": self.completed_files,
            "parent_task_id": self.parent_task_id,
            "is_master": self.is_master,
            "master_task_id": self.master_task_id,
            "total_targets": self.total_targets,
            "completed_targets": self.completed_targets,
            "children": None,
            "target_tasks": None
        }

