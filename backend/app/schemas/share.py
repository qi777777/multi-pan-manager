from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List


class ShareCreate(BaseModel):
    """创建分享请求"""
    account_id: int
    fid_list: List[str]
    title: Optional[str] = None
    expired_type: int = 1  # 1=永久, 2=临时


class ShareResponse(BaseModel):
    """分享响应"""
    id: int
    account_id: int
    share_id: Optional[str]
    share_url: str
    title: Optional[str]
    password: Optional[str]
    status: int
    file_path: Optional[str] = ""
    expired_at: Optional[datetime]
    created_at: datetime
    
    class Config:
        from_attributes = True


class FileItem(BaseModel):
    """文件项"""
    fid: str
    name: str
    size: Optional[int] = 0
    is_dir: bool = False
    updated_at: Optional[datetime] = None
    class Config:
        from_attributes = True


class SharePaginationResponse(BaseModel):
    """分享分页响应"""
    total: int
    items: List[ShareResponse]


class BatchActionRequest(BaseModel):
    """批量操作请求"""
    ids: List[int]
    action: str  # cancel, delete_local
