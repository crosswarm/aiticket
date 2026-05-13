import { chromium } from 'playwright';
import path from 'path';

console.log('🔍 启动打印效果验证...\n');

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
    viewport: { width: 1400, height: 900 }
});
const page = await context.newPage();

try {
    // 1. 打开报告页面
    console.log('📄 打开 http://localhost:3000/report.html');
    await page.goto('http://localhost:3000/report.html', {
        waitUntil: 'networkidle',
        timeout: 30000 
    });
    await page.waitForTimeout(2000);
    
    // 2. 点击第一个报告
    console.log('📋 加载第一个报告...');
    const reportBtn = await page.locator('#reportObjList button').first();
    if (await reportBtn.count() === 0) {
        console.log('❌ 没有找到报告按钮');
        await browser.close();
        process.exit(1);
    }
    await reportBtn.click();
    await page.waitForTimeout(3000);
    
    // 3. 截取正常显示效果
    const normalScreenshot = '/tmp/report_normal.png';
    await page.screenshot({ path: normalScreenshot, fullPage: false });
    console.log(`✅ 正常显示截图: ${normalScreenshot}`);
    
    // 4. 模拟打印媒体查询效果
    console.log('\n🖨️  模拟打印模式...');
    await page.emulateMedia({ media: 'print' });
    await page.waitForTimeout(1000);
    
    // 5. 截取打印效果
    const printScreenshot = '/tmp/report_print.png';
    await page.screenshot({ path: printScreenshot, fullPage: true });
    console.log(`✅ 打印模式截图: ${printScreenshot}`);
    
    // 6. 检查打印样式
    const printStyles = await page.evaluate(() => {
        const styles = [];
        for (const sheet of document.styleSheets) {
            try {
                for (const rule of sheet.cssRules || []) {
                    if (rule.cssText && rule.cssText.includes('@media print')) {
                        styles.push(rule.cssText.substring(0, 800));
                    }
                }
            } catch (e) {}
        }
        return styles;
    });
    
    console.log('\n📋 检测到的打印样式:');
    if (printStyles.length > 0) {
        printStyles.forEach((style, i) => {
            console.log(`\n--- 样式 ${i + 1} ---`);
            console.log(style);
        });
    } else {
        console.log('⚠️  未检测到 @media print 样式');
    }
    
    // 7. 检查是否有 Paged.js
    const hasPagedJs = await page.evaluate(() => {
        return typeof window.PagedPolyfill !== 'undefined' || 
               document.querySelector('script[src*="pagedjs"]') !== null;
    });
    
    console.log(`\n🔍 Paged.js 检测: ${hasPagedJs ? '❌ 存在 (会干扰)' : '✅ 不存在 (符合预期)'}`);
    
    // 8. 检查页码元素
    const hasPageNumbers = await page.evaluate(() => {
        const elements = document.querySelectorAll('*');
        for (const el of elements) {
            const text = el.textContent || '';
            if (/\d+\s*\/\s*\d+/.test(text) && el.children.length === 0) {
                return true;
            }
        }
        return false;
    });
    
    console.log(`🔍 页码检测: ${hasPageNumbers ? '❌ 发现页码' : '✅ 无页码 (符合预期)'}`);
    
    await browser.close();
    
    console.log('\n✅ 验证完成！');
    console.log('\n请查看截图:');
    console.log('  - 正常显示: /tmp/report_normal.png');
    console.log('  - 打印模式: /tmp/report_print.png');
    
} catch (error) {
    console.error('❌ 验证失败:', error.message);
    await browser.close();
    process.exit(1);
}
