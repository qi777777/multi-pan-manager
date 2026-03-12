from typing import Dict, List, Any, Optional, Set
from .base import BaseDiskService
from urllib.parse import urlparse
import asyncio
import json
import os
import time
import hashlib
import httpx
import uuid
import re
import base64
import random
from collections import deque
from ...core.logger import logger

# 任务管理器 (IDM 竞速模式核心)

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

class XunleiDiskService(BaseDiskService):
    """迅雷云盘服务"""
    
    BASE_URL = "https://api-pan.xunlei.com"
    CLIENT_ID = "Xqp0kJBXWhwaTpB6"
    # 基础设备配置，将在初始化时转为实例变量
    DEFAULT_DEVICE_ID = "6eee2d8075952ca663cdc6fefbbdca17"
    DEFAULT_DEVICE_SIGN = "wdi10.6eee2d8075952ca663cdc6fefbbdca17dc845c2b670178202c8ed414f0699144"
    CLIENT_VERSION = "1.92.7"
    PACKAGE_NAME = "pan.xunlei.com"
    UA_PC = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"

    # 核心镜像列表 (需长期维护)
    GLOBAL_MIRRORS = [
        "vod0007-h05-vip-lixian.xunlei.com", "vod0008-h05-vip-lixian.xunlei.com", "vod0009-h05-vip-lixian.xunlei.com", 
        "vod0010-h05-vip-lixian.xunlei.com", "vod0011-h05-vip-lixian.xunlei.com", "vod0012-h05-vip-lixian.xunlei.com", 
        "vod0013-h05-vip-lixian.xunlei.com", "vod0014-h05-vip-lixian.xunlei.com", "vod0067-aliyun08-vip-lixian.xunlei.com", 
        "vod0254-aliyun08-vip-lixian.xunlei.com", "vod0255-aliyun08-vip-lixian.xunlei.com", "vod0256-aliyun08-vip-lixian.xunlei.com", 
        "vod0257-aliyun08-vip-lixian.xunlei.com", "vod0258-aliyun08-vip-lixian.xunlei.com", "vod0259-aliyun08-vip-lixian.xunlei.com", 
        "vod0260-aliyun08-vip-lixian.xunlei.com", "vod0261-aliyun08-vip-lixian.xunlei.com", "vod0262-aliyun08-vip-lixian.xunlei.com", 
        "vod0263-aliyun08-vip-lixian.xunlei.com", "vod0264-aliyun08-vip-lixian.xunlei.com", "vod0265-aliyun08-vip-lixian.xunlei.com"
    ]

    def __init__(self, credentials: str, config: Dict[str, Any] = None):
        # 初始化默认值
        self.device_id = self.DEFAULT_DEVICE_ID
        self.device_sign = self.DEFAULT_DEVICE_SIGN
        
        # 路径缓存和锁，改为实例级别以支持多账号独立存储
        self._path_cache = {}
        self._path_lock = asyncio.Lock()
        
        # 尝试从 credentials 中提取真实的 device 信息
        try:
            if credentials and credentials.startswith('{'):
                import json
                cred_dict = json.loads(credentials)
                if isinstance(cred_dict, dict):
                    self.device_id = cred_dict.get("device_id", self.DEFAULT_DEVICE_ID)
                    self.device_sign = cred_dict.get("device_sign", self.DEFAULT_DEVICE_SIGN)
        except Exception:
            pass

        # 必须在 super().__init__ 之前初始化这些变量，因为基类会调用 _build_headers()
        super().__init__(credentials, config)
        
        # 🔒 Token刷新锁与内存缓存
        self._token_lock = asyncio.Lock()
        self._cached_access_token: Optional[str] = None
        self._cached_token_expires_at: float = 0
        self._cached_user_id: Optional[str] = None
        
        # 提取账号特征码用于日志识别
        self.account_tag = f"XUNLEI_{self._md5(credentials)[:6]}"
        self.account_id = config.get("account_id") if config else None

    async def _build_download_headers(self, url: str = "") -> Dict[str, str]:
        """构建专供下载流使用的增强型 Headers"""
        access_token = await self._get_access_token()
        user_id = await self._get_user_id(access_token)
        
        # 极关键：如果 URL 已经包含 at= 令牌，发送 Authorization/Cookie 头会导致 CDN 节点直接 Reset 连接
        parsed = urlparse(url)
        is_vod = 'vod' in parsed.netloc.lower() or 'lixian' in parsed.netloc.lower() or "at=" in url or "token=" in url
            
        headers = {
            "User-Agent": self.UA_PC,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",  # 显式告知镜像节点不进行压缩，提高老旧节点兼容性
            "Connection": "keep-alive",
            "Referer": "https://pan.xunlei.com/",
            "Origin": "https://pan.xunlei.com",
        }
        
        if not is_vod:
            headers["Authorization"] = f"Bearer {access_token}"
            headers["Cookie"] = f"userid={user_id}; sessionid={access_token}"
            
        return headers

    def _build_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "accept": "*/*",
            "accept-language": "zh-hans",
            "content-type": "application/json",
            "Origin": "https://pan.xunlei.com",
            "Referer": "https://pan.xunlei.com/",
            "x-client-id": self.CLIENT_ID,
            "x-client-version-code": "66533",
            "Version-Name": "5.80.0",
            "x-device-id": self.device_id,
            "x-guid": self.device_id,
            "x-net-work-type": "NONE",
            "x-provider-name": "NONE",
        }
    
    async def _get_access_token(self, force_refresh: bool = False) -> str:
        """获取 access_token (带内存加速与数据库持久化)"""
        # 1. 优先检查内存缓存 (极速路径)
        if not force_refresh and self._cached_access_token and self._cached_token_expires_at > (time.time() + 300):
            return self._cached_access_token

        async with self._token_lock:
            # 双重检查
            if not force_refresh and self._cached_access_token and self._cached_token_expires_at > (time.time() + 300):
                return self._cached_access_token

            # 2. 尝试从数据库加载 (中速路径)
            if not force_refresh and self.account_id:
                from ...database import SessionLocal
                from ...models.account import DiskAccount
                
                db = SessionLocal()
                try:
                    account = db.query(DiskAccount).filter(DiskAccount.id == self.account_id).first()
                    if account and account.cached_token and account.token_expires_at:
                        if account.token_expires_at > (time.time() + 300):
                            self._cached_access_token = account.cached_token
                            self._cached_token_expires_at = account.token_expires_at
                            return self._cached_access_token
                except Exception as e:
                    logger.debug(f"[{self.account_tag}] DB token read failed: {e}")
                finally:
                    db.close()
        
            # 2. 缓存过期或不存在，刷新token
            logger.info(f"[{self.account_tag}] 尝试通过 refresh_token 获取新 access_token...")
            
            headers = self._build_headers()
            data = {
                "client_id": self.CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self.credentials
            }


            
            try:
                data_json = json.dumps(data, separators=(',', ':'))
                async with httpx.AsyncClient(timeout=60, verify=False) as client:
                    resp = await client.post("https://xluser-ssl.xunlei.com/v1/auth/token", content=data_json, headers=headers)
                    res = resp.json()
                    
                if res.get("access_token"):
                    logger.info(f"[{self.account_tag}] Token 刷新成功")
                    new_token = res.get("access_token")
                    new_refresh_token = res.get("refresh_token")
                    expires_in = int(res.get("expires_in", 3600))
                    expires_at = int(time.time()) + expires_in - 60
                    
                    # 更新内存缓存
                    self._cached_access_token = new_token
                    self._cached_token_expires_at = expires_at
                    
                    # 3. 保存到数据库 (持久化)
                    if self.account_id:
                        from ...database import SessionLocal
                        from ...models.account import DiskAccount
                        from ...utils.crypto import encrypt_credentials
                        
                        db = SessionLocal()
                        try:
                            account = db.query(DiskAccount).filter(DiskAccount.id == self.account_id).first()
                            if account:
                                account.cached_token = new_token
                                account.token_expires_at = expires_at
                                
                                # 🔥 关键修复：更新refresh_token到credentials
                                # 迅雷的refresh_token是一次性的，必须更新！
                                if new_refresh_token:
                                    account.credentials = encrypt_credentials(new_refresh_token)
                                    self.credentials = new_refresh_token  # 同步更新内存
                                
                                db.commit()
                                logger.info(f"[{self.account_tag}] ✅ Token和RefreshToken已更新")
                        except Exception as e:
                            logger.error(f"[{self.account_tag}] ❌ 数据库更新失败: {e}")
                            db.rollback()
                        finally:
                            db.close()
                    
                    logger.info(f"[{self.account_tag}] Access token 获取成功")
                    return new_token
                else:
                    logger.error(f"[{self.account_tag}] 获取 token 失败: {res}")
            except Exception as e:
                logger.error(f"[{self.account_tag}] 获取 token 异常: {str(e)}", exc_info=True)
            return ""

    async def _ensure_auth(self):
        """确保认证信息可用"""
        self.access_token = await self._get_access_token()
        self.user_id = await self._get_user_id(self.access_token)
        # 验证码token通常在具体操作中获取，这里只需确保基础认证
        # if not getattr(self, 'captcha_token', None):
        #      pass

    @staticmethod
    def _md5(text: str) -> str:
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    def _get_captcha_sign(self, device_id: str, timestamp: str) -> str:
        """根据迅雷算法计算验证码签名"""
        encrypt_str = self.CLIENT_ID + self.CLIENT_VERSION + self.PACKAGE_NAME + device_id + timestamp
        algorithms = [
            "tLtrq5x+xxrRUu/UZIkkmU7",
            "aY9UFtoJqlGBOlpIT4UqWV",
            "pzx2CL",
            "6+IRN8a54byaIb8CyZbCceF",
            "hUE5qOFDsKQNddzYdvRGL",
            "xf1GdhWsa10qL/9rI8rz4qP49z",
            "MSruvHHfmkLmKPR3aFWM3LhR/8lD8f",
            "ZjGCb/ZAEKoTYkaebUVxJkDs",
            "boiLyrw+QqMmJ9gLr2yJXIk7XzQ9Gczik",
            "CahH/PwyYm/",
            "KgX/LKfw42dYMOxYLxeQ0RM4AZr99H/2t"
        ]
        for Algorithm in algorithms:
            encrypt_str = self._md5(encrypt_str + Algorithm)
        return "1." + encrypt_str

    async def _request(
        self, 
        url: str, 
        method: str = "GET", 
        data: Dict = None, 
        params: Dict = None,
        headers: Dict = None,
        retry_on_401: bool = True
    ) -> Dict[str, Any]:
        """增强的请求方法，支持强制无空格 JSON 和 401 自动重试"""
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            request_headers = headers or self._build_headers()
            
            if method.upper() == "GET":
                response = await client.get(url, params=params, headers=request_headers)
            else:
                if data is not None:
                    content = json.dumps(data, separators=(',', ':'))
                    response = await client.request(method.upper(), url, content=content, params=params, headers=request_headers)
                else:
                    response = await client.request(method.upper(), url, params=params, headers=request_headers)
            
            # 手动处理 401
            if response.status_code == 401 and retry_on_401:
                logger.warning(f"[{self.account_tag}] 收到 401，尝试刷新 Token 后重试...")
                new_token = await self._get_access_token(force_refresh=True)
                if new_token:
                    request_headers["Authorization"] = f"Bearer {new_token}"
                    # 递归请求，不再重试
                    return await self._request(url, method, data, params, request_headers, retry_on_401=False)
            
            try:
                return response.json()
            except:
                return {"error_code": response.status_code, "error_description": f"HTTP Error {response.status_code}"}

    async def _risk_report(self):
        """刷新设备指纹标识"""
        url = "https://xluser-ssl.xunlei.com/risk?cmd=report"
        xl_fp_raw = str(uuid.uuid4()).replace("-", "")
        xa = "a12247ffc2d9"
        data = {
            "xl_fp_raw": xl_fp_raw,
            "xl_fp": self._md5(xl_fp_raw),
            "version": 2,
            "xl_fp_sign": self._md5(xa + xl_fp_raw)
        }
        try:
            res = await self._request(url, "POST", data=data)
            if res.get("deviceid"):
                self.device_sign = res["deviceid"]
                match = re.search(r'\.([a-f0-9]{32})', self.device_sign)
                if match:
                    self.device_id = match.group(1)
                logger.info(f"[{self.account_tag}] 设备指纹更新成功: {self.device_id[:6]}...")
        except:
            pass

    async def _get_user_id(self, token: str) -> str:
        """从 JWT 获取 user_id (内存缓存)"""
        if self._cached_user_id:
            return self._cached_user_id
        try:
            parts = token.split('.')
            if len(parts) == 3:
                p = parts[1]
                p += "=" * ((4 - len(p) % 4) % 4)
                payload_json = base64.b64decode(p).decode('utf-8')
                payload = json.loads(payload_json)
                self._cached_user_id = str(payload.get("sub", ""))
                return self._cached_user_id
        except:
            pass
        return ""
    
    async def _get_captcha_token(self, action: str = "get:/drive/v1/files") -> str:
        """获取极简下载/列目录所需的验证码 Token"""
        timestamp = str(int(time.time() * 1000))
        
        access_token = await self._get_access_token()
        user_id = await self._get_user_id(access_token)
        
        headers = self._build_headers()
        data = {
            "client_id": self.CLIENT_ID,
            "action": action,
            "device_id": self.device_id,
            "meta": {
                "username": "",
                "phone_number": "",
                "email": "",
                "package_name": self.PACKAGE_NAME,
                "client_version": self.CLIENT_VERSION,
                "captcha_sign": self._get_captcha_sign(self.device_id, timestamp),
                "timestamp": timestamp,
                "user_id": user_id
            }
        }
        
        try:
            res = await self._request("https://xluser-ssl.xunlei.com/v1/shield/captcha/init", "POST", data=data, headers=headers)
            if res.get("captcha_token"):
                return res.get("captcha_token")
            else:
                # 如果获取失败，尝试刷新设备标识再次请求
                logger.warning(f"[{self.account_tag}] 初次获取验证码失败: {res}, 尝试刷新设备指纹后重试...")
                await self._risk_report()
                data["device_id"] = self.device_id
                data["meta"]["captcha_sign"] = self._get_captcha_sign(self.device_id, timestamp)
                res = await self._request("https://xluser-ssl.xunlei.com/v1/shield/captcha/init", "POST", data=data, headers=headers)
                token = res.get("captcha_token", "")
                if not token:
                    logger.error(f"[{self.account_tag}] 获取验证码Token最终失败: {res}")
                return token
        except Exception as e:
            logger.error(f"[{self.account_tag}] 获取验证码Token异常: {str(e)}")
            import traceback
            traceback.print_exc()
        return ""
    
    async def check_status(self) -> bool:
        try:
            token = await self._get_access_token()
            return bool(token)
        except:
            return False
    
    async def get_files(self, pdir_fid: str = "") -> Dict[str, Any]:
        """获取文件列表"""
        try:
            access_token = await self._get_access_token()
            captcha_token = await self._get_captcha_token()
            
            if not access_token:
                return self.error("获取token失败，请重新登录")
            
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {access_token}"
            headers["x-captcha-token"] = captcha_token
            
            parent_id = "" if not pdir_fid or pdir_fid == "0" else pdir_fid
            
            params = {
                "parent_id": parent_id,
                "filters": '{"phase":{"eq":"PHASE_TYPE_COMPLETE"},"trashed":{"eq":false}}',
                "with_audit": "true",
                "thumbnail_size": "SIZE_SMALL",
                "limit": 100
            }
            
            res = await self._request(f"{self.BASE_URL}/drive/v1/files", "GET", params=params, headers=headers)
            
            if res.get("error_code"):
                return self.error(f"{res.get('error_description', '获取文件列表失败')} (code: {res.get('error_code')})")

            files = []
            for item in res.get("files", []):
                audit = item.get("audit") or {}
                is_sensitive = audit.get("status") == "STATUS_SENSITIVE_RESOURCE"
                
                files.append({
                    "fid": item.get("id"),
                    "name": item.get("name"),
                    "size": int(item.get("size", 0)),
                    "is_dir": item.get("kind") == "drive#folder",
                    "updated_at": item.get("modified_time"),
                    "hash": item.get("hash"),
                    "audit_status": audit.get("status", "STATUS_OK"),
                    "is_sensitive": is_sensitive,
                    "disabled": is_sensitive  # UI通用字段
                })
            return self.ok("获取成功", files)
        except Exception as e:
            return self.error(f"获取文件列表失败: {str(e)}")

    async def list_folder_recursive(self, folder_fid: str, base_path: str = "") -> Dict[str, Any]:
        """递归获取文件夹内所有文件（扁平化列表）"""
        all_files = []
        try:
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
                    sub_result = await self.list_folder_recursive(item['fid'], item_path)
                    if sub_result.get("code") == 200:
                        all_files.extend(sub_result.get("data", []))
                else:
                    all_files.append({
                        **item,
                        "relative_path": item_path,
                        "audit_status": item.get("audit_status"),
                        "is_sensitive": item.get("is_sensitive", False),
                        "disabled": item.get("disabled", False)
                    })
            return self.ok("获取成功", all_files)
        except Exception as e:
            return self.error(f"递归获取文件夹失败: {str(e)}")

    async def get_file_download_info(self, fid: str) -> Dict[str, Any]:
        """获取文件下载信息"""
        try:
            access_token = await self._get_access_token()
            captcha_token = await self._get_captcha_token()
            
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {access_token}"
            headers["x-captcha-token"] = captcha_token
            
            params = {"with_audit": "true"}
            res = await self._request(f"{self.BASE_URL}/drive/v1/files/{fid}", "GET", params=params, headers=headers)
            
            if res.get("error_code") == 9:
                # 自动处理验证码
                logger.warning(f"[{self.account_tag}] 遇到验证码 (Error 9)，尝试自动处理...")
                ts = str(int(time.time() * 1000))
                
                # 计算 captcha_sign
                s = self.CLIENT_ID + "1.92.7" + "pan.xunlei.com" + self.device_id + ts
                algs = ["tLtrq5x+xxrRUu/UZIkkmU7", "aY9UFtoJqlGBOlpIT4UqWV", "pzx2CL", "6+IRN8a54byaIb8CyZbCceF", "hUE5qOFDsKQNddzYdvRGL", "xf1GdhWsa10qL/9rI8rz4qP49z", "MSruvHHfmkLmKPR3aFWM3LhR/8lD8f", "ZjGCb/ZAEKoTYkaebUVxJkDs", "boiLyrw+QqMmJ9gLr2yJXIk7XzQ9Gczik", "CahH/PwyYm/", "KgX/LKfw42dYMOxYLxeQ0RM4AZr99H/2t"]
                for a in algs: 
                    s = hashlib.md5((s + a).encode()).hexdigest()
                captcha_sign = "1." + s
                
                # 注意：_request 本身不支持 json 参数，我们手动构造
                captcha_payload = {
                    "client_id": self.CLIENT_ID, "action": "get:/drive/v1/files", "device_id": self.device_id,
                    "meta": {"package_name": "pan.xunlei.com", "client_version": "1.92.7", "timestamp": ts, "user_id": await self._get_user_id(access_token), "captcha_sign": captcha_sign}
                }
                
                # 使用 POST + json 必须使用 httpx.AsyncClient().post(json=...) 或者手动转 data + Content-Type
                # 既然 _request 封装太死，我们这里直接用一个新的 httpx client 仅仅完成这一步
                # 这样做最安全，不破坏现有 _request 逻辑
                try:
                    async with httpx.AsyncClient(verify=False, timeout=10.0) as captcha_client:
                       cr = await captcha_client.post(
                           "https://xluser-ssl.xunlei.com/v1/shield/captcha/init", 
                           json=captcha_payload, 
                           headers=headers
                       )
                       captcha_res = cr.json()
                       logger.info(f"[{self.account_tag}] 验证码响应: {captcha_res}")
                       
                       t = captcha_res.get("captcha_token")
                       if t:
                            self._captcha_token = t # 保存一下
                            headers["x-captcha-token"] = t
                            res = await self._request(f"{self.BASE_URL}/drive/v1/files/{fid}", "GET", params=params, headers=headers)
                except Exception as e:
                    logger.error(f"[{self.account_tag}] 验证码自动处理失败: {e}")
            
            if res.get("error_code"):
                # 如果是 16 (Unauthenticated)，且我们还没重试过
                return self.error(f"获取下载信息失败: {res.get('error_description')} (code: {res.get('error_code')})")
            
            download_url = res.get("web_content_link")
            if not download_url and res.get("links"):
                # 修复：links 里的内容通常是对象而非直接 URL
                octet_link = res.get("links", {}).get("application/octet-stream")
                if isinstance(octet_link, dict):
                    download_url = octet_link.get("url")
                else:
                    download_url = octet_link or res.get("links", {}).get("application/zip")
            
            # 刚转存的文件可能还没生成下载链接，重试等待
            if not download_url:
                # 文件夹没有下载链接，直接返回（不重试）
                if res.get("kind") == "drive#folder":
                    logger.info(f"[{self.account_tag}] fid {fid} 是文件夹(drive#folder)，无下载链接")
                    return self.ok("获取成功", {
                        "fid": res.get("id"),
                        "file_name": res.get("name"),
                        "size": int(res.get("size", 0)),
                        "md5": res.get("hash"),
                        "download_url": None,
                        "is_folder": True,
                        "is_sensitive": False
                    })
                
                # 调试：打印完整响应关键字段
                logger.warning(f"[{self.account_tag}] 文件 {fid} 下载链接为空. "
                             f"web_content_link={res.get('web_content_link')}, "
                             f"links={res.get('links')}, "
                             f"kind={res.get('kind')}, "
                             f"phase={res.get('phase')}, "
                             f"medias={res.get('medias')}, "
                             f"keys={list(res.keys())}")
                
                import asyncio as _asyncio
                for retry in range(2):
                    logger.info(f"[{self.account_tag}] 文件 {fid} 等待 3 秒后重试 ({retry+1}/2)...")
                    await _asyncio.sleep(3)
                    res = await self._request(f"{self.BASE_URL}/drive/v1/files/{fid}", "GET", params=params, headers=headers)
                    download_url = res.get("web_content_link")
                    if not download_url and res.get("links"):
                        octet_link = res.get("links", {}).get("application/octet-stream")
                        if isinstance(octet_link, dict):
                            download_url = octet_link.get("url")
                        else:
                            download_url = octet_link or res.get("links", {}).get("application/zip")
                    if download_url:
                        logger.info(f"[{self.account_tag}] 文件 {fid} 下载链接已就绪")
                        break
            
            audit = res.get("audit") or {}
            is_sensitive = audit.get("status") == "STATUS_SENSITIVE_RESOURCE"

            return self.ok("获取成功", {
                "fid": res.get("id"),
                "file_name": res.get("name"),
                "size": int(res.get("size", 0)),
                "md5": res.get("hash"),
                "download_url": download_url,
                "is_sensitive": is_sensitive
            })
        except Exception as e:
            return self.error(f"获取下载信息异常: {str(e)}")

    # 精选的高质量镜像类别 (验证通过列表)
    GLOBAL_MIRRORS = [
        "vod0007-h05-vip-lixian.xunlei.com", "vod0008-h05-vip-lixian.xunlei.com", "vod0009-h05-vip-lixian.xunlei.com", 
        "vod0010-h05-vip-lixian.xunlei.com", "vod0011-h05-vip-lixian.xunlei.com", "vod0012-h05-vip-lixian.xunlei.com", 
        "vod0013-h05-vip-lixian.xunlei.com", "vod0014-h05-vip-lixian.xunlei.com", "vod0067-aliyun08-vip-lixian.xunlei.com", 
        "vod0254-aliyun08-vip-lixian.xunlei.com", "vod0255-aliyun08-vip-lixian.xunlei.com", "vod0256-aliyun08-vip-lixian.xunlei.com", 
        "vod0257-aliyun08-vip-lixian.xunlei.com", "vod0258-aliyun08-vip-lixian.xunlei.com", "vod0259-aliyun08-vip-lixian.xunlei.com", 
        "vod0260-aliyun08-vip-lixian.xunlei.com", "vod0261-aliyun08-vip-lixian.xunlei.com", "vod0262-aliyun08-vip-lixian.xunlei.com", 
        "vod0263-aliyun08-vip-lixian.xunlei.com", "vod0264-aliyun08-vip-lixian.xunlei.com", "vod0265-aliyun08-vip-lixian.xunlei.com"
    ]

    # 动态黑名单 (Session 级别，失败后自动屏蔽以减少噪音)
    _mirrors_blacklist: Set[str] = set()

    async def _pick_mirrors(self, mirrors: List[str], cookie: str, top_k: int = 3, base_url: str = "") -> List[str]:
        """增强集群感知探测：锁定并探测同集群镜像以避免 404"""
        if len(mirrors) <= 1: return mirrors[:1]
        
        # 提取原始集群关键词 (如 aliyun08)
        cluster_key = ""
        if base_url:
            host_parts = urlparse(base_url).netloc.split('-')
            if len(host_parts) > 1:
                cluster_key = host_parts[1] # 提取集群标识
        
        # 优先选择同集群节点
        prioritized = [m for m in mirrors if cluster_key and cluster_key in m]
        others = [m for m in mirrors if m not in prioritized]
        # 限制探测数量，避免风控 (Reduced to 15 to avoid captcha)
        candidates = (prioritized + others)[:15] 
        
        valid_mirrors = []
        async def check(hostname):
            try:
                parsed_base = urlparse(base_url)
                urls = [
                    f"http://{hostname}{parsed_base.path}?{parsed_base.query}",
                    f"https://{hostname}{parsed_base.path}?{parsed_base.query}"
                ]
                for url in urls:
                    headers = await self._build_download_headers(url)
                    headers["Range"] = "bytes=0-0"
                    start = time.time()
                    try:
                        async with httpx.AsyncClient(verify=False, timeout=2.5, http2=False) as client:
                            resp = await client.get(url, headers=headers)
                            # 🚀 优化：403 也视为有效镜像 (可能是因为没带鉴权头，但节点是活的)
                            if resp.status_code in [200, 206, 403]:
                                return (url, (time.time() - start) * 1000)
                    except: continue
            except: pass
            return None

        logger.info(f"[{self.account_tag}] 探测集群 {cluster_key or 'Any'} 中的 {len(candidates)} 个镜像...")
        tasks = [check(m) for m in candidates]
        results = await asyncio.gather(*tasks)
        for res in results:
            if res: valid_mirrors.append(res)
            
        if not valid_mirrors:
            logger.warning(f"[{self.account_tag}] 集群探测未发现有效节点，启动全量镜像漫游模式")
            parsed_base = urlparse(base_url)
            path_query = f"{parsed_base.path}?{parsed_base.query}" if parsed_base.query else parsed_base.path
            return [f"http://{m}{path_query}" for m in mirrors[:10]]
            
        valid_mirrors.sort(key=lambda x: x[1])
        return [x[0] for x in valid_mirrors[:top_k]]

    async def download_slice(self, url: str, offset: int, length: int) -> Optional[bytes]:
        """下载文件分片 (用于秒传验证等)"""
        headers = {
            "User-Agent": self.UA_PC,
            "Range": f"bytes={offset}-{offset + length - 1}",
            "Connection": "keep-alive"
        }
        
        # 处理鉴权 (VOD/Lixian 不需要)
        if 'vod' not in url and 'lixian' not in url and 'at=' not in url:
             try:
                 token = await self._get_access_token()
                 user_id = await self._get_user_id(token)
                 headers["Authorization"] = f"Bearer {token}"
                 headers["Cookie"] = f"userid={user_id}; sessionid={token}"
             except:
                 pass

        try:
             async with httpx.AsyncClient(verify=False, timeout=30.0, http2=False) as client:
                resp = await client.get(url, headers=headers, follow_redirects=True)
                if resp.status_code in [200, 206]:
                    return resp.content
        except Exception as e:
            logger.warning(f"[{self.account_tag}] download_slice failed: {e}")
        
        # 失败尝试 curl_cffi
        try:
            from curl_cffi.requests import AsyncSession
            async with AsyncSession(impersonate="chrome110", verify=False, timeout=30) as session:
                resp = await session.get(url, headers=headers, allow_redirects=True)
                if resp.status_code in [200, 206]:
                    return resp.content
        except Exception:
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
        """下载文件 (使用统一并发下载器 + Cluster-Aware 镜像加速)"""
        try:
            from ..download_manager import get_downloader
            import uuid
            import random
            from urllib.parse import urlparse
            
            # 1. 获取下载信息
            info = await self.get_file_download_info(fid)
            if info.get("code") != 200:
                logger.warning(f"[{self.account_tag}] 获取下载信息失败: {info.get('message')}")
                return False
            
            data = info["data"]
            original_url = data.get("download_url")
            file_size = int(data.get("size", 0))
            
            if not original_url:
                logger.error(f"[{self.account_tag}] 文件 {fid} 未返回下载URL (web_content_link为空)")
                return False
            
            # 1.1 预生成镜像列表
            parsed_url = urlparse(original_url)
            original_host = parsed_url.netloc
            
            all_mirrors = self.GLOBAL_MIRRORS[:]
            all_mirror_urls = []
            for m in all_mirrors:
                for j in range(2): 
                    # 宿主替换法，避免任何路径构造偏差
                    m_url = original_url.replace(original_host, m) + ("&" * j)
                    all_mirror_urls.append(m_url)
            
            # 🚀 集群优化：移除 _pick_mirrors 预探测，实现秒级开跑
            # 将探测逻辑下放至 Worker 内部动态进行
            
            # 2. 定义 Fetcher
            async def xunlei_chunk_fetcher(start: int, end: int, chunk_idx: int, httpx_client=None, curl_session=None, proven_mirrors=None, fetcher_task_id=None, worker_id=None) -> Optional[bytes]:
                """分片下载器实现"""
                try:
                    # 🚀 并发优化：Worker 级别客户端持久化仓库
                    # 在 WrappedFetcher 的闭包范围内为每个 Worker 维护一个专属长连接客户端
                    nonlocal worker_clients
                    if worker_id not in worker_clients:
                        # 客户端超时与并发配置
                        worker_clients[worker_id] = httpx.AsyncClient(
                            verify=False, 
                            http2=False, 
                            timeout=30.0,
                            # 隔离池：每个 Worker 物理独立
                            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2)
                        )
                    client = worker_clients[worker_id]

                    # 动态构建本次请求的候选列表
                    url_candidates = []
                    
                    # A. 优先尝试已证实的镜像
                    if proven_mirrors:
                        async with mirrors_lock:
                            url_candidates.extend(proven_mirrors[:10])
                    
                    # B. 深度采样变体池 (对齐 801 行：抽 30 个变体，最终限制到 20 个候选)
                    if len(url_candidates) < 10:
                        random_samples = random.sample(all_mirror_urls, min(30, len(all_mirror_urls)))
                        for m_url in random_samples:
                            # 强制转为 http
                            target_url = m_url.replace("https://", "http://", 1)
                            if target_url not in url_candidates:
                                url_candidates.append(target_url)
                        url_candidates = url_candidates[:20] 
                    
                    # C. 源站 HTTP 保底 (对齐脚本 808 行)
                    source_http = original_url.replace("https://", "http://", 1)
                    if source_http not in url_candidates:
                        url_candidates.append(source_http)
                    
                    # 🚀 响应加速：使用 8s 连接超时保护优质节点 
                    timeout_config = httpx.Timeout(8.0, read=30.0, connect=8.0) 
                    
                    for url in url_candidates:
                        # 检查取消
                        from app.api.cross_transfer import TASK_CANCEL_EVENTS
                        if fetcher_task_id and fetcher_task_id in TASK_CANCEL_EVENTS:
                            if TASK_CANCEL_EVENTS[fetcher_task_id].is_set(): return None

                        # 构造基础请求头
                        parsed = urlparse(url)
                        is_vod = 'vod' in parsed.netloc.lower() or 'lixian' in parsed.netloc.lower() or 'at=' in url or 'token=' in url
                        
                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
                            "Referer": "https://pan.xunlei.com/",
                            "Connection": "keep-alive",
                            "Range": f"bytes={start}-{end}"
                        }
                        if not is_vod:
                             token = await self._get_access_token()
                             if token: headers["Authorization"] = f"Bearer {token}"
                        
                        try:
                            # 🚀 执行对齐读取 (使用专用隔离 Client)
                            async with client.stream("GET", url, headers=headers, follow_redirects=True, timeout=timeout_config) as r:
                                if r.status_code in [200, 206]:
                                    # 对齐备份脚本 821 行
                                    data = await asyncio.wait_for(r.aread(), timeout=30.0)
                                    if len(data) == (end - start + 1):
                                        # 记录并在成功列表中置顶 (对齐脚本 833 行)
                                        if url != source_http:
                                            async with mirrors_lock:
                                                if url not in proven_mirrors:
                                                    proven_mirrors.insert(0, url)
                                                    if len(proven_mirrors) > 20: proven_mirrors.pop()
                                        return data
                        except:
                            continue 
                    
                    return None
                except Exception as e:
                    logger.error(f"Fetcher error: {e}")
                    return None

            # 4. 创建任务
            downloader = get_downloader()
            dl_task_id = task_id if task_id else f"xl_{uuid.uuid4().hex[:8]}"
            
            proven_mirrors = []
            mirrors_lock = asyncio.Lock()
            worker_clients = {} # 🚀 对齐核心：Worker 物理隔离客户端池
            
            async def wrapped_fetcher(s, e, idx, worker_id=None):
                return await xunlei_chunk_fetcher(s, e, idx, None, None, proven_mirrors, dl_task_id, worker_id=worker_id)

            try:
                await downloader.create_task(
                    task_id=dl_task_id,
                    url=original_url,
                    output_path=save_path,
                    concurrency=16, 
                    custom_chunk_fetcher=wrapped_fetcher,
                    file_size=file_size,
                    chunk_size=1 * 1024 * 1024
                )
                
                result = await downloader.start(dl_task_id, progress_callback)
            finally:
                # 清理隔离池
                for c in worker_clients.values(): await c.aclose()
            
            return bool(result)
            
        except Exception as e:
            logger.error(f"[{self.account_tag}] 下载异常: {str(e)}")
            import traceback
            traceback.print_exc()
            return False
    async def create_folder(self, name: str, parent_id: str = "0") -> Dict[str, Any]:
        """创建文件夹"""
        try:
            access_token = await self._get_access_token()
            captcha_token = await self._get_captcha_token()
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {access_token}"
            headers["x-captcha-token"] = captcha_token
            
            pid = "" if not parent_id or parent_id == "0" else parent_id
            data = {"parent_id": pid, "name": name, "kind": "drive#folder", "space": ""}
            
            logger.info(f"[XUNLEI] [{self.account_tag}] 正在创建文件夹: {name} (Parent: {pid})")
            res = await self._request(f"{self.BASE_URL}/drive/v1/files", "POST", data=data, headers=headers)
            if res.get("error_code"):
                err_desc = res.get("error_description", "创建文件夹失败")
                logger.error(f"[XUNLEI] [{self.account_tag}] 文件夹创建失败 ({name}): {err_desc}")
                return self.error(err_desc)
            
            logger.info(f"[XUNLEI] [{self.account_tag}] 文件夹创建成功: {name} (FID: {res.get('file', {}).get('id')})")
            return self.ok("创建成功", {"fid": res.get("file", {}).get("id")})
        except Exception as e:
            logger.error(f"[XUNLEI] [{self.account_tag}] 文件夹创建异常 ({name}): {str(e)}")
            return self.error(f"创建文件夹异常: {str(e)}")

    async def _init_upload(self, name: str, size: int, parent_id: str = "") -> Dict[str, Any]:
        """初始化上传"""
        try:
            access_token = await self._get_access_token()
            captcha_token = await self._get_captcha_token()
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {access_token}"
            headers["x-captcha-token"] = captcha_token
            
            pid = "" if not parent_id or parent_id == "0" else parent_id
            logger.debug(f"[{self.account_tag}] 初始化上传: parent_id={parent_id}, pid={pid}, name={name}")
            data = {
                "kind": "drive#file",
                "parent_id": pid,
                "name": name,
                "size": size,
                "space": "",
                "upload_type": "UPLOAD_TYPE_FORM"
            }
            
            res = await self._request(f"{self.BASE_URL}/drive/v1/files", "POST", data=data, headers=headers)
            logger.debug(f"[{self.account_tag}] 初始化上传响应: {res}")
            if res.get("error_code"):
                return self.error(res.get("error_description", "初始化上传失败"))
            
            return self.ok("初始化成功", res)
        except Exception as e:
            return self.error(f"初始化上传异常: {str(e)}")

    async def upload_file(self, file_data: Any, file_name: str, pdir_fid: str = "0", progress_callback=None, check_cancel=None) -> Dict[str, Any]:
        """上传文件到指定目录 (直接通过 FID)"""
        try:
            # 获取文件大小
            if hasattr(file_data, 'seek'):
                file_data.seek(0, os.SEEK_END)
                size = file_data.tell()
                file_data.seek(0)
            else:
                size = len(file_data)

            # 初始化上传
            # 注意：pdir_fid 如果为空或为 "0"，迅雷可能需要特殊的根目录处理或者就是 "0"
            # 这里的 _init_upload 已经处理了 "0" 的情况
            init_res = await self._init_upload(file_name, size, pdir_fid)
            if init_res.get("code") != 200:
                return init_res
            
            form_info = init_res["data"]["form"]
            upload_url = form_info["url"]
            multi_parts = form_info["multi_parts"]
            
            logger.info(f"[XUNLEI] [UPLOAD] 初始化上传成功: {file_name} (Size: {size}B, URL已获取)")
            
            # 执行实际上传
            async with httpx.AsyncClient(timeout=600, verify=False) as client:
                data = multi_parts.copy()
                
                # 包装文件对象以支持进度回调
                wrapped_file = ProgressFileWrapper(file_data, size, progress_callback, check_cancel=check_cancel)
                files = {'file': (file_name, wrapped_file)}
                
                # 迅雷表单上传
                resp = await client.post(upload_url, data=data, files=files, headers={"User-Agent": self.UA_PC})
                
                if resp.status_code not in [200, 204]:
                    logger.error(f"[XUNLEI] [UPLOAD] 数据上传失败 ({resp.status_code}): {resp.text[:200]} ({file_name})")
                    return self.error(f"数据上传失败 ({resp.status_code}): {resp.text[:200]}")
                
                logger.info(f"[XUNLEI] [UPLOAD] 文件数据包投递成功: {file_name}")
            
            # 获取 FID (迅雷的 id 嵌套在 file 对象中)
            fid = init_res.get("data", {}).get("file", {}).get("id")
            return self.ok("上传成功", {"fid": fid})
        except Exception as e:
            logger.error(f"[XUNLEI] [UPLOAD] 上传链路异常 ({file_name}): {str(e)}", exc_info=True)
            return self.error(f"上传异常: {str(e)}")

    async def upload_to_path(self, file_data: Any, file_name: str, target_path: str = "/", progress_callback=None, check_cancel=None) -> Dict[str, Any]:
        """上传文件到指定路径（带并发锁保护）"""
        try:
            parts = [p for p in target_path.split("/") if p]
            current_fid = ""
            
            for part in parts:
                cache_key = f"{current_fid}_{part}"
                
                # 1. 快速检查缓存（无锁）
                if cache_key in self._path_cache:
                    current_fid = self._path_cache[cache_key]
                    continue
                
                # 2. 使用锁保护目录创建
                async with self._path_lock:
                    # 3. 双重检查缓存（避免等锁期间其他协程已创建）
                    if cache_key in self._path_cache:
                        current_fid = self._path_cache[cache_key]
                        continue
                    
                    # 4. 列出当前目录，检查子目录是否存在
                    files_res = await self.get_files(current_fid)
                    found_fid = ""
                    if files_res.get("code") == 200:
                        for f in files_res.get("data", []):
                            if f["name"] == part and f["is_dir"]:
                                found_fid = f["fid"]
                                break
                    
                    # 5. 如果不存在则创建
                    if not found_fid:
                        create_res = await self.create_folder(part, current_fid)
                        if create_res.get("code") == 200:
                            found_fid = create_res["data"]["fid"]
                        else:
                            return self.error(f"创建目录失败: {create_res.get('message')}")
                    
                    # 6. 更新类级别缓存
                    self._path_cache[cache_key] = found_fid
                    current_fid = found_fid

            # 获取文件大小
            if hasattr(file_data, 'seek'):
                file_data.seek(0, os.SEEK_END)
                size = file_data.tell()
                file_data.seek(0)
            else:
                size = len(file_data)

            # 初始化上传
            logger.info(f"[{self.account_tag}] 正在初始化上传: {file_name} (Size: {size}B)")
            init_res = await self._init_upload(file_name, size, current_fid)
            if init_res.get("code") != 200:
                logger.error(f"[{self.account_tag}] 上传初始化失败 ({file_name}): {init_res.get('message')}")
                return init_res
            
            form_info = init_res["data"]["form"]
            upload_url = form_info["url"]
            multi_parts = form_info["multi_parts"]
            
            # 执行实际上传
            async with httpx.AsyncClient(timeout=600, verify=False) as client:
                data = multi_parts.copy()
                
                # 包装文件对象以支持进度回调
                wrapped_file = ProgressFileWrapper(file_data, size, progress_callback, check_cancel=check_cancel)
                files = {'file': (file_name, wrapped_file)}
                
                resp = await client.post(upload_url, data=data, files=files, headers={"User-Agent": self.UA_PC})
                
                if resp.status_code not in [200, 204]:
                    logger.error(f"[{self.account_tag}] 数据上传失败 ({file_name}): {resp.status_code}")
                    return self.error(f"数据上传失败 ({resp.status_code}): {resp.text[:200]}")
            
            logger.info(f"[{self.account_tag}] 文件上传成功: {file_name}")
            return self.ok("上传成功")
        except Exception as e:
            logger.error(f"[XUNLEI] [{self.account_tag}] 上传异常 ({file_name}): {str(e)}")
            return self.error(f"上传异常: {str(e)}")

    async def delete_files(self, fid_list: List[str]) -> Dict[str, Any]:
        """批量删除"""
        try:
            access_token = await self._get_access_token()
            captcha_token = await self._get_captcha_token()
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {access_token}"
            headers["x-captcha-token"] = captcha_token
            data = {"ids": fid_list, "space": ""}
            logger.info(f"[XUNLEI] [{self.account_tag}] 开始批量删除 {len(fid_list)} 个文件/目录, FIDs: {fid_list}")
            await self._request(f"{self.BASE_URL}/drive/v1/files:batchDelete", "POST", data=data, headers=headers)
            logger.info(f"[XUNLEI] [{self.account_tag}] 批量删除完成, 数量: {len(fid_list)}, FIDs: {fid_list}")
            return self.ok("删除成功")
        except Exception as e:
            logger.error(f"[XUNLEI] [{self.account_tag}] 删除失败: FIDs={fid_list}, error={str(e)}")
            return self.error(f"删除失败: {str(e)}")

    # 迅雷网盘 Transfer 方法完整实现
    async def transfer(self, share_url: str, code: str = "", expired_type: int = 1, need_share: bool = True) -> Dict[str, Any]:
        """转存分享资源
        
        Args:
            share_url: 分享链接，如 https://pan.xunlei.com/s/xxx
            code: 提取码
            expired_type: 分享有效期类型 (1=永久)
            need_share: 是否创建新分享
        
        Returns:
            {"code": 200, "data": {"title": "", "share_url": "", "code": "", "fid": []}}
        """
        try:
            # 1. 获取access_token和captcha_token
            if not getattr(self, 'access_token', None) or not getattr(self, 'captcha_token', None):
                await self._ensure_auth()
            
            # 2. 解析分享ID
            import re
            match = re.search(r'/s/([A-Za-z0-9_-]+)', share_url)
            if not match:
                return self.error("分享链接格式错误")
            
            share_id = match.group(1)
            
            # 自动从URL提取提取码
            if not code:
                pwd_match = re.search(r'[?&]pwd=([A-Za-z0-9]+)', share_url)
                if pwd_match:
                    code = pwd_match.group(1)
            
            # 去掉code中可能的#号
            pass_code = code.replace('#', '').strip()
            
            logger.info(f"[XUNLEI] 开始转存分享: share_id={share_id}")
            
            # 3. 获取分享信息
            logger.info(f"[XUNLEI] 正在获取分享信息: share_id={share_id}, code={pass_code}, user_id={self.user_id}")
            share_res = await self._get_share_info(share_id, pass_code)
            if share_res.get("code") != 200:
                logger.error(f"[XUNLEI] 获取分享信息失败: {share_res.get('message')}, res={share_res}")
                return self.error(f"获取分享信息失败: {share_res.get('message')}")
            
            share_data = share_res["data"]
            title = share_data.get("title", "迅雷分享文件")
            pass_code_token = share_data.get("pass_code_token", "")
            file_ids = [f["id"] for f in share_data.get("files", [])]
            
            logger.info(f"[XUNLEI] 获取分享信息成功: {title}, 文件数: {len(file_ids)}")
            
            # 4. 解析用户指定的存储路径，获取目标目录 fid
            target_parent_id = ""  # 默认根目录
            storage_path = self.config.get("storage_path", "")
            if storage_path and storage_path not in ("0", "/", ""):
                # 用户指定了非根目录
                path_res = await self.get_or_create_path(storage_path)
                if path_res.get("code") == 200:
                    target_parent_id = path_res.get("data", {}).get("fid", "")
                    logger.info(f"[XUNLEI] 转存到指定目录: {storage_path} -> fid={target_parent_id}")
                else:
                    logger.warning(f"[XUNLEI] 解析存储路径失败: {storage_path}，使用根目录")
            
            # 5. 转存文件到指定目录
            restore_res = await self._restore_share(share_id, pass_code_token, file_ids, parent_id=target_parent_id)
            if restore_res.get("code") != 200:
                return self.error(f"转存失败: {restore_res.get('message')}")
            
            restore_task_id = restore_res["data"].get("restore_task_id")
            logger.info(f"[XUNLEI] 转存任务ID: {restore_task_id}")
            
            # 5. 轮询转存任务
            task_res = await self._poll_restore_task(restore_task_id, max_retries=20)
            if not task_res or task_res.get("progress") != 100:
                return self.error("转存任务失败或超时")
            
            # 提取转存后的文件ID
            trace_file_ids = task_res.get("params", {}).get("trace_file_ids", "{}")
            import json
            import re
            file_id_map = json.loads(trace_file_ids) if isinstance(trace_file_ids, str) else trace_file_ids
            raw_ids = list(file_id_map.values()) if isinstance(file_id_map, dict) else []
            restored_file_ids = []
            for r_id in raw_ids:
                s_id = str(r_id)
                clean_id = re.sub(r"^\[|\]$|^'|'$|^\"|\"$", "", s_id.strip())
                if clean_id:
                    restored_file_ids.append(clean_id)
            
            logger.info(f"[XUNLEI] 转存成功，文件ID: {restored_file_ids}")
            
            # 6. 根据 need_share 决定是否创建新分享
            if need_share:
                new_share_res = await self.create_share(restored_file_ids, title, expired_type=expired_type)
                if new_share_res.get("code") != 200:
                    return self.error(f"创建分享失败: {new_share_res.get('message')}")
                
                new_share_data = new_share_res["data"]
                share_url_with_code = new_share_data.get("share_url", "")
                pass_code_new = new_share_data.get("password", "")
                
                if pass_code_new and '?pwd=' not in share_url_with_code:
                    share_url_with_code = f"{share_url_with_code}?pwd={pass_code_new}"
                
                logger.info(f"[XUNLEI] 转存并分享成功: {title}")
            else:
                share_url_with_code = ""
                pass_code_new = ""
                logger.info(f"[XUNLEI] 转存成功（不分享）: {title}")
            
            return self.ok("转存成功", {
                "title": title,
                "share_url": share_url_with_code,
                "code": pass_code_new,
                "fid": restored_file_ids
            })
            
        except Exception as e:
            logger.error(f"[XUNLEI] 转存异常: {str(e)}")
            import traceback
            traceback.print_exc()
            return self.error(f"转存异常: {str(e)}")

    async def validate_share(self, share_url: str, password: str = "") -> Dict[str, Any]:
        """
        高精度检测迅雷云盘分享链接有效性
        """
        try:
            # 1. 解析分享ID
            import re
            match = re.search(r'/s/([A-Za-z0-9_-]+)', share_url)
            if not match:
                return self.error("分享链接格式错误")
            share_id = match.group(1)
            
            # 2. 提取提取码
            if not password:
                pwd_match = re.search(r'[?&]pwd=([A-Za-z0-9]+)', share_url)
                if pwd_match:
                    password = pwd_match.group(1)
            
            pass_code = password.replace('#', '').strip()

            # 3. 获取分享信息 (深度探测)
            share_res = await self._get_share_info(share_id, pass_code)
            if share_res.get("code") != 200:
                # 状态不为 200 通常意味着链接失效、过期或敏感
                return self.error(share_res.get("message", "分享链接已失效"))
            
            detail = share_res.get("data", {})
            # 检查是否有文件 (迅雷即使有效也可能 files 为空，通常意味着转存后文件被处理)
            if not detail.get("files"):
                 return self.error("分享内容为空或文件已失效")

            return self.ok("链接有效", detail)

        except Exception as e:
            logger.error(f"[XUNLEI] validate_share 异常: {e}")
            return self.error(f"检测异常: {str(e)}")

    async def _get_share_info(self, share_id: str, pass_code: str):
        """获取分享信息"""
        try:
            url = "https://api-pan.xunlei.com/drive/v1/share"
            params = {
                "share_id": share_id,
                "pass_code": pass_code,
                "limit": 100,
                "pass_code_token": "",
                "page_token": "",
                "thumbnail_size": "SIZE_SMALL"
            }
            
            # 获取验证码token
            captcha_token = await self._get_captcha_token(action="get:/drive/v1/share")
            
            if not captcha_token:
                logger.error(f"[XUNLEI] 无法获取验证码Token")
                return {"code": 500, "message": "无法获取验证码Token"}
            
            # 确保 access_token 已初始化
            access_token = await self._get_access_token()
            if not access_token:
                return {"code": 500, "message": "获取token失败"}
            
            headers = self.headers.copy()
            headers["Authorization"] = f"Bearer {access_token}"
            headers["Cookie"] = f"userid={getattr(self, 'user_id', '')}; sessionid={access_token}"
            headers["x-captcha-token"] = captcha_token
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, params=params, headers=headers)
                result = resp.json()
                
                # [DEBUG] 打印完整响应
                if result.get("error_code") or result.get("share_status") != "OK":
                    logger.error(f"[XUNLEI] API返回错误或状态异常: {result}")
                
                # 检查错误
                if result.get("error_code"):
                    return {"code": 500, "message": result.get("error_description", "获取分享信息失败")}
                
                # 检查分享状态
                share_status = result.get("share_status")
                if share_status and share_status != "OK":
                    if share_status == "SENSITIVE_RESOURCE":
                        return {"code": 500, "message": "该分享内容可能涉及侵权、色情等敏感信息"}
                    if share_status == "PASS_CODE_EMPTY":
                        return {"code": 500, "message": "该分享需要提取码，请提供提取码"}
                    return {"code": 500, "message": result.get("share_status_text", "分享已失效")}
                
                return { "code": 200, "data": result}
        except Exception as e:
            logger.error(f"[XUNLEI] 获取分享信息异常: {str(e)}")
            return {"code": 500, "message": str(e)}

    async def _restore_share(self, share_id: str, pass_code_token: str, file_ids: list, parent_id: str = ""):
        """转存分享文件到自己网盘
        
        Args:
            share_id: 分享ID
            pass_code_token: 提取码token
            file_ids: 文件ID列表
            parent_id: 目标目录ID，空字符串表示根目录
        """
        try:
            url = "https://api-pan.xunlei.com/drive/v1/share/restore"
            data = {
                "parent_id": parent_id,
                "share_id": share_id,
                "pass_code_token": pass_code_token,
                "file_ids": file_ids,
                "ancestor_ids": [],
                "specify_parent_id": True  # 强制使用指定的 parent_id
            }
            
            # 获取验证码token
            captcha_token = await self._get_captcha_token(action="post:/drive/v1/share/restore")
            
            headers = self.headers.copy()
            headers["Authorization"] = f"Bearer {self.access_token}"
            headers["Cookie"] = f"userid={self.user_id}; sessionid={self.access_token}"
            headers["x-captcha-token"] = captcha_token
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, json=data, headers=headers)
                result = resp.json()
                
                if result.get("error_code"):
                    return {"code": 500, "message": result.get("error_description", "转存失败")}
                
                return {"code": 200, "data": result}
        except Exception as e:
            logger.error(f"[XUNLEI] 转存文件异常: {str(e)}")
            return {"code": 500, "message": str(e)}

    async def _poll_restore_task(self, restore_task_id: str, max_retries: int = 20):
        """轮询转存任务状态"""
        try:
            url = f"https://api-pan.xunlei.com/drive/v1/tasks/{restore_task_id}"
            
            # 获取验证码token (Polling needs token too)
            captcha_token = await self._get_captcha_token(action="get:/drive/v1/tasks")
            
            headers = self.headers.copy()
            headers["Authorization"] = f"Bearer {self.access_token}"
            headers["Cookie"] = f"userid={self.user_id}; sessionid={self.access_token}"
            headers["x-captcha-token"] = captcha_token
            
            for i in range(max_retries):
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url, headers=headers)
                    result = resp.json()
                    
                    if result.get("error_code"):
                        err_code = result.get("error")
                        logger.warning(f"[XUNLEI] 查询任务失败: {result.get('error_description')}")
                        
                        # 如果是验证码错误或Token失效，直接终止
                        if err_code == "captcha_invalid" or result.get("error_code") == 401:
                             logger.error(f"[XUNLEI] 致命错误停止轮询: {result}")
                             return None
                             
                        await asyncio.sleep(1)
                        continue
                    
                    # 检查进度
                    if result.get("progress") == 100:
                        logger.info(f"[XUNLEI] 转存任务完成")
                        return result
                    
                    # 等待后继续
                    await asyncio.sleep(1)
            
            logger.error("[XUNLEI] 转存任务轮询超时")
            return None
            
        except Exception as e:
            logger.error(f"[XUNLEI] 轮询任务异常: {str(e)}")
            return None



    
    async def create_share(self, fid_list: List[str], title: str, expired_type: int = 1) -> Dict[str, Any]:
        """创建分享"""
        try:
            access_token = await self._get_access_token()
            captcha_token = await self._get_captcha_token()
            
            if not access_token:
                return self.error("获取token失败")
            
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {access_token}"
            headers["x-captcha-token"] = captcha_token
            
            # 有效期映射 (1=永久, 2=7天, 3=1天, 4=30天)
            input_type = int(expired_type)
            if input_type == 1:
                expiration_days = -1
            elif input_type == 2:
                expiration_days = 7
            elif input_type == 3:
                expiration_days = 1
            elif input_type == 4:
                expiration_days = 30
            else:
                expiration_days = -1  # 默认永久
            data = {
                "file_ids": fid_list,
                "share_to": "copy",
                "params": {"subscribe_push": "false", "WithPassCodeInLink": "true"},
                "title": title,
                "restore_limit": "-1",
                "expiration_days": expiration_days
            }
            
            logger.info(f"[XUNLEI] [{self.account_tag}] 正在创建分享: title='{title}', 数量={len(fid_list)}, fids={fid_list}, expiration_days={expiration_days}")
            res = await self._request(f"{self.BASE_URL}/drive/v1/share", "POST", data=data, headers=headers)
            
            if res.get("error_code"):
                logger.error(f"[XUNLEI] [{self.account_tag}] 创建分享失败: title='{title}', error={res.get('error_description')}")
                return self.error(res.get("error_description", "创建分享失败"))
            
            share_url = res.get("share_url", "")
            # 从 URL 提取 share_id (格式: https://pan.xunlei.com/s/{share_id})
            xl_share_id = res.get("share_id", "")
            if not xl_share_id and share_url:
                parts = share_url.rstrip("/").split("/")
                xl_share_id = parts[-1] if parts else ""
            password = res.get("pass_code")
            final_url = share_url
            if password and "?pwd=" not in final_url:
                final_url = f"{final_url}?pwd={password}"
                
            logger.info(f"[XUNLEI] [{self.account_tag}] 分享创建成功: title='{title}', share_url={final_url}")
            return self.ok("分享成功", {
                "share_id": xl_share_id,
                "share_url": final_url,
                "password": password,
                "title": title,
                "fid": fid_list
            })
        except Exception as e:
            logger.error(f"[XUNLEI] [{self.account_tag}] 创建分享异常: title='{title}', fids={fid_list}, error={str(e)}")
            return self.error(f"创建分享失败: {str(e)}")
    
    async def cancel_share(self, share_id: str) -> Dict[str, Any]:
        """取消/删除迅雷分享"""
        try:
            access_token = await self._get_access_token()
            captcha_token = await self._get_captcha_token()
            if not access_token:
                return self.error("获取token失败")
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {access_token}"
            headers["x-captcha-token"] = captcha_token
            logger.info(f"[XUNLEI] [{self.account_tag}] 正在取消分享: share_id='{share_id}'")
            res = await self._request(
                f"{self.BASE_URL}/drive/v1/share/delete", "POST", headers=headers, data={"share_id": share_id}
            )
            logger.debug(f"[XUNLEI] [{self.account_tag}] 取消分享返回: {res}")
            if not res.get("error_code"):
                logger.info(f"[XUNLEI] [{self.account_tag}] 取消分享成功: share_id='{share_id}'")
                return self.ok("取消分享成功")
            logger.error(f"[XUNLEI] [{self.account_tag}] 取消分享失败: share_id='{share_id}', error={res.get('error_description')}")
            return self.error(res.get("error_description", "取消分享失败"))
        except Exception as e:
            logger.error(f"[XUNLEI] [{self.account_tag}] 取消分享异常: share_id='{share_id}', error={str(e)}")
            return self.error(f"取消分享失败: {str(e)}")
    
    async def get_or_create_path(self, path: str) -> Dict[str, Any]:
        """递归创建路径并返回最终目录的 fid
        
        Args:
            path: 目标路径，如 "/folder1/folder2"
        
        Returns:
            {"code": 200, "data": {"fid": "final_folder_id"}}
        """
        try:
            path = path.strip('/')
            if not path:
                return self.ok("根目录", {"fid": ""})
            
            if path in self._path_cache:
                return self.ok("路径已缓存", {"fid": self._path_cache[path]})
            
            async with self._path_lock:
                if path in self._path_cache:
                    return self.ok("路径已缓存", {"fid": self._path_cache[path]})
                
                # 分割路径
                parts = path.split('/')
                current_fid = ""
                current_path = ""
                
                for part in parts:
                    if not part:
                        continue
                    
                    current_path = f"{current_path}/{part}" if current_path else part
                    
                    if current_path in self._path_cache:
                        current_fid = self._path_cache[current_path]
                        continue
                    
                    # 获取当前文件夹的文件列表
                    files_res = await self.get_files(current_fid)
                    if files_res.get("code") != 200:
                        return self.error(f"获取文件列表失败: {files_res.get('message')}")
                    
                    # 检查文件夹是否已存在
                    found = False
                    for item in files_res.get("data", []):
                        if item.get("name") == part and item.get("is_dir"):
                            current_fid = item.get("fid")
                            self._path_cache[current_path] = current_fid
                            found = True
                            logger.info(f"[XUNLEI] 文件夹已存在: {part} (FID: {current_fid})")
                            break
                    
                    if not found:
                        # 创建文件夹
                        create_res = await self.create_folder(part, current_fid)
                        if create_res.get("code") != 200:
                            return self.error(f"创建目录 {part} 失败: {create_res.get('message')}")
                        current_fid = create_res.get("data", {}).get("fid", "")
                        self._path_cache[current_path] = current_fid
                        logger.info(f"[XUNLEI] 目录创建成功: {part} (FID: {current_fid})")
                
                logger.info(f"[XUNLEI] 路径已就绪: {path} (Final FID: {current_fid})")
                return self.ok("路径已就绪", {"fid": current_fid})
        except Exception as e:
            logger.error(f"[XUNLEI] 创建路径异常: {str(e)}")
            return self.error(f"创建路径失败: {str(e)}")
    

