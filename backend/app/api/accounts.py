from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
import json
from datetime import datetime

from ..database import get_db
from ..models.account import DiskAccount
from ..schemas.account import DiskAccountCreate, DiskAccountUpdate, DiskAccountResponse
from ..services.disk import get_disk_service
from ..utils.crypto import encrypt_credentials, decrypt_credentials

router = APIRouter()


@router.get("", response_model=List[DiskAccountResponse])
async def get_accounts(db: Session = Depends(get_db)):
    """获取所有账户列表"""
    accounts = db.query(DiskAccount).all()
    result = []
    for acc in accounts:
        result.append({
            "id": acc.id,
            "name": acc.name,
            "type": acc.type,
            "type_name": acc.type_name,
            "storage_path": acc.storage_path,
            "storage_path_temp": acc.storage_path_temp,
            "status": acc.status,
            "last_check_at": acc.last_check_at,
            "created_at": acc.created_at,
            "config": acc.config
        })
    return result


@router.post("", response_model=DiskAccountResponse)
async def create_account(account: DiskAccountCreate, db: Session = Depends(get_db)):
    """创建新账户"""
    db_account = DiskAccount(
        name=account.name,
        type=account.type,
        credentials=encrypt_credentials(account.credentials),
        storage_path=account.storage_path,
        storage_path_temp=account.storage_path_temp,
        status=1,
        config=account.config or "{}"
    )
    
    # 为阿里云盘添加默认扩展配置
    if db_account.type == 1:  # 阿里
        try:
            config_data = json.loads(db_account.config or "{}")
            if "drive_id" not in config_data:
                config_data["drive_id"] = "385389082"
                db_account.config = json.dumps(config_data)
        except:
            pass
            
    db.add(db_account)
    db.commit()
    db.refresh(db_account)
    return {
        "id": db_account.id,
        "name": db_account.name,
        "type": db_account.type,
        "type_name": db_account.type_name,
        "storage_path": db_account.storage_path,
        "storage_path_temp": db_account.storage_path_temp,
        "status": db_account.status,
        "last_check_at": db_account.last_check_at,
        "created_at": db_account.created_at,
        "config": db_account.config
    }


@router.get("/{account_id}")
async def get_account_detail(account_id: int, db: Session = Depends(get_db)):
    """获取单个账户详情（包含凭证，用于编辑）"""
    db_account = db.query(DiskAccount).filter(DiskAccount.id == account_id).first()
    if not db_account:
        raise HTTPException(status_code=404, detail="账户不存在")
    
    # 解密凭证返回
    try:
        credentials = decrypt_credentials(db_account.credentials) if db_account.credentials else ""
    except:
        credentials = ""
    
    return {
        "id": db_account.id,
        "name": db_account.name,
        "type": db_account.type,
        "type_name": db_account.type_name,
        "credentials": credentials,
        "storage_path": db_account.storage_path,
        "storage_path_temp": db_account.storage_path_temp,
        "status": db_account.status,
        "last_check_at": db_account.last_check_at,
        "created_at": db_account.created_at,
        "config": db_account.config
    }


@router.put("/{account_id}", response_model=DiskAccountResponse)
async def update_account(account_id: int, account: DiskAccountUpdate, db: Session = Depends(get_db)):
    """更新账户"""
    db_account = db.query(DiskAccount).filter(DiskAccount.id == account_id).first()
    if not db_account:
        raise HTTPException(status_code=404, detail="账户不存在")
    
    if account.name is not None:
        db_account.name = account.name
    if account.credentials is not None and account.credentials.strip() != "":
        db_account.credentials = encrypt_credentials(account.credentials)
    if account.storage_path is not None:
        db_account.storage_path = account.storage_path
    if account.storage_path_temp is not None:
        db_account.storage_path_temp = account.storage_path_temp
    if account.status is not None:
        db_account.status = account.status
    if account.config is not None:
        db_account.config = account.config
    
    db.commit()
    db.refresh(db_account)
    return {
        "id": db_account.id,
        "name": db_account.name,
        "type": db_account.type,
        "type_name": db_account.type_name,
        "storage_path": db_account.storage_path,
        "storage_path_temp": db_account.storage_path_temp,
        "status": db_account.status,
        "last_check_at": db_account.last_check_at,
        "created_at": db_account.created_at,
        "config": db_account.config
    }


@router.delete("/{account_id}")
async def delete_account(account_id: int, db: Session = Depends(get_db)):
    """删除账户"""
    db_account = db.query(DiskAccount).filter(DiskAccount.id == account_id).first()
    if not db_account:
        raise HTTPException(status_code=404, detail="账户不存在")
    
    db.delete(db_account)
    db.commit()
    return {"message": "删除成功"}


@router.post("/{account_id}/check")
async def check_account_status(account_id: int, db: Session = Depends(get_db)):
    """检测账户登录状态"""
    db_account = db.query(DiskAccount).filter(DiskAccount.id == account_id).first()
    if not db_account:
        raise HTTPException(status_code=404, detail="账户不存在")
    
    try:
        credentials = decrypt_credentials(db_account.credentials)
        # 合并持久化配置与运行时上下文
        config = json.loads(db_account.config or "{}")
        config["account_id"] = account_id
        service = get_disk_service(db_account.type, credentials, config)
        is_valid = await service.check_status()
        
        db_account.status = 1 if is_valid else 2
        db_account.last_check_at = datetime.now()
        db.commit()
        
        return {
            "status": db_account.status,
            "message": "登录状态正常" if is_valid else "凭证已过期，请更新"
        }
    except Exception as e:
        db_account.status = 2
        db_account.last_check_at = datetime.now()
        db.commit()
        return {"status": 2, "message": f"检测失败: {str(e)}"}
