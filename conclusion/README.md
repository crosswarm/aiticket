# 结论目录

此目录存放系统自动生成的周报、月报、分析报告等输出文件。

## 目录结构

```
conclusion/
├── WeeklyReports/   # 自动生成的周报
├── MonthlyReports/  # 自动生成的月报
└── exports/         # 导出数据
```

## 注意

此目录内容**不纳入 Git 版本控制**。
报告文件由 JobMaster 定时生成，存储在 `DATA_DIR` 配置的路径下。
