const compressionLabels = {not_requested:'未压缩', requested:'压缩中', pending:'压缩中', completed:'压缩完成', failed:'压缩失败，原件保留', unsupported_without_image_encoder:'暂不支持图片压缩'};

function mediaPreview(media, label, urls) {
    const node = document.createElement(media.kind === 'image' ? 'img' : media.kind === 'video' ? 'video' : media.kind === 'audio' ? 'audio' : 'span');
    if (media.kind === 'image') {
        node.alt = label;
        node.loading = 'lazy';
        node.className = 'reference-thumbnail';
    } else if (media.kind === 'video' || media.kind === 'audio') {
        node.controls = true;
        node.preload = 'metadata';
        node.className = 'media-player';
    } else {
        node.textContent = label;
    }
    if (media.kind !== 'file') node.src = urls.url(media.source);
    return node;
}

function hydrateExampleMedia(prompt) {
    document.querySelectorAll('[data-example]').forEach(box => {
        box.addEventListener('toggle', () => {
            if (!box.open || box.dataset.hydrated) return;
            const example = prompt.examples.find(item => item.id === box.dataset.example);
            for (const purpose of ['references','results']) {
                const container = box.querySelector(`[data-example-${purpose}]`);
                example[purpose].forEach((media, index) => {
                    const row = document.createElement('div');
                    row.className = 'reference-row';
                    const label = media.label || (purpose === 'references' ? `参考 ${index+1}` : `结果 ${index+1}`);
                    row.appendChild(mediaPreview(media,label,exampleMedia));
                    const link = document.createElement('a');
                    link.href = exampleMedia.url(media.source);
                    link.target = '_blank';
                    link.rel = 'noopener';
                    link.textContent = label + ' · 打开原件';
                    row.appendChild(link);
                    if (media.kind === 'video') {
                        const status = document.createElement('span');
                        status.className = 'muted';
                        status.textContent = compressionLabels[media.compression_status] || media.compression_status;
                        row.appendChild(status);
                    }
                    if (media.compressed_source) {
                        const compressed = document.createElement('a');
                        compressed.href = media.compressed_source;
                        compressed.target = '_blank';
                        compressed.textContent = '打开压缩副本';
                        row.appendChild(compressed);
                    }
                    container.appendChild(row);
                });
            }
            box.dataset.hydrated = 'true';
        });
    });
}

function exampleHtml(example) {
    return `<details class="example" data-example="${example.id}">
        <summary>${esc(example.title || 'Example')} · 评分 ${example.rating || 0}/5 · ${esc(example.generator_model || '未记录模型')} · ${example.results.length} 个结果</summary>
        <div class="stack">
            <div class="row"><input class="field ex-title" value="${esc(example.title)}"><button class="secondary ex-save">保存</button><button class="danger ex-delete">删除</button></div>
            <label>实际使用的 Prompt<textarea class="field ex-input" rows="4">${esc(example.input_text)}</textarea></label>
            <label>文本结果 / 期望输出<textarea class="field ex-output" rows="3">${esc(example.output_text)}</textarea></label>
            <label>生成模型 / Workflow ID<input class="field ex-model" value="${esc(example.generator_model)}"></label>
            <div class="grid2"><label>评分<input class="field ex-rating" type="number" min="0" max="5" value="${example.rating || 0}"></label><label>Seed<input class="field ex-seed" value="${esc(example.seed)}"></label></div>
            <label>生成参数<textarea class="field ex-params" rows="3">${esc(example.generation_params)}</textarea></label>
            <label>说明<textarea class="field ex-notes" rows="2">${esc(example.notes)}</textarea></label>
            <div class="stack" data-example-references></div>
            <div class="stack" data-example-results></div>
            <div>${example.attachments.map(attachmentHtml).join('')}</div>
        </div></details>`;
}

function bindExamples() {
    document.querySelectorAll('.ex-delete').forEach(button => {
        button.onclick = async () => {
            if (!confirm('删除此 Example 及其保存的资源？')) return;
            try {
                await api('/api/examples/'+button.closest('.example').dataset.example,{method:'DELETE'});
                await open(state.current.id);
            } catch (error) { alert(error.message); }
        };
    });
    document.querySelectorAll('.ex-save').forEach(button => {
        button.onclick = async () => {
            const box = button.closest('.example');
            const example = state.current.examples.find(item=>item.id===box.dataset.example);
            const value = selector=>box.querySelector(selector).value;
            try {
                await api('/api/examples/'+example.id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({
                    title:value('.ex-title'),input_text:value('.ex-input'),output_text:value('.ex-output'),generator_model:value('.ex-model'),
                    rating:Number(value('.ex-rating')),seed:value('.ex-seed'),generation_params:value('.ex-params'),notes:value('.ex-notes'),
                    references:example.references,results:example.results,position:example.position
                })});
                await open(state.current.id);
            } catch (error) { alert(error.message); }
        };
    });
}
