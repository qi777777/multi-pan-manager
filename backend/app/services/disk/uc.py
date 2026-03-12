import os
import re
import httpx
import asyncio
import hashlib
import time
import base64
import json
import struct
import mimetypes
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple, Callable
from .base import BaseDiskService
from ...core.logger import logger


class ProgressFileWrapper:
    def __init__(self, file_obj, total_size, callback, check_cancel=None):
        self.file_obj = file_obj
        self.total_size = total_size
        self.callback = callback
        self.check_cancel = check_cancel
        self.uploaded = 0

    def read(self, size):
        if self.check_cancel and self.check_cancel():
             raise Exception("Upload cancelled")
        data = self.file_obj.read(size)
        if data:
            self.uploaded += len(data)
            if self.callback:
                 import asyncio
                 try:
                     asyncio.create_task(self.callback(self.uploaded, self.total_size))
                 except: pass
        return data

class UcDiskService(BaseDiskService):
    """UC网盘服务"""
    
    BASE_URL = "https://pc-api.uc.cn/1/clouddrive"
    UA_UC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"
    
    def __init__(self, credentials: str, config: Dict[str, Any] = None):
        # 严格清洗原始 Cookie 字符串
        self.raw_cookies = credentials.strip().replace("\r", "").replace("\n", "")
        self.cookies_dict = self._parse_cookies()
        super().__init__(credentials, config)
        self.verify = False # UC 证书有问题，全局禁用校验
        # 路径缓存和锁，改为实例级别以支持多账号独立存储
        self._path_cache = {}
        self._path_lock = asyncio.Lock()
        
    def _parse_cookies(self) -> Dict[str, str]:
        """将 Cookie 字符串解析为字典"""
        cookies = {}
        if not self.raw_cookies:
            return cookies
        for item in self.raw_cookies.split(';'):
            if '=' in item:
                k, v = item.strip().split('=', 1)
                cookies[k] = v
        return cookies
    
    
    def _build_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json;charset=utf-8",
            "Content-Type": "application/json;charset=UTF-8",
            # 注意：不建议在后端发送 Referer 和 Origin 到 pc-api.uc.cn，
            # 否则可能会由于 CORS 校验失败报 "Invalid CORS request"
            "User-Agent": self.UA_UC
        }
    
    async def check_status(self) -> bool:
        try:
            # 使用子类的 _request 确保带上 cookies_dict
            result = await self.get_files("0")
            return result.get("code") == 200
        except:
            return False

    async def _request(
        self, 
        url: str, 
        method: str = "GET", 
        data: Dict = None, 
        params: Dict = None,
        headers: Dict = None,
        verify: bool = None
    ) -> Dict[str, Any]:
        """重写请求方法，显式使用 cookies_dict"""
        if verify is None:
            verify = self.verify
            
        # 补齐基础头信息
        request_headers = self._build_headers()
        if headers:
            request_headers.update(headers)
        
        # 移除 Cookie Header，改由 cookies 参数统一托管
        if "Cookie" in request_headers:
            del request_headers["Cookie"]
        
        # 移除可能导致 403 Invalid CORS request 的 Header
        if "Referer" in request_headers:
            del request_headers["Referer"]
        if "Origin" in request_headers:
            del request_headers["Origin"]

        async with httpx.AsyncClient(
            cookies=self.cookies_dict,
            timeout=60, 
            verify=verify,
            follow_redirects=True
        ) as client:
            if method.upper() == "GET":
                response = await client.get(url, params=params, headers=request_headers)
            else:
                response = await client.post(url, json=data, params=params, headers=request_headers)
            
            return response.json()
    
    async def get_files(self, pdir_fid: str = "0") -> Dict[str, Any]:
        """获取文件列表"""
        params = {
            "pr": "UCBrowser",
            "fr": "pc",
            "pdir_fid": pdir_fid,
            "_page": 1,
            "_size": 50,
            "_fetch_total": 1,
            "_fetch_sub_dirs": 0,
            "_sort": "file_type:asc,updated_at:desc"
        }
        
        try:
            res = await self._request(f"{self.BASE_URL}/file/sort", "GET", params=params)
            if res.get("status") != 200:
                msg = res.get("message", "")
                if msg == "require login [guest]":
                    msg = "UC未登录，请检查Cookie"
                return self.error(msg)
            
            files = []
            for item in res.get("data", {}).get("list", []):
                files.append({
                    "fid": item.get("fid"),
                    "name": item.get("file_name"),
                    "size": item.get("size", 0),
                    # UC: file_type 0 为目录, 1 为文件
                    "is_dir": item.get("file_type") == 0,
                    "updated_at": item.get("updated_at")
                })
            return self.ok("获取成功", files)
        except Exception as e:
            return self.error(f"获取文件列表失败: {str(e)}")

    async def list_folder_recursive(self, folder_fid: str) -> Dict[str, Any]:
        """递归获取文件夹下所有文件 (用于跨盘传输)"""
        all_files = []
        try:
            async def _recursive(fid, current_path=""):
                res = await self.get_files(fid)
                if res.get("code") != 200:
                    return
                
                items = res.get("data", [])
                if not items and current_path:
                    all_files.append({
                        "fid": fid,
                        "name": current_path.split('/')[-1] if '/' in current_path else current_path,
                        "size": 0,
                        "is_dir": True,
                        "relative_path": current_path
                    })
                
                for item in items:
                    # 构造相对路径
                    if item.get("is_dir"):
                        await _recursive(item["fid"], f"{current_path}/{item['name']}" if current_path else item['name'])
                    else:
                        all_files.append({
                            "fid": item["fid"],
                            "name": item["name"],
                            "size": item["size"],
                            "relative_path": current_path # 相对文件夹根目录的路径
                        })
            
            await _recursive(folder_fid)
            return self.ok("获取成功", all_files)
        except Exception as e:
            return self.error(f"递归获取文件列表失败: {str(e)}")

    async def get_file_download_info(self, fid: str, session: Any = None) -> Dict[str, Any]:
        """获取文件下载信息 (支持传入并行会话以保持指纹一致)"""
        params = {
            "entry": "ft",
            "fr": "pc",
            "pr": "UCBrowser"
        }
        api_url = f"{self.BASE_URL}/file/download?entry=ft"
        data = {"fids": [fid]}

        try:
            if session:
                # 如果传入了 session (可能是 curl_cffi 或 httpx)，直接使用其 post
                # 注意处理不同 session 的参数差异
                headers = self._build_headers()
                if hasattr(session, "impersonate"): # curl_cffi
                    res = await session.post(api_url, json=data, params=params, headers=headers, allow_redirects=True)
                    res_data = res.json()
                else: # httpx
                    res = await session.post(api_url, json=data, params=params, headers=headers, follow_redirects=True)
                    res_data = res.json()
            else:
                res_data = await self._request(api_url, "POST", data=data, params=params)
            
            if res_data.get("status") != 200:
                return self.error(f"获取下载信息失败: {res_data.get('message', '未知错误')}")

            list_data = res_data.get("data", [])
            if not list_data:
                return self.error("接口未返回下载链接 (可能已被禁或文件异常)")
            
            info = list_data[0]
            
            return {
                "code": 200,
                "data": {
                    "download_url": info.get("download_url"),
                    "file_name": info.get("file_name"),
                    "size": int(info.get("size", 0)),
                    "md5": info.get("md5")
                }
            }
        except Exception as e:
            return self.error(f"获取下载信息异常: {str(e)}")
    
    async def search_files(self, keyword: str, page: int = 1, size: int = 50) -> Dict[str, Any]:
        """全盘搜索文件"""
        params = {
            "pr": "UCBrowser",
            "fr": "pc",
            "q": keyword,
            "_page": page,
            "_size": size,
            "_fetch_total": 1,
            "_sort": "file_type:asc,updated_at:desc"
        }
        
        try:
            res = await self._request(f"{self.BASE_URL}/file/search", "GET", params=params)
            if res.get("status") != 200:
                msg = res.get("message", "")
                if msg == "require login [guest]":
                    msg = "UC未登录，请检查Cookie"
                return self.error(msg)
            
            files = []
            for item in res.get("data", {}).get("list", []):
                files.append({
                    "fid": item.get("fid"),
                    "name": item.get("file_name"),
                    "size": item.get("size", 0),
                    "is_dir": item.get("dir", False),
                    "updated_at": item.get("updated_at")
                })
            
            total = res.get("metadata", {}).get("_total", len(files))
            logger.info(f"[UC] 搜索成功: 关键词'{keyword}', 找到 {total} 个结果")
            return self.ok("搜索成功", {"list": files, "total": total})
        except Exception as e:
            logger.error(f"[UC] 搜索异常: {str(e)}")
            return self.error(f"搜索文件失败: {str(e)}")
    

    # UC网盘 Transfer 方法完整实现
    async def transfer(self, share_url: str, code: str = "", expired_type: int = 1, need_share: bool = True) -> Dict[str, Any]:
        """转存分享资源
        
        Args:
            share_url: 分享链接，如 https://drive.uc.cn/s/xxx
            code: 提取码（可选，UC暂不支持）
            expired_type: 分享有效期类型 (1=永久)
            need_share: 是否创建新分享
        
        Returns:
            {"code": 200, "data": {"title": "", "share_url": "", "code": "", "fid": []}}
        """
        try:
            # 0. 解析存储路径
            storage_path = self.config.get("storage_path", "0")
            to_pdir_fid = "0"
            if storage_path and storage_path not in ("0", "/", ""):
                path_res = await self.get_or_create_path(storage_path)
                if path_res.get("code") == 200:
                    to_pdir_fid = path_res.get("data", {}).get("fid", "0")
                else:
                    logger.warning(f"[UC] 解析存储路径失败: {storage_path}，使用根目录")
            
            # 1. 解析分享ID
            import re
            match = re.search(r'/s/([A-Za-z0-9]+)', share_url)
            if not match:
                return self.error("分享链接格式错误")
            
            pwd_id = match.group(1)
            logger.info(f"[UC] 开始转存分享: pwd_id={pwd_id}")
            
            # 2. 获取stoken
            stoken_res = await self._get_stoken(pwd_id)
            if stoken_res.get("status") != 200:
                return self.error(f"获取stoken失败: {stoken_res.get('message')}")
            
            stoken = stoken_res["data"]["token_info"]["stoken"]
            title = stoken_res["data"]["token_info"].get("title", "UC分享文件")
            logger.info(f"[UC] 获取stoken成功: {title}")
            
            # 3. 获取分享详情
            detail_res = await self._get_share_detail(pwd_id, stoken)
            if detail_res.get("status") != 200:
                return self.error(f"获取分享详情失败: {detail_res.get('message')}")
            
            detail = detail_res["data"]
            fid_list = [f["fid"] for f in detail["list"]]
            fid_token_list = [f["share_fid_token"] for f in detail["list"]]
            logger.info(f"[UC] 获取到 {len(fid_list)} 个文件")
            
            # 4. 转存到自己网盘
            save_res = await self._save_share(pwd_id, stoken, fid_list, fid_token_list, to_pdir_fid)
            if save_res.get("status") != 200:
                return self.error(f"转存失败: {save_res.get('message')}")
            
            task_id = save_res["data"]["task_id"]
            logger.info(f"[UC] 转存任务ID: {task_id}")
            
            # 5. 轮询转存任务状态
            save_task_res = await self._poll_task(task_id, max_retries=50)
            if not save_task_res or save_task_res.get("status") != 2:
                return self.error("转存任务失败或超时")
            
            save_as_fids = save_task_res["save_as"]["save_as_top_fids"]
            logger.info(f"[UC] 转存成功，文件ID: {save_as_fids}")
            
            # 6. 根据 need_share 决定是否创建分享
            if need_share:
                share_payload = await self.create_share(save_as_fids, title, expired_type=expired_type)
                if share_payload.get("code") != 200:
                    return self.error(f"创建分享失败: {share_payload.get('message')}")
                
                share_data = share_payload["data"]
                logger.info(f"[UC] 转存并分享成功: {title}")
                
                return self.ok("转存成功", {
                    "title": title,
                    "share_url": share_data.get("share_url", ""),
                    "code": share_data.get("code", ""),
                    "fid": save_as_fids
                })
            else:
                logger.info(f"[UC] 转存成功（不分享）: {title}")
                return self.ok("转存成功", {
                    "title": title,
                    "share_url": "",
                    "code": "",
                    "fid": save_as_fids
                })
            
        except Exception as e:
            logger.error(f"[UC] 转存异常: {str(e)}")
            import traceback
            traceback.print_exc()
            return self.error(f"转存异常: {str(e)}")

    async def validate_share(self, share_url: str, password: str = "") -> Dict[str, Any]:
        """
        高精度检测UC网盘分享链接有效性
        """
        try:
            # 1. 解析 pwd_id
            import re
            match = re.search(r'/s/([A-Za-z0-9]+)', share_url)
            if not match:
                return self.error("分享链接格式错误")
            pwd_id = match.group(1)

            # 2. 获取 stoken
            stoken_res = await self._get_stoken(pwd_id)
            if stoken_res.get("status") != 200:
                return self.error(f"链接已失效: {stoken_res.get('message', '无法获取 stoken')}")
            
            stoken = stoken_res["data"]["token_info"]["stoken"]

            # 3. 获取分享详情 (深度探测)
            detail_res = await self._get_share_detail(pwd_id, stoken)
            if detail_res.get("status") != 200:
                return self.error(f"链接不可用: {detail_res.get('message', '无法获取详情')}")
            
            detail = detail_res.get("data", {})
            # 检查是否有文件
            if not detail.get("list"):
                 return self.error("分享内容为空或文件已失效")

            return self.ok("链接有效", detail)
            
        except Exception as e:
            logger.error(f"[UC] validate_share 异常: {e}")
            return self.error(f"检测异常: {str(e)}")

    async def _get_stoken(self, pwd_id: str):
        """获取分享的stoken"""
        try:
            url = "https://pc-api.uc.cn/1/clouddrive/share/sharepage/v2/detail"
            params = {"pr": "UCBrowser", "fr": "pc"}
            data = {"passcode": "", "pwd_id": pwd_id}
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, params=params, json=data, headers=self.headers)
                return resp.json()
        except Exception as e:
            logger.error(f"[UC] 获取stoken异常: {str(e)}")
            return {"status": 500, "message": str(e)}
    
    async def _get_share_detail(self, pwd_id: str, stoken: str):
        """获取分享文件详情"""
        try:
            url = "https://pc-api.uc.cn/1/clouddrive/share/sharepage/detail"
            params = {
                "pr": "UCBrowser",
                "fr": "pc",
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": "0",
                "force": "0",
                "_page": "1",
                "_size": "100",
                "_fetch_banner": "1",
                "_fetch_share": "1",
                "_fetch_total": "1",
                "_sort": "file_type:asc,updated_at:desc"
            }
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, params=params, headers=self.headers)
                return resp.json()
        except Exception as e:
            logger.error(f"[UC] 获取分享详情异常: {str(e)}")
            return {"status": 500, "message": str(e)}

    async def _save_share(self, pwd_id: str, stoken: str, fid_list: list, fid_token_list: list, to_pdir_fid: str = "0"):
        """转存分享文件到自己网盘"""
        try:
            url = "https://pc-api.uc.cn/1/clouddrive/share/sharepage/save"
            params = {"entry": "update_share", "pr": "UCBrowser", "fr": "pc"}
            data = {
                "fid_list": fid_list,
                "fid_token_list": fid_token_list,
                "to_pdir_fid": to_pdir_fid,
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": "0",
                "scene": "link"
            }
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, params=params, json=data, headers=self.headers)
                return resp.json()
        except Exception as e:
            logger.error(f"[UC] 转存文件异常: {str(e)}")
            return {"status": 500, "message": str(e)}
    



    
    async def delete_files(self, fid_list: List[str]) -> Dict[str, Any]:
        """删除文件"""
        try:
            data = {
                "action_type": 2,
                "exclude_fids": [],
                "filelist": fid_list
            }
            params = {"pr": "UCBrowser", "fr": "pc"}
            
            logger.info(f"[UC] 开始批量删除 {len(fid_list)} 个文件/目录, FIDs: {fid_list}")
            await self._request(f"{self.BASE_URL}/file/delete", "POST", data=data, params=params)
            logger.info(f"[UC] 批量删除完成, 数量: {len(fid_list)}, FIDs: {fid_list}")
            return self.ok("删除成功")
        except Exception as e:
            logger.error(f"[UC] 删除失败: FIDs: {fid_list}, error: {str(e)}")
            return self.error(f"删除失败: {str(e)}")

    async def download_slice(self, url: str, offset: int, length: int) -> Optional[bytes]:
        """下载文件分片 (用于秒传验证)"""
        from curl_cffi.requests import AsyncSession
        
        headers = {
            "User-Agent": self.UA_UC,
            "Referer": "https://drive.uc.cn/",
            "Range": f"bytes={offset}-{offset + length - 1}",
            "Connection": "keep-alive"
        }
        
        try:
             async with AsyncSession(cookies=self.cookies_dict, verify=False, impersonate="chrome110", timeout=30) as session:
                resp = await session.get(url, headers=headers, allow_redirects=True)
                if resp.status_code in [200, 206]:
                    return resp.content
        except Exception as e:
            pass
            
        return None

    async def download_slice_by_fid(self, fid: str, offset: int, length: int) -> Optional[bytes]:
        """通过 FID 下载分片"""
        from curl_cffi.requests import AsyncSession
        try:
            async with AsyncSession(cookies=self.cookies_dict, verify=False, impersonate="chrome110", timeout=30) as session:
                info = await self.get_file_download_info(fid, session=session)
                if info.get("code") == 200:
                   url = info["data"]["download_url"]
                   return await self.download_slice(url, offset, length)
        except Exception:
            pass
        return None

    async def download_file(self, fid: str, save_path: str, progress_callback=None, task_id: str = None) -> bool:
        """下载完整文件 (使用统一并发下载器 + curl_cffi Fetcher)"""
        try:
            from ..download_manager import get_downloader
            from curl_cffi.requests import AsyncSession
            import uuid
            
            # 1. 初始 Session (模拟浏览器指纹)
            session = AsyncSession(
                cookies=self.cookies_dict, 
                verify=False, 
                impersonate="chrome110",
                timeout=60
            )

            # Context for fetcher
            download_ctx = {"url": "", "fid": fid}
            
            try:
                # 2. 获取直链
                info = await self.get_file_download_info(fid, session=session)
                if info.get("code") != 200:
                    logger.error(f"[UC] 获取直链失败 ({fid}): {info.get('message')}")
                    return False
                
                data = info["data"]
                download_ctx["url"] = data["download_url"]
                file_size = int(data.get("size", 0))
                
                logger.info(f"[UC] 开启指纹对齐加速下载 ({fid}): 大小 {file_size // 1024 // 1024}MB")
                
                # 3. 定义 Custom Fetcher
                async def uc_chunk_fetcher(start: int, end: int, chunk_idx: int) -> Optional[bytes]:
                    current_url = download_ctx["url"]
                    headers = {
                        "User-Agent": self.UA_UC,
                        "Referer": "https://drive.uc.cn/",
                        "Range": f"bytes={start}-{end}",
                        "Connection": "keep-alive"
                    }

                    # 重试逻辑
                    for attempt in range(3):
                        try:
                            # 注意: curl_cffi 的 session.get 是 async 的
                            resp = await session.get(current_url, headers=headers, allow_redirects=True)
                            
                            if resp.status_code in [200, 206]:
                                return resp.content
                            elif resp.status_code == 403:
                                # 刷新链接
                                if attempt < 2:
                                    logger.warning(f"[UC] 下载链接已过期 (403)，正在刷新...")
                                    fresh = await self.get_file_download_info(download_ctx["fid"], session=session)
                                    if fresh.get("code") == 200:
                                        download_ctx["url"] = fresh["data"]["download_url"]
                                        current_url = download_ctx["url"]
                                        continue
                            
                            # 其他错误
                            if attempt < 2:
                                await asyncio.sleep(1)
                        except Exception as e:
                            if attempt < 2:
                                await asyncio.sleep(1)
                    
                    logger.error(f"[UC] 分片 {chunk_idx} 在多次重试后依然失败")
                    return None # 最终失败

                # 4. 创建任务
                downloader = get_downloader()
                task_id = task_id or f"uc_{uuid.uuid4().hex[:8]}"
                
                await downloader.create_task(
                    task_id=task_id,
                    url=download_ctx["url"],
                    output_path=save_path,
                    concurrency=8,
                    custom_chunk_fetcher=uc_chunk_fetcher,
                    file_size=file_size
                )
                
                result = await downloader.start(task_id, progress_callback)
                return bool(result)
            
            finally:
                await session.close()
                
        except Exception as e:
            logger.error(f"[UC] 下载异常: {str(e)}", exc_info=True)
            return False

    async def create_folder(self, folder_name: str, pdir_fid: str = "0") -> Dict[str, Any]:
        """在指定目录下创建文件夹"""
        try:
            # UC 的创建目录接口与夸克基本一致
            data = {
                "pdir_fid": pdir_fid,
                "file_name": folder_name,
                "dir_path": "",
                "dir_init_lock": False
            }
            params = {"pr": "UCBrowser", "fr": "pc"}
            logger.info(f"[UC] 正在创建文件夹: {folder_name} (Parent: {pdir_fid})")
            res = await self._request(f"{self.BASE_URL}/file", "POST", data=data, params=params)
            
            if res.get("status") == 200 and res.get("data"):
                logger.info(f"[UC] 文件夹创建成功: {folder_name} (FID: {res['data'].get('fid')})")
                return self.ok("创建成功", {"fid": res["data"].get("fid")})
            elif "已存在" in res.get("message", "") or res.get("code") == 23009:
                # 文件夹已存在，尝试获取其 fid
                list_res = await self.get_files(pdir_fid)
                if list_res.get("code") == 200:
                    for item in list_res.get("data", []):
                        if item.get("name") == folder_name and item.get("is_dir"):
                            logger.info(f"[UC] 文件夹已存在: {folder_name} (FID: {item.get('fid')})")
                            return self.ok("文件夹已存在", {"fid": item.get("fid")})
                logger.error(f"[UC] 文件夹已存在但无法获取 fid ({folder_name})")
                return self.error("文件夹已存在但无法获取 fid")
            else:
                err_msg = res.get("message", "创建失败")
                logger.error(f"[UC] 文件夹创建失败 ({folder_name}): {err_msg}")
                return self.error(err_msg)
        except Exception as e:
            logger.error(f"[UC] 文件夹创建异常 ({folder_name}): {str(e)}")
            return self.error(f"创建文件夹失败: {str(e)}")

    @staticmethod
    def _sanitize_path(path: str) -> str:
        """清理路径中的不兼容字符（全角转半角、移除禁止字符）"""
        original_path = path  # 保存原始路径用于日志
        
        # 仅删除UC明确禁止的全角字符（保守策略）
        replacements = {
            '？': '',   # 全角问号 → 删除
            '｜': '',   # 全角竪线 → 删除
            '＊': '',   # 全角星号 → 删除
            '＜': '',   # 全角小于号 → 删除
            '＞': '',   # 全角大于号 → 删除
            '％': '',   # 全角百分号 → 删除
            '“': '',   # 左双引号 → 删除
            '”': '',   # 右双引号 → 删除
        }
        
        for old, new in replacements.items():
            path = path.replace(old, new)
        
        # 移除 UC 禁止的字符
        forbidden = ['\\', ':', '<', '>', '|', '*', '?', '%', '"']
        for char in forbidden:
            path = path.replace(char, '')
        
        # 清理连续斜杠和首尾空格
        path = '/'.join(p.strip() for p in path.split('/') if p.strip())
        
        # 🔍 [诊断日志] 路径规范化记录
        if path != original_path:
            logger.info(f"[UC] 路径规范化: '{original_path}' → '{path}'")
        if not path:
            logger.warning(f"[UC] ⚠️ 路径规范化后为空！原路径: '{original_path}'")
        
        return path

    async def get_or_create_path(self, path: str) -> Dict[str, Any]:
        """递归创建路径并返回最终目录的 fid（带并发锁）"""
        # 规范化路径：清理特殊字符
        path = self._sanitize_path(path)
        path = path.strip('/')
        if not path:
            return self.ok("根目录", {"fid": "0"})
        
        # 检查缓存
        if path in self._path_cache:
            return self.ok("路径已缓存", {"fid": self._path_cache[path]})
        
        # 使用锁防止并发创建同一路径
        async with self._path_lock:
            # 再次检查缓存
            if path in self._path_cache:
                return self.ok("路径已缓存", {"fid": self._path_cache[path]})
            
            try:
                parts = path.split('/')
                current_fid = "0"
                current_path = ""
                
                for part in parts:
                    if not part: continue
                    current_path = f"{current_path}/{part}" if current_path else part
                    
                    if current_path in self._path_cache:
                        current_fid = self._path_cache[current_path]
                        continue
                    
                    # 检查目录是否存在
                    list_res = await self.get_files(current_fid)
                    if list_res.get("code") != 200:
                        return self.error(f"无法列出目录内容: {list_res.get('message')}")
                    
                    found = False
                    for item in list_res.get("data", []):
                        if item.get("name") == part and item.get("is_dir"):
                            current_fid = item.get("fid")
                            self._path_cache[current_path] = current_fid
                            found = True
                            break
                    
                    if not found:
                        # 创建目录
                        logger.info(f"[UC] 创建目录: {part} (父目录 fid={current_fid})")
                        create_res = await self.create_folder(part, current_fid)
                        if create_res.get("code") != 200:
                            return self.error(f"创建目录 {part} 失败: {create_res.get('message')}")
                        current_fid = create_res.get("data", {}).get("fid")
                        logger.info(f"[UC] 目录创建成功: {part} (新 fid={current_fid})")
                        self._path_cache[current_path] = current_fid
                
                logger.info(f"[UC] 路径 '{path}' 最终 fid={current_fid}")
                return self.ok("路径已就绪", {"fid": current_fid})
            except Exception as e:
                return self.error(f"创建路径失败: {str(e)}")

    async def upload_file(self, file_data: Any, file_name: str, pdir_fid: str = "0", progress_callback=None, check_cancel=None) -> Dict[str, Any]:
        """上传文件到指定目录"""
        # 🔍 [诊断日志] 上传文件信息
        logger.info(f"[UC] 上传文件: {file_name} 到目录 fid={pdir_fid}")
        try:
            if hasattr(file_data, 'read'):
                content = file_data.read()
            else:
                content = file_data
            
            # 兼容性修复：确保 file_name 是非空字符串，防止 mimetypes 报错
            if not file_name or not isinstance(file_name, str):
                file_name = str(file_name) if file_name else "未命名文件"
                
            file_size = len(content)
            mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            
            # 1. 预上传
            pre_res = await self._pre_upload(file_name, file_size, pdir_fid, mime_type)
            task_id = pre_res.get("task_id")
            upload_id = pre_res.get("upload_id")
            obj_key = pre_res.get("obj_key")
            bucket = pre_res.get("bucket", "ul-zb")
            callback_info = pre_res.get("callback", {})
            auth_info = pre_res.get("auth_info", "")
            
            if not task_id: return self.error("预上传未返回task_id")

            # 2. 更新哈希
            md5_hash = hashlib.md5(content).hexdigest()
            sha1_hash = hashlib.sha1(content).hexdigest()
            
            hash_data = {"task_id": task_id, "md5": md5_hash, "sha1": sha1_hash}
            hash_params = {"pr": "UCBrowser", "fr": "pc"}
            await self._request(f"{self.BASE_URL}/file/update/hash", "POST", data=hash_data, params=hash_params)

            # 3. 上传分片
            chunk_size = 4 * 1024 * 1024
            xml_parts = []
            total_chunks = (file_size + chunk_size - 1) // chunk_size
            
            for i in range(0, file_size, chunk_size):
                # [Fix] Check cancellation
                if check_cancel and check_cancel():
                    raise asyncio.CancelledError("Upload cancelled by user")
                    
                part_num = i // chunk_size + 1
                chunk_data = content[i : i + chunk_size]
                
                auth = await self._get_upload_auth(task_id, mime_type, part_num, auth_info, upload_id, obj_key, bucket)
                etag = await self._upload_part_to_oss(auth['upload_url'], auth['headers'], chunk_data)
                xml_parts.append(f'<Part>\n<PartNumber>{part_num}</PartNumber>\n<ETag>"{etag}"</ETag>\n</Part>')
                
                logger.info(f"[UC] [UPLOAD] 分片 {part_num}/{total_chunks} 上传完成 ({file_name})")
                
                if progress_callback:
                    try:
                        await progress_callback(i + len(chunk_data), file_size)
                    except:
                        pass
            
            xml_data = '<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>\n' + '\n'.join(xml_parts) + '\n</CompleteMultipartUpload>'
            post_auth = await self._get_complete_upload_auth(task_id, upload_id, obj_key, bucket, xml_data, callback_info)
            await self._complete_multipart_upload(post_auth['upload_url'], post_auth['headers'], xml_data)

            # 4. 完成
            await asyncio.sleep(0.5)
            logger.info(f"[UC] 上传完成，正在确认任务 ({file_name})")
            finish_res = await self._finish_upload(task_id, obj_key)
            
            fid = finish_res.get("fid") or finish_res.get("file_id")
            logger.info(f"[UC] 文件上传成功: {file_name}, fid={fid}")
            return self.ok("上传成功", {"fid": fid})
        except Exception as e:
            logger.error(f"[UC] 上传失败 ({file_name}): {repr(e)}")
            return self.error(f"UC上传失败: {repr(e)}")

    async def _pre_upload(self, file_name: str, file_size: int, pdir_fid: str, mime_type: str) -> Dict[str, Any]:
        data = {
            "ccp_hash_update": True,
            "parallel_upload": False,
            "pdir_fid": pdir_fid,
            "dir_name": "", 
            "size": file_size, 
            "file_name": file_name,
            "format_type": mime_type, 
            "l_updated_at": int(time.time() * 1000),
            "l_created_at": int(time.time() * 1000)
        }
        res = await self._request(f"{self.BASE_URL}/file/upload/pre", "POST", data=data, params={'pr': 'UCBrowser', 'fr': 'pc'})
        if res.get("status") != 200:
            raise Exception(res.get("message", "预上传失败"))
        return res.get("data", {})

    async def _get_upload_auth(self, task_id: str, mime_type: str, part_number: int, auth_info: str, upload_id: str, obj_key: str, bucket: str) -> Dict[str, Any]:
        oss_date = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
        auth_meta = f"PUT\n\n{mime_type}\n{oss_date}\nx-oss-date:{oss_date}\nx-oss-user-agent:aliyun-sdk-js/1.0.0\n/{bucket}/{obj_key}?partNumber={part_number}&uploadId={upload_id}"
        
        data = {"task_id": task_id, "auth_info": auth_info, "auth_meta": auth_meta}
        res = await self._request(f"{self.BASE_URL}/file/upload/auth", "POST", data=data, params={"pr": "UCBrowser", "fr": "pc"})
        if res.get("status") != 200:
            raise Exception(f"获取授权失败: {res.get('message')}")
        
        auth_key = res.get("data", {}).get("auth_key", "")
        upload_url = f"https://{bucket}.pds.uc.cn/{obj_key}?partNumber={part_number}&uploadId={upload_id}"
        headers = {'Content-Type': mime_type, 'x-oss-date': oss_date, 'x-oss-user-agent': 'aliyun-sdk-js/1.0.0'}
        if auth_key: headers['authorization'] = auth_key
        return {'upload_url': upload_url, 'headers': headers}

    async def _upload_part_to_oss(self, upload_url: str, headers: Dict, data: bytes) -> str:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=300, verify=False) as client:
                    resp = await client.put(upload_url, headers=headers, content=data)
                    if resp.status_code != 200:
                        raise Exception(f"OSS上传失败: {resp.status_code}, {resp.text}")
                    return resp.headers.get("ETag", "").strip('"')
            except Exception as e:
                logger.warning(f"[UC] 分片上传异常 (第 {attempt + 1} 次尝试): {repr(e)}")
                if attempt == 2:
                    raise e
                await asyncio.sleep(2)
        raise Exception("分片上传重试耗尽")

    async def _get_complete_upload_auth(self, task_id: str, upload_id: str, obj_key: str, bucket: str, xml_data: str, callback_info: Dict) -> Dict:
        oss_date = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
        md5_xml = base64.b64encode(hashlib.md5(xml_data.encode('utf-8')).digest()).decode('utf-8')
        
        callback_str = json.dumps(callback_info).replace("/", "\\/")
        b64_callback = base64.b64encode(callback_str.encode('utf-8')).decode('utf-8')
        
        auth_meta = f"POST\n{md5_xml}\napplication/xml\n{oss_date}\nx-oss-callback:{b64_callback}\nx-oss-date:{oss_date}\nx-oss-user-agent:aliyun-sdk-js/1.0.0\n/{bucket}/{obj_key}?uploadId={upload_id}"
        
        data = {"task_id": task_id, "auth_info": callback_info.get("auth_info", ""), "auth_meta": auth_meta}
        res = await self._request(f"{self.BASE_URL}/file/upload/auth", "POST", data=data, params={"pr": "UCBrowser", "fr": "pc"})
        if res.get("status") != 200:
             raise Exception(f"获取合并授权失败: {res.get('message')}")
             
        auth_key = res.get("data", {}).get("auth_key", "")
        upload_url = f"https://{bucket}.pds.uc.cn/{obj_key}?uploadId={upload_id}"
        headers = {
            'Content-Type': 'application/xml', 
            'Content-MD5': md5_xml,
            'x-oss-callback': b64_callback,
            'x-oss-date': oss_date, 
            'x-oss-user-agent': 'aliyun-sdk-js/1.0.0'
        }
        if auth_key: headers['authorization'] = auth_key
        return {'upload_url': upload_url, 'headers': headers}

    async def _complete_multipart_upload(self, upload_url: str, headers: Dict, xml_data: str):
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            resp = await client.post(upload_url, headers=headers, content=xml_data)
            if resp.status_code not in [200, 203]:
                raise Exception(f"合并文件失败: {resp.status_code}, {resp.text}")

    async def _finish_upload(self, task_id: str, obj_key: str = None) -> Dict[str, Any]:
        data = {"task_id": task_id}
        if obj_key: data["obj_key"] = obj_key
        res = await self._request(f"{self.BASE_URL}/file/upload/finish", "POST", data=data, params={"pr": "UCBrowser", "fr": "pc"})
        if res.get("status") != 200:
             raise Exception(f"完成上传失败: {res.get('message', '未知错误')}")
        return res.get("data", {})


    async def _create_share_task(self, fid_list: list, title: str, expired_type: int = 1):
        """创建分享（返回task_id）"""
        try:
            url = f"{self.BASE_URL}/share"
            params = {"pr": "UCBrowser", "fr": "pc"}
            
            # 内部 expired_type: 1=永久, 2=7天, 3=1天, 4=30天
            # UC API expired_type: 1=永久, 2=1天, 3=7天, 4=30天
            uc_expired_map = {1: 1, 2: 3, 3: 2, 4: 4}
            uc_expired_type = uc_expired_map.get(int(expired_type), 1)
            
            data = {
                "fid_list": fid_list,
                "expired_type": uc_expired_type,
                "title": title,
                "url_type": 1
            }
            
            res = await self._request(url, "POST", data=data, params=params)
            return res
        except Exception as e:
            logger.error(f"[UC] 创建分享异常: {repr(e)}")
            return {"status": 500, "message": repr(e)}
    
    async def _poll_task(self, task_id: str, max_retries: int = 50):
        """轮询任务状态直到完成"""
        try:
            url = f"{self.BASE_URL}/task"
            
            for retry_index in range(max_retries):
                params = {
                    "pr": "UCBrowser",
                    "fr": "pc",
                    "task_id": task_id,
                    "retry_index": retry_index
                }
                
                result = await self._request(url, "GET", params=params)
                
                if result.get("status") != 200:
                    logger.warning(f"[UC] 任务查询失败: {result.get('message')}")
                    await asyncio.sleep(0.5)
                    continue
                
                task_data = result.get("data", {})
                
                # 检查任务状态
                if task_data.get("status") == 2:  # 完成
                    logger.info(f"[UC] 任务完成: {task_id}")
                    return task_data
                
                # 容量不足
                if result.get("message") == "capacity limit[{0}]":
                    logger.error("[UC] 容量不足")
                    return None
                
                # 等待后继续
                await asyncio.sleep(0.5)
            
            logger.error(f"[UC] 任务轮询超时: {task_id}")
            return None
            
        except Exception as e:
            logger.error(f"[UC] 轮询任务异常: {repr(e)}")
            return None

    async def _get_share_password(self, share_id: str):
        """获取分享链接和密码"""
        try:
            url = f"{self.BASE_URL}/share/password"
            params = {"pr": "UCBrowser", "fr": "pc"}
            data = {"share_id": share_id}
            
            res = await self._request(url, "POST", data=data, params=params)
            return res
        except Exception as e:
            logger.error(f"[UC] 获取分享密码异常: {repr(e)}")
            return {"status": 500, "message": repr(e)}

    async def create_share(self, fid_list: List[str], title: str, expired_type: int = 1) -> Dict[str, Any]:
        """创建分享"""
        try:
            logger.info(f"[UC] 开始创建分享: title='{title}', 数量={len(fid_list)}, fids={fid_list}, expired_type={expired_type}")
            # 1. 创建分享任务
            share_res = await self._create_share_task(fid_list, title, expired_type)
            if share_res.get("status") != 200:
                logger.error(f"[UC] 创建分享失败: title='{title}', res={share_res}")
                return self.error(f"创建分享失败: {share_res.get('message')}")
            
            share_task_id = share_res["data"]["task_id"]
            
            # 2. 轮询任务
            share_task_res = await self._poll_task(share_task_id, max_retries=50)
            if not share_task_res or share_task_res.get("status") != 2:
                return self.error("创建分享任务失败或超时")
            
            share_id = share_task_res.get("share_id")
            if not share_id:
                return self.error("未获取到share_id")
            
            # 3. 获取链接
            password_res = await self._get_share_password(share_id)
            if password_res.get("status") != 200:
                return self.error(f"获取分享链接失败: {password_res.get('message')}")
            
            share_data = password_res["data"]
            share_url = share_data.get("share_url", "")
            password = share_data.get("passcode", "")
            
            if password and "?pwd=" not in share_url:
                share_url = f"{share_url}?pwd={password}"
            
            logger.info(f"[UC] 创建分享成功: title='{title}', share_url={share_url}")
            return self.ok("分享成功", {
                "share_id": share_id,
                "title": title,
                "share_url": share_url,
                "password": password,
                "fid": fid_list
            })
        except Exception as e:
            logger.error(f"[UC] 创建分享异常: title='{title}', fids={fid_list}, error={str(e)}")
            return self.error(f"创建分享失败: {str(e)}")
    
    async def cancel_share(self, share_id: str) -> Dict[str, Any]:
        """取消/删除UC分享"""
        try:
            params = {"pr": "UCBrowser", "fr": "pc", "uc_param_str": ""}
            data = {"share_ids": [share_id]}
            logger.info(f"[UC] 正在取消分享: share_id='{share_id}'")
            res = await self._request(f"{self.BASE_URL}/share/delete", "POST", data=data, params=params)
            logger.debug(f"[UC] 取消分享返回: {res}")
            if res.get("status") == 200 or res.get("code") == 0:
                logger.info(f"[UC] 取消分享成功: share_id='{share_id}'")
                return self.ok("取消分享成功")
            logger.error(f"[UC] 取消分享失败: share_id='{share_id}', res={res}")
            return self.error(res.get("message", "取消分享失败"))
        except Exception as e:
            logger.error(f"[UC] 取消分享异常: share_id='{share_id}', error={str(e)}")
            return self.error(f"取消分享失败: {str(e)}")
