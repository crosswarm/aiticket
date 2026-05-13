"""
PM 任务调度器
提供定时任务调度功能
"""

import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable

from services.pm_collaboration_service import get_pm_service
from services.pm_notifier import get_pm_notifier


class PMScheduler:
    """PM任务调度器"""

    def __init__(self):
        self.pm_service = get_pm_service()
        self.notifier = get_pm_notifier()

        # 任务线程
        self._sync_thread: Optional[threading.Thread] = None
        self._process_thread: Optional[threading.Thread] = None
        self._overdue_thread: Optional[threading.Thread] = None

        # 运行标志
        self._running = False
        self._stop_event = threading.Event()

        # 配置
        self.sync_interval = 5  # 同步间隔（分钟）
        self.process_interval = 10  # 处理间隔（分钟）
        self.overdue_interval = 60  # 超时检查间隔（分钟）

        # 统计
        self.stats: Dict[str, Any] = {
            "last_sync_at": None,
            "last_process_at": None,
            "last_overdue_check_at": None,
            "total_processed": 0,
            "today_processed": 0,
            "last_reset_date": datetime.now().date(),
        }

        # 锁
        self._lock = threading.Lock()

    def start(
        self,
        sync_interval: int = 5,
        process_interval: int = 10,
        overdue_interval: int = 60,
    ):
        """
        启动调度器

        Args:
            sync_interval: 同步间隔（分钟）
            process_interval: 处理间隔（分钟）
            overdue_interval: 超时检查间隔（分钟）
        """
        if self._running:
            print("[PMScheduler] 调度器已在运行")
            return

        self.sync_interval = sync_interval
        self.process_interval = process_interval
        self.overdue_interval = overdue_interval

        self._running = True
        self._stop_event.clear()

        # 启动同步任务
        self._sync_thread = threading.Thread(
            target=self._sync_task,
            name="PM-Sync",
            daemon=True,
        )
        self._sync_thread.start()

        # 启动处理任务
        self._process_thread = threading.Thread(
            target=self._process_task,
            name="PM-Process",
            daemon=True,
        )
        self._process_thread.start()

        # 启动超时检查任务
        self._overdue_thread = threading.Thread(
            target=self._overdue_task,
            name="PM-Overdue",
            daemon=True,
        )
        self._overdue_thread.start()

        print(f"[PMScheduler] 调度器已启动")
        print(f"  - 同步间隔: {sync_interval}分钟")
        print(f"  - 处理间隔: {process_interval}分钟")
        print(f"  - 超时检查间隔: {overdue_interval}分钟")

    def stop(self):
        """停止调度器"""
        if not self._running:
            return

        print("[PMScheduler] 正在停止调度器...")
        self._running = False
        self._stop_event.set()

        # 等待线程结束
        if self._sync_thread:
            self._sync_thread.join(timeout=5)
        if self._process_thread:
            self._process_thread.join(timeout=5)
        if self._overdue_thread:
            self._overdue_thread.join(timeout=5)

        print("[PMScheduler] 调度器已停止")

    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running

    def get_status(self) -> Dict[str, Any]:
        """获取调度器状态"""
        with self._lock:
            # 检查是否需要重置今日统计
            today = datetime.now().date()
            if self.stats["last_reset_date"] != today:
                self.stats["today_processed"] = 0
                self.stats["last_reset_date"] = today

            return {
                "running": self._running,
                "sync_interval": self.sync_interval,
                "process_interval": self.process_interval,
                "overdue_interval": self.overdue_interval,
                "last_sync_at": self.stats["last_sync_at"],
                "last_process_at": self.stats["last_process_at"],
                "last_overdue_check_at": self.stats["last_overdue_check_at"],
                "total_processed": self.stats["total_processed"],
                "today_processed": self.stats["today_processed"],
            }

    def _sync_task(self):
        """同步任务"""
        print("[PMScheduler] 同步任务已启动")

        while not self._stop_event.is_set():
            try:
                print(f"[PMScheduler] 开始同步 PM 需求数据...")

                # 获取待分析的需求
                demands = self.pm_service.get_pending_demands()

                # 获取其他状态的需求（用于完整缓存）
                all_demands = []
                for status in ["WAIT_ANALYSIS", "COO_ACCEPT", "COO_HANG"]:
                    status_demands = self.pm_service.fetch_demands(status_list=[status])
                    all_demands.extend(status_demands)

                # 同步到缓存
                result = self.pm_service.sync_to_cache(all_demands)

                with self._lock:
                    self.stats["last_sync_at"] = datetime.now()

                print(
                    f"[PMScheduler] 同步完成: 总数 {result['total']}, "
                    f"新增 {result['new']}, 更新 {result['updated']}"
                )

            except Exception as e:
                print(f"[PMScheduler] 同步任务异常: {e}")

            # 等待下一次同步
            if self._stop_event.wait(self.sync_interval * 60):
                break

        print("[PMScheduler] 同步任务已停止")

    def _process_task(self):
        """自动处理任务"""
        print("[PMScheduler] 处理任务已启动")

        # 初始延迟，让同步任务先执行
        time.sleep(10)

        while not self._stop_event.is_set():
            try:
                print(f"[PMScheduler] 开始自动处理待分析需求...")

                # 获取待处理的需求
                results = self.pm_service.auto_processor.process_all_pending()

                with self._lock:
                    self.stats["last_process_at"] = datetime.now()
                    self.stats["total_processed"] += len(results)
                    self.stats["today_processed"] += len(results)

                # 发送通知
                if results:
                    self.notifier.notify_auto_process_result(results)
                    print(
                        f"[PMScheduler] 处理完成: 共 {len(results)} 个需求, "
                        f"采纳 {len([r for r in results if r.action == 'accept'])}, "
                        f"拒绝 {len([r for r in results if r.action == 'reject'])}"
                    )
                else:
                    print("[PMScheduler] 本次无需要处理的需求")

            except Exception as e:
                print(f"[PMScheduler] 处理任务异常: {e}")

            # 等待下一次处理
            if self._stop_event.wait(self.process_interval * 60):
                break

        print("[PMScheduler] 处理任务已停止")

    def _overdue_task(self):
        """超时检查任务"""
        print("[PMScheduler] 超时检查任务已启动")

        # 初始延迟，让其他任务先执行
        time.sleep(30)

        while not self._stop_event.is_set():
            try:
                print(f"[PMScheduler] 开始检查超时需求...")

                # 检查超时需求
                overdue = self.pm_service.overdue_monitor.check_overdue_demands()

                with self._lock:
                    self.stats["last_overdue_check_at"] = datetime.now()

                # 发送通知
                if overdue:
                    self.notifier.notify_overdue_demands(overdue)
                    print(f"[PMScheduler] 发现 {len(overdue)} 个超时需求，已发送通知")
                else:
                    print("[PMScheduler] 暂无超时需求")

            except Exception as e:
                print(f"[PMScheduler] 超时检查任务异常: {e}")

            # 等待下一次检查
            if self._stop_event.wait(self.overdue_interval * 60):
                break

        print("[PMScheduler] 超时检查任务已停止")

    def trigger_sync(self) -> Dict[str, Any]:
        """手动触发同步"""
        try:
            demands = []
            for status in ["WAIT_ANALYSIS", "COO_ACCEPT", "COO_HANG"]:
                status_demands = self.pm_service.fetch_demands(status_list=[status])
                demands.extend(status_demands)

            result = self.pm_service.sync_to_cache(demands)

            with self._lock:
                self.stats["last_sync_at"] = datetime.now()

            return {"success": True, **result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def trigger_process(self) -> Dict[str, Any]:
        """手动触发处理"""
        try:
            results = self.pm_service.auto_processor.process_all_pending()

            with self._lock:
                self.stats["last_process_at"] = datetime.now()
                self.stats["total_processed"] += len(results)
                self.stats["today_processed"] += len(results)

            # 发送通知
            if results:
                self.notifier.notify_auto_process_result(results)

            return {
                "success": True,
                "processed_count": len(results),
                "results": [
                    {
                        "demand_id": r.demand_id,
                        "action": r.action,
                        "reason": r.reason,
                    }
                    for r in results
                ],
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


# 单例实例
_scheduler: Optional[PMScheduler] = None


def get_pm_scheduler() -> PMScheduler:
    """获取调度器单例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = PMScheduler()
    return _scheduler


def start_pm_scheduler(
    sync_interval: int = 5,
    process_interval: int = 10,
    overdue_interval: int = 60,
) -> PMScheduler:
    """启动PM调度器"""
    scheduler = get_pm_scheduler()
    scheduler.start(sync_interval, process_interval, overdue_interval)
    return scheduler


def stop_pm_scheduler():
    """停止PM调度器"""
    global _scheduler
    if _scheduler:
        _scheduler.stop()
