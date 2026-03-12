from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class DiskAccountBase(BaseModel):
    """网盘账户基础模型"""
    name: str
    type: int  # 0=夸克,1=阿里,2=百度,3=UC,4=迅雷
    credentials: str
    storage_path: Optional[str] = None
    storage_path_temp: Optional[str] = None
    config: Optional[str] = "{}"


class DiskAccountCreate(DiskAccountBase):
    """创建账户请求模型"""
    pass


class DiskAccountUpdate(BaseModel):
    """更新账户请求模型"""
    name: Optional[str] = None
    credentials: Optional[str] = None
    storage_path: Optional[str] = None
    storage_path_temp: Optional[str] = None
    status: Optional[int] = None
    config: Optional[str] = None


class DiskAccountResponse(BaseModel):
    """账户响应模型"""
    id: int
    name: str
    type: int
    type_name: str
    storage_path: Optional[str]
    storage_path_temp: Optional[str]
    status: int
    last_check_at: Optional[datetime]
    created_at: datetime
    config: Optional[str]
    
    class Config:
        from_attributes = True
