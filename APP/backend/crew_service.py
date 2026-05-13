"""
人员列表服务
解析 crewlist.md 文件，提供人员信息查询和搜索功能
"""

import os
import re
import threading
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, asdict
from datetime import datetime


@dataclass
class CrewMember:
    """团队成员数据结构"""
    username: str      # 可能为空
    realname: str
    role: str          # 产品经理/开发/测试
    subrole: str       # 流程/后端开发/前端开发


class CrewService:
    """
    人员列表服务

    功能:
    - 解析 crewlist.md 提取人员信息
    - 支持username/中文名双向查询
    - 本地缓存（启动时加载，文件修改时刷新）
    - 按角色分组返回
    """

    def __init__(self, crewlist_path: Optional[str] = None):
        """
        初始化人员服务

        Args:
            crewlist_path: crewlist.md文件路径，默认为项目根目录下的crewlist.md
        """
        if crewlist_path is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.normpath(os.path.join(base_dir, "../.."))
            crewlist_path = os.path.join(project_root, "_local/notes/crewlist.md")

        self.crewlist_path = crewlist_path
        self._personnel: List[CrewMember] = []
        self._last_modified: float = 0
        self._lock = threading.Lock()

        # 初始加载
        self._load_personnel()

    def _load_personnel(self):
        """
        从crewlist.md解析人员信息

        解析规则:
        - 二级标题 (##) -> role
        - 三级标题 (###) -> subrole
        - 列表项 (- username, 中文名) -> username, realname
        """
        if not os.path.exists(self.crewlist_path):
            print(f"[CrewService] 文件不存在: {self.crewlist_path}")
            return

        try:
            with open(self.crewlist_path, 'r', encoding='utf-8') as f:
                content = f.read()

            self._last_modified = os.path.getmtime(self.crewlist_path)

            personnel = []
            current_role = ""
            current_subrole = ""

            for line in content.split('\n'):
                line = line.strip()

                # 二级标题 -> role
                if line.startswith('## ') and not line.startswith('### '):
                    current_role = line[3:].strip()
                    current_subrole = ""  # 重置子角色

                # 三级标题 -> subrole
                elif line.startswith('### '):
                    current_subrole = line[4:].strip()

                # 列表项 -> personnel
                elif line.startswith('- '):
                    # 解析格式: - username, 中文名 或 - username,中文名 或 - 中文名
                    content_part = line[2:].strip()

                    # 匹配模式: username, 中文名 或 username,中文名
                    match = re.match(r'^([a-zA-Z0-9_]+)\s*,\s*(.+)$', content_part)

                    if match:
                        username = match.group(1).strip()
                        realname = match.group(2).strip()
                    else:
                        # 只有中文名，username为空
                        username = ""
                        realname = content_part

                    if realname:  # 至少要有中文名
                        personnel.append(CrewMember(
                            username=username,
                            realname=realname,
                            role=current_role,
                            subrole=current_subrole
                        ))

            with self._lock:
                self._personnel = personnel

            print(f"[CrewService] 已加载 {len(personnel)} 位人员信息")

        except Exception as e:
            print(f"[CrewService] 加载失败: {e}")

    def _check_reload(self):
        """检查文件是否已修改，需要重新加载"""
        try:
            if os.path.exists(self.crewlist_path):
                current_mtime = os.path.getmtime(self.crewlist_path)
                if current_mtime > self._last_modified:
                    self._load_personnel()
        except Exception as e:
            print(f"[CrewService] 检查文件修改失败: {e}")

    def get_all_personnel(self) -> List[CrewMember]:
        """
        获取所有人员列表

        Returns:
            List[CrewMember]: 所有人员列表
        """
        self._check_reload()
        with self._lock:
            return self._personnel.copy()

    def search(self, query: str) -> List[CrewMember]:
        """
        模糊搜索人员

        支持:
        - 中文名搜索（部分匹配）
        - 用户名搜索（部分匹配，不区分大小写）
        - 角色搜索

        Args:
            query: 搜索关键词

        Returns:
            List[CrewMember]: 匹配的人员列表
        """
        self._check_reload()

        if not query or not query.strip():
            return self.get_all_personnel()

        query = query.strip().lower()
        results = []

        with self._lock:
            for person in self._personnel:
                # 中文名包含查询
                if query in person.realname.lower():
                    results.append(person)
                    continue

                # 用户名包含查询
                if person.username and query in person.username.lower():
                    results.append(person)
                    continue

                # 角色包含查询
                if query in person.role.lower():
                    results.append(person)
                    continue

                # 子角色包含查询
                if person.subrole and query in person.subrole.lower():
                    results.append(person)
                    continue

        return results

    def find_by_username(self, username: str) -> Optional[CrewMember]:
        """
        根据用户名查找人员

        Args:
            username: 用户名

        Returns:
            Optional[CrewMember]: 找到的人员，未找到返回None
        """
        self._check_reload()

        if not username:
            return None

        username_lower = username.lower()

        with self._lock:
            for person in self._personnel:
                if person.username and person.username.lower() == username_lower:
                    return person

        return None

    def find_by_realname(self, realname: str) -> Optional[CrewMember]:
        """
        根据中文名查找人员

        Args:
            realname: 中文名

        Returns:
            Optional[CrewMember]: 找到的人员，未找到返回None
        """
        self._check_reload()

        if not realname:
            return None

        realname_lower = realname.lower()

        with self._lock:
            for person in self._personnel:
                if person.realname.lower() == realname_lower:
                    return person

        return None

    def get_grouped_personnel(self) -> Dict[str, Any]:
        """
        获取按角色分组的人员列表

        Returns:
            Dict: {
                "personnel": [...],
                "groups": {
                    "产品经理": ["流程", "消息中心"],
                    "开发": ["后端开发", "前端开发"],
                    "测试": []
                }
            }
        """
        self._check_reload()

        personnel_list = []
        groups: Dict[str, List[str]] = {}

        with self._lock:
            for person in self._personnel:
                personnel_list.append({
                    "username": person.username,
                    "realname": person.realname,
                    "role": person.role,
                    "subrole": person.subrole
                })

                # 按role分组subrole
                if person.role not in groups:
                    groups[person.role] = []

                if person.subrole and person.subrole not in groups[person.role]:
                    groups[person.role].append(person.subrole)

        return {
            "personnel": personnel_list,
            "groups": groups
        }

    def get_recent_users(self, limit: int = 5) -> List[Dict[str, str]]:
        """
        获取最近常用的人员列表

        由于我们没有持久化的使用记录，返回按角色排序的前N个人员

        Args:
            limit: 返回数量限制

        Returns:
            List[Dict]: 最近使用的人员列表
        """
        self._check_reload()

        # 按产品经理 > 开发 > 测试排序，每个角色取前几个
        result = []
        role_priority = {"产品经理": 1, "开发": 2, "测试": 3}

        with self._lock:
            sorted_personnel = sorted(
                self._personnel,
                key=lambda p: (role_priority.get(p.role, 99), p.realname)
            )

            for person in sorted_personnel[:limit]:
                result.append({
                    "username": person.username,
                    "realname": person.realname,
                    "role": person.role,
                    "subrole": person.subrole
                })

        return result


# 单例实例
crew_service = CrewService()


# 便捷函数
def get_crew_service() -> CrewService:
    """获取CrewService单例实例"""
    return crew_service
