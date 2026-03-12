from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List, Optional, Dict
import asyncio
import time
import json
from datetime import datetime

from ..database import get_db
from ..models.account import DiskAccount
from ..models.cross_transfer import CrossTransferTask
from ..schemas.cross_transfer import CrossTransferRequest, CrossTransferResponse, CrossTransferTaskSchema, TaskPaginationResponse
from ..services.disk import get_disk_service
from ..utils.crypto import decrypt_credentials
from ..services.broadcaster import task_broadcaster
from ..core.logger import logger

from fastapi.concurrency import run_in_threadpool
router = APIRouter()

# 全局并发控制
# 父任务（顶级文件夹或主任务）并发限制
PARENT_SEMAPHORE = asyncio.Semaphore(3)
# 子文件并发限制 (跨任务共享)
FILE_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(5)


# 用于序列化数据库更新的锁（按任务 ID）
_progress_locks = {}
# 用于进度节流
_last_update_time = {}
# 全局任务取消事件 Map {task_id: asyncio.Event}
TASK_CANCEL_EVENTS = {}

# 【新增】内存进度缓存 (Hot Progress Cache)，解决数据库节流带来的手动刷新回滚问题
# 格式: {task_id: {"progress": int, "current_step": str, "timestamp": float}}
HOT_PROGRESS_CACHE = {}

# 【新增】跨网盘并发路径解析锁及缓存，防止同一目录的瞬时并发建引发 API 阻塞 (429 TooManyRequests) / 冲突重复创建
global_path_cache = {}  # {(account_id, target_path): fid}
global_path_locks = {}  # {(account_id, target_path): asyncio.Lock()}

async def get_or_create_path_with_cache(target_service, account_id: int, target_path: str) -> dict:
    """带缓存的并发安全路径解析引擎 (防止重复并发请求相同目录)"""
    if not target_path or not target_path.strip('/'):
        return {"code": 200, "message": "根目录", "data": {"fid": "0"}}
        
    cache_key = (account_id, target_path)
    if cache_key not in global_path_locks:
        global_path_locks[cache_key] = asyncio.Lock()
        
    async with global_path_locks[cache_key]:
        if cache_key in global_path_cache:
            return {"code": 200, "message": "命中缓存", "data": {"fid": global_path_cache[cache_key]}}
            
        if hasattr(target_service, 'get_or_create_path'):
            logger.info(f"[TRANSFER] 获取或创建路径 (Cache Miss): account={account_id}, path={target_path}")
            path_res = await target_service.get_or_create_path(target_path)
            if path_res.get("code") == 200:
                fid = path_res.get("data", {}).get("fid", "0")
                global_path_cache[cache_key] = str(fid)
                return path_res
            else:
                return path_res
        return {"code": 500, "message": "目标服务不支持快速创建目录"}


async def update_progress(db: Session, task: CrossTransferTask, progress: int, step: str):
    """更新任务进度（带节流、锁保护和冲突抑制）"""
    # 1. 简单过滤：如果内容完全没变，直接返回
    if task.progress == progress and task.current_step == step:
        return

    # 2. 时间节流：上传/下载过程中，每 2 秒更新一次数据库，除非是关键节点(0/100/错误)
    now = time.time()
    is_critical = progress == 0 or progress == 100 or "失败" in step or "成功" in step or "秒传" in step
    last_time = _last_update_time.get(task.id, 0)
    
    if not is_critical and (now - last_time < 2.0):
        # 虽然不写 DB，但还是要广播到前端
        task.progress = progress
        task.current_step = step
        # 【新增】更新内存缓存，确保护刷新不倒退
        HOT_PROGRESS_CACHE[task.id] = {
            "progress": progress,
            "current_step": step,
            "status": task.status,
            "status_name": task.status_name,
            "timestamp": now
        }
        task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "progress": progress, "current_step": step, "status": task.status, "status_name": task.status_name})
        return

    # 3. 更新内存缓存 (无节流，总是最新)
    HOT_PROGRESS_CACHE[task.id] = {
        "progress": progress,
        "current_step": step,
        "status": task.status,
        "status_name": task.status_name,
        "timestamp": now
    }

    # 4. 加锁提交
    lock = _progress_locks.get(task.id)
    if not lock:
        lock = asyncio.Lock()
        _progress_locks[task.id] = lock
        
    async with lock:
        try:
            task.progress = progress
            task.current_step = step
            _last_update_time[task.id] = now
            await run_in_threadpool(db.commit)
            
            # [Fix] Trigger parent status recalculation if this is a child task
            # Only do this after DB commit so recalculate_parent_status sees the new progress
            if task.parent_task_id:
                # Avoid circular import or undefined reference execution time
                # Using asyncio.create_task to run in background
                asyncio.create_task(recalculate_parent_status(task.parent_task_id))
                
        except Exception as e:
            # ...
            pass
            
    task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "progress": progress, "current_step": step, "status": task.status, "status_name": task.status_name})


async def recalculate_master_status(master_task_id: int, db: Session = None):
    """重新计算主任务状态（针对三层结构）"""
    # 如果没有传入 db session，则创建新的
    if db is None:
        from ..database import SessionLocal
        db = SessionLocal()
        close_db = True
    else:
        close_db = False

    try:
        master_task = db.query(CrossTransferTask).filter(CrossTransferTask.id == master_task_id).first()
        if not master_task:
            return
            
        # 查询所有属于该主任务的目标父任务状态
        target_tasks = db.query(CrossTransferTask).filter(
            CrossTransferTask.master_task_id == master_task_id,
            CrossTransferTask.parent_task_id.is_(None),  # 排除子文件任务
            CrossTransferTask.is_master == 0             # 排除主任务自身
        ).all()
        
        if not target_tasks:
            return
            
        total = len(target_tasks)
        success_count = sum(1 for t in target_tasks if t.status == CrossTransferTask.STATUS_SUCCESS)
        failed_count = sum(1 for t in target_tasks if t.status == CrossTransferTask.STATUS_FAILED)
        partial_count = sum(1 for t in target_tasks if t.status == CrossTransferTask.STATUS_PARTIAL_SUCCESS)
        cancelled_count = sum(1 for t in target_tasks if t.status == CrossTransferTask.STATUS_CANCELLED)
        
        done_count = success_count + failed_count + partial_count + cancelled_count
        pending_count = total - done_count
        
        # 更新进度相关计数
        master_task.completed_targets = done_count
        
        # 计算主文件层级的统计项
        total_success_files = sum(t.completed_files for t in target_tasks)
        master_task.completed_files = total_success_files
        
        # 如果还有目标在跑，持续显示运行中
        if pending_count > 0:
            # [恢复] 核心进度计算：主任务进度 = 所有目标父任务进度的平均值
            avg_progress = sum(t.progress for t in target_tasks) / total
            master_task.progress = int(avg_progress)

            # [修复] 提前捕获属性，防止 commit 后访问触发延迟加载
            status_id = master_task.status
            status_name = master_task.status_name
            progress = master_task.progress
            step = master_task.current_step
            comp_targets = master_task.completed_targets
            comp_files = master_task.completed_files

            await run_in_threadpool(db.commit)
            task_broadcaster.broadcast({
                "type": "task_updated", 
                "task_id": master_task.id, 
                "status": status_id, 
                "status_name": status_name, 
                "progress": progress, 
                "current_step": step, 
                "completed_targets": comp_targets, 
                "completed_files": comp_files
            })
            return
            
        # 全部主流程目标跑完了，根据整体情况结算状态
        master_task.progress = 100
        master_task.completed_at = datetime.now()
        
        if failed_count == 0 and partial_count == 0 and cancelled_count == 0:
            master_task.status = CrossTransferTask.STATUS_SUCCESS
            master_task.transfer_type = CrossTransferTask.TRANSFER_STREAM
            master_task.current_step = f"全部完成: {total} 个网盘目标成功"
        elif success_count > 0 or partial_count > 0:
            master_task.status = CrossTransferTask.STATUS_PARTIAL_SUCCESS
            msg = f"部分完成: 成功 {success_count}"
            if failed_count > 0: msg += f", 失败 {failed_count}"
            if cancelled_count > 0: msg += f", 取消 {cancelled_count}"
            master_task.current_step = msg
        elif failed_count > 0:
            # 重要：失败权重高于取消，确保展示最严重的执行异常
            master_task.status = CrossTransferTask.STATUS_FAILED
            master_task.current_step = f"全部失败: {failed_count} 个网盘目标均失败"
        elif cancelled_count > 0:
            master_task.status = CrossTransferTask.STATUS_CANCELLED
            master_task.current_step = f"已取消: {cancelled_count} 个目标被取消"
        else:
            master_task.status = CrossTransferTask.STATUS_SUCCESS
            master_task.transfer_type = CrossTransferTask.TRANSFER_STREAM
            master_task.current_step = "完成"
        
        # [修复] 在 commit 之前捕获所有必要属性，防止 commit 后访问触发延迟加载请求数据库连接
        status_id = master_task.status
        status_name = master_task.status_name
        progress = master_task.progress
        step = master_task.current_step
        err_msg = master_task.error_message
        comp_targets = master_task.completed_targets
        comp_files = master_task.completed_files

        await run_in_threadpool(db.commit)
        logger.debug(f"主任务 {master_task_id} 状态已更新: {step}")
        task_broadcaster.broadcast({
            "type": "task_updated", 
            "task_id": master_task.id, 
            "status": status_id, 
            "status_name": status_name, 
            "progress": progress, 
            "current_step": step, 
            "error_message": err_msg,
            "completed_targets": comp_targets, 
            "completed_files": comp_files
        })
    finally:
        if close_db:
            db.close()

async def recalculate_parent_status(parent_task_id: int, db: Session = None):
    """重新计算父任务状态（根据所有子任务的当前状态）"""
    # 如果没有传入 db session，则创建新的
    if db is None:
        from ..database import SessionLocal
        db = SessionLocal()
        close_db = True
    else:
        close_db = False

    try:
        parent_task = db.query(CrossTransferTask).filter(CrossTransferTask.id == parent_task_id).first()
        if not parent_task:
            return
        
        # 查询所有子任务状态
        child_tasks = db.query(CrossTransferTask).filter(
            CrossTransferTask.parent_task_id == parent_task_id
        ).all()
        
        if not child_tasks:
            return
        
        total = len(child_tasks)
        success_count = sum(1 for t in child_tasks if t.status == CrossTransferTask.STATUS_SUCCESS)
        failed_count = sum(1 for t in child_tasks if t.status == CrossTransferTask.STATUS_FAILED)
        cancelled_count = sum(1 for t in child_tasks if t.status == CrossTransferTask.STATUS_CANCELLED)
        pending_count = total - success_count - failed_count - cancelled_count
        
        # 更新中间计数
        parent_task.completed_files = success_count
        
        if pending_count > 0:
            # [恢复] 核心进度计算：父任务进度 = 所有子文件任务进度的平均值
            avg_progress = sum(t.progress for t in child_tasks) / total
            parent_task.progress = int(avg_progress)

            # [修复] 提前捕获属性
            status_id = parent_task.status
            status_name = parent_task.status_name
            progress = parent_task.progress
            step = parent_task.current_step
            comp_files = parent_task.completed_files

            await run_in_threadpool(db.commit)
            task_broadcaster.broadcast({
                "type": "task_updated", 
                "task_id": parent_task.id, 
                "status": status_id, 
                "status_name": status_name, 
                "progress": progress, 
                "current_step": step, 
                "completed_files": comp_files
            })
            
            # 如果存在 master_task，同步同步主任务进度
            if parent_task.master_task_id:
                await recalculate_master_status(parent_task.master_task_id, db)
            return
        
        # 结算本层级父任务状态
        parent_task.progress = 100
        parent_task.completed_at = datetime.now()

        if failed_count == 0 and cancelled_count == 0:
            parent_task.status = CrossTransferTask.STATUS_SUCCESS
            parent_task.transfer_type = CrossTransferTask.TRANSFER_STREAM
            parent_task.current_step = f"完成: 成功传输 {success_count} 个文件"
        elif success_count > 0:
            parent_task.status = CrossTransferTask.STATUS_PARTIAL_SUCCESS
            msg = f"完成: 成功 {success_count}"
            if failed_count > 0: msg += f", 失败 {failed_count}"
            if cancelled_count > 0: msg += f", 取消 {cancelled_count}"
            parent_task.current_step = msg
        elif failed_count > 0:
            parent_task.status = CrossTransferTask.STATUS_FAILED
            parent_task.current_step = f"失败: 全部 {failed_count} 个文件传输失败"
        elif cancelled_count > 0:
            parent_task.status = CrossTransferTask.STATUS_CANCELLED
            parent_task.current_step = f"已取消: {cancelled_count} 个文件传输被取消"
        else:
            parent_task.status = CrossTransferTask.STATUS_SUCCESS
            parent_task.transfer_type = CrossTransferTask.TRANSFER_STREAM
            parent_task.current_step = "完成"
        
        # 同步更新错误信息并触发时间戳更新
        parent_task.error_message = parent_task.current_step
        parent_task.updated_at = datetime.now()
        
        # [修复] 提前捕获最终状态属性
        status_id = parent_task.status
        status_name = parent_task.status_name
        progress = parent_task.progress
        step = parent_task.current_step
        err_msg = parent_task.error_message
        comp_files = parent_task.completed_files

        await run_in_threadpool(db.commit)
        logger.debug(f"父任务 {parent_task_id} 状态已结算: {step}")
        task_broadcaster.broadcast({
            "type": "task_updated", 
            "task_id": parent_task.id, 
            "status": status_id, 
            "status_name": status_name, 
            "progress": progress, 
            "current_step": step, 
            "error_message": err_msg,
            "completed_files": comp_files
        })
        
        # 如果存在 master_task，同步同步主任务进度
        if parent_task.master_task_id:
            await recalculate_master_status(parent_task.master_task_id, db)
        
        # 并发触发 master_task 状态结算
        if parent_task.master_task_id:
            await recalculate_master_status(parent_task.master_task_id, db)
    finally:
        if close_db:
            db.close()

async def execute_cross_transfer_task(task_id: int, is_child: bool = False):
    """后台执行跨网盘转存任务"""
    if is_child:
        # 子任务使用全局文件并发信号量
        async with FILE_DOWNLOAD_SEMAPHORE:
            await _do_execute_cross_transfer_task(task_id, is_child=True)
    else:
        # 顶级任务先获取父信号量
        async with PARENT_SEMAPHORE:
            # 同时占用一个文件信号量槽位
            async with FILE_DOWNLOAD_SEMAPHORE:
                await _do_execute_cross_transfer_task(task_id)

async def _do_execute_cross_transfer_task(task_id: int, is_child: bool = False):
    """实际执行逻辑"""
    from ..database import SessionLocal
    db = SessionLocal()
    
    try:
        task = db.query(CrossTransferTask).filter(CrossTransferTask.id == task_id).first()
        if not task:
            return
        
        task.status = CrossTransferTask.STATUS_RUNNING
        await update_progress(db, task, 0, "初始化中...")
        
        # 注册取消事件
        if task_id not in TASK_CANCEL_EVENTS:
            TASK_CANCEL_EVENTS[task_id] = asyncio.Event()
            
        def check_cancelled():
            return TASK_CANCEL_EVENTS.get(task_id) and TASK_CANCEL_EVENTS[task_id].is_set()
        
        # 1. 获取源和目标账户
        source_account = db.query(DiskAccount).filter(DiskAccount.id == task.source_account_id).first()
        target_account = db.query(DiskAccount).filter(DiskAccount.id == task.target_account_id).first()
        
        if not source_account or not target_account:
            raise Exception("源或目标账户不存在")
            
        # 2. 初始化服务
        await update_progress(db, task, 5, "初始化网盘服务...")
        source_creds = decrypt_credentials(source_account.credentials)
        target_creds = decrypt_credentials(target_account.credentials)
        
        # 合并持久化配置与运行时上下文
        source_config = json.loads(source_account.config or "{}")
        source_config["account_id"] = source_account.id
        source_service = get_disk_service(source_account.type, source_creds, source_config)
        
        target_config = json.loads(target_account.config or "{}")
        target_config.update({
            "storage_path": task.target_path,
            "account_id": target_account.id
        })
        target_service = get_disk_service(target_account.type, target_creds, target_config)
        
        # 🔒 [新增] 任务启动前预检：检查自身和父/主任务取消状态
        if task.status == CrossTransferTask.STATUS_CANCELLED:
            logger.info(f"[{task.id}] 任务启动前检测到已取消，跳过执行")
            return
        
        # 检查父任务是否已取消
        if task.parent_task_id:
            db.refresh(task)
            parent = db.query(CrossTransferTask).filter(
                CrossTransferTask.id == task.parent_task_id
            ).first()
            if parent and parent.status == CrossTransferTask.STATUS_CANCELLED:
                logger.info(f"[{task.id}] 启动前检测到父任务已取消，标记为已取消")
                task.status = CrossTransferTask.STATUS_CANCELLED
                task.error_message = "父任务已取消"
                task.current_step = "已取消"
                task.completed_at = datetime.now()
                db.commit()
                task_broadcaster.broadcast({
                    "type": "task_updated",
                    "task_id": task.id,
                    "status": task.status,
                    "status_name": task.status_name,
                    "current_step": task.current_step,
                    "error_message": task.error_message
                })
                return
        
        # 检查主任务是否已取消
        if task.master_task_id:
            master = db.query(CrossTransferTask).filter(
                CrossTransferTask.id == task.master_task_id
            ).first()
            if master and master.status == CrossTransferTask.STATUS_CANCELLED:
                logger.info(f"[{task.id}] 启动前检测到主任务已取消，标记为已取消")
                task.status = CrossTransferTask.STATUS_CANCELLED
                task.error_message = "主任务已取消"
                task.current_step = "已取消"
                task.completed_at = datetime.now()
                db.commit()
                task_broadcaster.broadcast({
                    "type": "task_updated",
                    "task_id": task.id,
                    "status": task.status,
                    "status_name": task.status_name,
                    "current_step": task.current_step,
                    "error_message": task.error_message
                })
                return
        
        # 3. 获取源文件信息（下载地址、MD5）
        await update_progress(db, task, 10, "获取源文件信息...")
        logger.info(f"[TRANSFER] Task {task.id} 开始获取源文件信息")
        file_info_res = await source_service.get_file_download_info(task.source_fid)
        logger.info(f"[TRANSFER] Task {task.id} 文件信息获取结果: {file_info_res.get('code')}")
        if file_info_res.get("code") != 200:
            raise Exception(f"获取源文件信息失败: {file_info_res.get('message')}")
            
        file_info = file_info_res.get("data", {})
        if file_info.get("is_sensitive"):
            raise Exception("涉及违规/敏感内容，无法传输")
            
        # 只有在获取到新的非空文件名时才更新，防止被插件返回的空字段覆盖
        new_fname = file_info.get("file_name") or file_info.get("name")
        if new_fname:
            task.source_file_name = new_fname
            
        task.source_file_size = file_info.get("size")
        task.source_file_md5 = file_info.get("md5")
        
        # 注入目标路径
        file_info["path"] = task.target_path
        
        file_size = file_info.get("size", 0)
        chunk_size = 262144
        download_url = file_info.get("download_url")
        source_fid = task.source_fid
        
        slice_md5 = None
        
        # 5. 计算验证分片并尝试秒传
        # 注意：这里需要根据目标网盘的逻辑来计算验证分片
        # 目前主要针对百度网盘
        
        # 小文件（< 256KB）不支持秒传，直接走普通上传
        if target_account.type == 2 and file_size >= chunk_size:  # 百度网盘 且 文件足够大
            # 4. 获取首分片 (256KB) 用于秒传检测
            await update_progress(db, task, 15, "下载首分片...")
            logger.info(f"[TRANSFER] Task {task.id} 开始下载首分片 (256KB)")
            # 夸克和迅雷网盘使用 download_slice_by_fid（每次获取新链接避免过期和身份惩罚）
            slice_data = None
            if source_account.type in [0, 3, 4]:  # 夸克(0), UC(3) 或 迅雷(4)
                slice_data = await source_service.download_slice_by_fid(source_fid, 0, chunk_size)
            else:
                slice_data = await source_service.download_slice(download_url, 0, chunk_size)
            
            logger.info(f"[TRANSFER] Task {task.id} 首分片下载结果: {'成功 (%d bytes)' % len(slice_data) if slice_data else '失败'}")
            if not slice_data:
                raise Exception("下载文件首分片失败")
                
            import hashlib
            slice_md5 = hashlib.md5(slice_data).hexdigest()
             # ... (existing rapid upload logic) ...
             # 需要先初始化百度服务获取 uk 等信息
            if not await target_service.check_status():
                 raise Exception("目标百度网盘未登录或 Cookie 失效")
            
            uk = target_service.uk
            ts = int(datetime.now().timestamp())
            
            # 计算验证分片偏移量
            await update_progress(db, task, 2, "计算验证分片...")
            from ..services.disk.baidu import BaiduDiskService
            offset = BaiduDiskService.calculate_offset(uk, file_info["md5"], ts, file_info["size"], chunk_size)
            
            # 下载验证分片（针对特定网盘刷新链接）
            await update_progress(db, task, 30, "下载验证分片...")
            if source_account.type in [0, 3, 4]:  # 夸克, UC 或 迅雷
                verify_chunk = await source_service.download_slice_by_fid(source_fid, offset, chunk_size)
            else:
                verify_chunk = await source_service.download_slice(download_url, offset, chunk_size)
            if not verify_chunk:
                raise Exception("下载验证分片失败")
                
            # 执行秒传
            await update_progress(db, task, 5, "执行秒传...")
            rapid_res = await target_service.rapid_upload(file_info, slice_md5, verify_chunk, offset)
            
            if rapid_res.get("code") == 200:
                task.status = CrossTransferTask.STATUS_SUCCESS
                task.transfer_type = CrossTransferTask.TRANSFER_RAPID
                task.result_path = rapid_res.get("data", {}).get("path")
                task.result_fid = str(rapid_res.get("data", {}).get("fs_id"))
                task.completed_at = datetime.now()
                await update_progress(db, task, 100, "秒传成功")
                db.commit()
                task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "status": task.status, "status_name": task.status_name, "progress": task.progress, "current_step": task.current_step, "result_path": task.result_path, "result_fid": task.result_fid})
                return  # 秒传成功，直接返回
            else:
                # 秒传失败，回退到普通上传 (不回退进度数值，保持在 5% 并更新 log)
                await update_progress(db, task, 5, f"秒传失败({rapid_res.get('message')})，回退到普通上传...")
        
        # 通用转存 (下载 -> 上传) - 秒传失败回退或小文件
        if file_size < chunk_size:
            await update_progress(db, task, 5, f"小文件({file_size}B)，使用普通上传...")
        
        import os
        import aiofiles
        temp_dir = "temp_data/transfer"
        temp_path = f"{temp_dir}/transfer_{task.id}_{task.source_fid}"
        
        try:
            os.makedirs(temp_dir, exist_ok=True)
            file_size = task.source_file_size or 1
            if task.is_folder == 1:
                # 这是一个空文件夹，无需下载，直接创建目录
                target_service = get_disk_service(target_account.type, target_creds, target_config)
                await update_progress(db, task, 5, "创建目标空目录...")
                if hasattr(target_service, "get_or_create_path"):
                    path_res = await get_or_create_path_with_cache(target_service, target_account.id, task.target_path)
                    if path_res.get("code") == 200:
                        await update_progress(db, task, 100, "完成")
                        task.status = CrossTransferTask.STATUS_SUCCESS
                        task.transfer_type = CrossTransferTask.TRANSFER_STREAM
                        task.completed_at = datetime.now()
                        db.commit()
                        task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "status": task.status, "status_name": task.status_name, "progress": task.progress, "current_step": task.current_step, "result_path": task.result_path})
                        return
                    else:
                        raise Exception(f"创建空目录失败: {path_res.get('message')}")
                else:
                    raise Exception("目标服务不支持快速创建目录")
            
            # 使用协程并发下载器（仅百度源）
            if source_account.type == 2:  # 百度
                # 使用并发下载器
                await update_progress(db, task, 5, "启动并发下载...")
                
                _last_cb_progress = 0
                last_probe_time = 0
                async def progress_cb(completed, total, speed):
                    nonlocal _last_cb_progress, last_probe_time
                    try:
                        now = time.time()
                        if now - last_probe_time >= 2.0:
                            last_probe_time = now
                            if task_id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[task_id].is_set():
                                logger.info(f"[{task_id}] 检测到百度下载取消信号，中止并发任务")
                                from ..services.download_manager import get_downloader
                                get_downloader().cancel(f"transfer_{task_id}")
                                raise asyncio.CancelledError("User cancelled transfer")

                        if total > 0:
                            # 进度范围: 5% -> 50%
                            progress = 5 + int((completed / total) * 45)
                            if progress != _last_cb_progress:
                                _last_cb_progress = progress
                                speed_mb = speed / 1024 / 1024
                            await update_progress(db, task, progress, f"下载中... {completed / 1024 / 1024:.1f}MB/{total / 1024 / 1024:.1f}MB ({speed_mb:.1f}MB/s)")
                    except Exception as e:
                        logger.debug(f"Download progress callback suppressed: {e}")
                
                # 使用任务ID作为下载任务ID，方便控制
                dl_task_id = f"transfer_{task.id}"
                
                try:
                    # Explicitly pass file_name to match signature
                    file_name = f"transfer_{task.id}_{task.source_fid}"
                    res_path = await source_service.download_concurrent(
                        download_url, 
                        temp_dir, 
                        file_name,
                        dl_task_id,
                        progress_cb
                    )
                    if res_path:
                        temp_path = res_path
                except Exception as e:
                    if "暂停" in str(e):
                        await update_progress(db, task, task.progress, "已暂停")
                        return
                    raise e

            else:
                # 统一使用网盘服务自带的 download_file 方法，内部处理了针对不同网盘的鉴权
                await update_progress(db, task, 5, "启动下载...")
                
                _last_cb_dp = -1
                last_probe_time = time.time()
                async def dp_cb(downloaded, total, speed):
                    nonlocal _last_cb_dp, last_probe_time
                    try:
                        # 每 2 秒检查一次内存取消信号
                        now = time.time()
                        if now - last_probe_time >= 2.0:
                            last_probe_time = now
                            if task_id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[task_id].is_set():
                                logger.info(f"[{task_id}] 检测到内存取消信号，主动中断下载流")
                                from ..services.download_manager import get_downloader
                                get_downloader().cancel(f"transfer_{task_id}")
                                raise asyncio.CancelledError("User cancelled transfer")

                        if total > 0:
                            # 进度范围: 5% -> 50%
                            dp = 5 + int((downloaded / total) * 45)
                            if dp != _last_cb_dp:
                                _last_cb_dp = dp
                                speed_mb = speed / 1024 / 1024
                                await update_progress(db, task, dp, f"下载中... {downloaded / 1024 / 1024:.1f}MB / {total / 1024 / 1024:.1f}MB ({speed_mb:.1f}MB/s)")
                    except Exception as e:
                        if isinstance(e, asyncio.CancelledError): raise e
                        logger.debug(f"Download progress callback suppressed: {e}")
                
                dl_task_id = f"transfer_{task_id}"
                success = await source_service.download_file(source_fid, temp_path, dp_cb, task_id=dl_task_id)
                    
                if not success:
                    raise Exception(f"{source_account.name} 文件下载失败")
            
            # 检查临时文件大小
            actual_size = os.path.getsize(temp_path)
            logger.debug(f"Temp file size: {actual_size} bytes, expected: {file_size} bytes")
            
            # 上传阶段 (50% -> 100%)
            await update_progress(db, task, 50, f"上传到目标网盘 {task.target_path}...")
            
            # 定义上传进度回调
            _last_cb_percent = -1
            async def upload_progress_callback(uploaded_chunks, total_chunks):
                nonlocal _last_cb_percent
                try:
                    if total_chunks > 0:
                        # 进度范围: 50% -> 100%
                        upload_percent = 50 + int((uploaded_chunks / total_chunks) * 50)
                        # 仅在百分比变化时发起更新（内部 update_progress 还有时间节流）
                        # 仅在百分比变化时发起更新（内部 update_progress 还有时间节流）
                        if upload_percent != _last_cb_percent:
                            _last_cb_percent = upload_percent
                            
                            # [核心增强] 如果检测到任务已标记为取消，立即抛出异常阻断
                            if task.status == CrossTransferTask.STATUS_CANCELLED or (task.id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[task.id].is_set()):
                                raise asyncio.CancelledError("Task cancelled internally")
                                
                            # 统一进度显示为 MB
                            uploaded_mb = uploaded_chunks / 1024 / 1024
                            total_mb = total_chunks / 1024 / 1024
                            await update_progress(db, task, upload_percent, f"上传中... {uploaded_mb:.1f}MB / {total_mb:.1f}MB")
                except Exception as e:
                    logger.debug(f"Upload progress callback suppressed: {e}")
            
            with open(temp_path, 'rb') as f:
                # 定义取消检测函数
                def check_cancel_signal():
                    if task_id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[task_id].is_set():
                        return True
                    return False

                # 使用目标路径上传（自动创建目录）
                if hasattr(target_service, 'upload_to_path'):
                    upload_res = await target_service.upload_to_path(
                        f, 
                        task.source_file_name, 
                        task.target_path or "/",
                        progress_callback=upload_progress_callback,
                        check_cancel=check_cancel_signal
                    )
                else:
                    # UC、百度等需要先获取目录 fid
                    target_fid = "0"  # 默认根目录
                    if task.target_path and task.target_path.strip('/'):
                        # 使用 get_or_create_path 获取目标目录的 fid
                        if hasattr(target_service, 'get_or_create_path'):
                            path_res = await get_or_create_path_with_cache(target_service, target_account.id, task.target_path)
                            if path_res.get("code") == 200:
                                target_fid = path_res.get("data", {}).get("fid", "0")
                            else:
                                raise Exception(f"创建目标路径失败: {path_res.get('message')}")
                    
                    upload_res = await target_service.upload_file(f, task.source_file_name, target_fid, progress_callback=upload_progress_callback, check_cancel=check_cancel_signal)
                
                if upload_res.get("code") == 200:
                    task.status = CrossTransferTask.STATUS_SUCCESS
                    task.transfer_type = CrossTransferTask.TRANSFER_STREAM
                    task.result_path = f"{task.target_path or '/'}/{task.source_file_name}".replace('//', '/')
                    task.completed_at = datetime.now()
                    await update_progress(db, task, 100, "上传成功")
                else:
                    raise Exception(f"普通上传失败: {upload_res.get('message')}")


                    
        finally:
            # 清理临时文件
            if os.path.exists(temp_path):
                os.remove(temp_path)
        
        db.commit()
        task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "status": task.status, "status_name": task.status_name, "progress": task.progress, "current_step": task.current_step, "result_path": task.result_path})
        
    except asyncio.CancelledError:
        logger.info(f"[{task_id}] 任务已捕获取消信号，正在更新状态...")
        task.status = CrossTransferTask.STATUS_CANCELLED
        task.current_step = "已取消"
        task.completed_at = datetime.now()
        db.commit()
    except Exception as e:
        # [核心优化] 增加取消信号二次检查，防止下载器返回 False 或抛出异常时覆盖已取消状态
        if task_id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[task_id].is_set():
            logger.info(f"[{task_id}] 捕获异常但在取消名单中，强制转入取消逻辑")
            task.status = CrossTransferTask.STATUS_CANCELLED
            task.current_step = "已取消"
        else:
            task.status = CrossTransferTask.STATUS_FAILED
            task.error_message = str(e)
            await update_progress(db, task, 0, f"失败: {str(e)[:50]}")
        
        task.completed_at = datetime.now()
        db.commit()
        task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "status": task.status, "status_name": task.status_name, "progress": task.progress, "current_step": task.current_step, "error_message": task.error_message})
    finally:
        # 如果是子任务，完成后重新计算父任务状态
        if is_child:
            # 内部任务不更新主状态
            task_broadcaster.broadcast({"type": "task_updated", "task_id": task_id, "parent_id": task.parent_task_id})
            
            # 清理取消事件
            if task_id in TASK_CANCEL_EVENTS:
                del TASK_CANCEL_EVENTS[task_id]
                
            db.close()
            return
        
        # 刷新主任务或父任务状态
        if task.parent_task_id:
            await recalculate_parent_status(task.parent_task_id, db)
        elif task.master_task_id:
            await recalculate_master_status(task.master_task_id, db)
        
        task_broadcaster.broadcast({"type": "task_updated", "task_id": task_id, "status": task.status, "status_name": task.status_name})
        
        # 清理取消事件
        if task_id in TASK_CANCEL_EVENTS:
            del TASK_CANCEL_EVENTS[task_id]
            
        db.close()

async def execute_folder_transfer_task(parent_task_id: int):
    """后台执行文件夹传输任务（并发执行子任务）"""
    # 父任务获取全局信号量
    async with PARENT_SEMAPHORE:
        from ..database import SessionLocal
        # 注意：不要在长时间并发任务中一直持有同一个 Session，最好按需创建
        
        # 1. 获取任务信息
        db = SessionLocal()
        try:
            parent_task = db.query(CrossTransferTask).filter(CrossTransferTask.id == parent_task_id).first()
            if not parent_task:
                return
            
            # 立即更新状态为运行中
            if parent_task.status != CrossTransferTask.STATUS_RUNNING:
                parent_task.status = CrossTransferTask.STATUS_RUNNING
                parent_task.error_message = None
                parent_task.current_step = "文件夹传输进行中..."
                await run_in_threadpool(db.commit)
                task_broadcaster.broadcast({"type": "task_updated", "task_id": parent_task.id, "status": parent_task.status, "status_name": parent_task.status_name})
            
            # 获取所有子任务ID和状态
            child_tasks_data = db.query(
                CrossTransferTask.id, 
                CrossTransferTask.source_file_name,
                CrossTransferTask.status
            ).filter(
                CrossTransferTask.parent_task_id == parent_task_id
            ).all()
            total = len(child_tasks_data)
        finally:
            db.close()

        # 2. 并发执行子任务
        completed_count = 0
        failed_count = 0
        
        # 内部锁，控制父任务进度更新
        parent_lock = asyncio.Lock()
        
        async def process_child(child_id, child_name):
            nonlocal completed_count, failed_count
            
            # 🔥 极速检查：如果父任务已取消，直接跳过子任务
            if parent_task_id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[parent_task_id].is_set():
                return
                
            # 直接调用，execute_cross_transfer_task 内部有全局 FILE_DOWNLOAD_SEMAPHORE 限制
            try:
                await execute_cross_transfer_task(child_id, is_child=True)
            except asyncio.CancelledError:
                logger.info(f"[{parent_task_id}] 子任务 {child_id} 已由父任务触发取消流中断")
                return
            
            # 任务完成后更新父任务进度 (需要锁和新Session)
            async with parent_lock:
                db_update = SessionLocal()
                try:
                    # 获取子任务最终状态
                    ct = db_update.query(CrossTransferTask).filter(CrossTransferTask.id == child_id).first()
                    if ct.status == CrossTransferTask.STATUS_SUCCESS:
                        completed_count += 1
                    else:
                        failed_count += 1
                    
                    # 更新父任务
                    pt = db_update.query(CrossTransferTask).filter(CrossTransferTask.id == parent_task_id).first()
                    if pt:
                        # 🔧 修复：计算进度时排除已取消的子任务
                        non_cancelled_count = db_update.query(CrossTransferTask).filter(
                            CrossTransferTask.parent_task_id == parent_task_id,
                            CrossTransferTask.status != CrossTransferTask.STATUS_CANCELLED
                        ).count()
                        
                        pt.completed_files = completed_count
                        if non_cancelled_count > 0:
                            pt.progress = min(100, int(((completed_count + failed_count) / non_cancelled_count) * 100))
                        else:
                            pt.progress = 0
                        pt.current_step = f"传输中... 成功{completed_count}/失败{failed_count}"
                        await run_in_threadpool(db_update.commit)
                        task_broadcaster.broadcast({"type": "task_updated", "task_id": pt.id, "progress": pt.progress, "current_step": pt.current_step, "completed_files": pt.completed_files, "status": pt.status, "status_name": pt.status_name})
                except Exception as e:
                    logger.error(f"更新父任务进度失败: {e}")
                finally:
                    db_update.close()

        # 启动所有协程 (跳过已成功的任务)
        tasks = []
        for ct in child_tasks_data:
            if ct.status == CrossTransferTask.STATUS_SUCCESS:
                # 已成功的任务直接计数，并不再执行
                completed_count += 1
                continue
            tasks.append(process_child(ct.id, ct.source_file_name))
            
        # 3. 执行任务
        # tasks = [process_child(cid, cname) for cid, cname, _ in child_tasks_data] # This line was in the instruction but would re-add successful tasks.
        try:
             # 并发运行
             if tasks: # Only gather if there are actual tasks to run
                await asyncio.gather(*tasks)
        except asyncio.CancelledError:
             logger.info(f"[{parent_task_id}] 文件夹转存主任务执行流捕获到取消信号，正在广播给子任务...")
             # 标记所有子任务为取消 (TASK_CANCEL_EVENTS 共享)
             for cid, _, _ in child_tasks_data:
                 if cid not in TASK_CANCEL_EVENTS:
                     TASK_CANCEL_EVENTS[cid] = asyncio.Event()
                 TASK_CANCEL_EVENTS[cid].set()
             raise # 重新抛出以由顶层捕获进行 DB 更新
        
        # 3. 最终状态更新
        db_final = SessionLocal()
        try:
            pt = db_final.query(CrossTransferTask).filter(CrossTransferTask.id == parent_task_id).first()
            if pt:
                # 🔧 修复：计算进度时排除已取消的子任务
                # 统计非取消状态的子任务总数
                non_cancelled_count = db_final.query(CrossTransferTask).filter(
                    CrossTransferTask.parent_task_id == parent_task_id,
                    CrossTransferTask.status != CrossTransferTask.STATUS_CANCELLED
                ).count()
                
                pt.completed_files = completed_count
                # 全成功或部分成功
                if failed_count == 0:
                    pt.status = CrossTransferTask.STATUS_SUCCESS
                    pt.current_step = f"完成: 成功传输 {completed_count} 个文件"
                elif completed_count > 0:
                    pt.status = CrossTransferTask.STATUS_PARTIAL_SUCCESS  # 部分成功
                    pt.current_step = f"完成: 成功 {completed_count} 个, 失败 {failed_count} 个"
                else:
                    pt.status = CrossTransferTask.STATUS_FAILED
                    pt.current_step = f"失败: 全部 {failed_count} 个文件传输失败"
                
                # 进度基于非取消任务总数
                if non_cancelled_count > 0:
                    pt.progress = min(100, int(((completed_count + failed_count) / non_cancelled_count) * 100))
                else:
                    pt.progress = 0  # 全部取消时进度为0
                
                pt.completed_at = datetime.now()
                db_final.commit()
                task_broadcaster.broadcast({"type": "task_updated", "task_id": pt.id, "status": pt.status, "status_name": pt.status_name, "progress": pt.progress, "current_step": pt.current_step, "completed_files": pt.completed_files})
        finally:
            db_final.close()


async def execute_multi_target_transfer(task_ids: List[int]):
    """多目标共享下载传输（下载一次，上传多个目标）"""
    from ..database import SessionLocal
    import tempfile
    import os
    import aiofiles
    
    if not task_ids:
        return
    
    db = SessionLocal()
    session_lock = asyncio.Lock()
    temp_path = None
    
    try:
        # 获取所有任务
        tasks = db.query(CrossTransferTask).filter(CrossTransferTask.id.in_(task_ids)).all()
        if not tasks:
            return
        
        # 使用第一个任务获取源文件信息
        first_task = tasks[0]
        
        # 更新所有任务状态为运行中
        for task in tasks:
            task.status = CrossTransferTask.STATUS_RUNNING
            task.current_step = "多目标传输初始化..."
        async with session_lock:
            await run_in_threadpool(db.commit)
        for task in tasks:
            task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "status": task.status, "status_name": task.status_name, "current_step": task.current_step})
        
        # 获取源账户
        source_account = db.query(DiskAccount).filter(DiskAccount.id == first_task.source_account_id).first()
        if not source_account:
            raise Exception("源账户不存在")
        
        # 初始化源服务
        source_creds = decrypt_credentials(source_account.credentials)
        # 合并持久化配置与运行时上下文
        source_config = json.loads(source_account.config or "{}")
        source_config["account_id"] = source_account.id
        source_service = get_disk_service(source_account.type, source_creds, source_config)
        
        # 获取源文件信息
        for task in tasks:
            task.current_step = "获取源文件信息..."
            task.progress = 5
        async with session_lock:
            await run_in_threadpool(db.commit)
        for task in tasks:
            task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "progress": task.progress, "current_step": task.current_step, "status": task.status, "status_name": task.status_name})
        
        file_info_res = await source_service.get_file_download_info(first_task.source_fid)
        if file_info_res.get("code") != 200:
            raise Exception(f"获取源文件信息失败: {file_info_res.get('message')}")
        
        file_info = file_info_res.get("data", {})
        file_size = file_info.get("size", 0)
        file_name = file_info.get("file_name", first_task.source_file_name)
        
        # 更新所有任务的文件信息
        for task in tasks:
            task.source_file_name = file_name
            task.source_file_size = file_size
            task.source_file_md5 = file_info.get("md5")
        async with session_lock:
            await run_in_threadpool(db.commit)
        
        # 下载到临时文件（只下载一次）
        for task in tasks:
            task.current_step = "共享下载中..."
            task.progress = 5
        db.commit()
        for task in tasks:
            task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "progress": task.progress, "current_step": task.current_step})
        
        temp_dir = "temp_data/transfer"
        temp_path = os.path.join(temp_dir, f"multi_transfer_{first_task.id}_{file_name}")
        
        # 夸克、UC 和迅雷网盘使用流式下载
        if source_account.type in [0, 3, 4]:  # 夸克, UC 或 迅雷
            last_probe_time = time.time()
            async def download_progress(downloaded, total, speed=0):
                nonlocal last_probe_time
                now = time.time()
                # 每 2 秒通过 DB 探测一次取消信号 (提高响应灵敏度)
                if now - last_probe_time >= 2.0:
                    last_probe_time = now
                    # [核心加固] 检查所有关联任务是否均已取消
                    all_cancelled = True
                    for tid in task_ids:
                        if tid not in TASK_CANCEL_EVENTS or not TASK_CANCEL_EVENTS[tid].is_set():
                            all_cancelled = False
                            break
                    
                    if all_cancelled:
                        logger.debug(f"[DEBUG] All simple multi-tasks cancelled, actively interrupting shared download")
                        dl_task_id = f"multi_transfer_{first_task.id}"
                        from ..services.download_manager import get_downloader
                        get_downloader().cancel(dl_task_id)
                        raise asyncio.CancelledError("All tasks cancelled")

                if total > 0:
                    # 进度范围: 5% -> 50%
                    progress = 5 + int((downloaded / total) * 45)
                    for task in tasks:
                        task.progress = progress
                        task.current_step = f"共享下载... {downloaded // 1024 // 1024}MB / {total // 1024 // 1024}MB"
                        # 【加固】同步更新热进度缓存
                        HOT_PROGRESS_CACHE[task.id] = {
                            "progress": progress,
                            "current_step": task.current_step,
                            "status": task.status,
                            "timestamp": now
                        }
                    
                    try:
                        async with session_lock:
                            await run_in_threadpool(db.commit)
                    except Exception as e:
                        logger.debug(f"Suppressing concurrent commit in download_progress: {e}")
                        
                    for task in tasks:
                        task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "progress": task.progress, "current_step": task.current_step, "status": task.status})
            
            
            dl_task_id = f"multi_transfer_{first_task.id}"
            success = await source_service.download_file(first_task.source_fid, temp_path, download_progress, task_id=dl_task_id)
                
            if not success:
                raise Exception("文件下载失败")
        else:
            # 其他网盘使用分片下载
            download_url = file_info.get("download_url")
            current_offset = 0
            chunk_size = 1024 * 1024  # 1MB
            last_probe_time = time.time()
            
            async with aiofiles.open(temp_path, 'wb') as f:
                while current_offset < file_size:
                    # 每 5 秒探测一次取消信号
                    now = time.time()
                    if now - last_probe_time >= 5.0:
                        last_probe_time = now
                        # [核心加固] 检查所有关联任务是否均已取消
                        all_cancelled = True
                        for tid in task_ids:
                            if tid not in TASK_CANCEL_EVENTS or not TASK_CANCEL_EVENTS[tid].is_set():
                                all_cancelled = False
                                break
                        
                        if all_cancelled:
                            raise asyncio.CancelledError("All tasks cancelled")

                    data = await source_service.download_slice(download_url, current_offset, chunk_size)
                    if not data:
                        break
                    await f.write(data)
                    current_offset += len(data)
                    
                    progress = 5 + int((current_offset / file_size) * 45)
                    for task in tasks:
                        task.progress = progress
                        task.current_step = f"共享下载... {current_offset // 1024 // 1024}MB / {file_size // 1024 // 1024}MB"
                        # 【加固】同步更新热进度缓存
                        HOT_PROGRESS_CACHE[task.id] = {
                            "progress": progress,
                            "current_step": task.current_step,
                            "status": task.status,
                            "timestamp": now
                        }
                    
                    try:
                        async with session_lock:
                            await run_in_threadpool(db.commit)
                    except Exception as e:
                        logger.debug(f"Suppressing concurrent commit in download_slice: {e}")
                        
                    for task in tasks:
                        task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "progress": task.progress, "current_step": task.current_step, "status": task.status})
        
        # 下载完成，并发上传到所有目标
        for task in tasks:
            task.progress = 60
            task.current_step = "准备上传到目标..."
        async with session_lock:
            await run_in_threadpool(db.commit)
        for task in tasks:
            task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "progress": task.progress, "current_step": task.current_step, "status": task.status})
        
        async def upload_to_target(task: CrossTransferTask):
            """上传到单个目标"""
            upload_db = SessionLocal()
            try:
                # 重新获取任务
                t = upload_db.query(CrossTransferTask).filter(CrossTransferTask.id == task.id).first()
                if not t:
                    return
                
                # [核心加固] 预检取消状态
                if t.status == CrossTransferTask.STATUS_CANCELLED or (t.id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[t.id].is_set()):
                    return

                target_account = upload_db.query(DiskAccount).filter(DiskAccount.id == t.target_account_id).first()
                
                if not target_account:
                    t.status = CrossTransferTask.STATUS_FAILED
                    t.error_message = "目标账户不存在"
                    upload_db.commit()
                    task_broadcaster.broadcast({"type": "task_updated", "task_id": t.id, "status": t.status, "status_name": t.status_name, "error_message": t.error_message})
                    return
                
                target_creds = decrypt_credentials(target_account.credentials)
                # 合并持久化配置与运行时上下文
                target_config = json.loads(target_account.config or "{}")
                target_config["account_id"] = target_account.id
                target_service = get_disk_service(target_account.type, target_creds, target_config)
                
                t.current_step = f"上传到 {target_account.name}..."
                t.progress = 50
                upload_db.commit()
                task_broadcaster.broadcast({"type": "task_updated", "task_id": t.id, "progress": t.progress, "current_step": t.current_step, "status": t.status, "status_name": t.status_name})
                
                # 定义上传进度回调
                _last_cb_percent = -1
                async def upload_progress_callback(uploaded_chunks, total_chunks):
                    nonlocal _last_cb_percent
                    try:
                        if total_chunks > 0:
                            # 进度范围: 50% -> 100%
                            upload_percent = 50 + int((uploaded_chunks / total_chunks) * 50)
                            if upload_percent != _last_cb_percent:
                                _last_cb_percent = upload_percent
                                t.progress = upload_percent
                                
                                # [核心增强] 如果检测到任务已标记为取消，立即抛出异常阻断
                                if t.status == CrossTransferTask.STATUS_CANCELLED or (t.id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[t.id].is_set()):
                                    raise asyncio.CancelledError("Task cancelled internally")
                                    
                                uploaded_mb = uploaded_chunks / 1024 / 1024
                                total_mb = total_chunks / 1024 / 1024
                                t.current_step = f"上传中... {uploaded_mb:.1f}MB / {total_mb:.1f}MB"
                                await update_progress(upload_db, t, upload_percent, t.current_step)
                    except Exception as e:
                        logger.debug(f"Upload progress callback suppressed: {e}")
                
                with open(temp_path, 'rb') as f:
                    # 定义取消检测函数
                    def check_cancel_signal():
                        if t.id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[t.id].is_set():
                            return True
                        return False

                    if hasattr(target_service, 'upload_to_path'):
                        upload_res = await target_service.upload_to_path(
                            f, 
                            file_name, 
                            t.target_path or "/",
                            progress_callback=upload_progress_callback,
                            check_cancel=check_cancel_signal
                        )
                    else:
                        # UC、百度等需要先获取目录 fid
                        target_fid = "0"  # 默认根目录
                        if t.target_path and t.target_path.strip('/'):
                            # 使用 get_or_create_path 获取目标目录的 fid
                            if hasattr(target_service, 'get_or_create_path'):
                                path_res = await get_or_create_path_with_cache(target_service, target_account.id, t.target_path)
                                if path_res.get("code") == 200:
                                    target_fid = path_res.get("data", {}).get("fid", "0")
                                else:
                                    raise Exception(f"创建目标路径失败: {path_res.get('message')}")
                        
                        upload_res = await target_service.upload_file(f, file_name, target_fid, progress_callback=upload_progress_callback, check_cancel=check_cancel_signal)
                    
                    if upload_res.get("code") == 200:
                        t.status = CrossTransferTask.STATUS_SUCCESS
                        t.transfer_type = CrossTransferTask.TRANSFER_STREAM
                        t.result_path = f"{t.target_path or '/'}/{file_name}".replace('//', '/')
                        t.completed_at = datetime.now()
                        t.progress = 100
                        t.current_step = "上传成功"
                    else:
                        t.status = CrossTransferTask.STATUS_FAILED
                        t.error_message = upload_res.get("message", "上传失败")
                        t.completed_at = datetime.now()
                        t.current_step = f"上传失败: {t.error_message[:30]}"
                upload_db.commit()
                task_broadcaster.broadcast({"type": "task_updated", "task_id": t.id, "status": t.status, "status_name": t.status_name, "progress": t.progress, "current_step": t.current_step, "error_message": t.error_message, "result_path": t.result_path})
            except Exception as e:
                t = upload_db.query(CrossTransferTask).filter(CrossTransferTask.id == task.id).first()
                if t:
                    t.status = CrossTransferTask.STATUS_FAILED
                    t.error_message = str(e)
                    t.completed_at = datetime.now()
                    upload_db.commit()
                    task_broadcaster.broadcast({"type": "task_updated", "task_id": t.id, "status": t.status, "status_name": t.status_name, "error_message": t.error_message})
            finally:
                upload_db.close()
        
        # 并发上传到所有目标
        upload_tasks = [upload_to_target(task) for task in tasks]
        await asyncio.gather(*upload_tasks)
        
    except Exception as e:
        # 所有任务都标记为失败
        for task in tasks:
            task.status = CrossTransferTask.STATUS_FAILED
            task.error_message = str(e)
            task.completed_at = datetime.now()
            task.current_step = f"失败: {str(e)[:50]}"
        async with session_lock:
            await run_in_threadpool(db.commit)
        for task in tasks:
            task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "status": task.status, "status_name": task.status_name, "current_step": task.current_step, "error_message": task.error_message})
    finally:
        db.close()
        # 清理临时文件
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except:
                pass


async def execute_multi_target_folder_transfer(master_task_id: int, parent_task_ids: List[int]):
    """多目标文件夹共享下载传输（每个文件下载一次，上传到所有目标）"""
    async with PARENT_SEMAPHORE:
        await _do_execute_multi_target_folder_transfer(master_task_id, parent_task_ids)

async def _do_execute_multi_target_folder_transfer(master_task_id: int, parent_task_ids: List[int]):
    """实际执行逻辑"""
    from ..database import SessionLocal
    import tempfile
    import os
    import aiofiles
    
    db = SessionLocal()
    session_lock = asyncio.Lock()
    
    try:
        # 获取主任务
        master_task = db.query(CrossTransferTask).filter(CrossTransferTask.id == master_task_id).first()
        if not master_task:
            return
        
        master_task.status = CrossTransferTask.STATUS_RUNNING
        master_task.current_step = "初始化多目标传输..."
        logger.info(f"[TRANSFER] 主任务 {master_task_id} 开始执行: {master_task.source_file_name}")
        async with session_lock:
            await run_in_threadpool(db.commit)
        task_broadcaster.broadcast({"type": "task_updated", "task_id": master_task.id, "status": master_task.status, "status_name": master_task.status_name, "current_step": master_task.current_step})
        
        # 获取源账户
        source_account = db.query(DiskAccount).filter(DiskAccount.id == master_task.source_account_id).first()
        if not source_account:
            raise Exception("源账户不存在")
        
        source_creds = decrypt_credentials(source_account.credentials)
        # 合并持久化配置与运行时上下文
        source_config = json.loads(source_account.config or "{}")
        source_config["account_id"] = source_account.id
        source_service = get_disk_service(source_account.type, source_creds, source_config)
        
        # 获取所有目标父任务
        parent_tasks = db.query(CrossTransferTask).filter(
            CrossTransferTask.id.in_(parent_task_ids)
        ).all()
        
        # 按源文件分组所有子任务（仅处理本次指定的网盘目标，防止重试 A 误触发 B）
        all_child_tasks = db.query(CrossTransferTask).filter(
            CrossTransferTask.master_task_id == master_task_id,
            CrossTransferTask.parent_task_id.in_(parent_task_ids)
        ).all()
        
        # 按 source_fid 分组（每个源文件对应多个目标的子任务）
        file_groups = {}
        for child in all_child_tasks:
            if child.source_fid not in file_groups:
                file_groups[child.source_fid] = []
            file_groups[child.source_fid].append(child)
        
        total_files = len(file_groups)
        completed_files = 0
        failed_files = 0
        
        # 启动所有父任务
        for pt in parent_tasks:
            pt.status = CrossTransferTask.STATUS_RUNNING
            pt.current_step = "开始传输..."
        async with session_lock:
            await run_in_threadpool(db.commit)
        for pt in parent_tasks:
            task_broadcaster.broadcast({"type": "task_updated", "task_id": pt.id, "status": pt.status, "status_name": pt.status_name, "current_step": pt.current_step})
        
        # 遍历每个源文件，并发处理
        tasks = []
        
        async def process_file_group(source_fid, child_task_ids):
            """处理单个源文件的传输（共享下载 -> 分发上传）"""
            # 🔒 [增强] 启动前预检：检查主任务取消状态并标记所有子任务
            if master_task_id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[master_task_id].is_set():
                logger.info(f"[TRANSFER] 主任务 {master_task_id} 已取消，跳过文件组 {source_fid} 并标记子任务。")
                # 标记所有子任务为已取消
                local_db = SessionLocal()
                try:
                    for cid in child_task_ids:
                        ct = local_db.query(CrossTransferTask).filter(CrossTransferTask.id == cid).first()
                        if ct and ct.status not in [CrossTransferTask.STATUS_CANCELLED, CrossTransferTask.STATUS_SUCCESS]:
                            ct.status = CrossTransferTask.STATUS_CANCELLED
                            ct.error_message = "主任务已取消"
                            ct.current_step = "已取消"
                            ct.completed_at = datetime.now()
                            task_broadcaster.broadcast({
                                "type": "task_updated",
                                "task_id": ct.id,
                                "status": ct.status,
                                "status_name": ct.status_name,
                                "current_step": ct.current_step,
                                "error_message": ct.error_message
                            })
                    local_db.commit()
                finally:
                    local_db.close()
                return True  # 返回 True 表示已处理（跳过）
            async with FILE_DOWNLOAD_SEMAPHORE:
                local_db = SessionLocal()
                local_lock = asyncio.Lock()
                
                temp_path = None
                try:
                    # 获取该文件的所有目标子任务
                    child_tasks = local_db.query(CrossTransferTask).filter(CrossTransferTask.id.in_(child_task_ids)).all()
                    if not child_tasks:
                        return False
                        
                    first_child = child_tasks[0]
                    file_name = first_child.source_file_name
                    logger.info(f"[TRANSFER] 准备处理文件组: {file_name} (目标数: {len(child_tasks)})")
                    
                    # 检查是否所有子任务都已成功（断点续传优化）
                    if all(ct.status == CrossTransferTask.STATUS_SUCCESS for ct in child_tasks):
                        logger.info(f"[TRANSFER] 文件组 {file_name} 所有子任务已完成，跳过。")
                        return True

                    # 更新状态为运行中
                    for ct in child_tasks:
                        if ct.status != CrossTransferTask.STATUS_SUCCESS:
                            # 如果任务已被取消，则跳过状态更新
                            if ct.id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[ct.id].is_set():
                                continue
                            ct.status = CrossTransferTask.STATUS_RUNNING
                            ct.current_step = "等待下载插槽..."
                    async with local_lock:
                        await run_in_threadpool(local_db.commit)
                    for ct in child_tasks:
                        task_broadcaster.broadcast({"type": "task_updated", "task_id": ct.id, "status": ct.status, "status_name": ct.status_name, "current_step": ct.current_step})

                    if first_child.is_folder == 1:
                        file_size = 0
                        temp_path = None
                    else:
                        # 获取源文件下载信息
                        file_info_res = await source_service.get_file_download_info(source_fid)
                        if file_info_res.get("code") != 200:
                            raise Exception(f"获取文件信息失败: {file_info_res.get('message')}")
                        
                        file_info = file_info_res.get("data", {})
                        file_size = file_info.get("size", 0)
                        
                        # 下载到临时文件
                        temp_dir = "temp_data/transfer"
                        temp_path = os.path.join(temp_dir, f"mt_folder_{master_task_id}_{source_fid}")
                    
                    # 下载进度回调
                    last_update_time = time.time()
                    last_probe_time = 0 
                    
                    async def dp_cb(downloaded, total, speed=0):
                        nonlocal last_update_time, last_probe_time
                        now = time.time()
                        
                        # 检测取消信号
                        if now - last_probe_time >= 2.0:
                            last_probe_time = now
                            dl_task_id = f"transfer_mt_{master_task_id}_{source_fid}"
                            
                            is_cancelled = False
                            # 1. 检查主任务是否取消
                            if master_task_id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[master_task_id].is_set():
                                is_cancelled = True
                            
                            # 2. 检查子任务取消情况 (只有当所有子任务都取消时，才中断下载)
                            if not is_cancelled:
                                cancel_count = 0
                                for cid in child_task_ids:
                                    if cid in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[cid].is_set():
                                        cancel_count += 1
                                if cancel_count == len(child_task_ids):
                                    is_cancelled = True
                                        
                            if is_cancelled:
                                logger.info(f"[{source_fid}] 检测到取消信号(Master或全部Child)，中断下载")
                                from ..services.download_manager import get_downloader
                                get_downloader().cancel(dl_task_id)
                                raise asyncio.CancelledError("User cancelled transfer")

                        if total > 0:
                            dp = int((downloaded / total) * 50)  # 下载占 0-50%
                            # 进度更新节流
                            # 使用 child_tasks[0] 的进度作为参考可能有误，应该单独判断
                            should_update = (now - last_update_time >= 2.0)
                            
                            if should_update:
                                updated_any = False
                                for ct in child_tasks:
                                    # 如果该子任务已取消或已完成，跳过更新
                                    if ct.status in [CrossTransferTask.STATUS_SUCCESS, CrossTransferTask.STATUS_FAILED]:
                                        continue
                                    if ct.id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[ct.id].is_set():
                                        continue
                                    
                                    current_step = f"共享下载中... {downloaded // 1024 // 1024}MB / {total // 1024 // 1024}MB"
                                    if dp > ct.progress or ct.current_step != current_step:
                                        ct.progress = dp
                                        ct.current_step = current_step
                                        # 更新内存缓存
                                        HOT_PROGRESS_CACHE[ct.id] = {
                                            "progress": dp,
                                            "current_step": current_step,
                                            "status": ct.status,
                                            "timestamp": now
                                        }
                                        updated_any = True
                                
                                if updated_any:
                                    try:
                                        async with local_lock:
                                            await run_in_threadpool(local_db.commit)
                                    except Exception as e:
                                        logger.debug(f"Suppressing concurrent commit in dp_cb: {e}")
                                    
                                    last_update_time = now
                                    # 批量广播
                                    for ct in child_tasks:
                                        if ct.id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[ct.id].is_set():
                                            continue
                                        task_broadcaster.broadcast({
                                            "type": "task_updated", 
                                            "task_id": ct.id, 
                                            "progress": ct.progress, 
                                            "current_step": ct.current_step
                                        })
                                    
                                    # 触发父任务更新
                                    processed_parents = set()
                                    for ct in child_tasks:
                                        if ct.parent_task_id and ct.parent_task_id not in processed_parents:
                                            asyncio.create_task(recalculate_parent_status(ct.parent_task_id))
                                            processed_parents.add(ct.parent_task_id)

                    if first_child.is_folder != 1:
                        # 执行下载
                        dl_task_id = f"transfer_mt_{master_task_id}_{source_fid}"
                        success = await source_service.download_file(source_fid, temp_path, dp_cb, task_id=dl_task_id)
                        if not success:
                            raise Exception("下载失败")
                    
                    # 并发上传到所有目标
                    # 注意：upload_to_target 内部会创建自己的 SessionLocal，所以这里的 concurrency 安全
                    
                    async def upload_to_target(child_id):
                        upload_db = SessionLocal()
                        upload_lock = asyncio.Lock()
                        try:
                            ct = upload_db.query(CrossTransferTask).filter(CrossTransferTask.id == child_id).first()
                            # 如果任务已取消/成功/失败，则跳过
                            if not ct or ct.status in [CrossTransferTask.STATUS_SUCCESS, CrossTransferTask.STATUS_FAILED, CrossTransferTask.STATUS_CANCELLED]:
                                return True
                            
                            # 双重检查取消事件
                            if child_id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[child_id].is_set():
                                return False

                            target_account = upload_db.query(DiskAccount).filter(DiskAccount.id == ct.target_account_id).first()
                            
                            if not target_account:
                                ct.status = CrossTransferTask.STATUS_FAILED
                                ct.error_message = "目标账户不存在"
                                async with upload_lock: await run_in_threadpool(upload_db.commit)
                                return False
                            
                            # 初始化目标服务
                            target_creds = decrypt_credentials(target_account.credentials)
                            target_config = json.loads(target_account.config or "{}")
                            target_config["account_id"] = target_account.id
                            target_service = get_disk_service(target_account.type, target_creds, target_config)
                            
                            if ct.is_folder == 1:
                                ct.current_step = f"在 {target_account.name} 创建目录..."
                                async with upload_lock: await run_in_threadpool(upload_db.commit)
                                task_broadcaster.broadcast({"type": "task_updated", "task_id": ct.id, "status": ct.status, "current_step": ct.current_step})
                                
                                if hasattr(target_service, 'get_or_create_path'):
                                    path_res = await get_or_create_path_with_cache(target_service, target_account.id, ct.target_path)
                                    if path_res.get("code") == 200:
                                        created_fid = path_res.get("data", {}).get("fid", "")
                                        ct.status = CrossTransferTask.STATUS_SUCCESS
                                        ct.transfer_type = CrossTransferTask.TRANSFER_STREAM
                                        ct.progress = 100
                                        ct.current_step = "完成"
                                        ct.completed_at = datetime.now()
                                        if created_fid:
                                            ct.result_fid = str(created_fid)
                                            # 注意：空目录任务的 target_path 是子目录（如 /嘉欣(1)/解压码7 ）
                                            # 不应该将它的 fid 回写到父任务（父任务要的是根目录 /嘉欣(1) 的 fid）
                                            # 根目录 fid 会在普通文件上传时通过 get_or_create_path 正确写入
                                        async with upload_lock: await run_in_threadpool(upload_db.commit)
                                        task_broadcaster.broadcast({"type": "task_updated", "task_id": ct.id, "status": ct.status, "progress": 100, "current_step": ct.current_step})
                                        if ct.parent_task_id:
                                            await recalculate_parent_status(ct.parent_task_id, upload_db)
                                        return True
                                    else:
                                        raise Exception(f"创建空目录失败: {path_res.get('message')}")
                                else:
                                    raise Exception("目标服务不支持快速创建目录")

                            ct.current_step = f"上传到 {target_account.name}..."
                            async with upload_lock: await run_in_threadpool(upload_db.commit)
                            task_broadcaster.broadcast({"type": "task_updated", "task_id": ct.id, "status": ct.status, "current_step": ct.current_step})
                            
                            last_up_time = 0
                            async def progress_cb(uploaded, total):
                                nonlocal last_up_time
                                now = time.time()
                                if total > 0:
                                    up_p = 50 + int((uploaded / total) * 50)
                                    current_step = f"上传中... {uploaded // 1024 // 1024}MB / {total // 1024 // 1024}MB"
                                    if (up_p > ct.progress or current_step != ct.current_step) and now - last_up_time >= 1.5:
                                        ct.progress = up_p
                                        ct.current_step = current_step
                                        async with upload_lock: await run_in_threadpool(upload_db.commit)
                                        last_up_time = now
                                        task_broadcaster.broadcast({
                                            "type": "task_updated", 
                                            "task_id": ct.id, 
                                            "progress": ct.progress,
                                            "current_step": ct.current_step
                                        })
                                        if ct.parent_task_id:
                                            asyncio.create_task(recalculate_parent_status(ct.parent_task_id))
                            
                            with open(temp_path, 'rb') as f:
                                def check_cancel_signal():
                                    # 检查全局取消 + 任务取消 + 主任务状态
                                    if master_task_id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[master_task_id].is_set():
                                        return True
                                    if ct.id in TASK_CANCEL_EVENTS and TASK_CANCEL_EVENTS[ct.id].is_set():
                                        return True
                                    if ct.status == CrossTransferTask.STATUS_CANCELLED:
                                        return True
                                    return False


                                # 处理路径：优先使用 upload_to_path，否则手动获取目录 fid
                                if hasattr(target_service, 'upload_to_path'):
                                    # 夸克、迅雷等有 upload_to_path 方法
                                    result = await target_service.upload_to_path(f, file_name, ct.target_path or "/", progress_cb, check_cancel=check_cancel_signal)
                                else:
                                    # UC、百度等需要先获取目录 fid
                                    target_fid = "0"  # 默认根目录
                                    if ct.target_path and ct.target_path.strip('/'):
                                        # 使用 get_or_create_path 获取目标目录的 fid
                                        if hasattr(target_service, 'get_or_create_path'):
                                            logger.info(f"[TRANSFER] 任务 {ct.id} 开始解析路径: {ct.target_path}")
                                            path_res = await get_or_create_path_with_cache(target_service, target_account.id, ct.target_path)
                                            logger.info(f"[TRANSFER] 任务 {ct.id} 路径解析结果: code={path_res.get('code')}, fid={path_res.get('data', {}).get('fid')}")
                                            if path_res.get("code") == 200:
                                                target_fid = path_res.get("data", {}).get("fid", "0")
                                                # 回写目标文件夹的 fid 到父任务，供分享链接时直接取用
                                                # 无需再回写给父任务，防止子任务竞争导致父任务被污染
                                                pass
                                            else:
                                                raise Exception(f"创建目标路径失败: {path_res.get('message')}")
                                    
                                    result = await target_service.upload_file(f, file_name, target_fid, progress_cb, check_cancel=check_cancel_signal)
                                
                                if result.get("code") == 200:
                                    ct.status = CrossTransferTask.STATUS_SUCCESS
                                    ct.progress = 100
                                    ct.current_step = "完成"
                                    ct.completed_at = datetime.now()
                                    async with upload_lock: await run_in_threadpool(upload_db.commit)
                                    task_broadcaster.broadcast({"type": "task_updated", "task_id": ct.id, "status": ct.status, "progress": 100, "current_step": ct.current_step})
                                    # 立即触发父任务状态重算
                                    if ct.parent_task_id:
                                        await recalculate_parent_status(ct.parent_task_id, upload_db)
                                    return True
                                else:
                                    ct.status = CrossTransferTask.STATUS_FAILED
                                    ct.error_message = result.get("message", "上传失败")
                                    ct.current_step = ct.error_message
                                    logger.error(f"[TRANSFER] 子任务 {child_id} 失败: {ct.error_message}")
                                    async with upload_lock: await run_in_threadpool(upload_db.commit)
                                    task_broadcaster.broadcast({"type": "task_updated", "task_id": ct.id, "status": ct.status, "error_message": ct.error_message, "current_step": ct.current_step})
                                    # 立即触发父任务状态重算
                                    if ct.parent_task_id:
                                        await recalculate_parent_status(ct.parent_task_id, upload_db)
                                    return False
                        except Exception as e:
                            # Handle specific error inside upload
                            logger.error(f"Upload error: {e}")
                            ct = upload_db.query(CrossTransferTask).filter(CrossTransferTask.id == child_id).first()
                            if ct:
                                ct.status = CrossTransferTask.STATUS_FAILED
                                ct.error_message = str(e)
                                async with upload_lock: await run_in_threadpool(upload_db.commit)
                            return False
                        finally:
                            upload_db.close()

                    # 执行并行的上传任务
                    upload_results = await asyncio.gather(*[upload_to_target(cid) for cid in child_task_ids])
                    
                    # 触发父任务更新
                    processed_parents = set()
                    for ct in child_tasks:
                        if ct.parent_task_id and ct.parent_task_id not in processed_parents:
                            asyncio.create_task(recalculate_parent_status(ct.parent_task_id))
                            processed_parents.add(ct.parent_task_id)

                    return all(upload_results)
                
                except asyncio.CancelledError:
                    logger.info(f"Group {source_fid} Cancelled")
                    # 标记失败或取消
                    return False
                except Exception as e:
                    logger.error(f"Group {source_fid} error: {e}")
                    # 标记组内所有任务失败
                    try:
                        child_tasks = local_db.query(CrossTransferTask).filter(CrossTransferTask.id.in_(child_task_ids)).all()
                        for ct in child_tasks:
                            ct.status = CrossTransferTask.STATUS_FAILED
                            ct.error_message = str(e)
                        async with local_lock:
                            await run_in_threadpool(local_db.commit)
                        for ct in child_tasks:
                            asyncio.create_task(recalculate_parent_status(ct.parent_task_id))
                    except:
                        pass
                    return False
                finally:
                    if temp_path and os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except:
                            pass
                    local_db.close()

        # 创建所有文件组的任务
        for source_fid, child_tasks in file_groups.items():
            tasks.append(process_file_group(source_fid, [ct.id for ct in child_tasks]))
        
        # 并发执行所有组
        if tasks:
            group_results = await asyncio.gather(*tasks)
            completed_files = sum(1 for r in group_results if r)
            failed_files = len(tasks) - completed_files
        
        # 更新主任务进度 (全量汇总)
        # 刷新 master_task
        db.refresh(master_task)
        all_pt_completed = db.query(CrossTransferTask).filter(
            CrossTransferTask.master_task_id == master_task_id,
            CrossTransferTask.parent_task_id.isnot(None),
            CrossTransferTask.status == CrossTransferTask.STATUS_SUCCESS
        ).count()
        master_task.completed_files = all_pt_completed
        
        total_non_cancelled_tasks = db.query(CrossTransferTask).filter(
            CrossTransferTask.master_task_id == master_task_id,
            CrossTransferTask.parent_task_id.isnot(None),
            CrossTransferTask.status != CrossTransferTask.STATUS_CANCELLED
        ).count()
        
        if total_non_cancelled_tasks > 0:
            master_task.progress = min(100, int((all_pt_completed / total_non_cancelled_tasks) * 100))
        else:
            master_task.progress = 0
        
        async with session_lock:
            await run_in_threadpool(db.commit)
        task_broadcaster.broadcast({"type": "task_updated", "task_id": master_task.id, "progress": master_task.progress, "completed_files": master_task.completed_files, "status": master_task.status})
        
        # 完成：更新所有父任务状态
        for pt_id in parent_task_ids:
            pt_db = SessionLocal()
            try:
                pt = pt_db.query(CrossTransferTask).filter(CrossTransferTask.id == pt_id).first()
                if pt:
                    success_count = pt_db.query(CrossTransferTask).filter(
                        CrossTransferTask.parent_task_id == pt_id,
                        CrossTransferTask.status == CrossTransferTask.STATUS_SUCCESS
                    ).count()
                    fail_count = pt_db.query(CrossTransferTask).filter(
                        CrossTransferTask.parent_task_id == pt_id,
                        CrossTransferTask.status == CrossTransferTask.STATUS_FAILED
                    ).count()
                    
                    pt.completed_files = success_count
                    if fail_count == 0:
                        pt.status = CrossTransferTask.STATUS_SUCCESS
                        pt.current_step = f"完成: 成功 {success_count} 个文件"
                    elif success_count > 0:
                        pt.status = CrossTransferTask.STATUS_PARTIAL_SUCCESS
                        pt.current_step = f"完成: 成功 {success_count}, 失败 {fail_count}"
                    else:
                        pt.status = CrossTransferTask.STATUS_FAILED
                        pt.current_step = f"失败: 全部 {fail_count} 个文件失败"
                    pt.progress = 100
                    pt.completed_at = datetime.now()
                    pt_db.commit()
                    task_broadcaster.broadcast({"type": "task_updated", "task_id": pt.id, "status": pt.status, "status_name": pt.status_name, "progress": pt.progress, "current_step": pt.current_step, "completed_files": pt.completed_files})
            finally:
                pt_db.close()
        
        # 更新主任务状态
        master_task.progress = 100
        master_task.completed_targets = len(parent_task_ids)
        if failed_files == 0:
            master_task.status = CrossTransferTask.STATUS_SUCCESS
            master_task.current_step = f"完成: {total_files} 文件 → {len(parent_task_ids)} 目标"
        elif completed_files > 0:
            master_task.status = CrossTransferTask.STATUS_PARTIAL_SUCCESS
            master_task.current_step = f"部分完成: 成功 {completed_files} 文件, 失败 {failed_files} 文件"
            master_task.completed_files = completed_files * len(parent_task_ids)
        else:
            master_task.status = CrossTransferTask.STATUS_FAILED
            master_task.current_step = f"全部失败: 共 {failed_files} 个文件转存失败"
            master_task.completed_files = 0
        master_task.completed_at = datetime.now()
        db.commit()
        task_broadcaster.broadcast({"type": "task_updated", "task_id": master_task.id, "status": master_task.status, "status_name": master_task.status_name, "progress": master_task.progress, "current_step": master_task.current_step, "completed_targets": master_task.completed_targets, "completed_files": master_task.completed_files})
        
    except Exception as e:
        master_task.status = CrossTransferTask.STATUS_FAILED
        master_task.error_message = str(e)
        master_task.current_step = f"失败: {str(e)[:50]}"
        master_task.completed_at = datetime.now()
        db.commit()
        task_broadcaster.broadcast({"type": "task_updated", "task_id": master_task.id, "status": master_task.status, "status_name": master_task.status_name, "current_step": master_task.current_step, "error_message": master_task.error_message})
    finally:
        db.close()


@router.post("/start", response_model=CrossTransferResponse)
async def start_cross_transfer(
    request: CrossTransferRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """启动跨网盘转存任务（支持多目标和文件夹）"""
    # 确定目标账户列表
    target_ids = []
    if request.target_account_ids:
        target_ids = request.target_account_ids
    elif request.target_account_id:
        target_ids = [request.target_account_id]
    else:
        raise HTTPException(status_code=400, detail="请选择目标账户")
    
    # 验证源账户
    source_account = db.query(DiskAccount).filter(DiskAccount.id == request.source_account_id).first()
    if not source_account:
        raise HTTPException(status_code=404, detail="源账户不存在")
    
    # 验证目标账户
    target_accounts = db.query(DiskAccount).filter(DiskAccount.id.in_(target_ids)).all()
    if len(target_accounts) != len(target_ids):
        raise HTTPException(status_code=404, detail="部分目标账户不存在")
    
    # 获取某个目标账户的路径（支持独立路径配置）
    def get_target_path(account_id: int) -> str:
        if request.target_paths and str(account_id) in request.target_paths:
            return request.target_paths[str(account_id)]
        return request.target_path or "/"
    
    task_ids = []
    
    # 账号服务缓存（避免循环内重复解密和实例化）
    service_cache = {}
    def get_svc(acc):
        if acc.id not in service_cache:
            creds = decrypt_credentials(acc.credentials)
            # 合并持久化配置与运行时上下文
            config = json.loads(acc.config or "{}")
            config["account_id"] = acc.id
            service_cache[acc.id] = get_disk_service(acc.type, creds, config)
        return service_cache[acc.id]
    
    # 文件夹传输处理
    if request.is_folder:
        source_creds = decrypt_credentials(source_account.credentials)
        # 合并持久化配置与运行时上下文
        source_config = json.loads(source_account.config or "{}")
        source_config["account_id"] = source_account.id
        source_service = get_disk_service(source_account.type, source_creds, source_config)
        
        # 递归获取文件列表
        if not hasattr(source_service, 'list_folder_recursive'):
            raise HTTPException(status_code=400, detail="该网盘类型暂不支持文件夹传输")
        
        files_result = await source_service.list_folder_recursive(request.source_fid)
        if files_result.get("code") != 200:
            raise HTTPException(status_code=400, detail=files_result.get("message", "获取文件列表失败"))
        
        file_list = files_result.get("data", [])
        if len(file_list) == 0:
            # 如果是空文件夹，降级为普通的、在各目标分别建立空目录的任务
            for target_id in target_ids:
                base_target_path = get_target_path(target_id)
                target_svc = get_svc(target_accounts[target_ids.index(target_id)])
                s_folder_name = target_svc.sanitize_path(folder_name)
                s_folder_target_path = f"{base_target_path.rstrip('/')}/{s_folder_name}"
                
                task = CrossTransferTask(
                    source_account_id=request.source_account_id,
                    source_fid=request.source_fid,
                    source_file_name=folder_name,
                    source_file_size=0,
                    target_account_id=target_id,
                    target_path=s_folder_target_path,
                    status=CrossTransferTask.STATUS_PENDING,
                    is_folder=1,
                    current_step="等待开始"
                )
                db.add(task)
                db.commit()
                db.refresh(task)
                task_ids.append(task.id)
                task_broadcaster.broadcast({"type": "task_created", "task_id": task.id, "task": task.to_dict()})
                
            for tid in task_ids:
                from .cross_transfer import _do_execute_cross_transfer_task
                background_tasks.add_task(_do_execute_cross_transfer_task, tid)
                
            return CrossTransferResponse(code=200, message="空文件夹互传任务已启动", data={"task_ids": task_ids})
        
        folder_name = request.source_file_name or "未命名文件夹"
        
        # 多目标文件夹传输：创建三层结构
        if len(target_ids) > 1:
            # 创建顶层主任务 (master_task)
            master_task = CrossTransferTask(
                source_account_id=request.source_account_id,
                source_fid=request.source_fid,
                source_file_name=folder_name,
                target_account_id=target_ids[0],  # 使用第一个目标，仅用于记录
                target_path=request.target_path or "/",
                status=CrossTransferTask.STATUS_RUNNING,
                is_folder=1,
                is_master=1,
                total_targets=len(target_ids),
                completed_targets=0,
                total_files=len(file_list), # total_files 应该是源文件数量，而不是源文件数量 * 目标数量
                completed_files=0,
                current_step=f"多目标传输: {len(file_list)} 文件 → {len(target_ids)} 目标"
            )
            db.add(master_task)
            db.commit()
            db.refresh(master_task)
            task_ids.append(master_task.id)
            task_broadcaster.broadcast({"type": "task_created", "task_id": master_task.id, "task": master_task.to_dict()})
            
            # 为每个目标创建父任务
            parent_task_ids = []
            for target_id in target_ids:
                base_target_path = get_target_path(target_id)
                # 为每个目标网盘清理文件夹名称和路径（如百度脱敏 emoji）
                target_svc = get_svc(target_accounts[target_ids.index(target_id)])
                s_folder_name = target_svc.sanitize_path(folder_name)
                s_folder_target_path = f"{base_target_path.rstrip('/')}/{s_folder_name}"
                
                parent_task = CrossTransferTask(
                    source_account_id=request.source_account_id,
                    source_fid=request.source_fid,
                    source_file_name=folder_name,
                    target_account_id=target_id,
                    target_path=target_svc.sanitize_path(base_target_path),
                    status=CrossTransferTask.STATUS_PENDING,
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
                task_broadcaster.broadcast({"type": "task_created", "task_id": parent_task.id, "parent_id": master_task.id, "task": parent_task.to_dict()})
                # 为每个文件创建子任务
                for file_info in file_list:
                    relative_path = file_info.get("relative_path", "")
                    s_relative_path = target_svc.sanitize_path(relative_path)
                    
                    is_dir = file_info.get("is_dir", False)
                    if is_dir:
                        # 核心修复：如果是空目录，其目标路径就是拼接自身相对路径
                        file_target_path = f"{s_folder_target_path}/{s_relative_path}".rstrip('/')
                    else:
                        if "/" in s_relative_path:
                            file_target_path = f"{s_folder_target_path}/{s_relative_path.rsplit('/', 1)[0]}"
                        else:
                            file_target_path = s_folder_target_path
                    
                    # 核心修复：增加源文件名空值兜底
                    sfname = file_info.get("name") or file_info.get("file_name") or file_info.get("fileName") or f"未知文件_{file_info.get('fid')}"
                    
                    child_task = CrossTransferTask(
                        source_account_id=request.source_account_id,
                        source_fid=file_info.get("fid"),
                        source_file_name=sfname,
                        source_file_size=file_info.get("size", 0),
                        target_account_id=target_id,
                        target_path=file_target_path,
                        status=CrossTransferTask.STATUS_FAILED if file_info.get("is_sensitive") else CrossTransferTask.STATUS_PENDING,
                        error_message="涉及违规/敏感内容，无法传输" if file_info.get("is_sensitive") else "",
                        current_step="违规资源(已屏蔽)" if file_info.get("is_sensitive") else "等待开始",
                        parent_task_id=parent_task.id,
                        master_task_id=master_task.id,
                        is_folder=1 if file_info.get("is_dir") else 0
                    )
                    db.add(child_task)
                    db.flush() # 确保ID生成
                    task_broadcaster.broadcast({"type": "task_created", "task_id": child_task.id, "parent_id": parent_task.id, "task": child_task.to_dict()})
                    
                    if file_info.get("is_sensitive"):
                        logger.warning(f"检测到违规资源: {file_info.get('name')} (fid={file_info.get('fid')})，已自动标记为失败")
                
                db.commit()
            
            # 启动多目标文件夹共享下载传输
            background_tasks.add_task(execute_multi_target_folder_transfer, master_task.id, parent_task_ids)
        else:
            # 单目标文件夹传输：两层结构（与原来一致）
            target_id = target_ids[0]
            target_svc = get_svc(target_accounts[0])
            base_target_path = get_target_path(target_id)
            s_folder_name = target_svc.sanitize_path(folder_name)
            s_folder_target_path = f"{base_target_path.rstrip('/')}/{s_folder_name}"
            
            parent_task = CrossTransferTask(
                source_account_id=request.source_account_id,
                source_fid=request.source_fid,
                source_file_name=folder_name,
                target_account_id=target_id,
                target_path=target_svc.sanitize_path(base_target_path),
                status=CrossTransferTask.STATUS_RUNNING,
                is_folder=1,
                total_files=len(file_list),
                completed_files=0,
                current_step=f"准备传输 {len(file_list)} 个文件"
            )
            db.add(parent_task)
            db.commit()
            db.refresh(parent_task)
            task_ids.append(parent_task.id)
            task_broadcaster.broadcast({"type": "task_created", "task_id": parent_task.id, "task": parent_task.to_dict()})
            
            for file_info in file_list:
                relative_path = file_info.get("relative_path", "")
                s_relative_path = target_svc.sanitize_path(relative_path)
                if "/" in s_relative_path:
                    file_target_path = f"{s_folder_target_path}/{s_relative_path.rsplit('/', 1)[0]}"
                else:
                    file_target_path = s_folder_target_path
                
                # 核心修复：增加源文件名空值兜底
                sfname = file_info.get("name") or file_info.get("file_name") or file_info.get("fileName") or f"未知文件_{file_info.get('fid')}"
                
                child_task = CrossTransferTask(
                    source_account_id=request.source_account_id,
                    source_fid=file_info.get("fid"),
                    source_file_name=sfname,
                    source_file_size=file_info.get("size", 0),
                    target_account_id=target_id,
                    target_path=file_target_path,
                    status=CrossTransferTask.STATUS_FAILED if file_info.get("is_sensitive") else CrossTransferTask.STATUS_PENDING,
                    error_message="涉及违规/敏感内容，无法传输" if file_info.get("is_sensitive") else "",
                    current_step="违规资源(已屏蔽)" if file_info.get("is_sensitive") else "等待开始",
                    parent_task_id=parent_task.id,
                    is_folder=1 if file_info.get("is_dir") else 0
                )
                db.add(child_task)
                db.flush() # 确保ID生成
                task_broadcaster.broadcast({"type": "task_created", "task_id": child_task.id, "parent_id": parent_task.id, "task": child_task.to_dict()})

                if file_info.get("is_sensitive"):
                    logger.warning(f"检测到违规资源: {file_info.get('name')} (fid={file_info.get('fid')})，已自动标记为失败")
            
            db.commit()
            background_tasks.add_task(execute_folder_transfer_task, parent_task.id)

    else:
        # 普通文件传输
        if len(target_ids) > 1:
            # 多目标单文件传输：采用主任务结构以提升 UI 展示一致性并复用共享下载逻辑
            master_task = CrossTransferTask(
                source_account_id=request.source_account_id,
                source_fid=request.source_fid,
                source_file_name=request.source_file_name,
                target_account_id=target_ids[0], 
                target_path=request.target_path or "/",
                status=CrossTransferTask.STATUS_RUNNING,
                is_folder=0,
                is_master=1,
                total_targets=len(target_ids),
                completed_targets=0,
                total_files=1,
                completed_files=0,
                current_step=f"多目标转存: 1 文件 -> {len(target_ids)} 目标"
            )
            db.add(master_task)
            db.commit()
            db.refresh(master_task)
            task_ids.append(master_task.id)
            task_broadcaster.broadcast({"type": "task_created", "task_id": master_task.id, "task": master_task.to_dict()})
            
            parent_task_ids = []
            for target_id in target_ids:
                target_path = get_target_path(target_id)
                target_svc = get_svc(target_accounts[target_ids.index(target_id)])
                s_target_path = target_svc.sanitize_path(target_path)

                parent_task = CrossTransferTask(
                    source_account_id=request.source_account_id,
                    source_fid=request.source_fid,
                    source_file_name=request.source_file_name,
                    target_account_id=target_id,
                    target_path=s_target_path,
                    status=CrossTransferTask.STATUS_PENDING,
                    is_folder=0,
                    total_files=1,
                    completed_files=0,
                    master_task_id=master_task.id,
                    current_step="等待开始"
                )
                db.add(parent_task)
                db.commit()
                db.refresh(parent_task)
                parent_task_ids.append(parent_task.id)
                task_broadcaster.broadcast({"type": "task_created", "task_id": parent_task.id, "parent_id": master_task.id, "task": parent_task.to_dict()})
                
                child_task = CrossTransferTask(
                    source_account_id=request.source_account_id,
                    source_fid=request.source_fid,
                    source_file_name=request.source_file_name,
                    source_file_size=request.source_file_size or 0,
                    target_account_id=target_id,
                    target_path=s_target_path,
                    status=CrossTransferTask.STATUS_PENDING,
                    parent_task_id=parent_task.id,
                    master_task_id=master_task.id
                )
                db.add(child_task)
                db.flush() # 确保ID生成
                task_broadcaster.broadcast({"type": "task_created", "task_id": child_task.id, "parent_id": parent_task.id, "task": child_task.to_dict()})
            
            db.commit()
            # 统一使用 execute_multi_target_folder_transfer 执行共享转存（支持单/多文件）
            background_tasks.add_task(execute_multi_target_folder_transfer, master_task.id, parent_task_ids)
        else:
            # 单目标单文件：保持轻量化独立执行
            target_id = target_ids[0]
            target_svc = get_svc(target_accounts[0])
            s_target_path = target_svc.sanitize_path(get_target_path(target_id))
            
            task = CrossTransferTask(
                source_account_id=request.source_account_id,
                source_fid=request.source_fid,
                source_file_name=request.source_file_name,
                target_account_id=target_id,
                target_path=s_target_path,
                status=CrossTransferTask.STATUS_PENDING
            )
            db.add(task)
            db.commit()
            db.refresh(task)
            task_ids.append(task.id)
            task_broadcaster.broadcast({"type": "task_created", "task_id": task.id, "task": task.to_dict()})
            background_tasks.add_task(execute_cross_transfer_task, task.id)

    
    # 返回结果
    if len(task_ids) == 1:
        return {"task_id": task_ids[0], "message": "转存任务已启动"}
    else:
        msg = f"已创建 {len(task_ids)} 个转存任务"
        if request.is_folder:
            msg = f"文件夹传输已启动，共 {len(target_ids)} 个目标"
        return {"task_ids": task_ids, "message": msg}

@router.get("/events")
async def sse_events(request: Request):
    """SSE 任务状态变更推送通道 (诊断增强版)"""
    async def event_generator():
        logger.info(f"[SSE] 收到连接请求，准备建立流...")
        # 立即发送初始化消息，解决 Vite/Nginx 缓冲区导致的 onopen 延迟
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
                logger.info(f"[SSE] 连接被取消 (客户端断开)")
                pass
            except Exception as e:
                logger.error(f"[SSE] 推送异常: {e}")
            finally:
                logger.info(f"[SSE] 连接已关闭并清理资源")
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


@router.get("/tasks", response_model=TaskPaginationResponse)
async def get_cross_transfer_tasks(
    skip: int = 0, 
    limit: int = 20, 
    db: Session = Depends(get_db)
):
    """获取任务列表（支持两层和三层任务结构） - 分页版"""
    from sqlalchemy.orm import joinedload
    
    # 构建基础查询（排除链式转存创建的任务）
    query = db.query(CrossTransferTask).filter(
        CrossTransferTask.parent_task_id.is_(None),
        CrossTransferTask.master_task_id.is_(None),
        CrossTransferTask.transfer_type != CrossTransferTask.TRANSFER_CHAIN
    )

    # 获取总数
    total = query.count()

    # 获取当前页数据
    parent_tasks = query.options(
        joinedload(CrossTransferTask.source_account),
        joinedload(CrossTransferTask.target_account)
    ).order_by(CrossTransferTask.id.desc()).offset(skip).limit(limit).all()
    
    # print(f"[DEBUG] Fetched {len(parent_tasks)} top-level tasks")
    
    def task_to_dict(task, include_children=True, include_target_tasks=True):
        """将任务转换为字典 (增强热进度合并)"""
        task_dict = task.to_dict()
        
        # 【核心修复】合并内存中的“热进度”，确保护手动刷新不倒退
        if task.id in HOT_PROGRESS_CACHE:
            hot = HOT_PROGRESS_CACHE[task.id]
            # 仅在内存进度比数据库进度更新时使用
            if hot["progress"] > task_dict["progress"]:
                task_dict["progress"] = hot["progress"]
                task_dict["current_step"] = hot["current_step"]
                task_dict["status"] = hot.get("status", task_dict["status"])
            elif hot["progress"] == task_dict["progress"]:
                if hot["current_step"] != task_dict["current_step"]:
                    task_dict["current_step"] = hot["current_step"]
                if hot.get("status") is not None and hot["status"] != task_dict["status"]:
                    task_dict["status"] = hot["status"]
        
        return task_dict
    
    items = []
    for task in parent_tasks:
        task_dict = task_to_dict(task)
        
        # 三层结构：如果是主任务(is_master=1)，获取目标父任务
        if getattr(task, 'is_master', 0) == 1:
            target_parent_tasks = db.query(CrossTransferTask).options(
                joinedload(CrossTransferTask.source_account),
                joinedload(CrossTransferTask.target_account)
            ).filter(
                CrossTransferTask.master_task_id == task.id,
                CrossTransferTask.parent_task_id.is_(None)
            ).order_by(CrossTransferTask.id.asc()).all()
            
            if target_parent_tasks:
                target_tasks_list = []
                for tpt in target_parent_tasks:
                    tpt_dict = task_to_dict(tpt)
                    
                    # 获取该目标父任务的子文件任务
                    child_tasks = db.query(CrossTransferTask).options(
                        joinedload(CrossTransferTask.source_account),
                        joinedload(CrossTransferTask.target_account)
                    ).filter(
                        CrossTransferTask.parent_task_id == tpt.id
                    ).order_by(CrossTransferTask.id.asc()).all()
                    
                    if child_tasks:
                        tpt_dict["children"] = [task_to_dict(c) for c in child_tasks]
                    
                    target_tasks_list.append(tpt_dict)
                
                task_dict["target_tasks"] = target_tasks_list
        
        # 两层结构：普通文件夹任务，获取子任务
        elif task.is_folder == 1:
            child_tasks = db.query(CrossTransferTask).options(
                joinedload(CrossTransferTask.source_account),
                joinedload(CrossTransferTask.target_account)
            ).filter(
                CrossTransferTask.parent_task_id == task.id
            ).order_by(CrossTransferTask.id.asc()).all()
            
            if child_tasks:
                task_dict["children"] = [task_to_dict(c) for c in child_tasks]
        
        items.append(task_dict)
    
    return {"total": total, "items": items}


@router.post("/pause/{task_id}")
async def pause_task(task_id: int, db: Session = Depends(get_db)):
    """暂停任务（支持递归暂停子任务及下载器）"""
    from ..services.download_manager import get_downloader
    
    task = db.query(CrossTransferTask).filter(CrossTransferTask.id == task_id).first()
    if not task:
        raise HTTPException(404, "任务不存在")
        
    if task.status != CrossTransferTask.STATUS_RUNNING:
        raise HTTPException(400, "任务未运行")
        
    # 状态更新函数
    def do_pause(t):
        t.status = CrossTransferTask.STATUS_PAUSED
        t.current_step = "已暂停"
        # 如果是单文件传输，尝试暂停下载器
        if not t.is_folder and not t.is_master:
            downloader = get_downloader()
            dl_task_id = f"transfer_{t.id}"
            downloader.pause(dl_task_id)

    # 如果是主任务，递归暂停所有父任务和子任务
    if getattr(task, 'is_master', 0) == 1:
        children = db.query(CrossTransferTask).filter(CrossTransferTask.master_task_id == task.id).all()
        for c in children:
            if c.status == CrossTransferTask.STATUS_RUNNING:
                do_pause(c)
    # 如果是父任务，递归暂停其下的子文件任务
    elif task.is_folder == 1:
        children = db.query(CrossTransferTask).filter(CrossTransferTask.parent_task_id == task.id).all()
        for c in children:
            if c.status == CrossTransferTask.STATUS_RUNNING:
                do_pause(c)
    
    do_pause(task)
    db.commit()
    task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "status": task.status, "current_step": task.current_step})
    return {"message": "任务已暂停"}

@router.post("/resume/{task_id}")
async def resume_task(task_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """恢复任务（支持递归恢复并重启后台执行器）"""
    from ..services.download_manager import get_downloader
    
    task = db.query(CrossTransferTask).filter(CrossTransferTask.id == task_id).first()
    if not task:
        raise HTTPException(404, "任务不存在")
        
    if task.status != CrossTransferTask.STATUS_PAUSED:
        raise HTTPException(400, "任务非暂停状态")
        
    # 状态更新函数
    def do_resume(t):
        t.status = CrossTransferTask.STATUS_PENDING if (t.is_folder or t.is_master) else CrossTransferTask.STATUS_RUNNING
        t.current_step = "等待恢复..." if (t.is_folder or t.is_master) else "正在恢复..."
        if not t.is_folder and not t.is_master:
            downloader = get_downloader()
            dl_task_id = f"transfer_{t.id}"
            downloader.resume(dl_task_id)

    # 递归恢复状态
    if getattr(task, 'is_master', 0) == 1:
        children = db.query(CrossTransferTask).filter(CrossTransferTask.master_task_id == task.id).all()
        for c in children:
            if c.status == CrossTransferTask.STATUS_PAUSED:
                do_resume(c)
    elif task.is_folder == 1:
        children = db.query(CrossTransferTask).filter(CrossTransferTask.parent_task_id == task.id).all()
        for c in children:
            if c.status == CrossTransferTask.STATUS_PAUSED:
                do_resume(c)
    
    do_resume(task)
    db.commit()
    task_broadcaster.broadcast({"type": "task_updated", "task_id": task.id, "status": task.status, "current_step": task.current_step})
    
    # 重新提交合适的后台任务
    if getattr(task, 'is_master', 0) == 1:
        # 重启多目标传输循环
        pt_ids = [pt.id for pt in db.query(CrossTransferTask).filter(
            CrossTransferTask.master_task_id == task.id, 
            CrossTransferTask.parent_task_id.is_(None)
        ).all()]
        background_tasks.add_task(execute_multi_target_folder_transfer, task.id, pt_ids)
    elif task.is_folder == 1:
        # 重启单目标文件夹传输
        background_tasks.add_task(execute_folder_transfer_task, task.id)
    else:
        # 重启单文件传输
        background_tasks.add_task(execute_cross_transfer_task, task.id)
    
    return {"message": "任务已恢复"}

@router.post("/cancel/{task_id}")
async def cancel_task(task_id: int, db: Session = Depends(get_db)):
    """取消任务（支持递归取消子任务）"""
    from ..services.download_manager import get_downloader
    downloader = get_downloader()
    
    task = db.query(CrossTransferTask).filter(CrossTransferTask.id == task_id).first()
    if not task:
        raise HTTPException(404, "任务不存在")

    # 1. 识别并标记所有关联任务
    # 无论是取消主任务、父任务还是子任务，我们都通过 master_task_id 找到整个树
    master_id = task.master_task_id or task.id if task.is_master else task.master_task_id
    
    affected_tasks = []
    if task.is_master:
        # 取消整个转存组
        affected_tasks = db.query(CrossTransferTask).filter(
            (CrossTransferTask.id == task_id) | (CrossTransferTask.master_task_id == task_id)
        ).all()
    elif task.is_folder:
        # 取消当前目标文件夹及其下所有文件
        affected_tasks = db.query(CrossTransferTask).filter(
            (CrossTransferTask.id == task_id) | (CrossTransferTask.parent_task_id == task_id)
        ).all()
    else:
        # 仅取消单个文件任务
        affected_tasks = [task]

    # 批量更新状态
    for ct in affected_tasks:
        if ct.status in [CrossTransferTask.STATUS_PENDING, CrossTransferTask.STATUS_RUNNING, CrossTransferTask.STATUS_PAUSED]:
            ct.status = CrossTransferTask.STATUS_CANCELLED
            ct.error_message = "用户手动取消"
            ct.completed_at = datetime.now()
            ct.current_step = "已取消"
            
            # 触发异步取消事件
            if ct.id not in TASK_CANCEL_EVENTS:
                TASK_CANCEL_EVENTS[ct.id] = asyncio.Event()
            TASK_CANCEL_EVENTS[ct.id].set()
            
            # 同时尝试取消下载任务 (覆盖所有可能的 ID 模式)
            # 1. 单目标传输
            downloader.cancel(f"transfer_{ct.id}") 
            # 2. 多目标转存 (第一阶段下载 ID)
            downloader.cancel(f"multi_transfer_{ct.id}")
            # 3. 多目标文件夹 (Master 全局下载 ID)
            if ct.master_task_id:
                downloader.cancel(f"transfer_mt_{ct.master_task_id}_{ct.source_fid}")
                downloader.cancel(f"master_download_{ct.master_task_id}")
            # 4. 兜底原始 ID
            downloader.cancel(str(ct.id))
            
            # [实时加固] 立即向前端广播每一个子任务的状态变化
            task_broadcaster.broadcast({
                "type": "task_updated", 
                "task_id": ct.id, 
                "status": ct.status, 
                "status_name": ct.status_name,
                "current_step": ct.current_step,
                "error_message": ct.error_message,
                "progress": ct.progress
            })
            
    db.commit()

    # 3. 向上触发状态重算
    if task.parent_task_id:
        await recalculate_parent_status(task.parent_task_id, db)
    if task.master_task_id:
        await recalculate_master_status(task.master_task_id, db)
        
    return {"message": "任务及其子任务已取消并广播信号"}


@router.post("/retry/{task_id}")
async def retry_task(
    task_id: int, 
    background_tasks: BackgroundTasks, 
    db: Session = Depends(get_db)
):
    """重试失败的任务（支持主任务/父任务递归重置）"""
    task = db.query(CrossTransferTask).filter(CrossTransferTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    # 辅助函数：重置任务状态
    def reset_task(t):
        # 核心修复：清理之前的取消标记，防止重试时立即被取消
        if t.id in TASK_CANCEL_EVENTS:
            del TASK_CANCEL_EVENTS[t.id]
            
        # [核心修复] 清理内存进度缓存，防止重试时前端显示旧的 100% 进度
        if t.id in HOT_PROGRESS_CACHE:
            del HOT_PROGRESS_CACHE[t.id]
            
        t.status = CrossTransferTask.STATUS_PENDING
        t.error_message = None
        t.progress = 0
        t.current_step = "等待重试..."
        t.completed_at = None
        if t.is_folder or t.is_master:
            t.completed_files = 0
            if t.is_master:
                t.completed_targets = 0

    # 1. 如果是主任务 (Master Task)
    if task.is_master == 1:
        reset_task(task)
        task.status = CrossTransferTask.STATUS_RUNNING
        
        # 递归重置下属所有网盘父任务和文件子任务
        target_tasks = db.query(CrossTransferTask).filter(CrossTransferTask.master_task_id == task_id, CrossTransferTask.parent_task_id == None).all()
        for tt in target_tasks:
            reset_task(tt)
            
        child_tasks = db.query(CrossTransferTask).filter(CrossTransferTask.master_task_id == task_id, CrossTransferTask.parent_task_id != None).all()
        for ct in child_tasks:
            reset_task(ct)
            
        db.commit()
        # 重新启动多目标共享任务
        task_broadcaster.broadcast({"type": "task_updated", "task_id": task_id, "status": task.status})
        parent_task_ids = [tt.id for tt in target_tasks]
        background_tasks.add_task(execute_multi_target_folder_transfer, task.id, parent_task_ids)
        return {"message": "主任务及其关联子任务已重置并重新开始"}

    # 2. 如果是单网盘文件夹任务（可能是独立任务也可能是主任务的子任务）
    if task.is_folder == 1:
        reset_task(task)
        # 重置属于该文件夹的所有子任务
        children = db.query(CrossTransferTask).filter(CrossTransferTask.parent_task_id == task_id).all()
        for ct in children:
            reset_task(ct)
        
        # 如果有主任务，确保主任务状态为运行中
        if task.master_task_id:
            master = db.query(CrossTransferTask).filter(CrossTransferTask.id == task.master_task_id).first()
            if master:
                master.status = CrossTransferTask.STATUS_RUNNING
                master.completed_at = None
        
        db.commit()
        task_broadcaster.broadcast({"type": "task_updated", "task_id": task_id, "status": task.status})
        
        # 路由调度逻辑
        if task.master_task_id:
            # 多目标模式下的单网盘重试，依然使用共享下载函数，但仅传入当前父ID
            background_tasks.add_task(execute_multi_target_folder_transfer, task.master_task_id, [task.id])
        else:
            # 普通单目标文件夹
            background_tasks.add_task(execute_folder_transfer_task, task.id)
        return {"message": "列表任务已重置"}

    # 3. 普通单文件任务
    reset_task(task)
    
    # 向上同步状态：确保父/主任务不会处于完成态
    if task.parent_task_id:
        p = db.query(CrossTransferTask).filter(CrossTransferTask.id == task.parent_task_id).first()
        if p:
            p.status = CrossTransferTask.STATUS_RUNNING
            p.completed_at = None
    if task.master_task_id:
        m = db.query(CrossTransferTask).filter(CrossTransferTask.id == task.master_task_id).first()
        if m:
            m.status = CrossTransferTask.STATUS_RUNNING
            m.completed_at = None
            
    db.commit()
    task_broadcaster.broadcast({"type": "task_updated", "task_id": task_id, "status": task.status})
    background_tasks.add_task(execute_cross_transfer_task, task.id, is_child=False)
    return {"message": "文件任务已重新加入队列"}
