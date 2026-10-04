function bindRevisionPresets() {
    const input = $('aiRequest');
    const section = input.closest('.section');
    section.classList.add('revision-panel', 'workflow-panel');
    const execute = $('aiBtn');
    execute.textContent = '请求 AI 修改建议';
    execute.classList.add('workflow-execute');
    const toolbar = document.createElement('div');
    toolbar.className = 'stack';
    toolbar.innerHTML = `
        <div class="row preset-heading"><span>常用修改建议</span><div class="actions">
            <button type="button" class="secondary" data-save-preset>保存为常用建议</button>
            <button type="button" class="secondary" data-manage-presets>管理</button>
        </div></div>
        <div class="actions preset-shortcuts" aria-label="常用修改建议"></div>
        <span class="muted" role="status"></span>`;
    input.before(toolbar);
    const shortcuts = toolbar.querySelector('.preset-shortcuts');
    const status = toolbar.querySelector('[role="status"]');
    let presets = [];
    const resize = () => {
        input.style.height = 'auto';
        input.style.height = input.scrollHeight + 'px';
    };
    input.addEventListener('input', resize);
    const refresh = async () => {
        presets = await api('/api/revision-presets');
        shortcuts.replaceChildren();
        status.textContent = presets.length ? '' : '尚未保存常用建议';
        for (const preset of presets) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'workflow-shortcut';
            button.textContent = preset.title;
            button.title = preset.content;
            button.onclick = () => {
                if (input.value.trim() && input.value !== preset.content && !confirm('用这条常用建议替换当前输入内容？')) return;
                input.value = preset.content;
                input.dispatchEvent(new Event('input', {bubbles:true}));
                input.focus();
            };
            shortcuts.appendChild(button);
        }
    };
    const showDialog = (preset = null, manage = false) => {
        const dialog = document.createElement('dialog');
        dialog.className = 'preset-dialog';
        dialog.innerHTML = `<form class="stack">
            <h2>${manage ? '管理常用修改建议' : preset ? '编辑常用修改建议' : '保存常用修改建议'}</h2>
            ${manage ? '<div class="stack preset-list"></div>' : `
                <label>标题<input name="title" class="field" required maxlength="120"></label>
                <label>修改建议<textarea name="content" class="field" rows="9" required></textarea></label>`}
            <div role="status" class="muted"></div>
            <div class="actions">${manage ? '' : '<button type="submit">保存</button>'}<button type="button" class="secondary" data-close>关闭</button></div>
        </form>`;
        document.body.appendChild(dialog);
        dialog.addEventListener('close', () => dialog.remove(), {once:true});
        dialog.querySelector('[data-close]').onclick = () => dialog.close();
        const message = dialog.querySelector('[role="status"]');
        if (manage) {
            const list = dialog.querySelector('.preset-list');
            if (!presets.length) list.textContent = '尚未保存常用建议';
            for (const item of presets) {
                const row = document.createElement('div');
                row.className = 'row preset-heading';
                const title = document.createElement('span');
                title.textContent = item.title;
                const actions = document.createElement('div');
                actions.className = 'actions';
                const edit = document.createElement('button');
                edit.type = 'button';
                edit.className = 'secondary';
                edit.textContent = '编辑';
                edit.onclick = () => { dialog.close(); showDialog(item); };
                const remove = document.createElement('button');
                remove.type = 'button';
                remove.className = 'danger';
                remove.textContent = '删除';
                remove.onclick = async () => {
                    if (!confirm(`删除常用建议“${item.title}”？`)) return;
                    remove.disabled = true;
                    try {
                        await api('/api/revision-presets/' + item.id, {method:'DELETE'});
                        await refresh();
                        row.remove();
                        if (!presets.length) list.textContent = '尚未保存常用建议';
                    } catch (error) { message.textContent = error.message; }
                    finally { remove.disabled = false; }
                };
                actions.append(edit, remove);
                row.append(title, actions);
                list.appendChild(row);
            }
        } else {
            const form = dialog.querySelector('form');
            form.elements.title.value = preset?.title || '';
            form.elements.content.value = preset?.content || input.value;
            form.onsubmit = async event => {
                event.preventDefault();
                const title = form.elements.title.value.trim();
                const content = form.elements.content.value.trim();
                if (!title || !content) { message.textContent = '请填写标题和修改建议'; return; }
                const save = form.querySelector('[type="submit"]');
                save.disabled = true;
                try {
                    await api('/api/revision-presets' + (preset ? '/' + preset.id : ''), {
                        method:preset ? 'PUT' : 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title, content})
                    });
                    await refresh();
                    dialog.close();
                } catch (error) { message.textContent = error.message; }
                finally { save.disabled = false; }
            };
        }
        dialog.showModal();
    };
    toolbar.querySelector('[data-save-preset]').onclick = () => showDialog();
    toolbar.querySelector('[data-manage-presets]').onclick = () => showDialog(null, true);
    refresh().catch(error => { status.textContent = error.message; });
}
