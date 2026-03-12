import logging
import json
import sys
import os
from datetime import datetime
import asyncio
from typing import List, Set

# Ensure the log directory exists
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs")
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# 全局日志队列管理器
class LogQueueManager:
    def __init__(self, max_size=1000):
        self.logs = [] # 存储最近的日志
        self.max_size = max_size
        self.subscribers: Set[asyncio.Queue] = set()

    def add_log(self, log_entry):
        self.logs.append(log_entry)
        if len(self.logs) > self.max_size:
            self.logs.pop(0)
        
        # 推送给所有在线订阅者
        for sub in self.subscribers:
            sub.put_nowait(log_entry)

    def subscribe(self) -> asyncio.Queue:
        queue = asyncio.Queue()
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        self.subscribers.discard(queue)

log_manager = LogQueueManager()

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
            "api_request_id": getattr(record, "api_request_id", None),
            "user_id": getattr(record, "user_id", None),
            "task_id": getattr(record, "task_id", None),
        }
        # Add extra fields if they exist
        if hasattr(record, "extra"):
            log_record.update(record.extra)
            
        return json.dumps(log_record, ensure_ascii=False)

class WebSocketLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            # 通过全局管理器分发日志
            log_manager.add_log(msg)
        except Exception:
            self.handleError(record)

def setup_logger(name: str = "app", level: str = "INFO"):
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper()))
    
    # Remove existing handlers
    if logger.handlers:
        logger.handlers.clear()

    # JSON Formatter
    json_formatter = JSONFormatter()

    # Console Handler (Human readable for dev)
    console_handler = logging.StreamHandler(sys.stdout)
    console_formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] [%(module)s:%(funcName)s:%(lineno)d] - %(message)s'
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File Handler (JSON for structured logs)
    file_json_handler = logging.FileHandler(os.path.join(LOG_DIR, "app.json.log"), encoding='utf-8')
    file_json_handler.setFormatter(json_formatter)
    logger.addHandler(file_json_handler)

    # [新增] File Handler (Plain Text for human readable logs)
    file_text_handler = logging.FileHandler(os.path.join(LOG_DIR, "app.log"), encoding='utf-8')
    file_text_handler.setFormatter(console_formatter)
    logger.addHandler(file_text_handler)

    # WebSocket Handler
    ws_handler = WebSocketLogHandler()
    ws_handler.setFormatter(json_formatter)
    logger.addHandler(ws_handler)

    # 如果是主要的 app logger，我们也拦截 root 和 uvicorn 的日志
    if name == "app":
        root_logger = logging.getLogger()
        if not any(isinstance(h, WebSocketLogHandler) for h in root_logger.handlers):
            root_logger.addHandler(ws_handler)
            
        # [新增] 确保 root 也把日志写到文件夹里的 app.log 和 app.json.log
        if not any(isinstance(h, logging.FileHandler) and "app.log" in h.baseFilename for h in root_logger.handlers):
            root_logger.addHandler(file_text_handler)
            root_logger.addHandler(file_json_handler)
            
        # 显式捕获 uvicorn 日志
        for uvicorn_logger_name in ["uvicorn", "uvicorn.access", "uvicorn.error"]:
            ul = logging.getLogger(uvicorn_logger_name)
            if not any(isinstance(h, WebSocketLogHandler) for h in ul.handlers):
                ul.addHandler(ws_handler)
            if not any(isinstance(h, logging.FileHandler) for h in ul.handlers):
                ul.addHandler(file_text_handler)
                ul.addHandler(file_json_handler)

    return logger

# [新增] 重定向 stdout/stderr 的类
class StreamToLogger:
    def __init__(self, logger_func, log_level=logging.INFO):
        self.logger_func = logger_func
        self.log_level = log_level
        self.linebuf = ''

    def write(self, buf):
        for line in buf.rstrip().splitlines():
            self.logger_func(line)

    def flush(self):
        pass

# Initialize default logger
logger = setup_logger()

# 只有在非交互或特定环境下才重定向，为了防止死循环和保持控制台输出
# 注意：这会捕获所有 print() 到日志文件中
sys.stdout_orig = sys.stdout
sys.stderr_orig = sys.stderr

# 激活重定向：捕获所有 print() 的内容也进 app.log
sys.stdout = StreamToLogger(logger.info)
sys.stderr = StreamToLogger(logger.error)

logger.info("Logging system initialized: app.json.log (JSON) and app.log (Text)")
logger.info("Standard Output and Error are now redirected to the logger.")
