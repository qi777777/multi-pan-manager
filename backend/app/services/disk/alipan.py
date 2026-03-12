from typing import Dict, List, Any
from .base import BaseDiskService
import re
import json
import os
import hashlib
import mimetypes
import time
import asyncio
import httpx
from ...core.logger import logger


class AlipanDiskService(BaseDiskService):
    """阿里云盘服务"""
    
    BASE_URL = "https://api.aliyundrive.com"
    
    def __init__(self, credentials: str, config: Dict[str, Any] = None):
        super().__init__(credentials, config)
        # 从config中获取account_id（用于数据库查询）
        self.account_id = config.get("account_id") if config else None
        
        # 🔒 Token刷新锁（防止并发刷新冲突）
        # 🔒 Token刷新锁（防止并发刷新冲突）
        import asyncio
        self._token_lock = asyncio.Lock()
        
        # 缓存 drive_id
        self._drive_id = None
    
    def _build_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": "https://www.alipan.com",
            "Referer": "https://www.alipan.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.160",
            "X-Canary": "client=web,app=adrive,version=v6.8.12",
            "X-Device-Id": "1234567890abcdef"
        }
    
    async def _get_access_token(self) -> str:
        """获取并缓存 access_token（使用数据库缓存，带并发保护）"""
        import time
        
        # 🔒 使用异步锁防止并发刷新冲突
        async with self._token_lock:
            # 1. 优先从数据库读取缓存
            if self.account_id:
                from ...database import SessionLocal
                from ...models.account import DiskAccount
                
                db = SessionLocal()
                try:
                    account = db.query(DiskAccount).filter(DiskAccount.id == self.account_id).first()
                    if account and account.cached_token and account.token_expires_at:
                        if account.token_expires_at > time.time():
                            return account.cached_token
                except:
                    pass
                finally:
                    db.close()
        
            # 2. 缓存过期或不存在，刷新token
            headers = {"Content-Type": "application/json"}
            data = {"refresh_token": self.credentials}
            
            res = await self._request(f"{self.BASE_URL}/token/refresh", "POST", data=data, headers=headers)
            access_token = res.get("access_token", "")
            
            if access_token:
                new_refresh_token = res.get("refresh_token")  # 阿里云盘也会返回新refresh_token
                expires_at = int(time.time()) + 3600
                
                # 3. 保存到数据库
                if self.account_id:
                    from ...database import SessionLocal
                    from ...models.account import DiskAccount
                    from ...utils.crypto import encrypt_credentials
                    
                    db = SessionLocal()
                    try:
                        account = db.query(DiskAccount).filter(DiskAccount.id == self.account_id).first()
                        if account:
                            account.cached_token = access_token
                            account.token_expires_at = expires_at
                            
                            # 🔥 关键修复：更新refresh_token到credentials
                            # 阿里云盘的refresh_token也是一次性的！
                            if new_refresh_token:
                                account.credentials = encrypt_credentials(new_refresh_token)
                                self.credentials = new_refresh_token  # 同步更新内存
                            
                            db.commit()
                            logger.info(f"[ALIPAN] Token和RefreshToken已成功更新到数据库")
                            return access_token
                    except Exception as e:
                        logger.error(f"[ALIPAN] 数据库更新失败: {e}", exc_info=True)
                        db.rollback()
                    finally:
                        db.close()
            
    async def _get_drive_id(self) -> str:
        """获取并缓存 default_drive_id"""
        if self._drive_id:
            return self._drive_id
        
        # 1. 优先尝试从配置中读取 drive_id
        if self.config and self.config.get("drive_id"):
             self._drive_id = self.config.get("drive_id")
             logger.debug(f"[ALIPAN] 使用配置中指定的 drive_id: {self._drive_id}")
             return self._drive_id
            
        try:
            # 优先从 access_token 获取（如果支持，但token不包含drive_id）
            # 调用 /v2/user/get 获取
            token = await self._get_access_token()
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {token}"
            
            res = await self._request(f"{self.BASE_URL}/v2/user/get", "POST", data={}, headers=headers)
            logger.debug(f"[ALIPAN] user_id: {res.get('user_id')}")
            logger.debug(f"[ALIPAN] default_drive_id: {res.get('default_drive_id')}")
            logger.debug(f"[ALIPAN] resource_drive_id: {res.get('resource_drive_id')}")
            logger.debug(f"[ALIPAN] backup_drive_id: {res.get('backup_drive_id')}")
            
            drive_id = res.get("resource_drive_id") or res.get("default_drive_id") or ""
            if drive_id:
                logger.debug(f"[ALIPAN] 自动选择了 drive_id: {drive_id}")
                self._drive_id = drive_id
                return drive_id
        except Exception as e:
            logger.error(f"[ALIPAN] 获取 drive_id 失败: {e}", exc_info=True)
        return ""

    async def check_status(self) -> bool:
        """检查登录状态"""
        try:
            token = await self._get_access_token()
            return bool(token)
        except:
            return False
    
    async def get_files(self, pdir_fid: str = "root") -> Dict[str, Any]:
        """获取文件列表"""
        try:
            token = await self._get_access_token()
            if not token:
                return self.error("获取token失败")
            
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {token}"
            
            if str(pdir_fid) == "0":
                pdir_fid = "root"
                
            drive_id = await self._get_drive_id()
            if not drive_id:
                return self.error("无法获取 drive_id")
            
            data = {
                "all": False,
                "drive_id": drive_id, 
                "fields": "*",
                "limit": 100,
                "order_by": "updated_at",
                "order_direction": "DESC",
                "parent_file_id": pdir_fid,
                "url_expire_sec": 14400
            }
            
            res = await self._request(f"{self.BASE_URL}/adrive/v3/file/list", "POST", data=data, headers=headers)
            
            if res.get("message"):
                return self.error(res.get("message"))
            
            files = []
            for item in res.get("items", []):
                files.append({
                    "fid": item.get("file_id"),
                    "name": item.get("name") or item.get("file_name") or item.get("fileName") or "未命名文件",
                    "size": item.get("size", 0),
                    "is_dir": item.get("type") == "folder",
                    "updated_at": item.get("updated_at")
                })
            return self.ok("获取成功", files)
        except Exception as e:
            return self.error(f"获取文件列表失败: {str(e)}")

    async def list_folder_recursive(self, folder_fid: str) -> Dict[str, Any]:
        """递归获取文件夹下所有文件 (用于跨盘传输)"""
        all_files = []
        try:
            if str(folder_fid) == "0":
                folder_fid = "root"
                
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
                            "relative_path": current_path # 相对文件夹根目录的路径（统一命名为 relative_path）
                        })
            
            await _recursive(folder_fid)
            return self.ok("获取成功", all_files)
        except Exception as e:
            return self.error(f"递归获取文件列表失败: {str(e)}")

    async def get_file_download_info(self, fid: str) -> Dict[str, Any]:
        """获取文件下载链接"""
        try:
            token = await self._get_access_token()
            if not token:
                return self.error("获取token失败")
            
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {token}"
            
            drive_id = await self._get_drive_id()
            if not drive_id:
                return self.error("无法获取 drive_id")
            
            data = {
                "drive_id": drive_id,
                "file_id": fid,
                "expire_sec": 14400
            }
            
            res = await self._request(f"{self.BASE_URL}/v2/file/get_download_url", "POST", data=data, headers=headers)
            
            if not res.get("url"):
                err_expr = f"errno: {res.get('code', 'N/A')}, info: {res.get('message', '未知错误')}"
                logger.error(f"[ALIPAN] 获取下载链接失败 ({fid}): {err_expr}")
                return self.error(err_expr)
            
            # v7 fix: 如果 API 没返回 name，就返回 None，不要返回 "未命名文件"
            # 这样 cross_transfer 在合并时就会保留任务创建时获取到的正确文件名
            name = res.get("name") or res.get("file_name") or res.get("fileName")
            logger.info(f"[ALIPAN] 成功获取下载链接: {name or fid}")
            return self.ok("获取成功", {
                "download_url": res.get("url"),
                "size": res.get("size", 0),
                "name": name,
                "file_name": name, 
                "headers": {
                    "Referer": "https://www.aliyundrive.com/",
                    "User-Agent": headers["User-Agent"],
                    "X-Canary": headers.get("X-Canary", "client=web,app=adrive,version=v6.8.12")
                }
            })
        except Exception as e:
            logger.error(f"[ALIPAN] 获取下载链接异常 ({fid}): {str(e)}")
            return self.error(f"获取下载链接失败: {str(e)}")

    async def download_file(self, fid: str, save_path: str, progress_callback=None, task_id: str = None) -> bool:
        """下载文件 (使用统一并发下载器)"""
        try:
            from ..download_manager import get_downloader
            import uuid
            
            # 1. 获取下载信息
            info = await self.get_file_download_info(fid)
            if info.get("code") != 200:
                logger.error(f"[ALIPAN] 获取下载信息失败: {info.get('message')}")
                return False
            
            data = info["data"]
            url = data["download_url"]
            if not url:
                logger.error(f"[ALIPAN] 未获取到下载链接 (FID: {fid})")
                return False
                
            # 2. 准备 Headers
            headers = data.get("headers", {})
            if not headers:
                headers = {
                    "User-Agent": self.UA_ALIPAN,
                    "Referer": "https://www.alipan.com/",
                    "Connection": "keep-alive"
                }
            
            # 3. 创建并启动任务
            downloader = get_downloader()
            task_id = task_id or f"ali_{uuid.uuid4().hex[:8]}"
            
            await downloader.create_task(
                task_id=task_id,
                url=url,
                output_path=save_path,
                headers=headers,
                concurrency=8  # 统一使用 8 线程
            )
            
            result = await downloader.start(task_id, progress_callback)
            return bool(result)
            
        except Exception as e:
            logger.error(f"[ALIPAN] 下载异常: {str(e)}", exc_info=True)
            return False

    async def search_files(self, keyword: str, page: int = 1, size: int = 50) -> Dict[str, Any]:
        """全盘搜索文件"""
        try:
            # 获取token和headers（与get_files保持一致）
            token = await self._get_access_token()
            if not token:
                return self.error("获取token失败")
            
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {token}"
            
            drive_id = await self._get_drive_id()
            
            # 根据真实curl请求修正
            data = {
                "drive_id_list": [drive_id],  # 使用drive_id_list而非drive_id
                "query": f'name match "{keyword}"',  # 使用双引号
                "limit": size,
                "order_by": "updated_at DESC"
            }
            
            logger.info(f"[ALIPAN] 搜索请求: {data}")
            res = await self._request(f"{self.BASE_URL}/adrive/v3/file/search", "POST", data=data, headers=headers)
            logger.info(f"[ALIPAN] 搜索响应: {res}")
            
            # 检查API错误
            if res.get("code"):
                error_msg = res.get("message", "未知错误")
                if "AccessToken" in error_msg:
                    logger.error(f"[ALIPAN] Token失效，请重新登录")
                    return self.error("登录已过期，请重新登录阿里云盘账户")
                logger.error(f"[ALIPAN] API错误: {error_msg}")
                return self.error(f"搜索失败: {error_msg}")
            
            if not res.get("items"):
                logger.info(f"[ALIPAN] 搜索成功: 关键词'{keyword}', 找到 0 个结果")
                return self.ok("搜索成功", {"list": [], "total": 0})
            
            files = []
            for item in res["items"]:
                files.append({
                    "fid": item.get("file_id"),
                    "name": item.get("name"),
                    "size": item.get("size", 0),
                    "is_dir": item.get("type") == "folder",
                    "updated_at": item.get("updated_at", "")
                })
            
            total = len(files)
            logger.info(f"[ALIPAN] 搜索成功: 关键词'{keyword}', 找到 {total} 个结果")
            return self.ok("搜索成功", {"list": files, "total": total})
        except Exception as e:
            logger.error(f"[ALIPAN] 搜索异常: {str(e)}")
            return self.error(f"搜索文件失败: {str(e)}")

    
    async def transfer(self, share_url: str, code: str = "", expired_type: int = 1, need_share: bool = True) -> Dict[str, Any]:
        """转存分享资源"""
        try:
            # 提取 share_id
            match = re.search(r's/([^/#?]+)', share_url)
            if not match:
                return self.error("资源地址格式有误")
            share_id = match.group(1)
            
            token = await self._get_access_token()
            if not token:
                return self.error("获取token失败")
            
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {token}"
            
            # 1. 获取分享信息
            share_headers = {"Content-Type": "application/json"}
            share_res = await self._request(
                f"{self.BASE_URL}/adrive/v3/share_link/get_share_by_anonymous",
                "POST",
                data={"share_id": share_id},
                headers=share_headers
            )
            
            if not share_res.get("file_infos"):
                return self.error(share_res.get("message", "获取分享信息失败"))
            
            title = share_res.get("share_name", "")
            file_infos = share_res.get("file_infos", [])
            
            # 2. 获取 share_token
            token_res = await self._request(
                f"{self.BASE_URL}/v2/share_link/get_share_token",
                "POST",
                data={"share_id": share_id},
                headers=headers
            )
            share_token = token_res.get("share_token", "")
            
            
            if need_share:
                share_res = await self.create_share([], title, expired_type=expired_type)
                # ... 省略详细实现，类似 xinyue-search 的逻辑 (由于 alipan 目前代码是模拟结构，我们不深究)
            
            logger.info(f"[ALIPAN] 分享转存完成: {title} (URL: {share_url})")
            return self.ok("转存成功", {"title": title, "share_url": share_url})
        except Exception as e:
            logger.error(f"[ALIPAN] 转存失败 ({share_url}): {str(e)}")
            return self.error(f"转存失败: {str(e)}")
    
    async def create_share(self, fid_list: List[str], title: str, expired_type: int = 1) -> Dict[str, Any]:
        """创建分享"""
        try:
            token = await self._get_access_token()
            if not token:
                return self.error("获取token失败")
            
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {token}"
            
            drive_id = await self._get_drive_id()
            if not drive_id:
                return self.error("无法获取 drive_id")
            
            # 计算过期时间
            from datetime import datetime, timedelta
            expiration_str = ""
            input_type = int(expired_type)
            if input_type in (2, 3, 4):
                days_map = {2: 7, 3: 1, 4: 30}
                dt = datetime.utcnow() + timedelta(days=days_map[input_type])
                expiration_str = dt.isoformat()[:19] + ".999Z"  # 阿里云盘通常接受带毫秒的 UTC

            data = {
                "drive_id": drive_id,
                "expiration": expiration_str,
                "share_pwd": "",
                "file_id_list": fid_list
            }
            
            logger.info(f"[ALIPAN] 正在创建分享: title='{title}', 数量={len(fid_list)}, fids={fid_list}, expiration={expiration_str}")
            res = await self._request(f"{self.BASE_URL}/adrive/v2/share_link/create", "POST", data=data, headers=headers)
            
            if not res.get("share_url"):
                err_msg = res.get("message", "创建分享失败")
                logger.error(f"[ALIPAN] 创建分享失败: title='{title}', error={err_msg}")
                return self.error(err_msg)
            
            share_url = res.get("share_url")
            logger.info(f"[ALIPAN] 分享创建成功: title='{title}', share_url={share_url}")
            return self.ok("分享成功", {
                "share_url": share_url,
                "title": res.get("share_title", title),
                "fid": fid_list
            })
        except Exception as e:
            logger.error(f"[ALIPAN] 创建分享异常: title='{title}', fids={fid_list}, error={str(e)}")
            return self.error(f"创建分享失败: {str(e)}")

    async def validate_share(self, share_url: str, password: str = "") -> Dict[str, Any]:
        """
        高精度检测阿里云盘分享链接有效性
        """
        try:
            # 1. 提取 share_id
            match = re.search(r's/([^/#?]+)', share_url)
            if not match:
                return self.error("资源地址格式有误")
            share_id = match.group(1)
            
            # 2. 如果提供了提取码，执行校验逻辑
            if password:
                token_res = await self._request(
                    f"{self.BASE_URL}/v2/share_link/get_share_token",
                    "POST",
                    data={"share_id": share_id, "share_pwd": password},
                    headers={"Content-Type": "application/json"}
                )
                if token_res.get("code") in ["ShareLink.Expired", "ShareLink.Cancelled", "ShareLink.Forbidden"]:
                    return self.error(f"链接已失效: {token_res.get('code')}")
                if "share_token" not in token_res:
                    return self.error("提取码错误或校验失败")

            # 3. 调用匿名获取分享信息接口 (深度探测)
            share_headers = {"Content-Type": "application/json"}
            share_res = await self._request(
                f"{self.BASE_URL}/adrive/v3/share_link/get_share_by_anonymous",
                "POST",
                data={"share_id": share_id},
                headers=share_headers
            )
            
            # 4. 判断状态
            err_code = share_res.get("code")
            if err_code == "ShareLink.Cancelled":
                return self.error("分享链接已取消")
            elif err_code == "ShareLink.Forbidden":
                return self.error("分享链接由于违规已被禁止")
            elif err_code == "ShareLink.Expired":
                return self.error("分享链接已过期")
            elif not share_res.get("file_infos") and err_code:
                return self.error(share_res.get("message", "链接已失效"))
            
            if share_res.get("file_infos"):
                # 统一返回 code: 200
                return {"code": 200, "message": "链接有效", "data": share_res}
            
            return self.error("链接状态异常")
            
        except Exception as e:
            logger.error(f"[ALIPAN] validate_share 异常: {e}")
            return self.error(f"检测异常: {str(e)}")

    async def cancel_share(self, share_id: str) -> Dict[str, Any]:
        """取消阿里云盘分享"""
        try:
            token = await self._get_access_token()
            if not token:
                return self.error("获取token失败")
            
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {token}"
            
            cancel_data = {"share_id": share_id}
            logger.info(f"[ALIPAN] 正在取消分享: share_id='{share_id}'")
            # 用 httpx 从底层调用，因为若没有 body res.get 会抛错
            import httpx
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                res = await client.post(f"{self.BASE_URL}/adrive/v2/share_link/cancel", json=cancel_data, headers=headers)
                if res.status_code in (200, 204):
                    logger.info(f"[ALIPAN] 取消分享成功: share_id='{share_id}'")
                    return self.ok("取消分享成功")
                else:
                    logger.error(f"[ALIPAN] 取消分享失败: share_id='{share_id}', status_code={res.status_code}")
                    return self.error(f"取消分享失败: HTTP {res.status_code}")
        except Exception as e:
            logger.error(f"[ALIPAN] 取消分享异常: share_id='{share_id}', error={str(e)}")
            return self.error(f"取消分享失败: {str(e)}")
    
    async def delete_files(self, fid_list: List[str]) -> Dict[str, Any]:
        """删除文件"""
        try:
            token = await self._get_access_token()
            if not token:
                return self.error("获取token失败")
            
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {token}"
            
            drive_id = await self._get_drive_id()
            if not drive_id:
                return self.error("无法获取 drive_id")
            
            requests = []
            for i, fid in enumerate(fid_list):
                requests.append({
                    "body": {"file_id": fid, "drive_id": drive_id},
                    "headers": {"Content-Type": "application/json"},
                    "id": str(i),
                    "method": "POST",
                    "url": "/recyclebin/trash"
                })
            
            data = {"requests": requests, "resource": "file"}
            logger.info(f"[ALIPAN] 开始批量删除 {len(fid_list)} 个文件/目录, FIDs: {fid_list}")
            res = await self._request(f"{self.BASE_URL}/adrive/v4/batch", "POST", data=data, headers=headers)
            
            logger.info(f"[ALIPAN] 批量删除完成, 数量: {len(fid_list)}, FIDs: {fid_list}")
            return self.ok("删除成功", res)
        except Exception as e:
            logger.error(f"[ALIPAN] 删除失败: FIDs: {fid_list}, error: {str(e)}")
            return self.error(f"删除失败: {str(e)}")

    async def create_folder(self, folder_name: str, pdir_fid: str = "0") -> Dict[str, Any]:
        """创建文件夹"""
        try:
            token = await self._get_access_token()
            if not token:
                return self.error("获取token失败")
            
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {token}"
            
            drive_id = await self._get_drive_id()
            if not drive_id:
                return self.error("无法获取 drive_id")
            
            if str(pdir_fid) == "0":
                pdir_fid = "root"
                
            data = {
                "drive_id": drive_id,
                "parent_file_id": pdir_fid,
                "name": folder_name,
                "type": "folder",
                "check_name_mode": "refuse"
            }
            
            logger.info(f"[ALIPAN] 正在创建文件夹: {folder_name} (Parent: {pdir_fid})")
            res = await self._request(f"{self.BASE_URL}/adrive/v2/file/create", "POST", data=data, headers=headers)
            
            if res.get("file_id"):
                logger.info(f"[ALIPAN] 文件夹创建成功: {folder_name} (FID: {res.get('file_id')})")
                return self.ok("创建成功", {"fid": res.get("file_id")})
            elif res.get("code") == "QuotaExceeded.FileCount":
                 logger.error(f"[ALIPAN] 文件夹创建失败: 数量超出限制")
                 return self.error("文件夹数量超出限制")
            else:
                err_msg = res.get("message", "创建失败")
                logger.error(f"[ALIPAN] 文件夹创建失败 ({folder_name}): {err_msg}")
                return self.error(err_msg)
        except Exception as e:
            logger.error(f"[ALIPAN] 文件夹创建异常 ({folder_name}): {str(e)}")
            return self.error(f"创建文件夹失败: {str(e)}")

    async def get_or_create_path(self, path: str) -> Dict[str, Any]:
        """递归创建路径并返回最终目录的 fid"""
        path = path.strip('/')
        if not path or path == "/" or path == "0":
            return self.ok("根目录", {"fid": "root"})
        
        try:
            parts = path.split('/')
            current_fid = "root"
            
            for part in parts:
                if not part: continue
                # 检查目录是否存在
                list_res = await self.get_files(current_fid)
                if list_res.get("code") != 200:
                    return self.error(f"无法列出目录内容: {list_res.get('message')}")
                
                found_fid = None
                for item in list_res.get("data", []):
                    if item.get("name") == part and item.get("is_dir"):
                        found_fid = item.get("fid")
                        break
                
                if found_fid:
                    current_fid = found_fid
                else:
                    # 创建目录
                    create_res = await self.create_folder(part, current_fid)
                    if create_res.get("code") == 200:
                        current_fid = create_res.get("data", {}).get("fid")
                    else:
                        return self.error(f"创建目录 '{part}' 失败: {create_res.get('message')}")
            
            return self.ok("获取成功", {"fid": current_fid})
        except Exception as e:
            return self.error(f"递归创建路径失败: {str(e)}")

    async def upload_file(self, file_data: Any, file_name: str, pdir_fid: str = "root", progress_callback=None, check_cancel=None) -> Dict[str, Any]:
        """上传文件（支持分块）"""
        try:
            content = file_data.read()
            file_size = len(content)
            
            token = await self._get_access_token()
            headers = self._build_headers()
            headers["Authorization"] = f"Bearer {token}"
            
            drive_id = await self._get_drive_id()
            
            # 1. 预上传（创建文件）
            chunk_size = 10 * 1024 * 1024 # 10MB per chunk
            num_chunks = (file_size + chunk_size - 1) // chunk_size if file_size > 0 else 1
            
            part_info_list = []
            for i in range(num_chunks):
                part_info_list.append({"part_number": i + 1})
            
            if str(pdir_fid) == "0":
                pdir_fid = "root"
                
            pre_data = {
                "drive_id": drive_id,
                "parent_file_id": pdir_fid,
                "name": file_name,
                "type": "file",
                "check_name_mode": "auto_rename",
                "size": file_size,
                "part_info_list": part_info_list
            }
            
            logger.info(f"[ALIPAN] 开始上传文件: {file_name} (Size: {file_size}B, Chunks: {num_chunks})")
            pre_res = await self._request(f"{self.BASE_URL}/adrive/v2/file/create", "POST", data=pre_data, headers=headers)
            
            if not pre_res.get("file_id"):
                err_msg = pre_res.get("message", "预上传失败")
                logger.error(f"[ALIPAN] 预上传失败 ({file_name}): {err_msg}")
                return self.error(err_msg)
            
            file_id = pre_res.get("file_id")
            upload_id = pre_res.get("upload_id")
            part_results = pre_res.get("part_info_list", [])
            
            # 2. 上传分片
            for i, part in enumerate(part_results):
                # [Fix] Check cancellation
                if check_cancel and check_cancel():
                    raise asyncio.CancelledError("Upload cancelled by user")

                upload_url = part.get("upload_url")
                if not upload_url: continue
                
                start = i * chunk_size
                end = min((i + 1) * chunk_size, file_size)
                chunk_data = content[start:end]
                
                # 直接使用 httpx PUT 上传
                async with httpx.AsyncClient(timeout=120.0, verify=False) as client:
                    up_res = await client.put(upload_url, content=chunk_data)
                    if up_res.status_code != 200:
                        logger.error(f"[ALIPAN] [UPLOAD] 分片 {i+1}/{num_chunks} 上传失败: {up_res.status_code} ({file_name})")
                        return self.error(f"分片 {i+1} 上传失败: {up_res.status_code}")
                
                logger.info(f"[ALIPAN] [UPLOAD] 分片 {i+1}/{num_chunks} 上传完成 ({file_name})")
                if progress_callback:
                    try:
                        res = progress_callback(end, file_size)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as progress_err: 
                        logger.error(f"[ALIPAN] Progress callback error: {progress_err}")
            
            # 3. 完成上传
            complete_data = {
                "drive_id": drive_id,
                "file_id": file_id,
                "upload_id": upload_id
            }
            
            comp_res = await self._request(f"{self.BASE_URL}/adrive/v2/file/complete", "POST", data=complete_data, headers=headers)
            
            if comp_res.get("file_id"):
                logger.info(f"[ALIPAN] 文件上传成功: {file_name}")
                return self.ok("上传成功", {"fid": comp_res.get("file_id")})
            else:
                err_msg = comp_res.get("message", "完成上传失败")
                logger.error(f"[ALIPAN] 完成上传失败 ({file_name}): {err_msg}")
                return self.error(err_msg)
                
        except Exception as e:
            return self.error(f"阿里云盘上传失败: {str(e)}")

