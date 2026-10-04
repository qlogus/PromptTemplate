function bindGenerationPresets(input) {
    const host = input.closest('.stack');
    const toolbar = document.createElement('div');
    toolbar.className = 'generation-presets stack';
    toolbar.innerHTML = `<div class="row preset-heading"><span>常用生图 Prompt</span><div class="actions"><button type="button" class="secondary" data-save-generation-preset>保存模板</button><button type="button" class="secondary" data-manage-generation-presets>管理</button></div></div><div class="actions preset-shortcuts"></div><span class="muted" role="status"></span>`;
    input.before(toolbar);
    const shortcuts = toolbar.querySelector('.preset-shortcuts');
    const status = toolbar.querySelector('[role="status"]');
    let presets = [];
    const refresh = async () => {
        presets = await api('/api/generation-presets');
        shortcuts.replaceChildren();
        status.textContent = presets.length ? '' : '尚未保存生图模板';
        for (const preset of presets) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'generation-preset-shortcut';
            button.textContent = preset.title;
            button.title = preset.content;
            button.onclick = () => {
                if (input.value.trim() && input.value !== preset.content && !confirm('用这个生图模板替换当前 Prompt？')) return;
                input.value = preset.content;
                input.dispatchEvent(new Event('input', {bubbles:true}));
                input.focus();
            };
            shortcuts.appendChild(button);
        }
    };
    const dialog = (preset = null, manage = false) => {
        const node = document.createElement('dialog');
        node.className = 'preset-dialog';
        node.innerHTML = `<form class="stack"><h2>${manage ? '管理生图 Prompt 模板' : preset ? '编辑生图 Prompt 模板' : '保存生图 Prompt 模板'}</h2>${manage ? '<div class="stack preset-list"></div>' : '<label>标题<input name="title" class="field" maxlength="120" required></label><label>Prompt<textarea name="content" class="field" rows="10" required></textarea></label>'}<div class="muted" role="status"></div><div class="actions">${manage ? '' : '<button type="submit">保存</button>'}<button type="button" class="secondary" data-close>关闭</button></div></form>`;
        document.body.appendChild(node);
        node.addEventListener('close', () => node.remove(), {once:true});
        node.querySelector('[data-close]').onclick = () => node.close();
        const message = node.querySelector('[role="status"]');
        if (manage) {
            const list = node.querySelector('.preset-list');
            if (!presets.length) list.textContent = '尚未保存生图模板';
            presets.forEach(item => {
                const row = document.createElement('div'); row.className = 'row preset-heading';
                const title = document.createElement('span'); title.textContent = item.title;
                const actions = document.createElement('div'); actions.className = 'actions';
                const edit = document.createElement('button'); edit.type='button'; edit.className='secondary'; edit.textContent='编辑'; edit.onclick=()=>{node.close();dialog(item)};
                const remove = document.createElement('button'); remove.type='button'; remove.className='danger'; remove.textContent='删除'; remove.onclick=async()=>{if(!confirm(`删除生图模板“${item.title}”？`))return;try{await api('/api/generation-presets/'+item.id,{method:'DELETE'});await refresh();row.remove();if(!presets.length)list.textContent='尚未保存生图模板';}catch(error){message.textContent=error.message;}};
                actions.append(edit,remove); row.append(title,actions); list.appendChild(row);
            });
        } else {
            const form = node.querySelector('form'); form.elements.title.value=preset?.title||''; form.elements.content.value=preset?.content||input.value;
            form.onsubmit=async event=>{event.preventDefault();const title=form.elements.title.value.trim(),content=form.elements.content.value.trim();if(!title||!content){message.textContent='请填写标题和 Prompt';return;}try{await api('/api/generation-presets'+(preset?'/'+preset.id:''),{method:preset?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,content})});await refresh();node.close();}catch(error){message.textContent=error.message;}};
        }
        node.showModal();
    };
    toolbar.querySelector('[data-save-generation-preset]').onclick = () => dialog();
    toolbar.querySelector('[data-manage-generation-presets]').onclick = () => dialog(null, true);
    refresh().catch(error => {status.textContent=error.message;});
}
