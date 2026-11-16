// 反馈入口：把捕获层与面板接起来，负责提交、离线队列与补传。
// 规格 §8.3：信封先入本地队列，失败自动重试（指数退避），网络恢复补传；幂等靠 idempotency_key。
window.WPFeedback = (function () {
    var QUEUE_KEY = 'wp_feedback_queue_v1';
    var MAX_QUEUE = 20;
    var MAX_ATTEMPTS = 6;
    var attempts = {};
    var flushing = false;
    var timer = null;

    function uuid() {
        if (window.crypto && crypto.randomUUID) { return crypto.randomUUID(); }
        var out = '';
        for (var i = 0; i < 32; i++) {
            var r = (window.crypto && crypto.getRandomValues)
                ? crypto.getRandomValues(new Uint8Array(1))[0]
                : Math.floor(Math.random() * 256);
            out += (r % 16).toString(16);
        }
        return out;
    }

    function appVersion() {
        var scripts = document.querySelectorAll('script[src*="/assets/js/"]');
        for (var i = 0; i < scripts.length; i++) {
            var m = /[?&]v=([^&]+)/.exec(scripts[i].getAttribute('src') || '');
            if (m && m[1] !== '__BUILD__') { return decodeURIComponent(m[1]); }
        }
        return 'dev';
    }

    // ---- 本地队列 ----
    function loadQueue() {
        try {
            var raw = localStorage.getItem(QUEUE_KEY);
            var list = raw ? JSON.parse(raw) : [];
            return Array.isArray(list) ? list.slice(-MAX_QUEUE) : [];
        } catch (e) {
            return [];
        }
    }

    function saveQueue(list) {
        try {
            localStorage.setItem(QUEUE_KEY, JSON.stringify(list.slice(-MAX_QUEUE)));
            return true;
        } catch (e) {
            // 配额不够（截图 dataURL 太大）：退一步只留文字，别把整条丢掉
            try {
                localStorage.setItem(QUEUE_KEY, JSON.stringify(list.slice(-MAX_QUEUE).map(function (item) {
                    delete item.shot;
                    item.envelope.capture = item.envelope.capture || {};
                    item.envelope.capture.note = '本地队列空间不足，截图未保留';
                    return item;
                })));
                return true;
            } catch (e2) {
                return false;
            }
        }
    }

    function enqueue(envelope, shotDataUrl) {
        var list = loadQueue();
        list.push({ envelope: envelope, shot: shotDataUrl || '', tries: 0, at: Date.now() });
        saveQueue(list);
    }

    // ---- 上传与提交 ----
    function blobFromDataUrl(dataUrl) {
        return fetch(dataUrl).then(function (res) { return res.blob(); });
    }

    function uploadShot(blob) {
        var form = new FormData();
        form.append('file', blob, 'shot.jpg');
        return fetch('/api/feedback/attachments', {
            method: 'POST', body: form, credentials: 'same-origin'
        }).then(function (res) {
            if (!res.ok) { return res.json().catch(function () { return {}; }).then(function (body) {
                throw new Error(body.error || ('上传截图失败 HTTP ' + res.status));
            }); }
            return res.json();
        });
    }

    // ---- 手动上传兜底（FB-5）：快照拿不到（终端/投屏/画不出来）时用户选一张图。
    // 读图 → 缩到 ≤2560 边 → 压到 ≤1MB → POST attachments；成功返 {file_id, canvas}
    // （canvas 交给面板接着标注），失败抛错由面板提示，不阻断提交
    function uploadManualShot(file) {
        return new Promise(function (resolve, reject) {
            var url = URL.createObjectURL(file);
            var img = document.createElement('img');
            img.onload = function () {
                URL.revokeObjectURL(url);
                var k = Math.min(1, 2560 / Math.max(img.naturalWidth, img.naturalHeight));
                var c = document.createElement('canvas');
                c.width = Math.max(1, Math.round(img.naturalWidth * k));
                c.height = Math.max(1, Math.round(img.naturalHeight * k));
                c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
                window.WPFeedbackCapture.compress(c, 1024 * 1024).then(function (blob) {
                    if (!blob) { reject(new Error('图片压不到 1MB 以内，换张小一点的试试')); return; }
                    uploadShot(blob).then(function (res) {
                        if (!res || !res.file_id) { reject(new Error('上传失败')); return; }
                        resolve({ file_id: res.file_id, canvas: c });
                    }, reject);
                }, reject);
            };
            img.onerror = function () {
                URL.revokeObjectURL(url);
                reject(new Error('这张图读不出来，换一张试试'));
            };
            img.src = url;
        });
    }

    function postEnvelope(envelope) {
        return fetch('/api/feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(envelope)
        }).then(function (res) {
            return res.json().catch(function () { return {}; }).then(function (body) {
                if (!res.ok) { throw new Error(body.error || ('提交失败 HTTP ' + res.status)); }
                return body;
            });
        });
    }

    function finish(panel, body) {
        var text = body && body.duplicate ? '这条反馈已经提交过了' : '收到，谢谢！可以在「我的反馈」里看进度。';
        panel.toast(text);
        panel.reset();
        panel.close();
    }

    // ---- 入口：面板点提交后走到这里 ----
    function submit(payload) {
        var panel = window.WPFeedbackPanel;
        var capture = window.WPFeedbackCapture;
        var envelope = capture.envelope({
            feedback_id: uuid(),
            idempotency_key: uuid(),
            app_version: appVersion(),
            message: payload.message,
            contact: payload.contact,
            attachments: [],
            capture: (payload.blob || payload.uploadedFileId)
                ? {} : { note: payload.captureNote || '未附带截图' }
        });
        panel.setHint('正在提交…');

        // 附件两条来路：画布快照/标注版（source=page 或上传图标注后的 upload），
        // 以及手动上传已拿到 file_id 的（source=upload，不重复传图）
        var upload = payload.blob ? uploadShot(payload.blob) : Promise.resolve(null);
        return upload.then(function (file) {
            if (file && file.file_id) {
                envelope.attachments = [{
                    type: 'screenshot', file_id: file.file_id,
                    has_annotations: !!payload.hasAnnotations,
                    source: payload.attachmentSource || 'page'
                }];
            } else if (payload.uploadedFileId) {
                envelope.attachments = [{
                    type: 'screenshot', file_id: payload.uploadedFileId,
                    has_annotations: !!payload.hasAnnotations,
                    source: 'upload'
                }];
            }
            return postEnvelope(envelope);
        }).then(function (body) {
            panel.setBusy(false);
            finish(panel, body);
        }).catch(function (err) {
            panel.setBusy(false);
            // 失败先入队：断网 / 服务重启都不丢（§8.3）
            var toDataUrl = payload.blob ? new Promise(function (resolve) {
                var reader = new FileReader();
                reader.onload = function () { resolve(String(reader.result || '')); };
                reader.onerror = function () { resolve(''); };
                reader.readAsDataURL(payload.blob);
            }) : Promise.resolve('');
            toDataUrl.then(function (dataUrl) {
                enqueue(envelope, dataUrl);
                panel.setHint('网络不给力，已暂存这条反馈，恢复后自动补传。', true);
                panel.toast('已暂存，联网后自动补传');
            });
        });
    }

    // ---- 补传：指数退避 + 网络恢复触发 ----
    function scheduleFlush(delay) {
        if (timer) { return; }
        timer = setTimeout(function () {
            timer = null;
            flush();
        }, delay || 15000);
    }

    function flush() {
        if (flushing) { return Promise.resolve(); }
        var list = loadQueue();
        if (!list.length) { return Promise.resolve(); }
        flushing = true;
        var head = list[0];
        var key = head.envelope.idempotency_key;
        attempts[key] = (attempts[key] || 0) + 1;
        var upload = head.shot ? blobFromDataUrl(head.shot).then(uploadShot) : Promise.resolve(null);
        return upload.then(function (file) {
            if (file && file.file_id && !(head.envelope.attachments || []).length) {
                head.envelope.attachments = [{ type: 'screenshot', file_id: file.file_id,
                                               has_annotations: false, source: 'page' }];
            }
            return postEnvelope(head.envelope);
        }).then(function () {
            var rest = loadQueue();
            rest.shift();
            saveQueue(rest);
            flushing = false;
            delete attempts[key];
            if (rest.length) { scheduleFlush(2000); }
        }).catch(function () {
            flushing = false;
            var tries = attempts[key];
            if (tries >= MAX_ATTEMPTS) {
                delete attempts[key];          // 交给下次打开页面再试，不再原地耗
                return;
            }
            scheduleFlush(Math.min(300000, Math.pow(2, tries) * 5000));
        });
    }

    // 「我的反馈」模块：9 个页面随脚本标签加载；tasks.html 本批不改（另一批在改），
    // 在那里按需兜底加载，红点与页签不缺功能
    function ensureMine(done) {
        if (window.WPFeedbackMine) { done(); return; }
        var s = document.createElement('script');
        s.src = '/assets/js/feedback-mine.js?v=' + encodeURIComponent(appVersion());
        s.onload = done;
        s.onerror = done;
        document.head.appendChild(s);
    }

    function init() {
        if (location.pathname === '/login') { return; }
        if (!window.WPFeedbackCapture || !window.WPFeedbackPanel) { return; }
        window.WPFeedbackCapture.init();
        if (window.WPFeedbackCapture.isFrame && window.WPFeedbackCapture.isFrame()) {
            return;                     // 壳里的 iframe：只把轨迹上报给顶层，不出按钮、不排队
        }
        window.WPFeedbackCapture.setAppVersion(appVersion());
        window.WPFeedbackPanel.init(submit);
        ensureMine(function () {
            if (window.WPFeedbackMine) { window.WPFeedbackMine.init(); }
        });
        document.addEventListener('keydown', function (ev) {
            if ((ev.ctrlKey || ev.metaKey) && ev.shiftKey && (ev.key === 'F' || ev.key === 'f')) {
                ev.preventDefault();
                window.WPFeedbackPanel.open('write');
            }
        });
        window.addEventListener('online', function () { flush(); });
        setTimeout(flush, 3000);              // 上次没送出去的，进来就补
        if (loadQueue().length) { scheduleFlush(15000); }
    }

    return { init: init, flush: flush, queueLength: function () { return loadQueue().length; },
             uploadAttachment: uploadShot, uploadManualShot: uploadManualShot };
})();

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { window.WPFeedback.init(); });
} else {
    window.WPFeedback.init();
}