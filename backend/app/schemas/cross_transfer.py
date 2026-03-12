from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime

class CrossTransferRequest(BaseModel):
    source_account_id: int
    source_fid: str
    source_file_name: Optional[str] = None
    target_account_id: Optional[int] = None  # 单目标（保持兼容）
    target_account_ids: Optional[List[int]] = None  # 多目标
    target_path: Optional[str] = "/"  # 单目标路径（保持兼容）
    target_paths: Optional[Dict[str, str]] = None  # 多目标独立路径 {"account_id": "path"}
    is_folder: bool = False  # 是否为文件夹传输
    source_file_size: Optional[int] = 0  # 源文件大小

class CrossTransferResponse(BaseModel):
    task_id: Optional[int] = None  # 单任务（保持兼容）
    task_ids: Optional[List[int]] = None  # 多任务
    message: str

class CrossTransferTaskSchema(BaseModel):
    id: int
    source_account_id: int
    source_fid: str
    source_file_name: Optional[str]
    source_file_size: Optional[int]
    target_account_id: int
    target_path: Optional[str]
    status: int
    status_name: str
    transfer_type: int
    result_path: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]
    
    # 新增：源账户和目标账户信息
    source_account_type: Optional[int] = None
    source_account_name: Optional[str] = None
    target_account_type: Optional[int] = None
    target_account_name: Optional[str] = None
    
    # 新增：进度信息
    progress: Optional[int] = 0
    current_step: Optional[str] = None
    
    # 新增：文件夹传输信息
    is_folder: Optional[int] = 0
    total_files: Optional[int] = 0
    completed_files: Optional[int] = 0
    parent_task_id: Optional[int] = None
    
    # 新增：三层结构支持（多目标文件夹）
    is_master: Optional[int] = 0
    master_task_id: Optional[int] = None
    total_targets: Optional[int] = 0
    completed_targets: Optional[int] = 0
    
    # 子任务列表（用于折叠显示）
    children: Optional[List["CrossTransferTaskSchema"]] = None
    
    # 目标任务列表（三层结构：主任务下的目标父任务）
    target_tasks: Optional[List["CrossTransferTaskSchema"]] = None


    class Config:
        from_attributes = True

class TaskPaginationResponse(BaseModel):
    total: int
    items: List[CrossTransferTaskSchema]

# 支持自引用
CrossTransferTaskSchema.model_rebuild()
