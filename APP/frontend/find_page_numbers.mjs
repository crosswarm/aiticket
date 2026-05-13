import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext();
const page = await context.newPage();

await page.goto('http://localhost:3000/report.html', { waitUntil: 'networkidle' });
await page.waitForTimeout(2000);
await page.locator('#reportObjList button').first().click();
await page.waitForTimeout(3000);

// 查找所有包含数字/数字模式的元素
const pageNumberElements = await page.evaluate(() => {
    const results = [];
    const elements = document.querySelectorAll('*');
    for (const el of elements) {
        const text = el.textContent?.trim() || '';
        // 匹配 "数字/数字" 或 "数字 / 数字" 模式
        if (/^\d+\s*\/\s*\d+$/.test(text)) {
            results.push({
                text: text,
                tag: el.tagName,
                class: el.className,
                id: el.id,
                parent: el.parentElement?.tagName + (el.parentElement?.className ? '.' + el.parentElement?.className : '')
            });
        }
    }
    return results;
});

console.log('发现的页码元素:');
console.log(JSON.stringify(pageNumberElements, null, 2));

await browser.close();
