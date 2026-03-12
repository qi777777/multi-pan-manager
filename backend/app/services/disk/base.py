from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
import httpx
from ...core.logger import logger


class BaseDiskService(ABC):
    """网盘服务基类"""
    
    def __init__(self, credentials: str, config: Dict[str, Any] = None):
        self.credentials = credentials
        self.config = config or {}
        self.storage_path = self.config.get("storage_path", "")
        self.storage_path_temp = self.config.get("storage_path_temp", "")
        self.verify = self.config.get("verify_ssl", True)
        self.headers = self._build_headers()
    
    @abstractmethod
    def _build_headers(self) -> Dict[str, str]:
        """构建请求头"""
        pass
    
    @abstractmethod
    async def get_files(self, pdir_fid: str = "0") -> Dict[str, Any]:
        """获取文件列表"""
        pass
    
    @abstractmethod
    async def transfer(self, share_url: str, code: str = "", expired_type: int = 1, need_share: bool = True) -> Dict[str, Any]:
        """转存分享资源"""
        pass
    
    @abstractmethod
    async def create_share(self, fid_list: List[str], title: str, expired_type: int = 1) -> Dict[str, Any]:
        """创建分享"""
        pass
    
    @abstractmethod
    async def delete_files(self, fid_list: List[str]) -> Dict[str, Any]:
        """删除文件"""
        pass
    
    @abstractmethod
    async def check_status(self) -> bool:
        """检查登录状态"""
        pass
    
    @abstractmethod
    async def create_folder(self, folder_name: str, pdir_fid: str = "0") -> Dict[str, Any]:
        """创建文件夹"""
        pass
    
    def sanitize_path(self, path: str) -> str:
        """清理网盘不支持的路径字符（如百度网盘不支持 emoji）"""
        return path
    
    async def upload_file(self, file_data: Any, file_name: str, pdir_fid: str = "0", progress_callback=None, check_cancel=None) -> Dict[str, Any]:
        """上传文件"""
        return self.error("此网盘暂不支持上传功能")
    
    async def download_file(self, fid: str, save_path: str, progress_callback=None, task_id: str = None) -> bool:
        """下载完整文件到本地路径的通用实现"""
        import aiofiles
        import time
        
        info_res = await self.get_file_download_info(fid)
        if info_res.get("code") != 200:
            logger.error(f"[BASE] 获取下载信息失败: {info_res.get('message')}")
            return False
            
        data = info_res["data"]
        url = data["download_url"]
        total_size = data["size"]
        
        try:
            # 基础 Header 包含鉴权
            headers = self.headers.copy()
            # 增加 Range 头以确保某些 CDN 节点正常工作
            headers["Range"] = f"bytes=0-{total_size-1}" if total_size > 0 else "bytes=0-"
            
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=600), follow_redirects=True) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code not in [200, 206]:
                        logger.error(f"[BASE] 下载文件失败: HTTP {resp.status_code}")
                        return False
                    
                    downloaded = 0
                    async with aiofiles.open(save_path, 'wb') as f:
                        async for chunk in resp.aiter_bytes(chunk_size=1024*1024):
                            if not chunk: continue
                            await f.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback:
                                await progress_callback(downloaded, total_size)
            return True
        except Exception as e:
            logger.error(f"[BASE] 通用下载异常: {str(e)}")
            return False

    async def get_file_download_info(self, fid: str) -> Dict[str, Any]:
        """获取文件下载信息（下载链接、MD5等）"""
        return self.error("此网盘不支持获取下载信息")
    async def download_slice(self, url: str, offset: int, length: int) -> Optional[bytes]:
        """下载文件分片
        
        Args:
            url: 下载链接
            offset: 偏移量
            length: 分片长度
        
        Returns:
            bytes 数据或 None
        """
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                headers = {'Range': f'bytes={offset}-{offset + length - 1}'}
                headers.update(self.headers)
                response = await client.get(url, headers=headers)
                if response.status_code in [200, 206]:
                    return response.content
                else:
                    logger.error(f"[BASE] 下载分片失败: {response.status_code}, {url}")
        except Exception as e:
            logger.error(f"[BASE] 下载分片异常: {str(e)}")
        return None
    
    async def rapid_upload(self, file_info: Dict, slice_md5: str, chunk_data: bytes, offset: int) -> Dict[str, Any]:
        """秒传上传
        
        Args:
            file_info: 文件信息 {file_name, size, md5, path}
            slice_md5: 首分片 MD5
            chunk_data: 验证分片数据
            offset: 验证分片偏移量
        
        Returns:
            成功返回 {code: 200, data: {...}}
        """
        return self.error("此网盘不支持秒传")
    
    async def _request(
        self, 
        url: str, 
        method: str = "GET", 
        data: Dict = None, 
        params: Dict = None,
        headers: Dict = None,
        verify: bool = None
    ) -> Dict[str, Any]:
        """发送 HTTP 请求"""
        if verify is None:
            verify = self.verify
            
        async with httpx.AsyncClient(timeout=60, verify=verify) as client:
            request_headers = headers or self.headers
            
            if method.upper() == "GET":
                response = await client.get(url, params=params, headers=request_headers)
            else:
                response = await client.post(
                    url, 
                    json=data, 
                    params=params, 
                    headers=request_headers
                )
            
            return response.json()
    
    @staticmethod
    def ok(message: str = "success", data: Any = None) -> Dict[str, Any]:
        """返回成功响应"""
        return {"code": 200, "message": message, "data": data}
    
    @staticmethod
    def error(message: str = "error", code: int = 500) -> Dict[str, Any]:
        """返回错误响应"""
        return {"code": code, "message": message}
