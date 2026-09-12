// UI Mock Backend & Test Automation Harness
// Injected into browser or test runner when window.__TAURI__ is not present

(function () {
  if (window.__TAURI__) return;

  const eventListeners = new Map();

  const MOCK_FILES = [
    { name: "DSC00827.HIF", path: "/test/photos/DSC00827.HIF", raw_score: 4.12, rating: 3, sharp: 0.82, comp: 0.91, veto: "", group: 1 },
    { name: "DSC00828.HIF", path: "/test/photos/DSC00828.HIF", raw_score: 4.35, rating: 4, sharp: 0.88, comp: 0.95, veto: "", group: 1 },
    { name: "DSC00829.HIF", path: "/test/photos/DSC00829.HIF", raw_score: 2.10, rating: -1, sharp: 0.03, comp: 0.45, veto: "sharpness < 0.05", group: 1 },
    { name: "DSC00830.HIF", path: "/test/photos/DSC00830.HIF", raw_score: 3.55, rating: 2, sharp: 0.75, comp: 0.80, veto: "", group: 1 },
    { name: "DSC00831.HIF", path: "/test/photos/DSC00831.HIF", raw_score: 1.80, rating: -1, sharp: 0.60, comp: 0.20, veto: "no_detection", group: 2 },
    { name: "DSC00832.HIF", path: "/test/photos/DSC00832.HIF", raw_score: 4.80, rating: 5, sharp: 0.96, comp: 0.98, veto: "", group: 2 },
    { name: "DSC00833.HIF", path: "/test/photos/DSC00833.HIF", raw_score: 3.20, rating: 1, sharp: 0.70, comp: 0.72, veto: "", group: 2 },
    { name: "DSC00834.HIF", path: "/test/photos/DSC00834.HIF", raw_score: 2.90, rating: -1, sharp: 0.65, comp: 0.68, veto: "raw < 3.1", group: 2 },
  ];

  // 1x1 transparent PNG data url fallback
  const DUMMY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";

  function emitEvent(name, payload) {
    const list = eventListeners.get(name) || [];
    for (const cb of list) {
      cb({ event: name, payload });
    }
  }

  let isRunning = false;
  let cancelRequested = false;

  async function mockInvoke(cmd, args = {}) {
    console.log(`[Mock Tauri invoke] cmd=${cmd}`, args);

    if (cmd === "select_folder") {
      return "/Users/test/camera_burst_01";
    }

    if (cmd === "scan") {
      const paths = {};
      MOCK_FILES.forEach(f => { paths[f.name] = f.path; });
      setTimeout(() => {
        emitEvent("scanned", { count: MOCK_FILES.length, paths });
      }, 50);
      return { count: MOCK_FILES.length };
    }

    if (cmd === "run") {
      isRunning = true;
      cancelRequested = false;
      
      // Simulate stages
      setTimeout(() => emitEvent("stage", { message: "正在收集照片...", progress: 0.1 }), 20);
      setTimeout(() => emitEvent("stage", { message: "正在解析 EXIF 元数据...", progress: 0.3 }), 60);
      setTimeout(() => emitEvent("stage", { message: "正在分析打分...", progress: 0.4 }), 100);

      // Simulate streaming frames
      let scored = 0;
      let keep = 0;
      let reject = 0;

      const interval = setInterval(() => {
        if (!isRunning || cancelRequested || scored >= MOCK_FILES.length) {
          clearInterval(interval);
          if (cancelRequested) {
            emitEvent("cancelled", {});
          } else if (scored >= MOCK_FILES.length) {
            emitEvent("done", {
              total: MOCK_FILES.length,
              keep,
              reject,
              elapsed: 1.2,
              dist: "5星:1 4星:1 3星:1 2星:1 1星:1 丢弃:3"
            });
          }
          isRunning = false;
          return;
        }

        const item = MOCK_FILES[scored];
        if (item.rating > 0) keep++; else reject++;
        scored++;

        emitEvent("frame", {
          name: item.name,
          rating: item.rating,
          sharp: item.sharp,
          comp: item.comp,
          raw: item.raw_score,
          veto: item.veto,
          status: "scored"
        });
      }, 50);

      return true;
    }

    if (cmd === "cancel") {
      cancelRequested = true;
      isRunning = false;
      return true;
    }

    if (cmd === "preview") {
      // Simulate preview return with detected boxes
      return {
        path: args.path,
        data: DUMMY_PNG_B64,
        boxes: [[100, 100, 300, 250, "car", 0.95]],
        crop: [50, 40, 450, 320]
      };
    }

    if (cmd === "export_csv") {
      return "export_mock.csv";
    }

    return null;
  }

  async function mockListen(eventName, handler) {
    if (!eventListeners.has(eventName)) {
      eventListeners.set(eventName, []);
    }
    eventListeners.get(eventName).push(handler);
    return () => {
      const arr = eventListeners.get(eventName) || [];
      const idx = arr.indexOf(handler);
      if (idx >= 0) arr.splice(idx, 1);
    };
  }

  window.__TAURI__ = {
    core: {
      invoke: mockInvoke,
    },
    event: {
      listen: mockListen,
    },
  };

  // Expose test automation helpers
  window.__MOCK_TEST_HELPERS__ = {
    emitEvent,
    MOCK_FILES,
    getEventListeners: () => eventListeners,
  };

  console.log("Mock Tauri IPC Backend initialized.");
})();
