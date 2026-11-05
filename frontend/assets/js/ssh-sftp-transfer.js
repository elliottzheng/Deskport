// SFTP 传输: 下载(单文件流式 / 多选打包) 、转存本机、上传(文件与文件夹)。
// 只负责"搬数据", 选中状态从 SftpState 读, 上传完成后回调 navigate 刷新。
window.SftpTransfer = (function () {
    'use strict';
    const state = () => window.SftpState.state;
    let api = null;
    let toastMsg = function () {};
    let alertMsg = function () {};
    let navigate = function () {};
    let uploading = false;

    function init(deps) {
        api = deps.api;
        toastMsg = deps.toastMsg || toastMsg;
        alertMsg = deps.alert || alertMsg;
        navigate = deps.navigate || navigate;
    }

    // 单个文件: 流式直下; 多选/文件夹: 服务端打包 zip
    // (浏览器会拦截连续多次自动下载)
    async function downloadSelected() {
        const sel = window.SftpState.selectedEntries();
        if (!sel.length) { toastMsg('请先选中文件或文件夹'); return; }
        if (sel.length === 1 && sel[0].type !== 'folder') {
            const a = document.createElement('a');
            a.href = '/api/ssh/sftp/download?hostId=' + encodeURIComponent(state().hostId)
                + '&path=' + encodeURIComponent(sel[0].path);
            a.download = '';
            document.body.appendChild(a);
            a.click();
            a.remove();
            return;
        }
        const paths = sel.map(x => x.path);
        toastMsg('正在打包 ' + paths.length + ' 项…');
        try {
            const r = await fetch('/api/ssh/sftp/download-zip?hostId='
                + encodeURIComponent(state().hostId)
                + '&paths=' + encodeURIComponent(JSON.stringify(paths)));
            if (!r.ok) {
                const d = await r.json().catch(() => ({}));
                throw new Error(d.error || ('HTTP ' + r.status));
            }
            const blob = await r.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'sftp-' + Date.now() + '.zip';
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 30000);
            toastMsg('打包完成 (' + Math.round(blob.size / 1024) + ' KB)');
        } catch (err) {
            toastMsg('打包下载失败: ' + err.message);
        }
    }

    async function toLocalSelected() {
        const p = window.SftpState.selectedPath()
            || (state().path !== '/' ? state().path : null);
        if (!p) { alertMsg('请先选中文件'); return; }
        try {
            const data = await api('/api/ssh/sftp/to-local', {
                method: 'POST',
                body: JSON.stringify({ hostId: state().hostId, remotePath: p }),
            });
            alertMsg('已存到本机: ' + data.localPath);
        } catch (err) { alertMsg('转存失败: ' + err.message); }
    }

    async function uploadFiles(ev) {
        const input = ev.target;
        const folderMode = input.id === 'sftp-folder-input';
        const files = Array.from(input.files || []);
        input.value = '';
        if (!files.length || uploading) { return; }
        const items = files.map(function (f) {
            return { file: f, rel: folderMode ? (f.webkitRelativePath || f.name) : f.name };
        });
        uploading = true;
        const BATCH = 20;
        // 总字节数已知, 可以算出跨批次的整体百分比
        const totalBytes = items.reduce(function (s, it) { return s + (it.file.size || 0); }, 0);
        let sentBytes = 0;
        let done = 0;
        const failed = [];
        const box = WPCommon.uploadBox;
        box.open('SFTP 上传');
        try {
            for (let i = 0; i < items.length; i += BATCH) {
                const slice = items.slice(i, i + BATCH);
                const sliceBytes = slice.reduce(function (s, it) { return s + (it.file.size || 0); }, 0);
                const baseSent = sentBytes;
                const current = slice[0];
                box.progress({
                    file: slice.length > 1
                        ? current.rel + ' 等 ' + slice.length + ' 个'
                        : current.rel,
                    label: done + 1 + '-' + (done + slice.length) + ' / ' + items.length
                           + ' · ' + WPCommon.fmtSize(totalBytes),
                    loaded: baseSent, total: totalBytes || 1,
                });
                const fd = new FormData();
                fd.append('hostId', state().hostId);
                fd.append('remoteDir', state().path);
                slice.forEach(function (it) {
                    fd.append('files', it.file, it.file.name);
                    fd.append('paths', it.rel);
                });
                const data = await WPCommon.postProgress('/api/ssh/sftp/upload-batch', fd,
                    function (loaded, total) {
                        // 批内进度线性折算到整体
                        var frac = total ? Math.min(1, loaded / total) : 1;
                        box.progress({
                            file: slice.length > 1
                                ? current.rel + ' 等 ' + slice.length + ' 个'
                                : current.rel,
                            label: done + 1 + '-' + (done + slice.length) + ' / ' + items.length
                                   + ' · ' + WPCommon.fmtSize(totalBytes),
                            loaded: baseSent + Math.round(sliceBytes * frac),
                            total: totalBytes || 1,
                        });
                    }, 0);
                sentBytes += sliceBytes;
                done += slice.length;
                if (data.failed && data.failed.length) {
                    data.failed.forEach(function (f) {
                        failed.push((f.name || '') + ': ' + (f.error || '失败'));
                    });
                }
                box.progress({
                    file: '', label: done + ' / ' + items.length,
                    loaded: sentBytes, total: totalBytes || 1,
                });
            }
            if (failed.length) {
                box.finish(false, '上传完成，' + failed.length + ' 个失败');
                alertMsg('部分上传失败:\n' + failed.slice(0, 8).join('\n'));
            } else {
                box.finish(true, '已上传 ' + items.length + ' 个文件');
            }
            await navigate(state().path || '.');
        } catch (err) {
            box.finish(false, '上传失败: ' + err.message);
            alertMsg('上传失败: ' + err.message);
        } finally {
            uploading = false;
        }
    }

    return {
        init: init,
        downloadSelected: downloadSelected,
        toLocalSelected: toLocalSelected,
        uploadFiles: uploadFiles,
    };
})();
