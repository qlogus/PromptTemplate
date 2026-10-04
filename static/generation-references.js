class GenerationReferences {
    constructor(container) {
        this.container = container;
        this.items = [];
        this.provider = null;
        container.innerHTML = `
            <fieldset class="reference-controls stack">
                <div class="row"><strong>参考图片</strong><span class="muted" data-capacity></span></div>
                <label data-files>添加本地图片<input class="field" type="file" accept="image/*" multiple></label>
                <label>添加图片 URL<textarea class="field" rows="2" placeholder="每行一个公网图片直链" data-urls></textarea></label>
                <button class="secondary" type="button" data-add>添加 URL</button>
                <div class="muted" role="status"></div>
                <div class="reference-list stack"></div>
            </fieldset>`;
        this.files = container.querySelector('[type="file"]');
        this.urls = container.querySelector('[data-urls]');
        this.message = container.querySelector('[role="status"]');
        this.files.onchange = () => {
            const files = [...this.files.files];
            this.files.value = '';
            if (!this.canAdd(files.length)) return;
            if (files.some(file => !file.type.startsWith('image/') || file.size > 20 * 1024 * 1024)) {
                this.message.textContent = '请选择图片文件，每张不超过 20 MB';
                return;
            }
            this.items.push(...files.map(file => ({file, name:file.name, preview:URL.createObjectURL(file)})));
            this.render();
        };
        container.querySelector('[data-add]').onclick = () => {
            const urls = this.urls.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
            if (!this.canAdd(urls.length)) return;
            try {
                for (const value of urls) {
                    if (!['http:', 'https:'].includes(new URL(value).protocol)) throw Error();
                }
            } catch {
                this.message.textContent = '请填写有效的 HTTP(S) 图片 URL';
                return;
            }
            this.items.push(...urls.map(url => ({url, name:url, preview:url})));
            this.urls.value = '';
            this.render();
        };
    }

    canAdd(count) {
        const limit = this.provider?.reference_limit || 0;
        if (this.items.length + count > limit) {
            this.message.textContent = `当前 Provider 最多支持 ${limit} 张，不能再添加 ${count} 张。`;
            return false;
        }
        return true;
    }

    setProvider(provider) {
        this.provider = provider;
        this.container.hidden = !provider || (provider.reference_limit === 0 && !this.items.length);
        this.container.querySelector('[data-files]').hidden = provider?.reference_protocol !== 'multipart_edit' || !provider?.reference_limit;
        this.files.multiple = provider?.reference_limit > 1;
        this.container.querySelector('[data-capacity]').textContent = !provider?.reference_limit ? '不支持参考图' : provider.reference_limit === 1 ? '单图' : `多图 · 最多 ${provider.reference_limit} 张`;
        this.render();
    }

    render() {
        const list = this.container.querySelector('.reference-list');
        list.replaceChildren();
        this.message.textContent = this.items.length > (this.provider?.reference_limit || 0)
            ? '参考图数量超过当前 Provider 限制，请删除多余图片或切换 Provider。'
            : this.provider?.reference_protocol === 'json_url' && this.items.some(item => item.file)
                ? '当前 Provider 仅接受 URL，请移除本地图片或切换 Provider。'
                : '编号即发送顺序；Prompt 中可引用图1 / image1 / 画像1。';
        this.items.forEach((item, index) => {
            const row = document.createElement('div');
            row.className = 'reference-row';
            const image = document.createElement('img');
            image.src = item.preview;
            image.alt = `图${index + 1}`;
            image.className = 'reference-thumbnail';
            const label = document.createElement('span');
            label.textContent = `图${index + 1} · ${item.name}`;
            label.className = 'reference-name';
            const actions = document.createElement('div');
            actions.className = 'actions';
            for (const [title, delta] of [['上移', -1], ['下移', 1], ['删除', 0]]) {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'secondary';
                button.textContent = title;
                button.setAttribute('aria-label', `${title}图${index + 1}`);
                button.disabled = delta !== 0 && (index + delta < 0 || index + delta >= this.items.length);
                button.onclick = () => {
                    if (delta) [this.items[index], this.items[index + delta]] = [this.items[index + delta], this.items[index]];
                    else {
                        if (item.file) URL.revokeObjectURL(item.preview);
                        this.items.splice(index, 1);
                    }
                    this.render();
                };
                actions.appendChild(button);
            }
            row.append(image, label, actions);
            list.appendChild(row);
        });
    }

    async snapshot() {
        if (this.urls.value.trim()) throw Error('URL 尚未添加，请先点击“添加 URL”。');
        if (this.items.length > (this.provider?.reference_limit || 0)) throw Error('参考图数量超过当前 Provider 限制');
        const items = [...this.items];
        if (this.provider?.reference_protocol === 'json_url' && items.some(item => item.file)) throw Error('当前 Provider 仅接受公网图片 URL');
        return Promise.all(items.map(item => item.file ? new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = () => reject(Error(`读取 ${item.name} 失败`));
            reader.readAsDataURL(item.file);
        }) : item.url));
    }

    dispose() {
        for (const item of this.items) if (item.file) URL.revokeObjectURL(item.preview);
    }
}
