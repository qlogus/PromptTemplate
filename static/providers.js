async function openProviderSettings() {
    const items = await api('/api/providers');
    let editing = null;
    let models = [];
    const kinds = {text:'文本 / Prompt', multimodal:'多模态模型', image:'图片生成 / Edit'};
    $('modal').innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="Provider 设置">
        <h2>Provider 设置</h2>
        ${Object.entries(kinds).map(([kind, title]) => `<h3>${title}</h3>${items.filter(item => item.provider_kind === kind).map(item => `<p>${esc(item.name)} · ${esc(item.model)}${item.is_default ? ' · 当前默认' : ''} <button class="secondary" data-edit="${item.id}">编辑</button> <button class="danger" data-delete="${item.id}">删除</button></p>`).join('') || '<p class="muted">暂无 provider</p>'}`).join('')}
        <form id="providerForm" class="stack">
            <h3 id="providerFormTitle">新增 Provider</h3>
            <label>名称<input id="pName" class="field" required></label>
            <label>类型<select id="pKind" class="field">${Object.entries(kinds).map(([kind,title]) => `<option value="${kind}">${title}</option>`).join('')}</select></label>
            <label id="multimodalOptions">图片输入格式<select id="pImageInput" class="field"><option value="both">URL 或上传</option><option value="url">仅公网 URL</option><option value="base64">仅上传</option></select></label>
            <div id="imageOptions" class="stack" hidden>
                <label>参考图提交方式<select id="pReferenceProtocol" class="field"><option value="multipart_edit">上传图片（也可从 URL 读取后上传）</option><option value="json_url">直接发送公网 URL</option><option value="none">不支持参考图</option></select></label>
                <div id="urlProtocolOptions" class="stack" hidden>
                    <label>参考图接口路径<input id="pReferenceEndpoint" class="field" value="/images/edits" placeholder="/images/edits"></label>
                    <label>URL 参数名<input id="pReferenceField" class="field" value="image" placeholder="image"></label>
                    <span class="muted">按接口文档填写；URL 作为 JSON 字符串发送到此参数。路径相对于 Base URL。</span>
                </div>
            </div>
            <label>Base URL<input id="pUrl" class="field" type="url" required placeholder="https://example.com/v1"></label>
            <label>模型<div class="row"><input id="pModel" class="field" required><button id="loadModels" type="button" class="secondary">加载模型</button></div></label>
            <select id="modelsList" class="field" hidden></select>
            <label>API Key<input id="pKey" type="password" class="field"></label>
            <label><input type="checkbox" id="verifyTls" checked> 严格校验 TLS 证书</label>
            <label><input type="checkbox" id="pDefault"> 设为此类型默认</label>
            <div id="providerMessage" class="muted" aria-live="polite"></div>
            <button id="pSave">保存 provider</button>
            <button id="pNew" type="button" class="secondary">新增另一项</button>
            <button id="pClose" type="button" class="secondary">关闭</button>
        </form>
    </div>`;
    const updateOptions = () => {
        $('multimodalOptions').hidden = $('pKind').value !== 'multimodal';
        $('imageOptions').hidden = $('pKind').value !== 'image';
        $('urlProtocolOptions').hidden = $('pReferenceProtocol').value !== 'json_url';
    };
    $('pKind').onchange = updateOptions;
    $('pReferenceProtocol').onchange = updateOptions;
    updateOptions();
    $('modal').querySelectorAll('[data-edit]').forEach(button => {
        button.onclick = () => {
            editing = items.find(item => item.id === button.dataset.edit);
            $('providerFormTitle').textContent = '编辑 ' + editing.name;
            for (const [id,key] of Object.entries({pName:'name',pKind:'provider_kind',pUrl:'base_url',pModel:'model',pKey:'api_key',pImageInput:'image_input',pReferenceProtocol:'reference_protocol',pReferenceEndpoint:'reference_endpoint',pReferenceField:'reference_field'})) $(id).value = editing[key];
            $('verifyTls').checked = Boolean(editing.verify_tls);
            $('pDefault').checked = Boolean(editing.is_default);
            models = [];
            $('modelsList').hidden = true;
            updateOptions();
            $('providerFormTitle').scrollIntoView({block:'nearest'});
        };
    });
    $('modal').querySelectorAll('[data-delete]').forEach(button => {
        button.onclick = async () => {
            try { await api('/api/providers/' + button.dataset.delete, {method:'DELETE'}); await openProviderSettings(); }
            catch (error) { $('providerMessage').textContent = error.message; }
        };
    });
    const renderModels = () => {
        const tokens = $('pModel').value.toLowerCase().trim().split(/\s+/).filter(Boolean);
        const matches = models.filter(model => tokens.every(token => model.toLowerCase().includes(token)));
        $('modelsList').innerHTML = `<option value="">匹配模型：${matches.length}</option>` + matches.map(model => `<option value="${esc(model)}">${esc(model)}</option>`).join('');
        $('modelsList').hidden = !models.length;
    };
    $('pModel').oninput = renderModels;
    $('modelsList').onchange = () => { if ($('modelsList').value) $('pModel').value = $('modelsList').value; };
    $('loadModels').onclick = async () => {
        $('loadModels').disabled = true;
        try {
            const response = await api('/api/providers/models', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider_id:editing?.id,base_url:$('pUrl').value,api_key:$('pKey').value,verify_tls:$('verifyTls').checked})});
            models = response.models;
            renderModels();
            $('providerMessage').textContent = `已加载 ${models.length} 个模型，输入关键词筛选。`;
        } catch (error) { $('providerMessage').textContent = error.message; }
        finally { $('loadModels').disabled = false; }
    };
    $('providerForm').onsubmit = async event => {
        event.preventDefault();
        $('pSave').disabled = true;
        try {
            const kind = $('pKind').value;
            await api(editing ? '/api/providers/' + editing.id : '/api/providers', {method:editing?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
                name:$('pName').value, provider_kind:kind, base_url:$('pUrl').value, model:$('pModel').value, api_key:$('pKey').value,
                image_input:$('pImageInput').value, verify_tls:$('verifyTls').checked, is_default:$('pDefault').checked,
                capabilities:kind==='image'?['image']:kind==='text'?['text']:['text','vision'],
                reference_protocol:$('pReferenceProtocol').value, reference_endpoint:$('pReferenceEndpoint').value, reference_field:$('pReferenceField').value
            })});
            await openProviderSettings();
            if ($('generationPanel')) {
                $('generationPanel').dispatchEvent(new Event('providers-updated'));
            }
        } catch (error) { $('providerMessage').textContent = error.message; }
        finally { $('pSave').disabled = false; }
    };
    $('pClose').onclick = () => $('modal').replaceChildren();
    $('pNew').onclick = () => {
        editing = null;
        $('providerForm').reset();
        $('providerFormTitle').textContent = '新增 Provider';
        models = [];
        renderModels();
        updateOptions();
    };
}
