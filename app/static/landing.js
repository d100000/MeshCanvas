document.addEventListener('DOMContentLoaded', () => {

    // 计算路径总长度，用于画线动画
    const paths = document.querySelectorAll('.draw-path');
    paths.forEach(p => {
        const len = p.getTotalLength();
        // 确保 stroke-dasharray 的单位是 px 才能触发正确的 CSS 掩码效果
        p.style.strokeDasharray = `${len}px`;
        p.style.strokeDashoffset = `${len}px`; // 初始隐藏
        // 存起来备用
        p.dataset.len = len;
    });

    const scenes = [
        // 0: 顶部 Hero
        { id: 0, p: 0.00, x: 0, y: 0, s: 1.0,  o_hero: 1, o_q: 0, o_a: 0, o_d: 0, o_e: 0, o_c: 0 },
        { id: 0, p: 0.08, x: 0, y: 0, s: 1.0,  o_hero: 1, o_q: 0, o_a: 0, o_d: 0, o_e: 0, o_c: 0 },

        // 1: 镜头下移，聚焦问题节点
        { id: 1, p: 0.15, x: 0, y: 200, s: 1.1, o_hero: 0, o_q: 1, o_a: 0, o_d: 0, o_e: 0, o_c: 0 },
        { id: 1, p: 0.25, x: 0, y: 200, s: 1.1, o_hero: 0, o_q: 1, o_a: 0, o_d: 0, o_e: 0, o_c: 0 },

        // 2: 镜头拉远并下移，展示多模型并发连线
        { id: 2, p: 0.35, x: 0, y: 450, s: 0.70, o_hero: 0, o_q: 1, o_a: 1, o_d: 0, o_e: 0, o_c: 0 },
        { id: 2, p: 0.45, x: 0, y: 450, s: 0.70, o_hero: 0, o_q: 1, o_a: 1, o_d: 0, o_e: 0, o_c: 0 },

        // 3: 镜头大幅右移，展示深度辩证区
        { id: 3, p: 0.55, x: 1500, y: 500, s: 0.85, o_hero: 0, o_q: 0, o_a: 0, o_d: 1, o_e: 0, o_c: 0 },
        { id: 3, p: 0.65, x: 1500, y: 500, s: 0.85, o_hero: 0, o_q: 0, o_a: 0, o_d: 1, o_e: 0, o_c: 0 },

        // 4: 镜头向左下移动，展示生态与计费卡片
        { id: 4, p: 0.80, x: 0, y: 1600, s: 0.9, o_hero: 0, o_q: 0, o_a: 0, o_d: 0, o_e: 1, o_c: 0 },
        { id: 4, p: 0.85, x: 0, y: 1600, s: 0.9, o_hero: 0, o_q: 0, o_a: 0, o_d: 0, o_e: 1, o_c: 0 },

        // 5: 镜头继续下沉，进入 CTA
        { id: 5, p: 0.95, x: 0, y: 2400, s: 1.0, o_hero: 0, o_q: 0, o_a: 0, o_d: 0, o_e: 0, o_c: 1 },
        { id: 5, p: 1.00, x: 0, y: 2400, s: 1.0, o_hero: 0, o_q: 0, o_a: 0, o_d: 0, o_e: 0, o_c: 1 }
    ];

    let scrollProgress = 0;

    // 监听滚动计算当前进度 (当用户拖动滚动条时更新)
    window.addEventListener('scroll', () => {
        const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
        scrollProgress = Math.max(0, Math.min(1, window.scrollY / maxScroll));
    });

    // 状态机（当前显示状态与目标状态）
    let current = { ...scenes[0] };
    let target = { ...scenes[0] };

    // 响应式基准缩放比（移动端适配缩小）
    let baseScale = 1;
    function updateBaseScale() {
        // 如果屏幕宽度小于 1000，则整体缩放以适应屏幕
        baseScale = window.innerWidth < 1000 ? window.innerWidth / 1000 : 1;
        // 移动端稍微放大一点，因为原比例太小
        if(window.innerWidth < 768) baseScale *= 1.2;
    }
    window.addEventListener('resize', updateBaseScale);
    updateBaseScale();

    const world = document.getElementById('world');
    const hero = document.getElementById('hero');
    const q = document.getElementById('q');
    const answers = document.getElementById('answers');
    const debate = document.getElementById('debate');
    const cta = document.getElementById('cta');

    // 缓动函数
    function lerp(start, end, factor) {
        return start + (end - start) * factor;
    }

    // 平滑曲线 (Smoothstep)
    function smoothstep(t) {
        return t * t * (3 - 2 * t);
    }

    // 渲染主循环
    function render() {
        // 1. 根据进度计算目标关键帧插值
        let k1 = scenes[0], k2 = scenes[scenes.length - 1];
        for (let i = 0; i < scenes.length - 1; i++) {
            if (scrollProgress >= scenes[i].p && scrollProgress <= scenes[i+1].p) {
                k1 = scenes[i];
                k2 = scenes[i+1];
                break;
            }
        }

        let t = 0;
        if (k2.p > k1.p) {
            t = (scrollProgress - k1.p) / (k2.p - k1.p);
            t = smoothstep(t); // 让过渡更平滑
        }

        target.x = lerp(k1.x, k2.x, t);
        target.y = lerp(k1.y, k2.y, t);
        target.s = lerp(k1.s, k2.s, t);
        target.o_hero = lerp(k1.o_hero, k2.o_hero, t);
        target.o_q = lerp(k1.o_q, k2.o_q, t);
        target.o_a = lerp(k1.o_a, k2.o_a, t);
        target.o_d = lerp(k1.o_d, k2.o_d, t);
        target.o_e = lerp(k1.o_e, k2.o_e, t);
        target.o_c = lerp(k1.o_c, k2.o_c, t);

        // 2. 将当前状态缓动逼近目标状态 (非常顺滑的物理惯性)
        const ease = 0.08;
        current.x = lerp(current.x, target.x, ease);
        current.y = lerp(current.y, target.y, ease);
        current.s = lerp(current.s, target.s, ease);
        current.o_hero = lerp(current.o_hero, target.o_hero, ease);
        current.o_q = lerp(current.o_q, target.o_q, ease);
        current.o_a = lerp(current.o_a, target.o_a, ease);
        current.o_d = lerp(current.o_d, target.o_d, ease);
        current.o_e = lerp(current.o_e, target.o_e, ease);
        current.o_c = lerp(current.o_c, target.o_c, ease);

        // 3. 应用变换到 DOM
        // World 平移算法：居中偏移量 + 逆向镜头平移 * 缩放比，使用 translate3d 开启硬件加速
        const finalScale = current.s * baseScale;
        world.style.transform = `translate3d(-50%, -50%, 0) scale(${finalScale}) translate3d(${-current.x}px, ${-current.y}px, 0)`;

        // 各元素透明度与微交互
        hero.style.opacity = current.o_hero;
        hero.style.transform = `translate3d(-50%, -50%, 0) translate3d(0, ${(1 - current.o_hero) * 40}px, 0)`; // 消失时往上飘
        hero.style.pointerEvents = current.o_hero > 0.5 ? 'auto' : 'none';

        q.style.opacity = current.o_q;
        q.style.transform = `translate3d(-50%, -50%, 0) scale(${0.9 + current.o_q * 0.1})`; // 出现时放大弹现
        q.style.pointerEvents = current.o_q > 0.5 ? 'auto' : 'none';

        answers.style.opacity = current.o_a;
        answers.style.pointerEvents = current.o_a > 0.5 ? 'auto' : 'none';

        // 画线逻辑：根据 answers 的透明度同步拉出 SVG 连线
        paths.forEach(p => {
            const len = parseFloat(p.dataset.len);
            // o_a 从 0 到 1，线条从无到有
            const offset = len * Math.max(0, (1 - current.o_a));

            // 给带有 flow-dash 的线条特殊处理，等线画完之后再让它流动
            if (p.classList.contains('flow-dash') && current.o_a > 0.9) {
                p.style.strokeDasharray = '10 15';
                p.style.strokeDashoffset = ""; // 移除内联样式，交给 CSS 动画接管
                p.style.animationPlayState = 'running';
            } else if (p.classList.contains('flow-dash-reverse') && current.o_d > 0.9) {
                p.style.strokeDasharray = '10 15';
                p.style.strokeDashoffset = ""; // 移除内联样式，交给 CSS 动画接管
                p.style.animationPlayState = 'running';
            } else {
                // 补充 px 单位
                p.style.strokeDasharray = `${len}px`;
                p.style.strokeDashoffset = `${offset}px`;
                p.style.animationPlayState = 'paused';
            }
        });

        debate.style.opacity = current.o_d;
        debate.style.transform = `translate3d(-50%, -50%, 0) scale(${0.95 + current.o_d * 0.05})`;
        debate.style.pointerEvents = current.o_d > 0.5 ? 'auto' : 'none';

        // 新增的生态节点控制
        const ecosystem = document.getElementById('ecosystem');
        if (ecosystem) {
            ecosystem.style.opacity = current.o_e;
            ecosystem.style.transform = `translate3d(-50%, -50%, 0) scale(${0.95 + current.o_e * 0.05})`;
            ecosystem.style.pointerEvents = current.o_e > 0.5 ? 'auto' : 'none';
        }

        cta.style.opacity = current.o_c;
        cta.style.transform = `translate3d(-50%, -50%, 0) translate3d(0, ${(1 - current.o_c) * 40}px, 0)`;
        cta.style.pointerEvents = current.o_c > 0.5 ? 'auto' : 'none';

        requestAnimationFrame(render);
    }

    // 启动渲染循环
    requestAnimationFrame(render);

    // 处理首次加载如果在半路的情况
    window.dispatchEvent(new Event('scroll'));
});
