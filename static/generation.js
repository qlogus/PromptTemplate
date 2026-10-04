function bindGeneration() {
    const promptId = state.current.id;
    const panel = document.createElement('section');
    panel.className = 'section workflow-panel generation-panel';
    panel.id = 'generationPanel';
    panel.innerHTML = `
        <h2>使用当前 Prompt 生图 / Edit</h2>
        <div class="stack">
            <div class="actions">
                <button class="workflow-shortcut" data-language="zh">使用中文 Prompt</button>
                <button class="workflow-shortcut" data-language="en">Use English Prompt</button>
                <button class="workflow-shortcut" data-language="ja">日本語 Prompt を使う</button>
            </div>
            <label>生图 Provider<select id="imageProvider" class="field"><option value="">加载中…</option></select></label>
            <textarea id="generationPrompt" class="field" rows="8" placeholder="实际使用的 Prompt"></textarea>
            <label>生图尺寸<input id="generationSize" class="field" list="generationSizes" placeholder="默认不指定；可输入宽x高，如 1536x1024">
                <datalist id="generationSizes">
                    <option value="auto">自动</option>
                    <option value="1024x1024">正方形</option>
                    <option value="1536x1024">横向</option>
                    <option value="1024x1536">纵向</option>
                    <option value="1920x1080">Full HD 横向</option>
                    <option value="2560x1440">2K 横向</option>
                    <option value="3840x2160">4K 横向</option>
                    <option value="1792x1024">宽幅</option>
                    <option value="1024x1792">长幅</option>
                </datalist>
            </label>
            <span class="muted">留空使用服务默认值；可用尺寸及 auto 支持以所选模型为准。</span>
            <div id="generationReferenceInputs" hidden></div>
            <button id="generateImageBtn" class="workflow-execute" disabled>生成图片</button>
            <div id="generationResult" aria-live="polite"></div>
            <label>生成模型 / Workflow ID<input id="generationModel" class="field"></label>
            <label>Seed（记录用，可选）<input id="generationSeed" class="field"></label>
            <label>生成参数（记录用，可选）<textarea id="generationParams" class="field" rows="3"></textarea></label>
            <label>评分<input id="generationRating" class="field" type="number" min="0" max="5" value="0"></label>
            <label>说明<textarea id="generationNotes" class="field" rows="3"></textarea></label>
            <button id="saveGenerationExample" class="secondary" disabled>保存为 Example</button>
        </div>`;
    $('editor').appendChild(panel);
    bindGenerationPresets(panel.querySelector('#generationPrompt'));
    let providers = [];
    let snapshot = null;
    const resultMedia = new MediaUrls();
    panel.addEventListener('generation-dispose', () => resultMedia.dispose());
    let busy = false;
    const references = new GenerationReferences(panel.querySelector('#generationReferenceInputs'));
    panel.addEventListener('generation-dispose', () => references.dispose());
    const providerSelect = panel.querySelector('#imageProvider');
    const resultPanel = panel.querySelector('#generationResult');
    const generateButton = panel.querySelector('#generateImageBtn');
    const saveButton = panel.querySelector('#saveGenerationExample');
    const modelInput = panel.querySelector('#generationModel');

    providerSelect.onchange = () => {
        const provider = providers.find(item => item.id === providerSelect.value);
        references.setProvider(provider);
        generateButton.disabled = !provider || busy;
    };
    const loadProviders = () => api('/api/providers').then(items => {
        if (!panel.isConnected) return;
        providers = items.filter(item => item.provider_kind === 'image');
        providerSelect.innerHTML = providers.map(item => `<option value="${esc(item.id)}" ${item.is_default ? 'selected' : ''}>${esc(item.name)} · ${esc(item.model)}</option>`).join('') || '<option value="">请先配置图片生成 provider</option>';
        providerSelect.onchange();
    }).catch(error => { resultPanel.textContent = error.message; });
    panel.addEventListener('providers-updated', loadProviders);
    loadProviders();
    panel.querySelectorAll('[data-language]').forEach(button => {
        button.onclick = () => {
            const fields = {zh: 'contentZh', en: 'contentEn', ja: 'contentJa'};
            panel.querySelector('#generationPrompt').value = $(fields[button.dataset.language]).value;
        };
    });
    generateButton.onclick = async () => {
        const provider = providers.find(item => item.id === providerSelect.value);
        const prompt = panel.querySelector('#generationPrompt').value.trim();
        const size = panel.querySelector('#generationSize').value.trim();
        if (!provider || !prompt) { resultPanel.textContent = '请选择 provider 并填写 Prompt。'; return; }
        if (size && !/^(auto|[1-9][0-9]*x[1-9][0-9]*)$/.test(size)) { resultPanel.textContent = '尺寸请填写 auto 或宽x高，例如 1536x1024。'; return; }
        busy = true;
        generateButton.disabled = true;
        saveButton.disabled = true;
        snapshot = null;
        resultPanel.textContent = '生成中…';
        try {
            const referenceSnapshot = await references.snapshot();
            const request = {prompt_id: promptId, provider_id: provider.id, prompt, references:referenceSnapshot, edit:Boolean(referenceSnapshot.length)};
            if (size) request.size = size;
            const data = await api('/api/generate/image', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(request)});
            if (!panel.isConnected) return;
            const results = data.results.map(item => item.url || (item.b64_json ? 'data:image/png;base64,' + item.b64_json : '')).filter(Boolean);
            if (!results.length) throw Error('Provider 没有返回图片结果');
            snapshot = {prompt, references: referenceSnapshot, results, model: provider.model, size: size || null};
            modelInput.value = provider.model;
            resultPanel.replaceChildren();
            resultMedia.dispose();
            results.forEach(url => {
                const image = document.createElement('img');
                image.src = resultMedia.url(url);
                image.alt = '临时生成结果';
                image.className = 'generation-preview';
                resultPanel.appendChild(image);
                const link = document.createElement('a');
                link.href = image.src;
                link.target = '_blank';
                link.rel = 'noopener';
                link.textContent = '打开原图';
                resultPanel.appendChild(link);
            });
            saveButton.disabled = false;
        } catch (error) {
            resultPanel.textContent = error.message;
        } finally {
            busy = false;
            generateButton.disabled = false;
        }
    };
    saveButton.onclick = async () => {
        if (!snapshot) return;
        saveButton.disabled = true;
        try {
            const rating = Number(panel.querySelector('#generationRating').value);
            if (!Number.isInteger(rating) || rating < 0 || rating > 5) throw Error('评分须为 0–5 的整数');
            await api('/api/prompts/' + promptId + '/examples', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
                title:'生图 Example', input_text:snapshot.prompt, references:snapshot.references.map((source,index)=>({kind:'image',source,label:`图${index+1}`})), results:snapshot.results.map(source=>({kind:'image',source})),
                generator_model:modelInput.value, rating, seed:panel.querySelector('#generationSeed').value,
                generation_params:JSON.stringify({size:snapshot.size, notes:panel.querySelector('#generationParams').value}), notes:panel.querySelector('#generationNotes').value
            })});
            if (state.current?.id === promptId && panel.isConnected) {
                await open(promptId);
            }
            saveButton.textContent = '已保存为 Example';
        } catch (error) {
            saveButton.disabled = false;
            resultPanel.textContent = error.message;
        }
    };
}
