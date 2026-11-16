// DOM 静默快照（FB-5）：把当前文档的可见内容画进 canvas——零授权、零弹框、
// http 非安全上下文也能用（规格 §1：换掉 getDisplayMedia 的屏共享抓帧）。
//
// 规格原定 vendor 引入 html2canvas；本机无外网（直连/CDN 均不通，已试
// curl 与浏览器两条路），按规格的降级条款改用等价零依赖方案：克隆文档 +
// 内联计算样式 + 同源图转 dataURL + foreignObject SVG 序列化绘制
// （dom-to-image 同款思路；注意 SVG 必须走 data URL——blob URL 会被
// Chromium 判跨源污染画布，getImageData/toBlob 全废）。UI 页面（设置/文件/
// 指挥台/概览）快照可用；终端 canvas / MJPEG img 这类画不出来的由上层
// （feedback-capture 的空白检测 + 页面类型提示）引导手动上传。
//
// 页面侧挂到 window.WPFeedbackCapture.snap（沿用既有入口，不为快照新增
// window 全局——ALLOWED_WINDOW 登记表在本批边界外，与 tasks-tree.js 挂
// WPTasksOverview.tree 同一约定）。纯 Node 下只出 module.exports。
(function () {
    'use strict';

    var XMLSerializer = (typeof window !== 'undefined' && window.XMLSerializer) || null;
    var IMAGE_TIMEOUT = 1500;      // 单张图内嵌等待（MJPEG 流永远不结束，超时就放弃它）

    // 内联进克隆体的计算样式：覆盖布局/盒模型/文字/颜色，足够还原 UI 页面
    var STYLE_PROPS = [
        'display', 'position', 'top', 'right', 'bottom', 'left', 'float', 'clear', 'z-index',
        'width', 'height', 'min-width', 'min-height', 'max-width', 'max-height', 'box-sizing',
        'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
        'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
        'border-top-width', 'border-right-width', 'border-bottom-width', 'border-left-width',
        'border-top-style', 'border-right-style', 'border-bottom-style', 'border-left-style',
        'border-top-color', 'border-right-color', 'border-bottom-color', 'border-left-color',
        'border-top-left-radius', 'border-top-right-radius',
        'border-bottom-right-radius', 'border-bottom-left-radius',
        'background-color', 'color', 'opacity', 'visibility', 'overflow-x', 'overflow-y',
        'font-family', 'font-size', 'font-style', 'font-weight', 'font-variant',
        'line-height', 'letter-spacing', 'text-align', 'text-decoration', 'text-indent',
        'text-transform', 'white-space', 'vertical-align',
        'flex-direction', 'flex-wrap', 'justify-content', 'align-items', 'align-self',
        'flex-grow', 'flex-shrink', 'flex-basis', 'gap',
        'box-shadow', 'transform', 'transform-origin', 'object-fit', 'cursor', 'list-style-type'
    ];

    function withTimeout(promise, ms) {
        return Promise.race([
            Promise.resolve(promise),
            new Promise(function (resolve, reject) {
                setTimeout(function () { reject(new Error('timeout')); }, ms);
            })
        ]);
    }

    function blobToDataUrl(blob) {
        return new Promise(function (resolve) {
            var reader = new FileReader();
            reader.onload = function () { resolve(String(reader.result || '')); };
            reader.onerror = function () { resolve(''); };
            reader.readAsDataURL(blob);
        });
    }

    // 成对走「真实文档 ↔ 克隆体」：计算样式只能从真实节点读（克隆体未挂载，
    // getComputedStyle 取不到值）。fixed/sticky 元素钉在视口上，正文整体上移
    // 后要给它们按实时矩形钉回去，否则滚过的页面里它们会跑出画面。
    function inlineAll(srcDoc, cloneDoc, scrollX, scrollY) {
        var view = srcDoc.defaultView;
        if (!view || !view.getComputedStyle) { return; }
        var stack = [[srcDoc.documentElement, cloneDoc.documentElement]];
        while (stack.length) {
            var pair = stack.pop();
            var a = pair[0], b = pair[1];
            if (!a || a.nodeType !== 1 || !b) { continue; }
            var tag = a.tagName ? a.tagName.toLowerCase() : '';
            if (tag === 'script' || tag === 'style' || tag === 'link') { continue; }
            // 密码框的 value 属性（= defaultValue）会随 DOM 序列化进快照，剥掉；
            // 用户键入的值是 property 不进序列化，这里只是彻底起见
            if (tag === 'input' && a.getAttribute('type') === 'password') {
                b.removeAttribute('value');
            }
            var cs = view.getComputedStyle(a);
            var css = '';
            for (var i = 0; i < STYLE_PROPS.length; i++) {
                var prop = STYLE_PROPS[i];
                var value = cs.getPropertyValue(prop);
                if (value) { css += prop + ':' + value + ';'; }
            }
            if ((cs.position === 'fixed' || cs.position === 'sticky') && (scrollX || scrollY)) {
                var r = a.getBoundingClientRect();
                css += 'position:fixed;left:' + (r.left + scrollX) + 'px;top:' + (r.top + scrollY) + 'px;'
                    + 'right:auto;bottom:auto;margin:0;'
                    + 'width:' + Math.round(r.width) + 'px;height:' + Math.round(r.height) + 'px;';
            }
            b.setAttribute('style', css);
            var ak = a.children, bk = b.children;
            for (var j = 0; j < ak.length; j++) {
                if (bk[j]) { stack.push([ak[j], bk[j]]); }
            }
        }
    }

    // 同源图内嵌成 dataURL：SVG 图像里外部资源一律不加载，不内嵌就是空白。
    // 跨域图保持原样（渲染为空，不拖慢快照）；MJPEG 流 fetch 不结束，超时放弃。
    function embedImages(cloneDoc, baseHref) {
        var imgs = cloneDoc.querySelectorAll('img');
        var jobs = [];
        var degraded = false;
        for (var i = 0; i < imgs.length; i++) {
            jobs.push((function (img) {
                var src = img.getAttribute('src') || '';
                if (!src || src.slice(0, 5) === 'data:' || src.slice(0, 5) === 'blob:') {
                    return Promise.resolve(false);
                }
                var abs;
                try { abs = new URL(src, baseHref); } catch (e) { return Promise.resolve(false); }
                if (abs.origin !== window.location.origin) { return Promise.resolve(false); }
                return withTimeout(
                    fetch(abs.href).then(function (res) { return res.blob(); }).then(blobToDataUrl),
                    IMAGE_TIMEOUT
                ).then(function (url) {
                    if (url) { img.setAttribute('src', url); return false; }
                    degraded = true;
                    return true;
                }, function () {
                    degraded = true;   // MJPEG 流/坏图：画不出来，交给上层提示手动上传
                    return true;
                });
            })(imgs[i]));
        }
        if (!jobs.length) { return Promise.resolve(degraded); }
        return Promise.all(jobs).then(function () { return degraded; });
    }

    function svgToCanvas(svgText, width, height) {
        return new Promise(function (resolve, reject) {
            // 必须走 data URL：blob URL 的 SVG 图像画进 canvas 会被 Chromium 判跨源
            // 污染（getImageData/toBlob 全抛 SecurityError，截图就没法提交了）
            var img = document.createElement('img');
            img.onload = function () {
                try {
                    var canvas = document.createElement('canvas');
                    canvas.width = width;
                    canvas.height = height;
                    var ctx = canvas.getContext('2d');
                    ctx.fillStyle = '#FFFFFF';
                    ctx.fillRect(0, 0, width, height);
                    ctx.drawImage(img, 0, 0, width, height);
                    resolve(canvas);
                } catch (e) {
                    reject(e);
                }
            };
            img.onerror = function () {
                reject(new Error('snapshot svg decode failed'));
            };
            img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgText);
        });
    }

    // 快照一个文档的可见区域：{shot: canvas|null, degraded: 是否有图没嵌上}
    function snapDocument(srcDoc, opts) {
        opts = opts || {};
        if (!XMLSerializer || !srcDoc || !srcDoc.documentElement) {
            return Promise.resolve({ shot: null, degraded: false });
        }
        var width = Math.max(1, Math.round(opts.width || srcDoc.documentElement.clientWidth || 0));
        var height = Math.max(1, Math.round(opts.height || srcDoc.documentElement.clientHeight || 0));
        var scrollX = opts.scrollX || 0;
        var scrollY = opts.scrollY || 0;
        var clone = srcDoc.documentElement.cloneNode(true);
        inlineAll(srcDoc, clone, scrollX, scrollY);
        // 反馈 UI 自己不出镜；脚本/样式表在 SVG 图像里不加载（样式已内联），清掉减负
        var junk = clone.querySelectorAll('.fb-root, script, link, style, noscript');
        for (var i = 0; i < junk.length; i++) { junk[i].remove(); }
        var body = clone.querySelector('body');
        if (body) {
            if (body.classList) {
                body.classList.remove('fb-open', 'fb-raise', 'fb-scroll', 'fb-avoid',
                                      'fb-hide', 'fb-lift1', 'fb-lift2');
            }
            // 正文整体上移一个滚动量：露出当前视口那一段（fixed/sticky 已另行钉回）
            if (scrollX || scrollY) {
                body.style.transform = 'translate(' + (-scrollX) + 'px,' + (-scrollY) + 'px)';
            }
        }
        return embedImages(clone, srcDoc.location ? srcDoc.location.href : window.location.href)
            .then(function (degraded) {
                var xml;
                try {
                    xml = new XMLSerializer().serializeToString(clone);
                } catch (e) {
                    return { shot: null, degraded: true };
                }
                var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + width
                    + '" height="' + height + '">'
                    + '<foreignObject x="0" y="0" width="100%" height="100%">'
                    + xml + '</foreignObject></svg>';
                return svgToCanvas(svg, width, height).then(function (canvas) {
                    return { shot: canvas, degraded: degraded };
                }, function () {
                    return { shot: null, degraded: true };
                });
            });
    }

    var api = { snapDocument: snapDocument };
    if (typeof window !== 'undefined' && window.WPFeedbackCapture) {
        window.WPFeedbackCapture.snap = api;
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
})();
