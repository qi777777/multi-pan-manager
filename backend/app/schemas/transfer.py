from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List


class TransferParseRequest(BaseModel):
    """解析链接请求"""
    url: str
    code: Optional[str] = ""


class TransferParseResponse(BaseModel):
    """解析链接响应"""
    title: str
    share_url: str
    source_type: int
    source_type_name: str
    stoken: Optional[str] = None


class TransferTarget(BaseModel):
    """转存目标"""
    account_id: int
    path: Optional[str] = "0"  # 该账户的存储路径
    need_share: Optional[bool] = True  # 是否创建分享
    expired_type: Optional[int] = 1  # 提取时效 1=永久 2=临时


class TransferExecuteRequest(BaseModel):
    """执行转存请求"""
    url: str
    code: Optional[str] = ""
    # 兼容旧字段
    target_account_id: Optional[int] = None
    target_account_ids: Optional[List[int]] = []
    storage_path: Optional[str] = "0"
    
    # 新字段：支持每个账户独立路径
    targets: Optional[List[TransferTarget]] = []
    
    expired_type: int = 1  # 1=永久, 2=临时
    enable_cross_pan: Optional[bool] = True  # 是否开启跨网盘互传


class TransferTaskResponse(BaseModel):
    """转存任务响应"""
    id: int
    source_url: str
    source_type: int
    target_account_id: int
    target_account_name: Optional[str] = None
    target_account_type: Optional[int] = None
    status: int
    status_name: str
    parent_task_id: Optional[int] = None
    chain_status: Optional[str] = None
    result_share_url: Optional[str] = None
    result_title: Optional[str] = None
    result_fid: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    cross_parent: Optional[dict] = None
    cross_tasks: Optional[list] = None
    children: Optional[List["TransferTaskResponse"]] = None
    
    class Config:
        from_attributes = True


class TransferPaginationResponse(BaseModel):
    """转存任务分页响应"""
    total: int
    items: List[TransferTaskResponse]


# 支持自引用
TransferTaskResponse.model_rebuild()

