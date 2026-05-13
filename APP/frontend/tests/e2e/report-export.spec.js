/**
 * 报告导出功能完整测试
 * 测试PDF、MD、DOCX导出功能
 */

import { chromium } from 'playwright';
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BASE_URL = 'http://localhost:3000';
const REPORT_URL = `${BASE_URL}/report.html`;

async function testExportFunctions() {
    console.log('🚀 启动报告导出功能测试...\n');

    const browser = await chromium.launch({ headless: false });
    const context = await browser.newContext({
        viewport: { width: 1400, height: 900 }
    });
    const page = await context.newPage();

    // 监听控制台日志
    page.on('console', msg => {
        if (msg.text().includes('PDF') || msg.text().includes('export')) {
            console.log('  [浏览器日志]', msg.text());
        }
    });

    try {
        // 1. 打开报告页面
        console.log('📄 步骤1: 打开报告页面...');
        await page.goto(REPORT_URL, { waitUntil: 'networkidle' });
        await page.waitForTimeout(2000);

        // 2. 等待报告列表加载并点击第一个
        console.log('📋 步骤2: 加载报告...');
        const reportCount = await page.locator('#reportObjList button').count();
        console.log(`   找到 ${reportCount} 个报告`);

        if (reportCount === 0) {
            console.log('❌ 没有找到报告');
            return;
        }

        // 点击第一个报告
        await page.locator('#reportObjList button').first().click();
        await page.waitForTimeout(3000);

        // 检查报告内容
        const reportContent = await page.locator('#reportContent').innerHTML();
        const hasRealContent = reportContent.length > 500 && !reportContent.includes('请选择左侧周报');
        console.log(`   报告内容: ${hasRealContent ? '✅ 已加载' : '❌ 未加载'}`);

        if (!hasRealContent) {
            console.log('   内容预览:', reportContent.substring(0, 200));
            return;
        }

        // 截图报告内容
        await page.screenshot({ path: 'test-results/report_content.png', fullPage: false });
        console.log('   截图已保存: test-results/report_content.png');

        // 3. 测试PDF导出（前端html2pdf）
        console.log('\n📕 步骤3: 测试PDF导出（前端html2pdf）...');

        // 只选择PDF格式
        await page.locator('#export-pdf').check();
        await page.locator('#export-md').uncheck();
        await page.locator('#export-docx').uncheck();
        await page.locator('#export-xlsx').uncheck();

        // 点击导出按钮
        await page.locator('#exportBtn').click();

        // 等待导出完成
        console.log('   等待PDF生成...');
        await page.waitForTimeout(8000);

        // 检查是否有下载文件
        const downloadsDir = path.join(process.env.HOME || '/tmp', 'Downloads');
        const pdfFiles = fs.existsSync(downloadsDir)
            ? fs.readdirSync(downloadsDir).filter(f => f.endsWith('.pdf') && f.includes('周报'))
            : [];
        console.log(`   下载目录PDF文件: ${pdfFiles.length} 个`);

        // 4. 测试多格式导出（后端）
        console.log('\n📦 步骤4: 测试多格式导出（后端）...');

        await page.locator('#export-pdf').check();
        await page.locator('#export-md').check();
        await page.locator('#export-docx').check();

        await page.locator('#exportBtn').click();
        console.log('   等待后端处理...');

        // 轮询导出状态
        for (let i = 0; i < 30; i++) {
            await page.waitForTimeout(2000);
            const btnText = await page.locator('#exportBtnText').innerText();
            console.log(`   进度: ${btnText}`);

            if (btnText === '导出') {
                break;
            }
        }

        // 5. 检查导出文件
        console.log('\n📁 步骤5: 检查导出文件...');
        const exportDir = path.join(__dirname, '../../../conclusion/exports');
        if (fs.existsSync(exportDir)) {
            const files = fs.readdirSync(exportDir)
                .filter(f => f.endsWith('.pdf') || f.endsWith('.md') || f.endsWith('.docx'))
                .sort((a, b) => fs.statSync(path.join(exportDir, b)).mtimeMs - fs.statSync(path.join(exportDir, a)).mtimeMs);

            console.log(`   导出目录文件数: ${files.length}`);
            files.slice(0, 5).forEach(f => {
                const stat = fs.statSync(path.join(exportDir, f));
                console.log(`   - ${f} (${Math.round(stat.size/1024)}KB)`);
            });

            // 检查MD文件内容
            const latestMd = files.find(f => f.endsWith('.md'));
            if (latestMd) {
                const mdContent = fs.readFileSync(path.join(exportDir, latestMd), 'utf-8');
                const isMarkdown = mdContent.startsWith('#') && !mdContent.startsWith('{');
                console.log(`\n   MD文件格式: ${isMarkdown ? '✅ 正确(Markdown)' : '❌ 错误(JSON格式)'}`);
                if (!isMarkdown) {
                    console.log('   MD内容预览:', mdContent.substring(0, 100));
                }
            }

            // 检查PDF文件大小
            const latestPdf = files.find(f => f.endsWith('.pdf'));
            if (latestPdf) {
                const pdfSize = fs.statSync(path.join(exportDir, latestPdf)).size;
                console.log(`\n   PDF文件大小: ${Math.round(pdfSize/1024)}KB`);
                console.log(`   PDF状态: ${pdfSize > 10000 ? '✅ 有内容' : '⚠️ 可能为空'}`);
            }
        }

        // 6. 测试打印功能
        console.log('\n🖨️ 步骤6: 测试打印功能...');
        // 使用JavaScript调用打印预览
        await page.evaluate(() => {
            window.print();
        });
        await page.waitForTimeout(2000);
        await page.screenshot({ path: 'test-results/print_preview.png' });
        console.log('   打印预览截图已保存');

        console.log('\n✅ 测试完成!');

    } catch (error) {
        console.error('❌ 测试失败:', error.message);
        await page.screenshot({ path: 'test-results/error.png' });
    } finally {
        await browser.close();
    }
}

testExportFunctions();
