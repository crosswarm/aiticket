# SPA 导航策略

## React SPA 导航
- URL 不变时通过 Redux dispatch 导航：找到菜单项的 onClick handler，提取 appId/cloudId
- 使用 React Fiber 树遍历查找状态：`document.querySelector('[data-reactroot]')._reactRootContainer._internalRoot.current`
- 强制打开菜单面板：找到 `memoizedState` 中控制 left/display 的 state，dispatch 修改值
- 搜索栏辅助导航：`[placeholder="请输入应用/业务对象"]` 输入关键词，但注意下拉结果可能混入非目标项

## 菜单发现
- 全功能菜单面板通常有固定 class（如 `.xk-full-func-menu-model`），通过 CSS left 属性控制滑入/滑出
- 菜单面板 z-index 较高（>1000），可能被体验须知弹窗遮挡
- 先关闭所有弹窗再操作菜单：用 `style.display='none'` 而非 `.remove()`（防止 React 状态不一致）
- 滚动查找目标模块：菜单列表可能很长（25+ 个云模块），需要 scrollIntoView

## 弹窗处理
- 体验须知弹窗：点击"立即体验"按钮关闭，或直接隐藏
- 权限不足弹窗：截图记录，标注为限制项
- 确认弹窗：记录弹窗内容后关闭
- 不要 `.remove()` 任何弹窗元素，始终用 display 隐藏

## 页面切换后清理
- 每次导航后等待页面稳定（检查 loading 指示器消失）
- 清除上一页的残留弹窗
- 截图前确保无遮挡元素
- hard reload 可恢复被删除的面板/组件

## 失败恢复
- 菜单面板打不开：尝试 `window.location.reload(true)` 后重试
- 页面白屏：检查控制台错误，可能是权限问题
- 节点无法点击：对 SVG foreignObject 的父级 `g` 元素发送 pointerdown + click 事件
