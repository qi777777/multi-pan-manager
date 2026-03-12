from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from fastapi.responses import StreamingResponse
from sqlalchemy import text, inspect
from sqlalchemy.orm import Session
from typing import List, Optional
import asyncio
import json

from ..database import get_db, engine
from ..core.logger import log_manager, logger

router = APIRouter()

@router.get("/logs")
async def stream_logs():
    """Server-Sent Events 实时推送日志"""
    async def log_generator():
        # 立即发送握手消息，解决代理层缓冲区导致的延迟
        yield f"data: {json.dumps({'type': 'connected'})}\n\n"
        
        # 先发送最近的缓存日志
        for log in log_manager.logs:
            yield f"data: {log}\n\n"
        
        # 订阅后续日志
        queue = log_manager.subscribe()
        try:
            while True:
                log = await queue.get()
                yield f"data: {log}\n\n"
        finally:
            log_manager.unsubscribe(queue)

    return StreamingResponse(
        log_generator(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@router.get("/db/tables")
async def get_tables():
    """获取所有数据库表名 (排除敏感的 users 表)"""
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    return [t for t in tables if t != "users"]

class BatchDeleteRequest(BaseModel):
    ids: List[int]

class ExecuteSqlRequest(BaseModel):
    sql: str

@router.post("/db/execute-sql")
async def execute_sql(request: ExecuteSqlRequest, db: Session = Depends(get_db)):
    """执行原生 SQL 语句"""
    if not request.sql.strip():
        raise HTTPException(status_code=400, detail="SQL 语句不能为空")

    # 简单的写操作警告/记录可以在此处增加
    try:
        result = db.execute(text(request.sql))
        db.commit()

        # 如果有返回行（如 SELECT）
        if result.returns_rows:
            columns = result.keys()
            data = [dict(zip(columns, row)) for row in result.fetchall()]
            return {
                "type": "query",
                "columns": list(columns),
                "data": data,
                "total": len(data)
            }
        else:
            # 如 INSERT, UPDATE, DELETE
            return {
                "type": "execute",
                "affected_rows": result.rowcount,
                "message": f"执行成功，受影响行数: {result.rowcount}"
            }
    except Exception as e:
        db.rollback()
        logger.error(f"SQL 执行失败: {e}\nSQL: {request.sql}")
        raise HTTPException(status_code=400, detail=f"SQL 执行失败: {str(e)}")

@router.get("/db/metadata")
async def get_db_metadata():
    """获取数据库表和字段的中文元数据及枚举映射"""
    return {
        "disk_accounts": {
            "name": "网盘账户",
            "fields": {
                "id": "ID",
                "name": "账户名称",
                "type": "网盘类型",
                "credentials": "凭证(加密)",
                "storage_path": "存储路径",
                "storage_path_temp": "临时路径",
                "status": "账户状态",
                "last_check_at": "最后检测",
                "created_at": "创建时间",
                "updated_at": "更新时间",
                "cached_token": "缓存Token",
                "token_expires_at": "令牌有效期",
                "config": "扩展配置"
            },
            "enums": {
                "type": {0: "夸克网盘", 1: "阿里云盘", 2: "百度网盘", 3: "UC网盘", 4: "迅雷云盘"},
                "status": {1: "🟢 正常", 0: "🔴 禁用", 2: "🟠 凭证过期"}
            }
        },
        "shares": {
            "name": "分享记录",
            "fields": {
                "id": "ID",
                "account_id": "账户ID",
                "share_id": "网盘分享ID",
                "share_url": "分享链接",
                "title": "资源标题",
                "password": "提取码",
                "fid": "文件ID串",
                "expired_at": "过期时间",
                "status": "有效状态",
                "expired_type": "时长类型",
                "file_path": "对应网盘路径",
                "created_at": "创建时间"
            },
            "enums": {
                "status": {1: "✅ 有效", 0: "❌ 已失效"},
                "expired_type": {1: "永久", 2: "7天", 3: "1天", 4: "30天"}
            }
        },
        "transfer_tasks": {
            "name": "转存任务",
            "fields": {
                "id": "ID",
                "source_url": "源分享链接",
                "source_type": "源网盘类型",
                "source_code": "提取码",
                "target_account_id": "目标账户ID",
                "storage_path": "存储路径",
                "parent_task_id": "父任务ID",
                "chain_status": "链式描述",
                "status": "执行状态",
                "result_share_url": "生成的结果链接",
                "result_fid": "结果文件ID",
                "result_title": "资源标题",
                "error_message": "错误信息",
                "created_at": "创建时间",
                "completed_at": "完成时间"
            },
            "enums": {
                "status": {0: "⏳ 待处理", 1: "⚙️ 进行中", 2: "✨ 成功", 3: "❌ 失败"},
                "source_type": {0: "夸克", 1: "阿里", 2: "百度", 3: "UC", 4: "迅雷"}
            }
        },
        "cross_transfer_tasks": {
            "name": "跨网盘互传",
            "fields": {
                "id": "ID",
                "source_account_id": "源账户ID",
                "source_fid": "源文件ID",
                "source_file_name": "文件名",
                "source_file_size": "文件大小",
                "source_file_md5": "文件MD5",
                "target_account_id": "目标账户ID",
                "target_path": "目标路径",
                "status": "状态",
                "transfer_type": "传输方式",
                "result_fid": "结果FID",
                "result_path": "结果全路径",
                "error_message": "错误详情",
                "progress": "进度(%)",
                "current_step": "当前执行步骤",
                "parent_task_id": "父任务ID",
                "is_folder": "任务对象类型",
                "total_files": "总包含文件数",
                "completed_files": "已传输文件数",
                "source_folder_path": "原始目录路径",
                "master_task_id": "所属总任务ID",
                "is_master": "是否为任务组长",
                "total_targets": "分发账号总数",
                "completed_targets": "已完成分发数",
                "created_at": "创建时间",
                "completed_at": "完成时间"
            },
            "enums": {
                "status": {0: "⏳ 待处理", 1: "⚙️ 传输中", 2: "✅ 成功", 3: "❌ 失败", 4: "⏸️ 暂停", 5: "⚠️ 部分成功", 6: "🚫 已取消"},
                "transfer_type": {0: "🚀 秒传", 1: "☁️ 普通互传", 2: "📥 下载后上传", 3: "🔗 链式触发"},
                "is_folder": {0: "📄 文件", 1: "📂 文件夹"},
                "is_master": {1: "👑 总任务", 0: "🧩 子/普通任务"}
            }
        }
    }

@router.post("/db/{table_name}/batch-delete")
async def batch_delete_records(table_name: str, request: BatchDeleteRequest, db: Session = Depends(get_db)):
    """批量删除指定表的记录"""
    # 禁止操作用户表
    if table_name == "users":
        raise HTTPException(status_code=403, detail="Access to 'users' table is denied")
        
    allowed_tables = inspect(engine).get_table_names()
    if table_name not in allowed_tables:
        raise HTTPException(status_code=404, detail="Table not found")
    
    if not request.ids:
        return {"message": "未选择记录"}
        
    try:
        # 使用 IN 语句进行批量删除
        sql = f"DELETE FROM {table_name} WHERE id IN ({','.join([':id' + str(i) for i in range(len(request.ids))])})"
        params = {f"id{i}": id_val for i, id_val in enumerate(request.ids)}
        db.execute(text(sql), params)
        db.commit()
        return {"message": f"成功删除 {len(request.ids)} 条记录"}
    except Exception as e:
        db.rollback()
        logger.error(f"Batch delete error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/db/{table_name}")
async def get_table_data(
    table_name: str, 
    skip: int = 0, 
    limit: int = 50, 
    db: Session = Depends(get_db)
):
    """获取指定表的数据"""
    # 禁止访问用户表
    if table_name == "users":
        raise HTTPException(status_code=403, detail="Access to 'users' table is denied")
        
    # 安全检查：只允许访问特定的模型表
    allowed_tables = inspect(engine).get_table_names()
    if table_name not in allowed_tables:
        throw_msg = f"Table {table_name} not found or access denied"
        raise HTTPException(status_code=404, detail=throw_msg)

    try:
        # 使用原生 SQL 进行分页查询
        result = db.execute(text(f"SELECT * FROM {table_name} LIMIT :limit OFFSET :skip"), {"limit": limit, "skip": skip})
        columns = result.keys()
        data = [dict(zip(columns, row)) for row in result.fetchall()]
        
        # 获取总数
        total = db.execute(text(f"SELECT COUNT(*) FROM {table_name}")).scalar()
        
        return {"total": total, "data": data}
    except Exception as e:
        logger.error(f"Error fetching table {table_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/db/{table_name}/{id}")
async def delete_record(table_name: str, id: int, db: Session = Depends(get_db)):
    """删除指定表的记录"""
    # 禁止操作用户表
    if table_name == "users":
        raise HTTPException(status_code=403, detail="Access to 'users' table is denied")
        
    allowed_tables = inspect(engine).get_table_names()
    if table_name not in allowed_tables:
        raise HTTPException(status_code=404, detail="Table not found")
    
    try:
        db.execute(text(f"DELETE FROM {table_name} WHERE id = :id"), {"id": id})
        db.commit()
        return {"message": "删除成功"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/db/{table_name}/{id}")
async def update_record(table_name: str, id: int, data: dict, db: Session = Depends(get_db)):
    """更新指定表的记录 (通用字段更新)"""
    # 禁止操作用户表
    if table_name == "users":
        raise HTTPException(status_code=403, detail="Access to 'users' table is denied")
        
    allowed_tables = inspect(engine).get_table_names()
    if table_name not in allowed_tables:
        raise HTTPException(status_code=404, detail="Table not found")
    
    if not data:
        raise HTTPException(status_code=400, detail="No data provided")

    try:
        # 构建动态更新 SQL，排除 id 字段
        fields = [f"{k} = :{k}" for k in data.keys() if k != 'id']
        if not fields:
             return {"message": "无数据更新"}
             
        sql = f"UPDATE {table_name} SET {', '.join(fields)} WHERE id = :id"
        db.execute(text(sql), {**data, "id": id})
        db.commit()
        return {"message": "更新成功"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
