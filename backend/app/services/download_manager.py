"""
Python 协程并发下载管理器
支持：多协程并发下载、断点续传、暂停/恢复/取消
替代 aria2 实现
"""
import asyncio
import aiohttp
import aiofiles
import os
import json
import hashlib
from typing import Dict, Any, Optional, Callable, List
from enum import Enum
from dataclasses import dataclass, field, asdict
from datetime import datetime
import traceback


class DownloadStatus(Enum):
    """下载状态"""
    PENDING = "pending"      # 等待中
    DOWNLOADING = "downloading"  # 下载中
    PAUSED = "paused"        # 已暂停
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"        # 失败
    CANCELLED = "cancelled"  # 已取消


@dataclass
class ChunkInfo:
    """分片信息"""
    index: int
    start: int
    end: int
    downloaded: int = 0
    completed: bool = False


@dataclass
class DownloadTask:
    """下载任务"""
    task_id: str
    url: str
    output_path: str
    file_size: int
    headers: Dict[str, str] = field(default_factory=dict)
    
    # [新增] 自定义分片获取函数 (用于处理复杂下载逻辑，如镜像重试、CurlSession等)
    # async def fetcher(start: int, end: int, chunk_idx: int) -> Optional[bytes]
    custom_chunk_fetcher: Optional[Callable[[int, int, int], Any]] = None
    
    # 状态
    status: DownloadStatus = DownloadStatus.PENDING
    downloaded_bytes: int = 0
    speed: float = 0.0
    error_message: str = ""
    
    # 分片信息
    chunks: List[ChunkInfo] = field(default_factory=list)
    chunk_size: int = 8 * 1024 * 1024  # 优化：8MB per chunk
    concurrency: int = 8  # 优化：默认 8 并发 (降低被封禁风险)
    
    # 控制
    _cancel_event: asyncio.Event = field(default=None, repr=False)
    _pause_event: asyncio.Event = field(default=None, repr=False)
    
    def __post_init__(self):
        self._cancel_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # 初始未暂停
    
    @property
    def progress(self) -> float:
        if self.file_size == 0:
            return 0
        return (self.downloaded_bytes / self.file_size) * 100
    
    def to_dict(self) -> Dict:
        return {
            "task_id": self.task_id,
            "url": self.url[:50] + "..." if len(self.url) > 50 else self.url,
            "output_path": self.output_path,
            "file_size": self.file_size,
            "status": self.status.value,
            "downloaded_bytes": self.downloaded_bytes,
            "progress": round(self.progress, 2),
            "speed": self.speed,
            "error_message": self.error_message,
        }


class ConcurrentDownloader:
    """并发下载器"""
    
    def __init__(self, state_dir: str = "temp_data/download_state"):
        self.state_dir = state_dir
        self.tasks: Dict[str, DownloadTask] = {}
        os.makedirs(state_dir, exist_ok=True)
    
    def _get_state_path(self, task_id: str) -> str:
        return os.path.join(self.state_dir, f"{task_id}.json")
    
    def _save_state(self, task: DownloadTask, force: bool = False):
        """保存任务状态 (Throttle 优化)"""
        # 如果分片数量巨大 (如 > 500)，且不是强制保存，则降低保存频率
        if not force and len(task.chunks) > 500:
             # 对于超多小分块任务，每 100MB 或是 5% 的进度变化才物理写入一次
             chunk_progress = task.downloaded_bytes / task.file_size if task.file_size > 0 else 0
             last_save_progress = getattr(task, '_last_save_progress', 0)
             
             if (task.downloaded_bytes - getattr(task, '_last_save_bytes', 0) < 100 * 1024 * 1024) and (chunk_progress - last_save_progress < 0.05):
                 return
             
             task._last_save_bytes = task.downloaded_bytes
             task._last_save_progress = chunk_progress

        state = {
            "task_id": task.task_id,
            "url": task.url,
            "output_path": task.output_path,
            "file_size": task.file_size,
            "headers": task.headers,
            "status": task.status.value,
            "downloaded_bytes": task.downloaded_bytes,
            "chunks": [asdict(c) for c in task.chunks],
        }
        try:
            with open(self._get_state_path(task.task_id), 'w') as f:
                json.dump(state, f)
        except:
            pass
    
    def _load_state(self, task_id: str) -> Optional[Dict]:
        """加载任务状态"""
        path = self._get_state_path(task_id)
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    return json.load(f)
            except:
                return None
        return None
    
    def _clean_state(self, task_id: str):
        """清理状态文件"""
        path = self._get_state_path(task_id)
        if os.path.exists(path):
            try:
                os.remove(path)
            except:
                pass
    
    async def create_task(
        self,
        task_id: str,
        url: str,
        output_path: str,
        headers: Dict[str, str] = None,
        concurrency: int = 8,
        custom_chunk_fetcher: Optional[Callable[[int, int, int], Any]] = None,
        file_size: int = 0,
        chunk_size: int = 0  # [新增] 可选参数，自定义分片大小
    ) -> DownloadTask:
        """创建下载任务"""
        # 检查是否有已保存的状态（断点续传）
        saved_state = self._load_state(task_id)
        
        if saved_state and saved_state.get("status") in ["paused", "downloading", "failed"]:
            # 恢复任务
            task = DownloadTask(
                task_id=task_id,
                url=saved_state["url"],
                output_path=saved_state["output_path"],
                file_size=saved_state["file_size"],
                headers=saved_state.get("headers", {}),
                downloaded_bytes=saved_state.get("downloaded_bytes", 0),
                concurrency=concurrency,
                custom_chunk_fetcher=custom_chunk_fetcher
            )
            task.chunks = [ChunkInfo(**c) for c in saved_state.get("chunks", [])]
            task.status = DownloadStatus.PAUSED
        else:
            # 新任务 - 获取文件大小
            if file_size <= 0:
                try:
                    file_size = await self._get_file_size(url, headers or {})
                except Exception as e:
                    # 某些情况下，如果是自定义Fetcher，可能不需要预检URL？
                    # 但为了分片，必须知道大小。
                    raise e
            
            task = DownloadTask(
                task_id=task_id,
                url=url,
                output_path=output_path,
                file_size=file_size,
                headers=headers or {},
                concurrency=concurrency,
                custom_chunk_fetcher=custom_chunk_fetcher,
                chunk_size=chunk_size if chunk_size > 0 else 8 * 1024 * 1024
            )
            
            # 初始化分片
            task.chunks = self._create_chunks(task.file_size, task.chunk_size)
        
        self.tasks[task_id] = task
        self._save_state(task)
        return task
    
    async def _get_file_size(self, url: str, headers: Dict[str, str]) -> int:
        """获取文件大小（带错误检查）"""
        async with aiohttp.ClientSession() as session:
            # 先尝试 HEAD
            try:
                async with session.head(url, headers=headers, allow_redirects=True, timeout=15) as resp:
                    size = int(resp.headers.get("Content-Length", 0))
                    if size > 1024:
                        return size
            except:
                pass
            
            # 如果 HEAD 获取失败或大小很小，尝试 GET (只获取前 1KB)
            try:
                # 使用 range 获取前 1024 字节来检查
                check_headers = {**headers, "Range": "bytes=0-1023"}
                async with session.get(url, headers=check_headers, allow_redirects=True, timeout=30) as resp:
                    if resp.status not in [200, 206]:
                        raise Exception(f"HTTP {resp.status}")
                        
                    content = await resp.read()
                    
                    # 检查是否是 JSON 错误
                    if b'"errno"' in content or b'"error_code"' in content:
                        try:
                            err_json = json.loads(content)
                            if "errno" in err_json or "error_code" in err_json:
                                raise Exception(f"服务端返回错误: {content.decode('utf-8')[:200]}")
                        except:
                            pass
                    
                    # 检查是否是 HTML 错误页
                    if b'<html' in content or b'<!DOCTYPE html' in content:
                        if '<title>百度网盘-链接不存在</title>'.encode('utf-8') in content:
                             raise Exception("链接已失效或需要验证码")
                        raise Exception(f"服务端返回 HTML 页面而非文件 (可能未登录或连接失效): {content.decode('utf-8')[:200]}")

                    # 如果不是错误，返回总大小 (Content-Range: bytes 0-1023/888888)
                    content_range = resp.headers.get("Content-Range", "")
                    if "/" in content_range:
                        return int(content_range.split("/")[-1])
                    
                    # 如果没有 Content-Range，可能是小文件
                    if size > 0:
                        return size
                    
                    return len(content)
            except Exception as e:
                raise Exception(f"获取文件大小失败: {str(e)}")
    
    def _create_chunks(self, file_size: int, chunk_size: int) -> List[ChunkInfo]:
        """创建分片列表"""
        chunks = []
        index = 0
        start = 0
        
        while start < file_size:
            end = min(start + chunk_size - 1, file_size - 1)
            chunks.append(ChunkInfo(index=index, start=start, end=end))
            start = end + 1
            index += 1
        
        return chunks
    
    async def start(
        self,
        task_id: str,
        progress_callback: Callable[[int, int, float], None] = None
    ) -> str:
        """开始/恢复下载
        
        Returns:
            下载完成的文件路径
        """
        task = self.tasks.get(task_id)
        if not task:
            raise Exception(f"任务不存在: {task_id}")
        
        if task.status == DownloadStatus.COMPLETED:
            if os.path.exists(task.output_path) and os.path.getsize(task.output_path) == task.file_size:
                return task.output_path
            else:
                # 文件不存在或大小不对，重置状态
                task.status = DownloadStatus.PENDING
                for c in task.chunks:
                    c.completed = False
                    c.downloaded = 0
                task.downloaded_bytes = 0
        
        task.status = DownloadStatus.DOWNLOADING
        task._pause_event.set()
        task._cancel_event.clear()
        
        # [新增] 立即通报一次进度，确保 UI 状态切换到“下载中”
        if progress_callback:
            await progress_callback(task.downloaded_bytes, task.file_size, task.speed)
        
        session = None
        try:
            # 确保输出目录存在
            os.makedirs(os.path.dirname(task.output_path), exist_ok=True)
            
            # 创建/打开输出文件
            if not os.path.exists(task.output_path):
                # 预分配文件
                async with aiofiles.open(task.output_path, 'wb') as f:
                    await f.seek(task.file_size - 1)
                    await f.write(b'\0')
            
            # 获取未完成的分片
            pending_chunks = [c for c in task.chunks if not c.completed]
            
            # 如果全部已下载
            if not pending_chunks:
                task.status = DownloadStatus.COMPLETED
                self._clean_state(task_id)
                return task.output_path

            # 创建任务队列
            queue = asyncio.Queue()
            for chunk in pending_chunks:
                queue.put_nowait(chunk)
            
            # 创建共享 Session (仅当没有自定义 Fetcher 时才需要)
            if not task.custom_chunk_fetcher:
                timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=300)
                connector = aiohttp.TCPConnector(limit=task.concurrency, ssl=False)
                session = aiohttp.ClientSession(timeout=timeout, connector=connector)
            
            # 状态追踪
            last_update = asyncio.get_event_loop().time()
            last_downloaded = task.downloaded_bytes
            
            async def worker(worker_id: int):
                nonlocal last_update, last_downloaded
                while True:
                    # [Cancel Check 1]
                    if task._cancel_event.is_set(): return
                        
                    # [Pause Check]
                    if not task._pause_event.is_set():
                        await task._pause_event.wait()
                    
                    try:
                        chunk = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    
                    # [Cancel Check 2]
                    if task._cancel_event.is_set():
                        queue.task_done()
                        return

                    # 块内重试循环
                    retries = 0
                    max_retries = 10
                    while retries < max_retries:
                        if task._cancel_event.is_set():
                            queue.task_done()
                            return

                        # 记录当前下载量
                        initial_downloaded = chunk.downloaded

                        try:
                            # 计算实际下载范围
                            actual_start = chunk.start + chunk.downloaded
                            if actual_start > chunk.end:
                                chunk.completed = True
                                break # 完成此块
                            
                            # [自定义 Fetcher 逻辑]
                            if task.custom_chunk_fetcher:
                                if task._cancel_event.is_set(): return
                                
                                # 🚀 深度对齐：传入 worker_id 以便 Fetcher 实现物理级隔离的长连接客户端
                                try:
                                    # 尝试以 worker_id 参数调用
                                    chunk_data = await task.custom_chunk_fetcher(
                                        actual_start, 
                                        chunk.end, 
                                        chunk.index, 
                                        worker_id=worker_id
                                    )
                                except TypeError:
                                    # 兼容性兜底：如果 Fetcher 不支持 worker_id 参数
                                    chunk_data = await task.custom_chunk_fetcher(
                                        actual_start, 
                                        chunk.end, 
                                        chunk.index
                                    )
                                
                                # 检查是否因为取消而返回 None
                                if task._cancel_event.is_set(): return
                                
                                if chunk_data:
                                    # 写入文件
                                    if task._cancel_event.is_set(): return # 写入前最后一次检查
                                    async with aiofiles.open(task.output_path, 'r+b') as f:
                                        await f.seek(actual_start)
                                        await f.write(chunk_data)
                                    
                                    if task._cancel_event.is_set(): return # 写入后检查
                                    
                                    # 更新进度
                                    data_len = len(chunk_data)
                                    chunk.downloaded += data_len
                                    task.downloaded_bytes += data_len
                                    chunk.completed = True

                                    # [新增] 更新速度和回调
                                    now = asyncio.get_event_loop().time()
                                    if now - last_update >= 1.0:
                                        elapsed = now - last_update
                                        if elapsed > 0:
                                            task.speed = (task.downloaded_bytes - last_downloaded) / elapsed
                                            last_downloaded = task.downloaded_bytes
                                            last_update = now
                                            
                                            # 异步保存状态 (非强制)
                                            from fastapi.concurrency import run_in_threadpool
                                            await run_in_threadpool(self._save_state, task)
                                            
                                            if progress_callback:
                                                try:
                                                    # 增加 200ms 超时保护，防止回调阻塞下载 Worker
                                                    await asyncio.wait_for(progress_callback(task.downloaded_bytes, task.file_size, task.speed), timeout=0.2)
                                                except:
                                                    pass
                                    break 
                                else:
                                    raise Exception("Fetcher returned no data (possibly interrupted)")

                            # [默认 aiohttp 逻辑]
                            elif session:
                                headers = {**task.headers, "Range": f"bytes={actual_start}-{chunk.end}"}
                                async with session.get(task.url, headers=headers) as resp:
                                    if resp.status == 200:
                                         if not (chunk.index == 0 and len(task.chunks) == 1):
                                             raise Exception("服务端不支持并发分片下载 (200 OK)")
                                    elif resp.status != 206:
                                        raise Exception(f"HTTP {resp.status}")
                                    
                                    async with aiofiles.open(task.output_path, 'r+b') as f:
                                        await f.seek(actual_start)
                                        
                                        async for data in resp.content.iter_chunked(64 * 1024):
                                            if task._cancel_event.is_set(): return
                                            if not task._pause_event.is_set():
                                                await task._pause_event.wait()
                                            
                                            await f.write(data)
                                            chunk.downloaded += len(data)
                                            task.downloaded_bytes += len(data)
                                            
                                            # 更新速度
                                            now = asyncio.get_event_loop().time()
                                            if now - last_update >= 1.0:
                                                elapsed = now - last_update
                                                if elapsed > 0:
                                                    task.speed = (task.downloaded_bytes - last_downloaded) / elapsed
                                                    last_downloaded = task.downloaded_bytes
                                                    last_update = now
                                                    
                                                    # 异步保存状态
                                                    from fastapi.concurrency import run_in_threadpool
                                                    await run_in_threadpool(self._save_state, task)
                                                    
                                                    if progress_callback:
                                                        try:
                                                            await asyncio.wait_for(progress_callback(task.downloaded_bytes, task.file_size, task.speed), timeout=0.2)
                                                        except:
                                                            pass
                                    
                                    # 下载完成（流结束）
                                    if chunk.start + chunk.downloaded > chunk.end:
                                        chunk.completed = True
                                        break 
                                    
                                    # 未完成：检查是否有进度
                                    if chunk.downloaded > initial_downloaded:
                                        retries = 0
                                    else:
                                        raise Exception("连接中断且无数据接收")
                            
                        except Exception as e:
                            # [Cancel Check 3]
                            if task._cancel_event.is_set(): return

                            retries += 1
                            if retries >= max_retries:
                                logger.error(f"分片 {chunk.index} 最终失败: {e}")
                                raise e
                            else:
                                await asyncio.sleep(retries * 0.5)
                                continue
                    
                    queue.task_done()
            
            # 启动 Workers
            workers = [asyncio.create_task(worker(i)) for i in range(task.concurrency)]
            
            # 等待所有 Worker 结束 (BaseException 用于捕获 CancelledError)
            results = await asyncio.gather(*workers, return_exceptions=True)
            
            # 严重：检查是否有异常（如果是 CancelledError 则不视为错误）
            for res in results:
                if isinstance(res, asyncio.CancelledError):
                    # 明确标记取消
                    task._cancel_event.set()
                elif isinstance(res, Exception):
                    raise res
            
            # 检查是否取消
            if task._cancel_event.is_set():
                task.status = DownloadStatus.CANCELLED
                self._clean_state(task_id)
                # 可选：清理未下载完成的文件？
                # 用户要求取消后不残留。
                if os.path.exists(task.output_path):
                    try:
                        os.remove(task.output_path)
                    except:
                        pass
                return None
            
            # 验证所有分片
            if all(c.completed for c in task.chunks):
                task.status = DownloadStatus.COMPLETED
                self._clean_state(task_id)
                return task.output_path
            
            task.status = DownloadStatus.FAILED
            task.error_message = "下载未完全完成"
            self._save_state(task)
            return None
        
        except Exception as e:
            # traceback.print_exc()
            task.status = DownloadStatus.FAILED
            task.error_message = str(e)
            self._save_state(task)
            raise
        finally:
            if session:
                await session.close()
    
    def pause(self, task_id: str):
        """暂停下载"""
        task = self.tasks.get(task_id)
        if task and task.status == DownloadStatus.DOWNLOADING:
            task._pause_event.clear()
            task.status = DownloadStatus.PAUSED
            self._save_state(task)
    
    def resume(self, task_id: str):
        """恢复下载"""
        task = self.tasks.get(task_id)
        if task and task.status == DownloadStatus.PAUSED:
            task._pause_event.set()
            task.status = DownloadStatus.DOWNLOADING
    
    def cancel(self, task_id: str):
        """取消下载"""
        task = self.tasks.get(task_id)
        if task:
            task._cancel_event.set()
            task._pause_event.set()  # 解除暂停阻塞
            task.status = DownloadStatus.CANCELLED
            
            # 清理文件
            if os.path.exists(task.output_path):
                try:
                    os.remove(task.output_path)
                except:
                    pass
            self._clean_state(task_id)
    
    def get_task(self, task_id: str) -> Optional[DownloadTask]:
        """获取任务"""
        return self.tasks.get(task_id)


# 全局下载器实例
_downloader: Optional[ConcurrentDownloader] = None


def get_downloader() -> ConcurrentDownloader:
    """获取下载器单例"""
    global _downloader
    if _downloader is None:
        _downloader = ConcurrentDownloader()
    return _downloader
