from typing import Dict, List, Any, Optional, Tuple, Callable
from .base import BaseDiskService
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
from ...core.logger import logger


class QuarkDiskService(BaseDiskService):
    """夸克网盘服务"""
    
    BASE_URL = "https://drive-pc.quark.cn/1/clouddrive"
    
    logger.info("[QUARK] QuarkDiskService loaded - v2024.02.01 (final redirect fix)")
    UA_QUARK = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch'
    UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    
    def __init__(self, credentials: str, config: Dict[str, Any] = None):
        # 严格清洗原始 Cookie 字符串，去除换行、回车和两端空格
        self.raw_cookies = credentials.strip().replace("\r", "").replace("\n", "")
        self.cookies_dict = self._parse_cookies()
        super().__init__(credentials, config)

        # 路径缓存和锁，改为实例级别以支持多账号独立存储
        self._path_cache = {}
        self._path_lock = asyncio.Lock()
    
    def _build_headers(self) -> Dict[str, str]:
        """构建极简请求头"""
        return {
            "User-Agent": self.UA_QUARK,
            "Referer": "https://pan.quark.cn/",
            "Origin": "https://pan.quark.cn"
        }

    async def _request(
        self, 
        url: str, 
        method: str = "GET", 
        data: Dict = None, 
        params: Dict = None,
        headers: Dict = None
    ) -> Dict[str, Any]:
        """使用 httpx 发送请求（极简 Header 策略）"""
        
        # 补齐基础头信息
        request_headers = self._build_headers()
        if headers:
            request_headers.update(headers)

        try:
            async with httpx.AsyncClient(
                cookies=self.cookies_dict,
                http2=True, 
                follow_redirects=True, 
                timeout=60, 
                verify=False
            ) as client:
                if method.upper() == "GET":
                    resp = await client.get(url, params=params, headers=request_headers)
                else:
                    # POST 请求通常需要 Content-Type
                    if "Content-Type" not in request_headers:
                        request_headers["Content-Type"] = "application/json;charset=UTF-8"
                    resp = await client.post(url, json=data, params=params, headers=request_headers)
                
                # 关键修正：同步 Set-Cookie 回 self.cookies_dict，确保后续下载携带最新授权
                if resp.cookies:
                    self.cookies_dict.update(dict(resp.cookies))
                
                if resp.status_code == 401:
                    return self.error("夸克 Cookie 已过期，请重新登录")

                try:
                    return resp.json()
                except json.JSONDecodeError:
                    if resp.text.startswith("AATF"):
                        logger.error(f"[QUARK] 触发了 WAF 挑战 (wg)")
                        return self.error("触发了夸克 WAF 加密挑战 (wg)，请重新登录")
                    logger.error(f"[QUARK] API 返回非 JSON 数据: {resp.text[:100]}")
                    return self.error(f"API 返回非 JSON 数据: {resp.text[:100]}")
        except Exception as e:
            logger.error(f"[QUARK] API 请求异常 ({url}): {repr(e)}")
            return self.error(f"请求异常: {type(e).__name__}")
    async def get_file_download_info(self, fid: str) -> Dict[str, Any]:
        """获取文件下载信息"""
        params = {
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": ""
        }
        data = {"fids": [fid]}

        try:
            res = await self._request(f"{self.BASE_URL}/file/download", "POST", data=data, params=params)
            
            if res.get("status") != 200:
                msg = res.get("message", "Unknown error")
                logger.error(f"[QUARK] 获取下载信息失败 ({fid}): {msg} (Status: {res.get('status')})")
                return self.error(f"获取下载链接失败: {msg}")

            list_data = res.get("data", [])
            if not list_data:
                return self.error("No download info returned")
            
            # 这里的 info 是针对单个 fid 的
            info = list_data[0]
            
            # 对齐返回结构
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
            return self.error(str(e))

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

    async def download_slice(self, url: str, offset: int, length: int, fid: str = None) -> Optional[bytes]:
        """下载文件分片（发送 Cookie 在 Header 中以防 403）"""
        
        # 同步方式在线程池中执行
        def _sync_download():
            try:
                # 使用基础头信息（包含 Cookie）
                headers = self._build_headers()
                headers.update({'Range': f'bytes={offset}-{offset + length - 1}'})
                
                with httpx.Client(
                    headers=headers,
                    cookies=self.cookies_dict,
                    verify=False,
                    timeout=60.0
                ) as client:
                    # 夸克有些分片下载链接会重定向，开启自动跳转并依靠 Cookie Jar 维持
                    with client.stream('GET', url, follow_redirects=True) as resp:
                        if resp.status_code in [200, 206]:
                            return resp.read()
                        
                        logger.error(f"[QUARK] 下载分片失败: HTTP {resp.status_code}")
                        return None
            except Exception as e:
                logger.error(f"[QUARK] 下载分片异常: {e}")
                return None
        
        # 在线程池中执行同步下载
        result = await asyncio.to_thread(_sync_download)
        
        # 处理 403 链接失效（需要异步刷新链接）
        if result is None and fid:
            # 尝试刷新链接后重试
            fresh_info = await self.get_file_download_info(fid)
            if fresh_info.get("code") == 200:
                logger.info("[QUARK] 下载链接已过期，正在自动刷新并重试...")
                return await self.download_slice(fresh_info["data"]["download_url"], offset, length, None)
        
        return result
    
    async def download_slice(self, url: str, offset: int, length: int) -> Optional[bytes]:
        """下载文件分片 (带 Cookie)"""
        try:
            headers = {
                "User-Agent": self.UA_PC,
                "Referer": "https://pan.quark.cn/",
                "Range": f"bytes={offset}-{offset + length - 1}",
                "Connection": "keep-alive"
            }
            # Quark cookies are critical
            async with httpx.AsyncClient(cookies=self.cookies_dict, verify=False, timeout=30.0, http2=True) as client:
                resp = await client.get(url, headers=headers, follow_redirects=True)
                if resp.status_code in [200, 206]:
                    return resp.content
        except Exception as e:
            pass
        return None

    async def download_slice_by_fid(self, fid: str, offset: int, length: int) -> Optional[bytes]:
        """通过 FID 下载分片"""
        info = await self.get_file_download_info(fid)
        if info.get("code") == 200:
            url = info["data"]["download_url"]
            return await self.download_slice(url, offset, length)
        return None

    async def download_file(self, fid: str, save_path: str, progress_callback=None, task_id: str = None) -> bool:
        """下载文件 (使用统一并发下载器)"""
        try:
            from ..download_manager import get_downloader
            import uuid
            
            # 1. 获取下载信息
            info = await self.get_file_download_info(fid)
            if info.get("code") != 200:
                logger.error(f"[QUARK] 获取下载信息失败: {info.get('message')}")
                return False
            
            data = info["data"]
            url = data["download_url"]
            file_size = int(data.get("size", 0))  # [Fix] Explicitly get size

            if not url:
                logger.error(f"[QUARK] 未获取到下载链接 (FID: {fid})")
                return False
                
            # 2. 准备 Headers
            # Quark 需要 cookies
            cookies_str = "; ".join([f"{k}={v}" for k, v in self.cookies_dict.items()])
            headers = {
                "User-Agent": self.UA_PC,
                "Cookie": cookies_str,
                "Referer": "https://pan.quark.cn/",
                "Connection": "keep-alive"
            }
            
            # 3. 创建并启动任务
            downloader = get_downloader()
            task_id = task_id or f"quark_{uuid.uuid4().hex[:8]}"
            
            await downloader.create_task(
                task_id=task_id,
                url=url,
                output_path=save_path,
                headers=headers,
                concurrency=8,
                file_size=file_size  # [Fix] Pass explicit size to avoid HEAD check failure
            )
            
            result = await downloader.start(task_id, progress_callback)
            return bool(result)
            
        except Exception as e:
            logger.error(f"[QUARK] 下载异常: {str(e)}", exc_info=True)
            return False

    async def check_status(self) -> bool:
        """检查登录状态"""
        try:
            result = await self.get_files("0")
            return result.get("code") == 200
        except:
            return False
    
    async def get_files(self, pdir_fid: str = "0") -> Dict[str, Any]:
        """获取文件列表"""
        params = {
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
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
                    msg = "夸克未登录，请检查Cookie"
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
            return self.ok("获取成功", files)
        except Exception as e:
            return self.error(f"获取文件列表失败: {str(e)}")
    
    async def search_files(self, keyword: str, page: int = 1, size: int = 50) -> Dict[str, Any]:
        """全盘搜索文件"""
        params = {
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
            "q": keyword,
            "_page": page,
            "_size": size,
            "_fetch_total": 1,
            "_sort": "file_type:asc,updated_at:desc",
            "_is_hl": 1  # 启用高亮
        }
        
        try:
            res = await self._request(f"{self.BASE_URL}/file/search", "GET", params=params)
            if res.get("status") != 200:
                msg = res.get("message", "")
                if msg == "require login [guest]":
                    msg = "夸克未登录，请检查Cookie"
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
            return self.ok("搜索成功", {"list": files, "total": total})
        except Exception as e:
            return self.error(f"搜索文件失败: {str(e)}")
    
    async def list_folder_recursive(self, folder_fid: str, base_path: str = "") -> Dict[str, Any]:
        """递归获取文件夹内所有文件（扁平化列表）"""
        all_files = []
        
        try:
            # 获取当前文件夹内容
            result = await self.get_files(folder_fid)
            if result.get("code") != 200:
                return result
            
            items = result.get("data", [])
            
            if not items and base_path:
                all_files.append({
                    "fid": folder_fid,
                    "name": base_path.split('/')[-1] if '/' in base_path else base_path,
                    "size": 0,
                    "is_dir": True,
                    "relative_path": base_path
                })
            
            for item in items:
                item_path = f"{base_path}/{item['name']}" if base_path else item['name']
                
                if item['is_dir']:
                    # 递归获取子文件夹
                    sub_result = await self.list_folder_recursive(item['fid'], item_path)
                    if sub_result.get("code") == 200:
                        all_files.extend(sub_result.get("data", []))
                else:
                    # 文件：添加相对路径信息
                    all_files.append({
                        **item,
                        "relative_path": item_path
                    })
            
            return self.ok("获取成功", all_files)
        except Exception as e:
            return self.error(f"递归获取文件夹失败: {str(e)}")
    
    async def transfer(self, share_url: str, code: str = "", expired_type: int = 1, need_share: bool = True) -> Dict[str, Any]:
        """转存分享资源"""
        try:
            # 0. 解析存储路径
            storage_path = self.config.get("storage_path", "0")
            if storage_path and storage_path not in ("0", "/", ""):
                path_res = await self.get_or_create_path(storage_path)
                if path_res.get("code") == 200:
                    self.storage_path = path_res.get("data", {}).get("fid", "0")
                else:
                    logger.warning(f"[QUARK] 解析存储路径失败: {storage_path}，使用根目录")
                    self.storage_path = "0"
            else:
                self.storage_path = "0"

            # 提取 pwd_id
            match = re.search(r's/([^#?]+)', share_url)
            if not match:
                return self.error("资源地址格式有误")
            pwd_id = match.group(1)
            
            # 1. 获取 stoken
            stoken_res = await self._get_stoken(pwd_id)
            if stoken_res.get("status") != 200:
                return self.error(stoken_res.get("message", "获取stoken失败"))
            stoken = stoken_res.get("data", {}).get("stoken", "").replace(" ", "+")
            
            # 2. 获取分享详情
            detail_res = await self._get_share_detail(pwd_id, stoken)
            if detail_res.get("status") != 200:
                return self.error(detail_res.get("message", "获取分享详情失败"))
            
            detail = detail_res.get("data", {})
            title = detail.get("share", {}).get("title", "")
            fid_list = [item.get("fid") for item in detail.get("list", [])]
            fid_token_list = [item.get("share_fid_token") for item in detail.get("list", [])]
            
            # 3. 转存到网盘
            save_res = await self._save_share(pwd_id, stoken, fid_list, fid_token_list)
            if save_res.get("status") != 200:
                return self.error(save_res.get("message", "转存失败"))
            task_id = save_res.get("data", {}).get("task_id")
            
            # 4. 等待转存完成
            save_fids = await self._wait_task(task_id)
            if not save_fids:
                return self.error("转存任务超时")
            
            # 5. 根据 need_share 决定是否创建分享
            if need_share:
                share_result = await self.create_share(save_fids, title, expired_type=expired_type)
                return share_result
            else:
                return self.ok("转存成功", {
                    "title": title,
                    "share_url": "",
                    "code": "",
                    "fid": save_fids
                })
            
        except Exception as e:
            return self.error(f"转存失败: {str(e)}")
    
    async def create_share(self, fid_list: List[str], title: str, expired_type: int = 1) -> Dict[str, Any]:
        """创建分享"""
        try:
            # 内部 expired_type: 1=永久, 2=7天, 3=1天, 4=30天
            # 夸克 API expired_type: 1=永久, 2=1天, 3=7天, 4=30天
            quark_expired_map = {1: 1, 2: 3, 3: 2, 4: 4}
            quark_expired_type = quark_expired_map.get(int(expired_type), 1)
            
            # 创建分享任务
            data = {
                "fid_list": fid_list,
                "expired_type": quark_expired_type,
                "title": title,
                "url_type": 1
            }
            params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
            
            logger.info(f"[QUARK] 开始创建分享: title='{title}', 数量={len(fid_list)}, fids={fid_list}, expired_type={expired_type}")
            res = await self._request(f"{self.BASE_URL}/share", "POST", data=data, params=params)
            if res.get("status") != 200:
                return self.error(res.get("message", "创建分享失败"))
            
            task_id = res.get("data", {}).get("task_id")
            
            # 等待分享任务完成
            share_id = await self._wait_share_task(task_id)
            if not share_id:
                return self.error("分享任务超时")
            
            # 获取分享链接
            pwd_res = await self._get_share_password(share_id)
            if pwd_res.get("status") != 200:
                return self.error(pwd_res.get("message", "获取分享链接失败"))
            
            share_data = pwd_res.get("data", {})
            share_url = share_data.get("share_url", "")
            password = share_data.get("passcode")
            if password and "?pwd=" not in share_url:
                share_url = f"{share_url}?pwd={password}"
                
            logger.info(f"[QUARK] 创建分享成功: title='{title}', share_url={share_url}")
            return self.ok("分享成功", {
                "share_id": share_id,
                "share_url": share_url,
                "password": password,
                "title": title,
                "fid": fid_list
            })
        except Exception as e:
            logger.error(f"[QUARK] 创建分享失败: title='{title}', fids={fid_list}, error={str(e)}")
            return self.error(f"创建分享失败: {str(e)}")
    
    async def cancel_share(self, share_id: str) -> Dict[str, Any]:
        """取消/删除分享"""
        try:
            data = {"share_ids": [share_id]}
            params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
            logger.info(f"[QUARK] 正在取消分享: share_id='{share_id}'")
            res = await self._request(f"{self.BASE_URL}/share/delete", "POST", data=data, params=params)
            logger.debug(f"[QUARK] 取消分享返回: {res}")
            if res.get("status") == 200 or res.get("code") == 0:
                logger.info(f"[QUARK] 取消分享成功: share_id='{share_id}'")
                return self.ok("取消分享成功")
            logger.error(f"[QUARK] 取消分享失败: share_id='{share_id}', res={res}")
            return self.error(res.get("message", "取消分享失败"))
        except Exception as e:
            logger.error(f"[QUARK] 取消分享异常: share_id='{share_id}', error={str(e)}")
            return self.error(f"取消分享失败: {str(e)}")
    
    async def delete_files(self, fid_list: List[str]) -> Dict[str, Any]:
        """删除文件"""
        try:
            data = {
                "action_type": 2,
                "exclude_fids": [],
                "filelist": fid_list
            }
            params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
            
            logger.info(f"[QUARK] 开始批量删除 {len(fid_list)} 个文件/目录, FIDs: {fid_list}")
            res = await self._request(f"{self.BASE_URL}/file/delete", "POST", data=data, params=params)
            
            logger.info(f"[QUARK] 批量删除完成, 数量: {len(fid_list)}, FIDs: {fid_list}")
            return self.ok("删除成功", res)
        except Exception as e:
            logger.error(f"[QUARK] 删除失败: FIDs: {fid_list}, error: {str(e)}")
            return self.error(f"删除失败: {str(e)}")
    
    async def validate_share(self, share_url: str, password: str = "") -> Dict[str, Any]:
        """
        高精度检测夸克网盘分享链接有效性
        """
        try:
            # 1. 解析 pwd_id (Quark 分享 ID)
            match = re.search(r'/s/([A-Za-z0-9]+)', share_url)
            if not match:
                return self.error("分享链接格式错误")
            pwd_id = match.group(1)

            # 2. 获取 stoken
            stoken_res = await self._get_stoken(pwd_id)
            if stoken_res.get("status") != 200:
                return self.error(f"链接已失效: {stoken_res.get('message', '无法获取 stoken')}")
            
            stoken = stoken_res["data"]["stoken"]

            # 3. 获取分享详情 (深度探测)
            detail_res = await self._get_share_detail(pwd_id, stoken)
            if detail_res.get("status") != 200:
                # 即使 stoken 拿到了，详情可能报错（如文件被删、违规）
                return self.error(f"链接不可用: {detail_res.get('message', '无法获取详情')}")
            
            detail = detail_res.get("data", {})
            # 检查是否有文件
            if not detail.get("list") and not detail.get("share", {}).get("first_file"):
                 return self.error("分享内容为空或文件已失效")

            return self.ok("链接有效", detail)
            
        except Exception as e:
            logger.error(f"[QUARK] validate_share 异常: {e}")
            return self.error(f"检测异常: {str(e)}")

    async def _get_stoken(self, pwd_id: str) -> Dict[str, Any]:
        """获取分享 stoken"""
        data = {"passcode": "", "pwd_id": pwd_id}
        params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        return await self._request(f"{self.BASE_URL}/share/sharepage/token", "POST", data=data, params=params)
    
    async def _get_share_detail(self, pwd_id: str, stoken: str) -> Dict[str, Any]:
        """获取分享详情"""
        params = {
            "pr": "ucpro", "fr": "pc", "uc_param_str": "",
            "pwd_id": pwd_id, "stoken": stoken,
            "pdir_fid": "0", "force": "0",
            "_page": "1", "_size": "100",
            "_fetch_banner": "1", "_fetch_share": "1", "_fetch_total": "1",
            "_sort": "file_type:asc,updated_at:desc"
        }
        return await self._request(f"{self.BASE_URL}/share/sharepage/detail", "GET", params=params)
    
    async def _save_share(self, pwd_id: str, stoken: str, fid_list: List[str], fid_token_list: List[str]) -> Dict[str, Any]:
        """保存分享到网盘"""
        to_pdir_fid = self.storage_path or "0"
        data = {
            "fid_list": fid_list,
            "fid_token_list": fid_token_list,
            "to_pdir_fid": to_pdir_fid,
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": "0",
            "scene": "link"
        }
        params = {"entry": "update_share", "pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        return await self._request(f"{self.BASE_URL}/share/sharepage/save", "POST", data=data, params=params)
    
    async def _wait_task(self, task_id: str, max_retries: int = 50) -> List[str]:
        """等待任务完成"""
        for i in range(max_retries):
            params = {"pr": "ucpro", "fr": "pc", "uc_param_str": "", "task_id": task_id, "retry_index": i}
            res = await self._request(f"{self.BASE_URL}/task", "GET", params=params)
            
            if res.get("status") == 200 and res.get("data", {}).get("status") == 2:
                return res.get("data", {}).get("save_as", {}).get("save_as_top_fids", [])
            await asyncio.sleep(0.5)  # 添加延迟避免API限流
        return []
    
    async def _wait_share_task(self, task_id: str, max_retries: int = 50) -> str:
        """等待分享任务完成"""
        for i in range(max_retries):
            params = {"pr": "ucpro", "fr": "pc", "uc_param_str": "", "task_id": task_id, "retry_index": i}
            res = await self._request(f"{self.BASE_URL}/task", "GET", params=params)
            
            if res.get("status") == 200 and res.get("data", {}).get("status") == 2:
                return res.get("data", {}).get("share_id", "")
            await asyncio.sleep(0.5)  # 添加延迟避免API限流
        return ""
    
    async def upload_file(self, file_data: Any, file_name: str, pdir_fid: str = "0", progress_callback=None, check_cancel=None) -> Dict[str, Any]:
        """上传文件
        
        Args:
            file_data: 文件对象
            file_name: 文件名
            pdir_fid: 父目录 fid
            progress_callback: 进度回调函数 (uploaded_chunks, total_chunks) -> None
            check_cancel: 取消检测函数 () -> bool
        """
        try:
            # 兼容性修复：确保 file_name 是非空字符串，防止 mimetypes 报错
            if not file_name or not isinstance(file_name, str):
                file_name = str(file_name) if file_name else "未命名文件"
                
            content = file_data.read()
            file_size = len(content)
            mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            
            # 1. 预上传 (禁用并行上传模式，避免 ContextCompareFailed 错误)
            pre_res = await self._pre_upload(file_name, file_size, pdir_fid, mime_type, parallel=False)
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
            hash_params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
            await self._request(f"{self.BASE_URL}/file/update/hash", "POST", data=hash_data, params=hash_params)

            # 3. 上传分片 (串行模式，不使用增量哈希)
            if file_size < 5 * 1024 * 1024:
                # 单分片
                auth = await self._get_upload_auth(task_id, mime_type, 1, auth_info, upload_id, obj_key, bucket)
                etag = await self._upload_part_to_oss(auth['upload_url'], auth['headers'], content)
                
                # 完成合并
                xml_data = f'<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>\n<Part>\n<PartNumber>1</PartNumber>\n<ETag>"{etag}"</ETag>\n</Part>\n</CompleteMultipartUpload>'
                post_auth = await self._get_complete_upload_auth(task_id, upload_id, obj_key, bucket, xml_data, callback_info)
                await self._complete_multipart_upload(post_auth['upload_url'], post_auth['headers'], xml_data)
                
                # 小文件直接报告完成
                if progress_callback:
                    try:
                        await progress_callback(file_size, file_size)
                    except:
                        pass
            else:
                # 多分片 (串行上传，不使用增量哈希Context)
                chunk_size = 4 * 1024 * 1024
                xml_parts = []
                total_chunks = (file_size + chunk_size - 1) // chunk_size
                
                for i in range(0, file_size, chunk_size):
                    # [Fix] Check cancellation
                    if check_cancel and check_cancel():
                        raise asyncio.CancelledError("Upload cancelled by user")

                    part_num = i // chunk_size + 1
                    chunk_data = content[i : i + chunk_size]
                    
                    # 不使用增量哈希Context，避免 ContextCompareFailed
                    auth = await self._get_upload_auth(task_id, mime_type, part_num, auth_info, upload_id, obj_key, bucket, "")
                    etag = await self._upload_part_to_oss(auth['upload_url'], auth['headers'], chunk_data)
                    xml_parts.append(f'<Part>\n<PartNumber>{part_num}</PartNumber>\n<ETag>"{etag}"</ETag>\n</Part>')
                    
                    logger.info(f"[QUARK] [UPLOAD] 分片 {part_num}/{total_chunks} 上传完成 ({file_name})")
                    
                    # 调用进度回调
                    if progress_callback:
                        try:
                            await progress_callback(i + len(chunk_data), file_size)
                        except:
                            pass
                
                xml_data = '<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>\n' + '\n'.join(xml_parts) + '\n</CompleteMultipartUpload>'
                post_auth = await self._get_complete_upload_auth(task_id, upload_id, obj_key, bucket, xml_data, callback_info)
                await self._complete_multipart_upload(post_auth['upload_url'], post_auth['headers'], xml_data)

            # 4. 完成
            await asyncio.sleep(1) # 保持延迟
            logger.info(f"[QUARK] 上传完成，正在确认任务 ({file_name})")
            finish_res = await self._finish_upload(task_id, obj_key)
            
            fid = finish_res.get("fid") or finish_res.get("file_id")
            logger.info(f"[QUARK] 文件上传成功: {file_name}, fid={fid}")
            return self.ok("上传成功", {"fid": fid})

        except Exception as e:
            logger.error(f"[QUARK] 上传异常 ({file_name}): {str(e)}")
            return self.error(f"夸克上传失败: {str(e)}")
    
    async def create_folder(self, folder_name: str, pdir_fid: str = "0") -> Dict[str, Any]:
        """在指定目录下创建文件夹（带并发冲突重试）"""
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                data = {
                    "pdir_fid": pdir_fid,
                    "file_name": folder_name,
                    "dir_path": "",
                    "dir_init_lock": False
                }
                params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
                logger.info(f"[QUARK] 正在创建文件夹: {folder_name} (Parent: {pdir_fid}, Attempt: {attempt + 1})")
                res = await self._request(f"{self.BASE_URL}/file", "POST", data=data, params=params)
                
                if res.get("status") == 200 and res.get("data"):
                    logger.info(f"[QUARK] 文件夹创建成功: {folder_name} (FID: {res['data'].get('fid')})")
                    return self.ok("创建成功", {"fid": res["data"].get("fid")})
                elif res.get("code") == 23009 or "同名冲突" in res.get("message", "") or "doloading" in res.get("message", ""):
                    # 文件夹已存在，尝试获取其 fid
                    # 添加短暂延迟，让目录列表有时间更新
                    await asyncio.sleep(0.5 * (attempt + 1))
                    
                    list_res = await self.get_files(pdir_fid)
                    if list_res.get("code") == 200:
                        for item in list_res.get("data", []):
                            if item.get("name") == folder_name and item.get("is_dir"):
                                logger.info(f"[QUARK] 文件夹已存在: {folder_name} (FID: {item.get('fid')})")
                                return self.ok("文件夹已存在", {"fid": item.get("fid")})
                    
                    if attempt < max_retries - 1:
                        logger.warning(f"[QUARK] 文件夹 '{folder_name}' 冲突但未找到, 重试 {attempt + 1}/{max_retries}")
                        continue
                    logger.error(f"[QUARK] 文件夹已存在但无法获取 fid ({folder_name})")
                    return self.error("文件夹已存在但无法获取 fid")
                else:
                    err_msg = res.get("message", "创建失败")
                    logger.error(f"[QUARK] 文件夹创建失败 ({folder_name}): {err_msg}")
                    return self.error(err_msg)
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5)
                    continue
                return self.error(f"创建文件夹失败: {str(e)}")
        
        return self.error("创建文件夹失败: 超过最大重试次数")
    
    async def get_or_create_path(self, path: str) -> Dict[str, Any]:
        """递归创建路径并返回最终目录的 fid（带并发锁）"""
        path = path.strip('/')
        if not path or path == "0":
            return self.ok("根目录", {"fid": "0"})
        
        # 检查缓存
        if path in self._path_cache:
            return self.ok("路径已缓存", {"fid": self._path_cache[path]})
        
        # 使用锁防止并发创建同一路径
        async with self._path_lock:
            # 再次检查缓存（可能在等待锁期间被其他任务创建）
            if path in self._path_cache:
                return self.ok("路径已缓存", {"fid": self._path_cache[path]})
            
            try:
                parts = path.split('/')
                current_fid = "0"
                current_path = ""
                
                for part in parts:
                    if not part:
                        continue
                    
                    current_path = f"{current_path}/{part}" if current_path else part
                    
                    # 先检查缓存
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
                        create_res = await self.create_folder(part, current_fid)
                        if create_res.get("code") != 200:
                            return self.error(f"创建目录 {part} 失败: {create_res.get('message')}")
                        current_fid = create_res.get("data", {}).get("fid")
                        self._path_cache[current_path] = current_fid
                
                return self.ok("路径已就绪", {"fid": current_fid})
            except Exception as e:
                return self.error(f"创建路径失败: {str(e)}")
    

    async def _get_share_password(self, share_id: str) -> Dict[str, Any]:
        """获取分享链接"""
        data = {"share_id": share_id}
        params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        return await self._request(f"{self.BASE_URL}/share/password", "POST", data=data, params=params)
    

    # ============ 上传相关辅助方法 ============

    @staticmethod
    def _calculate_sha1_incremental_state(data: bytes) -> Tuple[int, int, int, int, int]:
        """SHA1增量状态计算"""
        h0, h1, h2, h3, h4 = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0
        data_len = len(data)
        for i in range(0, data_len - (data_len % 64), 64):
            block = data[i:i + 64]
            w = list(struct.unpack('>16I', block)) + [0] * 64
            for t in range(16, 80):
                w[t] = ((w[t-3] ^ w[t-8] ^ w[t-14] ^ w[t-16]) << 1 | (w[t-3] ^ w[t-8] ^ w[t-14] ^ w[t-16]) >> 31) & 0xFFFFFFFF
            a, b, c, d, e = h0, h1, h2, h3, h4
            for t in range(80):
                if t < 20: f, k = (b & c) | (~b & d), 0x5A827999
                elif t < 40: f, k = b ^ c ^ d, 0x6ED9EBA1
                elif t < 60: f, k = (b & c) | (b & d) | (c & d), 0x8F1BBCDC
                else: f, k = b ^ c ^ d, 0xCA62C1D6
                a, b, c, d, e = ((a << 5 | a >> 27) + f + e + k + w[t]) & 0xFFFFFFFF, a, (b << 30 | b >> 2) & 0xFFFFFFFF, c, d
                h0, h1, h2, h3, h4 = (h0 + a) & 0xFFFFFFFF, (h1 + b) & 0xFFFFFFFF, (h2 + c) & 0xFFFFFFFF, (h3 + d) & 0xFFFFFFFF, (h4 + e) & 0xFFFFFFFF
        return h0, h1, h2, h3, h4

    def _calculate_incremental_hash_context(self, previous_data: bytes, part_number: int) -> str:
        """计算增量哈希上下文"""
        chunk_size = 4 * 1024 * 1024
        processed_bits = (part_number - 1) * chunk_size * 8
        sha1_hex = hashlib.sha1(previous_data).hexdigest()
        feature_key = sha1_hex[:8]
        
        known_mappings = {
            'e50c2aba': {'h0': 2038062192, 'h1': 1156653562, 'h2': 2676986762, 'h3': 923228148, 'h4': 2314295291},
            'c85c1b38': {'h0': 4257391254, 'h1': 2998800684, 'h2': 2953477736, 'h3': 3425592001, 'h4': 1131671407},
            'fa7a3c46': {'h0': 1241139035, 'h1': 2735429804, 'h2': 1227958958, 'h3': 322089921, 'h4': 1130180806},
            '3146dae9': {'h0': 88233405, 'h1': 3250188692, 'h2': 4088466285, 'h3': 4145561436, 'h4': 4207629818},
        }
        
        if feature_key in known_mappings:
            kh = known_mappings[feature_key]
            h0, h1, h2, h3, h4 = kh['h0'], kh['h1'], kh['h2'], kh['h3'], kh['h4']
        else:
            h0, h1, h2, h3, h4 = self._calculate_sha1_incremental_state(previous_data)

        hash_context = {
            "hash_type": "sha1", "h0": str(h0), "h1": str(h1), "h2": str(h2), "h3": str(h3), "h4": str(h4),
            "Nl": str(processed_bits), "Nh": "0", "data": "", "num": "0"
        }
        return base64.b64encode(json.dumps(hash_context, separators=(',', ':')).encode('utf-8')).decode('utf-8')

    async def _pre_upload(self, file_name: str, file_size: int, pdir_fid: str, mime_type: str, parallel: bool = True) -> Dict[str, Any]:
        """预上传
        
        Args:
            parallel: 是否启用并行上传模式，设为 False 使用串行上传（但仍需要 ccp_hash_update=True 来正常更新哈希）
        """
        data = {
            "ccp_hash_update": True,  # 必须为 True，否则 hash 更新会失败
            "parallel_upload": parallel,  # 控制是否并行上传分片
            "pdir_fid": pdir_fid,
            "dir_name": "", 
            "size": file_size, 
            "file_name": file_name,
            "format_type": mime_type, 
            "l_updated_at": int(time.time() * 1000),
            "l_created_at": int(time.time() * 1000)
        }
        res = await self._request(f"{self.BASE_URL}/file/upload/pre", "POST", data=data, params={'pr': 'ucpro', 'fr': 'pc', 'uc_param_str': ''})
        if res.get("status") != 200:
            raise Exception(res.get("message", "预上传失败"))
        return res.get("data", {})

    async def _get_upload_auth(self, task_id: str, mime_type: str, part_number: int, auth_info: str, upload_id: str, obj_key: str, bucket: str, hash_ctx: str = "") -> Dict[str, Any]:
        """获取上传授权"""
        oss_date = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
        ctx_header = f"x-oss-hash-ctx:{hash_ctx}\n" if hash_ctx else ""
        auth_meta = f"PUT\n\n{mime_type}\n{oss_date}\nx-oss-date:{oss_date}\n{ctx_header}x-oss-user-agent:aliyun-sdk-js/1.0.0 Chrome Mobile 139.0.0.0 on Google Nexus 5 (Android 6.0)\n/{bucket}/{obj_key}?partNumber={part_number}&uploadId={upload_id}"
        
        data = {"task_id": task_id, "auth_info": auth_info, "auth_meta": auth_meta}
        # 添加 params 避免 guest 错误
        params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        res = await self._request(f"{self.BASE_URL}/file/upload/auth", "POST", data=data, params=params)
        if res.get("status") != 200:
            raise Exception(f"获取授权失败: {res.get('message')}")
        
        auth_key = res.get("data", {}).get("auth_key", "")
        upload_url = f"https://{bucket}.pds.quark.cn/{obj_key}?partNumber={part_number}&uploadId={upload_id}"
        headers = {'Content-Type': mime_type, 'x-oss-date': oss_date, 'x-oss-user-agent': 'aliyun-sdk-js/1.0.0 Chrome Mobile 139.0.0.0 on Google Nexus 5 (Android 6.0)'}
        if auth_key: headers['authorization'] = auth_key
        if hash_ctx: headers['X-Oss-Hash-Ctx'] = hash_ctx
        return {'upload_url': upload_url, 'headers': headers}

    async def _upload_part_to_oss(self, upload_url: str, headers: Dict, data: bytes) -> str:
        """上传分片到OSS"""
        import httpx
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.put(upload_url, headers=headers, content=data)
            if resp.status_code != 200:
                raise Exception(f"OSS上传失败: {resp.status_code}")
            return resp.headers.get("ETag", "").strip('"')

    async def _get_complete_upload_auth(self, task_id: str, upload_id: str, obj_key: str, bucket: str, xml_data: str, callback_info: Dict) -> Dict:
        """获取合并文件授权"""
        oss_date = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
        md5_xml = base64.b64encode(hashlib.md5(xml_data.encode('utf-8')).digest()).decode('utf-8')
        
        callback_str = json.dumps(callback_info).replace("/", "\\/")
        b64_callback = base64.b64encode(callback_str.encode('utf-8')).decode('utf-8')
        
        auth_meta = f"POST\n{md5_xml}\napplication/xml\n{oss_date}\nx-oss-callback:{b64_callback}\nx-oss-date:{oss_date}\nx-oss-user-agent:aliyun-sdk-js/1.0.0 Chrome Mobile 139.0.0.0 on Google Nexus 5 (Android 6.0)\n/{bucket}/{obj_key}?uploadId={upload_id}"
        
        data = {"task_id": task_id, "auth_info": callback_info.get("auth_info", ""), "auth_meta": auth_meta}
        params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        res = await self._request(f"{self.BASE_URL}/file/upload/auth", "POST", data=data, params=params)
        if res.get("status") != 200:
             raise Exception(f"获取合并授权失败: {res.get('message')}")
             
        auth_key = res.get("data", {}).get("auth_key", "")
        upload_url = f"https://{bucket}.pds.quark.cn/{obj_key}?uploadId={upload_id}"
        headers = {
            'Content-Type': 'application/xml', 
            'Content-MD5': md5_xml,
            'x-oss-callback': b64_callback,
            'x-oss-date': oss_date, 
            'x-oss-user-agent': 'aliyun-sdk-js/1.0.0 Chrome Mobile 139.0.0.0 on Google Nexus 5 (Android 6.0)'
        }
        if auth_key: headers['authorization'] = auth_key
        return {'upload_url': upload_url, 'headers': headers}


    async def _complete_multipart_upload(self, upload_url: str, headers: Dict, xml_data: str):
        """发送完成合并请求"""
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(upload_url, headers=headers, content=xml_data)
            if resp.status_code not in [200, 203]:
                raise Exception(f"合并文件失败: {resp.status_code}, {resp.text}")

    async def _finish_upload(self, task_id: str, obj_key: str = None) -> Dict[str, Any]:
        """完成上传（通知夸克服务器）"""
        data = {
            "task_id": task_id
        }
        if obj_key:
            data["obj_key"] = obj_key

        params = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
        res = await self._request(f"{self.BASE_URL}/file/upload/finish", "POST", data=data, params=params)
        
        if res.get("status") != 200:
             raise Exception(f"完成上传失败: {res.get('message', '未知错误')}")
             
        return res.get("data", {})

