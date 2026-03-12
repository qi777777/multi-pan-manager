import asyncio
import json
import time
from typing import Set, Dict, Any
from ..core.logger import logger


class TaskBroadcaster:
    """任务变更广播器 - 维护 SSE 连接池并分发消息"""

    def __init__(self):
        # 维护所有活跃的连接队列
        self.queues: Set[asyncio.Queue] = set()
        # 记录每个任务上一次广播的时间，用于节流
        self.last_broadcast: Dict[str, float] = {}

    async def subscribe(self):
        """订阅广播，返回一个新的队列"""
        queue = asyncio.Queue()
        self.queues.add(queue)
        logger.debug(f"[BROADCAST] + 新订阅者加入，当前总计: {len(self.queues)}")
        try:
            yield queue
        finally:
            self.queues.remove(queue)
            logger.debug(f"[BROADCAST] - 订阅者离开，剩余总计: {len(self.queues)}")

    def broadcast(self, message: Dict[str, Any]):
        """向所有订阅者广播消息，带有节流逻辑"""
        msg_type = message.get("type")
        task_id = message.get("task_id")
        
        # 节流逻辑：针对进度更新类型的消息，500ms 内不重复发送
        if msg_type in ["task_updated", "disk_upload_progress"] and task_id:
            task_key = f"{task_id}_{message.get('status', 'progress')}_{message.get('account_id', '')}"
            now = time.time()
            
            # 【优化】如果包含 status 且不是运行中状态，或者是错误消息，则不节流，确保终态立即送达
            is_running = message.get("status") == 1 # STATUS_RUNNING
            has_error = "error_message" in message or "失败" in str(message.get("current_step", ""))
            
            # 涉及计数变更（completed_files/targets）的消息不节流，防止计数器卡顿
            has_counters = "completed_files" in message or "completed_targets" in message
            
            # 状态转换消息（如 Pending -> Running）不节流
            is_state_change = "status" in message
            
            if not has_error and not has_counters and not is_state_change and (is_running or ("status" not in message)):
                last_time = self.last_broadcast.get(task_key, 0)
                # 纯进度更新节流间隔保持在 0.3s
                if now - last_time < 0.3:
                    return
                self.last_broadcast[task_key] = now
            else:
                # 状态变更、计数器更新或错误，允许发送
                self.last_broadcast[task_key] = now

        msg_json = json.dumps(message)
        sub_count = len(self.queues)
        if sub_count > 0:
            # 仅记录关键消息或错误
            if message.get("error_message") or msg_type != "task_updated":
                logger.debug(f"[BROADCAST] 发送消息给 {sub_count} 个订阅者: {msg_type}")
        
        for queue in list(self.queues):
            try:
                queue.put_nowait(msg_json)
            except Exception as e:
                logger.warning(f"[BROADCAST] 队列推送失败 (可能已断开): {e}")
                pass


# 全局单例
task_broadcaster = TaskBroadcaster()
