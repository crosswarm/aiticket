# HR 管理系统知识库

本目录存放人力资源管理系统相关政策和操作指南，供 AI 智能回复检索使用。

## 文档清单

| 文件 | 格式 | 内容 |
|------|------|------|
| 01_attendance_policy.txt | TXT | 考勤管理制度（迟到/早退/旷工/申诉规则）|
| 02_leave_application_guide.pdf | PDF | 请假申请流程（年假/病假/调休/各类假种）|
| 02_leave_application_guide.md  | MD  | 请假指南 Markdown 版（KB 直接索引）|
| 03_onboarding_checklist.docx | DOCX | 新员工入职手册（报到/社保/试用期/导师）|
| 04_salary_components.xlsx | XLSX | 薪酬构成说明（各项计算口径和发放频率）|

## 使用方式

将此目录的内容用于 KB 知识库索引时，运行：

```bash
curl -X POST http://localhost:18000/api/kb/sync
```

或使用导入脚本：

```bash
python3 scripts/import_kb.py
```

## 演示数据配套

与 `data/imports/demo_hr_tickets.xlsx`（16 条 HR 工单）配合使用，
每条工单的智能回复应能检索到本目录对应知识文档。
