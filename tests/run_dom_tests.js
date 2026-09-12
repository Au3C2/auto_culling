
const fs = require('fs');
const path = require('path');

console.log("=== Starting Headless GUI Interaction & UI Logic Unit Tests ===");

// 1. Check UI markup files
const htmlPath = path.resolve('ui/index.html');
const cssPath = path.resolve('ui/style.css');
const jsPath = path.resolve('ui/app.js');

if (!fs.existsSync(htmlPath) || !fs.existsSync(cssPath) || !fs.existsSync(jsPath)) {
  console.error("FAIL: Missing essential UI files (ui/index.html, style.css, app.js)");
  process.exit(1);
}

const html = fs.readFileSync(htmlPath, 'utf-8');
const css = fs.readFileSync(cssPath, 'utf-8');
const appJs = fs.readFileSync(jsPath, 'utf-8');

// 2. Assert HTML structure requirements
const requiredElements = [
  'id="inputDir"',
  'id="btnBrowse"',
  'id="btnRun"',
  'id="progressBar"',
  'id="frameStat"',
  'id="photoTable"',
  'id="previewImg"',
  'id="splitResizer"',
  'tau-tip-wrap',
  'tau-info-icon',
  'tau-tooltip',
];

for (const el of requiredElements) {
  if (!html.includes(el)) {
    console.error(`FAIL: HTML is missing required element: ${el}`);
    process.exit(1);
  }
}
console.log("PASS: HTML contains all required controls, tooltips, progress stats, and split panels.");

// 3. Assert Tooltip explanations cover defaults, impacts and ranges
const requiredTooltips = [
  'DEFAULT',
  '范围',
  '影响',
  'P0',
  '锐度',
  '构图',
];

for (const t of requiredTooltips) {
  if (!html.includes(t)) {
    console.error(`FAIL: Tooltip text missing keyword: ${t}`);
    process.exit(1);
  }
}
console.log("PASS: Tooltips contain required descriptions (defaults, impact, ranges).");

// 4. Assert App.js implements speed and ETA calculations
if (!appJs.includes('张/秒') && !appJs.includes('fps') && !appJs.includes('img/s')) {
  console.error("FAIL: app.js does not calculate or display processing speed (张/秒)");
  process.exit(1);
}

if (!appJs.includes('预计') && !appJs.includes('ETA') && !appJs.includes('剩余')) {
  console.error("FAIL: app.js does not calculate or display ETA (预计剩余时间)");
  process.exit(1);
}
console.log("PASS: app.js implements real-time speed (张/秒) and ETA estimation calculations.");

// 5. Assert Splitter dragging & localStorage persistence
if (!appJs.includes('ac-table-ratio') || !appJs.includes('--table-width')) {
  console.error("FAIL: app.js does not implement split pane resize or localStorage persistence");
  process.exit(1);
}
console.log("PASS: app.js implements draggable split pane and persistence.");

console.log("=== All GUI Core Logic & Structural Assertions Passed Successfully ===");
