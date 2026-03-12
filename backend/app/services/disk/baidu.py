"""
百度网盘服务 - 完整实现
"""
import hashlib
import time
import base64
import re
import json
import asyncio
from typing import Dict, List, Any, Optional, Tuple
from .base import BaseDiskService
import httpx
from ...core.logger import logger


# 百度固定的 Block List
FAKE_BLOCK_LIST_MD5 = ["5910a591dd8fc18c32a8f3df4fdc1761", "a5fc157d78e6ad1c7e114b056c92821e"]
DEFAULT_CHUNK_SIZE = 262144  # 256KB


class BaiduDiskService(BaseDiskService):
    """百度网盘服务"""
    
    PRECREATE_URL = "https://pan.baidu.com/api/precreate"
    RAPID_URL = "https://pan.baidu.com/api/rapidupload"
    
    def __init__(self, credentials: str, config: Dict[str, Any] = None):
        super().__init__(credentials, config)
        self.uk = None
        self.bdstoken = None
        self.chunk_size = config.get("chunk_size", DEFAULT_CHUNK_SIZE) if config else DEFAULT_CHUNK_SIZE
    
    def _build_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
            "Referer": "https://pan.baidu.com/disk/main",
            "Origin": "https://pan.baidu.com",
            "Host": "pan.baidu.com",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "navigate",
            "Cookie": self.credentials
        }
    
    async def _init_user_info(self) -> bool:
        """初始化 UK 和 Token"""
        try:
            # 1. 尝试使用 API 获取
            params = {
                "clienttype": "0",
                "app_id": "250528",
                "web": "1",
                "fields": '["bdstoken","token","uk","isdocuser","servertime"]'
            }
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://pan.baidu.com/api/gettemplatevariable", 
                    params=params, 
                    headers=self.headers
                )
                data = resp.json()
                if data.get("errno") == 0:
                    result = data.get("result", {})
                    self.bdstoken = result.get("bdstoken")
                    self.uk = str(result.get("uk"))
                    return True

            # 2. 备用方案：解析页面
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get('https://pan.baidu.com/disk/main', headers=self.headers)
                text = resp.text
                
                # 提取 uk 和 bdstoken
                uk_match = re.search(r'"uk"\s*:\s*"(\d+)"', text)
                token_match = re.search(r'"bdstoken"\s*:\s*"([a-z0-9]+)"', text)
                
                if uk_match and token_match:
                    self.uk = uk_match.group(1)
                    self.bdstoken = token_match.group(1)
                    return True
                
        except Exception as e:
            logger.error(f"[BAIDU] 初始化失败: {e}")
        return False
    
    async def check_status(self) -> bool:
        """检查登录状态"""
        try:
            return await self._init_user_info()
        except:
            return False
    
    async def _get_path_by_fid(self, fid: str) -> Dict[str, Any]:
        """通过 fid 获取文件路径
        Returns:
            {"path": str}: 成功
            {"error": str}: 失败
        """
        if fid == "0" or fid == "/" or str(fid).startswith('/'):
            return {"path": fid if fid.startswith('/') else "/"}
            
        try:
            # 尝试1: 标准格式 [123]
            params = {
                "fsids": f"[{fid}]",
                "dlink": 0,
                "bdstoken": self.bdstoken
            }
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://pan.baidu.com/api/filemetas",
                    params=params,
                    headers=self.headers
                )
                data = resp.json()
                # 兼容 list 和 info 字段
                file_list = data.get("list") or data.get("info")
                if data.get("errno") == 0 and file_list:
                    return {"path": file_list[0]["path"]}
                else:
                    logger.debug(f"[BAIDU] 获取路径尝试1失败: {data}")
            
            # 尝试2: 字符串格式 ["123"]
            params["fsids"] = f'["{fid}"]'
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://pan.baidu.com/api/filemetas",
                    params=params,
                    headers=self.headers
                )
                data = resp.json()
                file_list = data.get("list") or data.get("info")
                if data.get("errno") == 0 and file_list:
                    return {"path": file_list[0]["path"]}
                else:
                     logger.debug(f"[BAIDU] 获取路径尝试2失败: {data}")

            return {"error": f"无法获取路径, API响应: {data}"}

        except Exception as e:
            msg = f"获取路径异常: {str(e)}"
            logger.error(f"[BAIDU] {msg}", exc_info=True)
            return {"error": msg}

    async def get_files(self, pdir_fid: str = "/") -> Dict[str, Any]:
        """获取文件列表 - 百度网盘需先将 fid 转为 path"""
        try:
            if not self.bdstoken:
                await self._init_user_info()
            
            # 解析路径（支持传入真实完整路径或者数字 fid）
            if pdir_fid.startswith('/'):
                path = pdir_fid
            elif pdir_fid == "0":
                path = "/"
            else:
                path_res = await self._get_path_by_fid(pdir_fid)
                if "error" in path_res:
                    return self.error(f"无法获取文件夹路径: {path_res['error']}")
                path = path_res["path"]

            params = {
                "dir": path,
                "order": "time",
                "desc": 1,
                "showempty": 0,
                "web": 1,
                "page": 1,
                "num": 100,
                "bdstoken": self.bdstoken
            }
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://pan.baidu.com/api/list",
                    params=params,
                    headers=self.headers
                )
                data = resp.json()
                
                if data.get("errno") != 0:
                    return self.error(f"获取文件列表失败: {data.get('errmsg', '未知错误')} (errno={data.get('errno')})")
                
                files = []
                for item in data.get("list", []):
                    files.append({
                        "fid": str(item.get("fs_id")),
                        "name": item.get("server_filename"),
                        "size": item.get("size", 0),
                        "is_dir": item.get("isdir") == 1,
                        "path": item.get("path"),
                        "md5": item.get("md5", ""),
                        "updated_at": item.get("server_mtime")
                    })
                return self.ok("获取成功", files)
        except Exception as e:
            return self.error(f"获取文件列表失败: {str(e)}")
    
    async def transfer(self, share_url: str, code: str = "", expired_type: int = 1, need_share: bool = True) -> Dict[str, Any]:
        """转存分享资源
        
        Args:
            share_url: 分享链接，如 https://pan.baidu.com/s/xxx
            code: 提取码（可选）
            expired_type: 分享有效期类型 (1=永久)
            need_share: 是否创建新分享
        
        Returns:
            {"code": 200, "data": {"title": "标题", "share_url": "新分享链接", "code": "提取码", "fid": [...]}}
        """
        try:
            if not self.bdstoken:
                await self._init_user_info()
            
            # 1. 解析分享链接
            import re
            match = re.search(r'/s/([A-Za-z0-9_-]+)', share_url)
            if not match:
                return self.error("分享链接格式错误")
            
            surl = match.group(1)
            logger.info(f"[BAIDU] 开始转存分享: surl={surl}")
            
            # 2. 如果有提取码，先验证
            randsk = None
            if code:
                randsk = await self._verify_passcode(surl, code)
                if isinstance(randsk, int):  # 错误码
                    return self.error(f"提取码验证失败: {randsk}")
                # 更新cookie中的BDCLND
                if randsk:
                    self.headers["Cookie"] = self._update_bdclnd(self.headers.get("Cookie", ""), randsk)
            
            # 3. 获取转存参数
            transfer_params = await self._get_transfer_params(share_url)
            if not transfer_params:
                return self.error("无法获取分享信息")
            
            share_id, user_id, fs_ids, file_names = transfer_params
            
            # 4. 确定转存目录
            folder_name = self.storage_path
            if not folder_name or folder_name in ("0", "/"):
                folder_name = "/转存文件"
            
            if not folder_name.startswith('/'):
                folder_name = '/' + folder_name
                
            # 确保目录存在
            create_res = await self.get_or_create_path(folder_name.strip('/'))
            if create_res.get("code") != 200:
                return self.error(f"创建目录失败: {create_res.get('message')}")
            
            # 5. 执行转存
            transfer_result = await self._transfer_files(share_id, user_id, fs_ids, folder_name)
            if transfer_result != 0:
                return self.error(f"转存失败: errno={transfer_result}")
            
            logger.info(f"[BAIDU] 文件转存成功")
            
            # 6. 获取转存后的文件列表
            await asyncio.sleep(1)  # 等待转存完成
            dir_list_res = await self.get_files(folder_name)
            if dir_list_res.get("code") != 200:
                return self.error("获取转存文件列表失败")
            
            # 7. 找到刚转存的文件
            target_files = []
            for file in dir_list_res.get("data", []):
                if file.get("name") in file_names:
                    target_files.append(file)
            
            if not target_files:
                return self.error("未找到转存的文件")
            
            fid_list = [str(f["fid"]) for f in target_files]
            title = file_names[0] if file_names else "分享文件"
            
            # 8. 根据 need_share 决定是否创建分享
            if need_share:
                share_res = await self.create_share(fid_list, title, expired_type=expired_type)
                if share_res.get("code") != 200:
                    return self.error(f"创建分享失败: {share_res.get('message')}")
                
                share_data = share_res.get("data", {})
                
                logger.info(f"[BAIDU] 转存并分享成功: {title}")
                return self.ok("转存成功", {
                    "title": title,
                    "share_url": share_data.get("share_url", ""),
                    "code": "",
                    "fid": fid_list
                })
            else:
                logger.info(f"[BAIDU] 转存成功（不分享）: {title}")
                return self.ok("转存成功", {
                    "title": title,
                    "share_url": "",
                    "code": "",
                    "fid": fid_list
                })
            
        except Exception as e:
            logger.error(f"[BAIDU] 转存异常: {str(e)}")
            import traceback
            traceback.print_exc()
            return self.error(f"转存异常: {str(e)}")
    
    async def _verify_passcode(self, surl: str, passcode: str):
        """验证提取码"""
        try:
            url = "https://pan.baidu.com/share/verify"
            params = {
                "surl": surl,
                "bdstoken": self.bdstoken,
                "t": int(time.time() * 1000),
                "channel": "chunlei",
                "web": "1",
                "clienttype": "0"
            }
            data = {
                "pwd": passcode,
                "vcode": "",
                "vcode_str": ""
            }
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, params=params, data=data, headers=self.headers)
                res = resp.json()
                
                if res.get("errno") != 0:
                    return res.get("errno")
                
                return res.get("randsk", "")
        except Exception as e:
            logger.error(f"[BAIDU] 验证提取码异常: {str(e)}")
            return -1
    
    def _update_bdclnd(self, cookie: str, bdclnd: str) -> str:
        """更新Cookie中的BDCLND值"""
        cookie_dict = {}
        for pair in cookie.split(';'):
            pair = pair.strip()
            if '=' in pair:
                key, value = pair.split('=', 1)
                cookie_dict[key] = value
        
        cookie_dict['BDCLND'] = bdclnd
        return '; '.join([f"{k}={v}" for k, v in cookie_dict.items()])
    
    async def _get_transfer_params(self, share_url: str):
        """获取转存所需的参数（通过解析分享页面）"""
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(share_url, headers=self.headers)
                html = resp.text
                
                # 正则提取关键参数
                import re
                shareid_match = re.search(r'"shareid":(\d+?),', html)
                userid_match = re.search(r'"share_uk":"(\d+?)",', html)
                fsid_matches = re.findall(r'"fs_id":(\d+?),', html)
                filename_matches = re.findall(r'"server_filename":"(.+?)",', html)
                
                if not (shareid_match and userid_match and fsid_matches and filename_matches):
                    logger.error("[BAIDU] 无法解析分享页面参数")
                    return None
                
                return (
                    shareid_match.group(1),
                    userid_match.group(1),
                    fsid_matches,
                    list(set(filename_matches))  # 去重
                )
        except Exception as e:
            logger.error(f"[BAIDU] 解析分享页面异常: {str(e)}")
            return None
    
    async def _transfer_files(self, share_id: str, user_id: str, fs_ids: list, folder_name: str):
        """执行文件转存"""
        try:
            url = "https://pan.baidu.com/share/transfer"
            params = {
                "shareid": share_id,
                "from": user_id,
                "bdstoken": self.bdstoken,
                "channel": "chunlei",
                "web": "1",
                "clienttype": "0",
                "ondup": "newcopy"  # 同名文件重命名
            }
            data = {
                "fsidlist": "[" + ",".join(fs_ids) + "]",
                "path": folder_name
            }
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, params=params, data=data, headers=self.headers)
                res = resp.json()
                
                return res.get("errno", -1)
        except Exception as e:
            logger.error(f"[BAIDU] 转存文件异常: {str(e)}")
            return -1

    
    async def search_files(self, keyword: str, page: int = 1, size: int = 50) -> Dict[str, Any]:
        """全盘搜索文件"""
        try:
            if not self.bdstoken:
                await self._init_user_info()
            
            params = {
                "method": "search",
                "key": keyword,
                "page": page,
                "num": size,
                "recursion": 1,  # 全盘搜索
                "web": 1
            }
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://pan.baidu.com/rest/2.0/xpan/file",
                    params=params,
                    headers=self.headers
                )
                data = resp.json()
                
                if data.get("errno") != 0:
                    return self.error(f"搜索失败: {data.get('errmsg', '未知错误')} (errno={data.get('errno')})")
                
                files = []
                for item in data.get("list", []):
                    files.append({
                        "fid": str(item.get("fs_id")),
                        "name": item.get("server_filename"),
                        "size": item.get("size", 0),
                        "is_dir": item.get("isdir") == 1,
                        "path": item.get("path"),
                        "md5": item.get("md5", ""),
                        "updated_at": item.get("server_mtime")
                    })
                
                has_more = data.get("has_more", 0) == 1
                return self.ok("搜索成功", {"list": files, "total": len(files), "has_more": has_more})
        except Exception as e:
            return self.error(f"搜索文件失败: {str(e)}")
    
    async def list_folder_recursive(self, folder_path: str, base_path: str = "") -> Dict[str, Any]:
        """递归获取文件夹内所有文件（扁平化列表）"""
        # 兼容性修复：如果传入的是纯数字ID，尝试解析为路径
        if folder_path and not folder_path.startswith("/") and folder_path.isdigit():
             logger.info(f"[BAIDU] 自动尝试将 FID {folder_path} 解析为路径...")
             path_res = await self._get_path_by_fid(folder_path)
             if "path" in path_res:
                 folder_path = path_res["path"]
                 logger.info(f"[BAIDU] 已成功解析路径: {folder_path}")
             else:
                 logger.warning(f"[BAIDU] 无法解析 FID {folder_path} 为路径: {path_res.get('error')}")

        all_files = []
        
        try:
            if not self.bdstoken:
                await self._init_user_info()
            
            params = {
                "dir": folder_path,
                "order": "time",
                "desc": 1,
                "showempty": 0,
                "web": 1,
                "page": 1,
                "num": 1000,
                "bdstoken": self.bdstoken
            }
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://pan.baidu.com/api/list",
                    params=params,
                    headers=self.headers
                )
                data = resp.json()
                
                if data.get("errno") != 0:
                    logger.debug(f"[BAIDU] 列表获取失败: {data} (URL={resp.url})")
                    return self.error(f"获取文件夹内容失败: errno={data.get('errno')} msg={data.get('errmsg', '未知错误')}")
                items = data.get("list", [])
                
                if not items and base_path:
                    all_files.append({
                        "fid": folder_path,
                        "name": base_path.split('/')[-1] if '/' in base_path else base_path,
                        "size": 0,
                        "is_dir": True,
                        "path": folder_path, # 百度特有字段
                        "relative_path": base_path
                    })
                
                for item in items:
                    item_name = item.get("server_filename")
                    item_path = f"{base_path}/{item_name}" if base_path else item_name
                    
                    if item.get("isdir") == 1:
                        # 递归获取子文件夹
                        sub_result = await self.list_folder_recursive(item.get("path"), item_path)
                        if sub_result.get("code") == 200:
                            all_files.extend(sub_result.get("data", []))
                    else:
                        # 文件：添加相对路径信息
                        all_files.append({
                            "fid": str(item.get("fs_id")),
                            "name": item_name,
                            "size": item.get("size", 0),
                            "is_dir": False,
                            "path": item.get("path"),
                            "md5": item.get("md5", ""),
                            "relative_path": item_path
                        })
                
                return self.ok("获取成功", all_files)
        except Exception as e:
            return self.error(f"递归获取文件夹失败: {str(e)}")
    
    async def create_share(self, fid_list: List[str], title: str, expired_type: int = 1) -> Dict[str, Any]:
        """创建分享"""
        try:
            if not self.bdstoken:
                await self._init_user_info()
            
            params = {
                "bdstoken": self.bdstoken,
                "channel": "chunlei",
                "web": "1",
                "app_id": "250528"
            }
            
            # 设置有效期映射 (1=永久, 2=7天, 3=1天, 4=30天)
            period_map = {1: 0, 2: 7, 3: 1, 4: 30}  # 0=永久, 7=7天, 1=1天, 30=30天
            period = period_map.get(expired_type, 0)  # 默认永久
            
            import random
            import string
            pwd = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
            
            data = {
                "fid_list": json.dumps([int(fid) for fid in fid_list]),
                "schannel": 4,
                "channel_list": "[]",
                "period": period,
                "pwd": pwd
            }
            
            logger.info(f"[BAIDU] 正在创建分享: title='{title}', 数量={len(fid_list)}, fids={fid_list}, period={period}")
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://pan.baidu.com/share/set",
                    params=params,
                    data=data,
                    headers=self.headers
                )
                result = resp.json()
                
                if result.get("errno") != 0:
                    err_hint = result.get('errmsg') or result.get('show_msg') or ""
                    logger.error(f"[BAIDU] 创建分享失败: title='{title}', error={err_hint}, 原始返回={result}")
                    return self.error(f"创建分享失败: {err_hint} (原始返回:{result})")
                
                share_url = f"{result.get('link')}?pwd={pwd}"
                logger.info(f"[BAIDU] 分享创建成功: title='{title}', share_url={share_url}")
                return self.ok("分享成功", {
                    "share_id": result.get("shareid"),
                    "share_url": share_url,
                    "share_pwd": pwd,
                    "title": title,
                    "fid": fid_list
                })
        except Exception as e:
            logger.error(f"[BAIDU] 创建分享异常: title='{title}', fids={fid_list}, error={str(e)}")
            return self.error(f"创建分享失败: {str(e)}")
    
    async def cancel_share(self, share_id: str) -> Dict[str, Any]:
        """取消/删除百度分享"""
        try:
            if not self.bdstoken:
                await self._init_user_info()
            params = {
                "bdstoken": self.bdstoken,
                "channel": "chunlei",
                "web": "1",
                "app_id": "250528"
            }
            data = {"shareid_list": json.dumps([int(share_id)])}
            logger.info(f"[BAIDU] 正在取消分享: share_id='{share_id}', bdstoken={self.bdstoken[:8] if self.bdstoken else 'None'}...")
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://pan.baidu.com/share/cancel",
                    params=params,
                    data=data,
                    headers=self.headers
                )
                result = resp.json()
                logger.debug(f"[BAIDU] 取消分享返回: status={resp.status_code}, body={result}")
                if result.get("errno") == 0:
                    logger.info(f"[BAIDU] 取消分享成功: share_id='{share_id}'")
                    return self.ok("取消分享成功")
                logger.error(f"[BAIDU] 取消分享失败: share_id='{share_id}', error={result.get('errmsg', result)}")
                return self.error(f"取消分享失败: {result.get('errmsg', result)}")
        except Exception as e:
            logger.error(f"[BAIDU] 取消分享异常: share_id='{share_id}', error={str(e)}")
            return self.error(f"取消分享失败: {str(e)}")

    async def verify_pass_code(self, share_url: str, password: str) -> Dict[str, Any]:
        """验证提取码并返回 randsk"""
        try:
            # 提取 surl
            surl = ""
            match = re.search(r's/1?([a-zA-Z0-9_-]+)', share_url)
            if match:
                surl = match.group(1)
            else:
                if 'surl=' in share_url:
                    match = re.search(r'surl=([a-zA-Z0-9_-]+)', share_url)
                    if match:
                        surl = match.group(1)
            
            if not surl:
                return self.error("无法从链接中提取 surl")

            url = "https://pan.baidu.com/share/verify"
            params = {
                'surl': surl,
                't': round(time.time() * 1000),
                'channel': 'chunlei',
                'web': '1',
                'app_id': '250528',
                'clienttype': '0',
            }
            data = {
                'pwd': password,
                'vcode': '',
                'vcode_str': '',
            }
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, params=params, data=data, headers=self._build_headers())
                res = resp.json()
                if res.get("errno") == 0:
                    return self.ok("提取码验证成功", res)
                return self.error(f"提取码验证失败: {res.get('errno')}", res)
        except Exception as e:
            return self.error(f"验证提取码异常: {str(e)}")

    async def validate_share(self, share_url: str, password: str = "") -> Dict[str, Any]:
        """
        高精度检测百度分享链接有效性 (基于 share/list 深度逻辑)
        """
        try:
            # 1. 提取 shorturl
            surl = ""
            match = re.search(r's/1?([a-zA-Z0-9_-]+)', share_url)
            if match:
                surl = match.group(1)
            else:
                surl = share_url.split('s/')[-1].split('?')[0]
                if surl.startswith('1'):
                    surl = surl[1:]
            
            # 2. 准备 Headers 和 Cookie
            current_headers = self._build_headers()
            
            # 3. 如果有提取码，先 verify 以获取 randsk 并注入 Cookie
            if password:
                v_res = await self.verify_pass_code(share_url, password)
                if v_res.get("code") == 200:
                    randsk = v_res.get("data", {}).get("randsk")
                    if randsk:
                        if 'Cookie' not in current_headers:
                            current_headers['Cookie'] = f'BDCLND={randsk}'
                        else:
                            current_headers['Cookie'] += f'; BDCLND={randsk}'
            
            # 4. 调用 share/list API 验证
            url = "https://pan.baidu.com/share/list"
            params = {
                'app_id': 250528,
                'shorturl': surl,
                'root': 1,
                'page': 1,
                'num': 1, 
                'order': 'time',
                'desc': 1,
                'web': 1,
                'channel': 'chunlei',
                'clienttype': 0
            }

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, params=params, headers=current_headers)
                result = resp.json()
                
                errno = result.get("errno", -1)
                if errno == 0:
                    return self.ok("链接有效", result)
                
                # 精准匹配错误码
                error_msg = self.SHARE_ERROR_CODES.get(errno, f"未知错误 ({errno})")
                return self.error(error_msg, result)

        except Exception as e:
            logger.error(f"[BAIDU] validate_share 异常: {e}")
            return self.error(f"检测异常: {str(e)}")

    SHARE_ERROR_CODES = {
        -1: '访问频繁，触发风控（或链接解析失败）',
        -9: '链接不存在或提取码错误',
        105: '链接已失效 (404)',
        115: '该文件禁止分享 / 资源涉及侵权',
        110: '分享次数过多',
        2: '链接已失效',
    }
    
    async def create_folder(self, folder_name: str, pdir_fid: str = "/") -> Dict[str, Any]:
        """创建文件夹
        
        Args:
            folder_name: 文件夹名称
            pdir_fid: 父目录路径（百度使用路径而非fid）
        """
        try:
            if not self.bdstoken:
                await self._init_user_info()
            
            if str(pdir_fid) == "0":
                pdir_fid = "/"
            
            # 如果是数字 FID，先转为路径
            if not str(pdir_fid).startswith('/') and str(pdir_fid) != "/":
                path_res = await self._get_path_by_fid(str(pdir_fid))
                if "path" in path_res:
                    pdir_fid = path_res["path"]
                else:
                    return self.error(f"无法解析父目录路径: {path_res.get('error')}")

            pdir_fid = self._normalize_path(pdir_fid)
            
            # 构造完整路径
            full_path = f"{pdir_fid}{folder_name}".replace('//', '/')
            
            url = "https://pan.baidu.com/api/create"
            params = {
                "a": "commit",
                "bdstoken": self.bdstoken,
                "channel": "chunlei",
                "web": "1",
                "app_id": "250528",
                "clienttype": "0"
            }
            
            data = {
                "path": full_path,
                "isdir": "1",
                "block_list": "[]"
            }
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, params=params, data=data, headers=self.headers)
                res = resp.json()
                
                if res.get("errno") == 0:
                    logger.info(f"[BAIDU] 文件夹创建成功: {folder_name} (Path: {full_path})")
                    return self.ok("创建成功", {"path": full_path, "fid": full_path})
                elif res.get("errno") == -8:  # 文件夹已存在
                    logger.info(f"[BAIDU] 文件夹已存在: {folder_name}")
                    return self.ok("文件夹已存在", {"path": full_path, "fid": full_path})
                else:
                    err_msg = res.get("errmsg", "创建失败")
                    logger.error(f"[BAIDU] 文件夹创建失败 ({folder_name}): {err_msg}")
                    return self.error(err_msg)
        except Exception as e:
            logger.error(f"[BAIDU] 创建文件夹异常: {str(e)}")
            return self.error(f"创建文件夹失败: {str(e)}")
    
    async def get_or_create_path(self, path: str) -> Dict[str, Any]:
        """递归创建路径并返回最终目录的 fid (fs_id)
        
        Args:
            path: 目标路径，如 "/我的文件/subfolder"
        
        Returns:
            {"code": 200, "data": {"path": "/final/path/", "fid": "<fs_id>"}}
        """
        try:
            path = path.strip('/')
            if not path or path == "0":
                return self.ok("根目录", {"path": "/", "fid": ""})
            
            # 分割路径
            parts = path.split('/')
            current_path = "/"
            current_fid = ""
            
            for part in parts:
                if not part:
                    continue
                
                # 先查询当前路径下是否已存在同名目录
                list_res = await self.get_files(current_path.rstrip('/') or "/")
                found_fid = None
                if list_res.get("code") == 200:
                    for item in list_res.get("data", []):
                        if item.get("name") == part and item.get("is_dir"):
                            found_fid = str(item.get("fid", ""))
                            break
                
                if found_fid:
                    # 目录已存在，直接收集 fid
                    current_fid = found_fid
                    current_path = f"{current_path.rstrip('/')}/{part}"
                else:
                    # 目录不存在，创建并获取 fid
                    create_res = await self.create_folder(part, current_path)
                    if create_res.get("code") != 200:
                        return self.error(f"创建目录 {part} 失败: {create_res.get('message')}")
                    current_path = f"{current_path.rstrip('/')}/{part}"
                    # 创建后再查一次以获取 fs_id
                    list_res2 = await self.get_files(current_path.rsplit('/', 1)[0] or "/")
                    if list_res2.get("code") == 200:
                        for item in list_res2.get("data", []):
                            if item.get("name") == part and item.get("is_dir"):
                                current_fid = str(item.get("fid", ""))
                                break
            
            # 确保路径以 / 结尾
            if not current_path.endswith('/'):
                current_path += '/'
            
            logger.info(f"[BAIDU] 路径已就绪: {current_path} fid={current_fid}")
            return self.ok("路径已就绪", {"path": current_path, "fid": current_fid})
        except Exception as e:
            logger.error(f"[BAIDU] 创建路径异常: {str(e)}")
            return self.error(f"创建路径失败: {str(e)}")
    

    async def upload_file(self, file_data: Any, file_name: str, pdir_fid: str = "0", progress_callback=None, check_cancel=None) -> Dict[str, Any]:
        """上传文件 (普通上传)"""
        try:
            if not self.bdstoken:
                await self._init_user_info()
                
            # 1. 准备数据
            content = file_data.read()
            size = len(content)
            
            path_res = await self._get_path_by_fid(pdir_fid)
            if "error" in path_res:
                return self.error(f"无法获取目标路径: {path_res['error']}")
                
            target_path = path_res["path"]
            full_path = f"{target_path}/{file_name}".replace('//', '/')
            
            # 分片大小 4MB（百度推荐）
            CHUNK_SIZE = 4 * 1024 * 1024
            
            # 计算分片 MD5 列表
            block_list = []
            for i in range(0, size, CHUNK_SIZE):
                chunk = content[i:i + CHUNK_SIZE]
                block_list.append(hashlib.md5(chunk).hexdigest())
            
            total_chunks = len(block_list)
            
            # 2. 预创建
            upload_id, pre_err = await self._pre_create(target_path, file_name, size, block_list)
            if not upload_id:
                return self.error(f"预创建失败: {pre_err or '未知错误'}")
            
            # 3. 分片上传
            for i, chunk_md5 in enumerate(block_list):
                chunk_start = i * CHUNK_SIZE
                chunk_end = min((i + 1) * CHUNK_SIZE, size)
                chunk_end = min((i + 1) * CHUNK_SIZE, size)
                chunk_data = content[chunk_start:chunk_end]
                
                # Check cancellation
                if check_cancel and check_cancel():
                    raise asyncio.CancelledError("Upload cancelled")

                if not await self._upload_slice(upload_id, full_path, i, chunk_data):
                    return self.error(f"上传分片 {i+1}/{total_chunks} 失败")
                
                # 调用进度回调
                if progress_callback:
                    try:
                        await progress_callback(chunk_end, size)
                    except Exception as e:
                        logger.warning(f"[BAIDU] 进度回调失败: {e}")
            
            # 4. 创建文件（合并分片）
            success, res_data = await self._create_file(full_path, size, int(time.time()), json.dumps(block_list), upload_id)
            if not success:
                return self.error(f"创建文件失败: {res_data}")
                
            return self.ok("上传成功", {"fid": str(res_data.get("fs_id"))})
        except Exception as e:
            return self.error(f"上传异常: {str(e)}")
    
    async def upload_to_path(self, file_data: Any, file_name: str, target_path: str = "/", progress_callback=None, check_cancel=None) -> Dict[str, Any]:
        """上传文件到指定路径（直接使用路径，自动创建目录，支持大文件分片）
        
        Args:
            file_data: 文件对象
            file_name: 文件名
            target_path: 目标路径
            target_path: 目标路径
            progress_callback: 进度回调函数 (uploaded_chunks, total_chunks) -> None
            check_cancel: 取消检测函数
        """
        try:
            if not self.bdstoken:
                await self._init_user_info()
            
            # 1. 准备数据
            content = file_data.read()
            size = len(content)
            
            # 清理路径和文件名中的特殊字符（emoji 等）
            target_path = self.sanitize_path(target_path.rstrip('/'))
            file_name = self.sanitize_path(file_name)
            
            if not target_path:
                target_path = '/'
            full_path = f"{target_path}/{file_name}".replace('//', '/')
            
            # 分片大小 4MB（百度推荐）
            CHUNK_SIZE = 4 * 1024 * 1024
            
            # 计算分片 MD5 列表
            block_list = []
            for i in range(0, size, CHUNK_SIZE):
                chunk = content[i:i + CHUNK_SIZE]
                block_list.append(hashlib.md5(chunk).hexdigest())
            
            total_chunks = len(block_list)
            logger.debug(f"[BAIDU] [UPLOAD] 文件分析完成: {file_name}, 大小={size}B, 分片数={total_chunks}")
            
            # 2. 预创建（百度会自动创建不存在的父目录）
            logger.info(f"[BAIDU] 开始上传文件: {file_name} (Size: {size}B, Total Chunks: {total_chunks})")
            upload_id, pre_err = await self._pre_create(target_path, file_name, size, block_list)
            if not upload_id:
                logger.error(f"[BAIDU] 预创建失败 ({file_name}): {pre_err or '未知错误'}")
                return self.error(f"预创建失败: {pre_err or '未知错误'}")
            
            # 3. 分片上传
            for i, chunk_md5 in enumerate(block_list):
                chunk_start = i * CHUNK_SIZE
                chunk_end = min((i + 1) * CHUNK_SIZE, size)
                chunk_data = content[chunk_start:chunk_end]
                
                logger.info(f"[BAIDU] [UPLOAD] 分片 {i+1}/{total_chunks} 正在上传... ({file_name})")
                
                # Check cancellation
                if check_cancel and check_cancel():
                    raise asyncio.CancelledError("Upload cancelled")

                if not await self._upload_slice(upload_id, full_path, i, chunk_data):
                    return self.error(f"上传分片 {i+1}/{total_chunks} 失败")
                
                # 调用进度回调
                if progress_callback:
                    try:
                        await progress_callback(chunk_end, size)
                    except Exception as e:
                        logger.warning(f"[BAIDU] 进度回调失败: {e}")
            
            # 4. 创建文件（合并分片）
            if not await self._create_file(full_path, size, int(time.time()), json.dumps(block_list), upload_id):
                logger.error(f"[BAIDU] 创建文件失败 ({file_name})")
                return self.error("创建文件失败")
            
            logger.info(f"[BAIDU] 文件上传成功: {file_name}")
            return self.ok("上传成功", {"path": full_path})
        except Exception as e:
            logger.error(f"[BAIDU] 上传异常 ({file_name}): {str(e)}", exc_info=True)
            return self.error(f"上传异常: {str(e)}")


    async def delete_files(self, fid_list: List[str]) -> Dict[str, Any]:
        """删除文件"""
        try:
            if not self.bdstoken:
                await self._init_user_info()
            
            # 使用 xpan API 替代 Web API，以降低触发验证码的概率
            url = "https://pan.baidu.com/rest/2.0/xpan/file"
            params = {
                'method': 'filemanager',
                'opera': 'delete',
                'bdstoken': self.bdstoken,
                'channel': 'chunlei',
                'web': '1',
                'app_id': '250528',
                'clienttype': '0'
            }
            
            # Baidu 删除需要 path，所以先将 fid 转为 path
            paths = []
            for fid in fid_list:
                path_res = await self._get_path_by_fid(fid)
                if "path" in path_res:
                    paths.append(self._normalize_path(path_res["path"]))
                else:
                    logger.warning(f"[BAIDU] 删除文件时无法解析 FID {fid}: {path_res.get('error')}")

            if not paths:
                return self.error("无法解析任何文件的路径")

            # 确保 path 包含中文字符时不被 escape
            data = {
                "filelist": json.dumps(paths, ensure_ascii=False)
            }
            
            # 必须带上 bdstoken
            if not self.bdstoken:
                await self._init_user_info()
            params["bdstoken"] = self.bdstoken

            logger.info(f"[BAIDU] 开始批量删除 {len(fid_list)} 个文件/目录, FIDs: {fid_list}, Paths: {paths}")
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    params=params,
                    data=data,
                    headers=self.headers
                )
                res = resp.json()
                
                if res.get("errno") == 0 or res.get("errno") == 12: # 12=部分成功/正在处理?
                    logger.info(f"[BAIDU] 批量删除完成, 数量: {len(fid_list)}, FIDs: {fid_list}")
                    return self.ok("删除成功")
                elif res.get("errno") == 132 or res.get("errno") == -6: # 132=需要验证码, -6=身份验证失败
                    # 获取验证联系方式 (SMS/Email)
                    verify_info = res
                    try:
                        auth_widget = res.get("authwidget", {})
                        if auth_widget:
                            # 构造 Appeal Referer
                            saferand = auth_widget.get("saferand", "")
                            safesign = auth_widget.get("safesign", "")
                            safetpl = auth_widget.get("safetpl", "filemanager")
                            
                            appeal_headers = self.headers.copy()
                            appeal_headers["Referer"] = (
                                f"https://pan.baidu.com/disk/appeal?"
                                f"saferand={saferand}&safesign={safesign}&safetpl={safetpl}"
                                f"&feature=postMsg&from=web"
                            )

                            # 额外请求一次获取具体的手机号/邮箱信息
                            widget_url = "https://pan.baidu.com/api/authwidget"
                            widget_params = {
                                "method": "get",
                                "clienttype": "0",
                                "app_id": "250528",
                                "web": "1",
                                "dp-logid": "44069900998884640003" 
                            }
                            widget_data_body = {
                                "safetpl": safetpl,
                                "saferand": saferand,
                                "safesign": safesign
                            }
                            
                            logger.debug(f"[BAIDU] Fetching auth info with Referer: {appeal_headers['Referer']}")
                            
                            # 注意：百度 API 这里虽叫 method=get，但实际需要 POST 提交 safe 参数
                            widget_resp = await client.post(
                                widget_url,
                                params=widget_params,
                                data=widget_data_body,
                                headers=appeal_headers
                            )
                            widget_data = widget_resp.json()
                            logger.debug(f"[BAIDU] Auth widget response: {widget_data}")
                            
                            if widget_data.get("errno") == 0:
                                # 合并 verify_info
                                verify_info["data"] = widget_data.get("data")
                            else:
                                logger.warning(f"[BAIDU] Auth widget error: {widget_data}")
                    except Exception as e:
                        logger.warning(f"[BAIDU] Failed to fetch authwidget info: {e}")

                    # 返回验证所需信息
                    logger.info(f"[BAIDU] 批量删除触发验证码")
                    return {
                        "code": 403, # Use 403 to signal verification needed
                        "message": "需验证码",
                        "data": verify_info # Return updated info with sms/email
                    }
                else:
                    logger.error(f"[BAIDU] 删除失败: {res} Payload: {data}")
                    return self.error(f"删除失败: {res.get('errmsg', '未知错误')} (errno={res.get('errno')})")
        except Exception as e:
            logger.error(f"[BAIDU] 删除异常: {str(e)}")
            return self.error(f"删除异常: {str(e)}")

    async def send_verification_code(self, form_data: Dict[str, Any]) -> Dict[str, Any]:
        """发送验证码"""
        try:
            await self._init_user_info()
            
            url = "https://pan.baidu.com/api/authwidget"
            params = {
                "method": "send",
                "clienttype": "0",
                "app_id": "250528",
                "web": "1",
                "dp-logid": "44069900998884640005" 
            }
            
            # Construct Appeal Referer
            saferand = form_data.get("saferand", "")
            safesign = form_data.get("safesign", "")
            safetpl = form_data.get("safetpl", "filemanager")
            
            appeal_headers = self.headers.copy()
            appeal_headers["Referer"] = (
                f"https://pan.baidu.com/disk/appeal?"
                f"saferand={saferand}&safesign={safesign}&safetpl={safetpl}"
                f"&feature=postMsg&from=web"
            )
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    params=params,
                    data=form_data,
                    headers=appeal_headers
                )
                res = resp.json()
                
                if res.get("errno") == 0:
                    return self.ok("发送成功")
                else:
                    return self.error(f"发送验证码失败: {res.get('show_msg') or res.get('errmsg')}")
        except Exception as e:
             return self.error(f"发送验证码异常: {str(e)}")

    async def check_verification_code(self, form_data: Dict[str, Any]) -> Dict[str, Any]:
        """校验验证码"""
        try:
            url = "https://pan.baidu.com/api/authwidget"
            params = {
                "method": "check",
                "clienttype": "0",
                "app_id": "250528",
                "web": "1"
            }
            
            # Construct Appeal Referer
            saferand = form_data.get("saferand", "")
            safesign = form_data.get("safesign", "")
            safetpl = form_data.get("safetpl", "filemanager")
            
            appeal_headers = self.headers.copy()
            appeal_headers["Referer"] = (
                f"https://pan.baidu.com/disk/appeal?"
                f"saferand={saferand}&safesign={safesign}&safetpl={safetpl}"
                f"&feature=postMsg&from=web"
            )
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    params=params,
                    data=form_data,
                    headers=appeal_headers
                )
                res = resp.json()
                
                if res.get("errno") == 0:
                     # 返回 data (包含 dtoken)
                    return {
                        "code": 200,
                        "message": "验证通过",
                        "data": res.get("data")
                    }
                else:
                    return self.error(f"验证失败: {res.get('show_msg') or str(res.get('errno'))}")
        except Exception as e:
             return self.error(f"校验验证码异常: {str(e)}")

    def _get_bduss(self) -> str:
        """从 Cookie 中提取 BDUSS"""
        import re
        match = re.search(r'BDUSS=([^;]+)', self.credentials)
        return match.group(1) if match else ""
    
    def _generate_devuid(self) -> str:
        """生成设备 UID (来自 BaiduPCS-Go)"""
        bduss = self._get_bduss()
        md5_hash = hashlib.md5(bduss.encode()).hexdigest().upper()
        return f"{md5_hash}|0"
    
    def _generate_locate_sign(self, uid: int, timestamp: int, devuid: str) -> str:
        """生成 locatedownload 签名 (来自 BaiduPCS-Go)
        
        签名算法:
        rand = SHA1(SHA1(bduss) + uid + SALT + time + devuid)
        SALT = "ebrcUYiuxaZv2XGu7KIYKxUrqfnOfpDF"
        """
        bduss = self._get_bduss()
        
        # Step 1: SHA1(bduss).hex()
        bduss_sha1 = hashlib.sha1(bduss.encode()).hexdigest()
        
        # Step 2: 拼接并计算最终签名
        # SALT 来自 BaiduPCS-Go 源码
        salt = "ebrcUYiuxaZv2XGu7KIYKxUrqfnOfpDF"
        
        sign_data = f"{bduss_sha1}{uid}{salt}{timestamp}{devuid}"
        rand = hashlib.sha1(sign_data.encode()).hexdigest()
        
        return rand

    async def get_file_download_info(self, fid: str) -> Dict[str, Any]:
        """获取文件下载信息 (使用 BaiduPCS-Go locatedownload API)"""
        try:
            if not self.bdstoken or not self.uk:
                await self._init_user_info()
            
            # 先获取文件路径和基本信息
            meta_params = {
                "fsids": f"[{fid}]",
                "dlink": 1,
                "bdstoken": self.bdstoken
            }
            
            # 禁用自动解压的 headers
            safe_headers = self.headers.copy()
            safe_headers["Accept-Encoding"] = "identity"
            
            async with httpx.AsyncClient(timeout=30, http2=False) as client:
                resp = await client.get(
                    "https://pan.baidu.com/api/filemetas",
                    params=meta_params,
                    headers=safe_headers
                )
                
                # 检查响应状态
                if resp.status_code != 200:
                    return self.error(f"获取文件元信息失败: HTTP {resp.status_code}")
                
                try:
                    data = resp.json()
                except Exception as e:
                    logger.debug(f"[BAIDU] filemetas response: {resp.text[:200]}")
                    return self.error(f"解析文件元信息失败: {str(e)}")
                
                file_list = data.get("list") or data.get("info")
                
                if data.get("errno") == 0 and file_list:
                    info = file_list[0]
                    file_path = info.get("path")
                    dlink = info.get("dlink")  # 保存 dlink 作为备用
                    
                    # 使用 locatedownload API (来自 BaiduPCS-Go)
                    timestamp = int(time.time())
                    devuid = self._generate_devuid()
                    uid = int(self.uk) if self.uk else 0
                    rand = self._generate_locate_sign(uid, timestamp, devuid)
                    
                    locate_params = {
                        "method": "locatedownload",
                        "app_id": "250528",
                        "path": file_path,
                        "ver": "4.0",
                        "clienttype": "17",  # Android 客户端类型
                        "channel": "0",
                        "apn_id": "1_0",
                        "freeisp": "0",
                        "queryfree": "0",
                        "use": "0",
                        "ant": "1",
                        "check_blue": "1",
                        "es": "1",
                        "esl": "1",
                        "time": str(timestamp),
                        "rand": rand,
                        "devuid": devuid,
                        "cuid": devuid
                    }
                    
                    # 使用 Android 客户端 UA (禁用自动解压)
                    android_headers = {
                        "User-Agent": "netdisk;7.30.0.12;android-android;10.0",
                        "Cookie": self.credentials,
                        "Accept-Encoding": "identity"
                    }
                    
                    # 尝试解析 locatedownload 响应
                    download_url = None
                    try:
                        locate_resp = await client.post(
                            "https://d.pcs.baidu.com/rest/2.0/pcs/file",
                            params=locate_params,
                            headers=android_headers
                        )
                        if locate_resp.status_code == 200:
                            locate_data = locate_resp.json()
                            if "urls" in locate_data and locate_data["urls"]:
                                for url_info in locate_data["urls"]:
                                    if url_info.get("encrypt", 1) == 0 or "url" in url_info:
                                        download_url = url_info.get("url")
                                        break
                                if not download_url:
                                    download_url = locate_data["urls"][0].get("url")
                    except Exception as e:
                        logger.debug(f"[BAIDU] locatedownload parse error: {e}, body: {locate_resp.text[:200] if 'locate_resp' in locals() else 'N/A'}")
                    
                    # 如果 locatedownload 失败，回退到 dlink
                    if not download_url:
                        logger.debug(f"[BAIDU] locatedownload failed, using dlink")
                        download_url = dlink
                    
                    if not download_url:
                        return self.error("无法获取下载链接 (locatedownload failed and no dlink)")
                    
                    return self.ok("获取成功", {
                        "fid": str(info.get("fs_id")),
                        "file_name": info.get("server_filename"),
                        "name": info.get("server_filename"), # 对齐阿里等插件字段
                        "size": info.get("size"),
                        "md5": info.get("md5"),
                        "download_url": download_url,
                        "path": file_path
                    })
                
                return self.error(f"获取文件信息失败: {data}")
        except Exception as e:
            logger.error(f"[BAIDU] get_file_download_info error: {repr(e)}", exc_info=True)
            return self.error(f"获取下载信息异常: {type(e).__name__} {str(e)}")
    

    
    async def download_file(self, fid: str, save_path: str, progress_callback=None, task_id: str = None) -> bool:
        """百度下载必须使用与获取链接时一致的 Android UA"""
        try:
            from ..download_manager import get_downloader
            import uuid
            import httpx
            
            # 1. 获取下载信息
            info_res = await self.get_file_download_info(fid)
            if info_res.get("code") != 200:
                logger.error(f"[BAIDU] 获取下载信息失败: {info_res.get('message')}")
                return False
                
            data = info_res["data"]
            url = data["download_url"]
            file_size = int(data.get("size", 0))
            
            # 2. 准备参数
            cookie_str = self.credentials or ""
            ua = "netdisk;7.30.0.12;android-android;10.0" # 回归最稳 UA
            
            # 3. 定义自定义 Fetcher
            # 我们在外部创建一个 Client 并复用，避免每个分片都进行握手
            client = httpx.AsyncClient(verify=False, follow_redirects=True, timeout=60)
            
            async def baidu_chunk_fetcher(start, end, idx, **kwargs):
                headers = {
                    "User-Agent": ua,
                    "Cookie": cookie_str,
                    "Range": f"bytes={start}-{end}",
                    "Referer": "https://pan.baidu.com/",
                    "Accept-Encoding": "identity",
                    "Connection": "keep-alive"
                }
                
                try:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code in [200, 206]:
                        return resp.content
                    else:
                        logger.error(f"[BAIDU] Fetcher Error {resp.status_code} for chunk {idx}")
                        return None
                except Exception as e:
                    logger.error(f"[BAIDU] Fetcher Exception for chunk {idx}: {str(e)}")
                    return None

            # 4. 创建任务
            downloader = get_downloader()
            dl_task_id = task_id or f"baidu_{uuid.uuid4().hex[:8]}"
            
            try:
                await downloader.create_task(
                    task_id=dl_task_id,
                    url=url,
                    output_path=save_path,
                    headers={"User-Agent": ua, "Cookie": cookie_str},
                    concurrency=8,
                    custom_chunk_fetcher=baidu_chunk_fetcher,
                    file_size=file_size
                )
                
                result = await downloader.start(dl_task_id, progress_callback)
                return bool(result)
            finally:
                await client.aclose() # 确保关闭客户端

            
        except Exception as e:
            logger.error(f"[BAIDU] 下载异常: {str(e)}", exc_info=True)
            return False

    # ============ 秒传相关方法 ============
    
    @staticmethod
    def _enc_md5_simulator(md5: str) -> str:
        """
        百度特有的 MD5 变换算法
        """
        # 验证 MD5 格式（32位十六进制字符串）
        if not md5 or len(md5) != 32:
            logger.warning(f"[BAIDU] _enc_md5_simulator: 无效的 MD5 长度: {len(md5) if md5 else 0}")
            return md5 or ""
        
        # 检查是否全是十六进制字符
        try:
            int(md5, 16)
        except ValueError:
            logger.warning(f"[BAIDU] _enc_md5_simulator: MD5 包含非十六进制字符: {md5}")
            return md5
        
        temp = md5[8:16] + md5[0:8] + md5[24:32] + md5[16:24]
        res = []
        for i, c in enumerate(temp):
            digit = int(c, 16)
            mask = 15 & i
            res.append(format(digit ^ mask, 'x'))
        result_str = ''.join(res)
        if len(result_str) > 9:
            digit9 = int(result_str[9], 16)
            special_char = chr(digit9 + ord('g'))
            result_str = result_str[:9] + special_char + result_str[10:]
        return result_str
    
    @staticmethod
    def calculate_offset(uk: str, md5: str, ts: int, size: int, chunk_size: int) -> int:
        """计算验证分片的偏移量"""
        enc_md5 = BaiduDiskService._enc_md5_simulator(md5)
        hex_str = hashlib.md5(f"{uk}{enc_md5}{ts}".encode()).hexdigest()[:8]
        max_offset = size - chunk_size
        if max_offset < 0:
            return 0
        return int(hex_str, 16) % (max_offset + 1)
    
    def sanitize_path(self, path: str) -> str:
        """移除百度网盘不支持的特殊字符（如 emoji）"""
        import re
        # 移除 emoji 和其他特殊 Unicode 字符
        # 保留中文、英文、数字、常见符号
        # 移除 emoji 范围的字符
        emoji_pattern = re.compile(
            "["
            "\u2000-\u2BFF"  # 常用符号、箭头、几何图形 (包含 ⭕)
            "\u2600-\u27BF"  # 杂项符号、丁柏特字体
            "\uFE00-\uFE0F"  # 变体选择符 (Variation Selectors)
            "\U00010000-\U0010FFFF"  # 所有扩展平面的字符 (绝大多数 Emoji)
            "]+",
            flags=re.UNICODE
        )
        return emoji_pattern.sub('', path)
    
    def _normalize_path(self, path: str) -> str:
        """确保路径格式正确"""
        path = self.sanitize_path(path)  # 先清理特殊字符
        path = path.replace('\\', '/').replace('//', '/')
        if not path.startswith('/'):
            path = '/' + path
        if not path.endswith('/'):
            path = path + '/'
        return path
    
    async def _pre_create(self, path: str, filename: str, size: int, block_list: List[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """预创建文件，返回 (upload_id, error_msg)"""
        if not self.bdstoken:
            await self._init_user_info()
        
        full_path = f"{self._normalize_path(path)}{filename}".replace('//', '/')
        
        params = {
            'bdstoken': self.bdstoken,
            'app_id': '250528',
            'channel': 'chunlei',
            'web': '1',
            'clienttype': '0'
        }
        
        data = {
            'path': full_path,
            'autoinit': '1',
            'block_list': json.dumps(block_list if block_list else FAKE_BLOCK_LIST_MD5),
            'target_path': str('/'.join(full_path.split('/')[:-1])) + '/'
        }
        
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    self.PRECREATE_URL,
                    params=params,
                    data=data,
                    headers=self.headers
                )
                js = resp.json()
                if js.get('errno') == 0:
                    return js.get('uploadid'), None
                
                err_msg = f"errno: {js.get('errno')}, info: {js.get('info', '未知错误')}"
                if js.get('errno') == 10:
                    err_msg = "空间不足 (10)"
                elif js.get('errno') == -10:
                    err_msg = "Token失效 (-10)"
                
                return None, err_msg
        except Exception as e:
            return None, str(e)
        
    async def _upload_slice(self, uploadid: str, path: str, partseq: int, file_data: bytes) -> bool:
        """上传分片"""
        # 使用 pcs.baidu.com 而非 d.pcs.baidu.com，避免 302 跳转
        url = "https://pcs.baidu.com/rest/2.0/pcs/superfile2"
        params = {
            "method": "upload",
            "app_id": "250528",
            "path": path,
            "uploadid": uploadid,
            "partseq": str(partseq),
            "bdstoken": self.bdstoken 
        }
        
        # 上传重试循环
        upload_retries = 0
        max_upload_retries = 3
        
        while upload_retries < max_upload_retries:
            try:
                # 增加超时时间
                headers = self.headers.copy()
                headers['Cookie'] = self.credentials
                headers['Accept-Encoding'] = 'identity'
                # 关键修复：Host 必须与请求 URL 一致，否则 403/404
                headers['Host'] = 'pcs.baidu.com'
                headers['Origin'] = 'https://pcs.baidu.com'
                
                async with httpx.AsyncClient(timeout=300) as client:
                    files = {'file': ('blob', file_data)} # 显式指定文件名
                    resp = await client.post(url, params=params, files=files, headers=headers)
                    
                    response_text = resp.text
                    try:
                        js = resp.json()
                    except:
                        logger.error(f"[BAIDU] 上传分片失败 (非JSON响应): Status={resp.status_code}, Body={response_text[:500]}...")
                        # 非200也算失败，需要重试
                        if upload_retries < max_upload_retries - 1:
                            upload_retries += 1
                            continue
                        return False
                    
                    if js.get('md5') is not None:
                        return True
                        
                    logger.error(f"[BAIDU] 上传分片失败 API响应: {js} Status: {resp.status_code}")
                    if 'errno' in js:
                         # 典型错误码处理：如果是网络相关或临时错误则重试
                         errno = js['errno']
                         logger.debug(f"[BAIDU] 百度API错误码: {errno}")
                    
                    # 失败重试
                    upload_retries += 1
                    if upload_retries < max_upload_retries:
                         import asyncio
                         await asyncio.sleep(1)
                         continue
                         
                    return False
            except Exception as e:
                upload_retries += 1
                logger.warning(f"[BAIDU] 上传分片异常: {e} (重试 {upload_retries}/{max_upload_retries})")
                if upload_retries >= max_upload_retries:
                    logger.error(f"[BAIDU] 上传分片最终失败", exc_info=True)
                    return False
                import asyncio
                await asyncio.sleep(1)

    async def _create_file(self, path: str, size: int, ctime: int, block_list: str, uploadid: str) -> bool:
        """创建文件 (合并分片)"""
        url = "https://pan.baidu.com/api/create"
        params = {
            'a': 'commit',
            'bdstoken': self.bdstoken,
            'app_id': '250528',
            'channel': 'chunlei',
            'web': '1',
            'clienttype': '0'
        }
        
        data = {
            'path': path,
            'isdir': '0',
            'size': str(size),
            'block_list': block_list,
            'uploadid': uploadid,
            'rtype': '1', # 1=overwrite? 0=fail?
            'local_mtime': str(ctime)
        }
        
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    params=params,
                    data=data,
                    headers=self.headers
                )
                js = resp.json()
                if js.get('errno') != 0:
                    logger.error(f"[BAIDU] 百度创建文件失败: errno={js.get('errno')}, errmsg={js.get('errmsg')}")
                    return False, js.get('errmsg', '未知错误')
                return True, js
        except Exception as e:
            logger.error(f"[BAIDU] 创建文件失败: {e}", exc_info=True)
            return False, str(e)
    async def download_slice(self, url: str, offset: int, length: int) -> Optional[bytes]:
        """百度专属分片下载 - locatedownload模式"""
        try:
            # 使用 Android 客户端 UA (与 locatedownload API 返回的链接匹配)
            headers = {
                "User-Agent": "netdisk;7.30.0.12;android-android;10.0",
                "Cookie": self.credentials,
                "Range": f"bytes={offset}-{offset + length - 1}",
                "Accept-Encoding": "identity"  # 禁用压缩
            }
            
            # 手动处理跳转 + stream + aiter_raw = 绝对的原始数据
            import traceback
            async with httpx.AsyncClient(timeout=60, follow_redirects=False, verify=False) as client:
                current_url = url
                redirect_count = 0
                max_redirects = 5
                
                while redirect_count < max_redirects:
                    # 连接重试循环
                    connect_retries = 0
                    max_connect_retries = 3
                    
                    while connect_retries < max_connect_retries:
                        try:
                            # 使用 stream 模式
                            async with client.stream("GET", current_url, headers=headers) as resp:
                                # 检查跳转
                                if resp.status_code in [301, 302, 303, 307, 308]:
                                    location = resp.headers.get("Location")
                                    if location:
                                        current_url = location
                                        redirect_count += 1
                                        break # 跳出连接重试，进入下一次跳转循环
                                
                                if resp.status_code in [200, 206]:
                                    logger.debug(f"[BAIDU] download_slice final headers: {resp.headers}")
                                    content = b""
                                    async for chunk in resp.aiter_raw():
                                        content += chunk
                                    return content
                                else:
                                    err_text = await resp.aread()
                                    raise Exception(f"PCS下载拒绝: {resp.status_code} | {err_text.decode('utf-8')[:100]}")
                                    
                        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadTimeout) as e:
                            connect_retries += 1
                            if connect_retries >= max_connect_retries:
                                raise e
                            logger.warning(f"[BAIDU] download_slice connection error: {e}, retrying ({connect_retries}/{max_connect_retries})...")
                            import asyncio
                            await asyncio.sleep(1)
                            continue
                        
                        # 如果没有跳转也没返回（应当是不可能的，因为上面有return），或者break出来了
                        if redirect_count >= max_redirects:
                             break
                    else:
                        # 连接重试耗尽
                        if redirect_count >= max_redirects:
                            break
                        # 否则是正常流程中的break（跳转），继续外层循环
                        continue
                            
            raise Exception("重定向次数过多")

        except Exception as e:
             traceback.print_exc()
             raise Exception(f"下载异常: {str(e)}")
    
    async def download_concurrent(
        self, 
        download_url: str, 
        output_dir: str, 
        file_name: str,
        task_id: str = None,
        progress_callback = None
    ) -> str:
        """使用协程并发加速下载（替代 aria2）
        
        Args:
            download_url: 下载链接
            output_dir: 输出目录
            file_name: 文件名
            task_id: 任务ID（用于断点续传，可选）
            progress_callback: 进度回调 async def callback(completed, total, speed)
            
        Returns:
            下载文件的本地路径
        """
        from ..download_manager import get_downloader
        import os
        import uuid
        
        downloader = get_downloader()
        
        # 生成任务ID
        if not task_id:
            if not task_id:
                task_id = f"bd_{uuid.uuid4().hex[:8]}"
        
        # 确保输出目录存在
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, file_name)
        
        # 使用 Android 客户端 headers
        headers = {
            "User-Agent": "netdisk;7.30.0.12;android-android;10.0",
            "Cookie": self.credentials,
            "Accept-Encoding": "identity",
            "Referer": "https://pan.baidu.com/disk/home"
        }
        
        # 创建下载任务
        await downloader.create_task(
            task_id=task_id,
            url=download_url,
            output_path=output_path,
            headers=headers,
            concurrency=8  # 统一调整为 8 (降低被封禁风险)
        )
        
        # 开始下载
        result_path = await downloader.start(task_id, progress_callback)
        
        if result_path:
            return result_path
        else:
            task = downloader.get_task(task_id)
            if task and task.error_message:
                raise Exception(f"下载失败: {task.error_message}")
            raise Exception("下载被取消或暂停")
            
    async def rapid_upload(
        self, 
        file_info: Dict, 
        slice_md5: str, 
        chunk_data: bytes, 
        offset: int
    ) -> Dict[str, Any]:
        """
        执行秒传
        
        Args:
            file_info: 文件信息 {file_name, size, md5, path}
            slice_md5: 首分片 MD5 (前 256KB)
            chunk_data: 验证分片数据
            offset: 验证分片偏移量
        """
        try:
            if not self.bdstoken or not self.uk:
                success = await self._init_user_info()
                if not success:
                    return self.error("百度网盘初始化失败")
            
            file_name = file_info['file_name']
            size = file_info['size']
            md5 = file_info['md5']
            target_path = file_info.get('path', '/')
            
            # 文件太小不支持秒传
            if size < self.chunk_size:
                return self.error(f"文件大小({size}B)小于分片大小({self.chunk_size}B)，不支持秒传")
            
            # 1. 预创建
            upload_id, pre_err = await self._pre_create(target_path, file_name, size)
            if not upload_id:
                return self.error(f"预创建失败: {pre_err or '未知错误'}")
            
            # 2. 构建秒传请求
            full_path = f"{self._normalize_path(target_path)}{file_name}".replace('//', '/')
            ts = int(time.time())
            
            params = {
                'rtype': '1',
                'bdstoken': self.bdstoken,
                'web': '1',
                'clienttype': '0'
            }
            
            enc_content = self._enc_md5_simulator(md5)
            enc_slice = self._enc_md5_simulator(slice_md5)
            chunk_b64 = base64.b64encode(chunk_data).decode('utf-8')
            
            data = {
                'uploadid': upload_id,
                'path': full_path,
                'content-length': str(size),
                'content-md5': enc_content,
                'slice-md5': enc_slice,
                'target_path': str('/'.join(full_path.split('/')[:-1])) + '/',
                'local_mtime': str(ts),
                'data_time': str(ts),
                'data_offset': str(offset),
                'data_content': chunk_b64
            }
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    self.RAPID_URL,
                    params=params,
                    data=data,
                    headers=self.headers
                )
                js = resp.json()
                errno = js.get('errno')
                
                if errno == 0:
                    return self.ok("秒传成功", {
                        "path": full_path,
                        "fs_id": js.get("info", {}).get("fs_id")
                    })
                
                return self.error(f"秒传失败: errno={errno}")
                
        except Exception as e:
            return self.error(f"秒传异常: {str(e)}")

    async def download_slice(self, url: str, offset: int, length: int) -> Optional[bytes]:
        """下载文件分片 (使用 Android Headers)"""
        headers = {
            "User-Agent": "netdisk;7.30.0.12;android-android;10.0",
            "Cookie": self.credentials,
            "Accept-Encoding": "identity",
            "Range": f"bytes={offset}-{offset + length - 1}"
        }
        
        try:
             async with httpx.AsyncClient(verify=False, timeout=30.0, http2=False) as client:
                resp = await client.get(url, headers=headers, follow_redirects=True)
                if resp.status_code in [200, 206]:
                    return resp.content
        except Exception as e:
            pass
            
        return None
    async def transfer(self, share_url: str, code: str = "") -> Dict[str, Any]:
        """转存分享资源（暂未实现）
        
        百度网盘的分享转存需要登录态和复杂的验证流程，
        暂时不支持该功能。建议使用其他方式转存。
        """
        return self.error("百度网盘暂不支持分享转存功能")
