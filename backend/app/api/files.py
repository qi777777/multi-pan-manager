from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List
import asyncio
import json

from ..database import get_db
from ..models.account import DiskAccount
from ..schemas.share import FileItem
from ..services.disk import get_disk_service
from ..utils.crypto import decrypt_credentials
from ..core.logger import logger

router = APIRouter()


@router.get("/{account_id}", response_model=List[FileItem])
async def get_files(account_id: int, pdir_fid: str = "0", db: Session = Depends(get_db)):
    """获取文件列表"""
    db_account = db.query(DiskAccount).filter(DiskAccount.id == account_id).first()
    if not db_account:
        raise HTTPException(status_code=404, detail="账户不存在")
    
    try:
        credentials = decrypt_credentials(db_account.credentials)
        # 合并持久化配置与运行时上下文
        config = json.loads(db_account.config or "{}")
        config["account_id"] = account_id
        service = get_disk_service(db_account.type, credentials, config)
        result = await service.get_files(pdir_fid)
        
        if result.get("code") != 200:
            raise HTTPException(status_code=400, detail=result.get("message", "获取文件列表失败"))
        
        return result.get("data", [])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[FILES] 获取文件列表失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取文件列表失败: {str(e)}")


@router.get("/{account_id}/search")
async def search_files(account_id: int, keyword: str, page: int = 1, size: int = 50, db: Session = Depends(get_db)):
    """全盘搜索文件"""
    if not keyword or not keyword.strip():
        raise HTTPException(status_code=400, detail="搜索关键词不能为空")
    
    db_account = db.query(DiskAccount).filter(DiskAccount.id == account_id).first()
    if not db_account:
        raise HTTPException(status_code=404, detail="账户不存在")
    
    try:
        credentials = decrypt_credentials(db_account.credentials)
        # 合并持久化配置与运行时上下文
        config = json.loads(db_account.config or "{}")
        config["account_id"] = account_id
        service = get_disk_service(db_account.type, credentials, config)
        
        # 检查服务是否支持搜索
        if not hasattr(service, 'search_files'):
            raise HTTPException(status_code=400, detail="该网盘类型暂不支持搜索功能")
        
        result = await service.search_files(keyword.strip(), page, size)
        
        if result.get("code") != 200:
            raise HTTPException(status_code=400, detail=result.get("message", "搜索失败"))
        
        return result.get("data", {"list": [], "total": 0})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜索失败: {str(e)}")


@router.delete("/{account_id}")
async def delete_files(account_id: int, fid_list: List[str], db: Session = Depends(get_db)):
    """删除文件"""
    db_account = db.query(DiskAccount).filter(DiskAccount.id == account_id).first()
    if not db_account:
        raise HTTPException(status_code=404, detail="账户不存在")
    
    try:
        credentials = decrypt_credentials(db_account.credentials)
        # 合并持久化配置与运行时上下文
        config = json.loads(db_account.config or "{}")
        config["account_id"] = account_id
        service = get_disk_service(db_account.type, credentials, config)
        result = await service.delete_files(fid_list)
        
        if result.get("code") != 200:
            status = 403 if result.get("code") == 403 else 400
            # 如果是403，返回完整数据供前端使用
            raise HTTPException(status_code=status, detail=result)
            
        return {"message": "删除成功", "result": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {str(e)}")


@router.post("/{account_id}/verify/send")
async def send_verification_code(account_id: int, form_data: dict, db: Session = Depends(get_db)):
    """发送验证码"""
    db_account = db.query(DiskAccount).filter(DiskAccount.id == account_id).first()
    if not db_account:
        raise HTTPException(status_code=404, detail="账户不存在")
    
    try:
        credentials = decrypt_credentials(db_account.credentials)
        # 合并持久化配置与运行时上下文
        config = json.loads(db_account.config or "{}")
        config["account_id"] = account_id
        service = get_disk_service(db_account.type, credentials, config)
        
        if not hasattr(service, 'send_verification_code'):
            raise HTTPException(status_code=400, detail="该服务不支持发送验证码")
            
        result = await service.send_verification_code(form_data)
        
        if result.get("code") != 200:
            raise HTTPException(status_code=400, detail=result.get("message"))
            
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"发送验证码失败: {str(e)}")


@router.post("/{account_id}/verify/check")
async def check_verification_code(account_id: int, form_data: dict, db: Session = Depends(get_db)):
    """校验验证码"""
    db_account = db.query(DiskAccount).filter(DiskAccount.id == account_id).first()
    if not db_account:
        raise HTTPException(status_code=404, detail="账户不存在")
    
    try:
        credentials = decrypt_credentials(db_account.credentials)
        # 合并持久化配置与运行时上下文
        config = json.loads(db_account.config or "{}")
        config["account_id"] = account_id
        service = get_disk_service(db_account.type, credentials, config)
        
        if not hasattr(service, 'check_verification_code'):
            raise HTTPException(status_code=400, detail="该服务不支持校验验证码")
            
        result = await service.check_verification_code(form_data)
        
        if result.get("code") != 200:
            raise HTTPException(status_code=400, detail=result.get("message"))
            
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"校验验证码失败: {str(e)}")
async def ensure_remote_path(service, relative_path: str, base_fid: str = "0"):
    """递归确保远程路径存在并返回 (最终 FID, 根目录 FID)"""
    if not relative_path or relative_path in (".", "/", "./"):
        return base_fid, base_fid
        
    # 处理前导斜杠
    parts = [p for p in relative_path.replace('\\', '/').split('/') if p]
    current_fid = base_fid
    root_fid = None
    
    for i, part in enumerate(parts):
        # 1. 获取当前下的文件列表
        list_res = await service.get_files(current_fid)
        found_fid = None
        if list_res.get("code") == 200:
            for item in list_res.get("data", []):
                if item.get("name") == part and item.get("is_dir"):
                    found_fid = item.get("fid")
                    break
        
        # 2. 如果没找到，创建它
        if not found_fid:
            create_res = await service.create_folder(part, current_fid)
            if create_res.get("code") == 200:
                found_fid = create_res["data"]["fid"]
            else:
                # 容错：如果是因为“已存在”报错，再查一次
                list_res = await service.get_files(current_fid)
                if list_res.get("code") == 200:
                    for item in list_res.get("data", []):
                        if item.get("name") == part and item.get("is_dir"):
                            found_fid = item.get("fid")
                            break
                
                if not found_fid:
                    raise Exception(f"无法进入或创建目录: {part} ({create_res.get('message')})")
        
        current_fid = found_fid
        if i == 0:
            root_fid = found_fid
            
    return current_fid, root_fid


@router.post("/mkdir")
async def create_folder(
    folder_name: str = Form(...),
    account_ids: str = Form(...),
    pdir_fid: str = Form("0"),
    db: Session = Depends(get_db)
):
    """在多个网盘中创建文件夹"""
    try:
        ids = [int(i) for i in account_ids.split(",") if i.strip()]
        accounts = db.query(DiskAccount).filter(DiskAccount.id.in_(ids)).all()
        
        results = []
        for account in accounts:
            try:
                creds = decrypt_credentials(account.credentials)
                config = json.loads(account.config or "{}")
                service = get_disk_service(account.type, creds, config)
                res = await service.create_folder(folder_name, pdir_fid)
                results.append({"account_id": account.id, "account_name": account.name, **res})
            except Exception as e:
                results.append({"account_id": account.id, "account_name": account.name, "code": 500, "message": str(e)})
        
        return {"code": 200, "data": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/upload")
async def upload_file(
    file: UploadFile,
    account_ids: str = Form(...),  # 逗号分隔的账户ID
    pdir_fid: str = Form("0"),
    target_dirs: str = Form(None), # JSON map: {accountId: pdir_fid}
    relative_path: str = Form(None), # 文件的相对路径（用于上传文件夹）
    task_id: str = Form(None),     # 前端生成的任务唯一识别码，用作 SSE 广播
    db: Session = Depends(get_db)
):
    """上传文件到多个网盘"""
    try:
        from ..services.broadcaster import task_broadcaster
        import json
        ids = [int(i) for i in account_ids.split(",") if i.strip()]
        if not ids:
            raise HTTPException(status_code=400, detail="未选择目标账户")
        
        accounts = db.query(DiskAccount).filter(DiskAccount.id.in_(ids)).all()
        if not accounts:
            raise HTTPException(status_code=404, detail="账户不存在")

        # Parse target directories map
        target_map = {}
        if target_dirs:
            try:
                target_map = json.loads(target_dirs)
            except:
                pass 
            
        results = []
        
        # 读取文件内容（注意：这里假设文件较小，大文件需优化）
        import os
        content = await file.read()
        filename = os.path.basename(file.filename)
        
        
        
        async def _single_upload(account, data):
            try:
                creds = decrypt_credentials(account.credentials)
                # 合并持久化配置与运行时上下文
                config = json.loads(account.config or "{}")
                config["account_id"] = account.id
                service = get_disk_service(account.type, creds, config)
                # 重新封装为类文件对象，因为不同服务可能需要 read()
                from io import BytesIO
                file_obj = BytesIO(data)
                
                # Determine target root directory
                base_fid = str(target_map.get(str(account.id), pdir_fid))
                
                # 如果有相对路径，先确保路径存在
                final_target_fid = base_fid
                root_folder_fid = None
                if relative_path:
                    # 获取该文件所在的目录路径（去掉文件名部分）
                    # 比如 relative_path 为 "folder/sub/file.txt"，则目录路径为 "folder/sub"
                    dir_path = "/".join(relative_path.replace('\\', '/').split('/')[:-1])
                    if dir_path:
                        final_target_fid, root_folder_fid = await ensure_remote_path(service, dir_path, base_fid)

                # 定义进度回调...
                async def progress_callback(current, total):
                    if task_id:
                        percent = int((current / total) * 100)
                        task_broadcaster.broadcast({
                            "type": "disk_upload_progress",
                            "task_id": task_id,
                            "account_id": account.id,
                            "progress": percent,
                            "filename": filename
                        })

                # [修复] 立即广播一次 0% 进度，告知前端本账户已进入二阶段（同步中）
                if task_id:
                    task_broadcaster.broadcast({
                        "type": "disk_upload_progress",
                        "task_id": task_id,
                        "account_id": account.id,
                        "progress": 0,
                        "filename": filename
                    })

                if hasattr(service, 'upload_file'):
                    import inspect
                    sig = inspect.signature(service.upload_file)
                    if 'progress_callback' in sig.parameters:
                        res = await service.upload_file(file_obj, filename, final_target_fid, progress_callback=progress_callback)
                    else:
                        res = await service.upload_file(file_obj, filename, final_target_fid)
                    logger.debug(f"[UPLOAD] {account.name} (FID:{final_target_fid}) 结果: {res}")
                    res["root_folder_fid"] = root_folder_fid
                    return res

                if hasattr(service, 'upload_to_path'):
                    # 只有在没有 upload_file 时才回退到路径上传
                    target_path = "/"
                    if hasattr(service, '_get_path_by_fid'):
                        path_res = await service._get_path_by_fid(target_fid)
                        target_path = path_res.get("path", "/") if "path" in path_res else "/"
                    return await service.upload_to_path(file_obj, filename, target_path, progress_callback=progress_callback)
                
                return {"code": 500, "message": f"{account.name}: 不支持上传方法"}
            except Exception as e:
                return {"code": 500, "message": f"{account.name}: {str(e)}"}
        
        #并发执行
        tasks = [_single_upload(acc, content) for acc in accounts]
        raw_results = await asyncio.gather(*tasks)
        
        # 将结果与账户ID绑定，便于前端解析
        upload_results = []
        for i, acc in enumerate(accounts):
            res = raw_results[i]
            res["account_id"] = acc.id
            upload_results.append(res)
        
        return {"code": 200, "message": "上传完成", "results": upload_results}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")


@router.get("/events/subscribe")
async def subscribe_events(request: Request):
    """订阅文件事件 (SSE)"""
    from ..services.broadcaster import task_broadcaster
    async def event_generator():
        # 立即发送握手消息
        yield f"data: {json.dumps({'type': 'connected'})}\n\n"
        
        async for queue in task_broadcaster.subscribe():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield f"data: {data}\n\n"
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[FILES] SSE 推送异常: {e}")
                break
            finally:
                break

    return StreamingResponse(
        event_generator(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
