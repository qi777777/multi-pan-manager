from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime
import re
import json

from ..database import get_db
from ..models.account import DiskAccount
from ..models.transfer import TransferTask
from ..models.share import Share
from ..schemas.transfer import TransferParseRequest, TransferParseResponse, TransferExecuteRequest, TransferTaskResponse, TransferPaginationResponse
from ..services.disk import get_disk_service
from ..utils.crypto import decrypt_credentials
from ..core.logger import logger

router = APIRouter()

# 链接识别模式
URL_PATTERNS = {
    'pan.quark.cn': 0,
    'www.alipan.com': 1,
    'www.aliyundrive.com': 1,
    'pan.baidu.com': 2,
    'drive.uc.cn': 3,
    'fast.uc.cn': 3,
    'pan.xunlei.com': 4,
}

DISK_NAMES = {
    0: "夸克网盘",
    1: "阿里云盘",
    2: "百度网盘",
    3: "UC网盘",
    4: "迅雷云盘"
}


def detect_disk_type(url: str) -> int:
    """识别链接对应的网盘类型"""
    for pattern, disk_type in URL_PATTERNS.items():
        if pattern in url:
            return disk_type
    return -1


@router.post("/parse", response_model=TransferParseResponse)
async def parse_share_link(request: TransferParseRequest):
    """解析分享链接"""
    url = request.url
    disk_type = detect_disk_type(url)
    
    if disk_type == -1:
        raise HTTPException(status_code=400, detail="无法识别的分享链接")
    
    # 这里只做链接验证，不需要账户
    # 可以用于预览链接信息
    return {
        "title": "",
        "share_url": url,
        "source_type": disk_type,
        "source_type_name": DISK_NAMES.get(disk_type, "未知"),
        "stoken": None
    }


async def execute_transfer_task(task_id: int):
    """后台执行转存任务 - 使用独立的数据库会话"""
    from ..database import SessionLocal
    db = SessionLocal()
    
    try:
        task = db.query(TransferTask).filter(TransferTask.id == task_id).first()
        if not task:
            return
        
        task.status = TransferTask.STATUS_RUNNING
        db.commit()
        
        account = db.query(DiskAccount).filter(DiskAccount.id == task.target_account_id).first()
        if not account:
            raise Exception("目标账户不存在")
        
        credentials = decrypt_credentials(account.credentials)
        # 合并持久化配置与运行时上下文
        config = json.loads(account.config or "{}")
        # need_share 等参数存储在 chain_status 字段中
        need_share = True
        expired_type = 1
        if task.chain_status and task.chain_status.startswith("need_share:"):
            parts = task.chain_status.split(",")
            need_share = parts[0].split(":")[1].lower() == "true"
            if len(parts) > 1 and parts[1].startswith("expired_type:"):
                try:
                    expired_type = int(parts[1].split(":")[1])
                except:
                    pass
        
        config.update({
            "storage_path": task.storage_path or "0",
            "storage_path_temp": account.storage_path_temp,
            "account_id": account.id
        })
        service = get_disk_service(account.type, credentials, config)
        
        result = await service.transfer(task.source_url, task.source_code or "", expired_type=expired_type, need_share=need_share)
        
        if result.get("code") == 200:
            data = result.get("data", {})
            task.status = TransferTask.STATUS_SUCCESS
            task.result_share_url = data.get("share_url", "")
            task.result_title = data.get("title", "")
            
            # 修复：data.get("fid") 通常是一个列表，如 ["file_id_1", "file_id_2"]
            # 不要直接 str()，以免变成 "['file_id_1']" 这种脏数据
            _fids = data.get("fid", "")
            if isinstance(_fids, list) and _fids:
                task.result_fid = str(_fids[0])
            else:
                task.result_fid = str(_fids)
                
            task.completed_at = datetime.now()
            
            # 存入到分享管理表中
            if task.result_share_url:
                try:
                    password = data.get("share_pwd") or data.get("password") or data.get("code") or ""
                    
                    # 计算过期时间
                    _expired_at = None
                    if expired_type in (2, 3, 4):
                        from datetime import timedelta
                        _days_map = {2: 7, 3: 1, 4: 30}
                        _expired_at = datetime.now() + timedelta(days=_days_map[expired_type])

                    # 防止多次点击重试带来的重复
                    existing = db.query(Share).filter(Share.share_url == task.result_share_url).first()
                    if not existing:
                        _file_path = f"{task.storage_path.rstrip('/')}/{task.result_title}" if task.storage_path else task.result_title
                        new_share = Share(
                            account_id=account.id,
                            share_id=data.get("share_id", ""),
                            share_url=task.result_share_url,
                            title=task.result_title,
                            password=password,
                            expired_at=_expired_at,
                            file_path=_file_path,
                            fid=json.dumps([task.result_fid]) if task.result_fid else ""
                        )
                        db.add(new_share)
                except Exception as e:
                    logger.error(f"[TRANSFER] 网关存储分享记录到系统失败: {e}", exc_info=True)
        else:
            task.status = TransferTask.STATUS_FAILED
            task.error_message = result.get("message", "转存失败")
            task.completed_at = datetime.now()
        
        db.commit()
    except Exception as e:
        task.status = TransferTask.STATUS_FAILED
        task.error_message = str(e)
        task.completed_at = datetime.now()
        db.commit()
    finally:
        db.close()


@router.post("/execute")
async def execute_transfer(
    request: TransferExecuteRequest, 
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """执行转存"""
    url = request.url
    disk_type = detect_disk_type(url)
    
    if disk_type == -1:
        raise HTTPException(status_code=400, detail="无法识别的分享链接")
    
    # 统一转换目标格式
    final_targets = []
    
    # 优先使用新字段 targets
    if request.targets:
        for t in request.targets:
            final_targets.append({"id": t.account_id, "path": t.path, "need_share": t.need_share if t.need_share is not None else True, "expired_type": getattr(t, "expired_type", 1)})
    # 其次使用 target_account_ids
    elif request.target_account_ids:
        # 使用统一的 storage_path
        path = request.storage_path or "0"
        for tid in request.target_account_ids:
            final_targets.append({"id": tid, "path": path, "need_share": True, "expired_type": 1})
            
    # 最后处理旧字段 target_account_id (如果未包含在上述列表中)
    if request.target_account_id:
        exists = False
        for t in final_targets:
            if t["id"] == request.target_account_id:
                exists = True
                break
        if not exists:
            final_targets.append({"id": request.target_account_id, "path": request.storage_path or "0", "need_share": True, "expired_type": 1})
            
    # 去重 (按ID)
    unique_targets = {}
    for t in final_targets:
        unique_targets[t["id"]] = t
    final_targets = list(unique_targets.values())
    
    if not final_targets:
        raise HTTPException(status_code=400, detail="请至少选择一个目标网盘")
    
    # 获取所有目标账户信息
    target_ids = [t["id"] for t in final_targets]
    target_accounts = db.query(DiskAccount).filter(DiskAccount.id.in_(target_ids)).all()
    if not target_accounts:
        raise HTTPException(status_code=404, detail="未找到有效的目标账户")
        
    # 寻找匹配的网关账户
    gateway_target = None
    for t in final_targets:
        acc = next((a for a in target_accounts if a.id == t["id"]), None)
        if acc and acc.type == disk_type:
            gateway_target = t
            break
            
    # 如果没有同类网盘
    if not gateway_target:
        raise HTTPException(status_code=400, detail=f"请至少选择一个 {DISK_NAMES.get(disk_type, '同类')} 网盘作为中转")
        
    # === 处理“不开启互传”模式 ===
    if not request.enable_cross_pan:
        # 在该模式下，我们只关心那些与 source_type 匹配的目标账号
        valid_direct_targets = []
        for t in final_targets:
            acc = next((a for a in target_accounts if a.id == t["id"]), None)
            if acc and acc.type == disk_type:
                valid_direct_targets.append(t)
        
        if not valid_direct_targets:
            raise HTTPException(status_code=400, detail=f"未找到与 {DISK_NAMES.get(disk_type)} 类型匹配的目标账号，且已关闭互传。")
            
        # 逐个启动转存任务（无需分发环节）
        first_task_id = None
        for t in valid_direct_targets:
            task = TransferTask(
                source_url=url,
                source_type=disk_type,
                source_code=request.code,
                target_account_id=t["id"],
                storage_path=t["path"],
                chain_status=f"need_share:{str(t.get('need_share', True)).lower()},expired_type:{t.get('expired_type', 1)}",
                status=TransferTask.STATUS_PENDING
            )
            db.add(task)
            db.flush()
            if not first_task_id:
                first_task_id = task.id
            background_tasks.add_task(execute_transfer_task, task.id)
            
        db.commit()
        return {"task_id": first_task_id, "message": f"已提交 {len(valid_direct_targets)} 个同盘转存任务（互传已禁用）"}

    # === 执行原有的网关+分发逻辑（互传模式） ===
    gateway_need_share = gateway_target.get("need_share", True)
    gateway_expired = gateway_target.get("expired_type", 1)
    task = TransferTask(
        source_url=url,
        source_type=disk_type,
        source_code=request.code,
        target_account_id=gateway_target["id"],
        storage_path=gateway_target["path"],
        chain_status=f"need_share:{str(gateway_need_share).lower()},expired_type:{gateway_expired}",
        status=TransferTask.STATUS_PENDING
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    
    # 计算需要后续分发的账号
    chain_targets = [t for t in final_targets if t["id"] != gateway_target["id"]]

    if chain_targets:
        # 有后续分发任务，启动链式处理
        background_tasks.add_task(execute_transfer_chain, task.id, chain_targets)
        return {"task_id": task.id, "message": f"已启动链式任务：保存到网关 + 分发给 {len(chain_targets)} 个目标"}
    else:
        # 仅保存分享
        background_tasks.add_task(execute_transfer_task, task.id)
        return {"task_id": task.id, "message": "转存任务已提交"}


async def execute_transfer_chain(gateway_task_id: int, targets: List[dict]):
    """执行链式转存：先保存分享到源盘，成功后调用互传逻辑分发到其他盘"""
    # 1. 执行第一阶段：保存分享
    await execute_transfer_task(gateway_task_id)
    
    # 2. 检查结果并启动第二阶段
    from ..database import SessionLocal
    from ..models.cross_transfer import CrossTransferTask
    from .cross_transfer import (
        execute_cross_transfer_task, 
        execute_folder_transfer_task, 
        execute_multi_target_folder_transfer
    )
    import ast
    import asyncio
    
    db = SessionLocal()
    try:
        gateway_task = db.query(TransferTask).filter(TransferTask.id == gateway_task_id).first()
        
        if not gateway_task or gateway_task.status != TransferTask.STATUS_SUCCESS:
            if gateway_task:
                gateway_task.chain_status = "源盘转转失败，链式任务终止"
                db.commit()
            logger.error(f"[CHAIN] 网关任务 {gateway_task_id} 失败，链式任务终止")
            return
            
        if not gateway_task.result_fid:
            gateway_task.chain_status = "未返回文件ID，无法分发"
            db.commit()
            logger.error(f"[CHAIN] 网关任务 {gateway_task_id} 未返回 fid，无法分发")
            return
            
        # 解析 fid
        try:
            if gateway_task.result_fid.startswith('[') and gateway_task.result_fid.endswith(']'):
                source_fids = ast.literal_eval(gateway_task.result_fid)
            else:
                source_fids = [gateway_task.result_fid]
            if not isinstance(source_fids, list):
                source_fids = [str(source_fids)]
        except:
            source_fids = [gateway_task.result_fid]
            
        source_account_id = gateway_task.target_account_id
        file_name = gateway_task.result_title or "未命名文件"
        
        # 3. 获取源账户服务，检测 fid 类型
        source_account = db.query(DiskAccount).filter(DiskAccount.id == source_account_id).first()
        if not source_account:
            gateway_task.chain_status = "源账户不存在，无法分发"
            db.commit()
            return
            
        source_creds = decrypt_credentials(source_account.credentials)
        source_config = json.loads(source_account.config or "{}")
        source_config.update({"account_id": source_account.id})
        source_service = get_disk_service(source_account.type, source_creds, source_config)
        
        # 检测 fid 类型（文件/文件夹）
        is_folder = False
        for fid in source_fids:
            info = await source_service.get_file_download_info(str(fid))
            if info.get("code") == 200 and info.get("data", {}).get("is_folder"):
                is_folder = True
                break
            elif info.get("code") == 200 and not info.get("data", {}).get("download_url"):
                # 没有下载链接也可能是文件夹
                is_folder = True
                break
            elif info.get("code") != 200:
                # 下载链接获取失败（网盘如夸克可能拒绝了对文件夹的操作，或是失效）
                # 退一步通过 get_files 尝试能否列出内容，借此证实是否为目录。
                files_test = await source_service.get_files(str(fid))
                if files_test.get("code") == 200:
                    is_folder = True
                    break
        
        # 4. 获取目标账户列表
        target_ids = [t["id"] for t in targets]
        target_accounts = db.query(DiskAccount).filter(DiskAccount.id.in_(target_ids)).all()
        
        # 构建目标路径映射
        target_paths = {}
        for t in targets:
            target_paths[str(t["id"])] = t.get("path", "/")
        
        # 5. 创建 transfer_tasks 子任务（用于前端展示）
        child_tasks = {}  # target_id -> child_task
        for target in targets:
            child_task = TransferTask(
                source_url=gateway_task.source_url,
                source_type=gateway_task.source_type,
                source_code=gateway_task.source_code,
                target_account_id=target["id"],
                storage_path=target.get("path", "/"),
                parent_task_id=gateway_task_id,
                chain_status="等待互传",
                status=TransferTask.STATUS_PENDING
            )
            db.add(child_task)
            db.flush()
            child_tasks[target["id"]] = child_task
        db.commit()
        
        # 6. 直接复用互传逻辑创建 cross_transfer 任务
        # 构建与 start_cross_transfer 完全相同的任务结构
        service_cache = {}
        def get_svc(acc):
            if acc.id not in service_cache:
                creds = decrypt_credentials(acc.credentials)
                config = json.loads(acc.config or "{}")
                config["account_id"] = acc.id
                service_cache[acc.id] = get_disk_service(acc.type, creds, config)
            return service_cache[acc.id]
        
        gateway_task.chain_status = f"源盘转存成功，正在分发到 {len(targets)} 个目标"
        db.commit()
        
        # 为每个 source_fid 创建互传任务
        for fid in source_fids:
            fid_str = str(fid)
            
            if is_folder:
                # === 文件夹传输：复用互传的文件夹逻辑 ===
                logger.info(f"[CHAIN] FID {fid_str} 判定为文件夹，开始调用互传模块进行分发")
                
                if len(target_ids) > 1:
                    # 多目标文件夹 → 三层结构 (master → per-target parent → per-file child)
                    # 递归列出文件
                    files_result = await source_service.list_folder_recursive(fid_str)
                    if files_result.get("code") != 200:
                        for ct in child_tasks.values():
                            ct.status = TransferTask.STATUS_FAILED
                            ct.error_message = "获取文件夹内容失败"
                            ct.chain_status = "互传失败"
                            ct.completed_at = datetime.now()
                        db.commit()
                        continue
                    
                    file_list = files_result.get("data", [])
                    if not file_list:
                        logger.info(f"[CHAIN] 文件夹 '{file_name}' 为空，仅在目标端同步创建目录结构")
                        for target_id in target_ids:
                            target_acc = next((a for a in target_accounts if a.id == target_id), None)
                            if target_acc:
                                target_svc = get_svc(target_acc)
                                base_target_path = target_paths.get(str(target_id), "/")
                                s_folder_name = target_svc.sanitize_path(file_name)
                                s_folder_target_path = f"{base_target_path.rstrip('/')}/{s_folder_name}"
                                
                                try:
                                    path_res = await target_svc.get_or_create_path(s_folder_target_path)
                                    if path_res.get("code") == 200:
                                        if target_id in child_tasks:
                                            child_tasks[target_id].status = TransferTask.STATUS_SUCCESS
                                            child_tasks[target_id].chain_status = "互传完成(空文件夹)"
                                        target_fid = str(path_res.get("data", {}).get("fid", ""))
                                        if target_fid:
                                            target_dict = next((t for t in targets if t["id"] == target_id), {})
                                            if target_dict.get("need_share", True):
                                                expired_type = target_dict.get("expired_type", 1)
                                                share_res = await target_svc.create_share([target_fid], file_name, expired_type=expired_type)
                                                if share_res.get("code") == 200:
                                                    if target_id in child_tasks:
                                                        share_data = share_res.get("data", {})
                                                        child_tasks[target_id].result_share_url = share_data.get("share_url", "")
                                                        password = share_data.get("share_pwd") or share_data.get("password") or share_data.get("code") or ""
                                                        # 计算过期时间
                                                        from datetime import datetime, timedelta
                                                        _days_map = {2: 7, 3: 1, 4: 30}
                                                        _expired_days = _days_map.get(expired_type)
                                                        _expired_at = datetime.now() + timedelta(days=_expired_days) if _expired_days else None

                                                        existing = db.query(Share).filter(Share.share_url == child_tasks[target_id].result_share_url).first()
                                                        if not existing:
                                                            new_share = Share(
                                                                account_id=target_id,
                                                                share_id=share_data.get("share_id", ""),
                                                                share_url=child_tasks[target_id].result_share_url,
                                                                title=file_name,
                                                                password=password,
                                                                file_path=s_folder_target_path,
                                                                expired_at=_expired_at,
                                                                fid=json.dumps([target_fid])
                                                            )
                                                            db.add(new_share)
                                                else:
                                                    logger.error(f"[CHAIN] 目标对象 {target_id} (空文件夹) 创建分享失败: {share_res}")
                                    else:
                                        if target_id in child_tasks:
                                            child_tasks[target_id].status = TransferTask.STATUS_FAILED
                                            child_tasks[target_id].error_message = f"空文件夹创建失败: {path_res.get('message')}"
                                except Exception as e:
                                    if target_id in child_tasks:
                                        child_tasks[target_id].status = TransferTask.STATUS_FAILED
                                        child_tasks[target_id].error_message = str(e)
                        db.commit()
                        continue
                    
                    logger.info(f"[CHAIN] 文件夹包含 {len(file_list)} 个待传输对象，正在构建跨盘传输任务队列")
                    
                    # 创建 master 任务
                    master_task = CrossTransferTask(
                        source_account_id=source_account_id,
                        source_fid=fid_str,
                        source_file_name=file_name,
                        target_account_id=target_ids[0],
                        target_path=target_paths.get(str(target_ids[0]), "/"),
                        status=CrossTransferTask.STATUS_RUNNING,
                        transfer_type=CrossTransferTask.TRANSFER_CHAIN,
                        is_folder=1,
                        is_master=1,
                        total_targets=len(target_ids),
                        completed_targets=0,
                        total_files=len(file_list),
                        completed_files=0,
                        current_step=f"多目标传输: {len(file_list)} 文件 → {len(target_ids)} 目标"
                    )
                    db.add(master_task)
                    db.commit()
                    db.refresh(master_task)
                    
                    parent_task_ids = []
                    pt_id_map = {}
                    for target_id in target_ids:
                        base_target_path = target_paths.get(str(target_id), "/")
                        target_acc = next((a for a in target_accounts if a.id == target_id), None)
                        if not target_acc:
                            continue
                        target_svc = get_svc(target_acc)
                        s_folder_name = target_svc.sanitize_path(file_name)
                        s_folder_target_path = f"{base_target_path.rstrip('/')}/{s_folder_name}"
                        
                        parent_task = CrossTransferTask(
                            source_account_id=source_account_id,
                            source_fid=fid_str,
                            source_file_name=file_name,
                            target_account_id=target_id,
                            target_path=target_svc.sanitize_path(base_target_path),
                            status=CrossTransferTask.STATUS_PENDING,
                            transfer_type=CrossTransferTask.TRANSFER_CHAIN,
                            is_folder=1,
                            total_files=len(file_list),
                            completed_files=0,
                            master_task_id=master_task.id,
                            current_step="等待开始"
                        )
                        db.add(parent_task)
                        db.commit()
                        db.refresh(parent_task)
                        parent_task_ids.append(parent_task.id)
                        pt_id_map[target_id] = parent_task.id
                        
                        # 更新 transfer_tasks 子任务关联
                        if target_id in child_tasks:
                            child_tasks[target_id].status = TransferTask.STATUS_RUNNING
                            child_tasks[target_id].chain_status = f"正在互传 {len(file_list)} 个文件..."
                            child_tasks[target_id].result_fid = f"ct_parent:{parent_task.id}"
                        
                        for file_info in file_list:
                            relative_path = file_info.get("relative_path", "")
                            s_relative_path = target_svc.sanitize_path(relative_path)
                            if file_info.get("is_dir"):
                                # 空目录：target_path 应包含目录自身名称（即目录将被创建在该路径下）
                                file_target_path = f"{s_folder_target_path}/{s_relative_path}".rstrip("/")
                            elif "/" in s_relative_path:
                                file_target_path = f"{s_folder_target_path}/{s_relative_path.rsplit('/', 1)[0]}"
                            else:
                                file_target_path = s_folder_target_path
                            
                            sfname = file_info.get("name") or file_info.get("file_name") or f"未知文件_{file_info.get('fid')}"
                            
                            child_ct = CrossTransferTask(
                                source_account_id=source_account_id,
                                source_fid=file_info.get("fid"),
                                source_file_name=sfname,
                                source_file_size=file_info.get("size", 0),
                                target_account_id=target_id,
                                target_path=file_target_path,
                                transfer_type=CrossTransferTask.TRANSFER_CHAIN,
                                status=CrossTransferTask.STATUS_FAILED if file_info.get("is_sensitive") else CrossTransferTask.STATUS_PENDING,
                                error_message="涉及违规/敏感内容，无法传输" if file_info.get("is_sensitive") else "",
                                current_step="违规资源(已屏蔽)" if file_info.get("is_sensitive") else "等待开始",
                                parent_task_id=parent_task.id,
                                master_task_id=master_task.id,
                                is_folder=1 if file_info.get("is_dir") else 0
                            )
                            db.add(child_ct)
                        db.commit()
                    
                    # 提取子任务ID映射，避免在异步轮询中跨 session 访问引发 DetachedInstanceError
                    child_task_ids_map = {tid: t.id for tid, t in child_tasks.items()}
                    
                    # 定义单目标网盘完全后的异步独立轮询 watcher，打破全体等待
                    async def watch_and_share(w_target_id: int, pt_id: int):
                        w_db = SessionLocal()
                        try:
                            # 每次轮询前 expire 所有缓存，强制从数据库读取最新状态
                            # （SQLAlchemy session 缓存问题：不 expire 则每次都拿到旧状态）
                            while True:
                                await asyncio.sleep(2)
                                w_db.expire_all()  # 关键：强制让 session 下次访问时从 DB 刷新
                                w_pt = w_db.query(CrossTransferTask).filter(CrossTransferTask.id == pt_id).first()
                                if not w_pt:
                                    logger.debug(f"[CHAIN] 分发监测器: pt_id={pt_id} 已移除，停止轮询")
                                    break
                                logger.debug(f"[CHAIN] 分发进度监测: target_id={w_target_id}, pt_id={pt_id}, 当前状态={w_pt.status_name}")
                                    
                                if w_pt.status in [CrossTransferTask.STATUS_SUCCESS, CrossTransferTask.STATUS_PARTIAL_SUCCESS, CrossTransferTask.STATUS_FAILED, CrossTransferTask.STATUS_CANCELLED]:
                                    cb_is_success = w_pt.status in [CrossTransferTask.STATUS_SUCCESS, CrossTransferTask.STATUS_PARTIAL_SUCCESS]
                                    
                                    c_task = w_db.query(TransferTask).filter(TransferTask.id == child_task_ids_map[w_target_id]).first()
                                    if not c_task:
                                        break
                                        
                                    if not cb_is_success:
                                        c_task.status = TransferTask.STATUS_FAILED
                                        c_task.chain_status = "互传失败"
                                        w_db.commit()
                                        break
                                        
                                    c_task.chain_status = "互传完成，正在生成分享..."
                                    w_db.commit()
                                    
                                    target_dict = next((t for t in targets if t["id"] == w_target_id), {})
                                    need_share = target_dict.get("need_share", True)
                                    if need_share:
                                        try:
                                            target_acc = next((a for a in target_accounts if a.id == w_target_id), None)
                                            if target_acc:
                                                target_svc = get_svc(target_acc)
                                                target_fid = None
                                                base_target_path = target_paths.get(str(w_target_id), "/")
                                                s_folder_name = target_svc.sanitize_path(file_name)
                                                s_folder_target_path = f"{base_target_path.rstrip('/')}/{s_folder_name}"
                                                
                                                path_res = await target_svc.get_or_create_path(s_folder_target_path)
                                                if path_res.get("code") == 200:
                                                    target_fid = str(path_res.get("data", {}).get("fid", ""))
                                                
                                                if target_fid:
                                                    expired_type = target_dict.get("expired_type", 1)
                                                    share_res = await target_svc.create_share([target_fid], file_name, expired_type=expired_type)
                                                    if share_res.get("code") == 200:
                                                        share_data = share_res.get("data", {})
                                                        c_task.result_share_url = share_data.get("share_url", "")
                                                        password = share_data.get("share_pwd") or share_data.get("password") or share_data.get("code") or ""
                                                        
                                                        # 计算过期时间
                                                        from datetime import datetime, timedelta
                                                        _days_map = {2: 7, 3: 1, 4: 30}
                                                        _expired_days = _days_map.get(expired_type)
                                                        _expired_at = datetime.now() + timedelta(days=_expired_days) if _expired_days else None
                                                        
                                                        existing = w_db.query(Share).filter(Share.share_url == c_task.result_share_url).first()
                                                        if not existing:
                                                            new_share = Share(
                                                                account_id=w_target_id,
                                                                share_id=share_data.get("share_id", ""),
                                                                share_url=c_task.result_share_url,
                                                                title=file_name,
                                                                password=password,
                                                                fid=json.dumps([target_fid]),
                                                                file_path=s_folder_target_path,
                                                                expired_at=_expired_at,
                                                                status=1
                                                            )
                                                            w_db.add(new_share)
                                                        c_task.chain_status = f"互传完成，分享链接已生成"
                                                        w_db.commit()
                                                    else:
                                                        logger.error(f"[CHAIN] 账号 {w_target_id} (多目标文件夹) create_share 失败: {share_res}")
                                                        c_task.chain_status = "互传完成，分享创建失败"
                                        except Exception as e:
                                            logger.error(f"[CHAIN] 生成分享链接失败(多目标文件夹): {e}", exc_info=True)
                                            c_task.chain_status = "互传完成（分享创建异常）"
                                    else:
                                        # 不需要分享
                                        c_task.chain_status = "互传完成"
                                        
                                    c_task.status = TransferTask.STATUS_SUCCESS
                                    w_db.commit()
                                    break
                        except Exception as e:
                            logger.error(f"[CHAIN] 分发进度轮询器 (Watcher) 异常: {e}", exc_info=True)
                        finally:
                            w_db.close()
                    
                    # 并发启动所有目标网盘的分享轮询监测协程
                    watchers = []
                    for tid in target_ids:
                        if tid in child_task_ids_map and tid in pt_id_map:
                            watchers.append(asyncio.create_task(watch_and_share(tid, pt_id_map[tid])))
                            
                    # 调用互传的多目标文件夹执行函数 (内部自己会挂起直到所有分发完毕)
                    await execute_multi_target_folder_transfer(master_task.id, parent_task_ids)
                    
                    # 确保所有的分享创建协程都执行完
                    if watchers:
                        await asyncio.gather(*watchers, return_exceptions=True)
                    
                else:
                    # 单目标文件夹 → 两层结构 (parent → per-file child)
                    target_id = target_ids[0]
                    target_acc = next((a for a in target_accounts if a.id == target_id), None)
                    if not target_acc:
                        continue
                    target_svc = get_svc(target_acc)
                    base_target_path = target_paths.get(str(target_id), "/")
                    s_folder_name = target_svc.sanitize_path(file_name)
                    s_folder_target_path = f"{base_target_path.rstrip('/')}/{s_folder_name}"
                    
                    files_result = await source_service.list_folder_recursive(fid_str)
                    if files_result.get("code") != 200:
                        if target_id in child_tasks:
                            child_tasks[target_id].status = TransferTask.STATUS_FAILED
                            child_tasks[target_id].error_message = "获取文件夹内容失败"
                            child_tasks[target_id].completed_at = datetime.now()
                        db.commit()
                        continue
                    
                    file_list = files_result.get("data", [])
                    if not file_list:
                        logger.info(f"[CHAIN] 文件夹 '{file_name}' 为空，执行单目标同步创建")
                        try:
                            path_res = await target_svc.get_or_create_path(s_folder_target_path)
                            if path_res.get("code") == 200:
                                if target_id in child_tasks:
                                    child_tasks[target_id].chain_status = "互传完成(空文件夹)，正在生成分享..."
                                target_fid = str(path_res.get("data", {}).get("fid", ""))
                                if target_fid:
                                    target_dict = next((t for t in targets if t["id"] == target_id), {})
                                    if target_dict.get("need_share", True):
                                        expired_type = target_dict.get("expired_type", 1)
                                        share_res = await target_svc.create_share([target_fid], file_name, expired_type=expired_type)
                                        if share_res.get("code") == 200:
                                            if target_id in child_tasks:
                                                share_data = share_res.get("data", {})
                                                child_tasks[target_id].result_share_url = share_data.get("share_url", "")
                                                # 计算过期时间
                                                from datetime import datetime, timedelta
                                                _days_map = {2: 7, 3: 1, 4: 30}
                                                _expired_days = _days_map.get(expired_type)
                                                _expired_at = datetime.now() + timedelta(days=_expired_days) if _expired_days else None

                                                # 录入分享管理系统
                                                new_share = Share(
                                                    account_id=target_id,
                                                    share_id=share_data.get("share_id", ""),
                                                    share_url=share_data.get("share_url", ""),
                                                    title=file_name,
                                                    password=share_data.get("share_pwd") or share_data.get("password") or share_data.get("code") or "",
                                                    file_path=s_folder_target_path,
                                                    expired_at=_expired_at,
                                                    fid=json.dumps([target_fid])
                                                )
                                                db.add(new_share)
                                            if target_id in child_tasks:
                                                child_tasks[target_id].chain_status = "互传完成(空文件夹)"
                                        else:
                                            logger.error(f"[CHAIN] 账号 {target_id} (单目标空文件夹) create_share 失败: {share_res}")
                                            if target_id in child_tasks:
                                                child_tasks[target_id].chain_status = "互传完成(空文件夹无分享)"
                                    else:
                                        if target_id in child_tasks:
                                            child_tasks[target_id].chain_status = "互传完成(空文件夹)"
                                            
                                # 核心修复：在分享流程彻走完之后，再将状态变更为 SUCCESS，让前端停止轮询
                                if target_id in child_tasks:
                                    child_tasks[target_id].status = TransferTask.STATUS_SUCCESS
                            else:
                                if target_id in child_tasks:
                                    child_tasks[target_id].status = TransferTask.STATUS_FAILED
                                    child_tasks[target_id].error_message = f"空文件夹创建失败: {path_res.get('message')}"
                        except Exception as e:
                            if target_id in child_tasks:
                                child_tasks[target_id].status = TransferTask.STATUS_FAILED
                                child_tasks[target_id].error_message = str(e)
                        db.commit()
                        continue
                    
                    parent_task = CrossTransferTask(
                        source_account_id=source_account_id,
                        source_fid=fid_str,
                        source_file_name=file_name,
                        target_account_id=target_id,
                        target_path=target_svc.sanitize_path(base_target_path),
                        status=CrossTransferTask.STATUS_RUNNING,
                        transfer_type=CrossTransferTask.TRANSFER_CHAIN,
                        is_folder=1,
                        total_files=len(file_list),
                        completed_files=0,
                        current_step=f"准备传输 {len(file_list)} 个文件"
                    )
                    db.add(parent_task)
                    db.commit()
                    db.refresh(parent_task)
                    
                    if target_id in child_tasks:
                        child_tasks[target_id].status = TransferTask.STATUS_RUNNING
                        child_tasks[target_id].chain_status = f"正在互传 {len(file_list)} 个文件..."
                        child_tasks[target_id].result_fid = f"ct_parent:{parent_task.id}"
                    
                    for file_info in file_list:
                        relative_path = file_info.get("relative_path", "")
                        s_relative_path = target_svc.sanitize_path(relative_path)
                        
                        is_dir = file_info.get("is_dir", False)
                        if is_dir:
                            # 文件夹：target_path 应包含目录自身名称（即目录将被创建在该路径下）
                            file_target_path = f"{s_folder_target_path}/{s_relative_path}".rstrip("/")
                        elif "/" in s_relative_path:
                            file_target_path = f"{s_folder_target_path}/{s_relative_path.rsplit('/', 1)[0]}"
                        else:
                            file_target_path = s_folder_target_path
                        
                        sfname = file_info.get("name") or file_info.get("file_name") or f"未知文件_{file_info.get('fid')}"
                        child_ct = CrossTransferTask(
                            source_account_id=source_account_id,
                            source_fid=file_info.get("fid"),
                            source_file_name=sfname,
                            source_file_size=file_info.get("size", 0),
                            target_account_id=target_id,
                            target_path=file_target_path,
                            transfer_type=CrossTransferTask.TRANSFER_CHAIN,
                            status=CrossTransferTask.STATUS_FAILED if file_info.get("is_sensitive") else CrossTransferTask.STATUS_PENDING,
                            error_message="涉及违规/敏感内容，无法传输" if file_info.get("is_sensitive") else "",
                            current_step="违规资源(已屏蔽)" if file_info.get("is_sensitive") else "等待开始",
                            parent_task_id=parent_task.id,
                            is_folder=1 if is_dir else 0
                        )
                        db.add(child_ct)
                    db.commit()
                    
                    await execute_folder_transfer_task(parent_task.id)
                    
                    # 互传完成后更新 TransferTask 的状态
                    db.refresh(parent_task)
                    is_parent_success = parent_task.status in [CrossTransferTask.STATUS_SUCCESS, CrossTransferTask.STATUS_PARTIAL_SUCCESS]
                    if target_id in child_tasks:
                        if is_parent_success:
                            child_tasks[target_id].chain_status = "互传完成，正在生成分享..."
                        else:
                            child_tasks[target_id].status = TransferTask.STATUS_FAILED
                            child_tasks[target_id].chain_status = "互传失败"
                    db.commit()
                    
                    # 互传完成后尝试生成分享链接
                    db.refresh(child_tasks[target_id])
                    if target_id in child_tasks and is_parent_success:
                        target_dict = next((t for t in targets if t["id"] == target_id), {})
                        need_share = target_dict.get("need_share", True)
                        expired_type = target_dict.get("expired_type", 1)
                        if need_share:
                            try:
                                target_fid = None
                                path_res = await target_svc.get_or_create_path(s_folder_target_path)
                                if path_res.get("code") == 200:
                                    target_fid = str(path_res.get("data", {}).get("fid", ""))
                                
                                if target_fid:
                                    share_res = await target_svc.create_share([target_fid], file_name, expired_type=expired_type)
                                    if share_res.get("code") == 200:
                                        child_tasks[target_id].result_share_url = share_res.get("data", {}).get("share_url", "")
                                        child_tasks[target_id].chain_status = "互传完成"
                                        
                                        # 计算过期时间
                                        from datetime import datetime, timedelta
                                        _days_map = {2: 7, 3: 1, 4: 30}
                                        _expired_days = _days_map.get(expired_type)
                                        _expired_at = datetime.now() + timedelta(days=_expired_days) if _expired_days else None
                                        
                                        # 录入分享管理系统
                                        new_share = Share(
                                            account_id=target_id,
                                            share_id=share_res.get("data", {}).get("share_id", ""),
                                            share_url=share_res.get("data", {}).get("share_url", ""),
                                            title=file_name,
                                            password=share_res.get("data", {}).get("share_pwd") or share_res.get("data", {}).get("password") or share_res.get("data", {}).get("code") or "",
                                            file_path=s_folder_target_path,
                                            expired_at=_expired_at,
                                            fid=json.dumps([target_fid])
                                        )
                                        db.add(new_share)
                                        db.commit()
                                    else:
                                        logger.error(f"[CHAIN] 账号 {target_id} (单目标文件夹) create_share 失败: {share_res}")
                                        child_tasks[target_id].chain_status = "互传完成(无分享)"
                            except Exception as e:
                                logger.error(f"[CHAIN] 文件夹分享创建异常: {e}", exc_info=True)
                                child_tasks[target_id].chain_status = "互传完成(分享异常)"
                        else:
                            child_tasks[target_id].chain_status = "互传完成"
                        
                        # 核心修复：在分享流程彻底走完后，再将状态变更为 SUCCESS，让前端停止轮询
                        child_tasks[target_id].status = TransferTask.STATUS_SUCCESS
                        db.commit()
            else:
                # === 普通文件传输 ===
                logger.info(f"[CHAIN] FID {fid_str} 判定为普通文件，正在启动分发循环")
                
                for target in targets:
                    target_id = target["id"]
                    target_path = target.get("path", "/")
                    
                    cross_task = CrossTransferTask(
                        source_account_id=source_account_id,
                        source_fid=fid_str,
                        source_file_name=file_name,
                        target_account_id=target_id,
                        target_path=target_path,
                        transfer_type=CrossTransferTask.TRANSFER_CHAIN,
                        status=CrossTransferTask.STATUS_PENDING
                    )
                    db.add(cross_task)
                    db.commit()
                    db.refresh(cross_task)
                    
                    if target_id in child_tasks:
                        child_tasks[target_id].status = TransferTask.STATUS_RUNNING
                        child_tasks[target_id].chain_status = "正在互传..."
                        child_tasks[target_id].result_fid = f"ct:{cross_task.id}"
                    db.commit()
                    
                    try:
                        await execute_cross_transfer_task(cross_task.id)
                        
                        w_db = SessionLocal()
                        for _ in range(1800):
                            ct = w_db.query(CrossTransferTask).get(cross_task.id)
                            if ct and ct.status not in [CrossTransferTask.STATUS_PENDING, CrossTransferTask.STATUS_RUNNING]:
                                cross_task.status = ct.status
                                cross_task.error_message = ct.error_message
                                break
                            await asyncio.sleep(2)
                        w_db.close()
                        
                        db.refresh(cross_task)
                        if cross_task.status == CrossTransferTask.STATUS_SUCCESS:
                            if target_id in child_tasks:
                                child_tasks[target_id].result_title = file_name
                                child_tasks[target_id].chain_status = "互传完成，正在生成分享..."
                                
                                # 生成分享链接
                                target_dict = next((t for t in targets if t["id"] == target_id), {})
                                need_share = target_dict.get("need_share", True)
                                expired_type = target_dict.get("expired_type", 1)
                                if need_share:
                                    try:
                                        target_acc = next((a for a in target_accounts if a.id == target_id), None)
                                        if target_acc:
                                            target_svc = get_svc(target_acc)
                                            target_path = target_dict.get("path", "/")
                                            # file path is target_path/file_name
                                            file_path = f"{target_path.rstrip('/')}/{target_svc.sanitize_path(file_name)}"
                                            path_res = await target_svc.get_or_create_path(file_path)
                                            if path_res.get("code") == 200:
                                                target_fid = str(path_res.get("data", {}).get("fid", ""))
                                                if target_fid:
                                                    share_res = await target_svc.create_share([target_fid], file_name, expired_type=expired_type)
                                                    if share_res.get("code") == 200:
                                                        child_tasks[target_id].result_share_url = share_res.get("data", {}).get("share_url", "")
                                                        child_tasks[target_id].chain_status = "互传完成"
                                                        
                                                        # 计算过期时间
                                                        from datetime import datetime, timedelta
                                                        _days_map = {2: 7, 3: 1, 4: 30}
                                                        _expired_days = _days_map.get(expired_type)
                                                        _expired_at = datetime.now() + timedelta(days=_expired_days) if _expired_days else None

                                                        # 录入分享管理系统
                                                        new_share = Share(
                                                            account_id=target_id,
                                                            share_id=share_res.get("data", {}).get("share_id", ""),
                                                            share_url=share_res.get("data", {}).get("share_url", ""),
                                                            title=file_name,
                                                            password=share_res.get("data", {}).get("share_pwd") or share_res.get("data", {}).get("password") or share_res.get("data", {}).get("code") or "",
                                                            file_path=file_path,
                                                            expired_at=_expired_at,
                                                            fid=json.dumps([target_fid])
                                                        )
                                                        db.add(new_share)
                                                        db.commit()
                                                    else:
                                                        logger.error(f"[CHAIN] 账号 {target_id} (单文件) create_share 失败: {share_res}")
                                                        child_tasks[target_id].chain_status = "互传完成(无分享)"
                                    except Exception as e:
                                        logger.error(f"[CHAIN] 文件分享创建异常: {e}", exc_info=True)
                                        child_tasks[target_id].chain_status = "互传完成(分享异常)"
                                else:
                                    child_tasks[target_id].chain_status = "互传完成"
                                
                                # 核心修复：在分享流程走完后再赋予最终成功标志
                                child_tasks[target_id].status = TransferTask.STATUS_SUCCESS
                                db.commit()
                        else:
                            if target_id in child_tasks:
                                child_tasks[target_id].status = TransferTask.STATUS_FAILED
                                child_tasks[target_id].error_message = cross_task.error_message or "执行失败"
                                child_tasks[target_id].chain_status = "互传失败"
                    except Exception as e:
                        if target_id in child_tasks:
                            child_tasks[target_id].status = TransferTask.STATUS_FAILED
                            child_tasks[target_id].error_message = str(e)
                            child_tasks[target_id].chain_status = "互传异常"
                    
                    if target_id in child_tasks:
                        child_tasks[target_id].completed_at = datetime.now()
                    db.commit()
        
        # 7. 链式任务投递完成
        for target_id, child_task in child_tasks.items():
            db.refresh(child_task)
            if child_task.status not in [TransferTask.STATUS_SUCCESS, TransferTask.STATUS_FAILED]:
                child_task.chain_status = "互传处理中..."
        db.commit()
        
        # 8. 更新主任务状态
        gateway_task.status = TransferTask.STATUS_SUCCESS
        gateway_task.chain_status = "链式转存及分享链路执行完毕"
        db.commit()
        
        logger.info(f"[CHAIN] 链式分发任务处理流程已全部终结")
        
    except Exception as e:
        logger.error(f"[CHAIN] 链式任务全局执行异常: {e}", exc_info=True)
    finally:
        db.close()



@router.get("/tasks", response_model=TransferPaginationResponse)
async def get_tasks(skip: int = 0, limit: int = 20, db: Session = Depends(get_db)):
    """获取转存任务列表（树形结构：主任务+子任务）"""
    # 基础查询
    query = db.query(TransferTask).filter(
        TransferTask.parent_task_id == None
    )
    
    # 获取总数
    total = query.count()
    
    # 只查询主任务（没有父任务的）并分页
    tasks = query.order_by(TransferTask.id.desc()).offset(skip).limit(limit).all()
    
    result = []
    for task in tasks:
        task_data = _build_task_response(task, db)
        # 查询子任务
        child_tasks = db.query(TransferTask).filter(
            TransferTask.parent_task_id == task.id
        ).order_by(TransferTask.id.asc()).all()
        
        if child_tasks:
            task_data["children"] = [_build_task_response(ct, db, include_cross_tasks=True) for ct in child_tasks]
        
        result.append(task_data)
    return {"total": total, "items": result}


def _build_task_response(task, db, include_cross_tasks=False):
    """构建单个任务的响应数据"""
    # 获取目标账户信息
    account = db.query(DiskAccount).filter(DiskAccount.id == task.target_account_id).first()
    data = {
        "id": task.id,
        "source_url": task.source_url,
        "source_type": task.source_type,
        "target_account_id": task.target_account_id,
        "target_account_name": account.name if account else None,
        "target_account_type": account.type if account else None,
        "status": task.status,
        "status_name": task.status_name,
        "parent_task_id": task.parent_task_id,
        "chain_status": task.chain_status,
        "result_share_url": task.result_share_url,
        "result_title": task.result_title,
        "result_fid": task.result_fid,
        "error_message": task.error_message,
        "created_at": task.created_at,
        "completed_at": task.completed_at
    }
    
    # 如果是链式子任务，查询关联的 cross_transfer 任务详情
    if include_cross_tasks and task.result_fid:
        from ..models.cross_transfer import CrossTransferTask
        cross_tasks_data = []
        
        if task.result_fid.startswith("ct_parent:"):
            # 文件夹场景：result_fid = "ct_parent:{parent_task_id}"
            try:
                ct_parent_id = int(task.result_fid.split(":")[1])
                # 获取父任务信息
                ct_parent = db.query(CrossTransferTask).filter(CrossTransferTask.id == ct_parent_id).first()
                if ct_parent:
                    data["cross_parent"] = {
                        "id": ct_parent.id,
                        "status": ct_parent.status,
                        "status_name": ct_parent.STATUS_MAP.get(ct_parent.status, "未知"),
                        "total_files": ct_parent.total_files,
                        "completed_files": ct_parent.completed_files,
                        "current_step": ct_parent.current_step,
                        "is_folder": 1
                    }
                    # 获取子文件任务
                    ct_children = db.query(CrossTransferTask).filter(
                        CrossTransferTask.parent_task_id == ct_parent_id
                    ).order_by(CrossTransferTask.id.asc()).all()
                    for ct in ct_children:
                        cross_tasks_data.append({
                            "id": ct.id,
                            "source_file_name": ct.source_file_name,
                            "source_file_size": ct.source_file_size,
                            "target_path": ct.target_path,
                            "status": ct.status,
                            "status_name": ct.STATUS_MAP.get(ct.status, "未知"),
                            "progress": ct.progress,
                            "current_step": ct.current_step,
                            "error_message": ct.error_message,
                            "completed_at": ct.completed_at.isoformat() if ct.completed_at else None
                        })
            except (ValueError, IndexError):
                pass
        
        elif task.result_fid.startswith("ct:"):
            # 单文件场景：result_fid = "ct:{cross_task_id}"
            try:
                ct_id = int(task.result_fid.split(":")[1])
                ct = db.query(CrossTransferTask).filter(CrossTransferTask.id == ct_id).first()
                if ct:
                    cross_tasks_data.append({
                        "id": ct.id,
                        "source_file_name": ct.source_file_name,
                        "source_file_size": ct.source_file_size,
                        "target_path": ct.target_path,
                        "status": ct.status,
                        "status_name": ct.STATUS_MAP.get(ct.status, "未知"),
                        "progress": ct.progress,
                        "current_step": ct.current_step,
                        "error_message": ct.error_message,
                        "completed_at": ct.completed_at.isoformat() if ct.completed_at else None
                    })
            except (ValueError, IndexError):
                pass
        
        if cross_tasks_data:
            data["cross_tasks"] = cross_tasks_data
    
    return data


@router.get("/tasks/{task_id}", response_model=TransferTaskResponse)
async def get_task(task_id: int, db: Session = Depends(get_db)):
    """获取任务详情"""
    task = db.query(TransferTask).filter(TransferTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    task_data = _build_task_response(task, db)
    
    # 查询子任务
    child_tasks = db.query(TransferTask).filter(
        TransferTask.parent_task_id == task.id
    ).order_by(TransferTask.id.asc()).all()
    
    if child_tasks:
        task_data["children"] = [_build_task_response(ct, db, include_cross_tasks=True) for ct in child_tasks]
    
    return task_data

