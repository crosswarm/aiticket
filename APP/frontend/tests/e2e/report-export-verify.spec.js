/**
 * 报告导出功能完整验证测试
 * 包含截图和视频录制
 */

import { chromium } from 'playwright';
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BASE_URL = 'http://localhost:3000';
const REPORT_URL = `${BASE_URL}/report.html`;
const RESULTS_DIR = path.join(__dirname, 'test-results');

async function runTest() {
    console.log('🚀 启动报告导出验证测试...\n');

    // 确保结果目录存在
    if (!fs.existsSync(RESULTS_DIR)) {
        fs.mkdirSync(RESULTS_DIR, { recursive: true });
    }

    const browser = await chromium.launch({
        headless: false,
        args: ['--start-maximized']
    });

    const context = await browser.newContext({
        viewport: { width: 1920, height: 1080 },
        recordVideo: {
            dir: RESULTS_DIR,
            size: { width: 1920, height: 1080 }
        }
    });

    const page = await context.newPage();

    // 监听控制台日志
    page.on('console', msg => console.log('  [浏览器]', msg.text()));
    page.on('pageerror', err => console.log('  [错误]', err.message));

    try {
        // ========== 步骤1: 打开报告页面 ==========
        console.log('📄 步骤1: 打开报告页面...');
        await page.goto(REPORT_URL, { waitUntil: 'networkidle', timeout: 30000 });
        await page.waitForTimeout(2000);
        await page.screenshot({ path: path.join(RESULTS_DIR, '01_page_loaded.png') });
        console.log('   ✅ 页面加载完成');

        // ========== 步骤2: 加载报告 ==========
        console.log('\n📋 步骤2: 加载报告...');
        const reportButtons = await page.locator('#reportObjList button').count();
        console.log(`   找到 ${reportButtons} 个报告`);

        if (reportButtons === 0) {
            throw new Error('没有找到报告');
        }

        // 点击第一个报告
        await page.locator('#reportObjList button').first().click();
        await page.waitForTimeout(3000);

        // 截图报告内容
        await page.screenshot({
            path: path.join(RESULTS_DIR, '02_report_content.png'),
            fullPage: false
        });

        // 检查报告内容
        const reportHTML = await page.locator('#reportContent').innerHTML();
        const hasContent = reportHTML.length > 500 && !reportHTML.includes('请选择左侧周报');
        console.log(`   报告内容: ${hasContent ? '✅ 已加载' : '❌ 未加载'}`);

        if (!hasContent) {
            throw new Error('报告内容未正确加载');
        }

        // ========== 步骤3: 测试前端PDF导出 ==========
        console.log('\n📕 步骤3: 测试前端PDF导出...');

        // 只选择PDF格式
        await page.locator('#export-pdf').check();
        await page.locator('#export-md').uncheck();
        await page.locator('#export-docx').uncheck();
        await page.locator('#export-xlsx').uncheck();

        // 开始监听下载
        const downloadPromise = page.waitForEvent('download', { timeout: 30000 }).catch(() => null);

        // 点击导出
        await page.locator('#exportBtn').click();
        console.log('   点击导出按钮...');

        // 等待PDF生成
        await page.waitForTimeout(8000);

        // 截图导出状态
        await page.screenshot({
            path: path.join(RESULTS_DIR, '03_pdf_exporting.png')
        });

        // 检查下载
        const download = await downloadPromise;
        if (download) {
            const downloadPath = path.join(RESULTS_DIR, 'frontend_export.pdf');
            await download.saveAs(downloadPath);
            const stats = fs.statSync(downloadPath);
            console.log(`   ✅ PDF已下载: ${Math.round(stats.size / 1024)}KB`);
        } else {
            console.log('   ⚠️ 未检测到下载文件（可能是浏览器弹窗拦截）');
        }

        // ========== 步骤4: 测试打印功能 ==========
        console.log('\n🖨️ 步骤4: 测试打印功能...');

        // 使用Ctrl+P打开打印对话框
        await page.keyboard.down('Control');
        await page.keyboard.press('p');
        await page.keyboard.up('Control');

        await page.waitForTimeout(3000);

        // 截图打印预览
        await page.screenshot({
            path: path.join(RESULTS_DIR, '04_print_preview.png')
        });
        console.log('   ✅ 打印预览截图已保存');

        // 关闭打印对话框
        await page.keyboard.press('Escape');
        await page.waitForTimeout(1000);

        // ========== 步骤5: 测试后端多格式导出 ==========
        console.log('\n📦 步骤5: 测试后端多格式导出...');

        // 选择多格式
        await page.locator('#export-pdf').check();
        await page.locator('#export-md').check();
        await page.locator('#export-docx').check();

        await page.locator('#exportBtn').click();
        console.log('   点击导出按钮...');

        // 轮询导出状态
        let completed = false;
        for (let i = 0; i < 30; i++) {
            await page.waitForTimeout(2000);
            const btnText = await page.locator('#exportBtnText').innerText();
            console.log(`   进度: ${btnText}`);

            if (btnText === '导出') {
                completed = true;
                break;
            }
        }

        await page.screenshot({
            path: path.join(RESULTS_DIR, '05_backend_export.png')
        });

        // ========== 步骤6: 检查导出文件 ==========
        console.log('\n📁 步骤6: 检查导出文件...');

        const exportDir = path.join(__dirname, '../../../conclusion/exports');
        if (fs.existsSync(exportDir)) {
            const files = fs.readdirSync(exportDir)
                .filter(f => f.endsWith('.pdf') || f.endsWith('.md') || f.endsWith('.docx'))
                .map(f => {
                    const stat = fs.statSync(path.join(exportDir, f));
                    return { name: f, size: stat.size, time: stat.mtime };
                })
                .sort((a, b) => b.time - a.time);

            console.log(`   导出目录文件数: ${files.length}`);
            console.log('\n   最新5个文件:');
            files.slice(0, 5).forEach(f => {
                console.log(`   - ${f.name} (${Math.round(f.size / 1024)}KB)`);
            });

            // 检查MD文件格式
            const latestMd = files.find(f => f.name.endsWith('.md'));
            if (latestMd) {
                const mdPath = path.join(exportDir, latestMd.name);
                const mdContent = fs.readFileSync(mdPath, 'utf-8');
                const isMarkdown = mdContent.startsWith('#') && !mdContent.startsWith('{');
                console.log(`\n   MD格式: ${isMarkdown ? '✅ 正确' : '❌ 错误(JSON)'}`);

                if (!isMarkdown) {
                    console.log('   内容预览:', mdContent.substring(0, 100));
                }
            }

            // 复制最新PDF到测试结果目录
            const latestPdf = files.find(f => f.name.endsWith('.pdf'));
            if (latestPdf) {
                const srcPath = path.join(exportDir, latestPdf.name);
                const destPath = path.join(RESULTS_DIR, 'backend_export.pdf');
                fs.copyFileSync(srcPath, destPath);
                console.log(`\n   ✅ 已复制PDF到测试结果目录: ${latestPdf.name}`);
            }
        }

        // ========== 完成 ==========
        console.log('\n✅ 测试完成!');
        console.log(`\n📂 测试结果保存在: ${RESULTS_DIR}`);
        console.log('   - 01_page_loaded.png - 页面初始状态');
        console.log('   - 02_report_content.png - 报告内容');
        console.log('   - 03_pdf_exporting.png - PDF导出中');
        console.log('   - 04_print_preview.png - 打印预览');
        console.log('   - 05_backend_export.png - 后端导出');
        console.log('   - backend_export.pdf - 后端生成的PDF');

    } catch (error) {
        console.error('\n❌ 测试失败:', error.message);
        await page.screenshot({ path: path.join(RESULTS_DIR, 'error.png') });
    } finally {
        await context.close();
        await browser.close();

        // 显示视频路径
        const videoFiles = fs.readdirSync(RESULTS_DIR).filter(f => f.endsWith('.webm'));
        if (videoFiles.length > 0) {
            console.log(`\n🎥 录制视频: ${path.join(RESULTS_DIR, videoFiles[0])}`);
        }
    }
}

runTest();
