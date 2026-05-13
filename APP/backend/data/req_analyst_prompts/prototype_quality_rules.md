# 原型质量规则

## 布局准确性
- 原型必须还原 findings 中描述的布局模式（三栏/左树右表/纯列表）
- 三栏布局：左侧工具箱 + 中间画布/内容区 + 右侧属性面板
- 左树右表：左侧导航树 + 右侧数据表格
- 工具栏位置必须在内容区上方，按钮顺序与 findings 一致

## 元素完整性
- findings 中列出的所有工具栏按钮必须在 HTML 中出现
- 表单字段名、类型、必填标记必须与 findings 记录一致
- Tab 页签数量和名称必须匹配
- 表格列头文字必须与 findings 中的列头列表一致

## 数据保真度
- 使用 findings 中的真实数据样本（如流程名称、编码、组织名）
- 下拉选项需包含 findings 中提到的可选值
- 数据量提示需与 findings 记录匹配（如"共51条，3页"）

## 交互覆盖
- 每个按钮必须有 click 事件处理
- Tab 切换必须有 JS 逻辑实现内容联动
- 拖拽功能必须实现 dragstart/dragover/drop 事件链
- 节点选中必须有视觉反馈（高亮/边框变色）

## 样式规范
- 必须使用 CSS 自定义属性（--primary, --text, --bg 等）
- 字体栈必须包含中文字体（Microsoft YaHei / PingFang SC）
- 使用企业级语义类名（prop-field, toolbar-btn, data-table）
- 禁止外部 CDN 引用，所有资源本地化

## 独立可运行
- index.html + page.css + page.js 三文件结构
- 双击 index.html 即可在浏览器打开
- 无外部依赖，无构建步骤
