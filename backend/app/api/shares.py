from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
import json
import re

from ..database import get_db
from ..core.logger import logger

from ..models.account import DiskAccount
from ..models.share import Share
from ..schemas.share import ShareCreate, ShareResponse, BatchActionRequest, SharePaginationResponse
from ..services.disk import get_disk_service
from ..utils.crypto import decrypt_credentials

router = APIRouter()


@router.get("", response_model=SharePaginationResponse)
async def get_shares(
    account_id: int = None, 
    title: str = None,
    status: int = None,
    expired_type: int = None,
    time_status: int = None,
    skip: int = 0, 
    limit: int = 50, 
    db: Session = Depends(get_db)
):
    """获取分享列表，支持多维度筛选"""
    from datetime import datetime
    query = db.query(Share)
    
    if account_id:
        query = query.filter(Share.account_id == account_id)
    
    if title:
        query = query.filter(Share.title.ilike(f"%{title}%"))
        
    if status is not None:
        query = query.filter(Share.status == status)
        
    if expired_type:
        if expired_type == 1:  # 永久
            from sqlalchemy import or_
            query = query.filter(or_(Share.expired_type == 1, Share.expired_at == None))
        else:
            query = query.filter(Share.expired_type == expired_type)

    if time_status is not None:
        now = datetime.now()
        if time_status == 1:  # 永久
            query = query.filter(Share.expired_at == None)
        elif time_status == 2:  # 未过期
            query = query.filter(Share.expired_at > now)
        elif time_status == 3:  # 已过期
            query = query.filter(Share.expired_at <= now)
            
    total = query.count()
    shares = query.order_by(Share.id.desc()).offset(skip).limit(limit).all()
    return {"total": total, "items": shares}


@router.post("", response_model=ShareResponse)
async def create_share(request: ShareCreate, db: Session = Depends(get_db)):
    """创建分享"""
    account = db.query(DiskAccount).filter(DiskAccount.id == request.account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")
    try:
        credentials = decrypt_credentials(account.credentials)
        config = json.loads(account.config or "{}")
        config["account_id"] = account.id
        service = get_disk_service(account.type, credentials, config)
        result = await service.create_share(
            fid_list=request.fid_list,
            title=request.title or "分享资源",
            expired_type=request.expired_type
        )
        if result.get("code") != 200:
            raise HTTPException(status_code=400, detail=result.get("message", "创建分享失败"))
        data = result.get("data", {})
        
        # 计算过期时间：1=永久, 2=7天, 3=1天, 4=30天
        from datetime import datetime, timedelta
        _days_map = {2: 7, 3: 1, 4: 30}
        _expired_days = _days_map.get(request.expired_type)
        expired_at = datetime.now() + timedelta(days=_expired_days) if _expired_days else None
        
        share = Share(
            account_id=request.account_id,
            share_id=data.get("share_id"),
            share_url=data.get("share_url", ""),
            title=data.get("title", request.title),
            password=data.get("password") or data.get("code") or data.get("share_pwd"),
            fid=json.dumps(request.fid_list),  # 正确使用 json.dumps 序列化
            expired_at=expired_at,
            expired_type=request.expired_type,
            status=1
        )
        db.add(share)
        db.commit()
        db.refresh(share)
        return share
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建分享失败: {str(e)}")


@router.delete("/{share_id}")
async def delete_share(share_id: int, db: Session = Depends(get_db)):
    """删除分享：先调用网盘 API 取消分享，再删除本地记录"""
    share = db.query(Share).filter(Share.id == share_id).first()
    if not share:
        raise HTTPException(status_code=404, detail="分享记录不存在")

    cancel_msg = ""
    # 优先用 DB 里的 share_id，没有时从 URL 解析
    effective_share_id = share.share_id
    if not effective_share_id and share.share_url:
        effective_share_id = _extract_pwd_id(share.share_url)
        logger.debug(f"[SHARE] 解析前 share_id 为空，已从 URL 中成功解析: {effective_share_id}")
    else:
        logger.debug(f"[SHARE] 正在使用已有的 share_id={effective_share_id} (URL={share.share_url})")

    if effective_share_id:
        try:
            account = db.query(DiskAccount).filter(DiskAccount.id == share.account_id).first()
            if not account:
                logger.warning(f"[SHARE] 找不到关联账号 (account_id={share.account_id})，跳过网盘侧取消操作")
                cancel_msg = "（找不到关联账号，已仅删除本地记录）"
            else:
                # Quark (0) 和 UC (3) 的 share_id 是 UUID 长度（32位字符），pwd_id 较短（通常12位）
                # 历史残余记录中把 pwd_id 存成了 effective_share_id，导致网盘 API 报 500 "inner error"
                if account.type in (0, 3) and len(effective_share_id) < 20:
                    logger.info(f"[SHARE] Quark/UC 的 effective_share_id({effective_share_id}) 判定为历史 pwd_id，跳过 API 调用")
                    cancel_msg = "（历史分享记录缺失真实 share_id，已仅删除本地记录）"
                else:
                    credentials = decrypt_credentials(account.credentials)
                    config = json.loads(account.config or "{}")
                    config["account_id"] = account.id
                    service = get_disk_service(account.type, credentials, config)
                    logger.info(f"[SHARE] 正在调用网盘接口取消分享: 账号={account.name}, type={account.type}, share_id={effective_share_id}")
                    if hasattr(service, "cancel_share"):
                        cancel_result = await service.cancel_share(effective_share_id)
                        logger.debug(f"[SHARE] 网盘取消接口返回: {cancel_result}")
                        if cancel_result.get("code") != 200:
                            cancel_msg = f"（网盘取消失败: {cancel_result.get('message', '')}，已仅删除本地记录）"
                    else:
                        logger.info(f"[SHARE] 驱动不支持 cancel_share 方法 (type={account.type})")
                        cancel_msg = "（该网盘暂不支持取消分享，已仅删除本地记录）"
        except Exception as e:
            logger.error(f"[SHARE] 取消分享异常: {e}", exc_info=True)
            cancel_msg = f"（调用网盘取消分享出错: {str(e)}，已仅删除本地记录）"
    else:
        logger.warning(f"[SHARE] 无法解析 share_id (URL={share.share_url})，忽略网盘取消操作")
        cancel_msg = "（无法解析 share_id，跳过网盘取消操作）"

    db.delete(share)
    db.commit()
    return {"message": f"删除成功{cancel_msg}"}


@router.delete("/{share_id}/file")
async def delete_share_with_file(share_id: int, db: Session = Depends(get_db)):
    """删除分享并连带删除网盘中的源文件"""
    share = db.query(Share).filter(Share.id == share_id).first()
    if not share:
        raise HTTPException(status_code=404, detail="分享记录不存在")

    account = db.query(DiskAccount).filter(DiskAccount.id == share.account_id).first()
    if not account:
        db.delete(share)
        db.commit()
        return {"message": "找不到关联账号，已仅删除本地记录"}

    credentials = decrypt_credentials(account.credentials)
    config = json.loads(account.config or "{}")
    config["account_id"] = account.id
    service = get_disk_service(account.type, credentials, config)
    
    cancel_msg = ""
    # 1. 尝试删除网盘中的真实源文件
    fid_list = []
    if share.fid:
        try:
            # 尝试标准 JSON 转换 (格式应为 ["file_id_1", "file_id_2"])
            fid_list = json.loads(share.fid)
        except:
            # 兼容非常早期的纯字符串或者错误存储
            fid_list = [share.fid]
            
        if not isinstance(fid_list, list):
            fid_list = [fid_list]
            
        # 兼容：如果旧数据仍是 "['xxx']" 这种嵌套字符串数组形式，尝试安全解开一层
        cleaned_list = []
        for item in fid_list:
            s_item = str(item)
            if s_item.startswith("['") and s_item.endswith("']"):
                try:
                    import ast
                    inner = ast.literal_eval(s_item)
                    if isinstance(inner, list):
                        cleaned_list.extend([str(x) for x in inner])
                        continue
                except:
                    pass
            cleaned_list.append(s_item)
        fid_list = cleaned_list

    if fid_list:
        logger.info(f"[SHARE] 正在连带删除网盘源文件 (FID列表: {fid_list})")
        try:
            del_result = await service.delete_files(fid_list)
        except Exception as e:
            logger.error(f"[SHARE] 删除源文件调用异常: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"删除源文件异常: {str(e)}")

        if del_result.get("code") == 403:
            # 百度网盘等风控拦截：中止删除，保留分享记录，将 403 原样透传给前端触发验证弹窗
            logger.warning(f"[SHARE] 删除操作触发网盘风控 (403)，保留本地记录以便后续验证。详情: {del_result}")
            raise HTTPException(status_code=403, detail=del_result)
        elif del_result.get("code") != 200:
            cancel_msg += f" (源文件删除失败: {del_result.get('message', '')})"
        else:
            cancel_msg += " (源文件删除成功)"
    else:
        cancel_msg += " (未找到文件ID，跳过源文件删除)"

    # 2. 验证无误或跳过后，从本地记录中移除分享
    db.delete(share)
    db.commit()
    return {"message": f"操作完成{cancel_msg}"}

def _extract_pwd_id(share_url: str) -> str:
    """从分享 URL 中提取 pwd_id / share token"""
    # 匹配 URL 结尾的 ID 部分，去掉 ?pwd=xxx 等参数
    url = share_url.split("?")[0].rstrip("/")
    return url.split("/")[-1]


async def _do_check_share(share: Share, db: Session) -> dict:
    """执行单个分享校验的具体逻辑"""
    url = share.share_url
    password = share.password or ""
    is_valid = False
    reason = "未知"

    try:
        account = db.query(DiskAccount).filter(DiskAccount.id == share.account_id).first()
        if not account:
            is_valid = False
            reason = "找不到关联账号"
        else:
            credentials = decrypt_credentials(account.credentials)
            config = json.loads(account.config or "{}")
            config["account_id"] = account.id
            service = get_disk_service(account.type, credentials, config)
            
            # 统一使用高精度检测接口 (validate_share)
            if hasattr(service, "validate_share"):
                res = await service.validate_share(url, password)
                if res.get("code") == 200 or res.get("status") == 200:
                    is_valid = True
                    reason = "链接有效 (经 API 深度检测)"
                else:
                    is_valid = False
                    reason = res.get("message") or "链接已失效 (API 检测失败)"
            
            # 以下为旧版模糊检测/特定逻辑退路 (针对尚未适配 validate_share 的服务或简单重定向检测)
            else:
                import httpx
                async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                    resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                    body = resp.text
                    # 通用失效关键词
                    invalid_kw = ["分享已失效", "不存在", "已过期", "expired", "invalid", "你访问的页面不存在"]
                    if any(k in body for k in invalid_kw) or resp.status_code in (404, 403):
                        is_valid = False
                        reason = "链接已失效 (模糊检测)"
                    else:
                        is_valid = True
                        reason = "链接可能有效 (模糊检测)"
    except Exception as e:
        is_valid = False
        reason = f"检测出错: {str(e)}"

    share.status = 1 if is_valid else 0
    return {"is_valid": is_valid, "reason": reason}


@router.post("/{share_id}/check")
async def check_share(share_id: int, db: Session = Depends(get_db)):
    """用各网盘专属 API 检测分享链接是否有效"""
    share = db.query(Share).filter(Share.id == share_id).first()
    if not share:
        raise HTTPException(status_code=404, detail="分享记录不存在")

    res = await _do_check_share(share, db)
    db.commit()
    return {**res, "share_id": share_id}


# ============================================================
# 批量分享接口
# ============================================================

from pydantic import BaseModel
from typing import Optional


class BatchShareItem(BaseModel):
    fid: str
    name: str


class BatchShareRequest(BaseModel):
    account_id: int
    items: List[BatchShareItem]         # 每个文件的 fid + name
    expired_type: int = 1               # 1=永久, 2=7天, 3=1天, 4=30天
    file_path_prefix: Optional[str] = None  # 可选：文件路径前缀，用于记录 file_path


@router.post("/batch-create")
async def batch_create_shares(request: BatchShareRequest, db: Session = Depends(get_db)):
    """批量为文件创建分享，每个文件独立生成一条分享记录"""
    account = db.query(DiskAccount).filter(DiskAccount.id == request.account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账户不存在")

    credentials = decrypt_credentials(account.credentials)
    config = json.loads(account.config or "{}")
    config["account_id"] = account.id
    service = get_disk_service(account.type, credentials, config)

    from datetime import datetime, timedelta
    _days_map = {2: 7, 3: 1, 4: 30}
    _expired_days = _days_map.get(request.expired_type)
    expired_at = datetime.now() + timedelta(days=_expired_days) if _expired_days else None

    results = []
    for item in request.items:
        try:
            logger.info(f"[BATCH_SHARE] 正在为 {item.name} 创建分享, account={account.name}, fid={item.fid}")
            result = await service.create_share(
                fid_list=[item.fid],
                title=item.name,
                expired_type=request.expired_type
            )
            if result.get("code") != 200:
                logger.error(f"[BATCH_SHARE] {item.name} 创建分享失败: {result.get('message')}")
                results.append({"name": item.name, "success": False, "message": result.get("message", "创建失败")})
                continue

            data = result.get("data", {})
            share_url = data.get("share_url", "")

            # 防止重复写入（若同一 URL 已存在则跳过）
            existing = db.query(Share).filter(Share.share_url == share_url).first() if share_url else None
            if not existing:
                file_path = f"{request.file_path_prefix.rstrip('/')}/{item.name}" if request.file_path_prefix else item.name
                new_share = Share(
                    account_id=request.account_id,
                    share_id=data.get("share_id", ""),
                    share_url=share_url,
                    title=item.name,
                    password=data.get("password") or data.get("code") or data.get("share_pwd") or "",
                    fid=json.dumps([item.fid]),
                    file_path=file_path,
                    expired_at=expired_at,
                    status=1
                )
                db.add(new_share)
                db.commit()

            results.append({
                "name": item.name,
                "success": True,
                "share_url": share_url,
                "password": data.get("password") or data.get("code") or data.get("share_pwd") or ""
            })
        except Exception as e:
            logger.error(f"[BATCH_SHARE] 异常 ({item.name}): {str(e)}")
            results.append({"name": item.name, "success": False, "message": str(e)})

    success_count = sum(1 for r in results if r.get("success"))
    return {
        "total": len(results),
        "success": success_count,
        "failed": len(results) - success_count,
        "results": results
    }


@router.post("/batch")
async def batch_action(request: BatchActionRequest, db: Session = Depends(get_db)):
    """批量操作分享：删除记录、取消分享或校验状态"""
    success_ids = []
    failed_ids = []
    valid_count = 0
    invalid_count = 0
    
    for share_id in request.ids:
        try:
            share = db.query(Share).filter(Share.id == share_id).first()
            if not share:
                failed_ids.append({"id": share_id, "error": "记录不存在"})
                continue
            
            if request.action == "cancel":
                # 仅云端取消分享逻辑
                effective_share_id = share.share_id
                if not effective_share_id and share.share_url:
                    effective_share_id = _extract_pwd_id(share.share_url)
                
                if effective_share_id:
                    account = db.query(DiskAccount).filter(DiskAccount.id == share.account_id).first()
                    if account:
                        if not (account.type in (0, 3) and len(effective_share_id) < 20):
                            credentials = decrypt_credentials(account.credentials)
                            config = json.loads(account.config or "{}")
                            config["account_id"] = account.id
                            service = get_disk_service(account.type, credentials, config)
                            if hasattr(service, "cancel_share"):
                                await service.cancel_share(effective_share_id)
                db.delete(share)
            elif request.action == "cancel_with_file":
                # 云端取消 + 删除源文件逻辑
                account = db.query(DiskAccount).filter(DiskAccount.id == share.account_id).first()
                if account:
                    credentials = decrypt_credentials(account.credentials)
                    config = json.loads(account.config or "{}")
                    config["account_id"] = account.id
                    service = get_disk_service(account.type, credentials, config)
                    
                    # 1. 取消分享
                    effective_share_id = share.share_id
                    if not effective_share_id and share.share_url:
                        effective_share_id = _extract_pwd_id(share.share_url)
                    
                    if effective_share_id and not (account.type in (0, 3) and len(effective_share_id) < 20):
                        if hasattr(service, "cancel_share"):
                            await service.cancel_share(effective_share_id)
                    
                    # 2. 删除源文件
                    if share.fid:
                        try:
                            fid_list = json.loads(share.fid)
                            if not isinstance(fid_list, list):
                                fid_list = [fid_list]
                            if fid_list:
                                await service.delete_files(fid_list)
                        except Exception as fe:
                            logger.error(f"[BATCH_CANCEL_WITH_FILE] 删除源文件失败: {fe}")
                db.delete(share)
            elif request.action == "check":
                # 批量校验
                res = await _do_check_share(share, db)
                if res.get("is_valid"):
                    valid_count += 1
                else:
                    invalid_count += 1
            
            success_ids.append(share_id)
        except Exception as e:
            failed_ids.append({"id": share_id, "error": str(e)})
            
    db.commit()
    return {
        "total": len(request.ids),
        "success": len(success_ids),
        "failed": len(failed_ids),
        "valid": valid_count,
        "invalid": invalid_count,
        "success_ids": success_ids,
        "failed_ids": failed_ids
    }
