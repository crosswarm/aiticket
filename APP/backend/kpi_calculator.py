"""
KPI计算器 — 每客户问题数指标计算、趋势分析、不达标客户识别
"""

import json
import os
from typing import Dict, List, Optional

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
CONFIG_PATH = os.path.join(BASE_DIR, "config", "kpi_config.json")
CONCLUSION_DIR = os.path.join(PROJECT_ROOT, "conclusion")
WEEKLY_REPORT_DIR = os.path.join(CONCLUSION_DIR, "WeeklyReports")
MONTHLY_REPORT_DIR = os.path.join(CONCLUSION_DIR, "MonthlyReports")
SRC_DIR = os.path.join(PROJECT_ROOT, "src")


class KPICalculator:
    """每客户问题数KPI计算器"""

    def __init__(self, config_path: str = None):
        self.config = self._load_config(config_path or CONFIG_PATH)
        self.customer_field = self.config.get("customer_field", "自定义字段(项目名称)")
        self.target = self.config.get("target_per_customer", 3.37)
        self.baseline = self.config.get("baseline", {})
        self.baseline_per_customer = self.baseline.get("per_customer", 4.21)
        self.weekly_threshold = self.config.get("weekly_alert_threshold", 3)

    @staticmethod
    def _load_config(path: str) -> dict:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------------
    # 核心计算
    # ------------------------------------------------------------------

    def calculate_period_kpi(self, df: pd.DataFrame) -> dict:
        """计算某一周期的每客户问题数"""
        if df is None or df.empty:
            return {"total_issues": 0, "unique_customers": 0, "per_customer": 0, "customer_breakdown": {}}

        # 归一化列名 (不修改原始DataFrame)
        if any(c != c.strip() for c in df.columns):
            df = df.copy()
            df.columns = [c.strip() for c in df.columns]

        cust_col = self.customer_field
        if cust_col not in df.columns:
            return {"total_issues": len(df), "unique_customers": 0, "per_customer": 0,
                    "customer_breakdown": {}, "error": f"缺少字段: {cust_col}"}

        customers = df[cust_col].fillna("未知客户")
        breakdown = customers.value_counts().to_dict()
        unique = len(breakdown)
        total = len(df)
        per_customer = round(total / unique, 2) if unique > 0 else 0

        return {
            "total_issues": total,
            "unique_customers": unique,
            "per_customer": per_customer,
            "customer_breakdown": breakdown,
        }

    def calculate_yoy_kpi(self, current_kpi: dict, last_year_kpi: dict) -> dict:
        """计算同比KPI变化"""
        c = current_kpi.get("per_customer", 0)
        l = last_year_kpi.get("per_customer", 0)

        if l == 0:
            return {"current": c, "last_year": l, "change_pct": None, "arrow": "→",
                    "target": self.target, "gap": round(c - self.target, 2)}

        change = round((c - l) / l * 100, 1)
        arrow = "↑" if change > 0 else "↓" if change < 0 else "→"

        return {
            "current": c,
            "last_year": l,
            "change_pct": change,
            "arrow": arrow,
            "target": self.target,
            "gap": round(c - self.target, 2),
        }

    def calculate_mom_kpi(self, current_kpi: dict, last_period_kpi: dict) -> dict:
        """计算环比KPI变化"""
        c = current_kpi.get("per_customer", 0)
        l = last_period_kpi.get("per_customer", 0)

        if l == 0:
            return {"current": c, "last_period": l, "change_pct": None, "arrow": "→"}

        change = round((c - l) / l * 100, 1)
        arrow = "↑" if change > 0 else "↓" if change < 0 else "→"
        return {"current": c, "last_period": l, "change_pct": change, "arrow": arrow}

    # ------------------------------------------------------------------
    # 不达标客户
    # ------------------------------------------------------------------

    def get_non_compliant_customers(self, df: pd.DataFrame, threshold: int = None) -> List[dict]:
        """获取超过阈值的客户清单及其工单"""
        if df is None or df.empty:
            return []

        threshold = threshold or self.weekly_threshold
        cust_col = self.customer_field
        if cust_col not in df.columns:
            return []

        customers = df[cust_col].fillna("未知客户")
        counts = customers.value_counts()
        over = counts[counts > threshold]

        type_col = "自定义字段(研发确认问题类型)"
        result = []
        for cust_name, count in over.items():
            cust_df = df[df[cust_col] == cust_name]
            # 提取该客户TOP问题
            issues = []
            for _, row in cust_df.head(5).iterrows():
                issues.append({
                    "key": str(row.get("问题关键字", "")),
                    "summary": str(row.get("概要", ""))[:60],
                    "type": str(row.get(type_col, "")) if type_col in df.columns else "",
                })
            # TOP问题类型
            top_types = ""
            if type_col in cust_df.columns:
                top_types = ", ".join(cust_df[type_col].fillna("未分类").value_counts().head(3).index.tolist())

            result.append({
                "customer": str(cust_name),
                "issue_count": int(count),
                "top_issue_types": top_types,
                "issues": issues,
            })

        result.sort(key=lambda x: x["issue_count"], reverse=True)
        return result

    # ------------------------------------------------------------------
    # 客户分布区间
    # ------------------------------------------------------------------

    def get_customer_distribution_bands(self, df: pd.DataFrame) -> List[dict]:
        """按工单数区间统计客户分布"""
        if df is None or df.empty:
            return []

        cust_col = self.customer_field
        if cust_col not in df.columns:
            return []

        counts = df[cust_col].fillna("未知客户").value_counts()
        total_customers = len(counts)
        total_issues = int(counts.sum())

        bands = [
            ("1-2条", 1, 2),
            ("3-4条", 3, 4),
            ("5-8条", 5, 8),
            ("9+条", 9, 99999),
        ]

        result = []
        for label, lo, hi in bands:
            band_counts = counts[(counts >= lo) & (counts <= hi)]
            cust_count = len(band_counts)
            issue_count = int(band_counts.sum())
            result.append({
                "band": label,
                "customer_count": cust_count,
                "customer_pct": round(cust_count / total_customers * 100, 1) if total_customers > 0 else 0,
                "issue_count": issue_count,
                "issue_pct": round(issue_count / total_issues * 100, 1) if total_issues > 0 else 0,
            })
        return result

    # ------------------------------------------------------------------
    # YTD从CSV直接计算 (用于stat card)
    # ------------------------------------------------------------------

    def calculate_ytd_from_csv(self, year: int, end_date: str = None) -> dict:
        """从全部可用的周报CSV计算年初至今的KPI (精确去重)"""
        if not os.path.exists(SRC_DIR):
            return {}

        # 收集该年所有周数据CSV
        csv_files = sorted([f for f in os.listdir(SRC_DIR)
                           if "周数据" in f and f.endswith(".csv") and str(year) in f])

        if not csv_files:
            return {}

        dfs = []
        for cf in csv_files:
            try:
                tdf = pd.read_csv(os.path.join(SRC_DIR, cf))
                tdf.columns = [c.strip() for c in tdf.columns]
                dfs.append(tdf)
            except Exception:
                continue

        if not dfs:
            return {}

        combined = pd.concat(dfs, ignore_index=True)
        # 去重
        if "问题关键字" in combined.columns:
            combined = combined.drop_duplicates(subset=["问题关键字"])

        # 过滤到year范围
        combined["创建日期"] = pd.to_datetime(combined["创建日期"], errors="coerce")
        combined = combined[combined["创建日期"].dt.year == year]
        if end_date:
            combined = combined[combined["创建日期"] <= pd.to_datetime(end_date)]

        return self.calculate_period_kpi(combined)

    # ------------------------------------------------------------------
    # 每日每客户密度趋势
    # ------------------------------------------------------------------

    def calculate_daily_customer_density(self, df: pd.DataFrame) -> dict:
        """计算每日的每客户问题数密度, 用于趋势图"""
        if df is None or df.empty:
            return {}

        if any(c != c.strip() for c in df.columns):
            df = df.copy()
            df.columns = [c.strip() for c in df.columns]

        cust_col = self.customer_field
        if cust_col not in df.columns or "创建日期" not in df.columns:
            return {}

        df = df.copy()
        df["_date"] = pd.to_datetime(df["创建日期"], errors="coerce").dt.date
        df = df.dropna(subset=["_date"])

        result = {}
        for date, group in df.groupby("_date"):
            customers = group[cust_col].fillna("未知客户")
            unique = customers.nunique()
            total = len(group)
            density = round(total / unique, 2) if unique > 0 else 0
            result[str(date)] = density
        return result

    # ------------------------------------------------------------------
    # 趋势: 周KPI滚动 (最近N周)
    # ------------------------------------------------------------------

    def get_weekly_kpi_trend(self, weeks: int = 8) -> List[dict]:
        """从已生成的周报JSON中提取最近N周的YTD KPI滚动趋势"""
        if not os.path.exists(WEEKLY_REPORT_DIR):
            return []

        files = sorted(
            [f for f in os.listdir(WEEKLY_REPORT_DIR) if f.endswith(".json")],
            reverse=True,
        )

        trend = []
        for fname in files[:weeks]:
            try:
                with open(os.path.join(WEEKLY_REPORT_DIR, fname), "r", encoding="utf-8") as f:
                    data = json.load(f)
                kpi = data.get("kpi_analysis", {})
                ytd = kpi.get("ytd", {})
                # 优先使用YTD指标，旧报告无YTD时回退到当周current
                source = ytd if ytd and ytd.get("per_customer") else kpi.get("current", {})
                if source.get("per_customer"):
                    period = data.get("meta", {}).get("period", fname)
                    yoy = kpi.get("yoy_change_pct")
                    trend.append({
                        "period": period,
                        "per_customer": source["per_customer"],
                        "total_issues": source.get("total_issues", 0),
                        "unique_customers": source.get("unique_customers", 0),
                        "yoy_change": f"{yoy:+.1f}%" if yoy is not None else "-",
                        "is_ytd": bool(ytd and ytd.get("per_customer")),
                    })
            except Exception as e:
                print(f"[KPI] 读取周报KPI失败 {fname}: {e}")
                continue

        trend.reverse()  # 按时间正序
        return trend

    # ------------------------------------------------------------------
    # 趋势: 月度KPI (2025 vs 2026)
    # ------------------------------------------------------------------

    def get_monthly_kpi_trend(self, year: int = None) -> List[dict]:
        """合并2025 baseline + 2026已出报告的月度KPI趋势"""
        year = year or self.config.get("kpi_year", 2026)
        baseline_monthly = self.baseline.get("monthly_issues", {})

        # 加载2025全年CSV计算每月客户密度
        baseline_density = self._calc_baseline_monthly_density()

        trend = []
        for m in range(1, 13):
            entry = {
                "month": m,
                "baseline_issues": baseline_monthly.get(str(m), 0),
                "baseline_density": baseline_density.get(m, 0),
                "current_issues": 0,
                "current_density": 0,
                "yoy_change": None,
                "target": self.target,
            }

            # 尝试从已生成月报读取2026数据
            current_kpi = self._load_monthly_kpi(year, m)
            if current_kpi:
                entry["current_issues"] = current_kpi.get("total_issues", 0)
                entry["current_density"] = current_kpi.get("per_customer", 0)
                if entry["baseline_density"] > 0:
                    change = (entry["current_density"] - entry["baseline_density"]) / entry["baseline_density"] * 100
                    entry["yoy_change"] = round(change, 1)

            trend.append(entry)
        return trend

    def _calc_baseline_monthly_density(self) -> Dict[int, float]:
        """从2025全年CSV计算每月客户密度"""
        csv_path = self._find_baseline_csv()
        if not csv_path:
            return {}

        try:
            df = pd.read_csv(csv_path)
            df.columns = [c.strip() for c in df.columns]
            df["创建日期"] = pd.to_datetime(df["创建日期"], errors="coerce")
            df = df.dropna(subset=["创建日期"])

            baseline_year = self.config.get("baseline_year", 2025)
            df = df[df["创建日期"].dt.year == baseline_year]

            cust_col = self.customer_field
            if cust_col not in df.columns:
                return {}

            result = {}
            for m in range(1, 13):
                month_df = df[df["创建日期"].dt.month == m]
                if month_df.empty:
                    result[m] = 0
                    continue
                unique = month_df[cust_col].fillna("未知").nunique()
                result[m] = round(len(month_df) / unique, 2) if unique > 0 else 0
            return result
        except Exception as e:
            print(f"[KPI] 计算baseline月度密度失败: {e}")
            return {}

    def _find_baseline_csv(self) -> Optional[str]:
        """查找2025全年CSV"""
        if not os.path.exists(SRC_DIR):
            return None
        baseline_year = str(self.config.get("baseline_year", 2025))
        files = [f for f in os.listdir(SRC_DIR)
                 if f.endswith(".csv") and baseline_year in f and "完成" in f]
        if not files:
            return None
        return os.path.join(SRC_DIR, sorted(files)[0])

    def _load_monthly_kpi(self, year: int, month: int) -> Optional[dict]:
        """从已生成月报JSON加载KPI数据"""
        if not os.path.exists(MONTHLY_REPORT_DIR):
            return None

        for fname in os.listdir(MONTHLY_REPORT_DIR):
            if fname.startswith('_'):
                continue
            if not fname.endswith(".json"):
                continue
            pattern = f"{year}{month:02d}"
            if pattern in fname:
                try:
                    with open(os.path.join(MONTHLY_REPORT_DIR, fname), "r", encoding="utf-8") as f:
                        data = json.load(f)
                    return data.get("kpi_analysis", {}).get("current")
                except Exception as e:
                    print(f"[KPI] 读取月报KPI失败 {fname}: {e}")
                    continue
        return None

    # ------------------------------------------------------------------
    # YTD 累计进度
    # ------------------------------------------------------------------

    def get_ytd_progress(self, year: int, month: int) -> dict:
        """计算年初至今的累计KPI进度"""
        baseline_monthly = self.baseline.get("monthly_issues", {})
        baseline_density = self._calc_baseline_monthly_density()

        ytd_baseline_issues = sum(baseline_monthly.get(str(m), 0) for m in range(1, month + 1))
        ytd_baseline_density_avg = 0
        densities = [baseline_density.get(m, 0) for m in range(1, month + 1) if baseline_density.get(m, 0) > 0]
        if densities:
            ytd_baseline_density_avg = round(sum(densities) / len(densities), 2)

        # 从已生成月报累计2026数据
        ytd_current_issues = 0
        ytd_current_densities = []
        months_with_data = 0
        for m in range(1, month + 1):
            kpi = self._load_monthly_kpi(year, m)
            if kpi:
                ytd_current_issues += kpi.get("total_issues", 0)
                if kpi.get("per_customer", 0) > 0:
                    ytd_current_densities.append(kpi["per_customer"])
                    months_with_data += 1

        # YTD密度 = 各月密度的加权平均 (用月均密度近似, 跨月客户无法精确去重)
        ytd_per_customer = round(sum(ytd_current_densities) / len(ytd_current_densities), 2) if ytd_current_densities else 0
        yoy_change = None

        if ytd_baseline_density_avg > 0 and ytd_per_customer > 0:
            yoy_change = round((ytd_per_customer - ytd_baseline_density_avg) / ytd_baseline_density_avg * 100, 1)

        return {
            "ytd_issues": ytd_current_issues,
            "ytd_baseline_issues": ytd_baseline_issues,
            "ytd_baseline_density": ytd_baseline_density_avg,
            "ytd_current_density": ytd_per_customer,
            "yoy_change": yoy_change,
            "target": self.target,
            "months_covered": month,
        }

    # ------------------------------------------------------------------
    # 预测 / 风险警报
    # ------------------------------------------------------------------

    @staticmethod
    def _holt_linear(series: List[float], alpha: float = 0.3, beta: float = 0.1, h: int = 8) -> List[float]:
        """
        Holt双指数平滑（线性趋势，无季节性）。
        L[t] = α·y[t] + (1-α)·(L[t-1]+T[t-1])
        T[t] = β·(L[t]-L[t-1]) + (1-β)·T[t-1]
        forecast[t+h] = L[t] + h·T[t]

        n<2 → naive；n==2 → 线性漂移退化；n≥3 → 完整Holt。
        """
        n = len(series)
        if n < 1:
            return [0.0] * h
        if n == 1:
            return [series[0]] * h
        if n == 2:
            drift = series[-1] - series[0]
            return [series[-1] + (i + 1) * drift for i in range(h)]
        # n >= 3: 完整Holt
        L = series[0]
        T = series[1] - series[0]
        for y in series[1:]:
            L_prev, T_prev = L, T
            L = alpha * y + (1 - alpha) * (L_prev + T_prev)
            T = beta * (L - L_prev) + (1 - beta) * T_prev
        return [L + (i + 1) * T for i in range(h)]

    def _baseline_ytd_at_week(self, week_of_year: int) -> float:
        """
        估算2025在第week_of_year周时的YTD客户密度基线。
        工单数：月度累计插值；客户数：假设在年内均匀积累（近似）。
        """
        baseline_monthly = self.baseline.get("monthly_issues", {})
        baseline_total_customers = self.baseline.get("unique_customers", 1)
        month_float = min(week_of_year / 4.33, 12.0)
        full_months = int(month_float)
        partial = month_float - full_months

        cumulative_issues = sum(baseline_monthly.get(str(m), 0) for m in range(1, full_months + 1))
        if full_months < 12:
            cumulative_issues += baseline_monthly.get(str(full_months + 1), 0) * partial

        # 客户数按年内均匀积累估算：避免与工单数等比例导致的退化
        est_customers = baseline_total_customers * (month_float / 12.0)
        return round(cumulative_issues / max(est_customers, 1), 3)

    def forecast_weekly_ytd(self, trend_data: List[dict], current_ytd: dict, weeks_ahead: int = 8) -> List[dict]:
        """
        基于最近N周YTD趋势，用Holt平滑预测未来weeks_ahead周的YTD指标。

        算法：对每周新增工单/客户增量序列做Holt双指数平滑，累加回YTD。
        返回每周预测结果，含与2025同期YTD的同比%。
        """
        if len(trend_data) < 2 or not current_ytd.get("total_issues"):
            return []

        # 提取增量序列（相邻周YTD之差）
        issue_series = [t.get("total_issues", 0) for t in trend_data]
        cust_series = [t.get("unique_customers", 0) for t in trend_data]
        issue_deltas = [max(issue_series[i] - issue_series[i - 1], 0) for i in range(1, len(issue_series))]
        cust_deltas = [max(cust_series[i] - cust_series[i - 1], 0) for i in range(1, len(cust_series))]

        if not issue_deltas:
            return []

        # Holt平滑预测未来增量
        forecast_issue_d = self._holt_linear(issue_deltas, h=weeks_ahead)
        forecast_cust_d = self._holt_linear(cust_deltas, h=weeks_ahead)

        # 推算当前周序号（粗略：年初到最近一周）
        latest_period = trend_data[-1].get("period", "")
        try:
            from datetime import datetime, date
            # period格式如 "2026-03-30 至 2026-04-05"
            end_str = latest_period.split(" 至 ")[-1].strip() if " 至 " in latest_period else latest_period[:10]
            end_date = datetime.strptime(end_str[:10], "%Y-%m-%d").date()
            week_of_year = end_date.timetuple().tm_yday // 7
        except Exception:
            week_of_year = 14  # fallback

        base_issues = current_ytd.get("total_issues", issue_series[-1])
        base_customers = current_ytd.get("unique_customers", cust_series[-1])

        forecasts = []
        cumulative_issues = base_issues
        cumulative_customers = base_customers
        for i in range(weeks_ahead):
            cumulative_issues += max(forecast_issue_d[i], 0)
            cumulative_customers += max(forecast_cust_d[i], 0)
            proj_pc = round(cumulative_issues / max(cumulative_customers, 1), 3)
            future_week = week_of_year + i + 1
            baseline_ytd = self._baseline_ytd_at_week(future_week)
            yoy_pct = round((proj_pc - baseline_ytd) / max(baseline_ytd, 0.01) * 100, 1) if baseline_ytd > 0 else None
            # 生成周期标签
            try:
                from datetime import timedelta
                week_start = end_date + timedelta(weeks=i + 1) - timedelta(days=end_date.weekday())
                week_end = week_start + timedelta(days=6)
                period_label = f"{week_start.strftime('%m-%d')}~{week_end.strftime('%m-%d')}"
            except Exception:
                period_label = f"第{future_week}周"

            forecasts.append({
                "period": period_label,
                "projected_ytd_issues": int(cumulative_issues),
                "projected_ytd_customers": int(cumulative_customers),
                "projected_per_customer": proj_pc,
                "baseline_ytd_density": baseline_ytd,
                "yoy_pct": yoy_pct,
                "is_forecast": True,
            })
        return forecasts

    def forecast_monthly_remaining(self, monthly_trend: List[dict], year: int) -> dict:
        """
        基于已完成月份的YoY比率，预测剩余月份的KPI数据。
        采用YoY比例法：预测密度 = 2025基线密度 × 平均YoY比率。
        """
        baseline_density = self._calc_baseline_monthly_density()
        baseline_monthly = self.baseline.get("monthly_issues", {})

        # 从已完成月份提取YoY比率
        completed = [m for m in monthly_trend if m.get("current_density", 0) > 0 and m.get("baseline_density", 0) > 0]
        if not completed:
            return {"forecasts": [], "year_end_projection": {}}

        yoy_ratios = [m["current_density"] / m["baseline_density"] for m in completed]
        avg_ratio = sum(yoy_ratios) / len(yoy_ratios)

        forecasts = []
        for m in monthly_trend:
            month = m.get("month")
            if m.get("current_density", 0) > 0 or not month:
                continue
            b_density = baseline_density.get(month, 0)
            b_issues = baseline_monthly.get(str(month), 0)
            if b_density <= 0:
                continue
            pred_density = round(b_density * avg_ratio, 2)
            pred_issues = round(b_issues * avg_ratio)
            forecasts.append({
                "month": month,
                "predicted_density": pred_density,
                "predicted_issues": pred_issues,
                "baseline_density": b_density,
                "baseline_issues": b_issues,
                "yoy_pct": round((avg_ratio - 1) * 100, 1),
                "is_forecast": True,
            })

        # 年末全年预期：已完成月份 + 预测月份
        all_densities = [m["current_density"] for m in completed]
        all_densities += [f["predicted_density"] for f in forecasts]
        year_end_density = round(sum(all_densities) / max(len(all_densities), 1), 2) if all_densities else 0

        actual_issues = sum(m.get("current_issues", 0) for m in monthly_trend if m.get("current_issues", 0) > 0)
        forecast_issues = sum(f["predicted_issues"] for f in forecasts)
        year_end_total = actual_issues + forecast_issues
        vs_target = round(year_end_density - self.target, 2)

        return {
            "forecasts": forecasts,
            "year_end_projection": {
                "per_customer": year_end_density,
                "total_issues": year_end_total,
                "vs_target": vs_target,
                "will_meet_target": year_end_density <= self.target,
                "yoy_ratio": round(avg_ratio, 3),
            },
        }

    def _generate_risk_alert(self, forecast_data: dict, report_type: str) -> str:
        """
        生成风险警报Markdown块。
        周报：基于8周后YTD预测值；月报：基于年末全年预期密度。
        """
        if not forecast_data:
            return ""

        if report_type == "weekly":
            forecasts = forecast_data if isinstance(forecast_data, list) else []
            if not forecasts:
                return ""
            last_fc = forecasts[-1]
            projected = last_fc.get("projected_per_customer", 0)
            week_label = last_fc.get("period", "8周后")
            yoy = last_fc.get("yoy_pct")
            yoy_str = f"，同比 {yoy:+.1f}%" if yoy is not None else ""
        else:
            proj = forecast_data.get("year_end_projection", {})
            projected = proj.get("per_customer", 0)
            week_label = "年末全年"
            yoy_ratio = proj.get("yoy_ratio", 1)
            yoy_str = f"，较2025同比 {(yoy_ratio-1)*100:+.1f}%"

        if projected <= 0:
            return ""

        t = self.target
        if projected > t * 1.1:
            icon, level = "🔴", f"高风险：预计{week_label}密度 **{projected:.2f}**（超目标 {projected-t:+.2f}{yoy_str}）"
        elif projected > t:
            icon, level = "🟡", f"中风险：预计{week_label}密度 **{projected:.2f}**（略超目标 {projected-t:+.2f}{yoy_str}）"
        else:
            gap = t - projected
            icon, level = "✅", f"达标预测：预计{week_label}密度 **{projected:.2f}**（低于目标 {gap:.2f}{yoy_str}）"

        # 趋势恶化检测（仅对有序列的情况）
        worsening = ""
        if report_type == "weekly" and isinstance(forecast_data, list) and len(forecast_data) >= 3:
            yoy_vals = [f.get("yoy_pct") for f in forecast_data[-3:] if f.get("yoy_pct") is not None]
            if len(yoy_vals) == 3 and yoy_vals[0] < yoy_vals[1] < yoy_vals[2]:
                worsening = "\n> 🟢 **趋势预警**：同比差距连续3周扩大，建议关注增速变化。"

        return f"\n> {icon} **{level}**{worsening}\n\n"

    # ------------------------------------------------------------------
    # Markdown 报告生成
    # ------------------------------------------------------------------

    def generate_kpi_section_md(self, kpi_data: dict, report_type: str = "weekly") -> str:
        """生成KPI分析章节的Markdown内容

        Args:
            kpi_data: 包含 current, last_year, last_period, yoy, mom,
                      non_compliant, distribution, trend 等字段
            report_type: "weekly" 或 "monthly"
        """
        content = "## 2. KPI 达标分析\n\n"

        # -- KPI概览表 --
        current = kpi_data.get("current", {})
        yoy = kpi_data.get("yoy", {})
        mom = kpi_data.get("mom", {})

        period_label = "本周" if report_type == "weekly" else "本月"
        last_label = "去年同周" if report_type == "weekly" else "2025同月"
        prev_label = "上周" if report_type == "weekly" else "上月"

        content += "### 每客户问题数\n\n"
        content += f"| 指标 | {last_label} | {prev_label} | {period_label} | 环比 | 同比 | 年度目标 | 差距 |\n"
        content += "|------|---------|------|------|------|------|---------|------|\n"

        ly_val = yoy.get("last_year", "-")
        ly_str = f"{ly_val:.2f}" if isinstance(ly_val, (int, float)) and ly_val > 0 else "-"

        prev_val = mom.get("last_period", "-")
        prev_str = f"{prev_val:.2f}" if isinstance(prev_val, (int, float)) and prev_val > 0 else "-"

        cur_val = current.get("per_customer", 0)
        cur_str = f"{cur_val:.2f}" if cur_val > 0 else "-"

        mom_str = f"{mom.get('arrow', '→')}{abs(mom.get('change_pct', 0)):.1f}%" if mom.get("change_pct") is not None else "-"
        yoy_str = f"{yoy.get('arrow', '→')}{abs(yoy.get('change_pct', 0)):.1f}%" if yoy.get("change_pct") is not None else "-"

        gap = yoy.get("gap", cur_val - self.target if cur_val > 0 else 0)
        gap_str = f"{gap:+.2f}" if isinstance(gap, (int, float)) else "-"
        status = "达标" if gap <= 0 else "**未达标**"

        content += f"| 每客户问题数 | {ly_str} | {prev_str} | {cur_str} | {mom_str} | {yoy_str} | ≤{self.target} | {gap_str} {status} |\n\n"

        # 摘要
        content += f"> **本期**: {current.get('total_issues', 0)}条工单, "
        content += f"{current.get('unique_customers', 0)}家客户, "
        content += f"每客户{cur_str}条\n\n"

        # -- 趋势表 --
        trend = kpi_data.get("trend", [])
        forecast = kpi_data.get("forecast", [])  # 预测数据（周报:list，月报:dict）

        if trend and report_type == "weekly":
            content += "### 周KPI滚动趋势（YTD 年初至今累计）\n\n"
            content += "| 周期 | YTD工单数 | YTD客户数 | YTD每客户问题数 | 当周同比 | 目标 | 状态 |\n"
            content += "|------|-----------|-----------|----------------|---------|------|------|\n"
            for t in trend:
                pc = t["per_customer"]
                row_status = "✓ 达标" if pc <= self.target else "✗ 未达标"
                ytd_mark = "" if t.get("is_ytd", True) else "※"
                content += f"| {t['period']} | {t.get('total_issues', '-')} | {t.get('unique_customers', '-')} | **{pc:.2f}**{ytd_mark} | {t.get('yoy_change', '-')} | ≤{self.target} | {row_status} |\n"
            # 追加预测行
            if forecast and isinstance(forecast, list):
                for fc in forecast:
                    pc = fc["projected_per_customer"]
                    yoy_str = f"{fc['yoy_pct']:+.1f}%" if fc.get("yoy_pct") is not None else "-"
                    content += f"| *📈 {fc['period']}* | *~{fc['projected_ytd_issues']}* | *~{fc['projected_ytd_customers']}* | ***{pc:.2f}*** | *{yoy_str}* | ≤{self.target} | 📈预测 |\n"
            content += "\n> ※ 标注表示该周无YTD数据，显示为当周值。📈预测行为Holt趋势平滑外推，仅供参考。\n"
            # 风险警报
            if forecast:
                content += self._generate_risk_alert(forecast, "weekly")
            content += "\n"

        elif trend and report_type == "monthly":
            content += "### 月度KPI趋势 (2025 vs 2026)\n\n"
            content += "| 月份 | 2025工单 | 2025密度 | 2026工单 | 2026密度 | 同比变化 | 目标 |\n"
            content += "|------|---------|---------|---------|---------|---------|------|\n"
            for t in trend:
                if t.get("baseline_issues", 0) == 0 and t.get("current_issues", 0) == 0:
                    continue
                yoy_val = t.get("yoy_change")
                yoy_cell = f"{yoy_val:+.1f}%" if yoy_val is not None else "-"
                cur_density = f"{t['current_density']:.2f}" if t.get("current_density", 0) > 0 else "-"
                bl_density = f"{t['baseline_density']:.2f}" if t.get("baseline_density", 0) > 0 else "-"
                cur_issues = t.get("current_issues", 0) or "-"
                content += f"| {t['month']}月 | {t['baseline_issues']} | {bl_density} | {cur_issues} | {cur_density} | {yoy_cell} | ≤{self.target} |\n"
            # 追加预测月份行
            monthly_forecasts = forecast.get("forecasts", []) if isinstance(forecast, dict) else []
            for fc in monthly_forecasts:
                bl_d = f"{fc['baseline_density']:.2f}" if fc.get("baseline_density", 0) > 0 else "-"
                content += f"| *📈 {fc['month']}月(预测)* | {fc['baseline_issues']} | {bl_d} | *~{fc['predicted_issues']}* | *{fc['predicted_density']:.2f}* | *{fc['yoy_pct']:+.1f}%* | ≤{self.target} |\n"
            content += "\n> 📈预测行基于已完成月份的YoY比率推算，仅供参考。\n"
            # 年末预期摘要
            year_end = forecast.get("year_end_projection", {}) if isinstance(forecast, dict) else {}
            if year_end:
                yep = year_end["per_customer"]
                meet_icon = "✅" if year_end.get("will_meet_target") else "⚠️"
                meet_text = "达标" if year_end.get("will_meet_target") else "超目标"
                yoy_ratio = year_end.get("yoy_ratio", 1)
                content += f"\n> **年末预期**：全年工单 ~{year_end.get('total_issues', '-')}条，年度平均密度 ~{yep}，预计 {meet_icon} {meet_text}（较2025同比 {(yoy_ratio-1)*100:+.1f}%，距目标 {year_end['vs_target']:+.2f}）\n"
            # 风险警报
            if forecast and isinstance(forecast, dict):
                content += self._generate_risk_alert(forecast, "monthly")
            content += "\n"

        # -- YTD (月报) --
        ytd = kpi_data.get("ytd")
        if ytd and report_type == "monthly":
            content += "### YTD(年初至今)累计\n\n"
            content += "| 累计工单 | 2025同期工单 | 2025同期密度 | 年度目标 |\n"
            content += "|---------|-----------|-----------|--------|\n"
            content += f"| {ytd.get('ytd_issues', '-')} | {ytd.get('ytd_baseline_issues', '-')} | {ytd.get('ytd_baseline_density', '-')} | ≤{self.target} |\n\n"

        # -- 客户分布区间 --
        distribution = kpi_data.get("distribution", [])
        if distribution:
            content += "### 客户分布\n\n"
            content += "| 区间 | 客户数 | 客户占比 | 工单数 | 工单占比 |\n"
            content += "|------|--------|---------|--------|--------|\n"
            for d in distribution:
                content += f"| {d['band']} | {d['customer_count']} | {d['customer_pct']:.1f}% | {d['issue_count']} | {d['issue_pct']:.1f}% |\n"
            content += "\n"

        # -- 不达标客户清单 --
        non_compliant = kpi_data.get("non_compliant", [])
        if non_compliant:
            threshold_label = f"本{period_label[1]}工单数 > {kpi_data.get('threshold', self.weekly_threshold)}"
            content += f"### 不达标客户清单 ({threshold_label})\n\n"
            content += f"| 排名 | 客户 | {period_label}工单数 | TOP问题类型 |\n"
            content += "|------|------|-----------|----------|\n"
            for i, nc in enumerate(non_compliant[:20], 1):
                content += f"| {i} | {nc['customer']} | {nc['issue_count']} | {nc.get('top_issue_types', '-')} |\n"
            if len(non_compliant) > 20:
                content += f"\n*...共 {len(non_compliant)} 家不达标客户，仅显示前20家*\n"
            content += "\n"

            # 不达标客户问题明细 (前5家)
            content += "### 不达标客户问题明细 (TOP5)\n\n"
            for nc in non_compliant[:5]:
                content += f"**{nc['customer']}** ({nc['issue_count']}条)\n\n"
                if nc.get("issues"):
                    content += "| 工单号 | 问题描述 | 问题类型 |\n"
                    content += "|--------|---------|--------|\n"
                    for iss in nc["issues"]:
                        content += f"| {iss.get('key', '-')} | {iss.get('summary', '-')} | {iss.get('type', '-')} |\n"
                    content += "\n"

        return content

    # ------------------------------------------------------------------
    # 从CSV加载去年同周数据
    # ------------------------------------------------------------------

    def load_last_year_same_week(self, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """加载去年同一周的数据 (用于周报KPI对比)"""
        csv_path = self._find_baseline_csv()
        if not csv_path:
            return None

        try:
            df = pd.read_csv(csv_path)
            df.columns = [c.strip() for c in df.columns]
            df["创建日期"] = pd.to_datetime(df["创建日期"], errors="coerce")
            df = df.dropna(subset=["创建日期"])

            # 计算去年同周范围
            start = pd.to_datetime(start_date) - pd.DateOffset(years=1)
            end = pd.to_datetime(end_date) - pd.DateOffset(years=1)

            mask = (df["创建日期"] >= start) & (df["创建日期"] <= end)
            result = df[mask]
            return result if not result.empty else None
        except Exception as e:
            print(f"[KPI] 加载去年同周数据失败: {e}")
            return None

    def load_last_year_same_month(self, year: int, month: int) -> Optional[pd.DataFrame]:
        """加载去年同月数据 (用于月报KPI对比)"""
        csv_path = self._find_baseline_csv()
        if not csv_path:
            return None

        try:
            df = pd.read_csv(csv_path)
            df.columns = [c.strip() for c in df.columns]
            df["创建日期"] = pd.to_datetime(df["创建日期"], errors="coerce")
            df = df.dropna(subset=["创建日期"])

            last_year = year - 1
            mask = (df["创建日期"].dt.year == last_year) & (df["创建日期"].dt.month == month)
            result = df[mask]
            return result if not result.empty else None
        except Exception as e:
            print(f"[KPI] 加载去年同月数据失败: {e}")
            return None
