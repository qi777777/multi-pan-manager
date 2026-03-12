from slowapi import Limiter
from slowapi.util import get_remote_address

# 全局限流器实例，用于解决循环导入问题
limiter = Limiter(key_func=get_remote_address)
