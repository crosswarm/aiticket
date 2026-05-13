"""
PM 协作任务通知器
提供飞书消息通知功能
"""

from datetime import datetime
from typing import List, Optional, Dict, Any

from models.pm_models import PMDemand, ProcessResult, ProcessAction


class PMNotifier:
    """PM协作任务通知器"""

    def __init__(self, feishu_session_id: Optional[str] = None):
        self.session_id = feishu_session_id or "oc_72ef8553bb8b552435cd91b0fb1e86ab"
        self._notifier = None

    def _get_notifier(self):
        """获取飞书通知器实例"""
        if self._notifier is None:
            try:
                from services.feishu_notifier import get_notifier
                self._notifier = get_notifier()
            except ImportError:
                print("[PMNotifier] 飞书通知器未找到")
                return None
        return self._notifier

    def notify_overdue_demands(self, demands: List[PMDemand]) -> bool:
        """
        通知超时的需求

        Args:
            demands: 超时的需求列表

        Returns:
            是否发送成功
        """
        if not demands:
            return True

        notifier = self._get_notifier()
        if not notifier:
            print("[PMNotifier] 无法发送通知：飞书通知器不可用")
            return False

        # 构建消息内容
        lines = [
            "🔔 **PM协作任务超时提醒**",
            "",
            f"以下 **{len(demands)}** 个协作需求已超过2个工作日未处理：",
            "",
            "| 需求编号 | 标题 | 提出人 | 等待天数 |",
            "|---------|------|--------|---------|",
        ]

        for d in demands[:20]:  # 最多显示20条
            title = d.title[:20] + "..." if len(d.title) > 20 else d.title
            proposer = d.proposer_display[:15]
            waiting = f"{d.waiting_days}天 ⚠️" if d.waiting_days > 3 else f"{d.waiting_days}天"
            lines.append(f"| {d.code} | {title} | {proposer} | {waiting} |")

        if len(demands) > 20:
            lines.append(f"| ... | 还有 {len(demands) - 20} 个需求 | ... | ... |")

        lines.extend([
            "",
            f"[点击查看详情](http://localhost:8000/pm_board.html)",
        ])

        message = "\n".join(lines)

        try:
            notifier.send_message(message)
            print(f"[PMNotifier] 已发送超时提醒，共 {len(demands)} 个需求")
            return True
        except Exception as e:
            print(f"[PMNotifier] 发送超时提醒失败: {e}")
            return False

    def notify_auto_process_result(
        self,
        results: List[ProcessResult],
        notify_empty: bool = False,
    ) -> bool:
        """
        通知自动处理结果

        Args:
            results: 处理结果列表
            notify_empty: 无处理结果时是否通知

        Returns:
            是否发送成功
        """
        if not results and not notify_empty:
            return True

        notifier = self._get_notifier()
        if not notifier:
            print("[PMNotifier] 无法发送通知：飞书通知器不可用")
            return False

        # 统计
        accepted = [r for r in results if r.action == ProcessAction.ACCEPT]
        rejected = [r for r in results if r.action == ProcessAction.REJECT]
        manual = [r for r in results if r.action == ProcessAction.MANUAL]

        lines = [
            "🤖 **PM协作任务自动处理报告**",
            "",
            f"处理时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
        ]

        if accepted:
            lines.extend([
                f"**已自动采纳 ({len(accepted)})**:",
            ])
            for r in accepted[:10]:
                lines.append(f"- {r.demand_id}: {r.reason[:50]}")
            if len(accepted) > 10:
                lines.append(f"- ... 还有 {len(accepted) - 10} 个")
            lines.append("")

        if rejected:
            lines.extend([
                f"**已自动拒绝 ({len(rejected)})**:",
            ])
            for r in rejected[:10]:
                lines.append(f"- {r.demand_id}: {r.reason[:50]}")
            if len(rejected) > 10:
                lines.append(f"- ... 还有 {len(rejected) - 10} 个")
            lines.append("")

        if manual:
            lines.extend([
                f"**需人工处理 ({len(manual)})**:",
            ])
            for r in manual[:10]:
                error = r.error_message or ""
                lines.append(f"- {r.demand_id}: {r.reason} {error}")
            if len(manual) > 10:
                lines.append(f"- ... 还有 {len(manual) - 10} 个")
            lines.append("")

        if not results:
            lines.append("本次无需要处理的需求。")

        lines.append("[点击查看详情](http://localhost:8000/pm_board.html)")

        message = "\n".join(lines)

        try:
            notifier.send_message(message)
            print(f"[PMNotifier] 已发送自动处理报告，处理 {len(results)} 个需求")
            return True
        except Exception as e:
            print(f"[PMNotifier] 发送处理报告失败: {e}")
            return False

    def notify_new_predefine(self, predefine: Any) -> bool:
        """
        通知新创建的预定义协作

        Args:
            predefine: 预定义数据

        Returns:
            是否发送成功
        """
        notifier = self._get_notifier()
        if not notifier:
            return False

        message = f"""📝 **新的预定义协作已创建**

提出人: {predefine.proposer_name}
领域: {predefine.proposer_domain}
关键词: {predefine.keywords_display}
有效期至: {predefine.expires_at.strftime('%Y-%m-%d')}

系统将自动匹配符合此预定义的新协作需求。
"""

        try:
            notifier.send_message(message)
            return True
        except Exception as e:
            print(f"[PMNotifier] 发送预定义通知失败: {e}")
            return False

    def notify_daily_summary(
        self,
        stats: Dict[str, Any],
        overdue_demands: List[PMDemand],
    ) -> bool:
        """
        发送每日汇总通知

        Args:
            stats: 统计数据
            overdue_demands: 超时需求列表

        Returns:
            是否发送成功
        """
        notifier = self._get_notifier()
        if not notifier:
            return False

        lines = [
            "📊 **PM协作任务日报**",
            "",
            f"日期: {datetime.now().strftime('%Y-%m-%d')}",
            "",
            "**当前状态**:",
            f"- 待分析: {stats.get('wait_analysis', 0)}",
            f"- 已采纳: {stats.get('coo_accept', 0)}",
            f"- 已挂起: {stats.get('coo_hang', 0)}",
            f"- 已超时: {stats.get('overdue', 0)} ⚠️",
            "",
        ]

        if overdue_demands:
            lines.extend([
                f"**⚠️ 超时提醒**:",
                f"共有 {len(overdue_demands)} 个需求超过2个工作日未处理，请尽快处理！",
                "",
            ])

        lines.append("[点击查看详情](http://localhost:8000/pm_board.html)")

        message = "\n".join(lines)

        try:
            notifier.send_message(message)
            return True
        except Exception as e:
            print(f"[PMNotifier] 发送日报失败: {e}")
            return False


# 单例实例
_notifier: Optional[PMNotifier] = None


def get_pm_notifier() -> PMNotifier:
    """获取PM通知器单例"""
    global _notifier
    if _notifier is None:
        _notifier = PMNotifier()
    return _notifier
