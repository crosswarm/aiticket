"""BIP 产品分类解析：把 KB 文档归属到 domain_cloud / label / application。

背景
----
知识区隔的正确粒度是 **label**（BIP 主数据里的"领域"），不是 Jira project，
也不是 KB 的目录名。实测 LCZX 一个 Jira 项目的知识横跨 PF / OA / BMM 三个 label、
两个领域云，所以 label 只能做**加权**，绝不能做硬过滤。

本模块只负责"这篇文档属于哪个 label / application"，加权逻辑在 kb_runtime_service。

证据优先级（从高到低）
----------------------
1. ``override``         —— data/kb_taxonomy_overrides.json 人工指定，一票定音
2. ``directory``        —— 目录名与 application 名精确相同
3. ``alias``            —— 内置别名表（bip-workflow → 工作流）
4. ``second_category``  —— 二级目录名反查（云平台/开发平台 这类结构）
5. ``service_code``     —— 正文里的 BIP 服务编码（如 XTLCZX007）反查主数据
6. ``fallback``         —— 都没命中，原样退回目录名，行为与改造前一致

⚠️ service_code 排在目录名**之后**，这是实测修正过的顺序。
初版把 service_code 当最强证据，结果 977 篇实跑中
``UI模板/技术架构/10.UI模板管理-模板列表上复制按钮功能说明`` 因正文提到 ``GZTACT015``
被归到「权限管理」——**正文提及往往是引用而非归属**，而目录是人工批量组织的，更可靠。

快照缺失或损坏时整体降级为 ``unavailable``，classify 原样退回目录名。
172 是气隙机，这条降级路径必须永远可用——不能让分类问题拖垮索引构建。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_SNAPSHOT_PATH = _DATA_DIR / "bip_taxonomy.json"
DEFAULT_OVERRIDES_PATH = _DATA_DIR / "kb_taxonomy_overrides.json"

# KB 现有内容全部落在应用平台/云平台下，同名 application 优先取这两个领域云。
PREFERRED_DOMAIN_CLOUDS = ("PFC", "CPC")

# 目录名 → BIP 归属。目录名是历史形成的，与主数据不完全同名，这里做人工桥接。
# 值的形态：
#   {"application": 名称}      映射到具体应用
#   {"label": 编码}            只能定到 label（主数据里没有对应 application）
#   {"defer_to_second": True}  该层是聚合目录，真正的归属看二级目录名
#   {"external": True}         不属于 BIP 产品体系（竞品资料）
#   {"exclude": True}          压根不该进 KB（误扫入的源码/工程文件）
# 未列出的目录走精确匹配或 fallback，不硬塞。
DEFAULT_ALIASES: dict[str, dict] = {
    "bip-workflow": {"application": "工作流"},
    "流程中心": {"application": "工作流"},
    "打印": {"application": "打印模板"},
    "导入导出": {"application": "导入导出模板"},
    "规则": {"application": "规则引擎"},
    "消息": {"application": "消息平台"},
    "组织": {"application": "组织管理"},
    "权限": {"application": "权限管理"},
    "元数据": {"application": "元数据服务"},
    "配置迁移": {"application": "迁移工具"},
    "MDD开发框架": {"application": "MDD后端框架"},
    # YPD / 公式 在主数据里没有对应 application，只能定到 label。
    # 公式归 PF 的依据是文档内自述路径「应用平台/开发框架知识库/公式/产品使用文档」，
    # 与 MDD / YPD 同属开发框架知识库。如判断有变，用 overrides 覆盖即可。
    "YPD开发框架": {"label": "PF"},
    "公式": {"label": "PF"},
    # 云平台是聚合目录，二级目录才是真正的 label（开发平台/数据平台/智能平台/集成平台…）
    "云平台": {"defer_to_second": True},
    # 金蝶是竞品，BIP 体系里没有对应，不硬塞 label
    "kingdee-workflow": {"external": True},
    # AITicket 自身的源码目录被 KB 扫描器误收（APP/backend/README.md 之类），
    # 它们不是业务知识，会污染召回。根治要改 KB 扫描范围，这里先标记出来。
    "APP": {"exclude": True},
    # KB 扫描器把自己的转换产物（KB/OUTPUT/converted/**）也当源文件索引了一遍，
    # 同一份知识在库里存两份，检索时互相挤占名额。
    "OUTPUT": {"exclude": True},
}

EXTERNAL_LABEL = "_external"
EXCLUDED_LABEL = "_excluded"

# BIP 服务编码形如 XTLCZX007 / GZTTMP012 / XTLCZXGK001。
# 这里只做粗筛，真正的判定是"在不在 services 字典里"，所以宁可宽一点。
_SERVICE_CODE_CANDIDATE = re.compile(r"\b[A-Z][A-Z0-9_]{3,29}\b")

_UNSAFE_PATH_CHARS = re.compile(r'[\x00-\x1f/\\:*?"<>|]')


def safe_dirname(name: str) -> str:
    """把 label / application 名转成可直接当目录名的形式。

    主数据里确实有带斜杠的名字（application「批次/序列号」），
    直接拿去拼路径会凭空多切一级目录，归属再也解析不回来。
    上传落盘与归属解析必须共用这个函数，否则两边对不上。
    """
    cleaned = _UNSAFE_PATH_CHARS.sub("_", (name or "").strip())
    cleaned = cleaned.strip(". ")
    return cleaned[:120]


@dataclass
class Classification:
    """一篇文档的归属结论。字段名对齐 KB 索引里的 l1_module / l2_module 语义。"""

    label_code: str = ""
    label_name: str = ""
    application_code: str = ""
    application_name: str = ""
    domain_cloud_code: str = ""
    domain_cloud_name: str = ""
    evidence: str = "fallback"
    service_codes: list[str] = field(default_factory=list)

    @property
    def is_external(self) -> bool:
        """竞品资料：是知识，但不在 BIP 产品体系内。"""
        return self.label_code == EXTERNAL_LABEL

    @property
    def is_excluded(self) -> bool:
        """误扫入 KB 的非知识文件（源码、工程配置），应从知识库剔除。"""
        return self.label_code == EXCLUDED_LABEL

    @property
    def in_bip(self) -> bool:
        return bool(self.label_code) and not self.is_external and not self.is_excluded


class BipTaxonomy:
    def __init__(self, path: Path | None = None, overrides_path: Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_SNAPSHOT_PATH
        self.overrides_path = Path(overrides_path) if overrides_path else DEFAULT_OVERRIDES_PATH
        self.domain_clouds: dict[str, str] = {}
        self.labels: dict[str, dict] = {}
        self.applications: dict[str, dict] = {}
        self.services: dict[str, dict] = {}
        self.available = False
        self._applications_by_name: dict[str, list[str]] = defaultdict(list)
        self._labels_by_name: dict[str, list[str]] = defaultdict(list)
        self._aliases: dict[str, dict] = dict(DEFAULT_ALIASES)
        self._override_names: set[str] = set()
        # 领域模块 → label/application 的解析缓存（每次检索都要用，模块列表很稳定）
        self._boost_cache: dict[tuple[str, ...], tuple[set[str], set[str]]] = {}
        self._load()

    # ------------------------------------------------------------ 加载

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.warning("[BipTaxonomy] 分类快照不存在，归属解析降级为原样退回: %s", self.path)
            return
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("[BipTaxonomy] 分类快照读取失败（%s），归属解析降级: %s", exc, self.path)
            return

        if not isinstance(payload, dict):
            logger.warning("[BipTaxonomy] 分类快照格式异常，归属解析降级")
            return

        self.domain_clouds = payload.get("domain_clouds") or {}
        self.labels = payload.get("labels") or {}
        self.applications = payload.get("applications") or {}
        self.services = payload.get("services") or {}
        if not self.labels or not self.applications:
            logger.warning("[BipTaxonomy] 分类快照内容为空，归属解析降级")
            return

        for code, app in self.applications.items():
            name = (app or {}).get("name")
            if name:
                self._applications_by_name[name].append(code)
                # 落盘时名字里的斜杠会被替换，解析时要能按净化名找回来
                safe = safe_dirname(name)
                if safe and safe != name:
                    self._applications_by_name[safe].append(code)
        for code, label in self.labels.items():
            name = (label or {}).get("name")
            if name:
                self._labels_by_name[name].append(code)
                safe = safe_dirname(name)
                if safe and safe != name:
                    self._labels_by_name[safe].append(code)

        self._load_overrides()
        self.available = True

    def _load_overrides(self) -> None:
        try:
            payload = json.loads(self.overrides_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("[BipTaxonomy] 覆盖表读取失败，忽略: %s", exc)
            return
        aliases = (payload or {}).get("aliases")
        if isinstance(aliases, dict):
            self._aliases.update(aliases)
            self._override_names = set(aliases)

    # ------------------------------------------------------------ 反查

    def resolve_service(self, service_code: str) -> dict | None:
        """service_code → 完整四层链路。未知编码返回 None。"""
        service = self.services.get(service_code)
        if not service:
            return None
        app_code = service.get("application", "")
        app = self.applications.get(app_code) or {}
        label_code = app.get("label", "")
        label = self.labels.get(label_code) or {}
        return {
            "service_code": service_code,
            "service_name": service.get("name", ""),
            "application_code": app_code,
            "application_name": app.get("name", ""),
            "label_code": label_code,
            "label_name": label.get("name", ""),
            "domain_cloud_code": label.get("domain_cloud", ""),
            "domain_cloud_name": label.get("domain_cloud_name", ""),
        }

    def _in_preferred(self, label_code: str) -> bool:
        dc = (self.labels.get(label_code) or {}).get("domain_cloud", "")
        return dc in PREFERRED_DOMAIN_CLOUDS

    def _pick_application(self, name: str, strict: bool = False) -> str:
        """同名 application 消歧。

        主数据里 application 层混着租户自建应用和跨领域云的同名项，
        例如「权限管理」有 GZTACT / ALONE_GZTACT / auth 三个，
        「组织管理」在应用平台和人力云各有一个。

        ``strict=True`` 时只在应用平台/云平台里找。用于 topic 名这类自由文本——
        实测「主数据」会匹配到财务云的「境外财资/主数据」，宁可不归属也不能归错。
        """
        candidates = self._applications_by_name.get(name) or []
        if strict:
            candidates = [c for c in candidates if self._in_preferred((self.applications.get(c) or {}).get("label", ""))]
        if not candidates:
            return ""
        if len(candidates) == 1:
            return candidates[0]

        def rank(code: str) -> tuple:
            app = self.applications.get(code) or {}
            label = self.labels.get(app.get("label", "")) or {}
            dc = label.get("domain_cloud", "")
            return (
                bool(app.get("tenant_built")),          # 租户自建排最后
                dc not in PREFERRED_DOMAIN_CLOUDS,      # 目标领域云优先
                not dc,                                 # 无归属的排后面
                len(code),                              # ALONE_GZTACT 让位给 GZTACT
                code,
            )

        return sorted(candidates, key=rank)[0]

    def _build(self, application_code: str, evidence: str, service_codes: list[str] | None = None) -> Classification:
        app = self.applications.get(application_code) or {}
        label_code = app.get("label", "")
        label = self.labels.get(label_code) or {}
        return Classification(
            label_code=label_code,
            label_name=label.get("name", ""),
            application_code=application_code,
            application_name=app.get("name", ""),
            domain_cloud_code=label.get("domain_cloud", ""),
            domain_cloud_name=label.get("domain_cloud_name", ""),
            evidence=evidence,
            service_codes=service_codes or [],
        )

    def _from_label(self, label_code: str, evidence: str) -> Classification:
        label = self.labels.get(label_code) or {}
        return Classification(
            label_code=label_code,
            label_name=label.get("name", ""),
            domain_cloud_code=label.get("domain_cloud", ""),
            domain_cloud_name=label.get("domain_cloud_name", ""),
            evidence=evidence,
        )

    def _pick_label(self, name: str, strict: bool = False) -> str:
        """同名 label 消歧（"开发者中心" 有 PAAS / PASS 两个编码）。"""
        candidates = self._labels_by_name.get(name) or []
        if strict:
            candidates = [c for c in candidates if self._in_preferred(c)]
        if not candidates:
            return ""
        if len(candidates) == 1:
            return candidates[0]

        def rank(code: str) -> tuple:
            dc = (self.labels.get(code) or {}).get("domain_cloud", "")
            return (dc not in PREFERRED_DOMAIN_CLOUDS, not dc, len(code), code)

        return sorted(candidates, key=rank)[0]

    def _resolve_name(
        self,
        name: str,
        evidence: str,
        strict: bool = False,
        prefer: str = "application",
    ) -> Classification | None:
        """把一个名字解析成归属，可指定优先当 label 还是 application 解释。

        有些名字**两边都是**——「业务模型管理」既是 label(BMM) 又是 application(BMMMM)。
        知识按 ``KB/<label>/<application>/`` 存放，所以一级目录必须优先当 label 解释，
        否则 `KB/业务模型管理/UI模板/` 会把 l2 也解析成「业务模型管理」，把二级目录吃掉。
        """
        name = (name or "").strip()
        if not name:
            return None

        def as_label() -> Classification | None:
            if name in self._labels_by_name:
                code = self._pick_label(name, strict=strict)
                if code:
                    return self._from_label(code, evidence)
            return None

        def as_application() -> Classification | None:
            if name in self._applications_by_name:
                code = self._pick_application(name, strict=strict)
                if code:
                    return self._build(code, evidence)
            return None

        order = (as_label, as_application) if prefer == "label" else (as_application, as_label)
        for resolve in order:
            result = resolve()
            if result:
                return result
        return None

    def _from_alias(self, spec: dict, evidence: str, second_category: str = "") -> Classification | None:
        if spec.get("exclude"):
            return Classification(label_code=EXCLUDED_LABEL, label_name=EXCLUDED_LABEL, evidence=evidence)
        if spec.get("external"):
            return Classification(label_code=EXTERNAL_LABEL, label_name=EXTERNAL_LABEL, evidence=evidence)
        if spec.get("defer_to_second"):
            # 聚合目录：真正的归属在二级目录名上
            return self._resolve_name(second_category, "second_category")
        app_name = spec.get("application")
        if app_name:
            code = self._pick_application(app_name)
            if code:
                return self._build(code, evidence)
        label_code = spec.get("label")
        if label_code and label_code in self.labels:
            return self._from_label(label_code, evidence)
        return None

    # ------------------------------------------------------------ 主入口

    def extract_service_codes(self, text: str) -> list[str]:
        """从正文提取真实存在的 BIP 服务编码，顺序保留、去重。"""
        if not text or not self.services:
            return []
        seen: list[str] = []
        for token in _SERVICE_CODE_CANDIDATE.findall(text):
            if token in self.services and token not in seen:
                seen.append(token)
        return seen

    def classify(
        self,
        top_category: str = "",
        second_category: str = "",
        text: str | None = None,
        strict: bool = False,
    ) -> Classification:
        """判定一篇文档的 BIP 归属。任何输入都不抛异常。

        ``strict=True`` 把名称匹配限制在应用平台/云平台内，用于 topic 名这类
        自由文本来源——避免「主数据」被匹配到财务云去。
        """
        top_category = (top_category or "").strip()
        second_category = (second_category or "").strip()

        if not self.available:
            return Classification(
                label_name=top_category,
                application_name=second_category,
                evidence="unavailable",
            )

        # 1. 人工覆盖，一票定音
        if top_category in self._override_names:
            result = self._from_alias(self._aliases[top_category], "override", second_category)
            if result:
                return result

        # 2. 一级目录名。知识按 KB/<label>/<application>/ 存放，所以优先当 label 解释；
        #    历史目录名多是 application 名（UI模板/业务流/打印），label 匹配不上会自动回退。
        result = self._resolve_name(top_category, "directory", strict=strict, prefer="label")
        if result:
            # 一级只定到 label 时，二级目录很可能就是它下面的 application，
            # 确认归属一致后采用更精确的那个（KB/数字化建模/工作流/ → 工作流）
            if not result.application_code and second_category:
                nested = self._resolve_name(second_category, "directory", strict=strict)
                if nested and nested.application_code and nested.label_code == result.label_code:
                    return nested
            return result

        # 3. 内置别名表
        if top_category in self._aliases:
            result = self._from_alias(self._aliases[top_category], "alias", second_category)
            if result:
                return result

        # 4. 二级目录名反查（一级目录没能定位时）
        result = self._resolve_name(second_category, "second_category", strict=strict)
        if result:
            return result

        # 5. 正文 service_code 兜底：按 application 取众数。
        #    排在目录名之后是实测教训——正文提及往往是引用而非归属。
        service_codes = self.extract_service_codes(text or "")
        if service_codes:
            app_votes = Counter()
            for code in service_codes:
                app_code = (self.services.get(code) or {}).get("application")
                if app_code:
                    app_votes[app_code] += 1
            if app_votes:
                winner = app_votes.most_common(1)[0][0]
                hit_codes = [c for c in service_codes if (self.services.get(c) or {}).get("application") == winner]
                return self._build(winner, "service_code", hit_codes)

        # 6. 兜底：原样退回，与改造前行为一致
        return Classification(
            label_name=top_category,
            application_name=second_category,
            evidence="fallback",
        )


    # ------------------------------------------------------------ 检索加权

    def resolve_boost_targets(self, modules: list[str] | None) -> tuple[set[str], set[str]]:
        """把用户的领域模块解析成 (label 名集合, application 名集合)。

        领域模块可能是 ``流程中心|工作流设计(含所有属性设置)`` 这种两级值，
        逐段解析后取并集。strict 避免串到其它领域云。
        """
        labels: set[str] = set()
        apps: set[str] = set()
        if not modules or not self.available:
            return labels, apps
        cache_key = tuple(sorted(str(m) for m in modules))
        cached = self._boost_cache.get(cache_key)
        if cached is not None:
            return cached
        for module in modules:
            for part in str(module or "").split("|"):
                part = part.strip()
                if not part:
                    continue
                result = self.classify(top_category=part, strict=True)
                if result.label_name:
                    labels.add(result.label_name)
                if result.application_name:
                    apps.add(result.application_name)
        self._boost_cache[cache_key] = (labels, apps)
        return labels, apps


# 检索加权分层系数。label 是知识区隔的主维度，权重最高；
# project 只是统计维度，给很轻的一点倾向即可（知识跨项目复用是常态）。
LABEL_BOOST = 0.25
APPLICATION_BOOST = 0.10
PROJECT_BOOST = 0.05
# 索引尚未回填 BIP 分类时 l1/l2 仍是目录名，保留原有匹配加分，避免过渡期效果塌陷
LEGACY_MODULE_BOOST = 0.08


def apply_taxonomy_boost(
    items: list[dict],
    taxonomy: BipTaxonomy,
    module_boost: list[str] | None = None,
    project_key: str | None = None,
) -> list[dict]:
    """分层加权：label > application > project。全是加分，不做任何过滤。

    为什么不硬过滤：实测 LCZX 一个 Jira 项目的知识横跨 PF / OA / BMM 三个 label、
    两个领域云，硬过滤会直接丢掉正确答案。跨 label 的知识排后面即可，不能召不回。

    唯一的剔除是 ``_excluded``——那不是知识，是被扫描器误收的源码和转换产物。
    """
    kept = [item for item in items if item.get("l1_module") != EXCLUDED_LABEL]

    boost_labels, boost_apps = taxonomy.resolve_boost_targets(module_boost)
    legacy = set(module_boost or [])
    if not (boost_labels or boost_apps or legacy or project_key):
        return kept

    for item in kept:
        l1 = item.get("l1_module")
        l2 = item.get("l2_module")
        bonus = 0.0
        if l1 and l1 in boost_labels:
            bonus += LABEL_BOOST
        if l2 and l2 in boost_apps:
            bonus += APPLICATION_BOOST
        if legacy and (l1 in legacy or l2 in legacy):
            bonus += LEGACY_MODULE_BOOST
        if project_key and project_key != "_global" and item.get("project_key") == project_key:
            bonus += PROJECT_BOOST
        if bonus:
            item["score"] = item.get("score", 0) + bonus
    kept.sort(key=lambda x: x.get("score", 0), reverse=True)
    return kept


_INSTANCE: BipTaxonomy | None = None


def get_bip_taxonomy() -> BipTaxonomy:
    """进程内单例。快照 1.2MB，重复解析没必要。"""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = BipTaxonomy()
    return _INSTANCE
